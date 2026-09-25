"""Configuration loader for scopeloop.yaml files."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class DeviceMatch(BaseModel):
    """USB device matching criteria."""

    vid: str | None = None  # Vendor ID (e.g., "0x10c4")
    pid: str | None = None  # Product ID (e.g., "0xea60")
    serial: str | None = None  # Device serial number (optional)
    by_id: str | None = None  # Stable Linux /dev/serial/by-id path


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
    signal_type: Literal[
        "unknown", "analog", "logic", "clock", "rail", "reset", "strap", "uart"
    ] = "unknown"
    digital: bool = True
    analog: bool = False
    requested_measurements: list[str] = Field(default_factory=list)
    electrical: LogicElectricalLimitsConfig | None = None


class LogicElectricalLimitsConfig(BaseModel):
    """Guaranteed electrical regions for interpreting an analog logic signal."""

    input_low_max_v: float | None = None
    input_high_min_v: float | None = None
    output_low_max_v: float | None = None
    output_high_min_v: float | None = None

    @model_validator(mode="after")
    def validate_regions(self) -> LogicElectricalLimitsConfig:
        if (
            self.input_low_max_v is not None
            and self.input_high_min_v is not None
            and self.input_low_max_v >= self.input_high_min_v
        ):
            raise ValueError("input_low_max_v must be below input_high_min_v")
        if (
            self.output_low_max_v is not None
            and self.output_high_min_v is not None
            and self.output_low_max_v >= self.output_high_min_v
        ):
            raise ValueError("output_low_max_v must be below output_high_min_v")
        return self


class LogicTriggerConfig(BaseModel):
    """Edge-triggered capture window and bounded arm time."""

    channel: int = Field(ge=0)
    edge: Literal["rising", "falling"] = "rising"
    pre_trigger_seconds: float = Field(default=0.0, ge=0)
    post_trigger_seconds: float = Field(default=1.0, gt=0)
    timeout_seconds: float = Field(default=30.0, gt=0)


class LogicUartAnalyzerConfig(BaseModel):
    """Saleae Async Serial analyzer configuration."""

    name: str
    channel: int = Field(ge=0)
    baud_rate: int = Field(default=115200, gt=0)
    bits_per_frame: int = Field(default=8, ge=5, le=9)
    stop_bits: Literal[1.0, 1.5, 2.0] = 1.0
    parity: Literal["None", "Even", "Odd"] = "None"
    bit_order: Literal["least_significant_first", "most_significant_first"] = (
        "least_significant_first"
    )
    inverted: bool = False
    radix: Literal["hexadecimal", "decimal", "binary", "ascii"] = "hexadecimal"


class LogicComparisonConfig(BaseModel):
    """Default alignment and signal selection for reference comparisons."""

    align_channel: int = Field(ge=0)
    edge: Literal["rising", "falling"] = "rising"
    threshold_v: float | None = None
    signals: list[int] = Field(default_factory=list)


class LogicCaptureRecipeConfig(BaseModel):
    """Reproducible Saleae capture configuration."""

    description: str | None = None
    digital_channels: list[int] = Field(default_factory=list)
    analog_channels: list[int] = Field(default_factory=list)
    digital_sample_rate: int | None = Field(default=None, gt=0)
    analog_sample_rate: int | None = Field(default=None, gt=0)
    logic_family_volts: Literal[1.2, 1.8, 3.3] | None = 3.3
    duration_seconds: float | None = Field(default=0.1, gt=0)
    trigger: LogicTriggerConfig | None = None
    uart: list[LogicUartAnalyzerConfig] = Field(default_factory=list)
    required_metadata: list[str] = Field(default_factory=list)
    comparison: LogicComparisonConfig | None = None

    @field_validator("digital_channels", "analog_channels")
    @classmethod
    def unique_channels(cls, channels: list[int]) -> list[int]:
        if any(channel < 0 for channel in channels):
            raise ValueError("channel numbers must be non-negative")
        if len(channels) != len(set(channels)):
            raise ValueError("channel numbers must be unique")
        return channels

    @model_validator(mode="after")
    def validate_capture(self) -> LogicCaptureRecipeConfig:
        if not self.digital_channels and not self.analog_channels:
            raise ValueError("at least one digital or analog channel is required")
        if self.digital_channels and self.digital_sample_rate is None:
            raise ValueError("digital_sample_rate is required for digital channels")
        if self.analog_channels and self.analog_sample_rate is None:
            raise ValueError("analog_sample_rate is required for analog channels")
        if self.trigger and self.trigger.channel not in self.digital_channels:
            raise ValueError("trigger channel must be enabled as a digital channel")
        if self.trigger is None and self.duration_seconds is None:
            raise ValueError("duration_seconds is required for a timed capture")
        for uart in self.uart:
            if uart.channel not in self.digital_channels:
                raise ValueError(f"UART channel {uart.channel} is not enabled")
        return self


class LogicAnalyzerConfig(BaseModel):
    """Logic analyzer configuration."""

    type: str  # "saleae" or "sigrok"
    driver: str | None = None  # sigrok driver name (for sigrok type)
    device_id: str | None = None  # Saleae device ID (for saleae type)
    port: int = 10430  # Logic 2 automation port (for saleae type)
    connection: str | None = None  # sigrok connection string (for sigrok type)
    channels: dict[int, LogicChannelConfig] = Field(default_factory=dict)
    recipes: dict[str, LogicCaptureRecipeConfig] = Field(default_factory=dict)


class InstrumentsConfig(BaseModel):
    """All instrument configurations."""

    oscilloscope: OscilloscopeConfig | None = None
    logic_analyzer: LogicAnalyzerConfig | None = None


class FixtureConnectionConfig(BaseModel):
    """How a test fixture module connects to the ScopeLoop host or carrier."""

    type: str  # e.g., "carrier-gpio", "qwiic", "usb", "ethernet", "one-wire"
    controller: str | None = None  # e.g., "rp2350", "host", "fixture-hub"
    path: str | None = None  # e.g., "J5", "/dev/i2c-1", "/dev/ttyACM0"
    address: str | int | None = None  # e.g., I2C address, IP address, serial number
    voltage: str | None = None  # e.g., "3.3V", "5V tolerant"
    notes: str | None = None


class FixtureCapabilityConfig(BaseModel):
    """One controllable or observable capability exposed by a module."""

    name: str  # e.g., "power_button", "battery_disconnect", "ambient_temperature"
    kind: str  # e.g., "relay", "button", "sensor", "camera", "actuator"
    channel: str | None = None  # e.g., "RELAY1", "GPIO3", "mlx90640"
    direction: str = "output"  # "input", "output", or "bidirectional"
    signal: str | None = None  # What this capability touches or observes on the DUT
    unit: str | None = None  # Measurement unit for sensors
    safe_state: str | None = None  # e.g., "open", "off", "high-z"
    active_state: str | None = None  # e.g., "closed", "on", "low"
    limits: dict[str, float | int | str] = Field(default_factory=dict)
    notes: str | None = None


class FixtureModuleConfig(BaseModel):
    """Physical fixture/probe module attached around the DUT."""

    type: str  # e.g., "relay-card", "qwiic-sensor", "thermal-camera"
    description: str | None = None
    connection: FixtureConnectionConfig
    capabilities: list[FixtureCapabilityConfig] = Field(default_factory=list)
    optional: bool = True
    safety_notes: list[str] = Field(default_factory=list)
    calibration: dict[str, str] = Field(default_factory=dict)


class ExpansionModuleRailConfig(BaseModel):
    """Power rail required or used by an adapter card."""

    name: str  # e.g., "3v3", "5v", "12v", "-12v"
    voltage: str  # Keep string to support ranges like "+/-12V" or "5V"
    max_current_a: float | None = None
    optional: bool = False
    purpose: str | None = None  # e.g., "logic", "relay coils", "analog output stage"


class ExpansionModuleInterfaceConfig(BaseModel):
    """Data, control, clock, or analog interface used by an adapter card."""

    name: str  # e.g., "usb2", "i2c0", "trigger_in", "analog_ref"
    type: str  # e.g., "usb2", "usb3", "i2c", "gpio", "trigger", "analog", "clock"
    direction: str = "bidirectional"  # "input", "output", or "bidirectional"
    optional: bool = False
    purpose: str | None = None


class ExpansionModuleManifestConfig(BaseModel):
    """Self-description for a user-buildable ScopeLoop adapter card."""

    module_id: str  # Stable identifier, e.g., "scopeloop.relay-card.4ch"
    name: str
    vendor: str | None = None
    version: str | None = None
    hardware_revision: str | None = None
    slot_class: str  # e.g., "usb", "control", "sensor", "robust-fixture"
    electrical_interface: str  # e.g., "usb2", "usb3", "i2c", "usb2+gpio"
    open_hardware: bool = False
    source_url: str | None = None
    license: str | None = None
    power_budget_w: float | None = None
    rails: list[ExpansionModuleRailConfig] = Field(default_factory=list)
    interfaces: list[ExpansionModuleInterfaceConfig] = Field(default_factory=list)
    requires_safe_state: bool = True
    capabilities: list[FixtureCapabilityConfig] = Field(default_factory=list)


class ExpansionModuleConfig(BaseModel):
    """Installed ScopeLoop module-bay adapter card."""

    slot: str  # e.g., "M1", "front-left", "sensor-0"
    type: str  # e.g., "relay-card", "usb-a-card", "qwiic-card"
    manifest: ExpansionModuleManifestConfig
    connection: FixtureConnectionConfig | None = None
    optional: bool = True
    notes: str | None = None


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


class RuntimeConfig(BaseModel):
    """Where ScopeLoop hardware access runs."""

    mode: str = "local"  # "local" today; future: "remote"
    host: str | None = None
    port: int = 8765
    data_dir: str = "~/ScopeLoop"


class Config(BaseModel):
    """Root configuration for scopeloop.yaml."""

    project: ProjectConfig
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    hardware: HardwareConfig
    build: BuildConfig
    devices: dict[str, DeviceConfig] = Field(default_factory=dict)
    flash: FlashConfig | None = None
    serial: SerialConfig | None = None
    instruments: InstrumentsConfig = Field(default_factory=InstrumentsConfig)
    modules: dict[str, ExpansionModuleConfig] = Field(default_factory=dict)
    fixtures: dict[str, FixtureModuleConfig] = Field(default_factory=dict)
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
    path = find_config_file() if path is None else Path(path)

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

    raise FileNotFoundError(f"No scopeloop.yaml found in {start_dir} or parent directories")


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
# See README.md for documentation

project:
  name: "{project_name}"
  description: ""

# The instrument host is the machine with USB instruments attached.
# Use local while developing directly on your Mac; later this same config can
# point at a dedicated Linux hardware server.
runtime:
  mode: local
  host: null
  port: 8765
  data_dir: "~/ScopeLoop"

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
  #   connection: "tcp-raw/192.0.2.100/5025"
  #   probes:
  #     CH1:
  #       signal: "GPIO2 - PWM output"
  #       coupling: DC
  #       scale: "1V/div"
  #
  # Legacy direct SCPI (deprecated):
  # oscilloscope:
  #   type: siglent-sds1000x
  #   address: "192.0.2.100"

  logic_analyzer: null
  # Saleae Logic 2 (recommended for Saleae devices):
  # logic_analyzer:
  #   type: saleae
  #   port: 10430
  #   channels:
  #     0: {{signal: "RESETn", label: "reset", signal_type: reset,
  #         digital: true, analog: true}}
  #     1: {{signal: "UART_TX", label: "boot_uart", signal_type: uart}}
  #   recipes:
  #     boot:
  #       digital_channels: [0, 1]
  #       analog_channels: [0]
  #       digital_sample_rate: 6250000
  #       analog_sample_rate: 781250
  #       logic_family_volts: 3.3
  #       trigger: {{channel: 0, edge: rising, pre_trigger_seconds: 0.25,
  #                 post_trigger_seconds: 0.75, timeout_seconds: 30}}
  #       uart:
  #         - {{name: boot_uart, channel: 1, baud_rate: 115200}}
  #       required_metadata: [serial, board_revision, fixture_state]
  #
  # Sigrok-based logic analyzer (for fx2lafw, etc.):
  # logic_analyzer:
  #   type: sigrok
  #   driver: fx2lafw
  #   channels:
  #     0: {{signal: "SPI_CLK", label: "CLK"}}

modules: {{}}
# Optional ScopeLoop module-bay adapter cards. These are intended to support
# user-built cards, so each card should expose a manifest that describes the
# slot class, electrical interface, power budget, safe-state requirements, and
# capabilities it contributes.
# modules:
#   relay_card_m1:
#     slot: M1
#     type: relay-card
#     connection:
#       type: module-bay
#       controller: rp2350
#       path: M1
#     manifest:
#       module_id: "scopeloop.relay-card.4ch"
#       name: "4-Channel Relay Card"
#       vendor: "ScopeLoop"
#       hardware_revision: "A"
#       slot_class: control
#       electrical_interface: "usb2+gpio"
#       open_hardware: true
#       source_url: "https://github.com/example/scopeloop-relay-card"
#       license: "CERN-OHL-S-2.0"
#       power_budget_w: 2.5
#       rails:
#         - name: 5v
#           voltage: "5V"
#           max_current_a: 0.3
#           purpose: "relay coils"
#         - name: 3v3
#           voltage: "3.3V"
#           max_current_a: 0.05
#           purpose: "logic"
#       interfaces:
#         - name: usb2
#           type: usb2
#           purpose: "module identification and control"
#         - name: trigger_out
#           type: trigger
#           direction: output
#           optional: true
#           purpose: "timestamped relay action marker"
#       requires_safe_state: true
#       capabilities:
#         - name: relay_1
#           kind: relay
#           channel: RELAY1
#           direction: output
#           safe_state: open
#           active_state: closed
#           limits:
#             max_voltage_v: 24
#             max_current_a: 2

fixtures: {{}}
# Optional fixture-layer tools around the DUT. These are not calibrated bench
# instruments; they are physical actuation/probe capabilities ScopeLoop can
# reason about in test recipes.
# fixtures:
#   relay_card:
#     type: relay-card
#     description: "Low-voltage relay outputs for button press and disconnect tests"
#     connection:
#       type: carrier-gpio
#       controller: rp2350
#       path: J5
#       voltage: "3.3V control"
#     capabilities:
#       - name: power_button
#         kind: button
#         channel: RELAY1
#         direction: output
#         signal: "Short DUT power-button pads"
#         safe_state: open
#         active_state: closed
#       - name: battery_disconnect
#         kind: relay
#         channel: RELAY2
#         direction: output
#         signal: "DUT battery positive lead"
#         safe_state: closed
#         active_state: open
#         limits:
#           max_voltage_v: 24
#           max_current_a: 2
#   qwiic_environment:
#     type: qwiic-sensor-chain
#     connection:
#       type: qwiic
#       controller: rp2350
#       path: QWIIC0
#     capabilities:
#       - name: ambient_temperature
#         kind: temp-probe
#         direction: input
#         unit: degC
#       - name: status_led_light
#         kind: light-sensor
#         direction: input
#         unit: lux
#   thermal_camera:
#     type: flir-or-usb-thermal-camera
#     connection:
#       type: usb
#       controller: host
#     capabilities:
#       - name: board_thermal_image
#         kind: thermal-camera
#         direction: input
#         unit: degC

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
