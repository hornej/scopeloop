"""Siglent SDS1000X-E series oscilloscope driver.

Uses bounded SCPI transactions over a raw LAN TCP socket.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np

from scopeloop.instruments.base import Instrument, InstrumentError, InstrumentInfo
from scopeloop.resources import HostLease, TaskRLock, serialized
from scopeloop.scpi import integer, number

logger = logging.getLogger(__name__)


@dataclass
class WaveformData:
    """Captured waveform data."""

    channel: str
    samples: np.ndarray
    sample_rate: float  # Samples per second
    time_offset: float  # Time of first sample
    voltage_scale: float  # Volts per division
    voltage_offset: float  # Vertical offset in volts
    record_length: int  # Number of points

    raw_data: bytes = field(default=b"", repr=False)
    raw_response: bytes = field(default=b"", repr=False)
    metadata: dict = field(default_factory=dict)

    @property
    def time_array(self) -> np.ndarray:
        """Generate time array for the samples."""
        dt = 1.0 / self.sample_rate
        return np.arange(len(self.samples)) * dt + self.time_offset

    @property
    def vpp(self) -> float:
        """Peak-to-peak voltage."""
        return float(np.max(self.samples) - np.min(self.samples))

    @property
    def vmax(self) -> float:
        """Maximum voltage."""
        return float(np.max(self.samples))

    @property
    def vmin(self) -> float:
        """Minimum voltage."""
        return float(np.min(self.samples))

    @property
    def vrms(self) -> float:
        """RMS voltage."""
        return float(np.sqrt(np.mean(self.samples**2)))

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "channel": self.channel,
            "sample_rate": self.sample_rate,
            "time_offset": self.time_offset,
            "voltage_scale": self.voltage_scale,
            "voltage_offset": self.voltage_offset,
            "record_length": self.record_length,
            "metadata": self.metadata,
            "vpp": self.vpp,
            "vmax": self.vmax,
            "vmin": self.vmin,
            "vrms": self.vrms,
            # Note: samples not included - too large for JSON
        }


@dataclass
class Measurement:
    """A single measurement result."""

    measurement_type: str
    value: float
    unit: str
    channel: str
    timestamp: float | None = None

    def to_dict(self) -> dict:
        return {
            "type": self.measurement_type,
            "value": self.value,
            "unit": self.unit,
            "channel": self.channel,
            "timestamp": self.timestamp,
            "freshness": "unverified instrument snapshot; use capture evidence",
        }


class SiglentSDS1000X(Instrument):
    """Siglent SDS1000X-E series oscilloscope driver.

    Supports SDS1104X-E, SDS1204X-E, and similar models.

    Usage:
        scope = SiglentSDS1000X("192.0.2.100")

        async with scope:
            # Get info
            info = await scope.get_info()
            print(f"Connected to {info.model}")

            # Configure channel
            await scope.set_channel_scale("CH1", 1.0)  # 1V/div
            await scope.set_timebase(0.001)  # 1ms/div

            # Take measurement
            freq = await scope.measure_frequency("CH1")
            print(f"Frequency: {freq.value} {freq.unit}")

            # Capture waveform
            waveform = await scope.capture_waveform("CH1")
            print(f"Captured {len(waveform.samples)} samples")
    """

    def __init__(self, address: str, port: int = 5025, timeout: float = 10.0):
        """Initialize the oscilloscope driver.

        Args:
            address: IP address of the oscilloscope.
            port: SCPI port (default 5025 for raw socket).
            timeout: Command timeout in seconds.
        """
        self.address = address
        self.port = port
        self.timeout = timeout

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        self._lock = TaskRLock()
        self._lease = None
        self._armed_at = None
        self._freshness = None
        self.transcript = []
        self._raw_response = b""
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @serialized
    async def connect(self) -> None:
        """Connect to the oscilloscope."""
        if self._connected:
            return

        logger.info(f"Connecting to Siglent scope at {self.address}:{self.port}")

        try:
            addresses = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(self.address, self.port), self.timeout
            )
            canonical = addresses[0][4][0]
            self._lease = HostLease(f"scope:{canonical}:{self.port}")
            self._lease.acquire()
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(canonical, self.port),
                timeout=self.timeout,
            )
            self._connected = True

            # Verify connection
            idn = await self._query("*IDN?")
            logger.info(f"Connected to: {idn}")

        except BaseException:
            await self.disconnect()
            raise

    @serialized
    async def disconnect(self) -> None:
        """Disconnect from the oscilloscope."""
        if self._writer:
            self._writer.close()
        self._reader = None
        self._writer = None
        self._connected = False
        self._armed_at = None
        self._freshness = None
        if self._lease:
            self._lease.release()
            self._lease = None

    @serialized
    async def get_info(self) -> InstrumentInfo:
        """Get oscilloscope information."""
        idn = await self._query("*IDN?")
        parts = idn.split(",")

        return InstrumentInfo(
            instrument_type="oscilloscope",
            model=parts[1] if len(parts) > 1 else "Unknown",
            serial=parts[2] if len(parts) > 2 else None,
            firmware_version=parts[3] if len(parts) > 3 else None,
            address=f"{self.address}:{self.port}",
        )

    @serialized
    async def write(self, command: str) -> None:
        """Send a raw SCPI command."""
        await self._write(command)

    @serialized
    async def query(self, command: str) -> str:
        """Send a raw SCPI query and return its response."""
        return await self._query(command)

    # =========================================================================
    # Channel Configuration
    # =========================================================================

    @serialized
    async def set_channel_scale(self, channel: str, volts_per_div: float) -> None:
        """Set the vertical scale for a channel.

        Args:
            channel: Channel name (e.g., "CH1").
            volts_per_div: Volts per division.
        """
        ch = self._normalize_channel(channel)
        await self._write(f"{ch}:VDIV {volts_per_div}")

    @serialized
    async def get_channel_scale(self, channel: str) -> float:
        """Get the vertical scale for a channel."""
        ch = self._normalize_channel(channel)
        response = await self._query(f"{ch}:VDIV?")
        return number(response, f"{ch}:VDIV")

    @serialized
    async def set_channel_offset(self, channel: str, offset: float) -> None:
        """Set the vertical offset for a channel.

        Args:
            channel: Channel name.
            offset: Offset in volts.
        """
        ch = self._normalize_channel(channel)
        await self._write(f"{ch}:OFST {offset}")

    @serialized
    async def set_channel_coupling(self, channel: str, coupling: str) -> None:
        """Set the input coupling for a channel.

        Args:
            channel: Channel name.
            coupling: "DC", "AC", or "GND".
        """
        ch = self._normalize_channel(channel)
        coupling = {"DC": "D1M", "AC": "A1M", "GND": "GND"}.get(coupling.upper())
        if coupling is None:
            raise ValueError("coupling must be DC, AC, or GND")
        await self._write(f"{ch}:CPL {coupling}")

    @serialized
    async def set_channel_enabled(self, channel: str, enabled: bool) -> None:
        """Enable or disable a channel.

        Args:
            channel: Channel name.
            enabled: Whether to enable the channel.
        """
        ch = self._normalize_channel(channel)
        state = "ON" if enabled else "OFF"
        await self._write(f"{ch}:TRA {state}")

    # =========================================================================
    # Timebase Configuration
    # =========================================================================

    @serialized
    async def set_timebase(self, seconds_per_div: float) -> None:
        """Set the horizontal timebase.

        Args:
            seconds_per_div: Seconds per division.
        """
        await self._write(f"TDIV {seconds_per_div}")

    @serialized
    async def get_timebase(self) -> float:
        """Get the horizontal timebase in seconds per division."""
        response = await self._query("TDIV?")
        return number(response, "TDIV")

    # =========================================================================
    # Trigger Configuration
    # =========================================================================

    @serialized
    async def set_trigger_level(self, level: float, source: str = "CH1") -> None:
        """Set the trigger level.

        Args:
            level: Trigger level in volts.
            source: Trigger source channel.
        """
        src = self._normalize_channel(source)
        await self._write(f"{src}:TRLV {level}")

    @serialized
    async def set_trigger_mode(self, mode: str) -> None:
        """Set the trigger mode.

        Args:
            mode: "AUTO", "NORM", "SINGLE", or "STOP".
        """
        await self._write(f"TRMD {mode.upper()}")

    @serialized
    async def force_trigger(self) -> None:
        """Force a trigger."""
        await self._write("FRTR")

    # =========================================================================
    # Acquisition
    # =========================================================================

    @serialized
    async def run(self) -> None:
        """Start acquisition."""
        await self._write("TRMD AUTO")

    @serialized
    async def stop(self) -> None:
        """Stop acquisition."""
        await self._write("STOP")

    @serialized
    async def single(self) -> None:
        """Start a fresh single shot; this is armed, not a completed capture."""
        await self._begin_acquisition("SINGLE")

    @serialized
    async def _begin_acquisition(self, mode: str) -> None:
        self._freshness = None
        await self._write("STOP")
        self._armed_at = None
        info = await self.get_info()
        revision = re.search(r"(\d+)\.(\d+)\.(\d+)R(\d+)$", info.firmware_version or "")
        if (
            info.model in {"SDS1104X-E", "SDS1204X-E"}
            and revision
            and tuple(map(int, revision.groups())) >= (6, 1, 36, 6)
        ):
            await self._write(":ACQ:CSW")
        # INR reads and clears the event latch. Never accept a previous bit 0.
        await self._query("INR?")
        self._armed_at = datetime.now(UTC).isoformat()
        await self._write(f"TRMD {mode}")
        actual = (await self._query("TRMD?")).split()[-1].upper()
        if actual not in ({"AUTO"} if mode == "AUTO" else {"SINGLE", "STOP"}):
            raise InstrumentError(f"Requested {mode}, instrument returned {actual}")

    @serialized
    async def wait_for_trigger(self, timeout: float = 10.0) -> bool:
        if self._armed_at is None:
            raise InstrumentError("No acquisition was armed by this connection")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        try:
            async with asyncio.timeout(timeout):
                while True:
                    response = await self._query("INR?")
                    if integer(response, "INR") & 1:
                        self._freshness = {
                            "armed_at": self._armed_at,
                            "observed_at": datetime.now(UTC).isoformat(),
                            "inr": response,
                        }
                        return True
                    await asyncio.sleep(0.05)
        except TimeoutError:
            return False

    @serialized
    async def capture_waveform(
        self,
        channel: str = "CH1",
        points: int | None = None,
        *,
        acquisition: str = "auto",
        timeout: float = 10.0,
    ) -> WaveformData:
        """Acquire AUTO, arm SINGLE, or finish this connection's armed shot.

        The whole acquisition/readback/transfer is serialized. Failure never
        falls back to the previous waveform. Successful captures leave STOP.
        """
        ch = self._normalize_channel(channel)
        if points is not None and (
            isinstance(points, bool) or not isinstance(points, int) or points <= 0
        ):
            raise ValueError("points must be a positive integer")
        if acquisition not in {"auto", "single", "armed"}:
            raise ValueError("acquisition must be auto, single, or armed")
        info = await self.get_info()
        if info.model not in {"SDS1104X-E", "SDS1204X-E", "SDS1102X-E", "SDS1202X-E"}:
            raise InstrumentError(f"Unqualified waveform model: {info.model}")
        # Other acquisition modes can mix old sweeps or alter the sample layout.
        acqw = await self._query("ACQW?")
        if acqw.split()[-1].upper() != "SAMPLING":
            raise InstrumentError("Fresh capture requires ACQW SAMPLING; configure explicitly")
        try:
            if acquisition != "armed":
                await self._begin_acquisition(acquisition.upper())
            if self._freshness is None and not await self.wait_for_trigger(timeout):
                raise InstrumentError("No fresh acquisition before deadline; no waveform returned")
            events = [self._freshness]
            if acquisition == "auto":
                self._freshness = None
                await asyncio.sleep(0.3)
                if not await self.wait_for_trigger(timeout):
                    raise InstrumentError("No second fresh AUTO acquisition before deadline")
                events.append(self._freshness)
            await self._write("STOP")
            freshness = {"events": events}
            self._freshness = None
            self._armed_at = None
            await self._write(f"WFSU SP,1,NP,{points or 0},FP,0")
            commands = [
                "*IDN?",
                "TRMD?",
                "TDIV?",
                "TRDL?",
                "SARA?",
                "WFSU?",
                "ACQW?",
                "BWL?",
                "TRSE?",
                f"{ch}:VDIV?",
                f"{ch}:OFST?",
                f"{ch}:ATTN?",
                f"{ch}:CPL?",
                f"{ch}:TRA?",
            ]
            setup = {command: await self._query(command) for command in commands}
            if setup["TRMD?"].split()[-1].upper() != "STOP":
                raise InstrumentError("Acquisition did not stop")
            if setup[f"{ch}:TRA?"].split()[-1].upper() != "ON":
                raise InstrumentError(f"{ch} is not enabled")
            transfer = re.fullmatch(
                r"(?:WFSU|WAVEFORM_SETUP)?\s*SP,(\d+),NP,(\d+),FP,(\d+)",
                setup["WFSU?"],
                re.IGNORECASE,
            )
            if not transfer or tuple(map(int, transfer.groups())) not in {
                (1, points or 0, 0),
                (0, points or 0, 0),
            }:
                raise InstrumentError(f"Unexpected transfer setup: {setup['WFSU?']}")
            rate = number(setup["SARA?"], "SARA")
            vdiv = number(setup[f"{ch}:VDIV?"], f"{ch}:VDIV")
            offset = number(setup[f"{ch}:OFST?"], f"{ch}:OFST")
            tdiv = number(setup["TDIV?"], "TDIV")
            delay = number(setup["TRDL?"], "TRDL")
            attenuation = number(setup[f"{ch}:ATTN?"], f"{ch}:ATTN")
            if min(rate, vdiv, tdiv, attenuation) <= 0:
                raise InstrumentError("Invalid acquisition metadata")
            raw = await self._query_binary(f"{ch}:WF? DAT2")
            if points is not None and len(raw) > points:
                raise InstrumentError("Scope ignored requested transfer point limit")
            codes = np.frombuffer(raw, dtype=np.int8)
            return WaveformData(
                channel=channel,
                samples=codes.astype(np.float64) * (vdiv / 25) - offset,
                sample_rate=rate,
                time_offset=delay - 7 * tdiv,
                voltage_scale=vdiv,
                voltage_offset=offset,
                record_length=len(raw),
                raw_data=raw,
                raw_response=self._raw_response,
                metadata={
                    "setup": setup,
                    "freshness": freshness,
                    "acquisition": acquisition,
                    "acquired_points": len(raw) if points is None else None,
                    "transfer_point_limit": points,
                    "partial_transfer": None if points else False,
                    "attenuation": attenuation,
                    "attenuation_already_in_vdiv": True,
                    "clipping_detected": bool(np.any((codes <= -125) | (codes >= 125))),
                    "clipping_check": "ADC endpoint heuristic; inspect trace and probe limits",
                    "timing_source": "SARA, TDIV, TRDL; SP=1, FP=0",
                    "acquired_at": datetime.now(UTC).isoformat(),
                },
            )
        finally:
            self._armed_at = None
            self._freshness = None
            if self._connected:
                await self._write("STOP")

    # =========================================================================
    # Measurements
    # =========================================================================

    @serialized
    async def measure_frequency(self, channel: str) -> Measurement:
        """Measure signal frequency."""
        ch = self._normalize_channel(channel)
        await self._write(f"PACU FREQ,{ch}")
        response = await self._query(f"{ch}:PAVA? FREQ")
        value = self._parse_measurement(response)
        return Measurement("frequency", value, "Hz", channel)

    @serialized
    async def measure_period(self, channel: str) -> Measurement:
        """Measure signal period."""
        ch = self._normalize_channel(channel)
        await self._write(f"PACU PER,{ch}")
        response = await self._query(f"{ch}:PAVA? PER")
        value = self._parse_measurement(response)
        return Measurement("period", value, "s", channel)

    @serialized
    async def measure_amplitude(self, channel: str) -> Measurement:
        """Measure signal amplitude (Vpp)."""
        ch = self._normalize_channel(channel)
        await self._write(f"PACU PKPK,{ch}")
        response = await self._query(f"{ch}:PAVA? PKPK")
        value = self._parse_measurement(response)
        return Measurement("amplitude", value, "V", channel)

    @serialized
    async def measure_vmax(self, channel: str) -> Measurement:
        """Measure maximum voltage."""
        ch = self._normalize_channel(channel)
        await self._write(f"PACU MAX,{ch}")
        response = await self._query(f"{ch}:PAVA? MAX")
        value = self._parse_measurement(response)
        return Measurement("vmax", value, "V", channel)

    @serialized
    async def measure_vmin(self, channel: str) -> Measurement:
        """Measure minimum voltage."""
        ch = self._normalize_channel(channel)
        await self._write(f"PACU MIN,{ch}")
        response = await self._query(f"{ch}:PAVA? MIN")
        value = self._parse_measurement(response)
        return Measurement("vmin", value, "V", channel)

    @serialized
    async def measure_vrms(self, channel: str) -> Measurement:
        """Measure RMS voltage."""
        ch = self._normalize_channel(channel)
        await self._write(f"PACU RMS,{ch}")
        response = await self._query(f"{ch}:PAVA? RMS")
        value = self._parse_measurement(response)
        return Measurement("vrms", value, "V", channel)

    @serialized
    async def measure_rise_time(self, channel: str) -> Measurement:
        """Measure rise time (10-90%)."""
        ch = self._normalize_channel(channel)
        await self._write(f"PACU RISE,{ch}")
        response = await self._query(f"{ch}:PAVA? RISE")
        value = self._parse_measurement(response)
        return Measurement("rise_time", value, "s", channel)

    @serialized
    async def measure_fall_time(self, channel: str) -> Measurement:
        """Measure fall time (90-10%)."""
        ch = self._normalize_channel(channel)
        await self._write(f"PACU FALL,{ch}")
        response = await self._query(f"{ch}:PAVA? FALL")
        value = self._parse_measurement(response)
        return Measurement("fall_time", value, "s", channel)

    @serialized
    async def measure_duty_cycle(self, channel: str) -> Measurement:
        """Measure duty cycle."""
        ch = self._normalize_channel(channel)
        await self._write(f"PACU DUTY,{ch}")
        response = await self._query(f"{ch}:PAVA? DUTY")
        value = self._parse_measurement(response)
        return Measurement("duty_cycle", value, "%", channel)

    @serialized
    async def measure(self, channel: str, measurement_type: str) -> Measurement:
        """Take a measurement of the specified type.

        Args:
            channel: Channel to measure.
            measurement_type: One of: frequency, period, amplitude, vpp,
                              vmax, vmin, vrms, rise_time, fall_time, duty_cycle

        Returns:
            Measurement result.
        """
        measurement_map = {
            "frequency": self.measure_frequency,
            "period": self.measure_period,
            "amplitude": self.measure_amplitude,
            "vpp": self.measure_amplitude,
            "vmax": self.measure_vmax,
            "vmin": self.measure_vmin,
            "vrms": self.measure_vrms,
            "rise_time": self.measure_rise_time,
            "fall_time": self.measure_fall_time,
            "duty_cycle": self.measure_duty_cycle,
        }

        func = measurement_map.get(measurement_type.lower())
        if func is None:
            raise InstrumentError(f"Unknown measurement type: {measurement_type}")

        return await func(channel)

    # =========================================================================
    # Screenshot
    # =========================================================================

    @serialized
    async def screenshot(self) -> bytes:
        # SDS1000X-E SCDP is a BMP stream, not a SCPI block or PNG.
        async with asyncio.timeout(self.timeout):
            try:
                await self._write("SCDP")
                header = await self._reader.readexactly(14)
                size = int.from_bytes(header[2:6], "little")
                if header[:2] != b"BM" or not 14 <= size <= 32_000_000:
                    raise InstrumentError("Invalid BMP screenshot header")
                return header + await self._reader.readexactly(size - 14)
            except BaseException:
                await self.disconnect()
                raise

    def _normalize_channel(self, channel: str) -> str:
        ch = channel.upper().removeprefix("CH").removeprefix("C")
        if ch not in {"1", "2", "3", "4"}:
            raise ValueError(f"Invalid analog channel: {channel}")
        return f"C{ch}"

    @serialized
    async def _write(self, command: str) -> None:
        if not self._connected or not self._writer:
            raise InstrumentError("Not connected")
        if not command.strip() or "\n" in command or "\r" in command:
            raise ValueError("SCPI command must be one nonempty line")
        try:
            async with asyncio.timeout(self.timeout):
                self._writer.write((command + "\n").encode("ascii"))
                await self._writer.drain()
            self.transcript.append({"utc": datetime.now(UTC).isoformat(), "command": command})
        except BaseException:
            await self.disconnect()
            raise

    @serialized
    async def _query(self, command: str) -> str:
        try:
            async with asyncio.timeout(self.timeout):
                await self._write(command)
                # Permit a delayed extra LF after a binary block, never empty data.
                for _ in range(3):
                    response = await self._reader.readline()
                    if not response:
                        raise InstrumentError("EOF waiting for SCPI response")
                    if response.strip():
                        result = response.decode("ascii").strip()
                        self.transcript[-1]["response"] = result
                        return result
                raise InstrumentError("Empty SCPI response")
        except BaseException:
            await self.disconnect()
            raise

    @serialized
    async def _query_binary(self, command: str) -> bytes:
        try:
            async with asyncio.timeout(self.timeout):
                await self._write(command)
                prefix = bytearray()
                while len(prefix) < 128:
                    byte = await self._reader.readexactly(1)
                    prefix.extend(byte)
                    if byte == b"#":
                        break
                else:
                    raise InstrumentError("Missing bounded SCPI block header")
                lead = bytes(prefix[:-1]).strip()
                expected_channel = command.split(":", 1)[0].encode()
                if lead and not re.fullmatch(
                    expected_channel + rb":(?:WF|WAVEFORM)\s+DAT2,?", lead
                ):
                    raise InstrumentError(f"Unexpected waveform prefix: {lead!r}")
                digits = await self._reader.readexactly(1)
                if digits not in b"123456789" or len(digits) != 1:
                    raise InstrumentError("Invalid definite block length")
                length = await self._reader.readexactly(int(digits))
                if not length.isdigit():
                    raise InstrumentError("Invalid block byte count")
                size = int(length)
                if not 0 < size <= 32_000_000:
                    raise InstrumentError(f"Invalid or empty waveform length: {size}")
                payload = await self._reader.readexactly(size)
                # Manufacturer specifies LF LF. CR LF is accepted as a transport variant.
                trailer = await self._reader.readexactly(2)
                if trailer not in (b"\n\n", b"\r\n"):
                    raise InstrumentError(f"Invalid waveform trailer: {trailer!r}")
                self._raw_response = bytes(prefix) + digits + length + payload + trailer
                self.transcript[-1]["binary_bytes"] = size
                return payload
        except BaseException:
            # Never reuse a stream whose framing is uncertain, including cancellation.
            await self.disconnect()
            raise

    def _parse_measurement(self, response: str) -> float:
        value = response.split(",")[-1].strip()
        if "*" in value:
            raise InstrumentError(f"Measurement unavailable: {response}")
        return number(value)

    def _parse_value_with_unit(self, value_str: str) -> float:
        return number(value_str)
