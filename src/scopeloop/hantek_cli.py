"""CLI access to the combined Hantek instrument."""

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from scopeloop.config import load_config
from scopeloop.hantek import HantekService
from scopeloop.instruments.base import InstrumentError
from scopeloop.resources import ResourceError

app = typer.Typer(help="Hantek 2000 scope, DMM and bounded generator workflows.")
ConfigPath = Annotated[Path | None, typer.Option("--config", "-c")]


def _run(config, operation, **args):
    try:
        service = HantekService(load_config(config).instruments.hantek)
        if isinstance(args.get("settings"), Path):
            args["settings"] = json.loads(args["settings"].read_text(encoding="utf-8-sig"))
        result = asyncio.run(service.command(operation, args, "cli"))
    except (OSError, ValueError, InstrumentError, ResourceError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, indent=2, allow_nan=False))
    if result.get("functional_match") is False:
        raise typer.Exit(2)


@app.command()
def status(config: ConfigPath = None, output: Path | None = None):
    """Read identity, DMM, generator setpoints and scope settings."""
    _run(config, "status", output=output)


@app.command("dmm-read")
def dmm_read(config: ConfigPath = None, output: Path | None = None):
    """Read the current DMM display with units, mode and overload status."""
    _run(config, "dmm_read", output=output)


@app.command("dmm-configure")
def dmm_configure(mode: str, config: ConfigPath = None, output: Path | None = None):
    """Select a DMM function. Leads and input must suit the selected function."""
    _run(config, "dmm_configure", mode=mode, output=output)


@app.command()
def screen(name: str, config: ConfigPath = None):
    """Select scope, dmm or awg on the front panel."""
    _run(config, "screen", name=name)


@app.command("awg-configure")
def awg_configure(settings: Path, config: ConfigPath = None):
    """Apply a JSON settings file and verify readbacks; output stays off."""
    _run(config, "awg_configure", settings=settings)


@app.command("awg-run")
def awg_run(
    settings: Path,
    output: Path,
    duration: float = 5,
    dmm_mode: str | None = None,
    config: ConfigPath = None,
):
    """Run a bounded output, optionally log voltage, then turn output off."""
    _run(
        config, "awg_run", settings=settings, duration_s=duration, dmm_mode=dmm_mode, output=output
    )


@app.command("awg-off")
def awg_off(config: ConfigPath = None):
    """Send output-off under the instrument lease."""
    _run(config, "awg_off")


@app.command("scope-configure")
def scope_configure(settings: Path, config: ConfigPath = None):
    """Apply a JSON settings file and verify the returned scope configuration."""
    _run(config, "scope_configure", settings=settings)


@app.command("scope-capture")
def scope_capture(
    output: Path,
    channel: str = "CH1",
    zero_reference: Path | None = None,
    config: ConfigPath = None,
):
    """Save AUTO snapshot with raw ADC data, nominal volts, settings and hashes."""
    _run(
        config,
        "scope_capture",
        channel=channel,
        zero_reference=str(zero_reference) if zero_reference else None,
        output=output,
    )


@app.command()
def loopback(output: Path, with_scope: bool = False, config: ConfigPath = None):
    """Test GEN OUT connected to DMM V/COM using bounded AC and DC stimuli."""
    _run(config, "loopback", with_scope=with_scope, output=output)
