"""esptool flasher integration for ESP32/ESP8266."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

from scopeloop.flash.base import Flasher, FlashError, FlashResult, FlashStatus

logger = logging.getLogger(__name__)


class ESPToolFlasher(Flasher):
    """ESP32/ESP8266 flasher using esptool.py.

    Usage:
        flasher = ESPToolFlasher(port="/dev/tty.usbserial-0001", baud=460800)

        # Flash a single binary
        result = await flasher.flash(Path("firmware.bin"))

        # Flash with specific offset
        result = await flasher.flash_at_offset(Path("firmware.bin"), "0x10000")

        # Flash multiple files (bootloader, partition table, app)
        result = await flasher.flash_files({
            "0x1000": "build/bootloader/bootloader.bin",
            "0x8000": "build/partition_table/partition-table.bin",
            "0x10000": "build/my_project.bin",
        })

        # Reset device
        await flasher.reset()
    """

    def __init__(
        self,
        port: str,
        baud: int = 460800,
        chip: str = "auto",
        before: str = "default_reset",
        after: str = "hard_reset",
    ):
        super().__init__(port, baud)
        self.chip = chip
        self.before = before
        self.after = after

    async def flash(self, firmware_path: Path, verify: bool = True) -> FlashResult:
        """Flash firmware to the device at the default offset (0x10000 for ESP32).

        Args:
            firmware_path: Path to firmware binary.
            verify: Whether to verify after flashing.

        Returns:
            Flash result.
        """
        return await self.flash_at_offset(firmware_path, "0x10000", verify)

    async def flash_at_offset(
        self,
        firmware_path: Path,
        offset: str,
        verify: bool = True,
    ) -> FlashResult:
        """Flash firmware at a specific offset.

        Args:
            firmware_path: Path to firmware binary.
            offset: Flash offset (e.g., "0x10000").
            verify: Whether to verify after flashing.

        Returns:
            Flash result.
        """
        return await self.flash_files({offset: str(firmware_path)}, verify)

    async def flash_files(
        self,
        files: dict[str, str],
        verify: bool = True,
    ) -> FlashResult:
        """Flash multiple files at specified offsets.

        Args:
            files: Dict of offset -> file path.
            verify: Whether to verify after flashing.

        Returns:
            Flash result.
        """
        start_time = time.time()

        # Verify all files exist
        for offset, file_path in files.items():
            path = Path(file_path)
            if not path.exists():
                return FlashResult(
                    status=FlashStatus.FAILURE,
                    output=f"File not found: {file_path}",
                    duration_seconds=time.time() - start_time,
                )

        # Build command
        cmd = [
            "esptool.py",
            "--chip", self.chip,
            "--port", self.port,
            "--baud", str(self.baud),
            "--before", self.before,
            "--after", self.after,
            "write_flash",
        ]

        # Add verify flag if requested
        if verify:
            cmd.append("--verify")

        # Add offset/file pairs
        for offset, file_path in files.items():
            cmd.extend([offset, file_path])

        logger.info(f"Flashing to {self.port}...")
        logger.debug(f"Command: {' '.join(cmd)}")

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            stdout, _ = await process.communicate()
            output = stdout.decode("utf-8", errors="replace")

            duration = time.time() - start_time

            # Parse output
            bytes_written = self._parse_bytes_written(output)

            if process.returncode == 0:
                # Check for verification failure
                if verify and "verify" in output.lower() and "failed" in output.lower():
                    return FlashResult(
                        status=FlashStatus.VERIFICATION_FAILED,
                        output=output,
                        duration_seconds=duration,
                        bytes_written=bytes_written,
                    )

                logger.info(f"Flash successful ({bytes_written} bytes in {duration:.1f}s)")
                return FlashResult(
                    status=FlashStatus.SUCCESS,
                    output=output,
                    duration_seconds=duration,
                    bytes_written=bytes_written,
                )
            else:
                logger.error(f"Flash failed: {output}")
                return FlashResult(
                    status=FlashStatus.FAILURE,
                    output=output,
                    duration_seconds=duration,
                    bytes_written=bytes_written,
                )

        except FileNotFoundError:
            return FlashResult(
                status=FlashStatus.FAILURE,
                output="esptool.py not found. Install with: pip install esptool",
                duration_seconds=time.time() - start_time,
            )
        except asyncio.TimeoutError:
            return FlashResult(
                status=FlashStatus.TIMEOUT,
                output="Flash operation timed out",
                duration_seconds=time.time() - start_time,
            )

    async def verify(self, firmware_path: Path) -> bool:
        """Verify firmware matches what's on the device.

        Args:
            firmware_path: Path to firmware binary.

        Returns:
            True if verification passed.
        """
        cmd = [
            "esptool.py",
            "--chip", self.chip,
            "--port", self.port,
            "--baud", str(self.baud),
            "verify_flash",
            "0x10000", str(firmware_path),
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            stdout, _ = await process.communicate()
            output = stdout.decode("utf-8", errors="replace")

            return process.returncode == 0 and "verify ok" in output.lower()

        except Exception as e:
            logger.error(f"Verification error: {e}")
            return False

    async def reset(self) -> None:
        """Reset the device using RTS/DTR."""
        cmd = [
            "esptool.py",
            "--chip", self.chip,
            "--port", self.port,
            "--after", "hard_reset",
            "read_mac",  # Dummy command just to trigger reset
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            await process.communicate()
            logger.info("Device reset")
        except Exception as e:
            logger.error(f"Reset error: {e}")

    async def erase_flash(self) -> FlashResult:
        """Erase the entire flash.

        Returns:
            Flash result.
        """
        start_time = time.time()

        cmd = [
            "esptool.py",
            "--chip", self.chip,
            "--port", self.port,
            "--baud", str(self.baud),
            "erase_flash",
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            stdout, _ = await process.communicate()
            output = stdout.decode("utf-8", errors="replace")

            duration = time.time() - start_time

            if process.returncode == 0:
                logger.info(f"Flash erased in {duration:.1f}s")
                return FlashResult(
                    status=FlashStatus.SUCCESS,
                    output=output,
                    duration_seconds=duration,
                )
            else:
                return FlashResult(
                    status=FlashStatus.FAILURE,
                    output=output,
                    duration_seconds=duration,
                )

        except Exception as e:
            return FlashResult(
                status=FlashStatus.FAILURE,
                output=str(e),
                duration_seconds=time.time() - start_time,
            )

    async def get_chip_info(self) -> dict:
        """Get information about the connected chip.

        Returns:
            Dict with chip info (chip type, MAC, features, etc.).
        """
        cmd = [
            "esptool.py",
            "--chip", self.chip,
            "--port", self.port,
            "chip_id",
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            stdout, _ = await process.communicate()
            output = stdout.decode("utf-8", errors="replace")

            # Parse output
            info = {"raw_output": output}

            # Extract chip type
            chip_match = re.search(r"Chip is (\S+)", output)
            if chip_match:
                info["chip"] = chip_match.group(1)

            # Extract MAC
            mac_match = re.search(r"MAC: ([0-9a-f:]+)", output, re.IGNORECASE)
            if mac_match:
                info["mac"] = mac_match.group(1)

            # Extract features
            features_match = re.search(r"Features: (.+)", output)
            if features_match:
                info["features"] = features_match.group(1)

            return info

        except Exception as e:
            return {"error": str(e)}

    def _parse_bytes_written(self, output: str) -> int:
        """Parse bytes written from esptool output."""
        # Look for patterns like "Wrote 123456 bytes"
        match = re.search(r"Wrote (\d+) bytes", output)
        if match:
            return int(match.group(1))
        return 0
