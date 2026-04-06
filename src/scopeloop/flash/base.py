"""Base classes for flash/programming integrations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class FlashError(Exception):
    """Error during flash process."""

    pass


class FlashStatus(Enum):
    """Flash status."""

    SUCCESS = "success"
    FAILURE = "failure"
    VERIFICATION_FAILED = "verification_failed"
    TIMEOUT = "timeout"


@dataclass
class FlashResult:
    """Result of a flash operation."""

    status: FlashStatus
    output: str = ""
    duration_seconds: float = 0.0
    bytes_written: int = 0


class Flasher(ABC):
    """Abstract base class for firmware flashers."""

    def __init__(self, port: str, baud: int = 460800):
        self.port = port
        self.baud = baud

    @abstractmethod
    async def flash(self, firmware_path: Path, verify: bool = True) -> FlashResult:
        """Flash firmware to the device.

        Args:
            firmware_path: Path to firmware binary.
            verify: Whether to verify after flashing.

        Returns:
            Flash result.
        """
        pass

    @abstractmethod
    async def verify(self, firmware_path: Path) -> bool:
        """Verify firmware matches what's on the device.

        Args:
            firmware_path: Path to firmware binary.

        Returns:
            True if verification passed.
        """
        pass

    @abstractmethod
    async def reset(self) -> None:
        """Reset the device."""
        pass
