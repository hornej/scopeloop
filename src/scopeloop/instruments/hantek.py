"""Hantek 2000 binary protocol, independently implemented from wire behavior.

See docs/hantek.md for response layouts, provenance and physical limits.
Settings readbacks are setpoints, never measurements of the generator output.
"""

from __future__ import annotations

import asyncio
import math
import os
import struct
import time
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from scopeloop.config import HantekConfig
from scopeloop.instruments.base import InstrumentError
from scopeloop.instruments.hantek_usb import LibUSBTransport, WinUSBTransport
from scopeloop.resources import HostLease, TaskRLock, serialized

DMM_MODES = (
    "ac_a",
    "dc_a",
    "ac_ma",
    "dc_ma",
    "dc_mv",
    "dc_v",
    "ac_v",
    "capacitance",
    "resistance",
    "continuity",
    "diode",
)
DMM_UNITS = ("A", "A", "A", "A", "V", "V", "V", "F", "ohm", "ohm", "V")
PREFIXES = {
    0: ("n", 1e-9),
    1: ("u", 1e-6),
    2: ("m", 1e-3),
    3: ("k", 1e3),
    4: ("M", 1e6),
    5: ("", 1.0),
}
WAVES = ("square", "ramp", "sine", "trapezoid")
SCALES = tuple(v * 10.0**e for e in range(-2, 2) for v in (1, 2, 5))[:10]
TIMEBASES = (5e-9,) + tuple(v * 10.0**e for e in range(-8, 3) for v in (1, 2, 5))


class ProtocolError(InstrumentError):
    pass


class AWGSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    waveform: str = "sine"
    frequency_hz: int = Field(default=100, ge=1, le=25_000_000, strict=True)
    amplitude_vpp: float = Field(default=2, ge=0.002, le=5, allow_inf_nan=False)
    offset_v: float = Field(default=0, ge=-2.5, le=2.5, allow_inf_nan=False)
    duty_percent: float = Field(default=50, ge=1, le=99, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_output(self):
        if self.waveform not in WAVES:
            raise ValueError(f"waveform must be one of {WAVES}")
        limit = {
            "sine": 25_000_000,
            "square": 10_000_000,
            "ramp": 1_000_000,
            "trapezoid": 1_000_000,
        }[self.waveform]
        if self.frequency_hz > limit:
            raise ValueError(f"{self.waveform} frequency must be <= {limit} Hz")
        if self.amplitude_vpp / 2 + abs(self.offset_v) > 2.5 + 1e-12:
            raise ValueError("High-impedance amplitude/2 + |offset| must be <= 2.5 V")
        return self


def packet(group: int, command: int, value: int = 0, *, read: bool = False) -> bytes:
    return struct.pack("<BBBBBIB", 0, 10, group, int(read), command, value, 0)


def decode_dmm(raw: bytes) -> dict:
    if len(raw) != 14 or raw[:3] != b"\x55\x0b\x01" or raw[-1] != 0x55:
        raise ProtocolError(f"Malformed DMM response: {raw.hex()}")
    mode, sign, decimals, digits, prefix = raw[3], raw[5], raw[6], raw[7:11], raw[11]
    if mode >= len(DMM_MODES) or sign not in (0, 1) or prefix not in PREFIXES:
        raise ProtocolError(f"Unknown DMM mode/sign/unit: {raw.hex()}")
    if decimals > 4:
        raise ProtocolError(f"Invalid DMM decimal position: {decimals}")
    overload = any(d > 9 for d in digits)
    unit_prefix, multiplier = PREFIXES[prefix]
    number = int("".join(str(d) for d in digits)) if not overload else None
    display = (number / 10**decimals * (-1 if sign else 1)) if number is not None else None
    return {
        "mode": DMM_MODES[mode],
        "value": display * multiplier if display is not None else None,
        "unit": DMM_UNITS[mode],
        "display_value": display,
        "display_unit": unit_prefix + DMM_UNITS[mode],
        "display": "OL" if overload else f"{display:.{decimals}f}",
        "overload": overload,
        "raw_hex": raw.hex(),
        "range_code": raw[4],
        "status_byte": raw[12],
        "observed_at": datetime.now(UTC).isoformat(),
        "freshness": "polled_display; instrument sample timestamp and hold status unavailable",
    }


def decode_scope(raw: bytes) -> dict:
    if len(raw) != 25 or raw[:4] != bytes.fromhex("55190015"):
        raise ProtocolError(f"Malformed scope settings: {raw.hex()}")
    data = raw[4:]
    channels = {}
    for i in range(2):
        enabled, coupling, probe, bw, scale, position = data[i * 6 : i * 6 + 6]
        if (
            enabled > 1
            or coupling > 2
            or probe > 3
            or bw > 1
            or scale >= len(SCALES)
            or position > 200
        ):
            raise ProtocolError("Unknown scope channel setting")
        channels[f"CH{i + 1}"] = {
            "enabled": bool(enabled),
            "coupling": ("AC", "DC", "GND")[coupling],
            "probe": (1, 10, 100, 1000)[probe],
            "bandwidth_limit": bool(bw),
            "volts_per_div_at_bnc": SCALES[scale],
            "position_code": position,
        }
    if (
        data[14] >= len(TIMEBASES)
        or data[18] > 2
        or data[19] > 2
        or data[17] > 1
        or data[12] > 1
        or data[20] > 200
    ):
        raise ProtocolError("Unknown scope timebase/trigger setting")
    return {
        "channels": channels,
        "running": bool(data[12]),
        "format_code": data[13],
        "seconds_per_div": TIMEBASES[data[14]],
        "timebase_code": data[14],
        "horizontal_position_code": int.from_bytes(data[15:17], "little"),
        "trigger_source": f"CH{data[17] + 1}",
        "trigger_slope": ("rising", "falling", "both")[data[18]],
        "trigger_mode": ("auto", "normal", "single")[data[19]],
        "trigger_level_code": data[20],
        "raw_hex": raw.hex(),
    }


def _index(values, value, name):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    for index, allowed in enumerate(values):
        if math.isclose(value, allowed, rel_tol=1e-9, abs_tol=0):
            return index
    raise ValueError(f"Unsupported {name}: {value}; choose from {values}")


class Hantek2000:
    def __init__(self, config: HantekConfig, transport=None):
        self.config = config
        backend = config.transport
        if backend == "auto":
            backend = "winusb" if os.name == "nt" else "libusb"
        self.transport = transport or (
            WinUSBTransport(config.usb_path, config.timeout_ms)
            if backend == "winusb"
            else LibUSBTransport(config.usb_serial, config.timeout_ms)
        )
        # Conservative family lease also conflicts with the commissioning scripts.
        self._lease = HostLease("scope:usb:0483:2d42")
        self._lock = TaskRLock()
        self.connected = False
        self._output_owned = False
        self.transcript: list[dict] = []
        self.identity: dict = {}

    async def _io(self, fn, *args):
        task = asyncio.create_task(asyncio.to_thread(fn, *args))
        cancelled = False
        # Repeated cancellation must not leave a native call using a closed handle.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        if cancelled:
            if not task.cancelled():
                task.exception()
            raise asyncio.CancelledError
        return task.result()

    @serialized
    async def connect(self):
        if self.connected:
            return self.identity
        self._lease.acquire()
        try:
            usb = await self._io(self.transport.open)
            self.connected = True
            serial = await self._text_query(9, 32)
            if self.config.expected_serial and serial != self.config.expected_serial:
                raise ProtocolError("Connected Hantek serial does not match expected_serial")
            uid = await self._setting(3, 1, 16)
            self.identity = {
                "manufacturer": "Hantek",
                "model": "Hantek2000 series",
                "model_readback_available": False,
                "controller_uid": uid[4:16].hex(),
                "serial": serial,
                "firmware": await self._text_query(10, 20),
                "usb": usb,
                "protocol": "hantek-2000",
            }
            return self.identity
        except BaseException:
            try:
                await self._io(self.transport.close)
            finally:
                self.connected = False
                self._lease.release()
            raise

    @serialized
    async def disconnect(self):
        try:
            if self.connected and self._output_owned:
                await self.awg_output(False)
        finally:
            try:
                await self._io(self.transport.close)
            finally:
                self.connected = False
                # A later connection cannot claim output ownership from this one,
                # including when OFF failed and the physical state is unknown.
                self._output_owned = False
                self._lease.release()

    async def _write(self, data: bytes):
        if not self.connected:
            raise ProtocolError("Hantek is disconnected")
        entry = {"at": datetime.now(UTC).isoformat(), "tx": data.hex()}
        self.transcript.append(entry)
        try:
            await self._io(self.transport.write, data)
        except BaseException as exc:
            entry["error"] = str(exc)
            raise

    async def _query(self, data: bytes) -> bytes:
        await self._write(data)
        entry = self.transcript[-1]
        try:
            response = await self._io(self.transport.read, 64)
            entry["rx"] = response.hex()
            return response
        except BaseException as exc:
            entry["error"] = str(exc)
            raise

    async def _setting(self, group, command, minimum=5):
        raw = await self._query(packet(group, command, read=True))
        if len(raw) < minimum or raw[0] != 0x55 or raw[2:4] != bytes([group, command]):
            raise ProtocolError(f"Wrong/truncated response for {group}:{command}: {raw.hex()}")
        return raw

    async def _text_query(self, code, maximum):
        raw = await self._setting(3, code)
        text = raw[4 : 4 + maximum].split(b"\0", 1)[0]
        if not text or any(c < 32 or c > 126 for c in text):
            raise ProtocolError("Invalid Hantek identity string")
        return text.decode("ascii")

    @serialized
    async def dmm_read(self):
        return decode_dmm(await self._query(bytes.fromhex("0005010100")))

    @serialized
    async def dmm_configure(self, mode: str):
        if mode not in DMM_MODES:
            raise ValueError(f"Unsupported DMM mode: {mode}")
        await self._write(bytes([0, 5, 1, 0, DMM_MODES.index(mode)]))
        deadline = time.monotonic() + 4
        while True:
            await asyncio.sleep(0.2)
            value = await self.dmm_read()
            if value["mode"] == mode:
                return value
            if time.monotonic() > deadline:
                raise ProtocolError(f"DMM did not switch to {mode}")

    @serialized
    async def screen(self, name: str):
        if name not in ("scope", "dmm", "awg"):
            raise ValueError("Screen must be scope, dmm or awg")
        await self._write(packet(3, 0, ("scope", "dmm", "awg").index(name)))
        return {"requested_screen": name, "screen_readback_available": False}

    @serialized
    async def awg_status(self):
        raw = {code: await self._setting(2, code, 8) for code in (0, 1, 2, 3)}
        kind = raw[0][4]
        offset = int.from_bytes(raw[3][4:6], "little") / 1000
        if raw[3][6] not in (0, 1):
            raise ProtocolError("Invalid AWG offset sign")
        if raw[3][6]:
            offset = -offset
        return {
            "waveform": WAVES[kind] if kind < len(WAVES) else f"arbitrary_{kind - 3}",
            "frequency_hz": int.from_bytes(raw[1][4:8], "little"),
            "amplitude_vpp": int.from_bytes(raw[2][4:6], "little") / 500,
            "offset_v": offset,
            "load": "high_impedance",
            "output_enabled": None,
            "output_readback_available": False,
            "output_enabled_by_this_connection": self._output_owned,
            "qualification": "setting_readback; output voltage not measured",
        }

    @serialized
    async def awg_configure(self, settings: AWGSettings | dict):
        settings = AWGSettings.model_validate(settings)
        if settings.amplitude_vpp / 2 + abs(settings.offset_v) > self.config.max_output_peak_v:
            raise ValueError("AWG exceeds configured max_output_peak_v")
        amplitude = round(settings.amplitude_vpp * 500)
        offset = round(abs(settings.offset_v) * 1000) | ((settings.offset_v < 0) << 16)
        if amplitude / 1000 + (offset & 0xFFFF) / 1000 > self.config.max_output_peak_v + 1e-12:
            raise ValueError("Rounded AWG settings exceed configured max_output_peak_v")
        await self.awg_output(False)
        for code, value in [
            (0, WAVES.index(settings.waveform)),
            (1, settings.frequency_hz),
            (2, amplitude),
            (3, offset),
        ]:
            await self._write(packet(2, code, value))
            await asyncio.sleep(0.05)
        if settings.waveform in ("square", "ramp"):
            code = 4 if settings.waveform == "square" else 5
            await self._write(packet(2, code, round(settings.duty_percent * 100)))
        result = await self.awg_status()
        expected = {
            "waveform": settings.waveform,
            "frequency_hz": settings.frequency_hz,
            "amplitude_vpp": amplitude / 500,
            "offset_v": round(settings.offset_v * 1000) / 1000,
        }
        if any(result[key] != value for key, value in expected.items()):
            raise ProtocolError(f"AWG settings did not read back: {result}")
        return {
            **result,
            "requested": settings.model_dump(),
            "readback_verified": True,
            "duty_readback_verified": False,
        }

    @serialized
    async def awg_output(self, enabled: bool):
        if type(enabled) is not bool:
            raise ValueError("enabled must be boolean")
        if enabled:
            state = await self.awg_status()
            if state["waveform"] not in WAVES:
                raise ValueError("Unverified arbitrary-wave output cannot be enabled")
            limits = AWGSettings.model_validate(
                {k: state[k] for k in ("waveform", "frequency_hz", "amplitude_vpp", "offset_v")}
            )
            if limits.amplitude_vpp / 2 + abs(limits.offset_v) > self.config.max_output_peak_v:
                raise ValueError("AWG readback exceeds configured peak voltage")
            # Set before the write, so even a partial/failed enable attempts cleanup.
            self._output_owned = True
        await self._write(packet(2, 8, int(enabled)))
        if not enabled:
            self._output_owned = False
        return {"output_commanded": enabled, "output_readback_available": False}

    @serialized
    async def scope_status(self):
        return decode_scope(await self._query(packet(0, 0x15, read=True)))

    @serialized
    async def scope_configure(self, **settings):
        allowed = {
            "channel",
            "enabled",
            "coupling",
            "probe",
            "bandwidth_limit",
            "volts_per_div",
            "position_code",
            "seconds_per_div",
            "trigger_source",
            "trigger_slope",
            "trigger_mode",
            "trigger_level_code",
            "running",
        }
        if unknown := set(settings) - allowed:
            raise ValueError(f"Unknown scope settings: {sorted(unknown)}")
        channel = settings.get("channel", "CH1")
        if channel not in ("CH1", "CH2"):
            raise ValueError("Channel must be CH1 or CH2")
        base = 0 if channel == "CH1" else 6
        commands = []
        for name, value in settings.items():
            if name == "channel" or value is None:
                continue
            if name in ("enabled", "bandwidth_limit", "running"):
                if type(value) is not bool:
                    raise ValueError(f"{name} must be boolean")
                code = {"enabled": base, "bandwidth_limit": base + 3, "running": 12}[name]
                commands.append((code, int(value)))
            elif name in ("coupling", "probe", "trigger_source", "trigger_slope", "trigger_mode"):
                choices, code = {
                    "coupling": (("AC", "DC", "GND"), base + 1),
                    "probe": ((1, 10, 100, 1000), base + 2),
                    "trigger_source": (("CH1", "CH2"), 16),
                    "trigger_slope": (("rising", "falling", "both"), 17),
                    "trigger_mode": (("auto", "normal", "single"), 18),
                }[name]
                if value not in choices:
                    raise ValueError(f"Invalid {name}: {value}")
                commands.append((code, choices.index(value)))
            elif name in ("volts_per_div", "seconds_per_div"):
                table, code = (SCALES, base + 4) if name == "volts_per_div" else (TIMEBASES, 14)
                commands.append((code, _index(table, value, name)))
            else:
                if type(value) is not int or not 0 <= value <= 200:
                    raise ValueError(f"{name} must be an integer from 0 to 200")
                commands.append((base + 5 if name == "position_code" else 20, value))
        # Validate every setting before the first USB write.
        for code, value in commands:
            await self._write(packet(0, code, value))
            await asyncio.sleep(0.05)
        state = await self.scope_status()
        raw = bytes.fromhex(state["raw_hex"])[4:]
        for code, value in commands:
            if raw[{16: 17, 17: 18, 18: 19}.get(code, code)] != value:
                raise ProtocolError(f"Scope setting {code} did not read back")
        return state

    async def _frame(self, total=6000):
        raw = bytearray()
        deadline = time.monotonic() + 3
        while len(raw) < total:
            pos = len(raw)
            request = struct.pack(
                "<BBHBHHB", pos // 64 + 1, 10, 0x100, 0x16, total // 2, total // 2, 20
            )
            block = await self._query(request)
            if pos == 0 and block.startswith(bytes.fromhex("0000ffff") * 3):
                if time.monotonic() > deadline:
                    raise ProtocolError("Scope acquisition remained busy")
                await asyncio.sleep(0.1)
                continue
            if len(block) != min(64, total - pos):
                raise ProtocolError(f"Truncated scope block at {pos}: {len(block)} bytes")
            raw.extend(block)
        return bytes(raw)

    @serialized
    async def scope_capture(self, channel="CH1"):
        """Drain the observed one-frame pipeline; retain raw data and readbacks.

        No device acquisition timestamp/counter is available. This is an AUTO
        snapshot, never a qualified single-shot or calibrated ripple capture.
        """
        if channel not in ("CH1", "CH2"):
            raise ValueError("Channel must be CH1 or CH2")
        before = await self.scope_status()
        if not before["channels"][channel]["enabled"]:
            raise ValueError(f"{channel} is disabled")
        if before["format_code"] != 0 or before["timebase_code"] < 7:
            raise ValueError("Capture requires YT mode and timebase >= 1 us/div")
        if before["seconds_per_div"] > 0.1:
            raise ValueError("Capture currently supports timebase <= 100 ms/div")
        if before["trigger_mode"] != "auto":
            raise ValueError("Capture requires AUTO trigger mode; configure it explicitly")
        delay = max(0.3, before["seconds_per_div"] * 60)

        async def snapshot():
            await self.scope_configure(running=True)
            await asyncio.sleep(delay)
            discarded = await self._frame()
            await asyncio.sleep(delay)
            return await self._frame(), discarded

        try:
            raw, discarded = await snapshot()
            after = await self.scope_status()
            expected = {**before, "running": True}
            for key in expected.keys() - {"raw_hex"}:
                if after[key] != expected[key]:
                    raise ProtocolError("Scope setup changed during transfer")
            return {
                "channel": channel,
                "raw": raw,
                "discarded": discarded,
                "setup": after,
                "freshness": "pipeline_drained; no acquisition counter",
                "qualification": "AUTO snapshot; factory voltage calibration not applied",
            }
        finally:
            await self._write(packet(0, 12, 0))

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.disconnect()
