"""R&S/HAMEG HMP4040 over the raw TCP SCPI interface."""

from __future__ import annotations

import asyncio
import math
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from datetime import UTC, datetime

from scopeloop.config import PowerSupplyConfig
from scopeloop.instruments.base import Instrument, InstrumentError, InstrumentInfo
from scopeloop.resources import HostLease, TaskRLock, serialized


def _numeric(reply: str) -> float:
    try:
        value = float(reply)
    except ValueError as exc:
        raise InstrumentError(f"Invalid HMP4040 numeric reply: {reply!r}") from exc
    if not math.isfinite(value):
        raise InstrumentError(f"Non-finite HMP4040 numeric reply: {reply!r}")
    return value


class HMP4040(Instrument):
    def __init__(self, config: PowerSupplyConfig):
        self.config = config
        self._lock = TaskRLock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lease: HostLease | None = None
        self._info: InstrumentInfo | None = None
        self.transcript: list[dict] = []

    @property
    def is_connected(self) -> bool:
        return self._info is not None

    @serialized
    async def connect(self) -> None:
        if self.is_connected:
            return
        if self._lease:
            raise InstrumentError("HMP4040 transport failed; close the session before reconnecting")
        self.transcript.clear()
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                addresses = await asyncio.get_running_loop().getaddrinfo(
                    self.config.address, self.config.port, type=socket.SOCK_STREAM
                )
                address = addresses[0][4][0]
                self._lease = HostLease(f"power_supply:{address}:{self.config.port}")
                self._lease.acquire()
                self._reader, self._writer = await asyncio.open_connection(
                    address, self.config.port, limit=4096
                )
            idn = await self._query("*IDN?")
            fields = [field.strip() for field in idn.split(",")]
            if len(fields) != 4 or not all(fields) or fields[1].upper() != "HMP4040":
                raise InstrumentError(f"Expected an HMP4040, received {idn!r}")
            _, model, serial, firmware = fields
            if self.config.expected_serial and serial != self.config.expected_serial:
                raise InstrumentError("HMP4040 serial does not match the configured identity")
            self._info = InstrumentInfo("power_supply", model, serial, firmware, address)
        except BaseException:
            await self.disconnect()
            raise

    @serialized
    async def disconnect(self) -> None:
        try:
            await self._close_transport()
        finally:
            if self._lease:
                self._lease.release()
                self._lease = None

    async def _close_transport(self) -> None:
        writer = self._writer
        self._info = None
        self._reader = None
        self._writer = None
        if writer:
            writer.close()
            with suppress(OSError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), self.config.timeout_seconds)

    async def get_info(self) -> InstrumentInfo:
        if self._info is None:
            raise InstrumentError("HMP4040 is disconnected")
        return self._info

    async def _exchange(self, command: str, *, query: bool) -> str:
        if self._reader is None or self._writer is None:
            raise InstrumentError("HMP4040 is disconnected")
        entry = {"at": datetime.now(UTC).isoformat(), "command": command}
        self.transcript.append(entry)
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                self._writer.write((command + "\n").encode("ascii"))
                await self._writer.drain()
                if query:
                    raw = await self._reader.readuntil(b"\n")
                    reply = raw.decode("ascii").strip()
                    entry["reply"] = reply
                    if not reply:
                        raise InstrumentError(f"Empty HMP4040 reply to {command}")
                    return reply
                return ""
        except BaseException as exc:
            entry["error"] = type(exc).__name__
            # A late or partial reply cannot be associated with the next command.
            await self._close_transport()
            if isinstance(
                exc, (TimeoutError, OSError, EOFError, UnicodeError, asyncio.LimitOverrunError)
            ):
                raise InstrumentError(f"HMP4040 {command}: {type(exc).__name__}") from exc
            raise

    async def _query(self, command: str) -> str:
        return await self._exchange(command, query=True)

    async def _write(self, command: str) -> None:
        await self._exchange(command, query=False)

    async def _selected_channel(self) -> int:
        reply = await self._query("INST:NSEL?")
        if reply not in {"1", "2", "3", "4"}:
            raise InstrumentError(f"Invalid HMP4040 channel readback: {reply!r}")
        return int(reply)

    async def _select(self, channel: int) -> None:
        await self._write(f"INST:NSEL {channel}")
        if await self._selected_channel() != channel:
            raise InstrumentError(f"HMP4040 did not address CH{channel}")

    @asynccontextmanager
    async def _address_channel(self, channel: int) -> AsyncIterator[None]:
        if type(channel) is not int or channel not in range(1, 5):
            raise ValueError("HMP4040 channel must be an integer from 1 through 4")
        original = await self._selected_channel()
        try:
            if original != channel:
                await self._select(channel)
            yield
        finally:
            if original != channel and self.is_connected:
                await self._select(original)

    async def _output_state(self) -> bool:
        reply = await self._query("OUTP?")
        if reply not in {"0", "1"}:
            raise InstrumentError(f"Invalid HMP4040 output state: {reply!r}")
        return reply == "1"

    async def _setpoints(self) -> tuple[float, float]:
        reply = await self._query("APPL?")
        values = reply.split(",")
        if len(values) != 2:
            raise InstrumentError(f"Invalid HMP4040 setpoints: {reply!r}")
        return _numeric(values[0]), _numeric(values[1])

    async def _channel_status(self, channel: int) -> dict:
        voltage, current = await self._setpoints()
        return {
            "channel": channel,
            "timestamp": datetime.now(UTC).isoformat(),
            "voltage_set_v": voltage,
            "current_limit_a": current,
            "output_state": await self._output_state(),
            "measured_voltage_v": _numeric(await self._query("MEAS:VOLT?")),
            "measured_current_a": _numeric(await self._query("MEAS:CURR?")),
        }

    @serialized
    async def status(self, channel: int | None = None) -> dict:
        rows = []
        for selected in range(1, 5) if channel is None else [channel]:
            async with self._address_channel(selected):
                rows.append(await self._channel_status(selected))
        return {"instrument": asdict(await self.get_info()), "channels": rows}

    def _check_limits(self, channel: int, voltage_v: float, current_a: float) -> None:
        limits = self.config.channels.get(channel)
        if not limits or not self.config.expected_serial:
            raise ValueError("Channel control requires expected_serial and project channel limits")
        if not 0 <= voltage_v <= limits.max_voltage_v:
            raise ValueError(f"CH{channel} voltage outside project limit {limits.max_voltage_v} V")
        if not 0.001 <= current_a <= limits.max_current_a:
            raise ValueError(f"CH{channel} current outside project limit {limits.max_current_a} A")

    async def _check_error(self) -> None:
        reply = await self._query("SYST:ERR?")
        try:
            code = int(reply.split(",", 1)[0])
        except ValueError as exc:
            raise InstrumentError(f"Invalid HMP4040 error reply: {reply!r}") from exc
        if code:
            raise InstrumentError(f"HMP4040 SCPI error: {reply}")

    async def _checked_write(self, command: str) -> None:
        await self._check_error()
        await self._write(command)
        if await self._query("*OPC?") != "1":
            raise InstrumentError(f"HMP4040 completion unverified after {command}")
        await self._check_error()

    @serialized
    async def configure(self, channel: int, voltage_v: float, current_a: float) -> dict:
        self._check_limits(channel, voltage_v, current_a)
        async with self._address_channel(channel):
            if await self._output_state():
                raise InstrumentError(f"Disable CH{channel} before changing its setpoints")
            await self._checked_write(f"APPL {voltage_v:.9f},{current_a:.9f}")
            result = await self._channel_status(channel)
            actual = (result["voltage_set_v"], result["current_limit_a"])
            if actual != (voltage_v, current_a):
                raise InstrumentError(f"HMP4040 setpoint readback differs: {actual}")
            if result["output_state"]:
                raise InstrumentError(f"CH{channel} unexpectedly reports its output on")
            return result

    @serialized
    async def output(self, channel: int, enabled: bool) -> dict:
        """Change only channel selection. The front-panel master remains operator-controlled."""
        if type(enabled) is not bool:
            raise ValueError("enabled must be true or false")
        if not self.config.expected_serial or channel not in self.config.channels:
            raise ValueError("Channel control requires expected_serial and project channel limits")
        async with self._address_channel(channel):
            if enabled:
                self._check_limits(channel, *(await self._setpoints()))
            await self._checked_write(f"OUTP:SEL {int(enabled)}")
            if await self._output_state() != enabled:
                if enabled:
                    # Avoid leaving a channel selected for a later master-on.
                    await self._checked_write("OUTP:SEL 0")
                    if await self._output_state():
                        raise InstrumentError(f"CH{channel} disable readback failed")
                    raise InstrumentError(
                        f"CH{channel} did not turn on and was deselected. "
                        "Check the front-panel master and channel state."
                    )
                raise InstrumentError(f"CH{channel} disable readback failed")
            return await self._channel_status(channel)
