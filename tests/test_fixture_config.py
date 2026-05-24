"""Tests for fixture-layer configuration."""

from scopeloop.config import Config


def test_fixture_layer_config_parses():
    config = Config.model_validate(
        {
            "project": {"name": "test"},
            "hardware": {"mcu": "esp32"},
            "build": {"system": "esp-idf"},
            "fixtures": {
                "relay_card": {
                    "type": "relay-card",
                    "connection": {
                        "type": "carrier-gpio",
                        "controller": "rp2350",
                        "path": "J5",
                    },
                    "capabilities": [
                        {
                            "name": "power_button",
                            "kind": "button",
                            "channel": "RELAY1",
                            "signal": "Short DUT power-button pads",
                            "safe_state": "open",
                            "active_state": "closed",
                        },
                        {
                            "name": "battery_disconnect",
                            "kind": "relay",
                            "channel": "RELAY2",
                            "safe_state": "closed",
                            "active_state": "open",
                            "limits": {"max_voltage_v": 24, "max_current_a": 2},
                        },
                    ],
                },
                "qwiic_environment": {
                    "type": "qwiic-sensor-chain",
                    "connection": {
                        "type": "qwiic",
                        "controller": "rp2350",
                        "path": "QWIIC0",
                    },
                    "capabilities": [
                        {
                            "name": "ambient_temperature",
                            "kind": "temp-probe",
                            "direction": "input",
                            "unit": "degC",
                        }
                    ],
                },
            },
        }
    )

    relay_card = config.fixtures["relay_card"]
    assert relay_card.connection.controller == "rp2350"
    assert relay_card.capabilities[0].name == "power_button"
    assert relay_card.capabilities[0].direction == "output"
    assert relay_card.capabilities[1].limits["max_current_a"] == 2

    qwiic_environment = config.fixtures["qwiic_environment"]
    assert qwiic_environment.connection.type == "qwiic"
    assert qwiic_environment.capabilities[0].direction == "input"


def test_fixture_layer_defaults_to_empty():
    config = Config.model_validate(
        {
            "project": {"name": "test"},
            "hardware": {"mcu": "esp32"},
            "build": {"system": "esp-idf"},
        }
    )

    assert config.fixtures == {}


def test_user_buildable_module_manifest_parses():
    config = Config.model_validate(
        {
            "project": {"name": "test"},
            "hardware": {"mcu": "esp32"},
            "build": {"system": "esp-idf"},
            "modules": {
                "relay_card_m1": {
                    "slot": "M1",
                    "type": "relay-card",
                    "connection": {
                        "type": "module-bay",
                        "controller": "rp2350",
                        "path": "M1",
                    },
                    "manifest": {
                        "module_id": "scopeloop.relay-card.4ch",
                        "name": "4-Channel Relay Card",
                        "vendor": "ScopeLoop",
                        "hardware_revision": "A",
                        "slot_class": "control",
                        "electrical_interface": "usb2+gpio",
                        "open_hardware": True,
                        "source_url": "https://github.com/example/scopeloop-relay-card",
                        "license": "CERN-OHL-S-2.0",
                        "power_budget_w": 2.5,
                        "rails": [
                            {
                                "name": "5v",
                                "voltage": "5V",
                                "max_current_a": 0.3,
                                "purpose": "relay coils",
                            },
                            {
                                "name": "3v3",
                                "voltage": "3.3V",
                                "max_current_a": 0.05,
                                "purpose": "logic",
                            },
                        ],
                        "interfaces": [
                            {
                                "name": "usb2",
                                "type": "usb2",
                                "purpose": "module identification and control",
                            },
                            {
                                "name": "trigger_out",
                                "type": "trigger",
                                "direction": "output",
                                "optional": True,
                            },
                        ],
                        "capabilities": [
                            {
                                "name": "relay_1",
                                "kind": "relay",
                                "channel": "RELAY1",
                                "safe_state": "open",
                                "active_state": "closed",
                                "limits": {"max_voltage_v": 24},
                            }
                        ],
                    },
                }
            },
        }
    )

    module = config.modules["relay_card_m1"]
    assert module.slot == "M1"
    assert module.manifest.open_hardware is True
    assert module.manifest.slot_class == "control"
    assert module.manifest.rails[0].voltage == "5V"
    assert module.manifest.interfaces[1].type == "trigger"
    assert module.manifest.capabilities[0].safe_state == "open"
