"""Owned Hantek workflows shared by CLI, MCP, and bench scripts."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import math
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from scopeloop.config import HantekConfig
from scopeloop.evidence import require_artifact, validate_bundle
from scopeloop.instruments.hantek import AWGSettings, Hantek2000
from scopeloop.resources import ResourceManager, ResourceState
from scopeloop.saleae_labels import sha256
from scopeloop.session import Session

LIMITATIONS = [
    "Generator readbacks are setpoints; output-enable readback is unavailable.",
    "DMM reads poll display data; instrument sample timestamps and hold status are unavailable.",
    "Loopback comparisons use instruments in the same unit, not an independent calibration.",
    "Scope volts require a saved zero reference and nominal ADC gain, not factory calibration.",
    "Scope timing is nominal; device acquisition counter and trigger origin are unavailable.",
    "Reacquire zero reference after reconnecting probes or changing setup, temperature or power.",
]


class HantekService:
    def __init__(self, config: HantekConfig | None, manager=None, driver=None):
        if config is None:
            raise ValueError("Configure instruments.hantek in scopeloop.yaml")
        self.driver = driver or Hantek2000(config)
        self.manager = manager or ResourceManager()
        self.key = "hantek:usb:0483:2d42"
        self._registered = False

    async def _connect(self):
        await self.driver.connect()
        return self.driver

    @asynccontextmanager
    async def session(self, client_id="project"):
        """Retain this context across the entire multi-instrument bench test."""
        if not self._registered:
            await self.manager.register(
                self.key, "scope_dmm_awg", self._connect, Hantek2000.disconnect
            )
            self._registered = True
        async with self.manager.lock(self.key, client_id):
            connected = self.manager.get_resource_info(self.key).state == ResourceState.CONNECTED
            driver = await self.manager.connect(self.key)
            try:
                yield driver
            finally:
                if not connected:
                    # ResourceManager logs disconnect errors. Propagate our own
                    # cleanup failure so a failed output-off cannot report success.
                    try:
                        await driver.disconnect()
                    finally:
                        await self.manager.disconnect(self.key)

    async def command(self, name, args, client_id="project"):
        args = dict(args)
        output = Path(args.pop("output")) if args.get("output") else None
        args.pop("output", None)
        if name in ("scope_capture", "loopback", "awg_run") and output is None:
            raise ValueError(f"{name} requires a new output evidence directory")
        manifest = {
            "schema_version": 1,
            "kind": f"hantek_{name}",
            "started_at": datetime.now(UTC).isoformat(),
            "request": dict(args),
            "limitations": list(LIMITATIONS),
            "software": {"scopeloop": importlib.metadata.version("scopeloop")},
        }
        session = None
        if output:
            output.mkdir(parents=True, exist_ok=False)
            session = await Session.create(
                output / "sessions", project_name="Hantek bench", description=f"Hantek {name}"
            )
            await session.start()
        start = len(self.driver.transcript)
        try:
            async with self.session(client_id) as d:
                manifest["instrument"] = d.identity
                if name == "status":
                    result = {
                        "instrument": d.identity,
                        "dmm": await d.dmm_read(),
                        "awg": await d.awg_status(),
                        "scope": await d.scope_status(),
                    }
                elif name == "dmm_read":
                    result = await d.dmm_read()
                elif name == "dmm_configure":
                    result = await d.dmm_configure(**args)
                elif name == "screen":
                    result = await d.screen(**args)
                elif name == "awg_configure":
                    result = await d.awg_configure(args["settings"])
                elif name == "awg_off":
                    result = await d.awg_output(False)
                elif name == "scope_configure":
                    result = await d.scope_configure(**args["settings"])
                elif name == "scope_capture":
                    reference = args.pop("zero_reference", None) or d.config.scope_zero_reference
                    data = await d.scope_capture(**args)
                    result = save_scope(data, output, reference, d.identity)
                    manifest.update(result)
                elif name == "awg_run":
                    result = await run_awg(d, **args)
                elif name == "loopback":
                    result = await loopback(d, output=output, **args)
                else:
                    raise ValueError(f"Unknown Hantek operation: {name}")
            manifest["status"] = "complete"
            if output:
                (output / "result.json").write_text(
                    json.dumps(result, indent=2, allow_nan=False), encoding="utf-8"
                )
            return {**result, **({"manifest": str(output / "manifest.json")} if output else {})}
        except BaseException as exc:
            manifest.update(
                status="failed", error={"type": type(exc).__name__, "message": str(exc)}
            )
            raise
        finally:
            if output:
                transcript = self.driver.transcript[start:]
                (output / "usb.json").write_text(json.dumps(transcript, indent=2), encoding="utf-8")
                if session:
                    for event in transcript:
                        await session.log_event("hantek", "usb_transaction", event)
                    await session.end(
                        outcome="success" if manifest.get("status") == "complete" else "failure"
                    )
                manifest["completed_at"] = datetime.now(UTC).isoformat()
                manifest["files"] = {
                    str(p.relative_to(output)).replace("\\", "/"): sha256(p)
                    for p in sorted(output.rglob("*"))
                    if p.is_file()
                }
                (output / "manifest.json").write_text(
                    json.dumps(manifest, indent=2), encoding="utf-8"
                )
                (output / "SHA256SUMS").write_text(
                    "".join(
                        f"{sha256(p)}  {p.relative_to(output).as_posix()}\n"
                        for p in sorted(output.rglob("*"))
                        if p.is_file()
                    ),
                    encoding="utf-8",
                )


def save_scope(data: dict, output: Path, zero_reference=None, identity=None) -> dict:
    """Preserve raw ADC evidence and optionally derive explicitly nominal volts."""
    raw, discarded = data.pop("raw"), data.pop("discarded")
    (output / "waveform.bin").write_bytes(raw)
    (output / "pipeline-discarded.bin").write_bytes(discarded)
    setup = data["setup"]
    enabled = [ch for ch, s in setup["channels"].items() if s["enabled"]]
    codes = np.frombuffer(raw, dtype=np.uint8)[enabled.index(data["channel"]) :: len(enabled)]
    rate = min(100 / setup["seconds_per_div"], 250e6 if len(enabled) == 1 else 125e6)
    times = np.arange(len(codes)) / rate
    np.savez_compressed(output / "raw-waveform.npz", time_s=times, adc_codes=codes)
    np.savetxt(
        output / "raw-waveform.csv",
        np.column_stack((times, codes)),
        delimiter=",",
        header="relative_time_s,adc_code",
        comments="",
        fmt=("%.12g", "%d"),
    )
    result = {
        **data,
        "sample_rate_hz": rate,
        "sample_count": len(codes),
        "min_code": int(codes.min()),
        "max_code": int(codes.max()),
        "possible_clipping": bool(np.any((codes <= 1) | (codes >= 254))),
        "time_origin": "first returned sample; trigger-relative origin unverified",
        "voltage_scaling": "unavailable; raw ADC codes retained",
    }
    if zero_reference is not None:
        source = Path(zero_reference)
        ref = validate_bundle(source)
        require_artifact(ref, "waveform.bin")
        if (
            ref.get("kind") != "hantek_scope_zero"
            or ref.get("channel") != data["channel"]
            or ref.get("setup", {}).get("channels") != setup["channels"]
        ):
            raise ValueError("Zero reference must match channel and all channel settings")
        if not identity or any(
            ref.get("instrument", {}).get(k) != identity.get(k)
            for k in ("serial", "firmware", "controller_uid")
        ):
            raise ValueError("Zero reference instrument identity does not match")
        reference = (source / "waveform.bin").read_bytes()
        if len(reference) != len(raw):
            raise ValueError("Zero reference has a different frame length")
        (output / "zero-reference.bin").write_bytes(reference)
        (output / "zero-reference.json").write_text(json.dumps(ref, indent=2), encoding="utf-8")
        ground = np.frombuffer(reference, dtype=np.uint8)[
            enabled.index(data["channel"]) :: len(enabled)
        ]
        if np.any((ground <= 1) | (ground >= 254)):
            raise ValueError("Zero reference clips; cannot derive nominal volts")
        zero = float(np.mean(ground))
        codes_per_div = 25.6 if len(enabled) == 2 else 25.0
        channel = setup["channels"][data["channel"]]
        volts_per_code = channel["volts_per_div_at_bnc"] * channel["probe"] / codes_per_div
        volts = (codes.astype(float) - zero) * volts_per_code
        np.savez_compressed(output / "waveform.npz", time_s=times, voltage_v=volts)
        np.savetxt(
            output / "waveform.csv",
            np.column_stack((times, volts)),
            delimiter=",",
            header="relative_time_s,nominal_voltage_v",
            comments="",
            fmt="%.12g",
        )
        result.update(
            voltage_scaling="nominal gain with saved zero-input reference; uncalibrated",
            channel_map={data["channel"]: data["channel"] + " (nominal volts)"},
            waveform={
                "channel": data["channel"],
                "sample_rate": rate,
                "sample_count": len(codes),
                "calibrated": False,
            },
            zero_reference={
                "source": str(source),
                "source_manifest_sha256": sha256(source / "manifest.json"),
                "mean_code": zero,
                "rms_codes": float(np.std(ground)),
                "volts_per_code": volts_per_code,
                "codes_per_div": codes_per_div,
            },
            nominal_measurements={
                "mean_v": float(np.mean(volts)),
                "vpp": float(np.ptp(volts)),
                "ac_rms_v": float(np.std(volts)),
            },
        )
    return result


def scope_bundle(data, output, identity, reference=None, kind="hantek_scope_capture", **extra):
    """Save a nested scope bundle that can also be verified/exported on its own."""
    output.mkdir()
    result = save_scope(data, output, reference, identity)
    manifest = {
        "schema_version": 1,
        "kind": kind,
        "status": "complete",
        "created_at": datetime.now(UTC).isoformat(),
        "instrument": identity,
        "limitations": list(LIMITATIONS),
        **result,
        **extra,
        "files": {p.name: sha256(p) for p in output.iterdir() if p.is_file()},
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return result


async def run_awg(d, settings, duration_s=5.0, dmm_mode=None):
    settings = AWGSettings.model_validate(settings)
    if not math.isfinite(duration_s) or not 0.2 <= duration_s <= 300:
        raise ValueError("duration_s must be between 0.2 and 300")
    if dmm_mode is not None and dmm_mode not in ("dc_v", "ac_v"):
        raise ValueError("Generator loopback permits only dc_v or ac_v")
    readback = await d.awg_configure(settings)
    readings = []
    try:
        if dmm_mode:
            await d.dmm_configure(dmm_mode)
        await d.awg_output(True)
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            if dmm_mode:
                reading = await d.dmm_read()
                if reading["mode"] != dmm_mode:
                    raise ValueError("DMM mode changed during generator run")
                readings.append(reading)
            await asyncio.sleep(min(0.5, max(0, deadline - time.monotonic())))
    finally:
        await d.awg_output(False)
    return {
        "settings": readback,
        "readings": readings,
        "output_off_command_sent": True,
        "duration_s": duration_s,
        "limitations": list(LIMITATIONS),
    }


async def loopback(d, settle_s=3.0, samples=3, with_scope=False, output=None):
    """Test the existing physical GEN OUT -> V/COM connection, without DUT actions."""
    if not math.isfinite(settle_s) or not 2 <= settle_s <= 10:
        raise ValueError("settle_s must be 2..10 seconds")
    if type(samples) is not int or not 2 <= samples <= 20:
        raise ValueError("samples must be an integer from 2 to 20")
    if type(with_scope) is not bool or (with_scope and output is None):
        raise ValueError("with_scope must be boolean and requires an output directory")
    original_mode = (await d.dmm_read())["mode"]
    stages = []
    try:
        reference = None
        if with_scope:
            setup = await d.scope_status()
            ch = setup["channels"]["CH1"]
            if not (
                ch["enabled"]
                and ch["coupling"] == "DC"
                and ch["probe"] == 1
                and ch["volts_per_div_at_bnc"] == 1
                and math.isclose(setup["seconds_per_div"], 0.002)
                and setup["trigger_mode"] == "auto"
            ):
                raise ValueError("Scope loopback requires CH1 DC/1x, 1 V/div, 2 ms/div, AUTO")
            await d.awg_configure(AWGSettings(amplitude_vpp=0.002, offset_v=0))
            await d.dmm_configure("dc_v")
            await d.awg_output(True)
            await asyncio.sleep(settle_s)
            check = await d.dmm_read()
            if check["mode"] != "dc_v" or check["value"] is None or abs(check["value"]) > 0.01:
                raise ValueError("DMM did not confirm the near-zero reference stimulus")
            reference = output / "scope-zero"
            scope_bundle(
                await d.scope_capture("CH1"),
                reference,
                d.identity,
                kind="hantek_scope_zero",
                reference_dmm=check,
                reference_stimulus="100 Hz sine, 2 mVpp high-Z, zero offset",
            )
        for mode, vpp, offset in [
            ("ac_v", 1, 0),
            ("ac_v", 2, 0),
            ("dc_v", 1, 1),
            ("dc_v", 1, -1),
            ("dc_v", 1, 0),
        ]:
            settings = AWGSettings(amplitude_vpp=vpp, offset_v=offset)
            readback = await d.awg_configure(settings)
            await d.dmm_configure(mode)
            await d.awg_output(True)
            await asyncio.sleep(settle_s)
            readings = []
            for _ in range(samples):
                reading = await d.dmm_read()
                if reading["mode"] != mode or reading["unit"] != "V" or reading["overload"]:
                    raise ValueError("Loopback returned an unexpected mode or overload")
                readings.append(reading)
                await asyncio.sleep(0.5)
            expected = vpp / (2 * math.sqrt(2)) if mode == "ac_v" else offset
            measured = float(np.median([r["value"] for r in readings]))
            stage = {
                "settings": readback,
                "mode": mode,
                "readings": readings,
                "expected_v": expected,
                "median_v": measured,
                "functional_match": abs(measured - expected) <= 0.01 + abs(expected) * 0.05,
            }
            if with_scope:
                directory = output / f"scope-{len(stages) + 1}"
                scope = scope_bundle(await d.scope_capture("CH1"), directory, d.identity, reference)
                measured_scope = scope["nominal_measurements"][
                    "ac_rms_v" if mode == "ac_v" else "mean_v"
                ]
                stage["scope"] = {"directory": directory.name, **scope}
                stage["scope_functional_match"] = (
                    not scope["possible_clipping"]
                    and abs(measured_scope - expected) <= 0.06 + abs(expected) * 0.1
                )
            stages.append(stage)
        return {
            "stages": stages,
            "functional_match": all(
                s["functional_match"] and s.get("scope_functional_match", True) for s in stages
            ),
            "scope_functional_match": (
                all(s["scope_functional_match"] for s in stages) if with_scope else None
            ),
            "qualification": "functional loopback only; no independent calibration",
            "limitations": list(LIMITATIONS),
            "output_off_command_sent": True,
        }
    finally:
        await d.awg_output(False)
        await d.awg_configure(AWGSettings())
        await d.dmm_configure(original_mode)
