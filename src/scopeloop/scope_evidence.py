"""Portable ripple and externally triggered load-step evidence; no DUT actuation."""

from __future__ import annotations

import importlib.metadata
import json
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from scopeloop.saleae_labels import sha256


class ScopeRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["noise_floor", "ripple", "load_step"]
    channel: str = "CH1"
    signal: str = Field(min_length=1)
    points: int | None = Field(default=None, gt=1)
    timeout: float = Field(default=10, gt=0, le=300)
    probe: str = Field(min_length=1)
    connection: str = Field(min_length=1)
    ground: str = Field(min_length=1)
    limitations: list[str]
    noise_floor_manifest: Path | None = None
    before_window: tuple[float, float] | None = None
    after_window: tuple[float, float] | None = None
    settling_band_v: float | None = Field(default=None, gt=0)
    dc_telemetry: dict = Field(default_factory=dict)
    run_metadata: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def windows(self):
        if self.kind == "load_step":
            if not self.before_window or not self.after_window or not self.settling_band_v:
                raise ValueError("load_step requires before/after windows and settling_band_v")
            a, b = self.before_window
            c, d = self.after_window
            if not a < b <= 0 <= c < d:
                raise ValueError("Windows must be ordered around the trigger at t=0")
        return self


def waveform_metrics(waveform, recipe: ScopeRecipe) -> dict:
    samples = waveform.samples
    if len(samples) < 2 or not np.isfinite(samples).all():
        raise ValueError("Measurement requires at least two finite samples")
    mean = float(np.mean(samples))
    result = {
        "mean_v": mean,
        "peak_to_peak_v": float(np.ptp(samples)),
        "ac_rms_v": float(np.sqrt(np.mean((samples - mean) ** 2))),
        "total_rms_v": float(np.sqrt(np.mean(samples**2))),
    }
    if recipe.kind == "load_step":
        times = waveform.time_array
        averages = []
        for start, end in (recipe.before_window, recipe.after_window):
            if start < times[0] or end > times[-1]:
                raise ValueError("Requested measurement window is outside captured time range")
            selected = samples[(times >= start) & (times <= end)]
            if len(selected) < 2:
                raise ValueError("Each load-step window needs at least two samples")
            averages.append(float(np.mean(selected)))
        post = samples[times >= 0]
        post_times = times[times >= 0]
        target = averages[1]
        outside = np.flatnonzero(np.abs(post - target) > recipe.settling_band_v)
        next_index = int(outside[-1]) + 1 if len(outside) else 0
        result["load_step"] = {
            "before_mean_v": averages[0],
            "after_mean_v": target,
            "overshoot_v": max(0.0, float(np.max(post)) - target),
            "undershoot_v": max(0.0, target - float(np.min(post))),
            "settling_band_v": recipe.settling_band_v,
            "settled_by_s": (
                float(post_times[next_index]) if next_index < len(post_times) - 1 else None
            ),
            "settling_observation_end_s": float(times[-1]),
            "event_reference": "scope trigger; confirm alignment with the load control signal",
        }
    return result


def _setup_signature(waveform: dict) -> dict:
    setup = waveform["metadata"]["setup"]
    return {
        "setup": {
            key: value
            for key, value in setup.items()
            if key in {"*IDN?", "SARA?", "TDIV?", "BWL?", "ACQW?"}
            or key.endswith((":VDIV?", ":OFST?", ":CPL?", ":ATTN?"))
        },
        # Equal front-panel timebase/rate does not imply equal transferred coverage:
        # a points limit can hide noise peaks and understate the baseline.
        "record_length": waveform["record_length"],
        "sample_rate": waveform["sample_rate"],
        "time_offset": waveform["time_offset"],
    }


async def capture_scope_evidence(scope, recipe: ScopeRecipe, output: Path) -> dict:
    """Caller owns a connected scope; reserve it for capture and evidence writing."""
    output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": 1,
        "status": "started",
        "started_at": datetime.now(UTC).isoformat(),
        "recipe": recipe.model_dump(mode="json"),
        "channel_map": {recipe.channel: recipe.signal},
        "dc_telemetry": recipe.dc_telemetry,
        "software": {
            "scopeloop": importlib.metadata.version("scopeloop"),
            "python": sys.version,
            "platform": platform.platform(),
        },
    }
    try:
        async with scope._lock:
            waveform = await scope.capture_waveform(
                recipe.channel,
                recipe.points,
                acquisition="single" if recipe.kind == "load_step" else "auto",
                timeout=recipe.timeout,
            )
            (output / "waveform.bin").write_bytes(waveform.raw_data)
            (output / "response.bin").write_bytes(waveform.raw_response)
            np.savez(
                output / "waveform.npz", time_s=waveform.time_array, voltage_v=waveform.samples
            )
            manifest["waveform"] = waveform.to_dict()
            manifest["measurements"] = waveform_metrics(waveform, recipe)
            limitations = list(recipe.limitations)
            if waveform.metadata["clipping_detected"]:
                limitations.append("ADC endpoint clipping detected; amplitude is not qualified")
            manifest["noise_floor"] = None
            if recipe.noise_floor_manifest:
                path = recipe.noise_floor_manifest.expanduser().resolve()
                baseline = json.loads(path.read_text())
                if baseline["status"] != "complete" or baseline["recipe"]["kind"] != "noise_floor":
                    raise ValueError("Baseline must be a completed noise_floor capture")
                if _setup_signature(baseline["waveform"]) != _setup_signature(manifest["waveform"]):
                    raise ValueError("Noise-floor setup differs from this capture")
                for name, digest in baseline["files"].items():
                    artifact = (path.parent / name).resolve()
                    if artifact.parent != path.parent or sha256(artifact) != digest:
                        raise ValueError(f"Noise-floor artifact hash mismatch: {name}")
                if baseline["waveform"]["metadata"]["clipping_detected"]:
                    raise ValueError("Noise-floor capture is clipped")
                manifest["noise_floor"] = {
                    "manifest_sha256": sha256(path),
                    "measurements": baseline["measurements"],
                }
                (output / "noise-floor-manifest.json").write_bytes(path.read_bytes())
            elif recipe.kind != "noise_floor":
                limitations.append("Measurement noise floor not measured with matching setup")
            coupling = waveform.metadata["setup"][f"C{recipe.channel[-1]}:CPL?"]
            if "A1M" in coupling:
                limitations.append("AC coupling removes DC and attenuates low frequency content")
            manifest["limitations"] = limitations
            manifest["status"] = "complete"
    except BaseException as exc:
        manifest["status"] = "failed"
        manifest["error"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        (output / "scpi.json").write_text(json.dumps(scope.transcript, indent=2))
        manifest["completed_at"] = datetime.now(UTC).isoformat()
        manifest["files"] = {
            path.name: sha256(path)
            for path in sorted(output.iterdir())
            if path.is_file() and path.name != "manifest.json"
        }
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2, allow_nan=False))
        (output / "SHA256SUMS").write_text(
            "".join(
                f"{sha256(path)}  {path.name}\n"
                for path in sorted(output.iterdir())
                if path.is_file() and path.name != "SHA256SUMS"
            )
        )
    return {"manifest": str(output / "manifest.json"), **manifest}
