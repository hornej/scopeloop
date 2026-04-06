"""Waveform Storage - Efficient storage and streaming of waveform data.

Raw captures are stored locally by reference (as .npy files), with metadata
stored separately. The UI receives downsampled data for display.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class WaveformMetadata:
    """Metadata for a stored waveform."""

    capture_id: str
    channel: str
    sample_rate: float
    record_length: int
    voltage_scale: float
    voltage_offset: float
    time_offset: float
    captured_at: str  # ISO format timestamp

    # Computed measurements (cached)
    vpp: float | None = None
    vmax: float | None = None
    vmin: float | None = None
    vrms: float | None = None
    frequency: float | None = None

    # Instrument settings at capture time
    timebase: float | None = None
    trigger_level: float | None = None
    trigger_source: str | None = None
    coupling: str | None = None

    # Additional context
    session_id: str | None = None
    iteration: int | None = None
    description: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> WaveformMetadata:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class WaveformStore:
    """Manages waveform data storage with efficient storage and streaming.

    Features:
    - Stores raw samples as .npy files (fast, compact)
    - Separate metadata JSON files
    - Downsampled streaming for UI display
    - Automatic measurement computation and caching

    Usage:
        store = WaveformStore(Path("./waveforms"))

        # Store a capture
        capture_id = store.store_capture(
            samples=np.array([...]),
            metadata=WaveformMetadata(
                capture_id="",  # Will be generated
                channel="CH1",
                sample_rate=1e6,
                ...
            ),
        )

        # Get downsampled for UI
        downsampled = store.get_downsampled(capture_id, max_points=1000)

        # Get full resolution for analysis
        samples = store.get_full(capture_id)

        # Get metadata
        metadata = store.get_metadata(capture_id)
    """

    def __init__(self, storage_dir: Path):
        self.storage_dir = storage_dir
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def store_capture(
        self,
        samples: np.ndarray,
        metadata: WaveformMetadata,
        compute_measurements: bool = True,
    ) -> str:
        """Store a waveform capture.

        Args:
            samples: Numpy array of samples.
            metadata: Waveform metadata.
            compute_measurements: Whether to compute and cache measurements.

        Returns:
            Capture ID.
        """
        # Generate capture ID if not provided
        if not metadata.capture_id:
            metadata.capture_id = self._generate_capture_id()

        capture_id = metadata.capture_id

        # Update record length
        metadata.record_length = len(samples)

        # Compute measurements if requested
        if compute_measurements:
            self._compute_measurements(samples, metadata)

        # Save samples
        samples_path = self._get_samples_path(capture_id)
        np.save(samples_path, samples)

        # Save metadata
        metadata_path = self._get_metadata_path(capture_id)
        with open(metadata_path, "w") as f:
            json.dump(metadata.to_dict(), f, indent=2)

        logger.info(f"Stored waveform {capture_id}: {len(samples)} samples")
        return capture_id

    def get_full(self, capture_id: str) -> np.ndarray:
        """Get full resolution waveform data.

        Args:
            capture_id: Capture ID.

        Returns:
            Numpy array of samples.
        """
        samples_path = self._get_samples_path(capture_id)
        if not samples_path.exists():
            raise FileNotFoundError(f"Waveform not found: {capture_id}")

        return np.load(samples_path)

    def get_downsampled(
        self,
        capture_id: str,
        max_points: int = 10000,
    ) -> np.ndarray:
        """Get downsampled waveform data for UI display.

        Uses min-max downsampling to preserve signal shape.

        Args:
            capture_id: Capture ID.
            max_points: Maximum number of points to return.

        Returns:
            Downsampled numpy array.
        """
        samples = self.get_full(capture_id)

        if len(samples) <= max_points:
            return samples

        return self._downsample_minmax(samples, max_points)

    def get_metadata(self, capture_id: str) -> WaveformMetadata:
        """Get waveform metadata.

        Args:
            capture_id: Capture ID.

        Returns:
            Waveform metadata.
        """
        metadata_path = self._get_metadata_path(capture_id)
        if not metadata_path.exists():
            raise FileNotFoundError(f"Metadata not found: {capture_id}")

        with open(metadata_path) as f:
            data = json.load(f)

        return WaveformMetadata.from_dict(data)

    def list_captures(
        self,
        channel: str | None = None,
        session_id: str | None = None,
        limit: int | None = None,
    ) -> list[WaveformMetadata]:
        """List stored waveform captures.

        Args:
            channel: Filter by channel.
            session_id: Filter by session.
            limit: Maximum number to return.

        Returns:
            List of metadata objects.
        """
        captures = []

        for meta_path in self.storage_dir.glob("*.json"):
            try:
                metadata = self.get_metadata(meta_path.stem)

                if channel and metadata.channel != channel:
                    continue
                if session_id and metadata.session_id != session_id:
                    continue

                captures.append(metadata)
            except Exception as e:
                logger.warning(f"Error loading {meta_path}: {e}")

        # Sort by capture time (newest first)
        captures.sort(key=lambda m: m.captured_at, reverse=True)

        if limit:
            captures = captures[:limit]

        return captures

    def delete_capture(self, capture_id: str) -> None:
        """Delete a waveform capture.

        Args:
            capture_id: Capture ID.
        """
        samples_path = self._get_samples_path(capture_id)
        metadata_path = self._get_metadata_path(capture_id)

        if samples_path.exists():
            samples_path.unlink()
        if metadata_path.exists():
            metadata_path.unlink()

        logger.info(f"Deleted waveform: {capture_id}")

    def get_time_array(self, capture_id: str) -> np.ndarray:
        """Get time array for a capture.

        Args:
            capture_id: Capture ID.

        Returns:
            Numpy array of time values in seconds.
        """
        metadata = self.get_metadata(capture_id)
        samples = self.get_full(capture_id)

        dt = 1.0 / metadata.sample_rate
        return np.arange(len(samples)) * dt + metadata.time_offset

    def get_for_ui(
        self,
        capture_id: str,
        max_points: int = 10000,
    ) -> dict[str, Any]:
        """Get waveform data formatted for UI display.

        Args:
            capture_id: Capture ID.
            max_points: Maximum number of points.

        Returns:
            Dict with samples, time, and metadata for UI.
        """
        metadata = self.get_metadata(capture_id)
        samples = self.get_downsampled(capture_id, max_points)

        # Calculate time array for downsampled data
        dt = 1.0 / metadata.sample_rate
        downsample_factor = metadata.record_length / len(samples)
        effective_dt = dt * downsample_factor

        time_array = (
            np.arange(len(samples)) * effective_dt + metadata.time_offset
        ).tolist()

        return {
            "capture_id": capture_id,
            "channel": metadata.channel,
            "samples": samples.tolist(),
            "time": time_array,
            "sample_rate": metadata.sample_rate,
            "record_length": metadata.record_length,
            "downsampled_length": len(samples),
            "voltage_scale": metadata.voltage_scale,
            "voltage_offset": metadata.voltage_offset,
            "captured_at": metadata.captured_at,
            "measurements": {
                "vpp": metadata.vpp,
                "vmax": metadata.vmax,
                "vmin": metadata.vmin,
                "vrms": metadata.vrms,
                "frequency": metadata.frequency,
            },
        }

    def _generate_capture_id(self) -> str:
        """Generate a unique capture ID."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        short_uuid = uuid.uuid4().hex[:6]
        return f"cap_{timestamp}_{short_uuid}"

    def _get_samples_path(self, capture_id: str) -> Path:
        """Get path to samples file."""
        return self.storage_dir / f"{capture_id}.npy"

    def _get_metadata_path(self, capture_id: str) -> Path:
        """Get path to metadata file."""
        return self.storage_dir / f"{capture_id}.json"

    def _compute_measurements(
        self,
        samples: np.ndarray,
        metadata: WaveformMetadata,
    ) -> None:
        """Compute and cache measurements in metadata."""
        metadata.vpp = float(np.max(samples) - np.min(samples))
        metadata.vmax = float(np.max(samples))
        metadata.vmin = float(np.min(samples))
        metadata.vrms = float(np.sqrt(np.mean(samples**2)))

        # Compute frequency if we have enough samples
        if len(samples) > 100 and metadata.sample_rate > 0:
            try:
                from scopeloop.measurements import MeasurementEngine

                engine = MeasurementEngine()
                freq_result = engine.frequency(samples, metadata.sample_rate)
                if freq_result.confidence > 0.5:
                    metadata.frequency = freq_result.value
            except Exception as e:
                logger.debug(f"Could not compute frequency: {e}")

    def _downsample_minmax(
        self,
        samples: np.ndarray,
        max_points: int,
    ) -> np.ndarray:
        """Downsample using min-max method to preserve signal shape.

        For each output point, we keep both the min and max of the
        input samples in that bucket, preserving peaks and valleys.
        """
        n_samples = len(samples)
        bucket_size = n_samples // (max_points // 2)

        if bucket_size < 2:
            return samples

        # Trim to exact multiple of bucket size
        n_buckets = n_samples // bucket_size
        trimmed = samples[: n_buckets * bucket_size]

        # Reshape into buckets
        buckets = trimmed.reshape(n_buckets, bucket_size)

        # Get min and max for each bucket
        mins = np.min(buckets, axis=1)
        maxs = np.max(buckets, axis=1)

        # Interleave min and max
        result = np.empty(n_buckets * 2)
        result[0::2] = mins
        result[1::2] = maxs

        return result
