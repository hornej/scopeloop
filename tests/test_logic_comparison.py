"""Tests for generic known-good versus DUT capture comparison."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from scopeloop.comparison import SignalTrace, _trace_metrics, compare_bundles
from scopeloop.saleae_labels import sha256


def write_bundle(path: Path, edge_time: float, reset_time: float, sample_rate: int = 1000):
    (path / "raw").mkdir(parents=True)
    recipe = {
        "digital_channels": [2, 6],
        "analog_channels": [6],
        "digital_sample_rate": sample_rate,
        "analog_sample_rate": sample_rate,
        "logic_family_volts": 3.3,
        "duration_seconds": 1.0,
        "trigger": None,
    }
    channel_map = {
        "digital": {
            "2": {"name": "reset", "signal_type": "reset"},
            "6": {"name": "rail", "signal_type": "rail"},
        },
        "analog": {"6": {"name": "rail", "signal_type": "rail"}},
    }
    (path / "channel-map.json").write_text(json.dumps(channel_map), encoding="utf-8")
    times = [index / 100 for index in range(101)]
    digital = ["Time [s],Channel 2,Channel 6"]
    analog = ["Time [s],Channel 6"]
    for timestamp in times:
        digital.append(f"{timestamp},{int(timestamp >= reset_time)},{int(timestamp >= edge_time)}")
        rail = 3.3 if timestamp >= edge_time else 0.0
        analog.append(f"{timestamp},{rail}")
    (path / "raw" / "digital.csv").write_text("\n".join(digital), encoding="utf-8")
    (path / "raw" / "analog.csv").write_text("\n".join(analog), encoding="utf-8")
    files = {
        name: {"sha256": sha256(path / name)}
        for name in ("channel-map.json", "raw/digital.csv", "raw/analog.csv")
    }
    (path / "manifest.json").write_text(
        json.dumps({"recipe": recipe, "status": "complete", "files": files}), encoding="utf-8"
    )


def test_comparison_aligns_selected_edge_and_compares_timing(tmp_path: Path):
    reference = tmp_path / "reference"
    dut = tmp_path / "dut"
    write_bundle(reference, edge_time=0.2, reset_time=0.3)
    write_bundle(dut, edge_time=0.4, reset_time=0.55)

    result = compare_bundles(reference, dut, 6, "rising", 1.65, [2, 6])

    assert result["alignment"]["dut_minus_reference_s"] == pytest.approx(0.2)
    reset = next(signal for signal in result["signals"] if signal["channel"] == 2)
    assert reset["reference"]["first_activity_s"] == pytest.approx(0.1)
    assert reset["dut"]["first_activity_s"] == pytest.approx(0.15)
    assert reset["dut_minus_reference"]["first_activity_s"] == pytest.approx(0.05)


def test_comparison_rejects_nonidentical_capture_settings(tmp_path: Path):
    reference = tmp_path / "reference"
    dut = tmp_path / "dut"
    write_bundle(reference, 0.2, 0.3, sample_rate=1000)
    write_bundle(dut, 0.2, 0.3, sample_rate=2000)
    with pytest.raises(ValueError, match="settings differ"):
        compare_bundles(reference, dut, 6)


@pytest.mark.parametrize("damage", ["failed", "modified", "unhashed", "missing_stream"])
def test_comparison_rejects_invalid_evidence(tmp_path, damage):
    reference, dut = tmp_path / "reference", tmp_path / "dut"
    write_bundle(reference, 0.2, 0.3)
    write_bundle(dut, 0.2, 0.3)
    path = dut / "manifest.json"
    manifest = json.loads(path.read_text())
    if damage == "failed":
        manifest["status"] = "failed"
    elif damage == "modified":
        (dut / "raw/digital.csv").write_text("modified")
    else:
        del manifest["files"]["raw/digital.csv"]
        if damage == "missing_stream":
            (dut / "raw/digital.csv").unlink()
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="complete|hash"):
        compare_bundles(reference, dut, 6)


def test_rail_that_ends_outside_band_is_not_reported_as_settled():
    trace = SignalTrace(
        0, "rail", "rail", "analog", np.arange(100) * 0.01, np.r_[np.full(99, 3.3), 0.0]
    )
    metrics = _trace_metrics(trace, 0.0)
    assert metrics["settling_time_s"] is None
    assert metrics["settling_status"] == "not_observed"
    assert metrics["settling_observation_end_s"] == pytest.approx(0.99)
