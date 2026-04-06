"""ScopeLoop CLI - Command-line interface for testing and management."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from scopeloop import __version__
from scopeloop.config import Config, create_default_config, load_config
from scopeloop.devices import DeviceManager, DeviceMatch
from scopeloop.safety import GuardrailType, SafetyConfig, SafetyGuardrails
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
        Optional[Path],
        typer.Option("--output", "-o", help="Output path for config file."),
    ] = None,
) -> None:
    """Initialize a new ScopeLoop project with a scopeloop.yaml config file."""
    output_path = output or Path("scopeloop.yaml")

    if output_path.exists():
        if not typer.confirm(f"{output_path} already exists. Overwrite?"):
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
        Optional[Path],
        typer.Option("--config", "-c", help="Path to scopeloop.yaml."),
    ] = None,
) -> None:
    """Show current project and system status."""
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        console.print("[yellow]No scopeloop.yaml found.[/yellow]")
        console.print("Run [bold]scopeloop init <project-name>[/bold] to create one.")
        raise typer.Exit(1)

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

        for name, device in config.devices.items():
            vid_pid = f"{device.match.vid or '?'}:{device.match.pid or '?'}"
            # Try to find actual port
            status = "[yellow]Not checked[/yellow]"
            devices_table.add_row(device.alias, device.type, vid_pid, status)

        console.print(Panel(devices_table, title="Devices"))

    # Instruments panel
    instruments_info = []
    if config.instruments.oscilloscope:
        instruments_info.append(
            f"Oscilloscope: {config.instruments.oscilloscope.type} @ {config.instruments.oscilloscope.address}"
        )
    if config.instruments.logic_analyzer:
        instruments_info.append(
            f"Logic Analyzer: {config.instruments.logic_analyzer.type}"
        )

    if instruments_info:
        console.print(Panel("\n".join(instruments_info), title="Instruments"))
    else:
        console.print("[dim]No instruments configured.[/dim]")


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
        Optional[str],
        typer.Option("--vid", help="Vendor ID to search for (e.g., 10c4 or 0x10c4)."),
    ] = None,
    pid: Annotated[
        Optional[str],
        typer.Option("--pid", help="Product ID to search for."),
    ] = None,
    serial: Annotated[
        Optional[str],
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
            raise typer.Exit(1)

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
        Optional[str],
        typer.Argument(help="Specific guardrail to reset (boot_loop, build_failure, flash_failure)."),
    ] = None,
) -> None:
    """Reset safety guardrails."""
    safety = SafetyGuardrails(SafetyConfig())

    if guardrail:
        try:
            guardrail_type = GuardrailType(guardrail)
        except ValueError:
            console.print(f"[red]Unknown guardrail:[/red] {guardrail}")
            console.print(f"Valid options: {', '.join(g.value for g in GuardrailType)}")
            raise typer.Exit(1)
        safety.reset(guardrail_type)
        console.print(f"[green]Reset guardrail:[/green] {guardrail}")
    else:
        safety.reset()
        console.print("[green]Reset all guardrails[/green]")


# ============================================================================
# Config command
# ============================================================================


@app.command("config")
def show_config(
    config_path: Annotated[
        Optional[Path],
        typer.Option("--config", "-c", help="Path to scopeloop.yaml."),
    ] = None,
) -> None:
    """Show parsed configuration."""
    try:
        config = load_config(config_path)
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    import json

    console.print_json(json.dumps(config.model_dump(), indent=2, default=str))


# ============================================================================
# Entry point
# ============================================================================

if __name__ == "__main__":
    app()
