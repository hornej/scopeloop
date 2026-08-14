"""Instrument drivers for oscilloscopes, logic analyzers, and serial monitors."""

from scopeloop.instruments.base import Instrument, InstrumentError, InstrumentInfo

# Sigrok-based drivers (requires pysigrok)
try:
    from scopeloop.instruments.sigrok import (
        SigrokDevice,
        SigrokLogicAnalyzer,
        SigrokOscilloscope,
        list_sigrok_devices,
        list_sigrok_drivers,
    )

    SIGROK_AVAILABLE = True
except ImportError:
    SIGROK_AVAILABLE = False
    SigrokDevice = None  # type: ignore
    SigrokOscilloscope = None  # type: ignore
    SigrokLogicAnalyzer = None  # type: ignore
    list_sigrok_devices = None  # type: ignore
    list_sigrok_drivers = None  # type: ignore

# Saleae Logic 2 driver (requires logic2-automation)
try:
    from scopeloop.instruments.saleae import SALEAE_AVAILABLE as SALEAE_DRIVER_AVAILABLE
    from scopeloop.instruments.saleae import (
        SaleaeLogicAnalyzer,
        i2c_analyzer_settings,
        spi_analyzer_settings,
        uart_analyzer_settings,
    )

    SALEAE_AVAILABLE = SALEAE_DRIVER_AVAILABLE
except ImportError:
    SALEAE_AVAILABLE = False
    SaleaeLogicAnalyzer = None  # type: ignore
    spi_analyzer_settings = None  # type: ignore
    i2c_analyzer_settings = None  # type: ignore
    uart_analyzer_settings = None  # type: ignore

# Serial monitor
from scopeloop.instruments.serial_monitor import SerialMonitor, list_serial_ports

# Legacy Siglent driver (direct SCPI)
from scopeloop.instruments.siglent_scope import SiglentSDS1000X

__all__ = [
    # Base classes
    "Instrument",
    "InstrumentError",
    "InstrumentInfo",
    # Availability flags
    "SIGROK_AVAILABLE",
    "SALEAE_AVAILABLE",
    # Sigrok drivers
    "SigrokDevice",
    "SigrokOscilloscope",
    "SigrokLogicAnalyzer",
    "list_sigrok_devices",
    "list_sigrok_drivers",
    # Saleae driver
    "SaleaeLogicAnalyzer",
    "spi_analyzer_settings",
    "i2c_analyzer_settings",
    "uart_analyzer_settings",
    # Legacy Siglent
    "SiglentSDS1000X",
    # Serial monitor
    "SerialMonitor",
    "list_serial_ports",
]
