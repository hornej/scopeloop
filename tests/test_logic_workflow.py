"""Tests for recipe-driven Saleae evidence workflows."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scopeloop.config import Config
from scopeloop.instruments.base import InstrumentError, InstrumentInfo
from scopeloop.instruments.saleae import AnalyzerResult, CaptureResult
from scopeloop.logic import LogicCaptureService, parse_metadata


def logic_config() -> Config:
    return Config.model_validate(
        {
            "project": {"name": "logic-test"},
            "hardware": {"mcu": "esp32"},
            "build": {"system": "esp-idf"},
            "instruments": {
                "logic_analyzer": {
                    "type": "saleae",
                    "channels": {
                        2: {
                            "signal": "RESETn",
                            "label": "reset",
                            "signal_type": "reset",
                            "analog": True,
                            "electrical": {
                                "input_low_max_v": 0.825,
                                "input_high_min_v": 2.475,
                            },
                        },
                        3: {"signal": "TX", "label": "uart_tx", "signal_type": "uart"},
                    },
                    "recipes": {
                        "boot": {
                            "digital_channels": [2, 3],
                            "analog_channels": [2],
                            "digital_sample_rate": 10_000_000,
                            "analog_sample_rate": 1_000_000,
                            "logic_family_volts": 3.3,
                            "trigger": {
                                "channel": 2,
                                "edge": "rising",
                                "pre_trigger_seconds": 0.25,
                                "post_trigger_seconds": 0.75,
                                "timeout_seconds": 5,
                            },
                            "uart": [{"name": "boot_uart", "channel": 3}],
                            "required_metadata": ["serial", "board_revision"],
                        }
                    },
                }
            },
        }
    )


class FakeDriver:
    def __init__(self) -> None:
        self.is_connected = False
        self.closed = False
        self.analyzer_call: dict[str, Any] | None = None

    async def connect(self) -> None:
        self.is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False

    async def list_devices(self) -> list[dict[str, Any]]:
        return [{"device_id": "FAKE", "device_type": "LOGIC_PRO_16"}]

    async def get_info(self) -> InstrumentInfo:
        return InstrumentInfo(
            instrument_type="logic_analyzer",
            model="LOGIC_PRO_16",
            serial="FAKE",
            firmware_version="2.4.46",
            address="localhost:10430",
        )

    async def get_software_info(self) -> dict[str, Any]:
        return {
            "automation_package_version": "1.0.11",
            "logic_app_version": "2.4.46",
            "logic_api_version": "1.0.0",
            "native_channel_labels_supported": False,
        }

    async def capture_with_trigger(self, **kwargs: Any) -> CaptureResult:
        callback = kwargs["progress_callback"]
        callback({"state": "armed", "timestamp": "2026-08-14T00:00:00Z"})
        callback({"state": "complete", "timestamp": "2026-08-14T00:00:01Z"})
        return CaptureResult(
            capture_id="capture-1",
            duration=1.0,
            sample_rate_digital=kwargs["sample_rate"],
            sample_rate_analog=kwargs["sample_rate_analog"],
            digital_channels=kwargs["digital_channels"],
            analog_channels=kwargs["analog_channels"],
            _capture=SimpleNamespace(),
            capture_mode="digital_trigger",
            trigger={"channel": kwargs["trigger_channel"]},
        )

    async def capture(self, **kwargs: Any) -> CaptureResult:
        raise AssertionError("triggered recipe should use capture_with_trigger")

    async def add_analyzer(
        self,
        capture: CaptureResult,
        analyzer_type: str,
        settings: dict[str, Any],
        label: str | None = None,
    ) -> AnalyzerResult:
        self.analyzer_call = {
            "capture": capture.capture_id,
            "type": analyzer_type,
            "settings": settings,
            "label": label,
        }
        return AnalyzerResult("analyzer-1", analyzer_type, settings, SimpleNamespace())

    async def export_analyzer_csv(
        self,
        capture: CaptureResult,
        analyzer: AnalyzerResult,
        output_path: Path,
        radix: str = "hexadecimal",
    ) -> None:
        output_path.write_text("Time [s],Data\n0.0,boot\n", encoding="utf-8")

    async def export_raw_csv(
        self,
        capture: CaptureResult,
        output_dir: Path,
        digital_channels: list[int] | None = None,
        analog_channels: list[int] | None = None,
    ) -> list[Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        digital = output_dir / "digital.csv"
        analog = output_dir / "analog.csv"
        digital.write_text("Time [s],Channel 2,Channel 3\n0,0,1\n1,1,0\n", encoding="utf-8")
        analog.write_text("Time [s],Channel 2\n0,0.0\n1,3.3\n", encoding="utf-8")
        return [digital, analog]

    async def save_capture(self, capture: CaptureResult, output_path: Path) -> None:
        output_path.write_bytes(b"untouched-saleae-capture")

    async def load_capture(self, capture_path: Path) -> CaptureResult:
        raise NotImplementedError

    async def close_capture(self, capture: CaptureResult) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_capture_evidence_bundle_is_complete_and_hashed(tmp_path: Path):
    config = logic_config().instruments.logic_analyzer
    assert config is not None
    driver = FakeDriver()
    service = LogicCaptureService(config, driver=driver)

    bundle = await service.capture_evidence(
        "boot",
        {"serial": "unit-1", "board_revision": "A", "fixture.note": "known-good"},
        tmp_path,
    )

    manifest = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
    channel_map = json.loads((bundle.bundle_dir / "channel-map.json").read_text())
    assert manifest["status"] == "complete"
    assert manifest["recipe"]["trigger"]["pre_trigger_seconds"] == 0.25
    assert manifest["run_metadata"]["fixture.note"] == "known-good"
    assert manifest["channel_labels"]["native_labels_applied"] is False
    assert [item["state"] for item in manifest["capture_progress"]] == ["armed", "complete"]
    assert channel_map["digital"]["2"]["name"] == "reset"
    assert channel_map["analog"]["2"]["electrical"]["input_high_min_v"] == 2.475
    assert (bundle.bundle_dir / "capture.sal").read_bytes() == b"untouched-saleae-capture"
    assert (bundle.bundle_dir / "decoded" / "boot_uart.csv").exists()
    assert (bundle.bundle_dir / "SHA256SUMS").exists()
    assert driver.analyzer_call is not None
    assert driver.analyzer_call["label"] == "boot_uart"
    assert driver.analyzer_call["settings"]["Bits per Frame"] == ("8 Bits per Transfer (Standard)")
    assert driver.analyzer_call["settings"]["Stop Bits"] == "1 Stop Bit (Standard)"
    assert driver.analyzer_call["settings"]["Parity Bit"] == "No Parity Bit (Standard)"

    for relative, info in manifest["files"].items():
        digest = hashlib.sha256((bundle.bundle_dir / relative).read_bytes()).hexdigest()
        assert info["sha256"] == digest


@pytest.mark.asyncio
async def test_required_metadata_is_enforced_before_capture(tmp_path: Path):
    config = logic_config().instruments.logic_analyzer
    assert config is not None
    service = LogicCaptureService(config, driver=FakeDriver())
    with pytest.raises(InstrumentError, match="board_revision"):
        await service.capture_evidence("boot", {"serial": "unit-1"}, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_parse_metadata_supports_arbitrary_json_values():
    assert parse_metadata(["serial=unit-1", "dongle_power=false", "attempt=3"]) == {
        "serial": "unit-1",
        "dongle_power": False,
        "attempt": 3,
    }
