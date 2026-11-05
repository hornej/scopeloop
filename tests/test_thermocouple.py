import json

import pytest
from typer.testing import CliRunner

from scopeloop.cli import app
from scopeloop.thermocouple import TemperatureGuard, ThermocoupleConnection, strict_json
from scopeloop.thermocouple_firmware import Thermocouple


class Clock:
    def __init__(self):
        self.value = 1.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class Sensor:
    def __init__(self):
        self.regs = [0] * 16
        self.temperature_thresholds = (-20, 85)
        self.pending = 0
        self.stuck = False
        self.tc = 24.0
        self.cj = 23.0
        self.fault = {}
        self.io_error = False
        self.post_conversion_corruption = False

    def _read_register(self, address, length):
        assert length == 1  # Real driver has a small shared transfer buffer.
        if self.io_error:
            raise OSError("SPI unavailable")
        return bytes([self.regs[address]])

    def _write_u8(self, address, value):
        self.regs[address] = value

    def initiate_one_shot_measurement(self):
        self.pending = 2

    @property
    def oneshot_pending(self):
        if self.stuck:
            return True
        if self.pending:
            self.pending -= 1
            return True
        if self.post_conversion_corruption:
            self.regs[1] = 0
        return False

    def unpack_temperature(self):
        return self.tc

    def unpack_reference_temperature(self):
        return self.cj


def fixture(high=110):
    sensor = Sensor()
    engine = Thermocouple(lambda: sensor, "test-device", "boot-a", -20, high, clock=Clock())
    return sensor, engine


def recovery(engine, **overrides):
    return engine.command(
        {
            "thermocouple": "recover",
            "id": "recovery-1",
            "fault_id": engine.fault_id,
            "boot": engine.boot,
            "dut_idle": True,
            **overrides,
        }
    )


def test_config_drift_records_actual_expected_and_never_self_clears():
    sensor, engine = fixture()
    sensor.regs[0] = 0
    bad = engine.sample()
    assert bad["valid"] is False
    assert bad["fault_detail"]["detail"] == {"register": 0, "expected": 16, "actual": 0}
    assert bad["fault_detail"]["registers"]["0"] == 0
    sensor.regs[0] = 16
    for _ in range(4):
        assert engine.sample()["valid"] is False
    assert engine.fault_id == "boot-a:1"


@pytest.mark.parametrize(
    "overrides",
    [
        {"dut_idle": False},
        {"dut_idle": "true"},
        {"boot": "old"},
        {"fault_id": "old"},
    ],
)
def test_recovery_rejects_missing_idle_or_stale_token(overrides):
    sensor, engine = fixture()
    sensor.regs[0] = 0
    engine.sample()
    assert recovery(engine, **overrides)["ok"] is False
    assert sensor.regs[0] == 0


def test_recovery_requires_three_clean_conversions_and_retains_original_evidence():
    sensor, engine = fixture()
    sensor.regs[0] = 0
    original = engine.sample()["fault_detail"]
    assert recovery(engine)["ok"]
    assert recovery(engine)["ok"] is False
    samples = [engine.sample() for _ in range(3)]
    assert [x["valid"] for x in samples] == [False, False, True]
    assert samples[-1]["recovery"]["state"] == "verified"
    assert samples[-1]["fault_detail"] == original
    assert engine.fault_id is None
    assert recovery(engine)["ok"] is False


@pytest.mark.parametrize("fault", ["hot", "cold", "open", "timeout", "nan", "io", "drift"])
def test_faults_remain_fail_closed_during_recovery(fault):
    sensor, engine = fixture()
    sensor.regs[0] = 0
    engine.sample()
    assert recovery(engine)["ok"]
    if fault == "hot":
        sensor.tc = 110
    elif fault == "cold":
        sensor.tc = -20
    elif fault == "open":
        sensor.regs[15] = 1
    elif fault == "timeout":
        sensor.stuck = True
    elif fault == "nan":
        sensor.tc = float("nan")
    elif fault == "io":
        sensor.io_error = True
    else:
        sensor.post_conversion_corruption = True
    row = engine.sample()
    assert row["valid"] is False and row["latched"] is True
    assert row["recovery"]["state"] == "failed"
    json.dumps(row, allow_nan=False)


def test_initialize_failure_reports_invalid_samples_instead_of_crashing():
    def failed():
        raise OSError("sensor absent")

    engine = Thermocouple(failed, "test-device", "boot", -20, 110)
    assert engine.sample()["valid"] is False
    status = engine.command({"thermocouple": "status", "id": "diag"})
    assert status["ok"] and status["latched"]
    assert "error" in status["registers"]["0"]


def test_late_fault_flags_during_recovery_keep_logger_latched():
    sensor, engine = fixture()
    sensor.regs[0] = 0
    engine.sample()
    assert recovery(engine)["ok"]
    for _ in range(2):
        engine.sample()
    # The driver's flags come from a second register read after the raw mask.
    sensor.fault = {"open_tc": True}
    row = engine.sample()
    assert row["fault_mask"] == 0
    assert row["latched"] and not row["valid"]
    assert row["recovery"]["state"] == "failed"


def test_status_is_read_only_and_includes_request_identity():
    sensor, engine = fixture()
    sensor.regs[0] = 0
    before = sensor.regs[:]
    status = engine.command({"thermocouple": "status", "id": "diag"})
    assert status["id"] == "diag" and status["registers"]["0"] == 0
    assert before == sensor.regs


@pytest.mark.parametrize("low,high", [(-20, True), (-20, float("nan")), (100, 90), (-20, 85.1)])
def test_bad_configuration_rejected(low, high):
    with pytest.raises(ValueError):
        Thermocouple(Sensor, "test", "boot", low, high)


def test_host_accepts_legacy_shape_and_rejects_real_cr0_failure():
    sensor, engine = fixture()
    row = engine.sample()
    guard = TemperatureGuard("test-device", 110)
    guard.accept(row, 10)
    guard.check_fresh(12.9)
    with pytest.raises(ValueError, match="stale"):
        guard.check_fresh(13.1)
    real_fault_shape = {
        "device": "test-device",
        "uptime_s": 82198.64,
        "protocol": row["protocol"],
        "boot": "boot-a",
        "high_limit_c": 85,
        "kind": "temperature",
        "type": "K",
        "valid": False,
        "error": "AssertionError('CR0 mismatch',)",
        "seq": 164395,
        "latched": True,
    }
    with pytest.raises(ValueError, match="CR0"):
        guard.accept(real_fault_shape, 11)


@pytest.mark.parametrize(
    "change",
    [
        {"boot": "new"},
        {"seq": 4},
        {"seq": 1},
        {"device": "wrong"},
        {"uptime_s": 0},
        {"uptime_s": 10},
        {"high_limit_c": 85},
        {"tc_c": 110},
        {"tc_c": float("nan")},
        {"fault_mask": 1},
        {"faults": {"open_tc": True}},
        {"valid": False},
        {"latched": True},
    ],
)
def test_host_rejects_identity_timing_fault_or_cutoff_change(change):
    sensor, engine = fixture()
    guard = TemperatureGuard("test-device", 110)
    guard.accept(engine.sample(), 1)
    row = engine.sample()
    row.update(change)
    with pytest.raises(ValueError):
        guard.accept(row, 2)


def test_delayed_delivery_cannot_renew_freshness():
    _, engine = fixture()
    guard = TemperatureGuard("test-device", 110)
    first = guard.accept(engine.sample(), 10)
    with pytest.raises(ValueError, match="stale"):
        guard.accept(engine.sample(), 13.1)
    assert guard.received_at == 10 and guard.last is first


def test_parser_rejects_nonfinite_json_and_replays_split_banner():
    with pytest.raises(ValueError):
        strict_json('{"tc_c": NaN}')
    sensor, engine = fixture()
    connection = ThermocoupleConnection("unused", "test-device", "unused")
    row = engine.sample()
    connection.pending = b"Code done running.\r\n\x1b[0G" + json.dumps(row).encode() + b"\r\n"
    connection.event = lambda *args: None
    assert connection.read(__import__("time").monotonic() + 1) == row


def test_recovery_without_idle_never_opens_or_writes():
    connection = ThermocoupleConnection("unused", "test-device", "unused")
    with pytest.raises(ValueError, match="idle"):
        connection.recover("boot", "fault", 110, False)


def test_cli_exposes_commands_and_rejects_unacknowledged_recovery():
    runner = CliRunner()
    result = runner.invoke(app, ["thermocouple", "--help"])
    assert result.exit_code == 0
    assert all(word in result.stdout for word in ("capture", "diagnose", "recover"))
    result = runner.invoke(
        app,
        [
            "thermocouple",
            "recover",
            "unused",
            "test",
            "unused",
            "boot",
            "fault",
            "110",
        ],
    )
    assert result.exit_code != 0 and "idle" in result.output


def test_cleanup_closes_serial_and_releases_lease_even_when_event_write_fails(tmp_path):
    from types import SimpleNamespace

    actions = []
    connection = ThermocoupleConnection("test", "test", tmp_path / "bundle")
    connection.serial = SimpleNamespace(close=lambda: actions.append("serial"))
    connection.events = SimpleNamespace(close=lambda: actions.append("events"))
    connection.lease = SimpleNamespace(release=lambda: actions.append("lease"))

    def failed(*args):
        raise OSError("disk full")

    connection.event = failed
    with pytest.raises(OSError, match="disk full"):
        connection.__exit__(None, None, None)
    assert actions == ["serial", "events", "lease"]


def test_circuitpython_transport_requires_dtr_for_telemetry(monkeypatch, tmp_path):
    import time

    from scopeloop import thermocouple

    _, engine = fixture()
    sample = engine.sample()

    class Cdc:
        def __init__(self, **kwargs):
            self.dtr = False
            self.rts = True
            self.remaining = json.dumps(sample).encode() + b"\n"
            self.closed = False

        def open(self):
            pass

        def reset_input_buffer(self):
            pass

        def read(self, count):
            if not self.dtr:
                return b""
            data, self.remaining = self.remaining[:count], self.remaining[count:]
            return data

        def close(self):
            self.closed = True

    monkeypatch.setenv("SCOPELOOP_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setattr(thermocouple.serial, "Serial", Cdc)
    with ThermocoupleConnection("test-cdc", "test-device", tmp_path / "capture") as connection:
        assert connection.read(time.monotonic() + 0.2) == sample
        assert connection.serial.rts is False
    assert connection.serial.closed


def test_request_resynchronizes_console_and_ignores_uncorrelated_parser_error():
    from types import SimpleNamespace

    connection = ThermocoupleConnection("unused", "test-device", "unused")
    connection.event = lambda *args: None

    def write(data):
        # A previous client's complete command might be missing just its newline.
        # Even a legacy receiver must reject it before parsing our status request.
        old = b'{"thermocouple":"recover","dut_idle":true}'
        abandoned, framed = (old + data).split(b"\n", 1)
        with pytest.raises(ValueError):
            json.loads(abandoned)
        request = json.loads(framed)
        malformed = {"kind": "thermocouple", "ok": False, "error": "syntax error in JSON"}
        reply = {
            "kind": "thermocouple",
            "protocol": "scopeloop-max31856-v1",
            "device": "test-device",
            "boot": "boot-a",
            "ok": True,
            "id": request["id"],
        }
        connection.pending = (json.dumps(malformed) + "\n" + json.dumps(reply) + "\n").encode()
        return len(data)

    connection.serial = SimpleNamespace(write=write)
    assert connection.request("status")["ok"]


def test_correlated_reply_with_wrong_identity_is_still_rejected():
    import time

    connection = ThermocoupleConnection("unused", "test-device", "unused")
    connection.event = lambda *args: None
    connection.pending = (
        b'{"kind":"thermocouple","ok":true,"id":"x","device":"wrong",'
        b'"protocol":"scopeloop-max31856-v1"}\n'
    )
    with pytest.raises(ValueError, match="identity"):
        connection.read(time.monotonic() + 0.1)
