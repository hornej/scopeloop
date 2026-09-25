"""Read-only USB inventory. Never reset a device, hub, driver or vendor process."""

from __future__ import annotations

import json
import platform
import re
import subprocess
from pathlib import Path


def inventory(sysfs: Path = Path("/sys/bus/usb/devices")) -> dict:
    try:
        if platform.system() == "Linux":
            if not sysfs.is_dir():
                raise OSError("USB sysfs inventory is unavailable")
            devices = []
            for path in sorted(sysfs.iterdir()):
                if not re.fullmatch(r"\d+-[\d.]+", path.name):
                    continue
                item = {"path": str(path), "state": "present"}
                for name in ("idVendor", "idProduct", "serial", "product", "manufacturer"):
                    try:
                        item[name] = (path / name).read_text().strip()
                    except FileNotFoundError:
                        item[name] = None
                if not item["idVendor"] or not item["idProduct"]:
                    item["state"] = "enumeration_failed"
                devices.append(item)
        elif platform.system() == "Windows":
            command = (
                "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
                "$ErrorActionPreference='Stop'; "
                "Get-CimInstance Win32_PnPEntity -Filter \"PNPDeviceID LIKE 'USB\\\\%'\" | "
                "Select-Object Name,PNPDeviceID,ConfigManagerErrorCode,Status | ConvertTo-Json"
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                timeout=20,
                encoding="utf-8",
                check=True,
            )
            rows = json.loads(result.stdout or "[]")
            if isinstance(rows, dict):
                rows = [rows]
            devices = [
                {
                    **row,
                    "state": "present"
                    if row["ConfigManagerErrorCode"] == 0
                    else "enumeration_failed",
                }
                for row in (rows or [])
            ]
        else:
            return {"status": "unsupported", "devices": [], "application_status": "not_probed"}
        return {"status": "complete", "devices": devices, "application_status": "not_probed"}
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        detail = str(exc)
        if isinstance(exc, subprocess.CalledProcessError):
            detail += ": " + (exc.stderr or "")
        return {
            "status": "enumeration_failed",
            "error": detail,
            "devices": [],
            "application_status": "not_probed",
        }
