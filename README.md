# ScopeLoop

Hardware development automation platform for LLM coding agents.

ScopeLoop enables AI coding assistants like Claude Code to autonomously develop and debug embedded firmware by providing direct access to hardware tools, build systems, and measurement instruments.

## Features

- **MCP Integration**: Exposes hardware tools directly to Claude Code via Model Context Protocol
- **Build & Flash**: Automated firmware building (ESP-IDF) and flashing (esptool)
- **Serial Monitoring**: Capture and analyze serial output with timestamped logs
- **Oscilloscope Control**: Siglent SDS1000X-E integration for waveform capture and measurement
- **Logic Analyzer**: Saleae Logic 2 integration for digital signal analysis
- **Fixture Layer**: Model relays, Qwiic sensors, cameras, probes, and custom DUT actuation
- **Safety Guardrails**: Boot loop detection, circuit breakers, and rate limiting
- **Session Management**: Complete audit trail with artifacts and event timeline

## Installation

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev,all-instruments]"
```

## Quick Start

1. Initialize a project:
```bash
scopeloop init my-project --mcu esp32
```

2. Edit `scopeloop.yaml` to configure your devices and instruments.

3. Check status:
```bash
scopeloop status
```

4. Check whether this machine is ready to act as the instrument host:
```bash
scopeloop host status
```

5. List connected devices:
```bash
scopeloop devices
```

## Configuration

ScopeLoop uses a `scopeloop.yaml` file to configure your hardware setup:

```yaml
project:
  name: "My ESP32 Project"

runtime:
  mode: local
  host: null
  port: 8765
  data_dir: "~/ScopeLoop"

hardware:
  mcu: esp32
  board: esp32-devkitc

devices:
  dut:
    type: usb-serial
    match:
      vid: "0x10c4"
      pid: "0xea60"
    alias: "esp32-devkit"

flash:
  method: esptool
  device: dut
  baud: 460800

instruments:
  oscilloscope:
    type: siglent-sds1000x
    address: "192.0.2.100"
    probes:
      CH1:
        signal: "GPIO2 - PWM output"

fixtures:
  relay_card:
    type: relay-card
    connection:
      type: carrier-gpio
      controller: rp2350
      path: J5
    capabilities:
      - name: power_button
        kind: button
        channel: RELAY1
        signal: "Short DUT power-button pads"
        safe_state: open
        active_state: closed
```

## Instrument Host Modes

ScopeLoop treats the instrument host as the machine that physically owns the
USB instruments, serial ports, capture storage, and vendor applications.

Use `runtime.mode: local` while developing directly on a Mac or Windows
workstation. This is the default and is the right mode before a dedicated
hardware server is available. Later, the same project config can point at a
Linux hardware server without changing the project-level device and instrument
definitions.

ScopeLoop's supported hardware-host environment is a native Python virtualenv.
USB instruments and vendor GUI apps stay on the host OS.

Recommended local-first workflow:

```bash
.venv/bin/scopeloop host status
.venv/bin/scopeloop devices --verbose
```

For Saleae automation, install Logic 2, enable its automation server, and leave
the configured port at `10430` unless you have a reason to change it. For
PicoScope, install PicoScope 7 for GUI use; use PicoSDK or pyPicoSDK when you
want direct scripted captures.

See [docs/host-setup.md](docs/host-setup.md) for macOS, Windows, and dedicated
LattePanda Mu host setup notes.

## Fixture Layer

ScopeLoop separates calibrated bench instruments from physical test fixtures.
Instruments measure or generate signals. Fixtures manipulate and observe the
DUT's real-world state: relay-controlled button presses, battery disconnects,
fault injection, Qwiic/STEMMA QT sensors, temperature probes, light sensors,
thermal cameras, USB cameras, and small actuators.

Fixtures are described as capabilities in `scopeloop.yaml` so an agent can
generate reviewable test recipes such as "open the battery relay, press power,
wait for the status LED, capture thermal image, and verify recovery."

See [docs/hardware-spec.md](docs/hardware-spec.md) for the carrier board and
fixture expansion hardware direction.

## Architecture

```
Claude Code → MCP Server → Instrument Host → Hardware
                              ↓
                    ┌─────────┴─────────┐
                    │  Resource Manager │
                    │  Session Manager  │
                    │  Safety Guardrails│
                    └───────────────────┘
```

## CLI Commands

- `scopeloop init <name>` - Initialize new project
- `scopeloop status` - Show project status
- `scopeloop devices` - List connected devices
- `scopeloop find-device --vid <vid> --pid <pid>` - Find specific device
- `scopeloop sessions list` - List development sessions
- `scopeloop safety status` - Show safety guardrail status
- `scopeloop safety reset` - Reset safety guardrails

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Type checking
mypy src/scopeloop

# Linting
ruff check src/scopeloop
```

## License

MIT
