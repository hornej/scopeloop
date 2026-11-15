"""Replay fixture state and failure paths without requesting physical GPIO."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from scopeloop import mcp_server
from scopeloop.config import Config, ZeroPiHatConfig
from scopeloop.hat_cli import app
from scopeloop.resources import ResourceError
from scopeloop.zero_pi_hat import GpiodBackend, HatOwner, HatRequest, command, serve


class FakeGPIO:
    def __init__(self):
        self.claimed = False
        self.enabled = True  # Physical default, before an owner claims the pin.
        self.fault = False
        self.writes = []
        self.read_error = False

    def claim_off(self):
        if self.claimed:
            raise RuntimeError("GPIO already owned")
        self.claimed = True
        self.set_enabled(False)

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.writes.append(enabled)

    def read(self):
        if self.read_error:
            raise OSError("readback failed")
        return {"enable_gpio_high": self.enabled, "fault_active": self.fault}

    def release(self):
        self.claimed = False
        self.enabled = True


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOPELOOP_LOCK_DIR", str(tmp_path / "locks"))
    return ZeroPiHatConfig(revision="0.1.0", expected_pi_serial="test-pi",
                           wiring_confirmed=True, loads=["test-load"],
                           socket_path=str(tmp_path / "owner.sock"))


@pytest.fixture
async def owner(config):
    service = HatOwner(config, FakeGPIO())
    await service.start()
    yield service
    await service.close()


def test_unverified_revision_and_unknown_fields_rejected():
    with pytest.raises(ValidationError):
        ZeroPiHatConfig(revision="0.0.1")
    with pytest.raises(ValidationError):
        ZeroPiHatConfig(revision="0.1.0", enable_gpio=4)


@pytest.mark.parametrize("consumer,mux", [("other-bench", "7: op -- pu | lo"),
                                         (None, "7: a0 -- pu | hi // SPI0_CE1_N")])
def test_backend_never_overrides_busy_or_muxed_pin(config, monkeypatch, consumer, mux):
    calls = []
    monkeypatch.setitem(sys.modules, "gpiod", SimpleNamespace(
        request_lines=lambda *a, **kw: calls.append(kw)))
    monkeypatch.setitem(sys.modules, "gpiod.line", SimpleNamespace(
        Bias=SimpleNamespace(), Direction=SimpleNamespace(), Value=SimpleNamespace()))
    backend = GpiodBackend(config)
    monkeypatch.setattr(backend, "inspect", lambda: {"lines": {
        "7": {"used": bool(consumer), "consumer": consumer, "mux": mux},
        "8": {"used": False, "consumer": None, "mux": "8: ip pu | hi"}}})
    with pytest.raises(RuntimeError, match="no takeover|no mux override"):
        backend.claim_off()
    backend.release()
    assert calls == []


def test_backend_initial_low_is_part_of_request(config, monkeypatch):
    calls = []
    request = SimpleNamespace(release=lambda: calls.append("release"))
    monkeypatch.setitem(sys.modules, "gpiod", SimpleNamespace(
        LineSettings=lambda **kw: kw,
        request_lines=lambda *a, **kw: (calls.append(kw) or request)))
    monkeypatch.setitem(sys.modules, "gpiod.line", SimpleNamespace(
        Bias=SimpleNamespace(PULL_UP="up"), Direction=SimpleNamespace(OUTPUT="out", INPUT="in"),
        Value=SimpleNamespace(INACTIVE=0)))
    backend = GpiodBackend(config)
    monkeypatch.setattr(backend, "inspect", lambda: {"lines": {
        str(n): {"used": False, "consumer": None, "mux": f"{n}: ip pu | hi"}
        for n in (7, 8)}})
    backend.claim_off()
    assert calls[0]["config"][7]["output_value"] == 0
    assert calls[0]["config"][8]["direction"] == "in"
    backend.release()
    assert calls[-1] == "release"


@pytest.mark.parametrize("change", [{"expected_pi_serial": None}, {"loads": []},
                                  {"wiring_confirmed": False}])
async def test_missing_identity_or_load_map_never_claims(config, change):
    service = HatOwner(config.model_copy(update=change), FakeGPIO())
    with pytest.raises(ValueError):
        await service.start()
    assert service.backend.writes == []


async def test_client_commands_keep_off_hold_and_do_not_claim_voltage(owner):
    first = await owner.command(HatRequest(operation="status"))
    assert first["enable_gpio_high"] is False
    assert first["rail_voltage_v"] is None and first["rail_state"] == "not_measured"
    await owner.command(HatRequest(operation="output", enabled=True))
    result = await owner.command(HatRequest(operation="output", enabled=False))
    assert result["enable_gpio_high"] is False and owner.backend.claimed
    assert owner.lease._file is not None


async def test_cross_process_lease_blocks_second_owner_before_gpio(owner, config):
    other = HatOwner(config, FakeGPIO())
    with pytest.raises(ResourceError, match="busy"):
        await other.start()
    assert other.backend.writes == []


async def test_busy_gpio_is_not_taken_over_and_lease_is_released(config):
    backend = FakeGPIO()
    backend.claimed = True
    service = HatOwner(config, backend)
    with pytest.raises(ResourceError, match="already owned"):
        await service.start()
    assert backend.writes == [] and service.lease._file is None


async def test_active_fault_blocks_enable_but_not_off(owner):
    owner.backend.fault = True
    with pytest.raises(RuntimeError, match="Fault"):
        await owner.command(HatRequest(operation="output", enabled=True))
    assert not owner.backend.enabled
    await owner.command(HatRequest(operation="output", enabled=False))
    assert owner.backend.writes[-1] is False


async def test_off_is_written_even_if_readback_fails(owner):
    owner.backend.enabled = True
    owner.backend.read_error = True
    with pytest.raises(OSError):
        await owner.command(HatRequest(operation="output", enabled=False))
    assert owner.backend.writes[-1] is False and not owner.backend.enabled


async def test_failed_enable_readback_returns_to_off(owner):
    original = owner.backend.read
    calls = 0

    def read():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("post-enable read failed")
        return original()

    owner.backend.read = read
    with pytest.raises(OSError):
        await owner.command(HatRequest(operation="output", enabled=True))
    assert owner.backend.writes[-2:] == [True, False]


async def test_cycle_does_not_enable_already_off(owner):
    with pytest.raises(RuntimeError, match="already off"):
        await owner.command(HatRequest(operation="cycle"))
    assert owner.backend.writes == [False]


async def test_successful_cycle_retains_owner(owner):
    owner.change(True)
    result = await owner.command(HatRequest(operation="cycle", off_seconds=.5, settle_seconds=0))
    assert owner.backend.writes[-2:] == [False, True]
    assert result["enable_gpio_high"] and owner.backend.claimed


async def test_cycle_cancellation_holds_off_and_competing_call_fails(owner):
    owner.change(True)
    task = asyncio.create_task(owner.command(HatRequest(operation="cycle", off_seconds=10)))
    await asyncio.sleep(.02)
    with pytest.raises(ResourceError):
        await owner.command(HatRequest(operation="output", enabled=True), "other")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not owner.backend.enabled and owner.backend.claimed
    assert owner.lease._file is not None


@pytest.mark.parametrize("payload", [
    {"operation": "output"}, {"operation": "output", "enabled": "false"},
    {"operation": "cycle", "off_seconds": float("nan")},
    {"operation": "cycle", "off_seconds": 11}, {"operation": "unknown"},
    {"operation": "status", "enabled": True},
])
def test_invalid_requests_rejected(payload):
    with pytest.raises(ValidationError):
        HatRequest.model_validate(payload)


def project_config(config):
    return Config(project={"name": "test"}, hardware={"mcu": "test"},
                  build={"system": "test"}, zero_pi_hat=config)


def test_cli_requires_explicit_initial_off_and_exposes_commands(config, tmp_path, monkeypatch):
    path = tmp_path / "fixture.json"
    path.write_text(project_config(config).model_dump_json())
    result = CliRunner().invoke(app, ["serve", "--config", str(path)])
    assert result.exit_code != 0 and "--start-off" in result.output
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert all(name in result.output for name in ["inspect", "serve", "output", "cycle", "status"])


async def test_mcp_discovery_routing_and_error_is_not_success(config, monkeypatch):
    monkeypatch.setattr(mcp_server, "_config", project_config(config))
    monkeypatch.setattr(mcp_server, "_session_manager", None)
    calls = []

    async def fake_command(settings, operation, **args):
        calls.append((settings, operation, args))
        HatRequest(operation=operation, **args)
        return {"enable_gpio_high": False, "rail_voltage_v": None}

    monkeypatch.setattr("scopeloop.zero_pi_hat.command", fake_command)
    names = {tool.name for tool in await mcp_server.list_tools()}
    assert {"scopeloop_hat_status", "scopeloop_hat_output", "scopeloop_hat_cycle"} <= names
    response = await mcp_server.call_tool("scopeloop_hat_output", {"enabled": False})
    assert json.loads(response[0].text)["rail_voltage_v"] is None
    assert calls[-1][1:] == ("output", {"enabled": False})
    with pytest.raises(ValidationError):
        await mcp_server.call_tool("scopeloop_hat_output", {"enabled": "false"})


@pytest.mark.skipif(sys.platform != "linux", reason="Unix socket transport runs on the Pi")
async def test_real_socket_client_disconnect_keeps_gpio_off(config):
    service = HatOwner(config, FakeGPIO())
    task = asyncio.create_task(serve(config, service))
    try:
        for _ in range(100):
            if service.started:
                break
            if task.done():
                await task
            await asyncio.sleep(.01)
        result = await command(config, "status")
        assert result["enable_gpio_high"] is False
        second = HatOwner(config, FakeGPIO())
        with pytest.raises(ResourceError, match="busy"):
            await serve(config, second)
        assert (await command(config, "status"))["enable_gpio_high"] is False
        # A truncated request/client disconnect must not release the GPIO hold.
        _, disconnected = await asyncio.open_unix_connection(config.socket_path)
        disconnected.write(b'{"request_id":')
        await disconnected.drain()
        disconnected.close()
        await disconnected.wait_closed()
        await asyncio.sleep(.02)
        assert service.backend.claimed and not service.backend.enabled
        await command(config, "output", enabled=True)
        await command(config, "output", enabled=False)
        assert service.backend.claimed and not service.backend.enabled
        wrong = config.model_copy(update={"loads": ["different-load"]})
        with pytest.raises(RuntimeError, match="configuration differs"):
            await command(wrong, "output", enabled=True)
        assert service.backend.writes == [False, True, False]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not Path(config.socket_path).exists()
