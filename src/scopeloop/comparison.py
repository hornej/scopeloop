"""Generic, edge-aligned comparison of reference and DUT logic captures."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import numpy as np

from scopeloop.evidence import require_artifact, validate_bundle


@dataclass
class SignalTrace:
    """One exported digital or analog channel."""

    channel: int
    name: str
    signal_type: str
    kind: Literal["digital", "analog"]
    time: np.ndarray
    values: np.ndarray


def find_edge(
    trace: SignalTrace,
    edge: Literal["rising", "falling"],
    threshold: float | None = None,
) -> float:
    """Return the first selected threshold crossing in capture-relative seconds."""
    if len(trace.values) < 2:
        raise ValueError(f"Signal {trace.name} has fewer than two samples")
    if threshold is None:
        low, high = np.percentile(trace.values, [5, 95])
        if high <= low:
            raise ValueError(f"Signal {trace.name} has no measurable excursion")
        threshold = float((low + high) / 2)
    previous = trace.values[:-1]
    current = trace.values[1:]
    if edge == "rising":
        indices = np.flatnonzero((previous < threshold) & (current >= threshold))
    else:
        indices = np.flatnonzero((previous > threshold) & (current <= threshold))
    if len(indices) == 0:
        raise ValueError(f"No {edge} edge found on {trace.name} at {threshold} V")
    index = int(indices[0] + 1)
    return float(trace.time[index])


def compare_trace(
    reference: SignalTrace,
    dut: SignalTrace,
    reference_alignment_s: float,
    dut_alignment_s: float,
) -> dict[str, Any]:
    """Compare signal timing/activity without baking in board-specific expectations."""
    ref_metrics = _trace_metrics(reference, reference_alignment_s)
    dut_metrics = _trace_metrics(dut, dut_alignment_s)
    deltas: dict[str, float] = {}
    for key in set(ref_metrics) & set(dut_metrics):
        if isinstance(ref_metrics[key], (int, float)) and isinstance(
            dut_metrics[key], (int, float)
        ):
            deltas[key] = float(dut_metrics[key] - ref_metrics[key])
    return {
        "channel": reference.channel,
        "name": reference.name,
        "signal_type": reference.signal_type,
        "reference": ref_metrics,
        "dut": dut_metrics,
        "dut_minus_reference": deltas,
    }


def compare_bundles(
    reference_bundle: Path,
    dut_bundle: Path,
    align_channel: int,
    edge: Literal["rising", "falling"] = "rising",
    threshold_v: float | None = None,
    selected_channels: list[int] | None = None,
) -> dict[str, Any]:
    """Compare two evidence bundles after proving their capture settings match."""
    reference_manifest = validate_bundle(reference_bundle)
    dut_manifest = validate_bundle(dut_bundle)
    for manifest in (reference_manifest, dut_manifest):
        require_artifact(manifest, "channel-map.json")
        for kind in ("digital", "analog"):
            if manifest.get("recipe", {}).get(f"{kind}_channels"):
                require_artifact(manifest, f"raw/{kind}.csv")
    settings = _comparable_settings(reference_manifest)
    dut_settings = _comparable_settings(dut_manifest)
    if settings != dut_settings:
        raise ValueError(
            "Reference and DUT capture settings differ; repeat them with one recipe "
            f"(reference={settings}, dut={dut_settings})"
        )

    reference = load_bundle_traces(reference_bundle)
    dut = load_bundle_traces(dut_bundle)
    for manifest, traces in ((reference_manifest, reference), (dut_manifest, dut)):
        for kind in ("digital", "analog"):
            actual = {channel for channel, choices in traces.items()
                      if any(trace.kind == kind for trace in choices)}
            declared = set(manifest["recipe"].get(f"{kind}_channels") or [])
            if actual != declared:
                raise ValueError(f"Evidence {kind} channel coverage differs from the recipe")
    reference_align = _preferred_trace(reference, align_channel)
    dut_align = _preferred_trace(dut, align_channel)
    reference_alignment_s = find_edge(reference_align, edge, threshold_v)
    dut_alignment_s = find_edge(dut_align, edge, threshold_v)

    channels = selected_channels or sorted(set(reference) & set(dut))
    comparisons = []
    for channel in channels:
        comparisons.append(
            compare_trace(
                _preferred_trace(reference, channel),
                _preferred_trace(dut, channel),
                reference_alignment_s,
                dut_alignment_s,
            )
        )
    return {
        "schema_version": 1,
        "reference_bundle": str(reference_bundle.resolve()),
        "dut_bundle": str(dut_bundle.resolve()),
        "capture_settings": settings,
        "alignment": {
            "channel": align_channel,
            "name": reference_align.name,
            "edge": edge,
            "threshold_v": threshold_v,
            "reference_edge_s": reference_alignment_s,
            "dut_edge_s": dut_alignment_s,
            "dut_minus_reference_s": dut_alignment_s - reference_alignment_s,
        },
        "signals": comparisons,
    }


def load_bundle_traces(bundle_dir: Path) -> dict[int, list[SignalTrace]]:
    """Load Saleae analog.csv/digital.csv exports using the sidecar channel map."""
    channel_map = _read_json(bundle_dir / "channel-map.json")
    traces: dict[int, list[SignalTrace]] = {}
    for kind in ("digital", "analog"):
        csv_path = bundle_dir / "raw" / f"{kind}.csv"
        if not csv_path.exists():
            continue
        for channel, time, values in _read_saleae_csv(csv_path):
            mapping = channel_map.get(kind, {}).get(str(channel), {})
            trace = SignalTrace(
                channel=channel,
                name=mapping.get("name") or f"Channel {channel}",
                signal_type=mapping.get("signal_type") or "unknown",
                kind=kind,
                time=time,
                values=values,
            )
            traces.setdefault(channel, []).append(trace)
    return traces


def _read_saleae_csv(path: Path) -> list[tuple[int, np.ndarray, np.ndarray]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or len(reader.fieldnames) < 2:
            raise ValueError(f"No channel data in {path}")
        rows = list(reader)
    time_header = reader.fieldnames[0]
    result = []
    for header in reader.fieldnames[1:]:
        match = re.search(r"(?:Channel|Ch)[^0-9]*(\d+)", header, re.IGNORECASE)
        if not match:
            continue
        channel = int(match.group(1))
        time: list[float] = []
        values: list[float] = []
        for row in rows:
            if not row.get(time_header) or not row.get(header):
                continue
            time.append(float(row[time_header]))
            values.append(float(row[header]))
        result.append((channel, np.asarray(time), np.asarray(values)))
    return result


def _preferred_trace(traces: dict[int, list[SignalTrace]], channel: int) -> SignalTrace:
    choices = traces.get(channel)
    if not choices:
        raise ValueError(f"Channel {channel} is missing from an evidence bundle")
    signal_type = choices[0].signal_type
    if signal_type in {"analog", "rail", "strap"}:
        return next((trace for trace in choices if trace.kind == "analog"), choices[0])
    return next((trace for trace in choices if trace.kind == "digital"), choices[0])


def _trace_metrics(trace: SignalTrace, alignment_s: float) -> dict[str, Any]:
    if len(trace.values) == 0:
        return {"status": "no_samples"}
    values = trace.values
    times = trace.time - alignment_s
    low, high = np.percentile(values, [5, 95])
    threshold = (low + high) / 2
    edges = np.flatnonzero(np.diff(values >= threshold)) + 1 if high > low else np.array([])
    metrics: dict[str, Any] = {
        "status": "ok",
        "sample_count": len(values),
        "min": float(np.min(values)),
        "max": float(np.max(values)),
        "mean": float(np.mean(values)),
        "peak_to_peak": float(np.ptp(values)),
        "activity_edges": int(len(edges)),
        "first_activity_s": float(times[edges[0]]) if len(edges) else None,
        "last_activity_s": float(times[edges[-1]]) if len(edges) else None,
    }
    if trace.signal_type == "rail":
        final_value = float(np.median(values[max(0, int(len(values) * 0.9)) :]))
        tolerance = max(abs(final_value) * 0.05, float(np.ptp(values)) * 0.02, 0.01)
        outside = np.flatnonzero(np.abs(values - final_value) > tolerance)
        settle_index = int(outside[-1] + 1) if len(outside) else 0
        metrics["final_value"] = final_value
        metrics["settling_tolerance"] = tolerance
        metrics["settling_time_s"] = (
            float(times[settle_index]) if settle_index < len(values) - 1 else None
        )
        metrics["settling_observation_end_s"] = float(times[-1])
        metrics["settling_status"] = (
            "observed" if metrics["settling_time_s"] is not None else "not_observed"
        )
    if trace.signal_type in {"logic", "reset", "strap", "uart", "clock"}:
        metrics["transition_times_s"] = [float(times[index]) for index in edges[:100]]
    return metrics


def _comparable_settings(manifest: dict[str, Any]) -> dict[str, Any]:
    recipe = manifest.get("recipe", {})
    return {
        "digital_channels": recipe.get("digital_channels"),
        "analog_channels": recipe.get("analog_channels"),
        "digital_sample_rate": recipe.get("digital_sample_rate"),
        "analog_sample_rate": recipe.get("analog_sample_rate"),
        "logic_family_volts": recipe.get("logic_family_volts"),
        "duration_seconds": recipe.get("duration_seconds"),
        "trigger": recipe.get("trigger"),
    }


def _read_json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
