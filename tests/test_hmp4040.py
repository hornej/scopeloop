"""TCP replay of HMP4040 SW2.71 reply shapes with synthetic identity and setpoints."""

import asyncio
import json
import sys
from contextlib import suppress

import pytest

from scopeloop import mcp_server
from scopeloop.config import Config, PowerSupplyConfig
from scopeloop.evidence import validate_bundle
from scopeloop.instruments.base import InstrumentError
from scopeloop.power import PowerSupplyService, snapshot
from scopeloop.resources import ResourceError, ResourceManager


class Replay:
    def __init__(self):
        self.selected = 3
        self.setpoints = {ch: (float(ch), 0.25) for ch in range(1, 5)}
        self.outputs = dict.fromkeys(range(1, 5), False)
        self.commands = []
        self.override = {}
        self.connections = 0
        self.received = asyncio.Event()

    async def handle(self, reader, writer):
        self.connections += 1
        try:
            while raw := await reader.readline():
                command = raw.decode("ascii").strip()
                self.commands.append(command)
                self.received.set()
                if command in self.override:
                    reply = self.override[command]
                    if reply is None:
                        continue  # Simulate a timed-out query.
                    if isinstance(reply, bytes):
                        writer.write(reply)
                        break  # Truncated frame, then EOF.
                else:
                    reply = self.respond(command)
                if reply is not None:
                    writer.write((reply + "\r\n").encode("ascii"))
                    await writer.drain()
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    def respond(self, command):
        ch = self.selected
        if command.startswith("INST:NSEL "):
            self.selected = int(command.split()[1])
        elif command.startswith("APPL "):
            self.setpoints[ch] = tuple(map(float, command.split()[1].split(",")))
        elif command.startswith("OUTP:SEL "):
            self.outputs[ch] = command.endswith(" 1")
        else:
            voltage, current = self.setpoints[ch]
            return {
                "*IDN?": "HAMEG,HMP4040,TEST0001,HW50020001/SW2.71",
                "INST:NSEL?": str(ch),
                "APPL?": f"{voltage:.3f},{current:.4f}",
                "OUTP?": str(int(self.outputs[ch])),
                "MEAS:VOLT?": "0.000",
                "MEAS:CURR?": "0.0000",
                "*OPC?": "1",
                "SYST:ERR?": '0,"No error"',
            }[command]
        return None


@pytest.fixture
async def bench(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOPELOOP_LOCK_DIR", str(tmp_path / "locks"))
    replay = Replay()
    async with await asyncio.start_server(replay.handle, "127.0.0.1", 0) as server:
        config = PowerSupplyConfig(
            type="hmp4040",
            address="127.0.0.1",
            port=server.sockets[0].getsockname()[1],
            expected_serial="TEST0001",
            timeout_seconds=1,
            channels={2: {"max_voltage_v": 5, "max_current_a": 0.5}},
        )
        yield replay, PowerSupplyService(config)


async def test_snapshot_all_channels_and_evidence(bench, tmp_path):
    replay, service = bench
    result = await service.command("status", {"output": tmp_path / "bundle"}, "cli")
    assert [row["voltage_set_v"] for row in result["channels"]] == [1, 2, 3, 4]
    assert all(row["measured_current_a"] == 0 for row in result["channels"])
    assert replay.selected == 3
    assert all(c.endswith("?") or c.startswith("INST:NSEL ") for c in replay.commands)
    assert validate_bundle(tmp_path / "bundle")["kind"] == "power_supply_snapshot"
    assert not service.driver.is_connected


async def test_control_readback_and_single_channel_scope(bench):
    replay, service = bench
    async with service.session() as supply:
        result = await supply.configure(2, 3.3, 0.2)
        assert (result["voltage_set_v"], result["current_limit_a"]) == (3.3, 0.2)
        assert not any(replay.outputs.values())
        assert (await supply.output(2, True))["output_state"] is True
        with pytest.raises(InstrumentError, match="Disable CH2"):
            await supply.configure(2, 1.5, 0.2)
        assert (await supply.output(2, False))["output_state"] is False
    assert replay.selected == 3
    assert not any(c.startswith(("OUTP:GEN ", "OUTP ", "*RST")) for c in replay.commands)
    assert replay.connections == 1


@pytest.mark.parametrize(
    "channel,v,a",
    [(0, 1, 0.1), (1, 0.5, 0.1), (2, 5.1, 0.1), (2, 1, 0.6), (2, float("nan"), 0.1), (2, 1, 0)],
)
async def test_rejects_invalid_writes_before_addressing(bench, channel, v, a):
    replay, service = bench
    async with service.session() as supply:
        with pytest.raises(ValueError):
            await supply.configure(channel, v, a)
    assert replay.commands == ["*IDN?"]


async def test_enable_checks_actual_setpoint_and_scpi_errors(bench):
    replay, service = bench
    replay.setpoints[2] = (6, 0.25)
    async with service.session() as supply:
        with pytest.raises(ValueError, match="voltage"):
            await supply.output(2, True)
        replay.override["SYST:ERR?"] = '-200,"Execution error"'
        with pytest.raises(InstrumentError, match="Execution error"):
            await supply.configure(2, 2, 0.25)
        assert not any(cmd.startswith(("OUTP:SEL ", "APPL ")) for cmd in replay.commands)


async def test_unverified_enable_deselects_only_target(bench):
    replay, service = bench
    replay.override["OUTP?"] = "0"
    async with service.session() as supply:
        with pytest.raises(InstrumentError, match="deselected"):
            await supply.output(2, True)
    assert [cmd for cmd in replay.commands if cmd.startswith("OUTP:SEL")] == [
        "OUTP:SEL 1",
        "OUTP:SEL 0",
    ]
    assert not any(replay.outputs.values())


@pytest.mark.parametrize(
    "idn", ["HAMEG,HMP4030,TEST0001,SW2.71", "HAMEG,HMP4040,OTHER,SW2.71", "HMP4040"]
)
async def test_identity_failure_releases_lease_before_retry(bench, idn):
    replay, service = bench
    replay.override["*IDN?"] = idn
    with pytest.raises(ResourceError):
        await service.command("status", {}, "cli")
    assert replay.commands == ["*IDN?"]
    replay.override.clear()
    assert (await service.command("idn", {}, "cli"))["serial"] == "TEST0001"


@pytest.mark.parametrize(
    "query,reply",
    [
        ("MEAS:VOLT?", "NaN"),
        ("MEAS:CURR?", "overload"),
        ("APPL?", "1.000"),
        ("OUTP?", "x"),
        ("MEAS:CURR?", b"0.00"),
        ("MEAS:VOLT?", None),
    ],
)
async def test_failed_snapshot_and_lease_until_session_exit(bench, tmp_path, query, reply):
    replay, service = bench
    replay.override[query] = reply
    peer = PowerSupplyService(service.driver.config)
    async with service.session() as supply:
        with pytest.raises(InstrumentError):
            await snapshot(supply, 2, tmp_path / "failed")
        with pytest.raises(ResourceError, match="busy"):
            await peer.command("idn", {}, "other")
    with pytest.raises(ValueError, match="complete"):
        validate_bundle(tmp_path / "failed")
    assert replay.connections == 1
    await peer.command("idn", {}, "other")


async def test_cancel_connect_then_retry(bench):
    replay, service = bench
    replay.override["*IDN?"] = None
    task = asyncio.create_task(service.command("idn", {}, "client"))
    await asyncio.wait_for(replay.received.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    replay.override.clear()
    assert (await service.command("idn", {}, "client"))["model"] == "HMP4040"


async def test_complete_session_ownership(bench):
    replay, service = bench
    async with service.session("owner") as supply:
        async with service.session("owner"):
            await supply.status(1)
        assert supply.is_connected
        waiter = asyncio.create_task(service.command("status", {"channel": 2}, "owner"))
        await asyncio.sleep(0)
        assert not waiter.done()
        assert replay.connections == 1
    assert (await waiter)["channels"][0]["channel"] == 2


async def test_cli_and_mcp_use_project_limits(bench, tmp_path, monkeypatch):
    replay, service = bench
    config = Config.model_validate(
        {
            "project": {"name": "replay"},
            "build": {"system": "test"},
            "hardware": {"mcu": "test"},
            "instruments": {"power_supply": service.driver.config.model_dump()},
        }
    )
    path = tmp_path / "scopeloop.yaml"
    path.write_text(config.model_dump_json(), encoding="utf-8")
    cli = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "scopeloop.cli",
        "power",
        "status",
        "--config",
        str(path),
        "--channel",
        "4",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await cli.communicate()
    assert cli.returncode == 0, stderr
    assert json.loads(stdout)["channels"][0]["channel"] == 4
    monkeypatch.setattr(mcp_server, "_config", config)
    monkeypatch.setattr(mcp_server, "_power_service", None)
    monkeypatch.setattr(mcp_server, "_resource_manager", ResourceManager())
    result = await mcp_server.call_tool(
        "scopeloop_power_configure",
        {
            "channel": 2,
            "voltage_v": 1.8,
            "current_a": 0.15,
        },
    )
    assert json.loads(result[0].text)["voltage_set_v"] == 1.8
    assert replay.setpoints[2] == (1.8, 0.15)
    with pytest.raises(ValueError, match="project"):
        await mcp_server.call_tool("scopeloop_power_output", {"channel": 1, "enabled": True})


@pytest.mark.parametrize(
    "override,match",
    [
        ({"INST:NSEL 2": None}, "address CH2"),
        ({"APPL?": "1.000,0.2500"}, "readback differs"),
        ({"*OPC?": "0"}, "completion unverified"),
    ],
)
async def test_configure_fails_closed_on_unverified_commands(bench, override, match):
    replay, service = bench
    replay.override.update(override)
    async with service.session() as supply:
        with pytest.raises(InstrumentError, match=match):
            await supply.configure(2, 2.5, 0.2)
    assert not any(replay.outputs.values())
    if "INST:NSEL 2" in override:
        assert not any(command.startswith("APPL ") for command in replay.commands)
