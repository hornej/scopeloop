"""Siglent SDS1000X-E series oscilloscope driver.

Uses SCPI commands over LAN (TCP/IP) via PyVISA.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass
from typing import Any

import numpy as np

from scopeloop.instruments.base import Instrument, InstrumentError, InstrumentInfo

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
        }


class SiglentSDS1000X(Instrument):
    """Siglent SDS1000X-E series oscilloscope driver.

    Supports SDS1104X-E, SDS1204X-E, and similar models.

    Usage:
        scope = SiglentSDS1000X("192.168.1.100")

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
        self._lock = asyncio.Lock()
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        """Connect to the oscilloscope."""
        if self._connected:
            return

        logger.info(f"Connecting to Siglent scope at {self.address}:{self.port}")

        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.address, self.port),
                timeout=self.timeout,
            )
            self._connected = True

            # Clear any pending data
            await self._write("*CLS")

            # Verify connection
            idn = await self._query("*IDN?")
            logger.info(f"Connected to: {idn}")

        except Exception as e:
            self._connected = False
            raise InstrumentError(f"Failed to connect to oscilloscope: {e}") from e

    async def disconnect(self) -> None:
        """Disconnect from the oscilloscope."""
        if not self._connected:
            return

        logger.info("Disconnecting from oscilloscope")

        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass

        self._reader = None
        self._writer = None
        self._connected = False

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

    # =========================================================================
    # Channel Configuration
    # =========================================================================

    async def set_channel_scale(self, channel: str, volts_per_div: float) -> None:
        """Set the vertical scale for a channel.

        Args:
            channel: Channel name (e.g., "CH1").
            volts_per_div: Volts per division.
        """
        ch = self._normalize_channel(channel)
        await self._write(f"{ch}:VDIV {volts_per_div}")

    async def get_channel_scale(self, channel: str) -> float:
        """Get the vertical scale for a channel."""
        ch = self._normalize_channel(channel)
        response = await self._query(f"{ch}:VDIV?")
        return float(response.split()[-1])

    async def set_channel_offset(self, channel: str, offset: float) -> None:
        """Set the vertical offset for a channel.

        Args:
            channel: Channel name.
            offset: Offset in volts.
        """
        ch = self._normalize_channel(channel)
        await self._write(f"{ch}:OFST {offset}")

    async def set_channel_coupling(self, channel: str, coupling: str) -> None:
        """Set the input coupling for a channel.

        Args:
            channel: Channel name.
            coupling: "DC", "AC", or "GND".
        """
        ch = self._normalize_channel(channel)
        await self._write(f"{ch}:CPL {coupling.upper()}")

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

    async def set_timebase(self, seconds_per_div: float) -> None:
        """Set the horizontal timebase.

        Args:
            seconds_per_div: Seconds per division.
        """
        await self._write(f"TDIV {seconds_per_div}")

    async def get_timebase(self) -> float:
        """Get the horizontal timebase in seconds per division."""
        response = await self._query("TDIV?")
        return float(response.split()[-1])

    # =========================================================================
    # Trigger Configuration
    # =========================================================================

    async def set_trigger_level(self, level: float, source: str = "CH1") -> None:
        """Set the trigger level.

        Args:
            level: Trigger level in volts.
            source: Trigger source channel.
        """
        src = self._normalize_channel(source)
        await self._write(f"{src}:TRLV {level}")

    async def set_trigger_mode(self, mode: str) -> None:
        """Set the trigger mode.

        Args:
            mode: "AUTO", "NORM", "SINGLE", or "STOP".
        """
        await self._write(f"TRMD {mode.upper()}")

    async def force_trigger(self) -> None:
        """Force a trigger."""
        await self._write("FRTR")

    # =========================================================================
    # Acquisition
    # =========================================================================

    async def run(self) -> None:
        """Start acquisition."""
        await self._write("TRMD AUTO")

    async def stop(self) -> None:
        """Stop acquisition."""
        await self._write("STOP")

    async def single(self) -> None:
        """Single acquisition."""
        await self._write("TRMD SINGLE")

    async def wait_for_trigger(self, timeout: float = 10.0) -> bool:
        """Wait for trigger to occur.

        Args:
            timeout: Maximum time to wait.

        Returns:
            True if triggered, False if timeout.
        """
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < timeout:
            status = await self._query("INR?")
            # Bit 0 indicates trigger
            if int(status) & 1:
                return True
            await asyncio.sleep(0.1)
        return False

    # =========================================================================
    # Waveform Capture
    # =========================================================================

    async def capture_waveform(
        self,
        channel: str = "CH1",
        points: int | None = None,
    ) -> WaveformData:
        """Capture waveform data from a channel.

        Args:
            channel: Channel to capture.
            points: Number of points (None = all available).

        Returns:
            WaveformData object with samples and metadata.
        """
        ch = self._normalize_channel(channel)

        # Set up waveform transfer
        await self._write(f"WFSU SP,1,NP,{points or 0},FP,0")
        await self._write(f"{ch}:WF? DAT2")

        # Read waveform data
        raw_data = await self._read_binary()

        # Get waveform parameters
        vdiv = await self.get_channel_scale(channel)
        tdiv = await self.get_timebase()

        # Parse vertical parameters
        voffset = float((await self._query(f"{ch}:OFST?")).split()[-1])

        # Siglent sends 8-bit signed data, scaled by VDIV
        # Data format: header + data + terminator
        samples = np.frombuffer(raw_data, dtype=np.int8).astype(np.float64)

        # Scale to volts: value * (vdiv / 25) - voffset
        # The 25 is divisions of the 8-bit range per div
        samples = samples * (vdiv / 25.0) - voffset

        # Calculate sample rate from timebase
        # 14 divisions on screen, typical memory depth
        sample_rate = len(samples) / (14 * tdiv)

        return WaveformData(
            channel=channel,
            samples=samples,
            sample_rate=sample_rate,
            time_offset=0.0,  # Would need to query TRDL
            voltage_scale=vdiv,
            voltage_offset=voffset,
            record_length=len(samples),
        )

    # =========================================================================
    # Measurements
    # =========================================================================

    async def measure_frequency(self, channel: str) -> Measurement:
        """Measure signal frequency."""
        ch = self._normalize_channel(channel)
        await self._write(f"PACU FREQ,{ch}")
        response = await self._query(f"{ch}:PAVA? FREQ")
        value = self._parse_measurement(response)
        return Measurement("frequency", value, "Hz", channel)

    async def measure_period(self, channel: str) -> Measurement:
        """Measure signal period."""
        ch = self._normalize_channel(channel)
        await self._write(f"PACU PER,{ch}")
        response = await self._query(f"{ch}:PAVA? PER")
        value = self._parse_measurement(response)
        return Measurement("period", value, "s", channel)

    async def measure_amplitude(self, channel: str) -> Measurement:
        """Measure signal amplitude (Vpp)."""
        ch = self._normalize_channel(channel)
        await self._write(f"PACU PKPK,{ch}")
        response = await self._query(f"{ch}:PAVA? PKPK")
        value = self._parse_measurement(response)
        return Measurement("amplitude", value, "V", channel)

    async def measure_vmax(self, channel: str) -> Measurement:
        """Measure maximum voltage."""
        ch = self._normalize_channel(channel)
        await self._write(f"PACU MAX,{ch}")
        response = await self._query(f"{ch}:PAVA? MAX")
        value = self._parse_measurement(response)
        return Measurement("vmax", value, "V", channel)

    async def measure_vmin(self, channel: str) -> Measurement:
        """Measure minimum voltage."""
        ch = self._normalize_channel(channel)
        await self._write(f"PACU MIN,{ch}")
        response = await self._query(f"{ch}:PAVA? MIN")
        value = self._parse_measurement(response)
        return Measurement("vmin", value, "V", channel)

    async def measure_vrms(self, channel: str) -> Measurement:
        """Measure RMS voltage."""
        ch = self._normalize_channel(channel)
        await self._write(f"PACU RMS,{ch}")
        response = await self._query(f"{ch}:PAVA? RMS")
        value = self._parse_measurement(response)
        return Measurement("vrms", value, "V", channel)

    async def measure_rise_time(self, channel: str) -> Measurement:
        """Measure rise time (10-90%)."""
        ch = self._normalize_channel(channel)
        await self._write(f"PACU RISE,{ch}")
        response = await self._query(f"{ch}:PAVA? RISE")
        value = self._parse_measurement(response)
        return Measurement("rise_time", value, "s", channel)

    async def measure_fall_time(self, channel: str) -> Measurement:
        """Measure fall time (90-10%)."""
        ch = self._normalize_channel(channel)
        await self._write(f"PACU FALL,{ch}")
        response = await self._query(f"{ch}:PAVA? FALL")
        value = self._parse_measurement(response)
        return Measurement("fall_time", value, "s", channel)

    async def measure_duty_cycle(self, channel: str) -> Measurement:
        """Measure duty cycle."""
        ch = self._normalize_channel(channel)
        await self._write(f"PACU DUTY,{ch}")
        response = await self._query(f"{ch}:PAVA? DUTY")
        value = self._parse_measurement(response)
        return Measurement("duty_cycle", value, "%", channel)

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

    async def screenshot(self) -> bytes:
        """Capture a screenshot from the oscilloscope.

        Returns:
            PNG image data.
        """
        await self._write("SCDP")
        data = await self._read_binary()
        return data

    # =========================================================================
    # Internal Methods
    # =========================================================================

    def _normalize_channel(self, channel: str) -> str:
        """Normalize channel name to SCPI format."""
        ch = channel.upper()
        if ch.startswith("CH"):
            return f"C{ch[2:]}"
        elif ch.startswith("C") and ch[1:].isdigit():
            return ch
        else:
            return f"C{ch}"

    async def _write(self, command: str) -> None:
        """Send a command to the oscilloscope."""
        if not self._connected or not self._writer:
            raise InstrumentError("Not connected")

        async with self._lock:
            self._writer.write(f"{command}\n".encode())
            await self._writer.drain()
            logger.debug(f"SCPI write: {command}")

    async def _query(self, command: str) -> str:
        """Send a query and read the response."""
        if not self._connected or not self._reader:
            raise InstrumentError("Not connected")

        async with self._lock:
            self._writer.write(f"{command}\n".encode())
            await self._writer.drain()

            response = await asyncio.wait_for(
                self._reader.readline(),
                timeout=self.timeout,
            )
            result = response.decode().strip()
            logger.debug(f"SCPI query: {command} -> {result[:100]}...")
            return result

    async def _read_binary(self) -> bytes:
        """Read binary data block."""
        if not self._connected or not self._reader:
            raise InstrumentError("Not connected")

        async with self._lock:
            # Read header: #NXXXXX where N is number of digits and XXXXX is byte count
            header = await self._reader.read(2)
            if header[0:1] != b"#":
                raise InstrumentError(f"Invalid binary header: {header}")

            num_digits = int(header[1:2])
            size_bytes = await self._reader.read(num_digits)
            size = int(size_bytes)

            # Read data
            data = await self._reader.readexactly(size)

            # Read terminator
            await self._reader.readline()

            return data

    def _parse_measurement(self, response: str) -> float:
        """Parse a measurement response."""
        # Format: "PAVA parameter,value"
        try:
            parts = response.split(",")
            if len(parts) >= 2:
                value_str = parts[1].strip()
                # Handle special cases like "****" for invalid measurement
                if "*" in value_str:
                    return float("nan")
                # Parse value with unit suffix
                return self._parse_value_with_unit(value_str)
            return float(response)
        except (ValueError, IndexError) as e:
            logger.warning(f"Could not parse measurement: {response} - {e}")
            return float("nan")

    def _parse_value_with_unit(self, value_str: str) -> float:
        """Parse a value string that may have a unit suffix."""
        import re

        # Match number with optional unit
        match = re.match(r"([+-]?\d*\.?\d+(?:[eE][+-]?\d+)?)\s*(\w*)", value_str)
        if not match:
            return float(value_str)

        value = float(match.group(1))
        unit = match.group(2).lower()

        # Apply unit multiplier
        multipliers = {
            "mv": 1e-3,
            "uv": 1e-6,
            "nv": 1e-9,
            "kv": 1e3,
            "ms": 1e-3,
            "us": 1e-6,
            "ns": 1e-9,
            "khz": 1e3,
            "mhz": 1e6,
            "ghz": 1e9,
        }

        return value * multipliers.get(unit, 1.0)
