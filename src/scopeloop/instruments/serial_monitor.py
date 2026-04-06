"""Serial monitor for capturing device output."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator, Callable

import serial
import serial.tools.list_ports
from serial_asyncio import open_serial_connection

logger = logging.getLogger(__name__)


@dataclass
class SerialLine:
    """A timestamped line from serial output."""

    timestamp: float  # Unix timestamp
    line: str
    markers: list[str] = field(default_factory=list)  # Detected markers in this line

    @property
    def datetime(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "datetime": self.datetime.isoformat(),
            "line": self.line,
            "markers": self.markers,
        }


class SerialMonitor:
    """Async serial monitor with buffering and marker detection.

    Features:
    - Async reading with configurable buffer
    - Timestamped log lines
    - Marker detection (e.g., BOOT_OK, custom markers)
    - Line filtering and searching
    - Event callbacks for real-time processing

    Usage:
        monitor = SerialMonitor(port="/dev/tty.usbserial-0001", baud=115200)

        # Add markers to detect
        monitor.add_marker("BOOT_OK")
        monitor.add_marker("ERROR")

        # Start monitoring
        await monitor.start()

        # Read lines
        async for line in monitor.lines():
            print(f"[{line.datetime}] {line.line}")
            if "BOOT_OK" in line.markers:
                print("Boot successful!")

        # Or read recent lines
        recent = monitor.get_recent_lines(50)

        # Stop monitoring
        await monitor.stop()
    """

    def __init__(
        self,
        port: str,
        baud: int = 115200,
        buffer_size: int = 10000,
        encoding: str = "utf-8",
    ):
        self.port = port
        self.baud = baud
        self.buffer_size = buffer_size
        self.encoding = encoding

        self._buffer: deque[SerialLine] = deque(maxlen=buffer_size)
        self._markers: list[str] = []
        self._marker_callbacks: dict[str, list[Callable[[SerialLine], None]]] = {}
        self._line_callbacks: list[Callable[[SerialLine], None]] = []

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task | None = None
        self._running = False
        self._line_queue: asyncio.Queue[SerialLine] = asyncio.Queue()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def line_count(self) -> int:
        return len(self._buffer)

    def add_marker(self, marker: str) -> None:
        """Add a marker to detect in serial output."""
        if marker not in self._markers:
            self._markers.append(marker)
            self._marker_callbacks[marker] = []

    def on_marker(self, marker: str, callback: Callable[[SerialLine], None]) -> None:
        """Register a callback for when a marker is detected.

        Args:
            marker: Marker string to watch for.
            callback: Function called with SerialLine when marker detected.
        """
        if marker not in self._markers:
            self.add_marker(marker)
        self._marker_callbacks[marker].append(callback)

    def on_line(self, callback: Callable[[SerialLine], None]) -> None:
        """Register a callback for every line received."""
        self._line_callbacks.append(callback)

    async def start(self) -> None:
        """Start the serial monitor."""
        if self._running:
            logger.warning("Serial monitor already running")
            return

        logger.info(f"Starting serial monitor on {self.port} at {self.baud} baud")

        try:
            self._reader, self._writer = await open_serial_connection(
                url=self.port,
                baudrate=self.baud,
            )
            self._running = True
            self._read_task = asyncio.create_task(self._read_loop())
            logger.info("Serial monitor started")

        except Exception as e:
            logger.error(f"Failed to open serial port: {e}")
            raise

    async def stop(self) -> None:
        """Stop the serial monitor."""
        if not self._running:
            return

        logger.info("Stopping serial monitor...")
        self._running = False

        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass

        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass

        self._reader = None
        self._writer = None
        logger.info("Serial monitor stopped")

    async def send(self, data: str | bytes, newline: bool = True) -> None:
        """Send data to the serial port.

        Args:
            data: Data to send.
            newline: Whether to append newline.
        """
        if not self._running or not self._writer:
            raise RuntimeError("Serial monitor not running")

        if isinstance(data, str):
            if newline:
                data = data + "\n"
            data = data.encode(self.encoding)

        self._writer.write(data)
        await self._writer.drain()

    async def lines(self) -> AsyncIterator[SerialLine]:
        """Async iterator for serial lines.

        Yields lines as they are received.
        """
        while self._running:
            try:
                line = await asyncio.wait_for(self._line_queue.get(), timeout=1.0)
                yield line
            except asyncio.TimeoutError:
                continue

    def get_recent_lines(self, count: int = 50) -> list[SerialLine]:
        """Get recent lines from the buffer.

        Args:
            count: Number of lines to return.

        Returns:
            List of recent SerialLine objects (newest last).
        """
        if count >= len(self._buffer):
            return list(self._buffer)
        return list(self._buffer)[-count:]

    def get_lines_since_marker(self, marker: str) -> list[SerialLine]:
        """Get all lines since a marker was last seen.

        Args:
            marker: Marker to search for.

        Returns:
            Lines since (and including) the marker, or empty if not found.
        """
        result = []
        found = False

        for line in self._buffer:
            if marker in line.markers:
                found = True
                result = [line]  # Reset, start from this marker
            elif found:
                result.append(line)

        return result

    def get_lines_matching(self, pattern: str) -> list[SerialLine]:
        """Get lines matching a regex pattern.

        Args:
            pattern: Regex pattern to match.

        Returns:
            Matching lines.
        """
        regex = re.compile(pattern)
        return [line for line in self._buffer if regex.search(line.line)]

    def search(self, text: str) -> list[SerialLine]:
        """Search for text in buffered lines.

        Args:
            text: Text to search for (case-insensitive).

        Returns:
            Lines containing the text.
        """
        text_lower = text.lower()
        return [line for line in self._buffer if text_lower in line.line.lower()]

    def clear_buffer(self) -> None:
        """Clear the line buffer."""
        self._buffer.clear()

    def wait_for_marker(
        self,
        marker: str,
        timeout: float = 30.0,
    ) -> asyncio.Future[SerialLine]:
        """Wait for a marker to appear.

        Args:
            marker: Marker to wait for.
            timeout: Maximum time to wait.

        Returns:
            Future that resolves with the SerialLine containing the marker.
        """
        future: asyncio.Future[SerialLine] = asyncio.get_event_loop().create_future()

        def on_marker_found(line: SerialLine) -> None:
            if not future.done():
                future.set_result(line)

        # Add temporary callback
        self.on_marker(marker, on_marker_found)

        # Set up timeout
        async def timeout_handler() -> None:
            await asyncio.sleep(timeout)
            if not future.done():
                future.set_exception(asyncio.TimeoutError(f"Marker '{marker}' not found"))

        asyncio.create_task(timeout_handler())

        return future

    async def _read_loop(self) -> None:
        """Internal read loop."""
        partial_line = ""

        while self._running and self._reader:
            try:
                # Read data
                data = await asyncio.wait_for(
                    self._reader.read(1024),
                    timeout=1.0,
                )

                if not data:
                    continue

                # Decode and process
                try:
                    text = data.decode(self.encoding, errors="replace")
                except UnicodeDecodeError:
                    text = data.decode("latin-1", errors="replace")

                # Handle line assembly
                text = partial_line + text
                lines = text.split("\n")

                # Last element might be partial
                partial_line = lines[-1]
                complete_lines = lines[:-1]

                # Process complete lines
                for line_text in complete_lines:
                    line_text = line_text.rstrip("\r")  # Remove CR if present
                    if line_text:  # Skip empty lines
                        self._process_line(line_text)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Serial read error: {e}")
                await asyncio.sleep(0.1)

    def _process_line(self, line_text: str) -> None:
        """Process a complete line."""
        timestamp = time.time()

        # Detect markers
        detected_markers = []
        for marker in self._markers:
            if marker in line_text:
                detected_markers.append(marker)

        # Create SerialLine
        serial_line = SerialLine(
            timestamp=timestamp,
            line=line_text,
            markers=detected_markers,
        )

        # Add to buffer
        self._buffer.append(serial_line)

        # Put in queue for async iteration
        try:
            self._line_queue.put_nowait(serial_line)
        except asyncio.QueueFull:
            pass  # Drop if queue is full

        # Call line callbacks
        for callback in self._line_callbacks:
            try:
                callback(serial_line)
            except Exception as e:
                logger.warning(f"Line callback error: {e}")

        # Call marker callbacks
        for marker in detected_markers:
            for callback in self._marker_callbacks.get(marker, []):
                try:
                    callback(serial_line)
                except Exception as e:
                    logger.warning(f"Marker callback error: {e}")

    async def __aenter__(self) -> SerialMonitor:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.stop()


def list_serial_ports() -> list[dict]:
    """List available serial ports.

    Returns:
        List of dicts with port info.
    """
    ports = []
    for port in serial.tools.list_ports.comports():
        ports.append({
            "device": port.device,
            "description": port.description,
            "hwid": port.hwid,
            "vid": f"{port.vid:04x}" if port.vid else None,
            "pid": f"{port.pid:04x}" if port.pid else None,
            "serial_number": port.serial_number,
            "manufacturer": port.manufacturer,
            "product": port.product,
        })
    return ports
