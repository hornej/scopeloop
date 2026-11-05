"""Project CLI for the configured power supply."""

import asyncio
import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from scopeloop.config import load_config
from scopeloop.instruments.base import InstrumentError
from scopeloop.power import PowerSupplyService
from scopeloop.resources import ResourceError

app = typer.Typer(help="Identify, read or control the configured HMP4040.")
ConfigPath = Annotated[Path | None, typer.Option("--config", "-c")]


class OutputState(StrEnum):
    on = "on"
    off = "off"


def _run(config: Path | None, name: str, **args) -> None:
    try:
        service = PowerSupplyService(load_config(config).instruments.power_supply)
        result = asyncio.run(service.command(name, args, "cli"))
    except (OSError, ValueError, InstrumentError, ResourceError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, indent=2))


@app.command()
def idn(config: ConfigPath = None) -> None:
    """Identify the supply without addressing a channel."""
    _run(config, "idn")


@app.command()
def status(channel: int | None = None, output: Path | None = None, config: ConfigPath = None):
    """Read one or all channels. Optionally save a snapshot in a new directory."""
    _run(config, "status", channel=channel, output=output)


@app.command()
def configure(
    channel: int,
    voltage: Annotated[float, typer.Option()],
    current_limit: Annotated[float, typer.Option()],
    config: ConfigPath = None,
):
    """Set voltage (V) and current limit (A) on a disabled channel."""
    _run(config, "configure", channel=channel, voltage_v=voltage, current_a=current_limit)


@app.command()
def output(channel: int, state: OutputState, config: ConfigPath = None):
    """Select or deselect one output. The operator controls the master switch."""
    _run(config, "output", channel=channel, enabled=state == OutputState.on)
