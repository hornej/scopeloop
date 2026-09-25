"""Offline exports for ngscopeclient and PulseView, preserving source evidence."""

from __future__ import annotations

import csv
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from scopeloop.evidence import require_artifact, validate_bundle
from scopeloop.saleae_labels import sha256


@dataclass
class ExportTrace:
    kind: str
    labels: list[str]
    times: np.ndarray
    values: np.ndarray
    sample_rate: int
    indices: np.ndarray

    @property
    def sample_count(self) -> int:
        return int(self.indices[-1]) + 1


def _trace(kind, labels, times, values, sample_rate, max_samples) -> ExportTrace:
    times, values = np.asarray(times, dtype=float), np.asarray(values, dtype=float)
    rate = float(sample_rate)
    if not np.isfinite(rate) or rate <= 0 or rate != int(rate) or rate > 10**12:
        raise ValueError("Viewer export requires a positive integral sample rate <= 1 THz")
    if (
        times.ndim != 1
        or len(times) < 2
        or values.shape != (len(times), len(labels))
        or not len(labels)
        or not np.isfinite(times).all()
        or not np.isfinite(values).all()
        or not np.all(np.diff(times) > 0)
    ):
        raise ValueError("Export requires at least two finite, strictly increasing sample rows")
    positions = (times - times[0]) * rate
    if positions[-1] >= max_samples:
        raise ValueError(
            f"Export exceeds the {max_samples} sample limit; raise --max-samples explicitly"
        )
    indices = np.rint(positions).astype(np.int64)
    if int(indices[-1]) + 1 > max_samples:
        raise ValueError(f"Export exceeds the {max_samples} sample limit after rounding")
    if not np.allclose(positions, indices, rtol=0, atol=0.001):
        raise ValueError("Timestamps are off the recorded sample grid")
    if not np.all(np.diff(indices) > 0):
        raise ValueError("Timestamps do not identify distinct samples")
    if kind == "digital":
        if not np.isin(values, [0, 1]).all():
            raise ValueError("Digital values must be exactly 0 or 1")
    elif not np.all(np.diff(indices) == 1):
        raise ValueError("Analog data must be uniformly sampled at its recorded rate")
    elif np.max(np.abs(values)) > np.finfo(np.float32).max:
        raise ValueError("Analog values exceed the viewers' float32 range")
    return ExportTrace(kind, labels, times, values, int(rate), indices)


def _saleae_trace(bundle, manifest, kind, channel_map, max_samples):
    name = f"raw/{kind}.csv"
    require_artifact(manifest, name)
    with (bundle / name).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream)
        header = next(reader, [])
        if len(header) < 2 or not header[0].startswith("Time"):
            raise ValueError(f"Unsupported Saleae CSV header: {name}")
        channels = []
        for field in header[1:]:
            match = re.fullmatch(r"(?:Channel|Ch)\s+(\d+)", field, flags=re.IGNORECASE)
            if not match:
                raise ValueError(f"Unsupported channel header: {field}")
            channels.append(int(match[1]))
        if len(channels) != len(set(channels)):
            raise ValueError("Duplicate CSV channel identifiers")
        if set(channels) != set(manifest["recipe"].get(f"{kind}_channels") or []):
            raise ValueError(f"Evidence {kind} channel coverage differs from the recipe")
        rows = []
        for row in reader:
            if not row:
                continue
            if len(row) != len(header):
                raise ValueError("CSV rows must contain every channel and timestamp")
            rows.append([float(value) for value in row])
            if len(rows) > max_samples:
                raise ValueError("CSV exceeds the configured sample limit")
    if len(rows) < 2:
        raise ValueError("CSV has no established time span; at least two rows are required")
    data = np.asarray(rows)
    labels = [
        channel_map.get(kind, {}).get(str(ch), {}).get("name") or f"Channel {ch}" for ch in channels
    ]
    rate = manifest["recipe"][f"{kind}_sample_rate"]
    return _trace(kind, labels, data[:, 0], data[:, 1:], rate, max_samples)


def _export_labels(labels: list[str]) -> list[str]:
    # ngscopeclient's CSV importer does not implement RFC 4180 quoted fields.
    result = []
    for index, original in enumerate(labels):
        label = re.sub(r"[\x00-\x1f,\\]", " ", original).strip() or f"Channel {index}"
        if label in result:
            label = f"{label} [{index}]"
        while label in result:
            label += "_"
        result.append(label)
    return result


def _write_csv(path: Path, trace: ExportTrace, labels: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write("Time (s)," + ",".join(labels) + "\n")
        for timestamp, values in zip(trace.times, trace.values, strict=True):
            # Analog 0/1 MUST include an exponent to avoid digital autodetection.
            fields = (
                [str(int(value)) for value in values]
                if trace.kind == "digital"
                else [format(value, ".17e") for value in values]
            )
            stream.write(format(timestamp, ".17e") + "," + ",".join(fields) + "\n")


def _write_sr(path: Path, trace: ExportTrace, labels: list[str]) -> None:
    metadata = [
        "[global]",
        "sigrok version=0.5.2",
        "",
        "[device 1]",
        f"samplerate={trace.sample_rate} Hz",
    ]
    if trace.kind == "digital":
        metadata += [
            "capturefile=logic-1",
            f"total probes={len(labels)}",
            "total analog=0",
            f"unitsize={(len(labels) + 7) // 8}",
        ]
        prefix = "probe"
    else:
        metadata += [f"total analog={len(labels)}"]
        prefix = "analog"
    metadata += [f"{prefix}{index}={label}" for index, label in enumerate(labels, 1)]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        archive.writestr("version", "2")
        archive.writestr("metadata", "\n".join(metadata) + "\n")
        if trace.kind == "digital":
            packed = np.packbits(trace.values.astype(np.uint8), axis=1, bitorder="little")
            with archive.open("logic-1-1", "w", force_zip64=True) as stream:
                for start in range(0, trace.sample_count, 1_000_000):
                    samples = np.arange(start, min(start + 1_000_000, trace.sample_count))
                    positions = np.searchsorted(trace.indices, samples, side="right") - 1
                    stream.write(packed[positions].tobytes())
        else:
            for channel in range(len(labels)):
                with archive.open(f"analog-1-{channel + 1}-1", "w", force_zip64=True) as stream:
                    for start in range(0, len(trace.times), 1_000_000):
                        stream.write(
                            trace.values[start : start + 1_000_000, channel].astype("<f4").tobytes()
                        )


def export_viewers(bundle: Path, output: Path, max_samples: int = 50_000_000) -> dict:
    """Create derived exports in a new directory without any instrument connection."""
    if not isinstance(max_samples, int) or max_samples < 2:
        raise ValueError("max_samples must be an integer >= 2")
    bundle, output = bundle.resolve(), output.resolve()
    if output == bundle or bundle in output.parents:
        raise ValueError("Viewer output must be outside the immutable source bundle")
    manifest = validate_bundle(bundle)
    traces = []
    if "waveform" in manifest:
        require_artifact(manifest, "waveform.npz")
        with np.load(bundle / "waveform.npz", allow_pickle=False) as data:
            waveform = manifest["waveform"]
            label = manifest.get("channel_map", {}).get(waveform["channel"], waveform["channel"])
            traces.append(
                _trace(
                    "analog",
                    [label],
                    data["time_s"],
                    data["voltage_v"][:, None],
                    waveform["sample_rate"],
                    max_samples,
                )
            )
    else:
        require_artifact(manifest, "channel-map.json")
        channel_map = json.loads((bundle / "channel-map.json").read_text(encoding="utf-8"))
        for kind in ("digital", "analog"):
            if manifest["recipe"].get(f"{kind}_channels"):
                traces.append(_saleae_trace(bundle, manifest, kind, channel_map, max_samples))
    if not traces:
        raise ValueError("Bundle contains no supported raw waveform exports")
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "schema_version": 1,
        "status": "started",
        "created_at": datetime.now(UTC).isoformat(),
        "source_bundle": str(bundle),
        "source_manifest_sha256": sha256(bundle / "manifest.json"),
        "source_artifacts": manifest["files"],
        "source_limitations": manifest.get("limitations", []),
        "exports": [],
        "limitations": [
            "Derived viewer files do not replace the original evidence and its settings.",
            "PulseView starts at zero; add time_origin_s to recover source timestamps.",
            "PulseView analog uses float32; ngscopeclient also converts analog values to float32.",
            "Digital holds cover only observed CSV timestamps; requested duration is not used.",
            "Last timestamp is the final observed sample; viewers display a final sample interval.",
        ],
    }
    try:
        (output / "source-manifest.json").write_bytes((bundle / "manifest.json").read_bytes())
        for trace in traces:
            labels = _export_labels(trace.labels)
            csv_name, sr_name = f"{trace.kind}-ngscopeclient.csv", f"{trace.kind}-pulseview.sr"
            _write_csv(output / csv_name, trace, labels)
            _write_sr(output / sr_name, trace, labels)
            report["exports"].append(
                {
                    "kind": trace.kind,
                    "ngscopeclient_csv": csv_name,
                    "pulseview_session": sr_name,
                    "labels": [
                        {"original": a, "exported": b}
                        for a, b in zip(trace.labels, labels, strict=True)
                    ],
                    "sample_rate_hz": trace.sample_rate,
                    "pulseview_sample_count": trace.sample_count,
                    "source_row_count": len(trace.times),
                    "time_origin_s": float(trace.times[0]),
                    "last_observed_time_s": float(trace.times[-1]),
                    "observed_span_s": float(trace.times[-1] - trace.times[0]),
                }
            )
        report["status"] = "complete"
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        report["files"] = {
            path.name: sha256(path)
            for path in sorted(output.iterdir())
            if path.is_file() and path.name != "export.json"
        }
        (output / "export.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        (output / "SHA256SUMS").write_text(
            "".join(
                f"{sha256(path)}  {path.name}\n"
                for path in sorted(output.iterdir())
                if path.is_file() and path.name != "SHA256SUMS"
            ),
            encoding="utf-8",
        )
    return report
