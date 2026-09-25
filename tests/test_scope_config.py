"""Tests for configured oscilloscope helpers."""

from scopeloop.config import Config
from scopeloop.scope import ScopeConfigError, create_scope_from_config, parse_si_value


def test_create_scope_from_direct_siglent_config():
    config = Config.model_validate(
        {
            "project": {"name": "test"},
            "hardware": {"mcu": "esp32"},
            "build": {"system": "esp-idf"},
            "instruments": {
                "oscilloscope": {
                    "type": "siglent-sds1000x",
                    "address": "192.0.2.100",
                }
            },
        }
    )

    scope = create_scope_from_config(config)

    assert scope.address == "192.0.2.100"
    assert scope.port == 5025


def test_create_scope_from_sigrok_tcp_raw_config():
    config = Config.model_validate(
        {
            "project": {"name": "test"},
            "hardware": {"mcu": "esp32"},
            "build": {"system": "esp-idf"},
            "instruments": {
                "oscilloscope": {
                    "type": "sigrok",
                    "driver": "siglent-sds",
                    "connection": "tcp-raw/192.0.2.100/5025",
                }
            },
        }
    )

    scope = create_scope_from_config(config)

    assert scope.address == "192.0.2.100"
    assert scope.port == 5025


def test_create_scope_requires_oscilloscope_config():
    config = Config.model_validate(
        {
            "project": {"name": "test"},
            "hardware": {"mcu": "esp32"},
            "build": {"system": "esp-idf"},
        }
    )

    try:
        create_scope_from_config(config)
    except ScopeConfigError as e:
        assert "No oscilloscope configured" in str(e)
    else:
        raise AssertionError("Expected ScopeConfigError")


def test_parse_si_value():
    assert parse_si_value("1V") == 1.0
    assert parse_si_value("500mV") == 0.5
    assert parse_si_value("1ms") == 0.001
    assert abs(parse_si_value("100us") - 0.0001) < 1e-12
    assert parse_si_value(2) == 2.0
