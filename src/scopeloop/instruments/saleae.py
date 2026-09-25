"""Saleae Logic 2 automation API driver.

Provides full-featured integration with Saleae Logic analyzers
(Logic 8, Logic Pro 8, Logic Pro 16) via the official automation API.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from scopeloop.instruments.base import Instrument, InstrumentError, InstrumentInfo
from scopeloop.resources import HostLease, TaskRLock, serialized

logger = logging.getLogger(__name__)


# Check if saleae automation is available
try:
    import grpc
    from saleae.automation import (
        CaptureConfiguration,
        DataTableExportConfiguration,
        DigitalTriggerCaptureMode,
        DigitalTriggerType,
        GlitchFilterEntry,
        LogicDeviceConfiguration,
        Manager,
        RadixType,
        TimedCaptureMode,
    )
    from saleae.grpc import saleae_pb2

    SALEAE_AVAILABLE = True
except ImportError:
    SALEAE_AVAILABLE = False
    grpc = None  # type: ignore
    saleae_pb2 = None  # type: ignore


class CaptureTimeoutError(InstrumentError):
    """Raised when a capture does not complete within its bounded wait."""


ProgressCallback = Callable[[dict[str, Any]], None]


class SaleaeDeviceType(StrEnum):
    """Saleae device types."""

    LOGIC_8 = "logic_8"
    LOGIC_PRO_8 = "logic_pro_8"
    LOGIC_PRO_16 = "logic_pro_16"


class VoltageThreshold(StrEnum):
    """Digital voltage thresholds for Logic Pro devices."""

    V_1_2 = "1.2V"
    V_1_8 = "1.8V"
    V_3_3 = "3.3V"


@dataclass
class CaptureResult:
    """Result of a Saleae capture operation."""

    capture_id: str
    duration: float
    sample_rate_digital: int | None
    sample_rate_analog: int | None
    digital_channels: list[int]
    analog_channels: list[int]
    _capture: Any = field(repr=False)  # saleae.automation.Capture
    state: str = "complete"
    capture_mode: str = "timed"
    started_at: str | None = None
    completed_at: str | None = None
    trigger: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "duration": self.duration,
            "sample_rate_digital": self.sample_rate_digital,
            "sample_rate_analog": self.sample_rate_analog,
            "digital_channels": self.digital_channels,
            "analog_channels": self.analog_channels,
            "state": self.state,
            "capture_mode": self.capture_mode,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "trigger": self.trigger,
        }


@dataclass
class AnalyzerResult:
    """Result of adding an analyzer to a capture."""

    analyzer_id: str
    analyzer_type: str
    settings: dict[str, Any]
    _analyzer: Any = field(repr=False)  # Analyzer handle

    def to_dict(self) -> dict[str, Any]:
        return {
            "analyzer_id": self.analyzer_id,
            "analyzer_type": self.analyzer_type,
            "settings": self.settings,
        }


@dataclass
class AnalyzerData:
    """Exported analyzer data."""

    analyzer_type: str
    rows: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "analyzer_type": self.analyzer_type,
            "row_count": len(self.rows),
            "rows": self.rows[:100],  # Limit for JSON
        }


class SaleaeLogicAnalyzer(Instrument):
    """Saleae Logic 2 automation API driver.

    Provides full access to Saleae Logic analyzers including:
    - Digital and analog capture (Pro models)
    - High sample rates (up to 500 MHz digital)
    - Built-in protocol analyzers
    - Hardware triggers
    - Data export

    Requires Logic 2 software (v2.4.0+) to be running.

    Usage:
        la = SaleaeLogicAnalyzer()

        async with la:
            # Simple timed capture
            capture = await la.capture(
                duration=1.0,
                digital_channels=[0, 1, 2, 3],
                sample_rate=24_000_000,
            )

            # Add SPI analyzer
            analyzer = await la.add_analyzer(
                capture,
                "SPI",
                settings={
                    "MISO": 0,
                    "Clock": 1,
                    "Enable": 2,
                    "MOSI": 3,
                },
            )

            # Export data
            await la.export_analyzer_csv(capture, analyzer, Path("spi_data.csv"))
    """

    def __init__(
        self,
        port: int = 10430,
        device_id: str | None = None,
        launch: bool = False,
    ):
        """Initialize the Saleae Logic analyzer driver.

        Args:
            port: Logic 2 automation port (default 10430).
            device_id: Specific device ID to use (None = first available).
            launch: If True, launch Logic 2 if not running.
        """
        if not SALEAE_AVAILABLE:
            raise InstrumentError(
                "Saleae automation not available. Install with: pip install logic2-automation"
            )

        self._lock = TaskRLock()
        self._lease = HostLease(f"saleae:127.0.0.1:{port}")
        self.port = port
        self.device_id = device_id
        self.launch = launch

        self._manager: Manager | None = None
        self._device: Any = None
        self._device_config: Any = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @serialized
    async def connect(self) -> None:
        """Connect to Logic 2 application."""
        if self._connected:
            return

        logger.info(f"Connecting to Saleae Logic 2 on port {self.port}")

        self._lease.acquire()
        try:
            # Run connection in thread pool since it's blocking
            loop = asyncio.get_event_loop()

            if self.launch:
                self._manager = await loop.run_in_executor(None, Manager.launch)
            else:
                self._manager = await loop.run_in_executor(
                    None,
                    lambda: Manager.connect(
                        port=self.port,
                        connect_timeout_seconds=10,
                        grpc_channel_arguments=[
                            (
                                "grpc.service_config",
                                json.dumps(
                                    {
                                        "methodConfig": [
                                            {
                                                "name": [
                                                    {
                                                        "service": "saleae.automation.Manager",
                                                        "method": method.name,
                                                    }
                                                    for method in saleae_pb2.DESCRIPTOR.services_by_name[
                                                        "Manager"
                                                    ].methods
                                                    if method.name != "WaitCapture"
                                                ],
                                                "timeout": "60s",
                                            }
                                        ]
                                    }
                                ),
                            )
                        ],
                    ),
                )

            # Get devices
            devices = await loop.run_in_executor(None, self._manager.get_devices)

            if not devices:
                raise InstrumentError("No Saleae devices found")

            # Select device
            if self.device_id:
                self._device = next(
                    (d for d in devices if d.device_id == self.device_id),
                    None,
                )
                if not self._device:
                    raise InstrumentError(
                        f"Device {self.device_id} not found. "
                        f"Available: {[d.device_id for d in devices]}"
                    )
            else:
                if len(devices) != 1 or devices[0].is_simulation:
                    raise InstrumentError(
                        "Select a stable Saleae device_id; device is ambiguous/simulated"
                    )
                self._device = devices[0]

            self._connected = True
            logger.info(
                f"Connected to Saleae {self._device.device_type} (ID: {self._device.device_id})"
            )

        except BaseException as e:
            try:
                if self._manager:
                    with suppress(Exception):
                        await asyncio.to_thread(self._manager.close)
            finally:
                self._lease.release()
                self._connected = False
                self._manager = None
            if isinstance(e, asyncio.CancelledError):
                raise
            raise InstrumentError(f"Failed to connect to Saleae: {e}") from e

    @serialized
    async def disconnect(self) -> None:
        """Disconnect from Logic 2."""
        if not self._connected:
            return

        logger.info("Disconnecting from Saleae Logic 2")

        if self._manager:
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._manager.close)
            except Exception as e:
                logger.warning(f"Error closing manager: {e}")

        self._manager = None
        self._device = None
        self._connected = False
        self._lease.release()

    @serialized
    async def get_info(self) -> InstrumentInfo:
        """Get device information."""
        if not self._device:
            return InstrumentInfo(
                instrument_type="logic_analyzer",
                model="Saleae Logic",
            )

        return InstrumentInfo(
            instrument_type="logic_analyzer",
            model=str(self._device.device_type),
            serial=self._device.device_id,
            firmware_version=None,  # Device firmware is not exposed by the automation API.
            address=f"localhost:{self.port}",
        )

    @serialized
    async def list_devices(self) -> list[dict[str, Any]]:
        """List connected Saleae devices.

        Returns:
            List of device info dictionaries.
        """
        if not self._manager:
            raise InstrumentError("Not connected")

        loop = asyncio.get_event_loop()
        devices = await loop.run_in_executor(None, self._manager.get_devices)

        return [
            {
                "device_id": d.device_id,
                "device_type": str(d.device_type),
                "is_simulation": d.is_simulation,
            }
            for d in devices
        ]

    @serialized
    async def get_software_info(self) -> dict[str, Any]:
        """Return Logic 2 and automation API identity when exposed by the API."""
        result: dict[str, Any] = {
            "automation_package": "logic2-automation",
            "automation_package_version": None,
            "logic_app_version": None,
            "logic_api_version": None,
            "native_channel_labels_supported": False,
        }
        with suppress(importlib.metadata.PackageNotFoundError):
            result["automation_package_version"] = importlib.metadata.version("logic2-automation")

        if not self._manager:
            return result

        loop = asyncio.get_running_loop()
        try:
            app_info = await loop.run_in_executor(None, self._manager.get_app_info)
        except Exception as exc:
            result["app_info_error"] = str(exc)
            return result

        result["logic_app_version"] = self._format_version(getattr(app_info, "app_version", None))
        result["logic_api_version"] = self._format_version(getattr(app_info, "api_version", None))
        result["logic_app_pid"] = getattr(app_info, "app_pid", None)
        return result

    @staticmethod
    def _format_version(version: Any) -> str | None:
        if version is None:
            return None
        if all(hasattr(version, part) for part in ("major", "minor", "patch")):
            return f"{version.major}.{version.minor}.{version.patch}"
        value = str(version)
        return value or None

    @staticmethod
    def _threshold_volts(voltage_threshold: VoltageThreshold) -> float:
        return {
            VoltageThreshold.V_1_2: 1.2,
            VoltageThreshold.V_1_8: 1.8,
            VoltageThreshold.V_3_3: 3.3,
        }[voltage_threshold]

    @staticmethod
    def _validate_capture_request(
        digital_channels: list[int],
        analog_channels: list[int],
        sample_rate_digital: int | None,
        sample_rate_analog: int | None,
        trigger_channel: int | None = None,
    ) -> None:
        if not digital_channels and not analog_channels:
            raise InstrumentError("At least one digital or analog channel is required")
        for name, channels in (
            ("digital", digital_channels),
            ("analog", analog_channels),
        ):
            if len(channels) != len(set(channels)) or any(ch < 0 for ch in channels):
                raise InstrumentError(f"{name} channels must be unique and non-negative")
        if digital_channels and (sample_rate_digital is None or sample_rate_digital <= 0):
            raise InstrumentError("A positive digital sample rate is required")
        if analog_channels and (sample_rate_analog is None or sample_rate_analog <= 0):
            raise InstrumentError("A positive analog sample rate is required")
        if trigger_channel is not None and trigger_channel not in digital_channels:
            raise InstrumentError("Trigger channel must be enabled as a digital channel")

    @staticmethod
    def _emit_progress(
        callback: ProgressCallback | None,
        state: str,
        **details: Any,
    ) -> None:
        if callback:
            callback(
                {
                    "state": state,
                    "timestamp": datetime.now(UTC).isoformat(),
                    **details,
                }
            )

    async def _wait_for_capture(
        self,
        capture: Any,
        timeout_seconds: float | None,
        progress_callback: ProgressCallback | None,
        waiting_state: str,
    ) -> None:
        """Wait with progress and a real gRPC deadline when the official API allows it."""
        loop = asyncio.get_running_loop()

        def do_wait() -> None:
            if timeout_seconds is None:
                capture.wait()
                return

            manager = getattr(capture, "manager", None)
            capture_id = getattr(capture, "capture_id", None)
            stub = getattr(manager, "stub", None)
            if saleae_pb2 is None or grpc is None or stub is None or capture_id is None:
                raise InstrumentError(
                    "This logic2-automation build cannot provide a bounded capture wait"
                )

            request = saleae_pb2.WaitCaptureRequest(capture_id=capture_id)
            try:
                stub.WaitCapture(request, timeout=timeout_seconds)
            except grpc.RpcError as exc:
                if exc.code() != grpc.StatusCode.DEADLINE_EXCEEDED:
                    raise
                try:
                    capture.stop()
                except Exception as stop_exc:
                    logger.warning("Could not stop timed-out Saleae capture: %s", stop_exc)
                raise CaptureTimeoutError(
                    f"Capture timed out after {timeout_seconds:.3f} seconds"
                ) from exc

        wait_future = loop.run_in_executor(None, do_wait)
        started = loop.time()
        try:
            while not wait_future.done():
                elapsed = loop.time() - started
                self._emit_progress(
                    progress_callback,
                    waiting_state,
                    elapsed_seconds=round(elapsed, 3),
                    timeout_seconds=timeout_seconds,
                )
                await asyncio.wait({wait_future}, timeout=1.0)
            await wait_future
        except asyncio.CancelledError:
            # Retain driver ownership until the worker's RPC has settled, so it
            # cannot later stop a capture belonging to a new client.
            with suppress(Exception):
                await asyncio.shield(asyncio.to_thread(capture.stop))
            with suppress(Exception):
                await asyncio.shield(wait_future)
            raise

    @serialized
    async def capture(
        self,
        duration: float,
        digital_channels: list[int],
        analog_channels: list[int] | None = None,
        sample_rate_digital: int = 10_000_000,
        sample_rate_analog: int | None = None,
        voltage_threshold: VoltageThreshold = VoltageThreshold.V_3_3,
        glitch_filter_ns: int | None = None,
        wait_timeout_seconds: float | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> CaptureResult:
        """Start a timed capture.

        Args:
            duration: Capture duration in seconds.
            digital_channels: List of digital channel indices to capture.
            analog_channels: List of analog channel indices (Pro models only).
            sample_rate_digital: Digital sample rate in Hz.
            sample_rate_analog: Analog sample rate (defaults to digital / 10).
            voltage_threshold: Logic voltage threshold (Pro models).
            glitch_filter_ns: Glitch filter in nanoseconds (optional).

        Returns:
            CaptureResult with capture handle.
        """
        if not self._connected or not self._manager:
            raise InstrumentError("Not connected")

        analog_channels = analog_channels or []
        if analog_channels and sample_rate_analog is None:
            sample_rate_analog = sample_rate_digital // 10
        self._validate_capture_request(
            digital_channels,
            analog_channels,
            sample_rate_digital,
            sample_rate_analog,
        )
        loop = asyncio.get_running_loop()

        # Build device configuration
        glitch_filters = None
        if glitch_filter_ns:
            glitch_filters = [
                GlitchFilterEntry(
                    channel_index=ch,
                    pulse_width_seconds=glitch_filter_ns * 1e-9,
                )
                for ch in digital_channels
            ]

        device_args: dict[str, Any] = {
            "enabled_digital_channels": digital_channels,
            "digital_sample_rate": sample_rate_digital if digital_channels else None,
            "digital_threshold_volts": (
                self._threshold_volts(voltage_threshold) if digital_channels else None
            ),
            "enabled_analog_channels": analog_channels,
            "analog_sample_rate": sample_rate_analog if analog_channels else None,
            "glitch_filters": glitch_filters or [],
        }
        device_config = LogicDeviceConfiguration(
            **device_args,
        )

        capture_config = CaptureConfiguration(
            capture_mode=TimedCaptureMode(duration_seconds=duration)
        )

        logger.info(
            f"Starting capture: {duration}s, channels {digital_channels}, "
            f"sample rate {sample_rate_digital / 1e6:.1f} MHz"
        )

        # Start capture (blocking)
        def do_capture() -> Any:
            return self._manager.start_capture(  # type: ignore
                device_id=self._device.device_id,
                device_configuration=device_config,
                capture_configuration=capture_config,
            )

        started_at = datetime.now(UTC).isoformat()
        self._emit_progress(progress_callback, "starting", capture_mode="timed")
        try:
            capture = await loop.run_in_executor(None, do_capture)
        except Exception as exc:
            raise InstrumentError(
                "Logic 2 rejected the channel/sample-rate configuration: "
                f"digital={digital_channels}@{sample_rate_digital}, "
                f"analog={analog_channels}@{sample_rate_analog}: {exc}"
            ) from exc

        self._emit_progress(progress_callback, "capturing", duration_seconds=duration)
        await self._wait_for_capture(
            capture,
            wait_timeout_seconds or duration + 10.0,
            progress_callback,
            "capturing",
        )
        completed_at = datetime.now(UTC).isoformat()
        self._emit_progress(progress_callback, "complete")

        return CaptureResult(
            capture_id=str(id(capture)),
            duration=duration,
            sample_rate_digital=sample_rate_digital,
            sample_rate_analog=sample_rate_analog,
            digital_channels=digital_channels,
            analog_channels=analog_channels,
            _capture=capture,
            state="complete",
            capture_mode="timed",
            started_at=started_at,
            completed_at=completed_at,
        )

    @serialized
    async def capture_with_trigger(
        self,
        digital_channels: list[int],
        trigger_channel: int,
        trigger_edge: str = "rising",
        sample_rate: int = 10_000_000,
        analog_channels: list[int] | None = None,
        sample_rate_analog: int | None = None,
        voltage_threshold: VoltageThreshold = VoltageThreshold.V_3_3,
        pre_trigger_samples: int | None = None,
        pre_trigger_seconds: float | None = None,
        post_trigger_seconds: float = 1.0,
        trigger_timeout_seconds: float = 30.0,
        progress_callback: ProgressCallback | None = None,
        max_duration: float | None = None,
    ) -> CaptureResult:
        """Start a triggered capture.

        Args:
            digital_channels: Channels to capture.
            trigger_channel: Channel to trigger on.
            trigger_edge: "rising" or "falling".
            sample_rate: Digital sample rate in Hz.
            pre_trigger_samples: Samples to capture before trigger.
            post_trigger_seconds: Data to retain after the trigger.
            trigger_timeout_seconds: Maximum time to wait for a trigger.

        Returns:
            CaptureResult with capture handle.
        """
        if not self._connected or not self._manager:
            raise InstrumentError("Not connected")

        analog_channels = analog_channels or []
        if max_duration is not None:
            post_trigger_seconds = max_duration
        if pre_trigger_seconds is None:
            pre_trigger_seconds = (pre_trigger_samples or 0) / sample_rate
        if pre_trigger_seconds < 0 or post_trigger_seconds <= 0:
            raise InstrumentError("Pre-trigger must be non-negative and post-trigger positive")
        if trigger_timeout_seconds <= 0:
            raise InstrumentError("Trigger timeout must be positive")
        self._validate_capture_request(
            digital_channels,
            analog_channels,
            sample_rate,
            sample_rate_analog,
            trigger_channel,
        )
        if trigger_edge.lower() not in {"rising", "falling"}:
            raise InstrumentError("Trigger edge must be 'rising' or 'falling'")

        loop = asyncio.get_running_loop()

        # Map trigger edge
        trigger_type = (
            DigitalTriggerType.RISING
            if trigger_edge.lower() == "rising"
            else DigitalTriggerType.FALLING
        )

        device_config = LogicDeviceConfiguration(
            enabled_digital_channels=digital_channels,
            digital_sample_rate=sample_rate,
            digital_threshold_volts=self._threshold_volts(voltage_threshold),
            enabled_analog_channels=analog_channels,
            analog_sample_rate=sample_rate_analog if analog_channels else None,
        )

        capture_config = CaptureConfiguration(
            capture_mode=DigitalTriggerCaptureMode(
                trigger_channel_index=trigger_channel,
                trigger_type=trigger_type,
                min_pulse_width_seconds=None,
                max_pulse_width_seconds=None,
                after_trigger_seconds=post_trigger_seconds,
                trim_data_seconds=pre_trigger_seconds + post_trigger_seconds,
            )
        )

        logger.info(
            f"Starting triggered capture on channel {trigger_channel} ({trigger_edge} edge)"
        )

        def do_capture() -> Any:
            return self._manager.start_capture(  # type: ignore
                device_id=self._device.device_id,
                device_configuration=device_config,
                capture_configuration=capture_config,
            )

        started_at = datetime.now(UTC).isoformat()
        try:
            capture = await loop.run_in_executor(None, do_capture)
        except Exception as exc:
            raise InstrumentError(
                "Logic 2 rejected the triggered channel/sample-rate configuration: "
                f"digital={digital_channels}@{sample_rate}, "
                f"analog={analog_channels}@{sample_rate_analog}: {exc}"
            ) from exc

        self._emit_progress(
            progress_callback,
            "armed",
            trigger_channel=trigger_channel,
            trigger_edge=trigger_edge.lower(),
            pre_trigger_seconds=pre_trigger_seconds,
            post_trigger_seconds=post_trigger_seconds,
            timeout_seconds=trigger_timeout_seconds,
        )
        try:
            await self._wait_for_capture(
                capture,
                trigger_timeout_seconds + post_trigger_seconds,
                progress_callback,
                "waiting_for_trigger",
            )
        except CaptureTimeoutError:
            self._emit_progress(progress_callback, "trigger_timeout")
            raise
        completed_at = datetime.now(UTC).isoformat()
        self._emit_progress(progress_callback, "complete")

        return CaptureResult(
            capture_id=str(id(capture)),
            duration=pre_trigger_seconds + post_trigger_seconds,
            sample_rate_digital=sample_rate,
            sample_rate_analog=sample_rate_analog,
            digital_channels=digital_channels,
            analog_channels=analog_channels,
            _capture=capture,
            state="complete",
            capture_mode="digital_trigger",
            started_at=started_at,
            completed_at=completed_at,
            trigger={
                "channel": trigger_channel,
                "edge": trigger_edge.lower(),
                "pre_trigger_seconds": pre_trigger_seconds,
                "post_trigger_seconds": post_trigger_seconds,
                "timeout_seconds": trigger_timeout_seconds,
            },
        )

    @serialized
    async def add_analyzer(
        self,
        capture: CaptureResult,
        analyzer_type: str,
        settings: dict[str, Any],
        label: str | None = None,
    ) -> AnalyzerResult:
        """Add a protocol analyzer to a capture.

        Args:
            capture: Capture to analyze.
            analyzer_type: Analyzer name (e.g., "SPI", "I2C", "Async Serial").
            settings: Analyzer-specific settings.

        Returns:
            AnalyzerResult with analyzer handle.

        Common analyzer types and settings:
            - "SPI": MISO, MOSI, Clock, Enable
            - "I2C": SDA, SCL
            - "Async Serial": Input Channel, Bit Rate
        """
        if not self._connected:
            raise InstrumentError("Not connected")

        loop = asyncio.get_event_loop()

        logger.info(f"Adding {analyzer_type} analyzer")

        def do_add_analyzer() -> Any:
            return capture._capture.add_analyzer(
                analyzer_type,
                label=label or f"{analyzer_type}_analyzer",
                settings=settings,
            )

        analyzer = await loop.run_in_executor(None, do_add_analyzer)

        return AnalyzerResult(
            analyzer_id=str(id(analyzer)),
            analyzer_type=analyzer_type,
            settings=settings,
            _analyzer=analyzer,
        )

    @serialized
    async def export_analyzer_csv(
        self,
        capture: CaptureResult,
        analyzer: AnalyzerResult,
        output_path: Path,
        radix: str = "hexadecimal",
    ) -> None:
        """Export analyzer data to CSV.

        Args:
            capture: Capture containing the analyzer.
            analyzer: Analyzer to export.
            output_path: Path for CSV file.
            radix: Number format ("hexadecimal", "decimal", "binary", "ascii").
        """
        if not self._connected:
            raise InstrumentError("Not connected")

        loop = asyncio.get_event_loop()

        radix_map = {
            "hexadecimal": RadixType.HEXADECIMAL,
            "decimal": RadixType.DECIMAL,
            "binary": RadixType.BINARY,
            "ascii": RadixType.ASCII,
        }
        radix_type = radix_map.get(radix.lower(), RadixType.HEXADECIMAL)

        def do_export() -> None:
            capture._capture.export_data_table(
                filepath=str(output_path),
                analyzers=[
                    DataTableExportConfiguration(
                        analyzer=analyzer._analyzer,
                        radix=radix_type,
                    )
                ],
            )

        await loop.run_in_executor(None, do_export)
        logger.info(f"Exported analyzer data to {output_path}")

    @serialized
    async def export_raw_csv(
        self,
        capture: CaptureResult,
        output_dir: Path,
        digital_channels: list[int] | None = None,
        analog_channels: list[int] | None = None,
    ) -> list[Path]:
        """Export raw capture data to CSV.

        Args:
            capture: Capture to export.
            output_dir: Existing or new directory for Logic's analog.csv/digital.csv.
            digital_channels: Channels to export (None = all).
            analog_channels: Analog channels to export (None = all).
        """
        if not self._connected:
            raise InstrumentError("Not connected")

        output_dir.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_running_loop()

        def do_export() -> None:
            capture._capture.export_raw_data_csv(
                directory=str(output_dir.resolve()),
                digital_channels=(
                    digital_channels if digital_channels is not None else capture.digital_channels
                ),
                analog_channels=(
                    analog_channels if analog_channels is not None else capture.analog_channels
                ),
            )

        await loop.run_in_executor(None, do_export)
        paths = [
            path
            for path in (output_dir / "digital.csv", output_dir / "analog.csv")
            if path.exists()
        ]
        logger.info("Exported raw data to %s", output_dir)
        return paths

    @serialized
    async def save_capture(self, capture: CaptureResult, output_path: Path) -> None:
        """Save capture to a .sal file.

        Args:
            capture: Capture to save.
            output_path: Path for .sal file.
        """
        if not self._connected:
            raise InstrumentError("Not connected")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_running_loop()

        def do_save() -> None:
            capture._capture.save_capture(filepath=str(output_path))

        await loop.run_in_executor(None, do_save)
        logger.info(f"Saved capture to {output_path}")

    @serialized
    async def close_capture(self, capture: CaptureResult) -> None:
        """Close a Logic capture tab and release its memory."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, capture._capture.close)

    @serialized
    async def load_capture(self, capture_path: Path) -> CaptureResult:
        """Load a previously saved capture.

        Args:
            capture_path: Path to .sal file.

        Returns:
            CaptureResult with loaded capture.
        """
        if not self._connected or not self._manager:
            raise InstrumentError("Not connected")

        loop = asyncio.get_event_loop()

        def do_load() -> Any:
            return self._manager.load_capture(str(capture_path))  # type: ignore

        capture = await loop.run_in_executor(None, do_load)

        return CaptureResult(
            capture_id=str(id(capture)),
            duration=0.0,  # Unknown for loaded captures
            sample_rate_digital=None,
            sample_rate_analog=None,
            digital_channels=[],  # Unknown for loaded captures
            analog_channels=[],
            _capture=capture,
        )

    @serialized
    async def get_analyzer_data(
        self,
        capture: CaptureResult,
        analyzer: AnalyzerResult,
    ) -> AnalyzerData:
        """Get analyzed data as Python objects.

        Exports to temp CSV and parses it.

        Args:
            capture: Capture containing the analyzer.
            analyzer: Analyzer to get data from.

        Returns:
            AnalyzerData with parsed rows.
        """
        import csv
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as f:
            temp_path = Path(f.name)

        try:
            await self.export_analyzer_csv(capture, analyzer, temp_path)

            rows = []
            with open(temp_path, newline="") as csvfile:
                reader = csv.DictReader(csvfile)
                for row in reader:
                    rows.append(dict(row))

            return AnalyzerData(
                analyzer_type=analyzer.analyzer_type,
                rows=rows,
            )

        finally:
            temp_path.unlink(missing_ok=True)


# Convenience functions for common analyzer configurations


def spi_analyzer_settings(
    clk: int,
    mosi: int | None = None,
    miso: int | None = None,
    cs: int | None = None,
    bits_per_transfer: int = 8,
    clock_polarity: int = 0,
    clock_phase: int = 0,
    msb_first: bool = True,
) -> dict[str, Any]:
    """Generate SPI analyzer settings.

    Args:
        clk: Clock channel index.
        mosi: MOSI channel index (optional).
        miso: MISO channel index (optional).
        cs: Chip select channel index (optional).
        bits_per_transfer: Bits per transfer (typically 8).
        clock_polarity: CPOL (0 or 1).
        clock_phase: CPHA (0 or 1).
        msb_first: True for MSB first.

    Returns:
        Settings dict for add_analyzer().
    """
    settings: dict[str, Any] = {
        "Clock": clk,
        "Bits per Transfer": str(bits_per_transfer),
        "Significant Bit": (
            "Most Significant Bit First" if msb_first else "Least Significant Bit First"
        ),
        "Clock State": (
            "Clock is Low when inactive (CPOL = 0)"
            if clock_polarity == 0
            else "Clock is High when inactive (CPOL = 1)"
        ),
        "Clock Phase": (
            "Data is Valid on Clock Leading Edge (CPHA = 0)"
            if clock_phase == 0
            else "Data is Valid on Clock Trailing Edge (CPHA = 1)"
        ),
    }

    if mosi is not None:
        settings["MOSI"] = mosi
    if miso is not None:
        settings["MISO"] = miso
    if cs is not None:
        settings["Enable"] = cs

    return settings


def i2c_analyzer_settings(sda: int, scl: int) -> dict[str, Any]:
    """Generate I2C analyzer settings.

    Args:
        sda: SDA channel index.
        scl: SCL channel index.

    Returns:
        Settings dict for add_analyzer().
    """
    return {
        "SDA": sda,
        "SCL": scl,
    }


def uart_analyzer_settings(
    rx: int,
    baud_rate: int = 115200,
    bits_per_frame: int = 8,
    stop_bits: float = 1.0,
    parity: str = "None",
    msb_first: bool = False,
    inverted: bool = False,
) -> dict[str, Any]:
    """Generate UART/Async Serial analyzer settings.

    Args:
        rx: RX channel index.
        baud_rate: Baud rate.
        bits_per_frame: Data bits (5-9).
        stop_bits: Stop bits (1, 1.5, or 2).
        parity: "None", "Even", or "Odd".
        msb_first: True when the most significant bit is sent first.
        inverted: True for inverted signal.

    Returns:
        Settings dict for add_analyzer().
    """
    bits_value = (
        "8 Bits per Transfer (Standard)"
        if bits_per_frame == 8
        else f"{bits_per_frame} Bits per Transfer"
    )
    stop_value = {
        1.0: "1 Stop Bit (Standard)",
        1.5: "1.5 Stop Bits",
        2.0: "2 Stop Bits",
    }[stop_bits]
    parity_value = {
        "None": "No Parity Bit (Standard)",
        "Even": "Even Parity Bit",
        "Odd": "Odd Parity Bit",
    }[parity]
    return {
        "Input Channel": rx,
        "Bit Rate (Bits/s)": baud_rate,
        "Bits per Frame": bits_value,
        "Stop Bits": stop_value,
        "Parity Bit": parity_value,
        "Significant Bit": (
            "Most Significant Bit Sent First"
            if msb_first
            else "Least Significant Bit Sent First (Standard)"
        ),
        "Signal inversion": "Inverted" if inverted else "Non Inverted (Standard)",
        "Mode": "Normal",
    }
