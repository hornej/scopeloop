"""Tests for opt-in, signal-aware waveform frequency caching."""

from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from scopeloop.waveforms import WaveformMetadata, WaveformStore


def metadata(**overrides):
    values = {
        "capture_id": "capture",
        "channel": "CH0",
        "sample_rate": 100_000,
        "record_length": 0,
        "voltage_scale": 1.0,
        "voltage_offset": 0.0,
        "time_offset": 0.0,
        "captured_at": datetime.now(UTC).isoformat(),
    }
    values.update(overrides)
    return WaveformMetadata(**values)


def test_unknown_waveform_preserves_raw_measurements_without_frequency(tmp_path: Path):
    store = WaveformStore(tmp_path)
    samples = 1.65 + 0.1 * np.sin(2 * np.pi * 2500 * np.arange(2000) / 100_000)
    item = metadata()

    store.store_capture(samples, item)

    stored = store.get_metadata("capture")
    assert stored.vpp is not None
    assert stored.frequency is None
    assert stored.frequency_status == "not_requested"


def test_explicit_valid_clock_frequency_is_cached(tmp_path: Path):
    store = WaveformStore(tmp_path)
    time = np.arange(2000) / 100_000
    samples = np.where(np.sin(2 * np.pi * 1000 * time) >= 0, 3.3, 0.0)
    item = metadata(
        signal_type="clock",
        requested_measurements=["frequency"],
        electrical_limits={"input_low_max_v": 0.825, "input_high_min_v": 2.475},
    )

    store.store_capture(samples, item)

    stored = store.get_metadata("capture")
    assert stored.frequency_status == "valid"
    assert 990 < stored.frequency < 1010
