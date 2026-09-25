"""Measurement Engine - Python-computed metrics from raw waveform samples.

Computes measurements from raw samples for reliability, rather than relying
solely on oscilloscope built-in measurements which can be inconsistent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

import numpy as np
from scipy import signal
from scipy.fft import fft, fftfreq

logger = logging.getLogger(__name__)


@dataclass
class MeasurementResult:
    """Result of a measurement computation."""

    measurement_type: str
    value: float | None
    unit: str
    confidence: float = 1.0  # 0-1, how confident we are in this measurement
    method: str = ""  # Method used (e.g., "zero_crossing", "fft")
    details: dict | None = None  # Additional details
    status: str = "valid"

    def __post_init__(self):
        if self.details is None:
            self.details = {}

    def to_dict(self) -> dict:
        return {
            "type": self.measurement_type,
            "value": self.value,
            "unit": self.unit,
            "confidence": self.confidence,
            "method": self.method,
            "details": self.details,
            "status": self.status,
        }


class MeasurementEngine:
    """Compute measurements from raw waveform data.

    All measurements are computed in Python from raw samples, giving
    consistent and repeatable results.

    Usage:
        engine = MeasurementEngine()

        # From numpy array of samples
        samples = np.array([...])
        sample_rate = 1e6  # 1 MHz

        freq = engine.frequency(samples, sample_rate)
        print(f"Frequency: {freq.value} {freq.unit}")

        amp = engine.amplitude(samples)
        print(f"Amplitude: {amp.value} {amp.unit}")
    """

    def frequency(
        self,
        samples: np.ndarray,
        sample_rate: float,
        method: Literal["zero_crossing", "fft", "auto"] = "auto",
        signal_type: Literal["periodic", "clock", "logic", "uart"] = "periodic",
        logic_low_max_v: float | None = None,
        logic_high_min_v: float | None = None,
        min_peak_to_peak_v: float = 0.05,
        min_cycles: int = 3,
        max_period_cv: float = 0.2,
    ) -> MeasurementResult:
        """Compute frequency only after amplitude, electrical, and edge checks."""
        samples = np.asarray(samples, dtype=float)
        if (
            not np.isfinite(sample_rate)
            or sample_rate <= 0
            or len(samples) < 4
            or not np.all(np.isfinite(samples))
        ):
            return self._invalid_frequency(
                method,
                "insufficient_data",
                "Samples must be finite and sample_rate must be finite and positive",
            )

        low_percentile, high_percentile = np.percentile(samples, [5, 95])
        robust_vpp = float(high_percentile - low_percentile)
        if robust_vpp < min_peak_to_peak_v:
            return self._invalid_frequency(
                method,
                "insufficient_amplitude",
                f"Robust peak-to-peak amplitude {robust_vpp:.6g} V is below the minimum",
                {
                    "robust_peak_to_peak_v": robust_vpp,
                    "minimum_peak_to_peak_v": min_peak_to_peak_v,
                },
            )

        if signal_type in {"clock", "logic", "uart"}:
            if logic_low_max_v is None or logic_high_min_v is None:
                return self._invalid_frequency(
                    method,
                    "indeterminate_logic_levels",
                    "Logic frequency requires guaranteed low and high limits",
                )
            if low_percentile > logic_low_max_v or high_percentile < logic_high_min_v:
                return self._invalid_frequency(
                    method,
                    "indeterminate_logic_levels",
                    "Signal does not make valid low and high electrical excursions",
                    {
                        "low_percentile_v": float(low_percentile),
                        "high_percentile_v": float(high_percentile),
                        "logic_low_max_v": logic_low_max_v,
                        "logic_high_min_v": logic_high_min_v,
                    },
                )

        if method == "auto":
            method = "zero_crossing"

        if method == "zero_crossing":
            result = self._frequency_zero_crossing(samples, sample_rate)
        elif method == "fft":
            result = self._frequency_fft(samples, sample_rate)
        else:
            raise ValueError(f"Unknown method: {method}")

        result.details.update({"signal_type": signal_type, "robust_peak_to_peak_v": robust_vpp})
        if result.value is None:
            return result
        if result.method == "zero_crossing":
            cycles = int(result.details.get("periods_measured", 0))
            period_cv = float(result.details.get("period_cv", float("inf")))
            if cycles < min_cycles:
                return self._invalid_frequency(
                    result.method,
                    "insufficient_edges",
                    f"Only {cycles} cycles were measured; {min_cycles} are required",
                    result.details,
                )
            if period_cv > max_period_cv:
                return self._invalid_frequency(
                    result.method,
                    "poor_edge_quality",
                    f"Period coefficient of variation {period_cv:.3f} exceeds the limit",
                    result.details,
                )
        elif result.confidence < 0.5:
            return self._invalid_frequency(
                result.method,
                "poor_spectral_quality",
                "No sufficiently prominent periodic component was found",
                result.details,
            )
        return result

    @staticmethod
    def _invalid_frequency(
        method: str,
        status: str,
        reason: str,
        details: dict | None = None,
    ) -> MeasurementResult:
        return MeasurementResult(
            measurement_type="frequency",
            value=None,
            unit="Hz",
            confidence=0.0,
            method=method,
            details={"reason": reason, **(details or {})},
            status=status,
        )

    def _frequency_zero_crossing(
        self,
        samples: np.ndarray,
        sample_rate: float,
    ) -> MeasurementResult:
        """Compute frequency using zero-crossing detection."""
        # Remove DC offset
        samples = samples - np.mean(samples)

        # Find zero crossings (rising edge)
        zero_crossings = np.where(np.diff(np.signbit(samples)))[0]

        if len(zero_crossings) < 2:
            return MeasurementResult(
                measurement_type="frequency",
                value=None,
                unit="Hz",
                confidence=0.0,
                method="zero_crossing",
                details={"reason": "Insufficient zero crossings"},
                status="insufficient_edges",
            )

        # Calculate periods between crossings
        # Every two crossings is one period
        periods = np.diff(zero_crossings[::2]) / sample_rate

        if len(periods) == 0:
            return MeasurementResult(
                measurement_type="frequency",
                value=None,
                unit="Hz",
                confidence=0.0,
                method="zero_crossing",
            )

        # Average period
        avg_period = np.mean(periods)
        frequency = 1.0 / avg_period

        # Confidence based on period consistency
        if len(periods) > 1:
            std_dev = np.std(periods)
            confidence = max(0.0, 1.0 - (std_dev / avg_period))
        else:
            confidence = 0.5

        period_std = float(np.std(periods)) if len(periods) > 1 else 0.0
        return MeasurementResult(
            measurement_type="frequency",
            value=float(frequency),
            unit="Hz",
            confidence=float(confidence),
            method="zero_crossing",
            details={
                "periods_measured": len(periods),
                "period_std": period_std,
                "period_cv": period_std / float(avg_period) if avg_period else float("inf"),
            },
        )

    def _frequency_fft(
        self,
        samples: np.ndarray,
        sample_rate: float,
    ) -> MeasurementResult:
        """Compute frequency using FFT."""
        # Remove DC before windowing so offset does not leak into low-frequency bins.
        windowed = (samples - np.mean(samples)) * signal.windows.hann(len(samples))

        # Compute FFT
        n = len(samples)
        fft_result = fft(windowed)
        freqs = fftfreq(n, 1.0 / sample_rate)

        # Only look at positive frequencies
        positive_mask = freqs > 0
        fft_magnitude = np.abs(fft_result[positive_mask])
        positive_freqs = freqs[positive_mask]

        if len(fft_magnitude) == 0:
            return MeasurementResult(
                measurement_type="frequency",
                value=None,
                unit="Hz",
                confidence=0.0,
                method="fft",
            )

        # Find dominant frequency
        peak_idx = np.argmax(fft_magnitude)
        peak_freq = positive_freqs[peak_idx]

        # Confidence based on peak prominence
        peak_magnitude = fft_magnitude[peak_idx]
        mean_magnitude = np.mean(fft_magnitude)
        confidence = min(1.0, (peak_magnitude / mean_magnitude) / 10)

        return MeasurementResult(
            measurement_type="frequency",
            value=float(peak_freq),
            unit="Hz",
            confidence=float(confidence),
            method="fft",
            details={
                "peak_magnitude": float(peak_magnitude),
                "mean_magnitude": float(mean_magnitude),
            },
        )

    def period(
        self,
        samples: np.ndarray,
        sample_rate: float,
    ) -> MeasurementResult:
        """Compute signal period."""
        freq_result = self.frequency(samples, sample_rate)

        if freq_result.value is None or freq_result.confidence == 0:
            return MeasurementResult(
                measurement_type="period",
                value=None,
                unit="s",
                confidence=0.0,
                method=freq_result.method,
                details=freq_result.details,
                status=freq_result.status,
            )

        period = 1.0 / freq_result.value

        return MeasurementResult(
            measurement_type="period",
            value=float(period),
            unit="s",
            confidence=freq_result.confidence,
            method=freq_result.method,
        )

    def amplitude(self, samples: np.ndarray) -> MeasurementResult:
        """Compute peak-to-peak amplitude (Vpp)."""
        vmax = np.max(samples)
        vmin = np.min(samples)
        vpp = vmax - vmin

        return MeasurementResult(
            measurement_type="amplitude",
            value=float(vpp),
            unit="V",
            confidence=1.0,
            method="min_max",
            details={"vmax": float(vmax), "vmin": float(vmin)},
        )

    def vmax(self, samples: np.ndarray) -> MeasurementResult:
        """Compute maximum voltage."""
        return MeasurementResult(
            measurement_type="vmax",
            value=float(np.max(samples)),
            unit="V",
            confidence=1.0,
            method="direct",
        )

    def vmin(self, samples: np.ndarray) -> MeasurementResult:
        """Compute minimum voltage."""
        return MeasurementResult(
            measurement_type="vmin",
            value=float(np.min(samples)),
            unit="V",
            confidence=1.0,
            method="direct",
        )

    def vrms(self, samples: np.ndarray) -> MeasurementResult:
        """Compute RMS voltage."""
        rms = np.sqrt(np.mean(samples**2))
        return MeasurementResult(
            measurement_type="vrms",
            value=float(rms),
            unit="V",
            confidence=1.0,
            method="direct",
        )

    def vmean(self, samples: np.ndarray) -> MeasurementResult:
        """Compute mean voltage (DC level)."""
        return MeasurementResult(
            measurement_type="vmean",
            value=float(np.mean(samples)),
            unit="V",
            confidence=1.0,
            method="direct",
        )

    def duty_cycle(
        self,
        samples: np.ndarray,
        threshold: float | None = None,
    ) -> MeasurementResult:
        """Compute duty cycle.

        Args:
            samples: Waveform samples.
            threshold: Voltage threshold for high/low. None = midpoint.

        Returns:
            Duty cycle as percentage.
        """
        if threshold is None:
            threshold = (np.max(samples) + np.min(samples)) / 2

        high_samples = np.sum(samples > threshold)
        total_samples = len(samples)

        duty = (high_samples / total_samples) * 100

        return MeasurementResult(
            measurement_type="duty_cycle",
            value=float(duty),
            unit="%",
            confidence=1.0,
            method="threshold",
            details={"threshold": float(threshold)},
        )

    def rise_time(
        self,
        samples: np.ndarray,
        sample_rate: float,
        low_pct: float = 10,
        high_pct: float = 90,
    ) -> MeasurementResult:
        """Compute rise time (10-90% by default).

        Args:
            samples: Waveform samples.
            sample_rate: Sample rate in Hz.
            low_pct: Low threshold percentage.
            high_pct: High threshold percentage.

        Returns:
            Rise time in seconds.
        """
        vmin = np.min(samples)
        vmax = np.max(samples)
        vrange = vmax - vmin

        low_thresh = vmin + vrange * (low_pct / 100)
        high_thresh = vmin + vrange * (high_pct / 100)

        # Find rising edges
        rise_times = []

        in_rise = False
        rise_start = 0

        for i in range(1, len(samples)):
            previous = samples[i - 1]
            current = samples[i]
            if not in_rise and previous < low_thresh <= current:
                in_rise = True
                rise_start = i
            elif in_rise and current >= high_thresh:
                rise_time = (i - rise_start) / sample_rate
                rise_times.append(rise_time)
                in_rise = False

        if not rise_times:
            return MeasurementResult(
                measurement_type="rise_time",
                value=0.0,
                unit="s",
                confidence=0.0,
                method="threshold",
            )

        avg_rise_time = np.mean(rise_times)

        return MeasurementResult(
            measurement_type="rise_time",
            value=float(avg_rise_time),
            unit="s",
            confidence=1.0 if len(rise_times) > 1 else 0.5,
            method="threshold",
            details={
                "measurements": len(rise_times),
                "low_pct": low_pct,
                "high_pct": high_pct,
            },
        )

    def fall_time(
        self,
        samples: np.ndarray,
        sample_rate: float,
        low_pct: float = 10,
        high_pct: float = 90,
    ) -> MeasurementResult:
        """Compute fall time (90-10% by default)."""
        vmin = np.min(samples)
        vmax = np.max(samples)
        vrange = vmax - vmin

        low_thresh = vmin + vrange * (low_pct / 100)
        high_thresh = vmin + vrange * (high_pct / 100)

        # Find falling edges
        fall_times = []

        in_fall = False
        fall_start = 0

        for i in range(1, len(samples)):
            previous = samples[i - 1]
            current = samples[i]
            if not in_fall and previous > high_thresh >= current:
                in_fall = True
                fall_start = i
            elif in_fall and current <= low_thresh:
                fall_time = (i - fall_start) / sample_rate
                fall_times.append(fall_time)
                in_fall = False

        if not fall_times:
            return MeasurementResult(
                measurement_type="fall_time",
                value=0.0,
                unit="s",
                confidence=0.0,
                method="threshold",
            )

        avg_fall_time = np.mean(fall_times)

        return MeasurementResult(
            measurement_type="fall_time",
            value=float(avg_fall_time),
            unit="s",
            confidence=1.0 if len(fall_times) > 1 else 0.5,
            method="threshold",
            details={"measurements": len(fall_times)},
        )

    def measure(
        self,
        samples: np.ndarray,
        sample_rate: float,
        measurement_type: str,
    ) -> MeasurementResult:
        """Take a measurement of the specified type.

        Args:
            samples: Waveform samples.
            sample_rate: Sample rate in Hz.
            measurement_type: Measurement type name.

        Returns:
            Measurement result.
        """
        measurement_map = {
            "frequency": lambda: self.frequency(samples, sample_rate),
            "period": lambda: self.period(samples, sample_rate),
            "amplitude": lambda: self.amplitude(samples),
            "vpp": lambda: self.amplitude(samples),
            "vmax": lambda: self.vmax(samples),
            "vmin": lambda: self.vmin(samples),
            "vrms": lambda: self.vrms(samples),
            "vmean": lambda: self.vmean(samples),
            "duty_cycle": lambda: self.duty_cycle(samples),
            "rise_time": lambda: self.rise_time(samples, sample_rate),
            "fall_time": lambda: self.fall_time(samples, sample_rate),
        }

        func = measurement_map.get(measurement_type.lower())
        if func is None:
            raise ValueError(f"Unknown measurement type: {measurement_type}")

        return func()
