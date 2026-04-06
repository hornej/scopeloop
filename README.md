# ScopeLoop

Hardware development automation platform for LLM coding agents.

ScopeLoop enables AI coding assistants like Claude Code to autonomously develop and debug embedded firmware by providing direct access to hardware tools, build systems, and measurement instruments.

## Features

- **MCP Integration**: Exposes hardware tools directly to Claude Code via Model Context Protocol
- **Build & Flash**: Automated firmware building (ESP-IDF) and flashing (esptool)
- **Serial Monitoring**: Capture and analyze serial output with timestamped logs
- **Oscilloscope Control**: Siglent SDS1000X-E integration for waveform capture and measurement
- **Logic Analyzer**: Saleae Logic 2 integration for digital signal analysis
- **Safety Guardrails**: Boot loop detection, circuit breakers, and rate limiting
- **Session Management**: Complete audit trail with artifacts and event timeline

## Installation

```bash
pip install -e .
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

4. List connected devices:
```bash
scopeloop devices
```

## Configuration

ScopeLoop uses a `scopeloop.yaml` file to configure your hardware setup:

```yaml
project:
  name: "My ESP32 Project"

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
    address: "192.168.1.100"
    probes:
      CH1:
        signal: "GPIO2 - PWM output"
```

## Architecture

```
Claude Code → MCP Server → Control Plane Daemon → Hardware
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
