"""Device Manager - Device discovery by VID/PID instead of hardcoded ports.

This module provides stable device identification using USB VID/PID/serial
instead of volatile port paths like /dev/ttyUSB0 which can change.
"""

from __future__ import annotations

import asyncio
import logging
import platform
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class DeviceType(Enum):
    """Type of device."""

    USB_SERIAL = "usb-serial"
    USB_HID = "usb-hid"
    NETWORK = "network"


@dataclass
class DeviceMatch:
    """Criteria for matching a device."""

    vid: str | None = None  # Vendor ID (e.g., "10c4" or "0x10c4")
    pid: str | None = None  # Product ID (e.g., "ea60" or "0xea60")
    by_id: str | None = None
    serial: str | None = None  # Device serial number


@dataclass
class DeviceInfo:
    """Information about a discovered device."""

    port: str  # e.g., "/dev/tty.usbserial-0001" or "COM3"
    vid: str | None = None
    pid: str | None = None
    serial: str | None = None
    manufacturer: str | None = None
    product: str | None = None
    by_id: str | None = None
    device_type: DeviceType = DeviceType.USB_SERIAL

    def matches(self, criteria: DeviceMatch) -> bool:
        """Check if this device matches the given criteria."""
        if criteria.by_id is not None and criteria.by_id != self.by_id:
            return False
        if criteria.vid is not None and not self._match_hex(criteria.vid, self.vid):
            return False
        if criteria.pid is not None and not self._match_hex(criteria.pid, self.pid):
            return False
        return not (criteria.serial is not None and self.serial != criteria.serial)

    @staticmethod
    def _match_hex(pattern: str, value: str | None) -> bool:
        """Match hex values, handling 0x prefix variations."""
        if value is None:
            return False
        # Normalize both to lowercase without 0x prefix
        pattern_norm = pattern.lower().replace("0x", "")
        value_norm = value.lower().replace("0x", "")
        return pattern_norm == value_norm


class DeviceEvent(Enum):
    """Device event types."""

    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


@dataclass
class DeviceEventInfo:
    """Information about a device event."""

    event: DeviceEvent
    device: DeviceInfo


class DeviceManager:
    """Manage device discovery and identity.

    Provides stable device identification using VID/PID/serial instead of
    volatile port paths.

    Usage:
        manager = DeviceManager()

        # Find a specific device
        device = await manager.find_device(DeviceMatch(vid="10c4", pid="ea60"))
        if device:
            print(f"Found at: {device.port}")

        # List all serial devices
        devices = await manager.list_devices()
        for dev in devices:
            print(f"{dev.port}: {dev.vid}:{dev.pid}")

        # Watch for device events
        async for event in manager.watch_devices():
            print(f"Device {event.event}: {event.device.port}")
    """

    def __init__(self):
        self._system = platform.system()
        self._cached_devices: list[DeviceInfo] = []

    async def list_devices(
        self, device_type: DeviceType = DeviceType.USB_SERIAL
    ) -> list[DeviceInfo]:
        """List all devices of the given type.

        Args:
            device_type: Type of devices to list.

        Returns:
            List of discovered devices.
        """
        if device_type == DeviceType.USB_SERIAL:
            return await self._list_serial_devices()
        else:
            raise NotImplementedError(f"Device type not supported: {device_type}")

    async def find_device(self, match: DeviceMatch) -> DeviceInfo | None:
        """Find a device matching the given criteria.

        Args:
            match: Matching criteria (VID, PID, serial).

        Returns:
            Device info if found, None otherwise.
        """
        devices = await self.list_devices()
        matches = [device for device in devices if device.matches(match)]
        if len(matches) > 1:
            raise ValueError("Ambiguous device identity; configure serial and/or by_id")
        return matches[0] if matches else None

    async def diagnose(self, match: DeviceMatch) -> dict:
        """Enumerate only; do not open serial ports, reset USB or restart applications."""
        from dataclasses import asdict

        try:
            devices = await self.list_devices()
        except Exception as exc:
            return {
                "status": "enumeration_failed",
                "error": str(exc),
                "application_status": "not_probed",
            }
        found = [d for d in devices if d.matches(match)]
        return {
            "status": "ambiguous" if len(found) > 1 else "present" if found else "absent",
            "devices": [{**asdict(d), "device_type": d.device_type.value} for d in found],
            "application_status": "not_probed",
        }

    async def find_device_port(self, match: DeviceMatch) -> str | None:
        """Find the port path for a device matching the given criteria.

        Args:
            match: Matching criteria.

        Returns:
            Port path (e.g., "/dev/tty.usbserial-0001") if found.
        """
        device = await self.find_device(match)
        return device.port if device else None

    async def wait_for_device(
        self,
        match: DeviceMatch,
        timeout: float = 30.0,
        poll_interval: float = 1.0,
    ) -> DeviceInfo | None:
        """Wait for a device to appear.

        Args:
            match: Matching criteria.
            timeout: Maximum time to wait in seconds.
            poll_interval: How often to check for the device.

        Returns:
            Device info if found within timeout, None otherwise.
        """
        import time

        start = time.monotonic()
        while time.monotonic() - start < timeout:
            device = await self.find_device(match)
            if device:
                return device
            await asyncio.sleep(poll_interval)
        return None

    async def watch_devices(
        self,
        device_type: DeviceType = DeviceType.USB_SERIAL,
        poll_interval: float = 2.0,
    ) -> AsyncIterator[DeviceEventInfo]:
        """Watch for device connect/disconnect events.

        Args:
            device_type: Type of devices to watch.
            poll_interval: How often to check for changes.

        Yields:
            Device events as they occur.
        """
        previous_devices = {d.port: d for d in await self.list_devices(device_type)}

        while True:
            await asyncio.sleep(poll_interval)
            current_devices = {d.port: d for d in await self.list_devices(device_type)}

            # Check for new devices
            for port, device in current_devices.items():
                if port not in previous_devices:
                    yield DeviceEventInfo(event=DeviceEvent.CONNECTED, device=device)

            # Check for removed devices
            for port, device in previous_devices.items():
                if port not in current_devices:
                    yield DeviceEventInfo(event=DeviceEvent.DISCONNECTED, device=device)

            previous_devices = current_devices

    async def _list_serial_devices(self) -> list[DeviceInfo]:
        """List serial devices on the system."""
        import serial.tools.list_ports

        try:
            ports = await asyncio.to_thread(serial.tools.list_ports.comports)
        except Exception as exc:
            raise RuntimeError(f"Serial enumeration failed: {exc}") from exc
        aliases = {}
        if self._system == "Linux":
            for alias in Path("/dev/serial/by-id").glob("*"):
                aliases[str(alias.resolve())] = str(alias)
        return [
            DeviceInfo(
                port=aliases.get(p.device, p.device),
                vid=f"{p.vid:04x}" if p.vid is not None else None,
                pid=f"{p.pid:04x}" if p.pid is not None else None,
                serial=p.serial_number,
                manufacturer=p.manufacturer,
                product=p.product or p.description,
                by_id=aliases.get(p.device),
            )
            for p in ports
            if p.vid is not None or p.pid is not None
        ]


# Convenience function for quick device lookup
async def find_device_port(
    vid: str | None = None,
    pid: str | None = None,
    serial: str | None = None,
) -> str | None:
    """Find a device port by VID/PID/serial.

    Args:
        vid: Vendor ID (e.g., "10c4" or "0x10c4").
        pid: Product ID.
        serial: Device serial number.

    Returns:
        Port path if found, None otherwise.
    """
    manager = DeviceManager()
    return await manager.find_device_port(DeviceMatch(vid=vid, pid=pid, serial=serial))
