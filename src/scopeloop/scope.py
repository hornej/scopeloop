"""Helpers for configured oscilloscope access."""

from __future__ import annotations

from scopeloop.config import Config, OscilloscopeConfig
from scopeloop.instruments.siglent_scope import SiglentSDS1000X


class ScopeConfigError(ValueError):
    """Raised when the oscilloscope configuration cannot be used."""


def create_scope_from_config(config: Config) -> SiglentSDS1000X:
    """Create a scope driver from the loaded ScopeLoop config."""
    oscilloscope = config.instruments.oscilloscope
    if oscilloscope is None:
        raise ScopeConfigError("No oscilloscope configured in scopeloop.yaml")

    address, port = _siglent_address_and_port(oscilloscope)
    return SiglentSDS1000X(address, port=port, expected_serial=oscilloscope.expected_serial)


def parse_si_value(value: str | float | int) -> float:
    """Parse an SI value string such as ``500mV`` or ``1ms``."""
    from scopeloop.scpi import number

    return number(str(value))


def _siglent_address_and_port(oscilloscope: OscilloscopeConfig) -> tuple[str, int]:
    if oscilloscope.type == "siglent-sds1000x":
        if not oscilloscope.address:
            raise ScopeConfigError("Siglent oscilloscope config requires address")
        return oscilloscope.address, 5025

    if oscilloscope.type == "sigrok":
        if oscilloscope.driver and oscilloscope.driver != "siglent-sds":
            raise ScopeConfigError(
                f"Unsupported sigrok oscilloscope driver for direct control: {oscilloscope.driver}"
            )
        if oscilloscope.connection:
            return _parse_tcp_raw_connection(oscilloscope.connection)
        if oscilloscope.address:
            return oscilloscope.address, 5025
        raise ScopeConfigError("Sigrok oscilloscope config requires connection or address")

    raise ScopeConfigError(f"Unsupported oscilloscope type: {oscilloscope.type}")


def _parse_tcp_raw_connection(connection: str) -> tuple[str, int]:
    parts = connection.split("/")
    if len(parts) != 3 or parts[0] != "tcp-raw":
        raise ScopeConfigError(f"Unsupported scope connection string: {connection}")

    try:
        port = int(parts[2])
    except ValueError as exc:
        raise ScopeConfigError(f"Invalid scope connection port: {parts[2]}") from exc

    return parts[1], port
