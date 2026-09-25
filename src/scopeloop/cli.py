"""ScopeLoop CLI - Command-line interface for testing and management."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from scopeloop import __version__
from scopeloop.comparison import compare_bundles
from scopeloop.config import (
    Config,
    LogicUartAnalyzerConfig,
    create_default_config,
    load_config,
)
from scopeloop.devices import DeviceManager, DeviceMatch
from scopeloop.host import collect_host_status, format_bytes
from scopeloop.instruments.base import InstrumentError
from scopeloop.logic import LogicCaptureService, parse_metadata
from scopeloop.safety import GuardrailType, SafetyConfig, SafetyGuardrails
from scopeloop.scope import ScopeConfigError, create_scope_from_config, parse_si_value
from scopeloop.session import SessionManager

app = typer.Typer(
    name="scopeloop",
    help="Hardware development automation platform for LLM coding agents.",
    no_args_is_help=True,
)

console = Console()


def version_callback(value: bool) -> None:
    if value:
        console.print(f"ScopeLoop version {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-v",
            help="Show version and exit.",
            callback=version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """ScopeLoop - Hardware development automation platform."""
    pass


# ============================================================================
# Init command
# ============================================================================


@app.command()
def init(
    project_name: Annotated[
        str,
        typer.Argument(help="Name of the project."),
    ],
    mcu: Annotated[
        str,
        typer.Option("--mcu", "-m", help="Target MCU type."),
    ] = "esp32",
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Output path for config file."),
    ] = None,
) -> None:
    """Initialize a new ScopeLoop project with a scopeloop.yaml config file."""
    output_path = output or Path("scopeloop.yaml")

    if output_path.exists() and not typer.confirm(f"{output_path} already exists. Overwrite?"):
        raise typer.Exit(1)

    config_path = create_default_config(project_name, mcu, output_path)
    console.print(f"[green]Created[/green] {config_path}")
    console.print("\n[dim]Edit the config file to set up your devices and instruments.[/dim]")


# ============================================================================
# Status command
# ============================================================================


@app.command()
def status(
    config_path: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to scopeloop.yaml."),
    ] = None,
) -> None:
    """Show current project and system status."""
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        console.print("[yellow]No scopeloop.yaml found.[/yellow]")
        console.print("Run [bold]scopeloop init <project-name>[/bold] to create one.")
        raise typer.Exit(1) from None

    # Project info panel
    project_info = Table.grid(padding=(0, 2))
    project_info.add_row("[bold]Project:[/bold]", config.project.name)
    project_info.add_row("[bold]MCU:[/bold]", config.hardware.mcu)
    if config.hardware.board:
        project_info.add_row("[bold]Board:[/bold]", config.hardware.board)
    project_info.add_row("[bold]Build System:[/bold]", config.build.system)

    console.print(Panel(project_info, title="Project"))

    # Devices panel
    if config.devices:
        devices_table = Table(show_header=True, header_style="bold")
        devices_table.add_column("Alias")
        devices_table.add_column("Type")
        devices_table.add_column("VID:PID")
        devices_table.add_column("Status")

        for _name, device in config.devices.items():
            vid_pid = f"{device.match.vid or '?'}:{device.match.pid or '?'}"
            # Try to find actual port
            status = "[yellow]Not checked[/yellow]"
            devices_table.add_row(device.alias, device.type, vid_pid, status)

        console.print(Panel(devices_table, title="Devices"))

    # Instruments panel
    instruments_info = []
    if config.instruments.oscilloscope:
        scope = config.instruments.oscilloscope
        instruments_info.append(f"Oscilloscope: {scope.type} @ {scope.address}")
    if config.instruments.logic_analyzer:
        instruments_info.append(f"Logic Analyzer: {config.instruments.logic_analyzer.type}")

    if instruments_info:
        console.print(Panel("\n".join(instruments_info), title="Instruments"))
    else:
        console.print("[dim]No instruments configured.[/dim]")

    if config.modules:
        modules_table = Table(show_header=True, header_style="bold")
        modules_table.add_column("Name")
        modules_table.add_column("Slot")
        modules_table.add_column("Class")
        modules_table.add_column("Interface")
        modules_table.add_column("Capabilities")

        for name, module in config.modules.items():
            capabilities = ", ".join(
                capability.name for capability in module.manifest.capabilities[:3]
            )
            if len(module.manifest.capabilities) > 3:
                capabilities = f"{capabilities}, +{len(module.manifest.capabilities) - 3} more"
            modules_table.add_row(
                name,
                module.slot,
                module.manifest.slot_class,
                module.manifest.electrical_interface,
                capabilities or "-",
            )

        console.print(Panel(modules_table, title="Module Bay Cards"))

    if config.fixtures:
        fixtures_table = Table(show_header=True, header_style="bold")
        fixtures_table.add_column("Name")
        fixtures_table.add_column("Type")
        fixtures_table.add_column("Connection")
        fixtures_table.add_column("Capabilities")

        for name, fixture in config.fixtures.items():
            connection = fixture.connection.type
            if fixture.connection.controller:
                connection = f"{connection} via {fixture.connection.controller}"
            if fixture.connection.path:
                connection = f"{connection} ({fixture.connection.path})"

            capabilities = ", ".join(capability.name for capability in fixture.capabilities[:3])
            if len(fixture.capabilities) > 3:
                capabilities = f"{capabilities}, +{len(fixture.capabilities) - 3} more"
            fixtures_table.add_row(name, fixture.type, connection, capabilities or "-")

        console.print(Panel(fixtures_table, title="Fixtures"))


# ============================================================================
# Devices command
# ============================================================================


@app.command()
def devices(
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show detailed device info."),
    ] = False,
) -> None:
    """List connected USB serial devices."""

    async def _list_devices() -> None:
        manager = DeviceManager()
        device_list = await manager.list_devices()

        if not device_list:
            console.print("[yellow]No USB serial devices found.[/yellow]")
            return

        table = Table(show_header=True, header_style="bold")
        table.add_column("Port")
        table.add_column("VID:PID")
        if verbose:
            table.add_column("Serial")
            table.add_column("Product")

        for device in device_list:
            vid_pid = f"{device.vid or '?'}:{device.pid or '?'}"
            if verbose:
                table.add_row(
                    device.port,
                    vid_pid,
                    device.serial or "-",
                    device.product or "-",
                )
            else:
                table.add_row(device.port, vid_pid)

        console.print(table)

    asyncio.run(_list_devices())


@app.command("find-device")
def find_device(
    vid: Annotated[
        str | None,
        typer.Option("--vid", help="Vendor ID to search for (e.g., 10c4 or 0x10c4)."),
    ] = None,
    pid: Annotated[
        str | None,
        typer.Option("--pid", help="Product ID to search for."),
    ] = None,
    serial: Annotated[
        str | None,
        typer.Option("--serial", "-s", help="Serial number to search for."),
    ] = None,
) -> None:
    """Find a specific USB serial device by VID/PID/serial."""
    if not any([vid, pid, serial]):
        console.print("[red]Error:[/red] At least one of --vid, --pid, or --serial is required.")
        raise typer.Exit(1)

    async def _find() -> None:
        manager = DeviceManager()
        device = await manager.find_device(DeviceMatch(vid=vid, pid=pid, serial=serial))

        if device:
            console.print(f"[green]Found:[/green] {device.port}")
            console.print(f"  VID: {device.vid}")
            console.print(f"  PID: {device.pid}")
            console.print(f"  Serial: {device.serial or '-'}")
        else:
            console.print("[yellow]Device not found.[/yellow]")

    asyncio.run(_find())


@app.command("usb-diagnostics")
def usb_diagnostics() -> None:
    """Read OS USB inventory and enumeration errors without device recovery actions."""
    from scopeloop.usb import inventory

    console.print_json(json.dumps(inventory()))


@app.command("diagnose-device")
def diagnose_device(
    serial: Annotated[str | None, typer.Option("--serial")] = None,
    by_id: Annotated[str | None, typer.Option("--by-id")] = None,
    vid: Annotated[str | None, typer.Option("--vid")] = None,
    pid: Annotated[str | None, typer.Option("--pid")] = None,
) -> None:
    """Report absent, ambiguous or failed enumeration without opening hardware."""
    result = asyncio.run(
        DeviceManager().diagnose(DeviceMatch(serial=serial, by_id=by_id, vid=vid, pid=pid))
    )
    console.print_json(json.dumps(result))


# ============================================================================
# Scope command
# ============================================================================


scope_app = typer.Typer(help="Control the configured oscilloscope.")
app.add_typer(scope_app, name="scope")


def _load_configured_scope(config_path: Path | None):
    try:
        config = load_config(config_path)
        return create_scope_from_config(config)
    except (FileNotFoundError, ScopeConfigError, ValueError) as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1) from e


@scope_app.command("capture")
def scope_capture(
    recipe: Annotated[Path, typer.Argument(help="Scope evidence recipe JSON.")],
    output: Annotated[Path, typer.Argument(help="New evidence directory.")],
    config_path: Annotated[Path | None, typer.Option("--config", "-c")] = None,
) -> None:
    """Save a fresh ripple/noise-floor/load-step capture and its raw evidence."""
    from scopeloop.scope_evidence import ScopeRecipe, capture_scope_evidence

    parsed = ScopeRecipe.model_validate_json(recipe.read_text())

    async def capture():
        async with _load_configured_scope(config_path) as scope:
            result = await capture_scope_evidence(scope, parsed, output)
            console.print(result["manifest"])

    asyncio.run(capture())


@scope_app.command("idn")
def scope_idn(
    config_path: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to scopeloop.yaml."),
    ] = None,
) -> None:
    """Identify the configured oscilloscope."""

    async def _idn() -> None:
        scope = _load_configured_scope(config_path)
        async with scope:
            info = await scope.get_info()
            console.print(f"{info.model} {info.serial or ''} {info.firmware_version or ''}".strip())
            console.print(f"[dim]{info.address}[/dim]")

    asyncio.run(_idn())


@scope_app.command("query")
def scope_query(
    command: Annotated[str, typer.Argument(help="SCPI query to send.")],
    config_path: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to scopeloop.yaml."),
    ] = None,
) -> None:
    """Send a raw SCPI query to the configured oscilloscope."""

    async def _query() -> None:
        scope = _load_configured_scope(config_path)
        async with scope:
            console.print(await scope.query(command))

    asyncio.run(_query())


@scope_app.command("write")
def scope_write(
    command: Annotated[str, typer.Argument(help="SCPI command to send.")],
    config_path: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to scopeloop.yaml."),
    ] = None,
) -> None:
    """Send a raw SCPI command to the configured oscilloscope."""

    async def _write() -> None:
        scope = _load_configured_scope(config_path)
        async with scope:
            await scope.write(command)

    asyncio.run(_write())
    console.print("[green]ok[/green]")


@scope_app.command("measure")
def scope_measure(
    measurement: Annotated[
        str,
        typer.Argument(
            help="Snapshot measurement: frequency, period, vpp, vmax, vmin, vrms, "
            "rise_time, fall_time, duty_cycle."
        ),
    ],
    channel: Annotated[str, typer.Option("--channel", "-C", help="Channel to measure.")] = "CH1",
    config_path: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to scopeloop.yaml."),
    ] = None,
) -> None:
    """Take a built-in oscilloscope measurement."""

    async def _measure() -> None:
        scope = _load_configured_scope(config_path)
        async with scope:
            result = await scope.measure(channel, measurement)
            console.print(
                f"{result.measurement_type} {result.channel}: {result.value:g} {result.unit}"
            )

    asyncio.run(_measure())


@scope_app.command("configure")
def scope_configure(
    channel: Annotated[
        str,
        typer.Option("--channel", "-C", help="Channel to configure."),
    ] = "CH1",
    scale: Annotated[
        str | None,
        typer.Option("--scale", help="Vertical scale, for example 1V or 500mV."),
    ] = None,
    timebase: Annotated[
        str | None,
        typer.Option("--timebase", help="Horizontal timebase, for example 1ms or 100us."),
    ] = None,
    trigger_level: Annotated[
        float | None,
        typer.Option("--trigger-level", help="Trigger level in volts."),
    ] = None,
    config_path: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to scopeloop.yaml."),
    ] = None,
) -> None:
    """Configure basic oscilloscope channel, timebase, and trigger settings."""

    async def _configure() -> None:
        scope = _load_configured_scope(config_path)
        async with scope:
            if scale is not None:
                await scope.set_channel_scale(channel, parse_si_value(scale))
            if timebase is not None:
                await scope.set_timebase(parse_si_value(timebase))
            if trigger_level is not None:
                await scope.set_trigger_level(trigger_level, source=channel)

    asyncio.run(_configure())
    console.print("[green]ok[/green]")


@scope_app.command("status")
def scope_status(
    config_path: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to scopeloop.yaml."),
    ] = None,
) -> None:
    """Show a compact oscilloscope status summary."""

    async def _status() -> None:
        scope = _load_configured_scope(config_path)
        async with scope:
            rows = [
                ("idn", await scope.query("*IDN?")),
                ("trigger_mode", await scope.query("TRMD?")),
                ("timebase", await scope.query("TDIV?")),
            ]
            for channel in ("C1", "C2", "C3", "C4"):
                rows.append((f"{channel}_scale", await scope.query(f"{channel}:VDIV?")))
                rows.append((f"{channel}_offset", await scope.query(f"{channel}:OFST?")))

            table = Table(show_header=True, header_style="bold")
            table.add_column("Field")
            table.add_column("Value")
            for field, value in rows:
                table.add_row(field, value)
            console.print(table)

    asyncio.run(_status())


@scope_app.command("run")
def scope_run(
    config_path: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to scopeloop.yaml."),
    ] = None,
) -> None:
    """Start oscilloscope acquisition."""

    async def _run_scope() -> None:
        scope = _load_configured_scope(config_path)
        async with scope:
            await scope.run()

    asyncio.run(_run_scope())
    console.print("[green]ok[/green]")


@scope_app.command("stop")
def scope_stop(
    config_path: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to scopeloop.yaml."),
    ] = None,
) -> None:
    """Stop oscilloscope acquisition."""

    async def _stop() -> None:
        scope = _load_configured_scope(config_path)
        async with scope:
            await scope.stop()

    asyncio.run(_stop())
    console.print("[green]ok[/green]")


@scope_app.command("single")
def scope_single(
    config_path: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to scopeloop.yaml."),
    ] = None,
) -> None:
    """Arm a single oscilloscope acquisition."""

    async def _single() -> None:
        scope = _load_configured_scope(config_path)
        async with scope:
            await scope.single()

    asyncio.run(_single())
    console.print("[green]ok[/green]")


@scope_app.command("screenshot")
def scope_screenshot(
    output: Annotated[Path, typer.Argument(help="Output BMP path.")],
    config_path: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to scopeloop.yaml."),
    ] = None,
) -> None:
    """Save a screenshot from the oscilloscope display."""

    async def _screenshot() -> None:
        scope = _load_configured_scope(config_path)
        async with scope:
            output.write_bytes(await scope.screenshot())

    asyncio.run(_screenshot())
    console.print(f"[green]saved[/green] {output}")


# ============================================================================
# Sessions command
# ============================================================================


sessions_app = typer.Typer(help="Manage development sessions.")
app.add_typer(sessions_app, name="sessions")


@sessions_app.command("list")
def sessions_list(
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", help="Maximum number of sessions to show."),
    ] = 10,
    sessions_dir: Annotated[
        Path,
        typer.Option("--dir", "-d", help="Sessions directory."),
    ] = Path("./sessions"),
) -> None:
    """List recent sessions."""

    async def _list() -> None:
        if not sessions_dir.exists():
            console.print("[dim]No sessions found.[/dim]")
            return

        manager = SessionManager(sessions_dir)
        sessions = await manager.list_sessions(limit=limit)

        if not sessions:
            console.print("[dim]No sessions found.[/dim]")
            return

        table = Table(show_header=True, header_style="bold")
        table.add_column("Session ID")
        table.add_column("Created")
        table.add_column("State")
        table.add_column("Iterations")
        table.add_column("Outcome")

        for session in sessions:
            state_color = {
                "completed": "green",
                "failed": "red",
                "running": "yellow",
            }.get(session.state, "dim")

            outcome_display = session.outcome or "-"
            table.add_row(
                session.session_id,
                session.created_at[:19],
                f"[{state_color}]{session.state}[/{state_color}]",
                str(session.iteration_count),
                outcome_display,
            )

        console.print(table)

    asyncio.run(_list())


@sessions_app.command("show")
def sessions_show(
    session_id: Annotated[
        str,
        typer.Argument(help="Session ID to show."),
    ],
    sessions_dir: Annotated[
        Path,
        typer.Option("--dir", "-d", help="Sessions directory."),
    ] = Path("./sessions"),
) -> None:
    """Show details of a specific session."""

    async def _show() -> None:
        manager = SessionManager(sessions_dir)
        try:
            session = await manager.load_session(session_id)
        except FileNotFoundError:
            console.print(f"[red]Session not found:[/red] {session_id}")
            raise typer.Exit(1) from None

        # Session info
        info = Table.grid(padding=(0, 2))
        info.add_row("[bold]Session ID:[/bold]", session.metadata.session_id)
        info.add_row("[bold]Project:[/bold]", session.metadata.project_name or "-")
        info.add_row("[bold]State:[/bold]", session.metadata.state)
        info.add_row("[bold]Created:[/bold]", session.metadata.created_at)
        info.add_row("[bold]Iterations:[/bold]", str(session.metadata.iteration_count))
        if session.metadata.outcome:
            info.add_row("[bold]Outcome:[/bold]", session.metadata.outcome)

        console.print(Panel(info, title="Session"))

        # Git state
        if session.git_state and session.git_state.commit_hash:
            git_info = Table.grid(padding=(0, 2))
            git_info.add_row("[bold]Commit:[/bold]", session.git_state.commit_hash[:8])
            git_info.add_row("[bold]Branch:[/bold]", session.git_state.branch or "-")
            if session.git_state.dirty_files:
                git_info.add_row(
                    "[bold]Dirty:[/bold]",
                    f"{len(session.git_state.dirty_files)} files",
                )
            console.print(Panel(git_info, title="Git State"))

        # Timeline sample
        events = await session.get_timeline()
        if events:
            console.print(f"\n[bold]Timeline:[/bold] {len(events)} events")
            console.print("[dim]Use 'scopeloop sessions timeline <id>' to view full timeline[/dim]")

    asyncio.run(_show())


# ============================================================================
# Safety command
# ============================================================================


safety_app = typer.Typer(help="Manage safety guardrails.")
app.add_typer(safety_app, name="safety")


@safety_app.command("status")
def safety_status() -> None:
    """Show current safety guardrail status."""
    safety = SafetyGuardrails(SafetyConfig())
    statuses = safety.get_all_statuses()

    table = Table(show_header=True, header_style="bold")
    table.add_column("Guardrail")
    table.add_column("State")
    table.add_column("Message")

    for guardrail_type, status in statuses.items():
        state_color = {
            "ok": "green",
            "warning": "yellow",
            "blocked": "red",
        }.get(status.state.value, "dim")

        table.add_row(
            guardrail_type.value,
            f"[{state_color}]{status.state.value}[/{state_color}]",
            status.message,
        )

    console.print(table)


@safety_app.command("reset")
def safety_reset(
    guardrail: Annotated[
        str | None,
        typer.Argument(
            help="Specific guardrail to reset (boot_loop, build_failure, flash_failure)."
        ),
    ] = None,
) -> None:
    """Reset safety guardrails."""
    safety = SafetyGuardrails(SafetyConfig())

    if guardrail:
        try:
            guardrail_type = GuardrailType(guardrail)
        except ValueError as exc:
            console.print(f"[red]Unknown guardrail:[/red] {guardrail}")
            console.print(f"Valid options: {', '.join(g.value for g in GuardrailType)}")
            raise typer.Exit(1) from exc
        safety.reset(guardrail_type)
        console.print(f"[green]Reset guardrail:[/green] {guardrail}")
    else:
        safety.reset()
        console.print("[green]Reset all guardrails[/green]")


# ============================================================================
# Host command
# ============================================================================


host_app = typer.Typer(help="Inspect the local instrument host.")
app.add_typer(host_app, name="host")


@host_app.command("status")
def host_status(
    config_path: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to scopeloop.yaml."),
    ] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="Override runtime.data_dir for disk-space checks."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Show whether this machine is ready to act as the instrument host."""
    config: Config | None
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        config = None

    status = collect_host_status(config=config, data_dir=data_dir)

    if json_output:
        import json

        console.print_json(json.dumps(status.to_dict(), indent=2))
        return

    runtime = Table.grid(padding=(0, 2))
    runtime.add_row("[bold]Mode:[/bold]", status.mode)
    runtime.add_row("[bold]Host:[/bold]", status.host or "this machine")
    runtime.add_row("[bold]API Port:[/bold]", str(status.port))
    runtime.add_row("[bold]Platform:[/bold]", f"{status.platform} {status.machine}")
    runtime.add_row("[bold]Python:[/bold]", status.python)
    console.print(Panel(runtime, title="Instrument Host"))

    checks_table = Table(show_header=True, header_style="bold")
    checks_table.add_column("Dependency")
    checks_table.add_column("Kind")
    checks_table.add_column("Status")
    checks_table.add_column("Detail")

    for check in status.checks:
        state = "[green]ok[/green]" if check.available else "[yellow]missing[/yellow]"
        detail = check.detail
        if check.version:
            detail = f"{detail} ({check.version})"
        checks_table.add_row(check.name, check.kind, state, detail)

    console.print(Panel(checks_table, title="Local Capabilities"))

    storage = status.storage
    storage_info = Table.grid(padding=(0, 2))
    storage_info.add_row("[bold]Capture Dir:[/bold]", storage.path)
    storage_info.add_row("[bold]Exists:[/bold]", "yes" if storage.exists else "no")
    storage_info.add_row("[bold]Checked Path:[/bold]", storage.checked_path)
    storage_info.add_row("[bold]Free:[/bold]", format_bytes(storage.free_bytes))
    storage_info.add_row("[bold]Total:[/bold]", format_bytes(storage.total_bytes))
    console.print(Panel(storage_info, title="Storage"))


# ============================================================================
# Logic analyzer commands
# ============================================================================


logic_app = typer.Typer(help="Capture, decode, export, and compare logic evidence.")
app.add_typer(logic_app, name="logic")


def _logic_service(config_path: Path | None) -> tuple[Config, LogicCaptureService]:
    config = load_config(config_path)
    logic_config = config.instruments.logic_analyzer
    if logic_config is None:
        raise InstrumentError("No logic analyzer configured in scopeloop.yaml")
    return config, LogicCaptureService(logic_config)


@logic_app.command("connect")
def logic_connect(
    config_path: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to scopeloop.yaml."),
    ] = None,
) -> None:
    """Connect to Logic 2 and report the selected device and software versions."""

    async def run() -> dict:
        _, service = _logic_service(config_path)
        try:
            return await service.connect()
        finally:
            await service.disconnect()

    try:
        console.print_json(json.dumps(asyncio.run(run()), indent=2))
    except (FileNotFoundError, InstrumentError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc


@logic_app.command("capture")
def logic_capture(
    recipe: Annotated[str, typer.Option("--recipe", "-r", help="Configured recipe name.")],
    native_labels: Annotated[bool, typer.Option("--native-labels")] = False,
    metadata: Annotated[
        list[str] | None,
        typer.Option("--metadata", "-m", help="Required or optional key=value run metadata."),
    ] = None,
    output_root: Annotated[
        Path | None,
        typer.Option("--output-root", "-o", help="Evidence bundle parent directory."),
    ] = None,
    config_path: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to scopeloop.yaml."),
    ] = None,
) -> None:
    """Run a capture recipe and save a hashed evidence bundle automatically."""

    async def run() -> dict:
        config, service = _logic_service(config_path)
        root = output_root or Path(config.runtime.data_dir).expanduser() / "captures"
        try:
            bundle = await service.capture_evidence(
                recipe,
                parse_metadata(metadata or []),
                root,
                **({"native_labels": True} if native_labels else {}),
            )
            return bundle.to_dict()
        finally:
            await service.disconnect(close_captures=True)

    try:
        console.print_json(json.dumps(asyncio.run(run()), indent=2))
    except (FileNotFoundError, InstrumentError, ValueError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc


async def _with_loaded_capture(
    config_path: Path | None,
    capture_path: Path,
    action: Any,
) -> Any:
    _, service = _logic_service(config_path)
    try:
        capture = await service.load_capture(capture_path)
        return await action(service, capture.capture_id)
    finally:
        await service.disconnect(close_captures=True)


@logic_app.command("decode")
def logic_decode(
    capture_path: Annotated[Path, typer.Option("--capture", help="Source .sal capture.")],
    output_path: Annotated[Path, typer.Option("--output", "-o", help="Decoded UART CSV.")],
    channel: Annotated[int, typer.Option("--channel", help="UART input channel.")],
    baud_rate: Annotated[int, typer.Option("--baud", help="UART baud rate.")] = 115200,
    name: Annotated[str, typer.Option("--name", help="Analyzer label.")] = "uart",
    config_path: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to scopeloop.yaml."),
    ] = None,
) -> None:
    """Load a .sal capture, decode one UART channel, and export the decoder CSV."""
    uart = LogicUartAnalyzerConfig(name=name, channel=channel, baud_rate=baud_rate)

    async def action(service: LogicCaptureService, capture_id: str) -> str:
        return str(await service.decode_uart(capture_id, uart, output_path))

    try:
        result = asyncio.run(_with_loaded_capture(config_path, capture_path, action))
        console.print_json(json.dumps({"decoded_csv": result}, indent=2))
    except (FileNotFoundError, InstrumentError, ValueError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc


@logic_app.command("export")
def logic_export(
    capture_path: Annotated[Path, typer.Option("--capture", help="Source .sal capture.")],
    output_dir: Annotated[Path, typer.Option("--output-dir", "-o", help="Raw CSV directory.")],
    digital_channel: Annotated[
        list[int] | None,
        typer.Option("--digital-channel", help="Digital channel to export (repeatable)."),
    ] = None,
    analog_channel: Annotated[
        list[int] | None,
        typer.Option("--analog-channel", help="Analog channel to export (repeatable)."),
    ] = None,
    config_path: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to scopeloop.yaml."),
    ] = None,
) -> None:
    """Load a .sal capture and export untouched Saleae raw CSV files."""

    async def action(service: LogicCaptureService, capture_id: str) -> list[str]:
        paths = await service.export_capture(
            capture_id,
            output_dir,
            digital_channel,
            analog_channel,
        )
        return [str(path) for path in paths]

    try:
        paths = asyncio.run(_with_loaded_capture(config_path, capture_path, action))
        console.print_json(json.dumps({"raw_csv": paths}, indent=2))
    except (FileNotFoundError, InstrumentError, ValueError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc


@logic_app.command("save")
def logic_save(
    capture_path: Annotated[Path, typer.Option("--capture", help="Source .sal capture.")],
    output_path: Annotated[Path, typer.Option("--output", "-o", help="Destination .sal.")],
    config_path: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to scopeloop.yaml."),
    ] = None,
) -> None:
    """Load and save a .sal copy through Logic 2, proving that Logic accepts it."""

    async def action(service: LogicCaptureService, capture_id: str) -> str:
        return str(await service.save_capture(capture_id, output_path))

    try:
        saved = asyncio.run(_with_loaded_capture(config_path, capture_path, action))
        console.print_json(json.dumps({"saved_capture": saved}, indent=2))
    except (FileNotFoundError, InstrumentError, ValueError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc


@logic_app.command("compare")
def logic_compare(
    reference_bundle: Annotated[Path, typer.Option("--reference", help="Known-good bundle.")],
    dut_bundle: Annotated[Path, typer.Option("--dut", help="DUT evidence bundle.")],
    align_channel: Annotated[int, typer.Option("--align-channel", help="Alignment channel.")],
    signal: Annotated[
        list[int] | None,
        typer.Option("--signal", help="Channel to compare (repeatable; default all)."),
    ] = None,
    edge: Annotated[str, typer.Option("--edge", help="rising or falling.")] = "rising",
    threshold_v: Annotated[
        float | None,
        typer.Option("--threshold", help="Alignment threshold; default robust midpoint."),
    ] = None,
    output_path: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write comparison JSON."),
    ] = None,
) -> None:
    """Compare a DUT with a known-good capture under identical recipe settings."""
    if edge not in {"rising", "falling"}:
        raise typer.BadParameter("edge must be rising or falling")
    try:
        result = compare_bundles(
            reference_bundle,
            dut_bundle,
            align_channel,
            edge,
            threshold_v,
            signal,
        )
        content = json.dumps(result, indent=2) + "\n"
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(content, encoding="utf-8")
        console.print_json(content)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc


# ============================================================================
# Config command
# ============================================================================


@app.command("config")
def show_config(
    config_path: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to scopeloop.yaml."),
    ] = None,
) -> None:
    """Show parsed configuration."""
    try:
        config = load_config(config_path)
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1) from e

    import json

    console.print_json(json.dumps(config.model_dump(), indent=2, default=str))


# ============================================================================
# Saved evidence viewers
# ============================================================================

evidence_app = typer.Typer(help="Export saved evidence to ngscopeclient and PulseView.")
app.add_typer(evidence_app, name="evidence")


@evidence_app.command("export")
def evidence_export(
    bundle: Path,
    output: Path,
    max_samples: Annotated[
        int, typer.Option(min=2, help="Maximum expanded samples per stream.")
    ] = 50_000_000,
) -> None:
    """Verify a completed bundle and create offline viewer files in a new directory."""
    from scopeloop.viewers import export_viewers

    try:
        result = export_viewers(bundle, output, max_samples)
    except (OSError, ValueError, KeyError) as exc:
        typer.echo(f"Export failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, indent=2))


if __name__ == "__main__":
    app()
