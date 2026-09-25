"""Check viewer timing, native archive layout, and evidence integrity."""

import configparser
import csv
import json
import zipfile

import numpy as np
import pytest
from typer.testing import CliRunner

from scopeloop.cli import app
from scopeloop.saleae_labels import sha256
from scopeloop.viewers import export_viewers


def seal(bundle, manifest):
    manifest["status"] = "complete"
    manifest["files"] = {
        p.relative_to(bundle).as_posix(): sha256(p)
        for p in bundle.rglob("*")
        if p.is_file() and p.name != "manifest.json"
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest))


def scope_bundle(path):
    path.mkdir()
    np.savez(
        path / "waveform.npz",
        time_s=np.arange(4) / 1000 - 0.002,
        voltage_v=np.array([0.0, 1.0, -0.5, 0.25]),
    )
    seal(
        path,
        {
            "waveform": {"channel": "CH1", "sample_rate": 1000},
            "channel_map": {"CH1": "rail, output\nprobe"},
        },
    )
    return path


def logic_bundle(path, *, timestamps=(-0.002, 0.0, 0.002), kind="digital", rate=1000):
    (path / "raw").mkdir(parents=True)
    labels = {str(ch): {"name": f"Signal {ch}"} for ch in range(9)}
    (path / "channel-map.json").write_text(json.dumps({kind: labels}))
    with (path / "raw" / f"{kind}.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["Time [s]", *(f"Channel {ch}" for ch in range(9))])
        for index, timestamp in enumerate(timestamps):
            writer.writerow([timestamp, *([index % 2] * 8), 1])
    seal(
        path,
        {
            "recipe": {
                f"{kind}_sample_rate": rate,
                f"{kind}_channels": list(range(9)),
                "duration_seconds": 99,
            }
        },
    )
    return path


def test_scope_preserves_origin_and_analog_type_without_changing_source(tmp_path):
    source = scope_bundle(tmp_path / "source")
    digest = sha256(source / "manifest.json")
    report = export_viewers(source, tmp_path / "viewers")
    trace = report["exports"][0]
    assert trace["time_origin_s"] == -0.002
    assert trace["last_observed_time_s"] == 0.001
    assert trace["labels"][0] == {
        "original": "rail, output\nprobe",
        "exported": "rail  output probe",
    }
    with (tmp_path / "viewers/analog-ngscopeclient.csv").open() as stream:
        rows = list(csv.reader(stream))
    assert rows[1][1] not in {"0", "1"}
    assert rows[2][1] not in {"0", "1"}
    assert float(rows[1][0]) == -0.002
    with zipfile.ZipFile(tmp_path / "viewers/analog-pulseview.sr") as archive:
        assert archive.read("version") == b"2"
        metadata = configparser.ConfigParser()
        metadata.read_string(archive.read("metadata").decode())
        assert metadata["device 1"]["samplerate"] == "1000 Hz"
        assert metadata["device 1"]["analog1"] == "rail  output probe"
        samples = np.frombuffer(archive.read("analog-1-1-1"), dtype="<f4")
        np.testing.assert_array_equal(samples, [0.0, 1.0, -0.5, 0.25])
    assert sha256(source / "manifest.json") == digest


def test_sparse_digital_expands_holds_and_packs_more_than_eight_channels(tmp_path):
    source = logic_bundle(tmp_path / "source")
    report = export_viewers(source, tmp_path / "viewers")
    trace = report["exports"][0]
    assert trace["pulseview_sample_count"] == 5  # Final observed sample included, no 99s tail.
    assert trace["source_row_count"] == 3
    assert trace["time_origin_s"] == -0.002
    with zipfile.ZipFile(tmp_path / "viewers/digital-pulseview.sr") as archive:
        samples = np.frombuffer(archive.read("logic-1-1"), dtype=np.uint8).reshape(-1, 2)
        np.testing.assert_array_equal(samples, [[0, 1], [0, 1], [255, 1], [255, 1], [0, 1]])
    with (tmp_path / "viewers/digital-ngscopeclient.csv").open() as stream:
        rows = list(csv.reader(stream))
    assert len(rows) == 4
    assert rows[1][1:] == ["0"] * 8 + ["1"]


@pytest.mark.parametrize(
    "timestamps,kind,error",
    [
        ((0.0, 0.0015, 0.003), "digital", "off the recorded"),
        ((0.0, 0.002, 0.004), "analog", "uniformly sampled"),
        ((0.0, 0.0, 0.002), "digital", "strictly increasing"),
        ((0.0, float("nan"), 0.002), "digital", "finite"),
        ((0.0,), "digital", "time span"),
    ],
)
def test_invalid_timing_fails_before_output_creation(tmp_path, timestamps, kind, error):
    source = logic_bundle(tmp_path / "source", timestamps=timestamps, kind=kind)
    with pytest.raises(ValueError, match=error):
        export_viewers(source, tmp_path / "viewers")
    assert not (tmp_path / "viewers").exists()


def test_expansion_limit_and_source_integrity(tmp_path):
    source = logic_bundle(tmp_path / "source")
    with pytest.raises(ValueError, match="sample limit"):
        export_viewers(source, tmp_path / "viewers", max_samples=4)
    with pytest.raises(ValueError, match="outside"):
        export_viewers(source, source / "derived")
    (source / "raw/digital.csv").write_text("corrupt")
    with pytest.raises(ValueError, match="hash mismatch"):
        export_viewers(source, tmp_path / "viewers")


def test_cli_exports_offline_and_refuses_overwrite(tmp_path):
    source = scope_bundle(tmp_path / "source")
    output = tmp_path / "viewers"
    args = ["evidence", "export", str(source), str(output)]
    first = CliRunner().invoke(app, args)
    assert first.exit_code == 0, first.output
    assert json.loads(first.stdout)["status"] == "complete"
    digest = sha256(output / "export.json")
    second = CliRunner().invoke(app, args)
    assert second.exit_code == 1
    assert sha256(output / "export.json") == digest


def test_rounded_endpoint_cannot_exceed_sample_limit(tmp_path):
    source = logic_bundle(tmp_path / "source", timestamps=(0.0, 9.999999999e-6), rate=1_000_000)
    with pytest.raises(ValueError, match="sample limit"):
        export_viewers(source, tmp_path / "viewers", max_samples=10)


def test_missing_declared_stream_is_not_silently_omitted(tmp_path):
    source = logic_bundle(tmp_path / "source")
    manifest = json.loads((source / "manifest.json").read_text())
    manifest["recipe"]["analog_channels"] = [0]
    manifest["recipe"]["analog_sample_rate"] = 1000
    (source / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="no recorded hash"):
        export_viewers(source, tmp_path / "viewers")


@pytest.mark.asyncio
async def test_mcp_exports_saved_evidence_without_instrument_configuration(tmp_path, monkeypatch):
    import scopeloop.mcp_server as server

    source = scope_bundle(tmp_path / "source")
    monkeypatch.setattr(server, "_config", None)
    monkeypatch.setattr(server, "_logic_service", None)
    result = await server._handle_tool(
        "scopeloop_evidence_export",
        {
            "bundle": str(source),
            "output": str(tmp_path / "viewers"),
            "max_samples": 4,
        },
    )
    assert result["status"] == "complete"
    assert server._logic_service is None
