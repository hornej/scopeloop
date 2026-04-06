"""Device Manager - Device discovery by VID/PID instead of hardcoded ports.

This module provides stable device identification using USB VID/PID/serial
instead of volatile port paths like /dev/ttyUSB0 which can change.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import AsyncIterator

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
    device_type: DeviceType = DeviceType.USB_SERIAL

    def matches(self, criteria: DeviceMatch) -> bool:
        """Check if this device matches the given criteria."""
        if criteria.vid is not None:
            if not self._match_hex(criteria.vid, self.vid):
                return False
        if criteria.pid is not None:
            if not self._match_hex(criteria.pid, self.pid):
                return False
        if criteria.serial is not None:
            if self.serial != criteria.serial:
                return False
        return True

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

    async def list_devices(self, device_type: DeviceType = DeviceType.USB_SERIAL) -> list[DeviceInfo]:
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
        for device in devices:
            if device.matches(match):
                return device
        return None

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
        if self._system == "Darwin":
            return await self._list_serial_devices_macos()
        elif self._system == "Linux":
            return await self._list_serial_devices_linux()
        elif self._system == "Windows":
            return await self._list_serial_devices_windows()
        else:
            logger.warning(f"Unsupported platform: {self._system}")
            return []

    async def _list_serial_devices_macos(self) -> list[DeviceInfo]:
        """List serial devices on macOS."""
        # Prefer pyserial's comports() which reliably exposes VID/PID for both
        # /dev/cu.usbserial-* and /dev/cu.usbmodem* (ESP32-S3 native USB).
        try:
            import serial.tools.list_ports

            devices: list[DeviceInfo] = []
            for port in serial.tools.list_ports.comports():
                # Only show USB-ish devices; filters out bluetooth pseudo-ports.
                if port.vid is None or port.pid is None:
                    continue

                devices.append(
                    DeviceInfo(
                        port=port.device,
                        vid=f"{port.vid:04x}",
                        pid=f"{port.pid:04x}",
                        serial=port.serial_number,
                        manufacturer=port.manufacturer,
                        product=port.product or port.description,
                    )
                )
            return devices

        except Exception as e:
            logger.warning(f"Error listing macOS serial devices via pyserial: {e}")

        # Fallback: legacy system_profiler parsing + /dev globbing.
        devices: list[DeviceInfo] = []

        try:
            result = await asyncio.create_subprocess_exec(
                "system_profiler",
                "SPUSBDataType",
                "-xml",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, _ = await result.communicate()

            tty_devices = list(Path("/dev").glob("tty.usb*")) + list(
                Path("/dev").glob("cu.usb*")
            )

            import plistlib

            try:
                plist_data = plistlib.loads(stdout)
                usb_info = self._parse_macos_usb_plist(plist_data)
            except Exception as e:
                logger.debug(f"Could not parse USB plist: {e}")
                usb_info = {}

            for tty_path in tty_devices:
                port = str(tty_path)
                device = DeviceInfo(port=port)

                match = re.search(r"usbserial-(\w+)", port)
                if match:
                    serial_hint = match.group(1)
                    for usb_dev in usb_info.values():
                        if usb_dev.get("serial", "").endswith(serial_hint):
                            device.vid = usb_dev.get("vid")
                            device.pid = usb_dev.get("pid")
                            device.serial = usb_dev.get("serial")
                            device.manufacturer = usb_dev.get("manufacturer")
                            device.product = usb_dev.get("product")
                            break

                devices.append(device)

        except Exception as e:
            logger.warning(f"Error listing macOS serial devices: {e}")
            tty_devices = list(Path("/dev").glob("tty.usb*")) + list(
                Path("/dev").glob("cu.usb*")
            )
            for tty_path in tty_devices:
                devices.append(DeviceInfo(port=str(tty_path)))

        return devices

    def _parse_macos_usb_plist(self, plist_data: list) -> dict:
        """Parse macOS USB system profiler plist data."""
        result = {}

        def extract_usb_devices(items: list) -> None:
            for item in items:
                if isinstance(item, dict):
                    # Check if this is a USB device entry
                    if "vendor_id" in item or "_name" in item:
                        vid = item.get("vendor_id", "")
                        pid = item.get("product_id", "")
                        serial = item.get("serial_num", "")

                        # Clean up hex values
                        if isinstance(vid, str):
                            vid = vid.replace("0x", "").split()[0] if vid else None
                        if isinstance(pid, str):
                            pid = pid.replace("0x", "").split()[0] if pid else None

                        key = f"{vid}:{pid}:{serial}"
                        result[key] = {
                            "vid": vid,
                            "pid": pid,
                            "serial": serial,
                            "manufacturer": item.get("manufacturer", ""),
                            "product": item.get("_name", ""),
                        }

                    # Recurse into nested items
                    if "_items" in item:
                        extract_usb_devices(item["_items"])

        for entry in plist_data:
            if isinstance(entry, dict) and "_items" in entry:
                extract_usb_devices(entry["_items"])

        return result

    async def _list_serial_devices_linux(self) -> list[DeviceInfo]:
        """List serial devices on Linux."""
        devices = []

        # Check /sys/class/tty for USB serial devices
        tty_class = Path("/sys/class/tty")
        if not tty_class.exists():
            return devices

        for tty_dir in tty_class.iterdir():
            # Check if this is a USB device
            device_path = tty_dir / "device"
            if not device_path.exists():
                continue

            # Follow symlink to get to USB device
            try:
                real_path = device_path.resolve()
                # Look for idVendor and idProduct in parent directories
                usb_device = self._find_linux_usb_parent(real_path)
                if usb_device is None:
                    continue

                port = f"/dev/{tty_dir.name}"
                if not Path(port).exists():
                    continue

                vid = self._read_sysfs_file(usb_device / "idVendor")
                pid = self._read_sysfs_file(usb_device / "idProduct")
                serial = self._read_sysfs_file(usb_device / "serial")
                manufacturer = self._read_sysfs_file(usb_device / "manufacturer")
                product = self._read_sysfs_file(usb_device / "product")

                devices.append(
                    DeviceInfo(
                        port=port,
                        vid=vid,
                        pid=pid,
                        serial=serial,
                        manufacturer=manufacturer,
                        product=product,
                    )
                )
            except Exception as e:
                logger.debug(f"Error reading device {tty_dir}: {e}")

        return devices

    def _find_linux_usb_parent(self, path: Path) -> Path | None:
        """Find the USB device parent directory in sysfs."""
        current = path
        while current != current.parent:
            if (current / "idVendor").exists():
                return current
            current = current.parent
        return None

    def _read_sysfs_file(self, path: Path) -> str | None:
        """Read a sysfs file, returning None if it doesn't exist."""
        try:
            return path.read_text().strip()
        except (FileNotFoundError, PermissionError):
            return None

    async def _list_serial_devices_windows(self) -> list[DeviceInfo]:
        """List serial devices on Windows."""
        devices = []

        try:
            # Use Windows Management Instrumentation (WMI)
            result = await asyncio.create_subprocess_exec(
                "powershell",
                "-Command",
                "Get-WmiObject Win32_SerialPort | Select-Object DeviceID, PNPDeviceID | ConvertTo-Json",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, _ = await result.communicate()

            import json

            try:
                ports_data = json.loads(stdout.decode("utf-8"))
                if isinstance(ports_data, dict):
                    ports_data = [ports_data]

                for port_info in ports_data:
                    port = port_info.get("DeviceID", "")
                    pnp_id = port_info.get("PNPDeviceID", "")

                    # Parse VID/PID from PNPDeviceID (e.g., USB\VID_10C4&PID_EA60\0001)
                    vid = None
                    pid = None
                    serial = None

                    vid_match = re.search(r"VID_([0-9A-Fa-f]+)", pnp_id)
                    pid_match = re.search(r"PID_([0-9A-Fa-f]+)", pnp_id)
                    if vid_match:
                        vid = vid_match.group(1)
                    if pid_match:
                        pid = pid_match.group(1)

                    # Serial is usually the last part
                    parts = pnp_id.split("\\")
                    if len(parts) >= 3:
                        serial = parts[-1]

                    devices.append(
                        DeviceInfo(
                            port=port,
                            vid=vid,
                            pid=pid,
                            serial=serial,
                        )
                    )
            except json.JSONDecodeError:
                logger.warning("Could not parse Windows serial port info")

        except Exception as e:
            logger.warning(f"Error listing Windows serial devices: {e}")

        return devices


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
