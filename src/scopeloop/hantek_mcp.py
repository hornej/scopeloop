"""Discoverable combined-instrument tools; all operations use HantekService."""

from mcp.types import Tool, ToolAnnotations

from scopeloop.instruments.hantek import DMM_MODES, SCALES, TIMEBASES, AWGSettings

OUTPUT = {"type": "string", "description": "New local evidence directory; never overwrite."}
CHANNEL = {"type": "string", "enum": ["CH1", "CH2"]}
SCOPE_SETTINGS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "channel": CHANNEL,
        "enabled": {"type": "boolean"},
        "coupling": {"type": "string", "enum": ["AC", "DC", "GND"]},
        "probe": {"type": "integer", "enum": [1, 10, 100, 1000]},
        "bandwidth_limit": {"type": "boolean"},
        "volts_per_div": {"type": "number", "enum": list(SCALES)},
        "seconds_per_div": {"type": "number", "enum": list(TIMEBASES)},
        "position_code": {"type": "integer", "minimum": 0, "maximum": 200},
        "trigger_source": CHANNEL,
        "trigger_slope": {"type": "string", "enum": ["rising", "falling", "both"]},
        "trigger_mode": {"type": "string", "enum": ["auto", "normal", "single"]},
        "trigger_level_code": {"type": "integer", "minimum": 0, "maximum": 200},
        "running": {"type": "boolean"},
    },
}


def tool(name, description, properties=None, required=()):
    return Tool(
        name=f"scopeloop_hantek_{name}",
        description=description,
        annotations=ToolAnnotations(readOnlyHint=name in ("status", "dmm_read")),
        inputSchema={
            "type": "object",
            "properties": properties or {},
            "required": list(required),
            "additionalProperties": False,
        },
    )


TOOLS = [
    tool(
        "status",
        "Identify Hantek USB and read DMM, scope settings and AWG setpoints. "
        "Requires an idle bench handoff; output-enable readback is unavailable.",
        {"output": OUTPUT},
    ),
    tool(
        "dmm_read",
        "Read the Hantek DMM display: mode, signed SI value, unit and overload. "
        "No instrument sample timestamp or hold-state readback.",
        {"output": OUTPUT},
    ),
    tool(
        "dmm_configure",
        "Select DMM function. Check leads and input suit that function; "
        "AWG loopback uses voltage modes only.",
        {"mode": {"type": "string", "enum": list(DMM_MODES)}, "output": OUTPUT},
        ["mode"],
    ),
    tool(
        "screen",
        "Select the Hantek front-panel screen.",
        {"name": {"type": "string", "enum": ["scope", "dmm", "awg"]}},
        ["name"],
    ),
    tool(
        "awg_configure",
        "Disable AWG, apply settings and verify setpoints. Amplitude is "
        "Vpp into high impedance. Reject amplitude/2 + |offset| > configured peak limit.",
        {"settings": AWGSettings.model_json_schema()},
        ["settings"],
    ),
    tool(
        "awg_run",
        "Run authorized AWG output for a bounded duration, optionally log DMM "
        "voltage, then send output-off even on failure. Requires an evidence directory.",
        {
            "settings": AWGSettings.model_json_schema(),
            "output": OUTPUT,
            "duration_s": {"type": "number", "minimum": 0.2, "maximum": 300, "default": 5},
            "dmm_mode": {"type": "string", "enum": ["dc_v", "ac_v"]},
        },
        ["settings", "output"],
    ),
    tool("awg_off", "Send AWG output-off under exclusive instrument ownership."),
    tool(
        "scope_configure",
        "Set scope channel/trigger/timebase and verify readbacks. "
        "Volts/div is at the BNC; probe ratio is metadata and must match the physical probe.",
        {"settings": SCOPE_SETTINGS},
        ["settings"],
    ),
    tool(
        "scope_capture",
        "Save raw ADC AUTO snapshot, setup and USB evidence, then STOP. "
        "Requires AUTO, YT, 1 us..100 ms/div. Optional saved zero_reference from loopback "
        "enables nominal voltage export with matching settings. No single-shot freshness claim.",
        {
            "output": OUTPUT,
            "channel": CHANNEL,
            "zero_reference": {"type": "string", "description": "scope-zero evidence directory"},
        },
        ["output"],
    ),
    tool(
        "loopback",
        "With GEN OUT connected to DMM V/COM, run five bounded AC/DC checks. "
        "Restores DMM mode and leaves output off. Optional CH1 on the same signal must be "
        "configured DC/1x, 1 V/div, 2 ms/div, AUTO. Same-unit check, not calibration.",
        {
            "output": OUTPUT,
            "with_scope": {"type": "boolean", "default": False},
            "settle_s": {"type": "number", "minimum": 2, "maximum": 10, "default": 3},
            "samples": {"type": "integer", "minimum": 2, "maximum": 20, "default": 3},
        },
        ["output"],
    ),
]
