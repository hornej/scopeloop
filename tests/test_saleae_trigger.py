"""Tests for trigger-window translation without Saleae hardware."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

import scopeloop.instruments.saleae as saleae_module


@dataclass
class FakeDeviceConfiguration:
    enabled_digital_channels: list[int]
    digital_sample_rate: int
    digital_threshold_volts: float
    enabled_analog_channels: list[int]
    analog_sample_rate: int | None


@dataclass
class FakeTriggerMode:
    trigger_channel_index: int
    trigger_type: str
    min_pulse_width_seconds: float | None
    max_pulse_width_seconds: float | None
    after_trigger_seconds: float
    trim_data_seconds: float


@dataclass
class FakeCaptureConfiguration:
    capture_mode: FakeTriggerMode


class FakeManager:
    def __init__(self) -> None:
        self.kwargs = None

    def start_capture(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace()


@pytest.mark.asyncio
async def test_trigger_uses_trim_for_real_pretrigger_and_keeps_analog(monkeypatch):
    monkeypatch.setattr(saleae_module, "SALEAE_AVAILABLE", True)
    monkeypatch.setattr(
        saleae_module, "LogicDeviceConfiguration", FakeDeviceConfiguration, raising=False
    )
    monkeypatch.setattr(saleae_module, "DigitalTriggerCaptureMode", FakeTriggerMode, raising=False)
    monkeypatch.setattr(
        saleae_module, "CaptureConfiguration", FakeCaptureConfiguration, raising=False
    )
    monkeypatch.setattr(
        saleae_module,
        "DigitalTriggerType",
        SimpleNamespace(RISING="rising", FALLING="falling"),
        raising=False,
    )
    analyzer = saleae_module.SaleaeLogicAnalyzer()
    manager = FakeManager()
    analyzer._manager = manager
    analyzer._device = SimpleNamespace(device_id="FAKE")
    analyzer._connected = True

    async def no_wait(*args, **kwargs):
        return None

    monkeypatch.setattr(analyzer, "_wait_for_capture", no_wait)
    result = await analyzer.capture_with_trigger(
        digital_channels=[0, 1],
        analog_channels=[1],
        trigger_channel=0,
        sample_rate=10_000_000,
        sample_rate_analog=1_000_000,
        pre_trigger_seconds=0.25,
        post_trigger_seconds=0.75,
        trigger_timeout_seconds=5,
    )

    mode = manager.kwargs["capture_configuration"].capture_mode
    device = manager.kwargs["device_configuration"]
    assert mode.trim_data_seconds == 1.0
    assert mode.after_trigger_seconds == 0.75
    assert device.enabled_analog_channels == [1]
    assert device.digital_threshold_volts == 3.3
    assert result.duration == 1.0
    assert result.capture_mode == "digital_trigger"


@pytest.mark.asyncio
async def test_bounded_wait_stops_capture_on_grpc_deadline(monkeypatch):
    deadline = object()

    class FakeRpcError(Exception):
        def code(self):
            return deadline

    class FakeStub:
        def WaitCapture(self, request, timeout):  # noqa: N802 - mirrors gRPC stub
            assert request.capture_id == 17
            assert timeout == 0.01
            raise FakeRpcError("deadline")

    class FakeCapture:
        capture_id = 17
        manager = SimpleNamespace(stub=FakeStub())

        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(
        saleae_module,
        "saleae_pb2",
        SimpleNamespace(
            WaitCaptureRequest=lambda capture_id: SimpleNamespace(capture_id=capture_id)
        ),
    )
    monkeypatch.setattr(
        saleae_module,
        "grpc",
        SimpleNamespace(
            RpcError=FakeRpcError, StatusCode=SimpleNamespace(DEADLINE_EXCEEDED=deadline)
        ),
    )
    analyzer = object.__new__(saleae_module.SaleaeLogicAnalyzer)
    capture = FakeCapture()

    with pytest.raises(saleae_module.CaptureTimeoutError, match="0.010"):
        await analyzer._wait_for_capture(capture, 0.01, None, "waiting_for_trigger")
    assert capture.stopped is True
