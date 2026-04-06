"""Base classes for instrument drivers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


class InstrumentError(Exception):
    """Error related to instrument operations."""

    pass


@dataclass
class InstrumentInfo:
    """Information about an instrument."""

    instrument_type: str
    model: str | None = None
    serial: str | None = None
    firmware_version: str | None = None
    address: str | None = None


class Instrument(ABC):
    """Abstract base class for all instruments."""

    @abstractmethod
    async def connect(self) -> None:
        """Connect to the instrument."""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the instrument."""
        pass

    @abstractmethod
    async def get_info(self) -> InstrumentInfo:
        """Get instrument information."""
        pass

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Check if connected to the instrument."""
        pass

    async def __aenter__(self) -> Instrument:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.disconnect()
