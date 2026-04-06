"""Configuration loader for scopeloop.yaml files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator


class DeviceMatch(BaseModel):
    """USB device matching criteria."""

    vid: str | None = None  # Vendor ID (e.g., "0x10c4")
    pid: str | None = None  # Product ID (e.g., "0xea60")
    serial: str | None = None  # Device serial number (optional)


class DeviceConfig(BaseModel):
    """Device configuration."""

    type: str  # e.g., "usb-serial"
    match: DeviceMatch
    alias: str


class ProbeConfig(BaseModel):
    """Oscilloscope probe configuration."""

    signal: str  # Description of what's connected
    coupling: str = "DC"  # DC or AC
    scale: str = "1V/div"


class OscilloscopeConfig(BaseModel):
    """Oscilloscope configuration."""

    type: str  # "sigrok" or "siglent-sds1000x" (legacy)
    driver: str | None = None  # sigrok driver name (e.g., "siglent-sds")
    address: str | None = None  # IP address for network devices
    connection: str | None = None  # sigrok connection string
    probes: dict[str, ProbeConfig] = Field(default_factory=dict)


class LogicChannelConfig(BaseModel):
    """Logic analyzer channel configuration."""

    signal: str
    label: str | None = None


class LogicAnalyzerConfig(BaseModel):
    """Logic analyzer configuration."""

    type: str  # "saleae" or "sigrok"
    driver: str | None = None  # sigrok driver name (for sigrok type)
    device_id: str | None = None  # Saleae device ID (for saleae type)
    port: int = 10430  # Logic 2 automation port (for saleae type)
    connection: str | None = None  # sigrok connection string (for sigrok type)
    channels: dict[int, LogicChannelConfig] = Field(default_factory=dict)


class InstrumentsConfig(BaseModel):
    """All instrument configurations."""

    oscilloscope: OscilloscopeConfig | None = None
    logic_analyzer: LogicAnalyzerConfig | None = None


class CriterionConfig(BaseModel):
    """A single test criterion."""

    source: str  # e.g., "oscilloscope.CH1"
    measurement: str | None = None  # e.g., "frequency"
    expected: float | None = None
    unit: str | None = None
    tolerance_percent: float | None = None
    protocol: str | None = None  # For logic analyzer
    verify: str | None = None  # Expression to verify


class TestConfig(BaseModel):
    """Test definition."""

    name: str
    description: str | None = None
    setup: list[str] = Field(default_factory=list)  # Setup instructions
    criteria: list[CriterionConfig] = Field(default_factory=list)


class HardwareConfig(BaseModel):
    """Hardware description."""

    mcu: str  # e.g., "esp32"
    board: str | None = None  # e.g., "esp32-devkitc"
    schematic: str | None = None  # Path to schematic file
    bom: str | None = None  # Path to BOM file
    datasheets: list[str] = Field(default_factory=list)
    modifications: list[str] = Field(default_factory=list)
    locked: bool = False  # If true, only software changes allowed


class BuildConfig(BaseModel):
    """Build system configuration."""

    system: str  # e.g., "esp-idf"
    project_path: str = "."
    target: str | None = None  # e.g., "esp32"


class FlashConfig(BaseModel):
    """Flash configuration."""

    method: str  # e.g., "esptool"
    device: str  # Device alias
    baud: int = 460800


class SerialConfig(BaseModel):
    """Serial monitor configuration."""

    device: str  # Device alias
    baud: int = 115200


class GitConfig(BaseModel):
    """Git operations policy."""

    enabled: bool = True
    branch_prefix: str = "scopeloop/"
    auto_commit: bool = False
    protect_branches: list[str] = Field(default_factory=lambda: ["main", "master"])
    forbidden_operations: list[str] = Field(
        default_factory=lambda: ["force-push", "rebase", "reset --hard"]
    )


class SafetyConfig(BaseModel):
    """Safety guardrails configuration."""

    max_flash_attempts: int = 5
    flash_window_seconds: float = 60.0
    boot_success_marker: str = "BOOT_OK"
    boot_timeout_seconds: float = 10.0
    max_consecutive_build_failures: int = 5


class ProjectConfig(BaseModel):
    """Project metadata."""

    name: str
    description: str | None = None


class Config(BaseModel):
    """Root configuration for scopeloop.yaml."""

    project: ProjectConfig
    hardware: HardwareConfig
    build: BuildConfig
    devices: dict[str, DeviceConfig] = Field(default_factory=dict)
    flash: FlashConfig | None = None
    serial: SerialConfig | None = None
    instruments: InstrumentsConfig = Field(default_factory=InstrumentsConfig)
    tests: list[TestConfig] = Field(default_factory=list)
    git: GitConfig = Field(default_factory=GitConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)

    @field_validator("devices", mode="before")
    @classmethod
    def parse_devices(cls, v: Any) -> dict[str, DeviceConfig]:
        """Parse devices from YAML format."""
        if not v:
            return {}
        result = {}
        for name, config in v.items():
            if isinstance(config, dict):
                result[name] = DeviceConfig(**config)
            else:
                result[name] = config
        return result

    def get_device(self, alias: str) -> DeviceConfig | None:
        """Get a device by its alias."""
        for device in self.devices.values():
            if device.alias == alias:
                return device
        return self.devices.get(alias)

    def resolve_device_alias(self, alias: str) -> DeviceConfig:
        """Resolve a device alias to its configuration."""
        device = self.get_device(alias)
        if device is None:
            raise ValueError(f"Unknown device alias: {alias}")
        return device


def load_config(path: str | Path | None = None) -> Config:
    """Load configuration from a scopeloop.yaml file.

    Args:
        path: Path to config file. If None, searches for scopeloop.yaml
              in the current directory and parent directories.

    Returns:
        Parsed Config object.

    Raises:
        FileNotFoundError: If no config file is found.
        ValueError: If the config file is invalid.
    """
    if path is None:
        path = find_config_file()
    else:
        path = Path(path)

    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path) as f:
        data = yaml.safe_load(f)

    if data is None:
        raise ValueError(f"Empty config file: {path}")

    return Config.model_validate(data)


def find_config_file(start_dir: Path | None = None) -> Path:
    """Find scopeloop.yaml in the current directory or parent directories.

    Args:
        start_dir: Directory to start searching from. Defaults to cwd.

    Returns:
        Path to the config file.

    Raises:
        FileNotFoundError: If no config file is found.
    """
    if start_dir is None:
        start_dir = Path.cwd()

    current = start_dir
    while current != current.parent:
        config_path = current / "scopeloop.yaml"
        if config_path.exists():
            return config_path
        current = current.parent

    # Check root
    config_path = current / "scopeloop.yaml"
    if config_path.exists():
        return config_path

    raise FileNotFoundError(
        f"No scopeloop.yaml found in {start_dir} or parent directories"
    )


def create_default_config(
    project_name: str,
    mcu: str = "esp32",
    output_path: Path | None = None,
) -> Path:
    """Create a default scopeloop.yaml configuration file.

    Args:
        project_name: Name of the project.
        mcu: Target MCU (default: esp32).
        output_path: Where to write the file. Defaults to ./scopeloop.yaml.

    Returns:
        Path to the created config file.
    """
    if output_path is None:
        output_path = Path.cwd() / "scopeloop.yaml"

    config_content = f"""# ScopeLoop Configuration
# See https://github.com/joshhorne/scopeloop for documentation

project:
  name: "{project_name}"
  description: ""

hardware:
  mcu: {mcu}
  board: "{mcu}-devkitc"
  schematic: null
  bom: null
  datasheets: []
  modifications: []
  locked: false

build:
  system: esp-idf
  project_path: ./firmware
  target: {mcu}

devices:
  dut:
    type: usb-serial
    match:
      vid: null  # Set to your device's VID (e.g., "0x10c4")
      pid: null  # Set to your device's PID (e.g., "0xea60")
    alias: "dut"

flash:
  method: esptool
  device: dut
  baud: 460800

serial:
  device: dut
  baud: 115200

instruments:
  oscilloscope: null
  # Sigrok-based oscilloscope (recommended):
  # oscilloscope:
  #   type: sigrok
  #   driver: siglent-sds
  #   connection: "tcp-raw/192.168.1.XXX/5025"
  #   probes:
  #     CH1:
  #       signal: "GPIO2 - PWM output"
  #       coupling: DC
  #       scale: "1V/div"
  #
  # Legacy direct SCPI (deprecated):
  # oscilloscope:
  #   type: siglent-sds1000x
  #   address: "192.168.1.XXX"

  logic_analyzer: null
  # Saleae Logic 2 (recommended for Saleae devices):
  # logic_analyzer:
  #   type: saleae
  #   port: 10430
  #   channels:
  #     0: {{signal: "SPI_CLK", label: "CLK"}}
  #     1: {{signal: "SPI_MOSI", label: "MOSI"}}
  #
  # Sigrok-based logic analyzer (for fx2lafw, etc.):
  # logic_analyzer:
  #   type: sigrok
  #   driver: fx2lafw
  #   channels:
  #     0: {{signal: "SPI_CLK", label: "CLK"}}

tests: []
# tests:
#   - name: "PWM frequency verification"
#     description: "Verify PWM output is 1kHz"
#     criteria:
#       - source: oscilloscope.CH1
#         measurement: frequency
#         expected: 1000
#         unit: Hz
#         tolerance_percent: 5

git:
  enabled: true
  branch_prefix: "scopeloop/"
  auto_commit: false
  protect_branches:
    - main
    - master
  forbidden_operations:
    - force-push
    - rebase
    - reset --hard

safety:
  max_flash_attempts: 5
  flash_window_seconds: 60
  boot_success_marker: "BOOT_OK"
  boot_timeout_seconds: 10
  max_consecutive_build_failures: 5
"""

    with open(output_path, "w") as f:
        f.write(config_content)

    return output_path
