"""Local instrument-host environment checks.

The host is the machine that physically owns USB instruments and serial ports.
Today that may be a developer workstation; later it can be a dedicated Linux box.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import platform
import shutil
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scopeloop.config import Config


@dataclass(frozen=True)
class HostCheck:
    """Status for one local host dependency."""

    name: str
    kind: str
    available: bool
    detail: str
    path: str | None = None
    version: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe representation."""
        return asdict(self)


@dataclass(frozen=True)
class StorageStatus:
    """Disk-space status for the capture/artifact directory."""

    path: str
    exists: bool
    checked_path: str
    total_bytes: int
    free_bytes: int

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe representation."""
        return asdict(self)


@dataclass(frozen=True)
class HostStatus:
    """Complete status for the current instrument host."""

    mode: str
    host: str | None
    port: int
    data_dir: str
    platform: str
    machine: str
    python: str
    checks: list[HostCheck]
    storage: StorageStatus

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe representation."""
        return {
            "mode": self.mode,
            "host": self.host,
            "port": self.port,
            "data_dir": self.data_dir,
            "platform": self.platform,
            "machine": self.machine,
            "python": self.python,
            "checks": [check.to_dict() for check in self.checks],
            "storage": self.storage.to_dict(),
        }


def collect_host_status(config: Config | None = None, data_dir: Path | None = None) -> HostStatus:
    """Collect dependency and storage status for the current host."""
    runtime = config.runtime if config else None
    mode = runtime.mode if runtime else "local"
    host = runtime.host if runtime else None
    port = runtime.port if runtime else 8765

    configured_data_dir = runtime.data_dir if runtime else "~/ScopeLoop"
    resolved_data_dir = data_dir or Path(configured_data_dir).expanduser()

    logic_port = 10430
    if config and config.instruments.logic_analyzer:
        logic_port = config.instruments.logic_analyzer.port

    checks = [
        _python_check(),
        _module_check("logic2-automation", "saleae.automation", "Saleae automation API"),
        _saleae_app_check(),
        _tcp_port_check("127.0.0.1", logic_port, "Logic 2 automation server"),
        _picoscope_app_check(),
        _module_check("picosdk", "picosdk", "PicoSDK Python wrappers"),
        _module_check("pypicosdk", "pypicosdk", "pyPicoSDK"),
        _executable_check("pysigrok-cli", "pysigrok CLI"),
        _executable_check("sigrok-cli", "sigrok CLI"),
        _esptool_check(),
    ]

    return HostStatus(
        mode=mode,
        host=host,
        port=port,
        data_dir=str(resolved_data_dir),
        platform=platform.system(),
        machine=platform.machine(),
        python=sys.version.split()[0],
        checks=checks,
        storage=_storage_status(resolved_data_dir),
    )


def format_bytes(value: int) -> str:
    """Format bytes for terminal display."""
    units = ["B", "KB", "MB", "GB", "TB"]
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"


def _python_check() -> HostCheck:
    return HostCheck(
        name="Python",
        kind="runtime",
        available=True,
        detail=f"{sys.executable}",
        path=sys.executable,
        version=sys.version.split()[0],
    )


def _module_check(distribution: str, module_name: str, display_name: str) -> HostCheck:
    try:
        spec = importlib.util.find_spec(module_name)
    except ModuleNotFoundError:
        spec = None
    if spec is None:
        return HostCheck(
            name=display_name,
            kind="python-package",
            available=False,
            detail=f"Install Python package or module: {distribution}",
        )

    version = _distribution_version(distribution)
    origin = spec.origin if spec.origin and spec.origin != "built-in" else None
    return HostCheck(
        name=display_name,
        kind="python-package",
        available=True,
        detail="importable",
        path=origin,
        version=version,
    )


def _distribution_version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def _executable_check(command: str, display_name: str) -> HostCheck:
    path = _which_command(command)
    if not path:
        return HostCheck(
            name=display_name,
            kind="executable",
            available=False,
            detail=f"{command} not found on PATH",
        )

    version = _command_version([path, "--version"]) or _command_version([path, "-V"])
    return HostCheck(
        name=display_name,
        kind="executable",
        available=True,
        detail="found on PATH",
        path=path,
        version=version,
    )


def _command_version(command: list[str]) -> str | None:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    output = (result.stdout or result.stderr).strip()
    if not output:
        return None
    return output.splitlines()[0][:120]


def _which_command(command: str) -> str | None:
    path = shutil.which(command)
    if path:
        return path

    venv_candidate = Path(sys.executable).parent / command
    if venv_candidate.exists() and venv_candidate.is_file():
        return str(venv_candidate)

    return None


def _saleae_app_check() -> HostCheck:
    candidates = _saleae_app_candidates()
    for candidate in candidates:
        if candidate.exists():
            return HostCheck(
                name="Saleae Logic 2 app",
                kind="application",
                available=True,
                detail="installed",
                path=str(candidate),
            )

    path = _which_command("Logic")
    if path:
        return HostCheck(
            name="Saleae Logic 2 app",
            kind="application",
            available=True,
            detail="found on PATH",
            path=path,
        )

    return HostCheck(
        name="Saleae Logic 2 app",
        kind="application",
        available=False,
        detail="Install Logic 2 and enable the automation server when capturing",
    )


def _saleae_app_candidates() -> list[Path]:
    system = platform.system()
    if system == "Darwin":
        return [
            Path("/Applications/Logic 2.app"),
            Path("/Applications/Logic2.app"),
            Path.home() / "Applications/Logic 2.app",
            Path.home() / "Applications/Logic2.app",
        ]
    if system == "Linux":
        return [
            Path("/usr/bin/logic2"),
            Path("/usr/local/bin/logic2"),
            Path("/opt/Logic 2/Logic"),
        ]
    if system == "Windows":
        return [
            Path("C:/Program Files/Logic/Logic.exe"),
            Path("C:/Program Files/Saleae LLC/Logic/Logic.exe"),
            Path.home() / "AppData/Local/Programs/Logic/Logic.exe",
        ]
    return []


def _picoscope_app_check() -> HostCheck:
    candidates = _picoscope_app_candidates()
    for candidate in candidates:
        if candidate.exists():
            return HostCheck(
                name="PicoScope 7 app",
                kind="application",
                available=True,
                detail="installed",
                path=str(candidate),
            )

    for command in ("picoscope", "picoscope7"):
        path = _which_command(command)
        if path:
            return HostCheck(
                name="PicoScope 7 app",
                kind="application",
                available=True,
                detail="found on PATH",
                path=path,
            )

    return HostCheck(
        name="PicoScope 7 app",
        kind="application",
        available=False,
        detail="Install PicoScope 7 for GUI use, or PicoSDK for automation",
    )


def _picoscope_app_candidates() -> list[Path]:
    system = platform.system()
    if system == "Darwin":
        return [
            Path("/Applications/PicoScope 7.app"),
            Path("/Applications/PicoScope 7 T&M.app"),
            Path.home() / "Applications/PicoScope 7.app",
        ]
    if system == "Linux":
        return [
            Path("/usr/bin/picoscope"),
            Path("/usr/bin/picoscope7"),
            Path("/opt/picoscope/bin/picoscope"),
        ]
    if system == "Windows":
        return [
            Path("C:/Program Files/Pico Technology/PicoScope 7 T&M Stable/PicoScope.exe"),
            Path("C:/Program Files/Pico Technology/PicoScope 7/PicoScope.exe"),
            Path("C:/Program Files (x86)/Pico Technology/PicoScope 7/PicoScope.exe"),
        ]
    return []


def _tcp_port_check(host: str, port: int, display_name: str) -> HostCheck:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            pass
    except OSError as exc:
        return HostCheck(
            name=display_name,
            kind="tcp-port",
            available=False,
            detail=f"{host}:{port} not reachable ({exc.__class__.__name__})",
        )

    return HostCheck(
        name=display_name,
        kind="tcp-port",
        available=True,
        detail=f"listening on {host}:{port}",
    )


def _esptool_check() -> HostCheck:
    spec = importlib.util.find_spec("esptool")
    if spec is not None:
        return HostCheck(
            name="esptool",
            kind="python-package",
            available=True,
            detail="importable",
            path=spec.origin,
            version=_distribution_version("esptool"),
        )

    for command in ("esptool.py", "esptool"):
        path = _which_command(command)
        if path:
            return HostCheck(
                name="esptool",
                kind="executable",
                available=True,
                detail="found on PATH",
                path=path,
                version=_command_version([path, "--version"]),
            )

    return HostCheck(
        name="esptool",
        kind="python-package",
        available=False,
        detail="Install esptool for ESP flashing workflows",
    )


def _storage_status(path: Path) -> StorageStatus:
    expanded = path.expanduser()
    checked_path = expanded
    exists = expanded.exists()

    while not checked_path.exists() and checked_path != checked_path.parent:
        checked_path = checked_path.parent

    usage = shutil.disk_usage(checked_path)
    return StorageStatus(
        path=str(expanded),
        exists=exists,
        checked_path=str(checked_path),
        total_bytes=usage.total,
        free_bytes=usage.free,
    )
