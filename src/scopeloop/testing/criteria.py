"""Test criteria definitions for hardware validation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class CriterionType(Enum):
    """Type of test criterion."""

    MEASUREMENT = "measurement"
    PROTOCOL = "protocol"
    EXPRESSION = "expression"


@dataclass
class Criterion:
    """A single test criterion."""

    name: str
    source: str  # e.g., "oscilloscope.CH1", "logic_analyzer"
    criterion_type: CriterionType = CriterionType.MEASUREMENT

    # For measurement criteria
    measurement: str | None = None  # e.g., "frequency", "amplitude"
    expected: float | None = None
    unit: str | None = None
    tolerance_percent: float | None = None
    tolerance_absolute: float | None = None

    # For protocol criteria
    protocol: str | None = None  # e.g., "SPI", "I2C"

    # For expression criteria
    verify: str | None = None  # Expression to evaluate

    def check(self, actual_value: float) -> bool:
        """Check if the actual value meets the criterion.

        Args:
            actual_value: The measured value.

        Returns:
            True if criterion is met.
        """
        if self.expected is None:
            return True

        if self.tolerance_percent is not None:
            tolerance = self.expected * (self.tolerance_percent / 100.0)
        elif self.tolerance_absolute is not None:
            tolerance = self.tolerance_absolute
        else:
            tolerance = 0

        return abs(actual_value - self.expected) <= tolerance


@dataclass
class TestCriteria:
    """Collection of criteria for a test."""

    name: str
    description: str | None = None
    setup_instructions: list[str] | None = None
    criteria: list[Criterion] | None = None

    def __post_init__(self) -> None:
        if self.setup_instructions is None:
            self.setup_instructions = []
        if self.criteria is None:
            self.criteria = []
