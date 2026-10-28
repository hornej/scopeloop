"""Leased MAX31856 telemetry, diagnostics and explicitly acknowledged recovery."""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
import uuid
from pathlib import Path

import serial
import typer

from scopeloop.resources import HostLease
from scopeloop.session import TimelineEvent

PROTOCOL = "scopeloop-max31856-v1"
app = typer.Typer(help="Capture and diagnose a dedicated CircuitPython MAX31856 logger.")


def strict_json(text):
    def reject(value):
        raise ValueError("Nonfinite JSON constant: " + value)

    return json.loads(text, parse_constant=reject)


class TemperatureGuard:
    """No actuation: the owning DUT controller must stop on any exception."""

    def __init__(self, device, high_c, stale_s=3.0):
        if isinstance(high_c, bool) or not math.isfinite(high_c) or not 0 < stale_s <= 3:
            raise ValueError("Finite limit and freshness in (0, 3] seconds required")
        self.device, self.high_c, self.stale_s = device, high_c, stale_s
        self.last = None
        self.received_at = None

    def accept(self, sample, now):
        if sample.get("protocol") != PROTOCOL or sample.get("device") != self.device:
            raise ValueError("Unexpected logger protocol/device")
        if sample.get("valid") is not True or sample.get("latched") is not False:
            raise ValueError(
                "Logger fault: " + str(sample.get("error", sample.get("fault_detail")))
            )
        if sample.get("fault_mask") != 0 or sample.get("high_limit_c") != self.high_c:
            raise ValueError("Hardware fault or unexpected configured cutoff")
        if any(sample.get("faults", {}).values()):
            raise ValueError("Hardware fault flags set")
        for key in ("tc_c", "cj_c", "uptime_s"):
            value = sample.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("Invalid numeric field: " + key)
            if not math.isfinite(value):
                raise ValueError("Nonfinite field: " + key)
        if sample["tc_c"] >= self.high_c:
            raise ValueError("Temperature cutoff reached")
        if not isinstance(sample.get("boot"), str) or not sample["boot"]:
            raise ValueError("Missing boot identity")
        if type(sample.get("seq")) is not int or sample["seq"] < 1:
            raise ValueError("Invalid sequence")
        if self.last is not None:
            if sample["boot"] != self.last["boot"]:
                raise ValueError("Logger rebooted during capture")
            if sample["seq"] != self.last["seq"] + 1:
                raise ValueError("Missing/duplicate/out-of-order temperature sample")
            if sample["uptime_s"] <= self.last["uptime_s"]:
                raise ValueError("Logger monotonic time did not advance")
            if sample["uptime_s"] - self.last["uptime_s"] > self.stale_s:
                raise ValueError("Source sample gap exceeds freshness limit")
        self.last, self.received_at = sample, now
        return sample

    def check_fresh(self, now):
        if self.received_at is None or now - self.received_at > self.stale_s:
            raise ValueError("Temperature data stale/unavailable")


class ThermocoupleConnection:
    """Keep one lease for the complete workflow, including recovery verification."""

    def __init__(self, port, device, output):
        self.port, self.device = port, device
        self.output = Path(output)
        self.lease = HostLease("serial:" + port)
        self.serial = None
        self.raw = self.events = None
        self.pending = b""

    def __enter__(self):
        self.lease.acquire()
        try:
            self.output.mkdir(parents=True, exist_ok=False)
            self.raw = (self.output / "serial.bin").open("xb")
            self.events = (self.output / "timeline.jsonl").open("x", encoding="utf-8")
            self.serial = serial.Serial(port=None, baudrate=115200, timeout=0.1)
            self.serial.dtr = False
            self.serial.rts = False
            if os.name != "nt":
                self.serial.exclusive = True
            self.serial.port = self.port
            self.serial.open()
            self.serial.reset_input_buffer()
            self.event("connected", {"port": self.port, "expected_device": self.device})
            return self
        except BaseException:
            self.__exit__(*__import__("sys").exc_info())
            raise

    def event(self, name, data):
        event = TimelineEvent(time.time(), "serial", name, data)
        self.events.write(event.to_json_line() + "\n")
        self.events.flush()

    def read(self, deadline):
        while time.monotonic() < deadline:
            if b"\n" in self.pending:
                line, self.pending = self.pending.split(b"\n", 1)
                start = line.find(b"{")
                if start < 0:
                    continue  # CircuitPython banners are preserved in raw evidence.
                row = strict_json(line[start:].decode("utf-8"))
                if not isinstance(row, dict):
                    raise ValueError("Expected JSON object")
                self.event("rx", {"monotonic": time.monotonic(), "record": row})
                if row.get("kind") in ("identity", "temperature", "thermocouple"):
                    if row.get("device") != self.device or row.get("protocol") != PROTOCOL:
                        raise ValueError("Unexpected logger identity/protocol")
                    return row
                continue
            chunk = self.serial.read(1024)
            if chunk:
                self.raw.write(chunk)
                self.raw.flush()
                self.pending += chunk
                if len(self.pending) > 16384:
                    raise ValueError("Unterminated/oversized serial record")
        raise TimeoutError("Logger response deadline expired")

    def request(self, action, **kwargs):
        request_id = uuid.uuid4().hex
        request = {"thermocouple": action, "id": request_id, **kwargs}
        encoded = (json.dumps(request, allow_nan=False) + "\n").encode()
        self.event("tx", request)
        if self.serial.write(encoded) != len(encoded):
            raise OSError("Incomplete command write")
        deadline = time.monotonic() + 3
        while True:
            row = self.read(deadline)
            if row.get("kind") == "thermocouple" and row.get("id") == request_id:
                if row.get("ok") is not True:
                    raise ValueError("Command rejected: " + str(row.get("error")))
                return row

    def recover(self, boot, fault_id, high_c, dut_idle):
        if dut_idle is not True:
            raise ValueError("Explicit idle handoff required; never recover during active heating")
        status = self.request("status")
        if status.get("boot") != boot or status.get("fault_id") != fault_id:
            raise ValueError("Recovery token is stale")
        ack = self.request("recover", boot=boot, fault_id=fault_id, dut_idle=True)
        deadline = time.monotonic() + 8
        while True:
            row = self.read(min(deadline, time.monotonic() + 3))
            if row.get("kind") != "temperature":
                continue
            if row.get("boot") != boot:
                raise ValueError("Unexpected reboot during recovery")
            recovery = row.get("recovery", {})
            if recovery.get("id") != ack["id"]:
                raise ValueError("Unexpected recovery identity")
            if recovery.get("state") == "failed":
                raise ValueError("Recovery failed: " + str(row.get("error")))
            if recovery.get("state") == "verified":
                if recovery.get("good_samples", 0) < 3:
                    raise ValueError("Insufficient recovery conversions")
                TemperatureGuard(self.device, high_c).accept(row, time.monotonic())
                return row
            if time.monotonic() >= deadline:
                raise TimeoutError("Recovery verification deadline expired")

    def __exit__(self, kind, error, traceback):
        cleanup_error = None
        actions = []
        if self.events:
            actions.append(lambda: self.event("closed", {"error": str(error) if error else None}))
        actions.extend(
            handle.close for handle in (self.serial, self.raw, self.events) if handle is not None
        )
        actions.append(self.lease.release)
        for action in actions:
            try:
                action()
            except Exception as failure:
                cleanup_error = cleanup_error or failure
        if self.output.exists() and self.raw:
            files = {}
            for name in ("serial.bin", "timeline.jsonl"):
                path = self.output / name
                if path.exists():
                    files[name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            manifest = {
                "status": "failed" if error or cleanup_error else "complete",
                "kind": "thermocouple",
                "port": self.port,
                "expected_device": self.device,
                "files": files,
                "limitations": "No calibrated accuracy or DUT power cutoff is provided.",
            }
            (self.output / "manifest.json").write_text(json.dumps(manifest, indent=2))
        if cleanup_error is not None and error is None:
            raise cleanup_error


@app.command()
def capture(port: str, device: str, output: Path, high_c: float, seconds: float = 10):
    """Passive bounded capture; any invalid/stale sample fails immediately."""
    if not math.isfinite(seconds) or not 0 < seconds <= 3600:
        raise typer.BadParameter("seconds must be in (0, 3600]")
    guard = TemperatureGuard(device, high_c)
    with ThermocoupleConnection(port, device, output) as connection:
        started = time.monotonic()
        end = started + seconds
        while time.monotonic() < end:
            fresh_deadline = (guard.received_at if guard.received_at is not None else started) + 3
            try:
                row = connection.read(min(end, fresh_deadline))
            except TimeoutError:
                guard.check_fresh(time.monotonic())
                if time.monotonic() >= end:
                    break
                raise
            if row.get("kind") == "temperature":
                guard.accept(row, time.monotonic())
                typer.echo(json.dumps(row, allow_nan=False))
        guard.check_fresh(time.monotonic())


@app.command()
def diagnose(port: str, device: str, output: Path):
    """Read in-band status/registers. Does not enter REPL or restart the logger."""
    with ThermocoupleConnection(port, device, output) as connection:
        typer.echo(json.dumps(connection.request("status"), allow_nan=False))


@app.command()
def recover(
    port: str,
    device: str,
    output: Path,
    boot: str,
    fault_id: str,
    high_c: float,
    dut_idle: bool = False,
):
    """One explicit reinitialization, followed by three healthy conversions."""
    if not dut_idle:
        raise typer.BadParameter("--dut-idle requires an actual idle handoff")
    with ThermocoupleConnection(port, device, output) as connection:
        typer.echo(json.dumps(connection.recover(boot, fault_id, high_c, dut_idle)))


if __name__ == "__main__":
    app()
