"""Tests for the measurement engine."""

import numpy as np
import pytest

from scopeloop.measurements import MeasurementEngine


@pytest.fixture
def engine():
    return MeasurementEngine()


@pytest.fixture
def sine_wave():
    """Generate a 1kHz sine wave at 1V amplitude."""
    sample_rate = 100000  # 100 kHz
    duration = 0.01  # 10ms
    frequency = 1000  # 1 kHz
    amplitude = 1.0

    t = np.linspace(0, duration, int(sample_rate * duration))
    samples = amplitude * np.sin(2 * np.pi * frequency * t)
    return samples, sample_rate


@pytest.fixture
def square_wave():
    """Generate a 1kHz square wave with 50% duty cycle."""
    sample_rate = 100000  # 100 kHz
    duration = 0.01  # 10ms
    frequency = 1000  # 1 kHz

    t = np.linspace(0, duration, int(sample_rate * duration))
    samples = np.sign(np.sin(2 * np.pi * frequency * t))
    return samples, sample_rate


class TestFrequencyMeasurement:
    def test_sine_frequency_zero_crossing(self, engine, sine_wave):
        samples, sample_rate = sine_wave
        result = engine.frequency(samples, sample_rate, method="zero_crossing")

        assert result.measurement_type == "frequency"
        assert result.unit == "Hz"
        assert 990 < result.value < 1010  # Within 1% of 1kHz
        assert result.confidence > 0.8

    def test_sine_frequency_fft(self, engine, sine_wave):
        samples, sample_rate = sine_wave
        result = engine.frequency(samples, sample_rate, method="fft")

        assert result.measurement_type == "frequency"
        assert 950 < result.value < 1050  # Within 5% of 1kHz

    def test_square_frequency(self, engine, square_wave):
        samples, sample_rate = square_wave
        result = engine.frequency(samples, sample_rate)

        assert 990 < result.value < 1010

    def test_midrail_noise_is_not_a_valid_logic_frequency(self, engine):
        sample_rate = 100_000
        time = np.arange(2000) / sample_rate
        samples = 1.65 + 0.1 * np.sin(2 * np.pi * 2500 * time)

        result = engine.frequency(
            samples,
            sample_rate,
            signal_type="clock",
            logic_low_max_v=0.825,
            logic_high_min_v=2.475,
        )

        assert result.value is None
        assert result.status == "indeterminate_logic_levels"
        assert "valid low and high" in result.details["reason"]

    def test_valid_3v3_clock_frequency(self, engine):
        sample_rate = 100_000
        time = np.arange(2000) / sample_rate
        samples = np.where(np.sin(2 * np.pi * 1000 * time) >= 0, 3.3, 0.0)

        result = engine.frequency(
            samples,
            sample_rate,
            signal_type="clock",
            logic_low_max_v=0.825,
            logic_high_min_v=2.475,
        )

        assert result.status == "valid"
        assert 990 < result.value < 1010

    def test_insufficient_amplitude_is_explicit(self, engine):
        sample_rate = 100_000
        time = np.arange(2000) / sample_rate
        samples = 1.0 + 0.005 * np.sin(2 * np.pi * 1000 * time)

        result = engine.frequency(samples, sample_rate)

        assert result.value is None
        assert result.status == "insufficient_amplitude"


class TestAmplitudeMeasurements:
    def test_amplitude(self, engine, sine_wave):
        samples, sample_rate = sine_wave
        result = engine.amplitude(samples)

        assert result.measurement_type == "amplitude"
        assert result.unit == "V"
        assert 1.9 < result.value < 2.1  # Vpp should be ~2V for 1V amplitude sine

    def test_vmax(self, engine, sine_wave):
        samples, sample_rate = sine_wave
        result = engine.vmax(samples)

        assert 0.95 < result.value < 1.05  # Should be ~1V

    def test_vmin(self, engine, sine_wave):
        samples, sample_rate = sine_wave
        result = engine.vmin(samples)

        assert -1.05 < result.value < -0.95  # Should be ~-1V

    def test_vrms(self, engine, sine_wave):
        samples, sample_rate = sine_wave
        result = engine.vrms(samples)

        # RMS of sine wave is amplitude / sqrt(2)
        expected_rms = 1.0 / np.sqrt(2)
        assert abs(result.value - expected_rms) < 0.05


class TestDutyCycle:
    def test_square_wave_50_percent(self, engine, square_wave):
        samples, sample_rate = square_wave
        result = engine.duty_cycle(samples)

        assert result.measurement_type == "duty_cycle"
        assert result.unit == "%"
        assert 48 < result.value < 52  # Should be ~50%

    def test_custom_duty_cycle(self, engine):
        # Create a 25% duty cycle signal
        samples = np.zeros(1000)
        samples[:250] = 1.0  # 25% high

        result = engine.duty_cycle(samples, threshold=0.5)
        assert 24 < result.value < 26


class TestTimingMeasurements:
    def test_period(self, engine, sine_wave):
        samples, sample_rate = sine_wave
        result = engine.period(samples, sample_rate)

        assert result.measurement_type == "period"
        assert result.unit == "s"
        # 1kHz = 1ms period
        assert 0.0009 < result.value < 0.0011

    def test_rise_time(self, engine):
        # Create a ramp signal
        sample_rate = 100000
        samples = np.concatenate([
            np.zeros(100),
            np.linspace(0, 1, 50),  # Rise
            np.ones(100),
            np.linspace(1, 0, 50),  # Fall
            np.zeros(100),
        ])

        result = engine.rise_time(samples, sample_rate)
        assert result.measurement_type == "rise_time"
        # Rise is 50 samples, but 10-90% is ~40 samples
        # At 100kHz, that's 400us
        assert 0.0003 < result.value < 0.0005


class TestMeasurementResult:
    def test_to_dict(self, engine, sine_wave):
        samples, sample_rate = sine_wave
        result = engine.frequency(samples, sample_rate)

        d = result.to_dict()
        assert "type" in d
        assert "value" in d
        assert "unit" in d
        assert "confidence" in d
        assert "method" in d


class TestGenericMeasure:
    def test_measure_frequency(self, engine, sine_wave):
        samples, sample_rate = sine_wave
        result = engine.measure(samples, sample_rate, "frequency")
        assert result.measurement_type == "frequency"

    def test_measure_vpp(self, engine, sine_wave):
        samples, sample_rate = sine_wave
        result = engine.measure(samples, sample_rate, "vpp")
        assert result.measurement_type == "amplitude"

    def test_measure_unknown(self, engine, sine_wave):
        samples, sample_rate = sine_wave
        with pytest.raises(ValueError, match="Unknown measurement"):
            engine.measure(samples, sample_rate, "unknown_type")
