"""Revision-specific Zero Pi HAT control, held by a local persistent owner.

GPIO release/reboot can enable this board's output. Clients never own the GPIO
handle, and disconnecting a client never releases it. Run the owner on the Pi.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, StrictBool, model_validator

from scopeloop.config import ZeroPiHatConfig
from scopeloop.resources import HostLease, ResourceManager

PROFILE = {
    "revision": "0.1.0",
    "output": "switched_12v",
    "enable_gpio": 7,
    "enable_header_pin": 26,
    "enable_active_high": True,
    "fault_gpio": 8,
    "fault_header_pin": 24,
    "fault_active_low": True,
    "connector": "J9 pin2 +12V_Out, pin1 GND",
    "switch": "U1 TPS259631, EN pin3, FLT pin6",
    "unowned_default": "enabled via external 4.75k pull-up",
    "shared_output": "J9 and 12 V bus/touchscreen feed",
    "schematic_sha256": "b04db3a50a02e80988f62fe3383a0dec4de5d4a65d5f854974cd3ab93fc229de",
}
CONSUMER = "scopeloop-zero-pi-hat"


def config_identity(config: ZeroPiHatConfig) -> str:
    return hashlib.sha256(config.model_dump_json().encode()).hexdigest()


class HatRequest(BaseModel, extra="forbid"):
    operation: Literal["status", "output", "cycle"]
    enabled: StrictBool | None = None
    off_seconds: float = Field(default=3, ge=0.5, le=10, allow_inf_nan=False)
    settle_seconds: float = Field(default=0.5, ge=0, le=10, allow_inf_nan=False)

    @model_validator(mode="after")
    def check_output(self):
        if (self.operation == "output") != (self.enabled is not None):
            raise ValueError("enabled is required only for output")
        return self


class GpiodBackend:
    """libgpiod 2.x backend. Inspection never requests or reconfigures a line."""

    def __init__(self, config: ZeroPiHatConfig):
        self.config = config
        self.request = None

    def inspect(self) -> dict:
        if sys.platform != "linux":
            raise RuntimeError("Zero Pi HAT GPIO access runs locally on its Linux Pi host")
        try:
            import gpiod
        except ImportError as exc:
            raise RuntimeError("Install ScopeLoop's zero-pi-hat extra (libgpiod 2.x)") from exc
        if not hasattr(gpiod, "request_lines"):
            raise RuntimeError("libgpiod Python API 2.x is required")
        model = Path("/proc/device-tree/model").read_text().rstrip("\0")
        serial = Path("/proc/device-tree/serial-number").read_text().rstrip("\0")
        if not model.startswith("Raspberry Pi 4 Model B"):
            raise RuntimeError("This GPIO mapping is currently qualified only on Raspberry Pi 4")
        if self.config.expected_pi_serial and serial != self.config.expected_pi_serial:
            raise RuntimeError("Pi serial does not match the configured fixture host")
        with gpiod.Chip(self.config.gpiochip) as chip:
            if chip.get_info().label != "pinctrl-bcm2711":
                raise RuntimeError("Unexpected GPIO chip; refusing to assume BCM numbering")
            lines = {}
            for offset in (7, 8):
                line = chip.get_line_info(offset)
                if line.name != f"GPIO{offset}":
                    raise RuntimeError(f"GPIO line {offset} identity mismatch")
                mux = subprocess.check_output(
                    ["pinctrl", "get", str(offset)], text=True, timeout=3
                ).strip()
                lines[str(offset)] = {"used": line.used, "consumer": line.consumer, "mux": mux}
        return {"model": model, "pi_serial": serial, "gpiod_version": gpiod.__version__,
                "profile": PROFILE, "lines": lines, "rail_voltage_v": None}

    def claim_off(self):
        import gpiod
        from gpiod.line import Bias, Direction, Value

        info = self.inspect()
        for offset, line in info["lines"].items():
            if line["used"]:
                raise RuntimeError(f"GPIO{offset} already owned by {line['consumer']}; no takeover")
            if not re.match(rf"^{offset}:\s+(ip|op)\b", line["mux"]):
                raise RuntimeError(f"GPIO{offset} has an alternate function; no mux override")
        # Initial low is part of the atomic kernel request, not a later write.
        self.request = gpiod.request_lines(
            self.config.gpiochip, consumer=CONSUMER,
            config={7: gpiod.LineSettings(direction=Direction.OUTPUT, bias=Bias.PULL_UP,
                                          output_value=Value.INACTIVE),
                    8: gpiod.LineSettings(direction=Direction.INPUT, bias=Bias.PULL_UP)},
        )

    def set_enabled(self, enabled: bool):
        from gpiod.line import Value

        self.request.set_value(7, Value.ACTIVE if enabled else Value.INACTIVE)

    def read(self) -> dict:
        from gpiod.line import Value

        return {"enable_gpio_high": self.request.get_value(7) == Value.ACTIVE,
                "fault_active": self.request.get_value(8) == Value.INACTIVE}

    def release(self):
        if self.request is not None:
            self.request.release()
            self.request = None


class HatOwner:
    """Own both GPIO lines and a host lease for the whole service lifetime."""

    def __init__(self, config: ZeroPiHatConfig, backend=None):
        self.config = config
        self.backend = backend or GpiodBackend(config)
        self.key = f"zero-pi-hat:{Path(config.gpiochip).resolve()}:7"
        self.lease = HostLease(self.key)
        self.manager = ResourceManager()
        self.started = False

    async def _connect(self):
        self.backend.claim_off()
        return self.backend

    @staticmethod
    async def _disconnect(backend):
        backend.release()

    async def start(self):
        if not self.config.expected_pi_serial:
            raise ValueError("Set expected_pi_serial from a read-only inspect first")
        if not self.config.wiring_confirmed or not self.config.loads:
            raise ValueError("Confirm wiring and name every load on the shared 12 V output")
        self.lease.acquire()
        try:
            await self.manager.register(self.key, "zero_pi_hat", self._connect, self._disconnect)
            await self.manager.connect(self.key)
            self.started = True
        except BaseException:
            self.backend.release()
            self.lease.release()
            raise

    def status(self) -> dict:
        return {"time_utc": datetime.now(UTC).isoformat(), "profile": PROFILE,
                "owner_pid": os.getpid(), "loads": self.config.loads,
                "config_identity": config_identity(self.config), **self.backend.read(),
                "rail_voltage_v": None, "rail_state": "not_measured",
                "release_or_reboot_can_enable_output": True}

    def change(self, enabled: bool) -> dict:
        # Always issue an off request before querying fault/status.
        if enabled and self.backend.read()["fault_active"]:
            raise RuntimeError("Fault is active; refusing output enable")
        self.backend.set_enabled(enabled)
        try:
            result = self.status()
            if result["enable_gpio_high"] != enabled or (enabled and result["fault_active"]):
                raise RuntimeError("GPIO readback/fault check failed")
            return result
        except BaseException:
            if enabled:
                self.backend.set_enabled(False)
            raise

    async def command(self, request: HatRequest, client_id: str = "local") -> dict:
        if not self.started:
            raise RuntimeError("HAT owner has not started")
        async with self.manager.lock(self.key, client_id, timeout=0):
            if request.operation == "status":
                return self.status()
            if request.operation == "output":
                return self.change(request.enabled)
            if not self.backend.read()["enable_gpio_high"]:
                raise RuntimeError("Output is already off; cycle will not enable an off output")
            self.change(False)
            try:
                await asyncio.sleep(request.off_seconds)
                self.change(True)
                await asyncio.sleep(request.settle_seconds)
                result = self.status()
                if result["fault_active"] or not result["enable_gpio_high"]:
                    raise RuntimeError("Cycle final GPIO/fault check failed")
                return result
            except BaseException:
                self.backend.set_enabled(False)
                raise

    async def close(self):
        # This is a physical release, NOT an off operation. Only owner teardown
        # uses it. CLI/MCP client disconnects never call it.
        try:
            if self.started:
                await self.manager.disconnect(self.key)
        finally:
            self.started = False
            self.lease.release()


async def serve(config: ZeroPiHatConfig, owner: HatOwner | None = None):
    """Foreground Unix-socket owner. Supervise it for sustained off holds."""
    if sys.platform != "linux":
        raise RuntimeError("Run the HAT owner on its Linux Pi host")
    path = Path(config.socket_path).expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    owner = owner or HatOwner(config)
    socket_lease = HostLease(f"zero-pi-hat-socket:{path.resolve()}")
    journal = path.with_suffix(".jsonl")
    tasks = set()

    def record(event):
        with journal.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"time_utc": datetime.now(UTC).isoformat(), **event}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    async def client(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        request_id = None
        try:
            try:
                envelope = json.loads(await asyncio.wait_for(reader.readline(), 5))
                request_id = envelope["request_id"]
                if envelope["config_identity"] != config_identity(config):
                    raise ValueError("Client configuration differs from the running owner")
                request = HatRequest.model_validate(envelope["request"])
                record({"event": "request", "request_id": request_id,
                        "request": request.model_dump()})
                # Complete accepted operations even if the caller disconnects.
                result = await owner.command(request, str(request_id))
                response = {"request_id": request_id, "result": result}
            except Exception as exc:
                response = {"request_id": request_id, "error": str(exc)}
            record({"event": "response", **response})
            with suppress(ConnectionError):
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
        finally:
            writer.close()
            try:
                with suppress(ConnectionError):
                    await writer.wait_closed()
            finally:
                tasks.discard(task)

    server = None
    owner.lease.acquire()
    try:
        socket_lease.acquire()
        if path.exists() or path.is_symlink():
            raise RuntimeError("Owner socket already exists; inspect ownership, do not preempt it")
        server = await asyncio.start_unix_server(
            client, str(path), limit=16384, start_serving=False
        )
        os.chmod(path, 0o600)
        await owner.start()
        record({"event": "owner_started_off", "result": owner.status()})
        async with server:
            await server.serve_forever()
    finally:
        if server is not None:
            server.close()
            await server.wait_closed()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await owner.close()
        finally:
            if server is not None:
                path.unlink(missing_ok=True)
            socket_lease.release()


async def command(config: ZeroPiHatConfig | None, operation: str, **args) -> dict:
    """Send one bounded request; no GPIO acquisition and no implicit owner launch."""
    if config is None:
        raise ValueError("Configure zero_pi_hat in scopeloop.yaml")
    request = HatRequest(operation=operation, **args)
    if sys.platform != "linux":
        raise RuntimeError("Run HAT CLI/MCP on its Linux Pi host, optionally through SSH")
    request_id = str(uuid.uuid4())
    path = str(Path(config.socket_path).expanduser())
    reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(path, limit=16384), 5)
    try:
        writer.write(json.dumps({"request_id": request_id,
                                 "config_identity": config_identity(config),
                                 "request": request.model_dump()}).encode() + b"\n")
        await writer.drain()
        try:
            response = json.loads(await asyncio.wait_for(reader.readline(), 30))
        except (TimeoutError, ValueError) as exc:
            raise RuntimeError(
                "HAT result unknown; read status before retrying any action"
            ) from exc
        if response.get("request_id") != request_id:
            raise RuntimeError("HAT response identity mismatch; result unknown")
        if "error" in response:
            raise RuntimeError(response["error"])
        return response["result"]
    finally:
        writer.close()
        await writer.wait_closed()
