"""Zero Pi HAT fixture commands; GPIO ownership stays with the Pi owner."""

import asyncio
import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from scopeloop.config import load_config
from scopeloop.resources import ResourceError
from scopeloop.zero_pi_hat import GpiodBackend, command, serve

app = typer.Typer(help="Zero Pi HAT v0.1.0 shared 12 V output; run on the Pi.")
ConfigPath = Annotated[Path | None, typer.Option("--config", "-c")]


class OutputState(StrEnum):
    on = "on"
    off = "off"


def _run(config: Path | None, operation: str, **args):
    try:
        settings = load_config(config).zero_pi_hat
        if settings is None:
            raise ValueError("Configure zero_pi_hat in scopeloop.yaml")
        if operation == "inspect":
            result = GpiodBackend(settings).inspect()
        elif operation == "serve":
            asyncio.run(serve(settings))
            return
        else:
            result = asyncio.run(command(settings, operation, **args))
    except (OSError, ValueError, RuntimeError, ResourceError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, indent=2))


@app.command("inspect")
def inspect_hat(config: ConfigPath = None):
    """Read Pi identity, chip mapping and existing consumers without claiming GPIO."""
    _run(config, "inspect")


@app.command("serve")
def serve_hat(
    start_off: Annotated[bool, typer.Option("--start-off")] = False,
    config: ConfigPath = None,
):
    """Own GPIO continuously, starting OFF. Owner exit/reboot can enable output."""
    if not start_off:
        raise typer.BadParameter("Explicit --start-off is required; this changes shared rail power")
    _run(config, "serve")


@app.command()
def status(config: ConfigPath = None):
    """Read the running owner's GPIO/fault state, not measured rail voltage."""
    _run(config, "status")


@app.command()
def output(state: OutputState, config: ConfigPath = None):
    """Set the shared output; the persistent owner retains it after CLI exit."""
    _run(config, "output", enabled=state == OutputState.on)


@app.command()
def cycle(
    off_seconds: float = 3,
    settle_seconds: float = 0.5,
    config: ConfigPath = None,
):
    """Cycle an already-enabled output; aborts hold it off while owner remains alive."""
    _run(config, "cycle", off_seconds=off_seconds, settle_seconds=settle_seconds)
