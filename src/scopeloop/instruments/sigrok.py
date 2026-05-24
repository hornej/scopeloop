"""sigrok-based instrument drivers.

Provides unified access to oscilloscopes, logic analyzers, and other
measurement devices supported by the sigrok project.

Uses pysigrok (pure Python reimplementation) for easy installation
and broad device compatibility.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import tempfile
from abc import abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from scopeloop.instruments.base import Instrument, InstrumentError, InstrumentInfo

logger = logging.getLogger(__name__)


# Check if pysigrok is available
try:
    import pysigrok

    PYSIGROK_AVAILABLE = True
except ImportError:
    PYSIGROK_AVAILABLE = False


@dataclass
class WaveformData:
    """Captured waveform data from an oscilloscope."""

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

    def to_dict(self) -> dict[str, Any]:
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
        }


@dataclass
class Measurement:
    """A single measurement result."""

    measurement_type: str
    value: float
    unit: str
    channel: str
    timestamp: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.measurement_type,
            "value": self.value,
            "unit": self.unit,
            "channel": self.channel,
            "timestamp": self.timestamp,
        }


@dataclass
class LogicCapture:
    """Captured logic analyzer data."""

    channels: list[str]
    sample_rate: float
    samples: np.ndarray  # Shape: (num_samples, num_channels)
    duration: float
    trigger_position: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "channels": self.channels,
            "sample_rate": self.sample_rate,
            "num_samples": len(self.samples),
            "duration": self.duration,
            "trigger_position": self.trigger_position,
        }


@dataclass
class DecodedData:
    """Decoded protocol data from a logic analyzer."""

    protocol: str
    decoder: str
    annotations: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "decoder": self.decoder,
            "annotation_count": len(self.annotations),
            "annotations": self.annotations[:100],  # Limit for JSON
        }


class SigrokDevice(Instrument):
    """Base class for sigrok-backed devices.

    Uses pysigrok-cli subprocess calls for device communication.
    This approach is more reliable than direct Python bindings and
    leverages the full sigrok ecosystem.
    """

    def __init__(
        self,
        driver: str,
        connection: str | None = None,
        timeout: float = 30.0,
    ):
        """Initialize a sigrok device.

        Args:
            driver: sigrok driver name (e.g., "siglent-sds", "fx2lafw").
            connection: Connection string (e.g., "tcp-raw/192.0.2.100/5025").
            timeout: Command timeout in seconds.
        """
        self.driver = driver
        self.connection = connection
        self.timeout = timeout
        self._connected = False
        self._device_info: InstrumentInfo | None = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    def _build_device_arg(self) -> str:
        """Build the -d argument for pysigrok-cli."""
        if self.connection:
            return f"{self.driver}:conn={self.connection}"
        return self.driver

    async def _run_sigrok_cli(
        self,
        args: list[str],
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run pysigrok-cli with the given arguments.

        Args:
            args: Command line arguments (without pysigrok-cli).
            timeout: Override default timeout.

        Returns:
            CompletedProcess with stdout/stderr.

        Raises:
            InstrumentError: If command fails.
        """
        cmd = ["pysigrok-cli", "-d", self._build_device_arg()] + args
        logger.debug(f"Running: {' '.join(cmd)}")

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout or self.timeout,
            )

            result = subprocess.CompletedProcess(
                cmd,
                process.returncode or 0,
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
            )

            if result.returncode != 0:
                raise InstrumentError(
                    f"sigrok command failed: {result.stderr or result.stdout}"
                )

            return result

        except asyncio.TimeoutError:
            raise InstrumentError(f"sigrok command timed out after {timeout}s")
        except FileNotFoundError:
            raise InstrumentError(
                "pysigrok-cli not found. Install with: pip install pysigrok"
            )

    async def connect(self) -> None:
        """Connect to the device and verify communication."""
        if self._connected:
            return

        logger.info(f"Connecting to sigrok device: {self.driver}")

        # Try to get device info to verify connection
        try:
            # Use --show to get device information
            result = await self._run_sigrok_cli(["--show"])
            self._connected = True
            logger.info(f"Connected to sigrok device: {self.driver}")
            logger.debug(f"Device info: {result.stdout}")
        except InstrumentError as e:
            self._connected = False
            raise InstrumentError(f"Failed to connect to {self.driver}: {e}")

    async def disconnect(self) -> None:
        """Disconnect from the device."""
        self._connected = False
        self._device_info = None
        logger.info(f"Disconnected from sigrok device: {self.driver}")

    async def get_info(self) -> InstrumentInfo:
        """Get device information."""
        if self._device_info:
            return self._device_info

        try:
            result = await self._run_sigrok_cli(["--show"])

            # Parse the output to extract device info
            model = self.driver
            serial = None
            firmware = None

            for line in result.stdout.split("\n"):
                line = line.strip()
                if ":" in line:
                    key, _, value = line.partition(":")
                    key = key.strip().lower()
                    value = value.strip()

                    if "model" in key:
                        model = value
                    elif "serial" in key:
                        serial = value
                    elif "firmware" in key or "version" in key:
                        firmware = value

            self._device_info = InstrumentInfo(
                instrument_type=self._get_instrument_type(),
                model=model,
                serial=serial,
                firmware_version=firmware,
                address=self.connection,
            )
            return self._device_info

        except InstrumentError:
            return InstrumentInfo(
                instrument_type=self._get_instrument_type(),
                model=self.driver,
                address=self.connection,
            )

    @abstractmethod
    def _get_instrument_type(self) -> str:
        """Return the instrument type string."""
        pass


class SigrokOscilloscope(SigrokDevice):
    """Oscilloscope driver using sigrok.

    Supports Siglent, Rigol, and other sigrok-compatible oscilloscopes.

    Usage:
        scope = SigrokOscilloscope(
            driver="siglent-sds",
            connection="tcp-raw/192.0.2.100/5025"
        )

        async with scope:
            waveform = await scope.capture_waveform("CH1")
            print(f"Vpp: {waveform.vpp}V")
    """

    def __init__(
        self,
        driver: str = "siglent-sds",
        connection: str | None = None,
        timeout: float = 30.0,
    ):
        super().__init__(driver, connection, timeout)
        self._channel_config: dict[str, dict[str, Any]] = {}

    def _get_instrument_type(self) -> str:
        return "oscilloscope"

    async def capture_waveform(
        self,
        channel: str = "CH1",
        num_samples: int | None = None,
    ) -> WaveformData:
        """Capture waveform data from a channel.

        Args:
            channel: Channel name (e.g., "CH1", "CH2").
            num_samples: Number of samples to capture (None = auto).

        Returns:
            WaveformData with samples and metadata.
        """
        if not self._connected:
            raise InstrumentError("Not connected")

        # Normalize channel name
        ch = channel.upper()
        if not ch.startswith("CH"):
            ch = f"CH{ch}"

        args = ["-C", ch]
        if num_samples:
            args.extend(["--samples", str(num_samples)])

        # Create temp file for output
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            output_path = Path(f.name)

        try:
            args.extend(["-O", "csv", "-o", str(output_path)])
            await self._run_sigrok_cli(args)

            # Parse CSV output
            samples, sample_rate = self._parse_analog_csv(output_path)

            # Get channel config if available
            config = self._channel_config.get(ch, {})

            return WaveformData(
                channel=channel,
                samples=samples,
                sample_rate=sample_rate,
                time_offset=0.0,
                voltage_scale=config.get("scale", 1.0),
                voltage_offset=config.get("offset", 0.0),
                record_length=len(samples),
            )

        finally:
            output_path.unlink(missing_ok=True)

    def _parse_analog_csv(self, path: Path) -> tuple[np.ndarray, float]:
        """Parse analog data from sigrok CSV output."""
        samples = []
        sample_rate = 1.0

        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith(";"):
                    # Check for sample rate in comments
                    if "samplerate" in line.lower():
                        # Parse something like "; Sample rate: 1000000"
                        parts = line.split(":")
                        if len(parts) >= 2:
                            try:
                                sample_rate = float(
                                    parts[-1].strip().replace(" ", "")
                                )
                            except ValueError:
                                pass
                    continue

                # Parse data line
                parts = line.split(",")
                if parts:
                    try:
                        # First column is usually time or index, second is value
                        if len(parts) >= 2:
                            samples.append(float(parts[1]))
                        else:
                            samples.append(float(parts[0]))
                    except ValueError:
                        continue

        return np.array(samples, dtype=np.float64), sample_rate

    async def set_channel_scale(self, channel: str, volts_per_div: float) -> None:
        """Set the vertical scale for a channel.

        Note: This stores the config locally. Actual device configuration
        depends on the specific sigrok driver capabilities.
        """
        ch = channel.upper()
        if ch not in self._channel_config:
            self._channel_config[ch] = {}
        self._channel_config[ch]["scale"] = volts_per_div

        # Try to set on device via config option
        try:
            await self._run_sigrok_cli(
                ["-c", f"vdiv={volts_per_div}", "-C", ch],
                timeout=5.0,
            )
        except InstrumentError:
            logger.debug(f"Could not set vdiv on device, stored locally")

    async def set_channel_offset(self, channel: str, offset: float) -> None:
        """Set the vertical offset for a channel."""
        ch = channel.upper()
        if ch not in self._channel_config:
            self._channel_config[ch] = {}
        self._channel_config[ch]["offset"] = offset

    async def set_timebase(self, seconds_per_div: float) -> None:
        """Set the horizontal timebase."""
        try:
            await self._run_sigrok_cli(
                ["-c", f"timebase={seconds_per_div}"],
                timeout=5.0,
            )
        except InstrumentError:
            logger.debug(f"Could not set timebase on device")

    async def set_sample_rate(self, sample_rate: float) -> None:
        """Set the sample rate."""
        try:
            await self._run_sigrok_cli(
                ["-c", f"samplerate={int(sample_rate)}"],
                timeout=5.0,
            )
        except InstrumentError:
            logger.debug(f"Could not set sample rate on device")

    async def measure(self, channel: str, measurement_type: str) -> Measurement:
        """Take a measurement on a channel.

        Captures waveform and computes measurement in software.

        Args:
            channel: Channel to measure.
            measurement_type: One of: frequency, period, vpp, vmax, vmin, vrms,
                            amplitude, duty_cycle.

        Returns:
            Measurement result.
        """
        waveform = await self.capture_waveform(channel)
        return self._compute_measurement(waveform, measurement_type)

    def _compute_measurement(
        self, waveform: WaveformData, measurement_type: str
    ) -> Measurement:
        """Compute a measurement from waveform data."""
        mtype = measurement_type.lower()
        samples = waveform.samples

        if mtype in ("vpp", "amplitude"):
            value = float(np.max(samples) - np.min(samples))
            unit = "V"
        elif mtype == "vmax":
            value = float(np.max(samples))
            unit = "V"
        elif mtype == "vmin":
            value = float(np.min(samples))
            unit = "V"
        elif mtype == "vrms":
            value = float(np.sqrt(np.mean(samples**2)))
            unit = "V"
        elif mtype == "frequency":
            value = self._compute_frequency(samples, waveform.sample_rate)
            unit = "Hz"
        elif mtype == "period":
            freq = self._compute_frequency(samples, waveform.sample_rate)
            value = 1.0 / freq if freq > 0 else 0.0
            unit = "s"
        elif mtype == "duty_cycle":
            value = self._compute_duty_cycle(samples)
            unit = "%"
        else:
            raise InstrumentError(f"Unknown measurement type: {measurement_type}")

        return Measurement(
            measurement_type=mtype,
            value=value,
            unit=unit,
            channel=waveform.channel,
        )

    def _compute_frequency(self, samples: np.ndarray, sample_rate: float) -> float:
        """Compute frequency using FFT."""
        if len(samples) < 4:
            return 0.0

        # Remove DC offset
        samples = samples - np.mean(samples)

        # FFT
        fft = np.fft.rfft(samples)
        freqs = np.fft.rfftfreq(len(samples), 1.0 / sample_rate)

        # Find peak (skip DC)
        magnitudes = np.abs(fft)
        if len(magnitudes) > 1:
            peak_idx = np.argmax(magnitudes[1:]) + 1
            return float(freqs[peak_idx])

        return 0.0

    def _compute_duty_cycle(self, samples: np.ndarray) -> float:
        """Compute duty cycle assuming a digital-like signal."""
        if len(samples) == 0:
            return 0.0

        threshold = (np.max(samples) + np.min(samples)) / 2
        high_samples = np.sum(samples > threshold)
        return float(high_samples / len(samples) * 100)


class SigrokLogicAnalyzer(SigrokDevice):
    """Logic analyzer driver using sigrok.

    Supports fx2lafw, saleae-logic (older), and many other devices.

    Usage:
        la = SigrokLogicAnalyzer(driver="fx2lafw")

        async with la:
            capture = await la.capture(
                channels=["D0", "D1", "D2", "D3"],
                sample_rate=24_000_000,
                num_samples=10000,
            )

            # Decode SPI
            decoded = await la.decode(
                capture,
                decoder="spi",
                options={"clk": "D0", "mosi": "D1", "cs": "D2"},
            )
    """

    def __init__(
        self,
        driver: str = "fx2lafw",
        connection: str | None = None,
        timeout: float = 30.0,
    ):
        super().__init__(driver, connection, timeout)

    def _get_instrument_type(self) -> str:
        return "logic_analyzer"

    async def capture(
        self,
        channels: list[str],
        sample_rate: int = 1_000_000,
        num_samples: int | None = None,
        duration: float | None = None,
    ) -> LogicCapture:
        """Capture digital data from specified channels.

        Args:
            channels: List of channel names (e.g., ["D0", "D1"]).
            sample_rate: Sample rate in Hz.
            num_samples: Number of samples (mutually exclusive with duration).
            duration: Capture duration in seconds.

        Returns:
            LogicCapture with sample data.
        """
        if not self._connected:
            raise InstrumentError("Not connected")

        if not channels:
            raise InstrumentError("At least one channel required")

        args = [
            "-C",
            ",".join(channels),
            "-c",
            f"samplerate={sample_rate}",
        ]

        if num_samples:
            args.extend(["--samples", str(num_samples)])
        elif duration:
            args.extend(["--time", str(int(duration * 1000))])
        else:
            args.extend(["--samples", "10000"])

        # Output to CSV
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            output_path = Path(f.name)

        try:
            args.extend(["-O", "csv", "-o", str(output_path)])
            await self._run_sigrok_cli(args)

            # Parse CSV
            samples = self._parse_logic_csv(output_path, len(channels))

            actual_samples = len(samples) if len(samples) > 0 else 0
            actual_duration = actual_samples / sample_rate if sample_rate > 0 else 0

            return LogicCapture(
                channels=channels,
                sample_rate=float(sample_rate),
                samples=samples,
                duration=actual_duration,
            )

        finally:
            output_path.unlink(missing_ok=True)

    def _parse_logic_csv(self, path: Path, num_channels: int) -> np.ndarray:
        """Parse logic data from sigrok CSV output."""
        samples = []

        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith(";"):
                    continue

                parts = line.split(",")
                if len(parts) >= num_channels:
                    try:
                        row = [int(p.strip()) for p in parts[:num_channels]]
                        samples.append(row)
                    except ValueError:
                        continue

        return np.array(samples, dtype=np.uint8)

    async def decode(
        self,
        capture: LogicCapture,
        decoder: str,
        options: dict[str, Any] | None = None,
    ) -> DecodedData:
        """Decode captured data using a protocol decoder.

        Args:
            capture: Previously captured data.
            decoder: Decoder name (e.g., "spi", "i2c", "uart").
            options: Decoder options (channel mappings, settings).

        Returns:
            DecodedData with decoded annotations.
        """
        # Save capture to temp file
        with tempfile.NamedTemporaryFile(suffix=".sr", delete=False) as f:
            capture_path = Path(f.name)

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            output_path = Path(f.name)

        try:
            # Save capture as sigrok session file
            self._save_capture(capture, capture_path)

            # Build decoder options string
            decoder_arg = decoder
            if options:
                opt_parts = [f"{k}={v}" for k, v in options.items()]
                decoder_arg = f"{decoder}:{':'.join(opt_parts)}"

            # Run decoder
            cmd = [
                "pysigrok-cli",
                "-i",
                str(capture_path),
                "-P",
                decoder_arg,
                "-A",
                decoder,
                "-o",
                str(output_path),
            ]

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.timeout,
            )

            # Parse annotations
            annotations = self._parse_annotations(output_path)

            return DecodedData(
                protocol=decoder,
                decoder=decoder,
                annotations=annotations,
            )

        except Exception as e:
            logger.warning(f"Decode failed: {e}")
            return DecodedData(protocol=decoder, decoder=decoder, annotations=[])

        finally:
            capture_path.unlink(missing_ok=True)
            output_path.unlink(missing_ok=True)

    def _save_capture(self, capture: LogicCapture, path: Path) -> None:
        """Save capture data to a sigrok session file."""
        # For simplicity, save as CSV that can be re-read
        with open(path, "w") as f:
            f.write(f"; Sample rate: {int(capture.sample_rate)}\n")
            f.write(f"; Channels: {','.join(capture.channels)}\n")
            for row in capture.samples:
                f.write(",".join(str(v) for v in row) + "\n")

    def _parse_annotations(self, path: Path) -> list[dict[str, Any]]:
        """Parse decoder annotations from output file."""
        annotations = []

        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    # Parse annotation format: start-end decoder: data
                    # Format varies by decoder
                    annotations.append({"raw": line})

        except Exception as e:
            logger.debug(f"Could not parse annotations: {e}")

        return annotations

    async def list_decoders(self) -> list[str]:
        """List available protocol decoders."""
        try:
            cmd = ["pysigrok-cli", "-L"]
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, _ = await process.communicate()
            output = stdout.decode("utf-8", errors="replace")

            decoders = []
            in_decoders = False

            for line in output.split("\n"):
                if "Supported protocol decoders" in line:
                    in_decoders = True
                    continue
                if in_decoders and line.strip():
                    if line.startswith("  "):
                        # Decoder name is first word
                        parts = line.strip().split()
                        if parts:
                            decoders.append(parts[0])
                    elif not line.startswith(" "):
                        # New section
                        break

            return decoders

        except Exception:
            return []


async def list_sigrok_devices() -> list[dict[str, Any]]:
    """List all available sigrok devices.

    Returns:
        List of device info dictionaries.
    """
    try:
        cmd = ["pysigrok-cli", "--list-serial"]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, _ = await process.communicate()
        output = stdout.decode("utf-8", errors="replace")

        devices = []
        for line in output.split("\n"):
            line = line.strip()
            if line and not line.startswith(";"):
                devices.append({"port": line})

        return devices

    except Exception:
        return []


async def list_sigrok_drivers() -> list[str]:
    """List available sigrok drivers.

    Returns:
        List of driver names.
    """
    try:
        cmd = ["pysigrok-cli", "-L"]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, _ = await process.communicate()
        output = stdout.decode("utf-8", errors="replace")

        drivers = []
        in_drivers = False

        for line in output.split("\n"):
            if "Supported hardware drivers" in line:
                in_drivers = True
                continue
            if in_drivers and line.strip():
                if line.startswith("  "):
                    parts = line.strip().split()
                    if parts:
                        drivers.append(parts[0])
                elif not line.startswith(" "):
                    break

        return drivers

    except Exception:
        return []
