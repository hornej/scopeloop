"""Owned supply workflows shared by the CLI, MCP and firmware test scripts."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from scopeloop.config import PowerSupplyConfig
from scopeloop.instruments.hmp4040 import HMP4040
from scopeloop.resources import ResourceManager, ResourceState
from scopeloop.saleae_labels import sha256


class PowerSupplyService:
    def __init__(self, config: PowerSupplyConfig | None, manager: ResourceManager | None = None):
        if config is None:
            raise ValueError("Configure instruments.power_supply in scopeloop.yaml")
        self.driver = HMP4040(config)
        self.manager = manager or ResourceManager()
        self._registered = False
        self.key = f"power_supply:{config.address}:{config.port}"

    async def _connect(self) -> HMP4040:
        await self.driver.connect()
        return self.driver

    @asynccontextmanager
    async def session(self, client_id: str = "project") -> AsyncIterator[HMP4040]:
        """Keep this context open across the entire project test."""
        if not self._registered:
            await self.manager.register(self.key, "power_supply", self._connect, HMP4040.disconnect)
            self._registered = True
        async with self.manager.lock(self.key, client_id):
            resource = self.manager.get_resource_info(self.key)
            already_connected = resource.state == ResourceState.CONNECTED
            supply = await self.manager.connect(self.key)
            try:
                yield supply
            finally:
                if not already_connected:
                    await self.manager.disconnect(self.key)

    async def command(self, name: str, args: dict, client_id: str) -> dict:
        async with self.session(client_id) as supply:
            if name == "idn":
                return asdict(await supply.get_info())
            if name == "status":
                output = args.get("output")
                return await snapshot(supply, args.get("channel"), Path(output) if output else None)
            if name == "configure":
                return await supply.configure(args["channel"], args["voltage_v"], args["current_a"])
            if name == "output":
                return await supply.output(args["channel"], args["enabled"])
            raise ValueError(f"Unknown power supply command: {name}")


async def snapshot(supply: HMP4040, channel: int | None = None, output: Path | None = None) -> dict:
    if output is None:
        return await supply.status(channel)
    identity = asdict(await supply.get_info())
    output.mkdir(parents=True, exist_ok=False)
    transcript_start = len(supply.transcript)
    manifest = {
        "schema_version": 1,
        "kind": "power_supply_snapshot",
        "started_at": datetime.now(UTC).isoformat(),
        "channel": channel,
        "instrument": identity,
    }
    try:
        result = await supply.status(channel)
        (output / "telemetry.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        manifest["status"] = "complete"
    except BaseException as exc:
        manifest.update(status="failed", error={"type": type(exc).__name__, "message": str(exc)})
        raise
    finally:
        transcript = supply.transcript[:1] + supply.transcript[transcript_start:]
        (output / "scpi.json").write_text(json.dumps(transcript, indent=2), encoding="utf-8")
        manifest["files"] = {path.name: sha256(path) for path in sorted(output.iterdir())}
        manifest["completed_at"] = datetime.now(UTC).isoformat()
        path = output / "manifest.json"
        path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        (output / "SHA256SUMS").write_text(
            "".join(f"{sha256(p)}  {p.name}\n" for p in sorted(output.iterdir())), encoding="ascii"
        )
    return {**result, "manifest": str(path)}
