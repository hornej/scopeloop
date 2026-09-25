"""Recipe-driven logic analyzer workflows and reproducible evidence bundles."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from scopeloop.config import (
    LogicAnalyzerConfig,
    LogicCaptureRecipeConfig,
    LogicUartAnalyzerConfig,
)
from scopeloop.instruments.base import InstrumentError
from scopeloop.instruments.saleae import (
    AnalyzerResult,
    CaptureResult,
    SaleaeLogicAnalyzer,
    VoltageThreshold,
    uart_analyzer_settings,
)
from scopeloop.resources import TaskRLock, serialized
from scopeloop.saleae_labels import label_and_verify


class LogicDriver(Protocol):
    """Driver surface used by the workflow, allowing hardware-free tests."""

    is_connected: bool

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def get_info(self) -> Any: ...

    async def get_software_info(self) -> dict[str, Any]: ...

    async def capture(self, **kwargs: Any) -> CaptureResult: ...

    async def capture_with_trigger(self, **kwargs: Any) -> CaptureResult: ...

    async def add_analyzer(
        self,
        capture: CaptureResult,
        analyzer_type: str,
        settings: dict[str, Any],
        label: str | None = None,
    ) -> AnalyzerResult: ...

    async def export_analyzer_csv(
        self,
        capture: CaptureResult,
        analyzer: AnalyzerResult,
        output_path: Path,
        radix: str = "hexadecimal",
    ) -> None: ...

    async def export_raw_csv(
        self,
        capture: CaptureResult,
        output_dir: Path,
        digital_channels: list[int] | None = None,
        analog_channels: list[int] | None = None,
    ) -> list[Path]: ...

    async def save_capture(self, capture: CaptureResult, output_path: Path) -> None: ...

    async def load_capture(self, capture_path: Path) -> CaptureResult: ...

    async def close_capture(self, capture: CaptureResult) -> None: ...


@dataclass
class EvidenceBundle:
    """Completed capture evidence directory and its authoritative manifest."""

    bundle_dir: Path
    manifest_path: Path
    capture_id: str
    files: dict[str, dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_dir": str(self.bundle_dir),
            "manifest_path": str(self.manifest_path),
            "capture_id": self.capture_id,
            "files": self.files,
        }


class LogicCaptureService:
    """Shared Saleae workflow used by the CLI and MCP handlers."""

    def __init__(
        self,
        config: LogicAnalyzerConfig,
        driver: LogicDriver | None = None,
    ) -> None:
        if config.type.lower() != "saleae":
            raise InstrumentError(
                "Recipe evidence bundles currently require logic_analyzer.type: saleae"
            )
        self._lock = TaskRLock()
        self.config = config
        self.driver = cast(
            LogicDriver,
            driver
            or SaleaeLogicAnalyzer(
                port=config.port,
                device_id=config.device_id,
            ),
        )
        self.captures: dict[str, CaptureResult] = {}
        self.analyzers: dict[str, dict[str, AnalyzerResult]] = {}
        self.progress: dict[str, list[dict[str, Any]]] = {}

    @serialized
    async def connect(self) -> dict[str, Any]:
        if not self.driver.is_connected:
            await self.driver.connect()
        info = await self.driver.get_info()
        software = await self.driver.get_software_info()
        return {
            "connected": True,
            "instrument": asdict(info),
            "software": software,
            "devices": await self._list_devices_if_available(),
        }

    @serialized
    async def disconnect(self, close_captures: bool = False) -> None:
        if close_captures:
            for capture in list(self.captures.values()):
                with suppress(Exception):
                    await self.driver.close_capture(capture)
            self.captures.clear()
            self.analyzers.clear()
        if self.driver.is_connected:
            await self.driver.disconnect()

    @serialized
    async def _list_devices_if_available(self) -> list[dict[str, Any]]:
        list_devices = getattr(self.driver, "list_devices", None)
        if not list_devices:
            return []
        return cast(list[dict[str, Any]], await list_devices())

    def get_recipe(self, name: str) -> LogicCaptureRecipeConfig:
        try:
            return self.config.recipes[name]
        except KeyError as exc:
            choices = ", ".join(sorted(self.config.recipes)) or "none configured"
            raise InstrumentError(f"Unknown capture recipe '{name}' ({choices})") from exc

    @serialized
    async def capture_evidence(
        self,
        recipe_name: str,
        run_metadata: dict[str, Any],
        output_root: Path,
        *,
        native_labels: bool = False,
    ) -> EvidenceBundle:
        """Run one recipe and automatically persist its complete evidence bundle."""
        recipe = self.get_recipe(recipe_name)
        self._validate_required_metadata(recipe, run_metadata)
        await self.connect()

        bundle_dir = self._new_bundle_dir(output_root, recipe_name)
        raw_dir = bundle_dir / "raw"
        decoded_dir = bundle_dir / "decoded"
        raw_dir.mkdir(parents=True, exist_ok=False)
        decoded_dir.mkdir(parents=True, exist_ok=False)

        progress: list[dict[str, Any]] = []

        def on_progress(update: dict[str, Any]) -> None:
            progress.append(update)

        started_at = datetime.now(UTC).isoformat()
        try:
            capture = await self._capture_recipe(recipe, on_progress)
            self.captures[capture.capture_id] = capture
            self.progress[capture.capture_id] = progress
            analyzers = await self._add_uart_analyzers(capture, recipe.uart)
            self.analyzers[capture.capture_id] = analyzers

            sal_path = bundle_dir / "capture.sal"
            await self.driver.save_capture(capture, sal_path)
            await self.driver.export_raw_csv(
                capture,
                raw_dir,
                recipe.digital_channels,
                recipe.analog_channels,
            )
            for uart in recipe.uart:
                await self.driver.export_analyzer_csv(
                    capture,
                    analyzers[uart.name],
                    decoded_dir / f"{self._safe_name(uart.name)}.csv",
                    uart.radix,
                )

            channel_map = self._channel_map(recipe)
            self._write_json(bundle_dir / "channel-map.json", channel_map)
            labels = {"native_labels_applied": False, "native_reopen_verified": False}
            if native_labels:
                labels = await label_and_verify(self.driver, sal_path, channel_map)
            instrument = await self.driver.get_info()
            software = await self.driver.get_software_info()
            completed_at = datetime.now(UTC).isoformat()

            files = self._hash_bundle_files(bundle_dir)
            manifest = {
                "schema_version": 1,
                "status": "complete",
                "recipe_name": recipe_name,
                "recipe": recipe.model_dump(mode="json"),
                "run_metadata": run_metadata,
                "required_metadata": recipe.required_metadata,
                "started_at": started_at,
                "completed_at": completed_at,
                "capture": capture.to_dict(),
                "capture_progress": progress,
                "instrument": asdict(instrument),
                "software": software,
                "channel_labels": labels,
                "files": files,
            }
            manifest_path = bundle_dir / "manifest.json"
            self._write_json(manifest_path, manifest)
            self._write_checksums(bundle_dir)
            return EvidenceBundle(bundle_dir, manifest_path, capture.capture_id, files)
        except Exception as exc:
            failure = {
                "schema_version": 1,
                "status": "failed",
                "recipe_name": recipe_name,
                "recipe": recipe.model_dump(mode="json"),
                "run_metadata": run_metadata,
                "started_at": started_at,
                "failed_at": datetime.now(UTC).isoformat(),
                "capture_progress": progress,
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
            self._write_json(bundle_dir / "manifest.json", failure)
            raise

    @serialized
    async def _capture_recipe(
        self,
        recipe: LogicCaptureRecipeConfig,
        progress_callback: Any,
    ) -> CaptureResult:
        threshold = self._voltage_threshold(recipe.logic_family_volts)
        if recipe.trigger:
            return await self.driver.capture_with_trigger(
                digital_channels=recipe.digital_channels,
                analog_channels=recipe.analog_channels,
                trigger_channel=recipe.trigger.channel,
                trigger_edge=recipe.trigger.edge,
                sample_rate=recipe.digital_sample_rate,
                sample_rate_analog=recipe.analog_sample_rate,
                voltage_threshold=threshold,
                pre_trigger_seconds=recipe.trigger.pre_trigger_seconds,
                post_trigger_seconds=recipe.trigger.post_trigger_seconds,
                trigger_timeout_seconds=recipe.trigger.timeout_seconds,
                progress_callback=progress_callback,
            )
        return await self.driver.capture(
            duration=recipe.duration_seconds,
            digital_channels=recipe.digital_channels,
            analog_channels=recipe.analog_channels,
            sample_rate_digital=recipe.digital_sample_rate,
            sample_rate_analog=recipe.analog_sample_rate,
            voltage_threshold=threshold,
            progress_callback=progress_callback,
        )

    @serialized
    async def _add_uart_analyzers(
        self,
        capture: CaptureResult,
        configs: list[LogicUartAnalyzerConfig],
    ) -> dict[str, AnalyzerResult]:
        results: dict[str, AnalyzerResult] = {}
        for uart in configs:
            settings = uart_analyzer_settings(
                rx=uart.channel,
                baud_rate=uart.baud_rate,
                bits_per_frame=uart.bits_per_frame,
                stop_bits=uart.stop_bits,
                parity=uart.parity,
                msb_first=uart.bit_order == "most_significant_first",
                inverted=uart.inverted,
            )
            results[uart.name] = await self.driver.add_analyzer(
                capture,
                "Async Serial",
                settings,
                label=uart.name,
            )
        return results

    @serialized
    async def load_capture(self, capture_path: Path) -> CaptureResult:
        await self.connect()
        capture = await self.driver.load_capture(capture_path.resolve())
        self.captures[capture.capture_id] = capture
        return capture

    @serialized
    async def save_capture(self, capture_id: str, output_path: Path) -> Path:
        capture = self._get_capture(capture_id)
        await self.driver.save_capture(capture, output_path.resolve())
        return output_path.resolve()

    @serialized
    async def export_capture(
        self,
        capture_id: str,
        output_dir: Path,
        digital_channels: list[int] | None = None,
        analog_channels: list[int] | None = None,
    ) -> list[Path]:
        capture = self._get_capture(capture_id)
        return await self.driver.export_raw_csv(
            capture,
            output_dir.resolve(),
            digital_channels,
            analog_channels,
        )

    @serialized
    async def decode_uart(
        self,
        capture_id: str,
        uart: LogicUartAnalyzerConfig,
        output_path: Path,
    ) -> Path:
        capture = self._get_capture(capture_id)
        output_path = output_path.resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        analyzer = (await self._add_uart_analyzers(capture, [uart]))[uart.name]
        self.analyzers.setdefault(capture_id, {})[uart.name] = analyzer
        await self.driver.export_analyzer_csv(
            capture,
            analyzer,
            output_path,
            uart.radix,
        )
        return output_path

    def _get_capture(self, capture_id: str) -> CaptureResult:
        try:
            return self.captures[capture_id]
        except KeyError as exc:
            raise InstrumentError(f"Unknown capture id '{capture_id}'") from exc

    def _channel_map(self, recipe: LogicCaptureRecipeConfig) -> dict[str, Any]:
        def entry(channel: int) -> dict[str, Any]:
            configured = self.config.channels.get(channel)
            if configured is None:
                return {
                    "name": f"Channel {channel}",
                    "signal": None,
                    "signal_type": "unknown",
                    "requested_measurements": [],
                    "electrical": None,
                }
            return {
                "name": configured.label or configured.signal,
                "signal": configured.signal,
                "signal_type": configured.signal_type,
                "requested_measurements": configured.requested_measurements,
                "electrical": (
                    configured.electrical.model_dump(mode="json") if configured.electrical else None
                ),
            }

        return {
            "schema_version": 1,
            "authoritative": True,
            "native_labels_applied": False,
            "digital": {str(channel): entry(channel) for channel in recipe.digital_channels},
            "analog": {str(channel): entry(channel) for channel in recipe.analog_channels},
        }

    @staticmethod
    def _validate_required_metadata(
        recipe: LogicCaptureRecipeConfig,
        run_metadata: dict[str, Any],
    ) -> None:
        missing = [
            key
            for key in recipe.required_metadata
            if key not in run_metadata or run_metadata[key] in (None, "")
        ]
        if missing:
            raise InstrumentError("Missing required run metadata: " + ", ".join(sorted(missing)))

    @staticmethod
    def _voltage_threshold(volts: float | None) -> VoltageThreshold:
        mapping = {
            1.2: VoltageThreshold.V_1_2,
            1.8: VoltageThreshold.V_1_8,
            3.3: VoltageThreshold.V_3_3,
        }
        if volts not in mapping:
            raise InstrumentError(f"Unsupported Saleae logic-family setting: {volts}")
        return mapping[volts]

    @staticmethod
    def _new_bundle_dir(output_root: Path, recipe_name: str) -> Path:
        output_root = output_root.expanduser().resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        path = output_root / f"{stamp}_{LogicCaptureService._safe_name(recipe_name)}"
        path.mkdir(parents=False, exist_ok=False)
        return path

    @staticmethod
    def _safe_name(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-.")
        return cleaned or "capture"

    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @classmethod
    def _hash_bundle_files(cls, bundle_dir: Path) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for path in sorted(candidate for candidate in bundle_dir.rglob("*") if candidate.is_file()):
            relative = path.relative_to(bundle_dir).as_posix()
            if relative in {"manifest.json", "SHA256SUMS"}:
                continue
            result[relative] = {"sha256": cls._sha256(path), "bytes": path.stat().st_size}
        return result

    @classmethod
    def _write_checksums(cls, bundle_dir: Path) -> None:
        paths = sorted(
            path for path in bundle_dir.rglob("*") if path.is_file() and path.name != "SHA256SUMS"
        )
        content = "".join(
            f"{cls._sha256(path)}  {path.relative_to(bundle_dir).as_posix()}\n" for path in paths
        )
        (bundle_dir / "SHA256SUMS").write_text(content, encoding="ascii")


def parse_metadata(items: list[str]) -> dict[str, Any]:
    """Parse repeatable key=value CLI metadata, accepting JSON values when possible."""
    result: dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"Metadata must use key=value syntax: {item}")
        key, raw_value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError("Metadata keys cannot be empty")
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            value = raw_value
        result[key] = value
    return result
