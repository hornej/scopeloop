"""ScopeLoop MCP Server - Exposes hardware tools to Claude Code.

This module implements the Model Context Protocol (MCP) server that exposes
ScopeLoop's hardware automation capabilities as tools that Claude Code can use.

Run with: python -m scopeloop.mcp_server
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from scopeloop.comparison import compare_bundles
from scopeloop.config import (
    Config,
    LogicUartAnalyzerConfig,
    create_default_config,
    load_config,
)
from scopeloop.devices import DeviceManager, DeviceMatch
from scopeloop.logic import LogicCaptureService
from scopeloop.resources import ResourceManager, generate_client_id
from scopeloop.safety import SafetyConfig, SafetyGuardrails
from scopeloop.scope import create_scope_from_config, parse_si_value
from scopeloop.session import SessionManager

logger = logging.getLogger(__name__)

# MCP Server instance
server = Server("scopeloop")

# Global state (initialized on startup)
_config: Config | None = None
_resource_manager: ResourceManager | None = None
_device_manager: DeviceManager | None = None
_session_manager: SessionManager | None = None
_safety: SafetyGuardrails | None = None
_logic_service: LogicCaptureService | None = None
_client_id: str = generate_client_id("mcp")


def _get_config() -> Config | None:
    """Get the current configuration."""
    return _config


def _get_resource_manager() -> ResourceManager:
    """Get the resource manager."""
    if _resource_manager is None:
        raise RuntimeError("Resource manager not initialized")
    return _resource_manager


def _get_device_manager() -> DeviceManager:
    """Get the device manager."""
    if _device_manager is None:
        raise RuntimeError("Device manager not initialized")
    return _device_manager


def _get_safety() -> SafetyGuardrails:
    """Get safety guardrails."""
    if _safety is None:
        raise RuntimeError("Safety guardrails not initialized")
    return _safety


def _get_logic_service() -> LogicCaptureService:
    """Get or lazily initialize the persistent MCP logic workflow."""
    global _logic_service
    if _logic_service is None:
        config = _get_config()
        if not config or not config.instruments.logic_analyzer:
            raise RuntimeError("No logic analyzer configured in scopeloop.yaml")
        _logic_service = LogicCaptureService(config.instruments.logic_analyzer)
    return _logic_service


# ============================================================================
# Tool Definitions
# ============================================================================

TOOLS = [
    Tool(
        name="scopeloop_status",
        description=(
            "Get the current status of ScopeLoop including project info, connected "
            "devices, and instrument status."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="scopeloop_init",
        description="Initialize a new ScopeLoop project with a scopeloop.yaml configuration file.",
        inputSchema={
            "type": "object",
            "properties": {
                "project_name": {
                    "type": "string",
                    "description": "Name of the project",
                },
                "mcu": {
                    "type": "string",
                    "description": "Target MCU type (default: esp32)",
                    "default": "esp32",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Path where to create scopeloop.yaml (default: current directory)"
                    ),
                },
            },
            "required": ["project_name"],
        },
    ),
    Tool(
        name="scopeloop_devices_list",
        description="List all connected USB serial devices with their VID/PID information.",
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="scopeloop_device_find",
        description="Find a specific USB serial device by VID, PID, or serial number.",
        inputSchema={
            "type": "object",
            "properties": {
                "vid": {
                    "type": "string",
                    "description": "Vendor ID to search for (e.g., '10c4' or '0x10c4')",
                },
                "pid": {
                    "type": "string",
                    "description": "Product ID to search for",
                },
                "serial": {
                    "type": "string",
                    "description": "Serial number to search for",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="scopeloop_build",
        description=(
            "Build the firmware project using the build system configured in scopeloop.yaml."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "clean": {
                    "type": "boolean",
                    "description": "Whether to clean before building",
                    "default": False,
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="scopeloop_flash",
        description="Flash the firmware to the target device. Requires a successful build first.",
        inputSchema={
            "type": "object",
            "properties": {
                "verify": {
                    "type": "boolean",
                    "description": "Whether to verify after flashing",
                    "default": True,
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="scopeloop_build_and_flash",
        description="Build the firmware and flash it to the device in one operation.",
        inputSchema={
            "type": "object",
            "properties": {
                "clean": {
                    "type": "boolean",
                    "description": "Whether to clean before building",
                    "default": False,
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="scopeloop_serial_start",
        description="Start the serial monitor to capture device output.",
        inputSchema={
            "type": "object",
            "properties": {
                "baud": {
                    "type": "integer",
                    "description": "Baud rate (uses config default if not specified)",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="scopeloop_serial_read",
        description="Read recent output from the serial monitor.",
        inputSchema={
            "type": "object",
            "properties": {
                "lines": {
                    "type": "integer",
                    "description": "Number of recent lines to return",
                    "default": 50,
                },
                "since_marker": {
                    "type": "string",
                    "description": "Return lines since this marker was seen",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="scopeloop_serial_send",
        description="Send data to the device over the serial connection.",
        inputSchema={
            "type": "object",
            "properties": {
                "data": {
                    "type": "string",
                    "description": "Data to send",
                },
                "newline": {
                    "type": "boolean",
                    "description": "Whether to append newline",
                    "default": True,
                },
            },
            "required": ["data"],
        },
    ),
    Tool(
        name="scopeloop_serial_stop",
        description="Stop the serial monitor.",
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="scopeloop_scope_connect",
        description="Connect to the oscilloscope configured in scopeloop.yaml.",
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="scopeloop_scope_capture",
        description="Acquire fresh scope evidence with setup, waveform, measurements and hashes.",
        inputSchema={
            "type": "object",
            "properties": {
                "recipe": {
                    "type": "object",
                    "description": "ScopeRecipe from docs/scope-capture.md",
                },
                "output": {"type": "string", "description": "New evidence directory"},
            },
            "required": ["recipe", "output"],
        },
    ),
    Tool(
        name="scopeloop_scope_measure",
        description="Take a measurement from the oscilloscope.",
        inputSchema={
            "type": "object",
            "properties": {
                "channel": {
                    "type": "string",
                    "description": "Channel to measure (e.g., 'CH1')",
                },
                "measurement": {
                    "type": "string",
                    "description": (
                        "Measurement type: frequency, period, amplitude, vpp, vmax, "
                        "vmin, vrms, rise_time, fall_time, duty_cycle"
                    ),
                },
            },
            "required": ["channel", "measurement"],
        },
    ),
    Tool(
        name="scopeloop_scope_configure",
        description="Configure oscilloscope settings.",
        inputSchema={
            "type": "object",
            "properties": {
                "channel": {
                    "type": "string",
                    "description": "Channel to configure",
                },
                "scale": {
                    "type": "string",
                    "description": "Vertical scale (e.g., '1V', '500mV')",
                },
                "timebase": {
                    "type": "string",
                    "description": "Horizontal timebase (e.g., '1ms', '100us')",
                },
                "trigger_level": {
                    "type": "number",
                    "description": "Trigger level in volts",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="scopeloop_logic_connect",
        description="Connect to the logic analyzer configured in scopeloop.yaml.",
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="scopeloop_logic_disconnect",
        description="Close owned captures and release this MCP client's Logic lease.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="scopeloop_logic_capture",
        description="Run a configured logic capture recipe and save an evidence bundle.",
        inputSchema={
            "type": "object",
            "properties": {
                "recipe": {
                    "type": "string",
                    "description": "Capture recipe name from scopeloop.yaml",
                },
                "metadata": {
                    "type": "object",
                    "description": "Arbitrary DUT and fixture run metadata",
                    "additionalProperties": True,
                },
                "output_root": {"type": "string"},
                "native_labels": {"type": "boolean", "default": False},
            },
            "required": ["recipe", "metadata"],
        },
    ),
    Tool(
        name="scopeloop_logic_decode",
        description="Decode a protocol from logic analyzer capture.",
        inputSchema={
            "type": "object",
            "properties": {
                "protocol": {
                    "type": "string",
                    "description": "Protocol to decode (UART in this increment)",
                },
                "capture_id": {"type": "string"},
                "channel": {"type": "integer"},
                "baud_rate": {"type": "integer", "default": 115200},
                "name": {"type": "string", "default": "uart"},
                "output": {"type": "string"},
            },
            "required": ["protocol", "capture_id", "channel", "output"],
        },
    ),
    Tool(
        name="scopeloop_logic_save",
        description="Save an active MCP capture to a .sal path.",
        inputSchema={
            "type": "object",
            "properties": {
                "capture_id": {"type": "string"},
                "output": {"type": "string"},
            },
            "required": ["capture_id", "output"],
        },
    ),
    Tool(
        name="scopeloop_logic_export",
        description="Export raw CSV files from an active MCP capture.",
        inputSchema={
            "type": "object",
            "properties": {
                "capture_id": {"type": "string"},
                "output_dir": {"type": "string"},
                "digital_channels": {"type": "array", "items": {"type": "integer"}},
                "analog_channels": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["capture_id", "output_dir"],
        },
    ),
    Tool(
        name="scopeloop_logic_compare",
        description="Edge-align and compare known-good and DUT evidence bundles.",
        inputSchema={
            "type": "object",
            "properties": {
                "reference_bundle": {"type": "string"},
                "dut_bundle": {"type": "string"},
                "align_channel": {"type": "integer"},
                "edge": {"type": "string", "enum": ["rising", "falling"]},
                "threshold_v": {"type": "number"},
                "signals": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["reference_bundle", "dut_bundle", "align_channel"],
        },
    ),
    Tool(
        name="scopeloop_test_run",
        description="Run tests defined in scopeloop.yaml.",
        inputSchema={
            "type": "object",
            "properties": {
                "test_name": {
                    "type": "string",
                    "description": "Specific test to run (runs all if not specified)",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="scopeloop_test_report",
        description="Generate a test report from the most recent test run.",
        inputSchema={
            "type": "object",
            "properties": {
                "format": {
                    "type": "string",
                    "description": "Report format: json, markdown",
                    "default": "markdown",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="scopeloop_safety_status",
        description=(
            "Get the status of safety guardrails (boot loop detection, build failures, etc.)."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="scopeloop_safety_reset",
        description="Reset safety guardrails if they have been triggered.",
        inputSchema={
            "type": "object",
            "properties": {
                "guardrail": {
                    "type": "string",
                    "description": (
                        "Specific guardrail to reset: boot_loop, build_failure, "
                        "flash_failure (resets all if not specified)"
                    ),
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="scopeloop_user_action",
        description=(
            "Request a physical action from the user (e.g., connect a probe, press a button)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Description of the action needed",
                },
                "wait_for_confirmation": {
                    "type": "boolean",
                    "description": "Whether to wait for user confirmation",
                    "default": True,
                },
            },
            "required": ["action"],
        },
    ),
    Tool(
        name="scopeloop_session_start",
        description="Start a new development session to track iterations and artifacts.",
        inputSchema={
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Description of the session goals",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="scopeloop_session_end",
        description="End the current development session.",
        inputSchema={
            "type": "object",
            "properties": {
                "outcome": {
                    "type": "string",
                    "description": "Session outcome: success, failure, interrupted",
                    "default": "success",
                },
            },
            "required": [],
        },
    ),
]


# ============================================================================
# Tool Handlers
# ============================================================================


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Handle tool calls."""
    try:
        result = await _handle_tool(name, arguments)
        return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]
    except Exception as e:
        logger.exception(f"Error handling tool {name}")
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": str(e),
                        "tool": name,
                    }
                ),
            )
        ]


async def _handle_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Route tool calls to handlers."""

    # Status and init tools
    if name == "scopeloop_status":
        return await _handle_status()
    elif name == "scopeloop_init":
        return await _handle_init(args)

    # Device tools
    elif name == "scopeloop_devices_list":
        return await _handle_devices_list()
    elif name == "scopeloop_device_find":
        return await _handle_device_find(args)

    # Build/flash tools
    elif name == "scopeloop_build":
        return await _handle_build(args)
    elif name == "scopeloop_flash":
        return await _handle_flash(args)
    elif name == "scopeloop_build_and_flash":
        return await _handle_build_and_flash(args)

    # Serial tools
    elif name == "scopeloop_serial_start":
        return await _handle_serial_start(args)
    elif name == "scopeloop_serial_read":
        return await _handle_serial_read(args)
    elif name == "scopeloop_serial_send":
        return await _handle_serial_send(args)
    elif name == "scopeloop_serial_stop":
        return await _handle_serial_stop()

    # Oscilloscope tools
    elif name == "scopeloop_scope_connect":
        return await _handle_scope_connect()
    elif name == "scopeloop_scope_capture":
        return await _handle_scope_capture(args)
    elif name == "scopeloop_scope_measure":
        return await _handle_scope_measure(args)
    elif name == "scopeloop_scope_configure":
        return await _handle_scope_configure(args)

    # Logic analyzer tools
    elif name == "scopeloop_logic_connect":
        return await _handle_logic_connect()
    elif name == "scopeloop_logic_disconnect":
        await _get_logic_service().disconnect(close_captures=True)
        return {"connected": False}
    elif name == "scopeloop_logic_capture":
        return await _handle_logic_capture(args)
    elif name == "scopeloop_logic_decode":
        return await _handle_logic_decode(args)
    elif name == "scopeloop_logic_save":
        return await _handle_logic_save(args)
    elif name == "scopeloop_logic_export":
        return await _handle_logic_export(args)
    elif name == "scopeloop_logic_compare":
        return await _handle_logic_compare(args)

    # Test tools
    elif name == "scopeloop_test_run":
        return await _handle_test_run(args)
    elif name == "scopeloop_test_report":
        return await _handle_test_report(args)

    # Safety tools
    elif name == "scopeloop_safety_status":
        return await _handle_safety_status()
    elif name == "scopeloop_safety_reset":
        return await _handle_safety_reset(args)

    # User interaction
    elif name == "scopeloop_user_action":
        return await _handle_user_action(args)

    # Session tools
    elif name == "scopeloop_session_start":
        return await _handle_session_start(args)
    elif name == "scopeloop_session_end":
        return await _handle_session_end(args)

    else:
        return {"error": f"Unknown tool: {name}"}


# ============================================================================
# Tool Implementations
# ============================================================================


async def _handle_status() -> dict[str, Any]:
    """Handle scopeloop_status tool."""
    config = _get_config()

    result: dict[str, Any] = {
        "initialized": config is not None,
    }

    if config:
        result["project"] = {
            "name": config.project.name,
            "mcu": config.hardware.mcu,
            "board": config.hardware.board,
            "build_system": config.build.system,
        }

        # Device status
        device_manager = _get_device_manager()
        devices = []
        for _name, device_config in config.devices.items():
            device = await device_manager.find_device(
                DeviceMatch(
                    vid=device_config.match.vid,
                    pid=device_config.match.pid,
                    serial=device_config.match.serial,
                    by_id=device_config.match.by_id,
                )
            )
            devices.append(
                {
                    "alias": device_config.alias,
                    "connected": device is not None,
                    "port": device.port if device else None,
                }
            )
        result["devices"] = devices

        # Instrument status
        instruments = {}
        if config.instruments.oscilloscope:
            instruments["oscilloscope"] = {
                "type": config.instruments.oscilloscope.type,
                "address": config.instruments.oscilloscope.address,
                "connected": False,  # TODO: Check actual connection
            }
        if config.instruments.logic_analyzer:
            instruments["logic_analyzer"] = {
                "type": config.instruments.logic_analyzer.type,
                "connected": False,  # TODO: Check actual connection
            }
        result["instruments"] = instruments

        # Safety status
        safety = _get_safety()
        result["safety"] = {
            "any_blocked": safety.is_any_blocked(),
        }

    return result


async def _handle_init(args: dict[str, Any]) -> dict[str, Any]:
    """Handle scopeloop_init tool."""
    global _config, _logic_service

    project_name = args["project_name"]
    mcu = args.get("mcu", "esp32")
    path = Path(args.get("path", ".")) / "scopeloop.yaml"

    config_path = create_default_config(project_name, mcu, path)

    # Reload config
    _config = load_config(config_path)
    _logic_service = None

    return {
        "success": True,
        "config_path": str(config_path),
        "message": f"Created scopeloop.yaml for project '{project_name}'",
    }


async def _handle_devices_list() -> dict[str, Any]:
    """Handle scopeloop_devices_list tool."""
    device_manager = _get_device_manager()
    devices = await device_manager.list_devices()

    return {
        "devices": [
            {
                "port": d.port,
                "vid": d.vid,
                "pid": d.pid,
                "serial": d.serial,
                "product": d.product,
                "manufacturer": d.manufacturer,
            }
            for d in devices
        ]
    }


async def _handle_device_find(args: dict[str, Any]) -> dict[str, Any]:
    """Handle scopeloop_device_find tool."""
    device_manager = _get_device_manager()
    device = await device_manager.find_device(
        DeviceMatch(
            vid=args.get("vid"),
            pid=args.get("pid"),
            serial=args.get("serial"),
            by_id=args.get("by_id"),
        )
    )

    if device:
        return {
            "found": True,
            "port": device.port,
            "vid": device.vid,
            "pid": device.pid,
            "serial": device.serial,
        }
    else:
        return {"found": False}


async def _handle_build(args: dict[str, Any]) -> dict[str, Any]:
    """Handle scopeloop_build tool."""
    config = _get_config()
    if not config:
        return {"error": "No scopeloop.yaml found. Run scopeloop_init first."}

    safety = _get_safety()
    allowed, message = await safety.check_build_allowed()
    if not allowed:
        return {"error": f"Build blocked by safety guardrails: {message}"}

    # TODO: Implement actual build using BuildSystem
    return {
        "status": "not_implemented",
        "message": "Build system integration not yet implemented. Use 'idf.py build' directly.",
        "build_system": config.build.system,
        "project_path": config.build.project_path,
    }


async def _handle_flash(args: dict[str, Any]) -> dict[str, Any]:
    """Handle scopeloop_flash tool."""
    config = _get_config()
    if not config:
        return {"error": "No scopeloop.yaml found. Run scopeloop_init first."}

    if not config.flash:
        return {"error": "No flash configuration in scopeloop.yaml"}

    safety = _get_safety()
    allowed, message = await safety.check_flash_allowed()
    if not allowed:
        return {"error": f"Flash blocked by safety guardrails: {message}"}

    safety.record_flash_attempt()

    # TODO: Implement actual flash using Flasher
    return {
        "status": "not_implemented",
        "message": "Flash integration not yet implemented. Use 'idf.py flash' directly.",
        "method": config.flash.method,
        "device": config.flash.device,
    }


async def _handle_build_and_flash(args: dict[str, Any]) -> dict[str, Any]:
    """Handle scopeloop_build_and_flash tool."""
    build_result = await _handle_build(args)
    if "error" in build_result:
        return build_result

    flash_result = await _handle_flash({"verify": True})
    return {
        "build": build_result,
        "flash": flash_result,
    }


async def _handle_serial_start(args: dict[str, Any]) -> dict[str, Any]:
    """Handle scopeloop_serial_start tool."""
    # TODO: Implement serial monitor
    return {
        "status": "not_implemented",
        "message": "Serial monitor not yet implemented",
    }


async def _handle_serial_read(args: dict[str, Any]) -> dict[str, Any]:
    """Handle scopeloop_serial_read tool."""
    # TODO: Implement serial read
    return {
        "status": "not_implemented",
        "message": "Serial monitor not yet implemented",
    }


async def _handle_serial_send(args: dict[str, Any]) -> dict[str, Any]:
    """Handle scopeloop_serial_send tool."""
    # TODO: Implement serial send
    return {
        "status": "not_implemented",
        "message": "Serial monitor not yet implemented",
    }


async def _handle_serial_stop() -> dict[str, Any]:
    """Handle scopeloop_serial_stop tool."""
    # TODO: Implement serial stop
    return {
        "status": "not_implemented",
        "message": "Serial monitor not yet implemented",
    }


async def _handle_scope_connect() -> dict[str, Any]:
    """Handle scopeloop_scope_connect tool."""
    config = _get_config()
    if not config or not config.instruments.oscilloscope:
        return {"error": "No oscilloscope configured in scopeloop.yaml"}

    try:
        scope = create_scope_from_config(config)
        async with scope:
            info = await scope.get_info()
            return {
                "status": "identified",
                "connected": False,
                "type": info.instrument_type,
                "model": info.model,
                "serial": info.serial,
                "firmware_version": info.firmware_version,
                "address": info.address,
            }
    except Exception as e:
        return {"error": str(e)}


async def _handle_scope_capture(args: dict[str, Any]) -> dict[str, Any]:
    """Handle scopeloop_scope_capture tool."""
    config = _get_config()
    if not config or not config.instruments.oscilloscope:
        return {"error": "No oscilloscope configured in scopeloop.yaml"}

    try:
        scope = create_scope_from_config(config)
        async with scope:
            from scopeloop.scope_evidence import ScopeRecipe, capture_scope_evidence

            recipe = ScopeRecipe.model_validate(args["recipe"])
            return await capture_scope_evidence(scope, recipe, Path(args["output"]))
    except Exception as e:
        return {"error": str(e)}


async def _handle_scope_measure(args: dict[str, Any]) -> dict[str, Any]:
    """Handle scopeloop_scope_measure tool."""
    config = _get_config()
    if not config or not config.instruments.oscilloscope:
        return {"error": "No oscilloscope configured in scopeloop.yaml"}

    try:
        scope = create_scope_from_config(config)
        async with scope:
            measurement = await scope.measure(
                args.get("channel", "CH1"),
                args["measurement"],
            )
            return {
                "status": "measured",
                "measurement": measurement.to_dict(),
            }
    except Exception as e:
        return {"error": str(e)}


async def _handle_scope_configure(args: dict[str, Any]) -> dict[str, Any]:
    """Handle scopeloop_scope_configure tool."""
    config = _get_config()
    if not config or not config.instruments.oscilloscope:
        return {"error": "No oscilloscope configured in scopeloop.yaml"}

    channel = args.get("channel", "CH1")
    applied: dict[str, Any] = {}

    try:
        scope = create_scope_from_config(config)
        async with scope:
            if args.get("scale") is not None:
                scale = parse_si_value(args["scale"])
                await scope.set_channel_scale(channel, scale)
                applied["scale"] = scale
            if args.get("timebase") is not None:
                timebase = parse_si_value(args["timebase"])
                await scope.set_timebase(timebase)
                applied["timebase"] = timebase
            if args.get("trigger_level") is not None:
                trigger_level = float(args["trigger_level"])
                await scope.set_trigger_level(trigger_level, source=channel)
                applied["trigger_level"] = trigger_level

        return {
            "status": "configured",
            "channel": channel,
            "applied": applied,
        }
    except Exception as e:
        return {"error": str(e)}


async def _handle_logic_connect() -> dict[str, Any]:
    """Handle scopeloop_logic_connect tool."""
    return await _get_logic_service().connect()


async def _handle_logic_capture(args: dict[str, Any]) -> dict[str, Any]:
    """Handle scopeloop_logic_capture tool."""
    config = _get_config()
    if config is None:
        return {"error": "No scopeloop.yaml found"}
    output_root = Path(
        args.get("output_root") or (Path(config.runtime.data_dir).expanduser() / "captures")
    )
    bundle = await _get_logic_service().capture_evidence(
        args["recipe"],
        args.get("metadata", {}),
        output_root,
        **({"native_labels": True} if args.get("native_labels") else {}),
    )
    return bundle.to_dict()


async def _handle_logic_decode(args: dict[str, Any]) -> dict[str, Any]:
    """Handle scopeloop_logic_decode tool."""
    if args["protocol"].lower() != "uart":
        return {
            "error": "This increment supports UART decode; use a configured recipe for UART",
            "protocol": args["protocol"],
        }
    uart = LogicUartAnalyzerConfig(
        name=args.get("name", "uart"),
        channel=args["channel"],
        baud_rate=args.get("baud_rate", 115200),
    )
    path = await _get_logic_service().decode_uart(
        args["capture_id"],
        uart,
        Path(args["output"]),
    )
    return {"capture_id": args["capture_id"], "decoded_csv": str(path)}


async def _handle_logic_save(args: dict[str, Any]) -> dict[str, Any]:
    """Handle scopeloop_logic_save tool."""
    path = await _get_logic_service().save_capture(args["capture_id"], Path(args["output"]))
    return {"capture_id": args["capture_id"], "saved_capture": str(path)}


async def _handle_logic_export(args: dict[str, Any]) -> dict[str, Any]:
    """Handle scopeloop_logic_export tool."""
    paths = await _get_logic_service().export_capture(
        args["capture_id"],
        Path(args["output_dir"]),
        args.get("digital_channels"),
        args.get("analog_channels"),
    )
    return {"capture_id": args["capture_id"], "raw_csv": [str(path) for path in paths]}


async def _handle_logic_compare(args: dict[str, Any]) -> dict[str, Any]:
    """Handle scopeloop_logic_compare tool."""
    return compare_bundles(
        Path(args["reference_bundle"]),
        Path(args["dut_bundle"]),
        args["align_channel"],
        args.get("edge", "rising"),
        args.get("threshold_v"),
        args.get("signals"),
    )


async def _handle_test_run(args: dict[str, Any]) -> dict[str, Any]:
    """Handle scopeloop_test_run tool."""
    config = _get_config()
    if not config:
        return {"error": "No scopeloop.yaml found. Run scopeloop_init first."}

    if not config.tests:
        return {"error": "No tests defined in scopeloop.yaml"}

    # TODO: Implement test runner
    return {
        "status": "not_implemented",
        "message": "Test runner not yet implemented",
        "tests_defined": [t.name for t in config.tests],
    }


async def _handle_test_report(args: dict[str, Any]) -> dict[str, Any]:
    """Handle scopeloop_test_report tool."""
    # TODO: Implement test reporting
    return {
        "status": "not_implemented",
        "message": "Test reporting not yet implemented",
    }


async def _handle_safety_status() -> dict[str, Any]:
    """Handle scopeloop_safety_status tool."""
    safety = _get_safety()
    statuses = safety.get_all_statuses()

    return {
        "guardrails": {
            gt.value: {
                "state": status.state.value,
                "message": status.message,
                "blocked_at": status.blocked_at,
            }
            for gt, status in statuses.items()
        },
        "any_blocked": safety.is_any_blocked(),
    }


async def _handle_safety_reset(args: dict[str, Any]) -> dict[str, Any]:
    """Handle scopeloop_safety_reset tool."""
    from scopeloop.safety import GuardrailType

    safety = _get_safety()
    guardrail = args.get("guardrail")

    if guardrail:
        try:
            guardrail_type = GuardrailType(guardrail)
            safety.reset(guardrail_type)
            return {"success": True, "reset": guardrail}
        except ValueError:
            return {"error": f"Unknown guardrail: {guardrail}"}
    else:
        safety.reset()
        return {"success": True, "reset": "all"}


async def _handle_user_action(args: dict[str, Any]) -> dict[str, Any]:
    """Handle scopeloop_user_action tool."""
    action = args["action"]

    # Log the request
    logger.info(f"User action requested: {action}")

    # TODO: Implement user notification (could use system notification, UI, etc.)
    return {
        "action_requested": action,
        "message": "Please perform the requested action and confirm when ready.",
        "note": "User notification system not yet implemented - please check the terminal.",
    }


async def _handle_session_start(args: dict[str, Any]) -> dict[str, Any]:
    """Handle scopeloop_session_start tool."""
    global _session_manager

    config = _get_config()
    config_content = None
    if config:
        # Read config file content for snapshot
        try:
            from scopeloop.config import find_config_file

            config_path = find_config_file()
            config_content = config_path.read_text()
        except Exception:
            pass

    session = await _session_manager.create_session(
        project_name=config.project.name if config else None,
        description=args.get("description"),
        config_content=config_content,
    )
    await session.start()

    return {
        "session_id": session.session_id,
        "started": True,
        "session_dir": str(session.session_dir),
    }


async def _handle_session_end(args: dict[str, Any]) -> dict[str, Any]:
    """Handle scopeloop_session_end tool."""
    session = _session_manager.active_session
    if not session:
        return {"error": "No active session"}

    outcome = args.get("outcome", "success")
    await session.end(outcome)

    return {
        "session_id": session.session_id,
        "ended": True,
        "outcome": outcome,
    }


# ============================================================================
# Server Startup
# ============================================================================


async def initialize() -> None:
    """Initialize server state."""
    global _config, _resource_manager, _device_manager, _session_manager, _safety
    global _logic_service

    logger.info("Initializing ScopeLoop MCP server...")

    # Try to load config from current directory
    try:
        _config = load_config()
        logger.info(f"Loaded config for project: {_config.project.name}")
    except FileNotFoundError:
        logger.info("No scopeloop.yaml found - tools will work in limited mode")
        _config = None

    # Initialize managers
    _resource_manager = ResourceManager()
    _device_manager = DeviceManager()
    _session_manager = SessionManager(Path("./sessions"))
    _logic_service = None

    # Initialize safety with config or defaults
    safety_config = _config.safety if _config else SafetyConfig()
    _safety = SafetyGuardrails(
        SafetyConfig(
            max_flash_attempts=safety_config.max_flash_attempts,
            flash_window_seconds=safety_config.flash_window_seconds,
            boot_success_marker=safety_config.boot_success_marker,
            boot_timeout_seconds=safety_config.boot_timeout_seconds,
            max_consecutive_build_failures=safety_config.max_consecutive_build_failures,
        )
    )

    logger.info("ScopeLoop MCP server initialized")


async def main() -> None:
    """Main entry point for the MCP server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    await initialize()

    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        if _logic_service:
            await _logic_service.disconnect(close_captures=True)


if __name__ == "__main__":
    asyncio.run(main())
