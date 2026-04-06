"""Safety Guardrails - Centralized safety enforcement.

This module enforces safety policies to prevent runaway operations:
- Boot loop detection: Pause if N flashes without BOOT_OK marker
- Build runaway: Pause if build fails N times consecutively
- Flash verification: Ensure flash succeeded before proceeding
- User notification: Alert user when safety circuit trips

These guardrails are enforced centrally, not delegated to the agent.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Coroutine

logger = logging.getLogger(__name__)


class GuardrailState(Enum):
    """State of a guardrail."""

    OK = "ok"
    WARNING = "warning"
    BLOCKED = "blocked"


class GuardrailType(Enum):
    """Type of guardrail."""

    BOOT_LOOP = "boot_loop"
    BUILD_FAILURE = "build_failure"
    FLASH_FAILURE = "flash_failure"
    FLASH_RATE = "flash_rate"


@dataclass
class GuardrailEvent:
    """An event tracked by guardrails."""

    timestamp: float
    event_type: str
    success: bool
    details: dict = field(default_factory=dict)


@dataclass
class GuardrailStatus:
    """Status of a guardrail."""

    guardrail_type: GuardrailType
    state: GuardrailState
    message: str
    recovery_options: list[str] = field(default_factory=list)
    blocked_at: float | None = None


@dataclass
class SafetyConfig:
    """Configuration for safety guardrails."""

    # Boot loop detection
    max_flash_attempts: int = 5
    flash_window_seconds: float = 60.0
    boot_success_marker: str = "BOOT_OK"
    boot_timeout_seconds: float = 10.0

    # Build failure detection
    max_consecutive_build_failures: int = 5

    # Flash failure detection
    max_consecutive_flash_failures: int = 3

    # Rate limiting
    min_seconds_between_flashes: float = 2.0


class CircuitBreaker:
    """Circuit breaker for a specific operation type.

    Tracks success/failure history and trips when threshold is exceeded.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int,
        window_seconds: float | None = None,
        reset_on_success: bool = True,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.window_seconds = window_seconds
        self.reset_on_success = reset_on_success

        self._events: deque[GuardrailEvent] = deque(maxlen=100)
        self._consecutive_failures = 0
        self._state = GuardrailState.OK
        self._blocked_at: float | None = None
        self._block_reason: str = ""

    @property
    def state(self) -> GuardrailState:
        return self._state

    @property
    def is_blocked(self) -> bool:
        return self._state == GuardrailState.BLOCKED

    def record_success(self, details: dict | None = None) -> None:
        """Record a successful operation."""
        self._events.append(
            GuardrailEvent(
                timestamp=time.time(),
                event_type=self.name,
                success=True,
                details=details or {},
            )
        )

        if self.reset_on_success:
            self._consecutive_failures = 0
            if self._state == GuardrailState.WARNING:
                self._state = GuardrailState.OK

    def record_failure(self, details: dict | None = None) -> GuardrailState:
        """Record a failed operation.

        Returns:
            Current state after recording failure.
        """
        self._events.append(
            GuardrailEvent(
                timestamp=time.time(),
                event_type=self.name,
                success=False,
                details=details or {},
            )
        )
        self._consecutive_failures += 1

        # Check window-based threshold
        if self.window_seconds is not None:
            failures_in_window = self._count_failures_in_window()
            if failures_in_window >= self.failure_threshold:
                self._trip(
                    f"{failures_in_window} failures in {self.window_seconds}s window"
                )
                return self._state

        # Check consecutive threshold
        if self._consecutive_failures >= self.failure_threshold:
            self._trip(f"{self._consecutive_failures} consecutive failures")
            return self._state

        # Warn when approaching threshold
        if self._consecutive_failures >= self.failure_threshold - 1:
            self._state = GuardrailState.WARNING

        return self._state

    def _count_failures_in_window(self) -> int:
        """Count failures within the time window."""
        if self.window_seconds is None:
            return 0

        cutoff = time.time() - self.window_seconds
        return sum(
            1
            for event in self._events
            if not event.success and event.timestamp >= cutoff
        )

    def _trip(self, reason: str) -> None:
        """Trip the circuit breaker."""
        self._state = GuardrailState.BLOCKED
        self._blocked_at = time.time()
        self._block_reason = reason
        logger.warning(f"Circuit breaker {self.name} tripped: {reason}")

    def reset(self) -> None:
        """Manually reset the circuit breaker."""
        self._state = GuardrailState.OK
        self._consecutive_failures = 0
        self._blocked_at = None
        self._block_reason = ""
        logger.info(f"Circuit breaker {self.name} reset")

    def get_status(self) -> GuardrailStatus:
        """Get current status of this circuit breaker."""
        if self._state == GuardrailState.BLOCKED:
            message = f"Blocked: {self._block_reason}"
            recovery = [
                "Check device connections",
                "Review recent changes",
                "Run 'scopeloop safety reset' to unblock",
            ]
        elif self._state == GuardrailState.WARNING:
            message = f"Warning: {self._consecutive_failures} failures (threshold: {self.failure_threshold})"
            recovery = []
        else:
            message = "OK"
            recovery = []

        return GuardrailStatus(
            guardrail_type=GuardrailType.BUILD_FAILURE,  # Will be overridden
            state=self._state,
            message=message,
            recovery_options=recovery,
            blocked_at=self._blocked_at,
        )


class SafetyGuardrails:
    """Centralized safety enforcement for ScopeLoop.

    Usage:
        safety = SafetyGuardrails(SafetyConfig())

        # Check before flash
        allowed, message = await safety.check_flash_allowed()
        if not allowed:
            print(f"Flash blocked: {message}")
            return

        # Record flash attempt
        safety.record_flash_attempt()

        # After boot
        if boot_succeeded:
            safety.record_boot_success()
        else:
            safety.record_boot_failure()

        # Check all guardrails
        statuses = safety.get_all_statuses()
    """

    def __init__(self, config: SafetyConfig | None = None):
        self.config = config or SafetyConfig()

        # Initialize circuit breakers
        self._boot_loop_breaker = CircuitBreaker(
            name="boot_loop",
            failure_threshold=self.config.max_flash_attempts,
            window_seconds=self.config.flash_window_seconds,
            reset_on_success=True,
        )

        self._build_breaker = CircuitBreaker(
            name="build",
            failure_threshold=self.config.max_consecutive_build_failures,
            reset_on_success=True,
        )

        self._flash_breaker = CircuitBreaker(
            name="flash",
            failure_threshold=self.config.max_consecutive_flash_failures,
            reset_on_success=True,
        )

        # Track last flash time for rate limiting
        self._last_flash_time: float = 0

        # Notification callbacks
        self._notification_callbacks: list[
            Callable[[GuardrailType, GuardrailStatus], Coroutine]
        ] = []

    async def check_flash_allowed(self) -> tuple[bool, str]:
        """Check if flash operation is allowed.

        Returns:
            Tuple of (allowed, reason).
        """
        # Check boot loop breaker
        if self._boot_loop_breaker.is_blocked:
            status = self._boot_loop_breaker.get_status()
            return False, f"Boot loop detected: {status.message}"

        # Check flash breaker
        if self._flash_breaker.is_blocked:
            status = self._flash_breaker.get_status()
            return False, f"Flash failures: {status.message}"

        # Check rate limiting
        elapsed = time.time() - self._last_flash_time
        if elapsed < self.config.min_seconds_between_flashes:
            wait_time = self.config.min_seconds_between_flashes - elapsed
            return False, f"Rate limited: wait {wait_time:.1f}s"

        return True, "OK"

    async def check_build_allowed(self) -> tuple[bool, str]:
        """Check if build operation is allowed.

        Returns:
            Tuple of (allowed, reason).
        """
        if self._build_breaker.is_blocked:
            status = self._build_breaker.get_status()
            return False, f"Build failures: {status.message}"

        return True, "OK"

    def record_flash_attempt(self) -> None:
        """Record that a flash was attempted."""
        self._last_flash_time = time.time()

    def record_flash_success(self) -> None:
        """Record a successful flash operation."""
        self._flash_breaker.record_success()
        logger.debug("Flash success recorded")

    def record_flash_failure(self, details: dict | None = None) -> GuardrailState:
        """Record a failed flash operation.

        Returns:
            State after recording failure.
        """
        state = self._flash_breaker.record_failure(details)
        if state == GuardrailState.BLOCKED:
            asyncio.create_task(
                self._notify(GuardrailType.FLASH_FAILURE, self._flash_breaker.get_status())
            )
        return state

    def record_boot_success(self) -> None:
        """Record that device booted successfully."""
        self._boot_loop_breaker.record_success()
        logger.debug("Boot success recorded")

    def record_boot_failure(self, details: dict | None = None) -> GuardrailState:
        """Record that device failed to boot.

        Returns:
            State after recording failure.
        """
        state = self._boot_loop_breaker.record_failure(details)
        if state == GuardrailState.BLOCKED:
            asyncio.create_task(
                self._notify(GuardrailType.BOOT_LOOP, self._boot_loop_breaker.get_status())
            )
        return state

    def record_build_success(self) -> None:
        """Record a successful build."""
        self._build_breaker.record_success()
        logger.debug("Build success recorded")

    def record_build_failure(self, details: dict | None = None) -> GuardrailState:
        """Record a failed build.

        Returns:
            State after recording failure.
        """
        state = self._build_breaker.record_failure(details)
        if state == GuardrailState.BLOCKED:
            asyncio.create_task(
                self._notify(GuardrailType.BUILD_FAILURE, self._build_breaker.get_status())
            )
        return state

    def reset(self, guardrail_type: GuardrailType | None = None) -> None:
        """Reset guardrails.

        Args:
            guardrail_type: Specific guardrail to reset, or None for all.
        """
        if guardrail_type is None or guardrail_type == GuardrailType.BOOT_LOOP:
            self._boot_loop_breaker.reset()
        if guardrail_type is None or guardrail_type == GuardrailType.BUILD_FAILURE:
            self._build_breaker.reset()
        if guardrail_type is None or guardrail_type == GuardrailType.FLASH_FAILURE:
            self._flash_breaker.reset()

        logger.info(f"Guardrails reset: {guardrail_type or 'all'}")

    def get_status(self, guardrail_type: GuardrailType) -> GuardrailStatus:
        """Get status of a specific guardrail."""
        if guardrail_type == GuardrailType.BOOT_LOOP:
            status = self._boot_loop_breaker.get_status()
            status.guardrail_type = GuardrailType.BOOT_LOOP
            return status
        elif guardrail_type == GuardrailType.BUILD_FAILURE:
            status = self._build_breaker.get_status()
            status.guardrail_type = GuardrailType.BUILD_FAILURE
            return status
        elif guardrail_type == GuardrailType.FLASH_FAILURE:
            status = self._flash_breaker.get_status()
            status.guardrail_type = GuardrailType.FLASH_FAILURE
            return status
        else:
            raise ValueError(f"Unknown guardrail type: {guardrail_type}")

    def get_all_statuses(self) -> dict[GuardrailType, GuardrailStatus]:
        """Get status of all guardrails."""
        return {
            GuardrailType.BOOT_LOOP: self.get_status(GuardrailType.BOOT_LOOP),
            GuardrailType.BUILD_FAILURE: self.get_status(GuardrailType.BUILD_FAILURE),
            GuardrailType.FLASH_FAILURE: self.get_status(GuardrailType.FLASH_FAILURE),
        }

    def is_any_blocked(self) -> bool:
        """Check if any guardrail is in blocked state."""
        return (
            self._boot_loop_breaker.is_blocked
            or self._build_breaker.is_blocked
            or self._flash_breaker.is_blocked
        )

    def on_blocked(
        self,
        callback: Callable[[GuardrailType, GuardrailStatus], Coroutine],
    ) -> None:
        """Register a callback for when a guardrail is blocked.

        Callback receives (guardrail_type, status).
        """
        self._notification_callbacks.append(callback)

    async def _notify(
        self,
        guardrail_type: GuardrailType,
        status: GuardrailStatus,
    ) -> None:
        """Notify all registered callbacks of a blocked guardrail."""
        for callback in self._notification_callbacks:
            try:
                await callback(guardrail_type, status)
            except Exception as e:
                logger.warning(f"Notification callback error: {e}")


class BootMonitor:
    """Monitor serial output for boot success/failure markers.

    Usage:
        monitor = BootMonitor(safety, config)

        # Feed serial lines
        for line in serial_output:
            if monitor.process_line(line):
                print("Boot success detected!")
                break

        # Or wait with timeout
        success = await monitor.wait_for_boot(serial_stream)
    """

    def __init__(
        self,
        safety: SafetyGuardrails,
        config: SafetyConfig | None = None,
    ):
        self._safety = safety
        self._config = config or SafetyConfig()
        self._boot_detected = False
        self._boot_markers: list[str] = [self._config.boot_success_marker]

    def add_marker(self, marker: str) -> None:
        """Add a boot success marker to look for."""
        self._boot_markers.append(marker)

    def process_line(self, line: str) -> bool:
        """Process a serial line, checking for boot markers.

        Args:
            line: Serial output line.

        Returns:
            True if boot success was detected.
        """
        for marker in self._boot_markers:
            if marker in line:
                self._boot_detected = True
                self._safety.record_boot_success()
                return True
        return False

    async def wait_for_boot(
        self,
        line_generator,
        timeout: float | None = None,
    ) -> bool:
        """Wait for boot success marker in serial output.

        Args:
            line_generator: Async generator yielding serial lines.
            timeout: Timeout in seconds. None uses config default.

        Returns:
            True if boot success detected within timeout.
        """
        timeout = timeout or self._config.boot_timeout_seconds
        self._boot_detected = False

        try:
            async with asyncio.timeout(timeout):
                async for line in line_generator:
                    if self.process_line(line):
                        return True
        except asyncio.TimeoutError:
            self._safety.record_boot_failure({"reason": "timeout"})
            return False

        return False

    def reset(self) -> None:
        """Reset boot detection state."""
        self._boot_detected = False
