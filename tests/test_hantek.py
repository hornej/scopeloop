"""Replay observed Hantek firmware response shapes; identity is synthetic.

DMM and setup frames below are verbatim wire responses from the commissioning
run. Emulator setpoints and waveform payloads are synthetic for failure testing.
"""

import asyncio
import json
import struct
import threading
from collections import deque

import numpy as np
import pytest
from typer.testing import CliRunner

from scopeloop import mcp_server
from scopeloop.cli import app
from scopeloop.config import HantekConfig
from scopeloop.evidence import validate_bundle
from scopeloop.hantek import HantekService, loopback, save_scope, scope_bundle
from scopeloop.hantek_mcp import TOOLS
from scopeloop.instruments.hantek import (
    Hantek2000,
    ProtocolError,
    decode_dmm,
    decode_scope,
    packet,
)
from scopeloop.instruments.hantek_usb import USBError
from scopeloop.resources import ResourceError
from scopeloop.viewers import export_viewers

DMM = "550b010501000300020309050155"  # +0.239 V DC, observed front-panel match
SETUP = "55190015010100000664010000000232000011000000000064"


class Replay:
    def __init__(self):
        self.opened = False
        self.tx = []
        self.pending = deque()
        self.awg = {0: 2, 1: 100, 2: 1000, 3: 0, 8: 0}
        self.scope = bytearray.fromhex(SETUP)[4:]
        self.dmm = bytearray.fromhex(DMM)
        self.override = {}
        self.frames = 0
        self.signal = 154
        self.busy = False
        self.fail_write = None

    def open(self):
        self.opened = True
        return {"transport": "replay"}

    def close(self):
        self.opened = False

    def write(self, p):
        assert self.opened
        self.tx.append(p)
        if self.fail_write and self.fail_write(p):
            raise USBError("injected write failure")
        if p in self.override:
            value = self.override[p]
            if isinstance(value, Exception):
                raise value
            self.pending.append(value)
        elif len(p) == 5:
            if p[3]:
                self.pending.append(bytes(self.dmm))
            else:
                self.dmm[3] = p[4]
        elif p[0]:
            if self.busy:
                self.pending.append(bytes.fromhex("0000ffff") * 16)
                self.busy = False
                return
            if p[0] == 1:
                self.frames += 1
            n = min(64, 6000 - (p[0] - 1) * 64)
            code = self.signal
            self.pending.append(bytes([code, 80]) * (n // 2))
        else:
            _, _, group, read, cmd, value, _ = struct.unpack("<BBBBBIB", p)
            if read:
                if group == 0:
                    self.pending.append(bytes.fromhex("55190015") + self.scope)
                elif group == 2:
                    # Real reply values start at byte FOUR; tail is stale scratch space.
                    self.pending.append(
                        bytes([0x55, 7, 2, cmd]) + self.awg[cmd].to_bytes(4, "little") + b"\xab\xcd"
                    )
                else:
                    self.pending.append(
                        {
                            9: b"\x55\x24\x03\x09TEST0001\0" + bytes(26),
                            10: b"\x55\x15\x03\x0a2021090901\0" + bytes(9),
                            1: b"\x55\x0d\x03\x01" + bytes(range(12)),
                        }[cmd]
                    )
            elif group == 2:
                self.awg[cmd] = value
            elif group == 0:
                self.scope[{16: 17, 17: 18, 18: 19}.get(cmd, cmd)] = value

    def read(self, size):
        assert self.opened
        result = self.pending.popleft()
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def bench(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOPELOOP_LOCK_DIR", str(tmp_path / "locks"))
    transport = Replay()
    config = HantekConfig(expected_serial="TEST0001")
    d = Hantek2000(config, transport)
    return transport, HantekService(config, driver=d)


@pytest.fixture
def fast(monkeypatch):
    sleep = asyncio.sleep

    async def quick(_):
        await sleep(0)

    monkeypatch.setattr(asyncio, "sleep", quick)


@pytest.mark.parametrize(
    ("raw", "value"),
    [
        (DMM, 0.239),
        ("550b010501000301000001050155", 1.001),
        ("550b010501010300090909050155", -0.999),
        ("550b010601000300070005050155", 0.705),
    ],
)
def test_observed_dmm_frames(raw, value):
    result = decode_dmm(bytes.fromhex(raw))
    assert result["value"] == value
    assert result["unit"] == "V"
    assert result["overload"] is False


def test_prefix_overload_and_bad_frames():
    p = bytearray.fromhex(DMM)
    p[3], p[11] = 8, 3
    assert decode_dmm(bytes(p))["value"] == 239
    p[7] = 10
    assert decode_dmm(bytes(p))["value"] is None
    assert decode_dmm(bytes(p))["overload"]
    for index, value in [(0, 0), (3, 11), (5, 2), (6, 5), (11, 6), (13, 0)]:
        p = bytearray.fromhex(DMM)
        p[index] = value
        with pytest.raises(ProtocolError):
            decode_dmm(bytes(p))
    with pytest.raises(ProtocolError):
        decode_dmm(bytes.fromhex(DMM)[:-1])


async def test_status_evidence_and_unknown_output(bench, tmp_path):
    replay, service = bench
    bundle = tmp_path / "status"
    result = await service.command("status", {"output": bundle})
    assert result["dmm"]["value"] == 0.239
    assert result["awg"]["amplitude_vpp"] == 2
    assert result["awg"]["output_enabled"] is None
    assert packet(2, 8, read=True) not in replay.tx  # Known stale firmware reply
    assert not replay.opened
    assert validate_bundle(bundle)["status"] == "complete"
    assert not any(p[3] == 0 for p in replay.tx)
    assert list((bundle / "sessions").glob("*/timeline.jsonl"))
    with pytest.raises(FileExistsError):
        await service.command("status", {"output": bundle})


async def test_identity_mismatch_releases_host_lease(bench):
    replay, service = bench
    service.driver.config.expected_serial = "WRONG"
    with pytest.raises(ResourceError, match="serial"):
        async with service.session():
            pass
    assert not replay.opened
    assert replay.tx == [packet(3, 9, read=True)]
    service.driver.config.expected_serial = "TEST0001"
    await service.command("dmm_read", {})


async def test_host_lease_blocks_second_driver(bench):
    replay, service = bench
    other = Replay()
    async with service.session():
        with pytest.raises(ResourceError, match="busy"):
            await Hantek2000(service.driver.config, other).connect()
        assert not other.opened
    assert not replay.opened


@pytest.mark.parametrize(
    "settings",
    [
        {"amplitude_vpp": 4, "offset_v": 1},
        {"amplitude_vpp": float("nan")},
        {"frequency_hz": 0},
        {"frequency_hz": 100.5},
        {"waveform": "arbitrary"},
        {"waveform": "ramp", "frequency_hz": 2_000_000},
    ],
)
async def test_invalid_awg_has_no_writes(bench, settings):
    replay, service = bench
    with pytest.raises(ValueError):
        await service.driver.awg_configure(settings)
    assert not replay.tx


async def test_awg_negative_offset_readback_and_cleanup(bench, fast):
    replay, service = bench
    async with service.session() as d:
        result = await d.awg_configure({"amplitude_vpp": 1, "offset_v": -1})
        assert result["offset_v"] == -1
        assert replay.awg[2] == 500
        assert replay.awg[3] == 66536
        await d.awg_output(True)
        assert replay.awg[8] == 1
    assert replay.awg[8] == 0


async def test_failed_enable_attempts_off(bench):
    replay, service = bench
    replay.fail_write = lambda p: p == packet(2, 8, 1)
    with pytest.raises(USBError):
        async with service.session() as d:
            await d.awg_output(True)
    assert replay.tx[-1] == packet(2, 8, 0)
    assert not replay.opened


async def test_failed_off_is_not_success(bench, tmp_path, fast):
    replay, service = bench
    # Configuration off succeeds; the off after enable fails.
    replay.fail_write = lambda p: replay.awg[8] == 1 and p == packet(2, 8, 0)
    with pytest.raises(USBError):
        await service.command(
            "awg_run", {"settings": {}, "duration_s": 0.2, "output": tmp_path / "failed"}
        )
    manifest = json.loads((tmp_path / "failed/manifest.json").read_text())
    assert manifest["status"] == "failed"
    assert not replay.opened
    assert service.manager.get_lock_info(service.key) is None


async def test_stale_reply_rejected(bench):
    replay, service = bench
    replay.override[packet(2, 1, read=True)] = bytes.fromhex("55070203e80300000000")
    with pytest.raises(ProtocolError, match="Wrong/truncated"):
        await service.command("status", {})
    assert not replay.opened


async def test_scope_settings_validated_before_write(bench, fast):
    replay, service = bench
    async with service.session() as d:
        count = len(replay.tx)
        with pytest.raises(ValueError):
            await d.scope_configure(enabled=True, seconds_per_div=0.003)
        assert len(replay.tx) == count
        result = await d.scope_configure(
            trigger_source="CH2", trigger_slope="falling", trigger_mode="single"
        )
        assert result["trigger_source"] == "CH2"
        assert result["trigger_mode"] == "single"
        with pytest.raises(ValueError, match="AUTO"):
            await d.scope_capture()


async def test_capture_pipeline_zero_reference_and_viewers(bench, tmp_path, fast):
    replay, service = bench
    replay.busy = True
    reference = tmp_path / "zero"
    async with service.session() as d:
        replay.signal = 128
        scope_bundle(await d.scope_capture(), reference, d.identity, kind="hantek_scope_zero")
        replay.signal = 154
    bundle = tmp_path / "scope"
    result = await service.command(
        "scope_capture", {"output": bundle, "zero_reference": str(reference)}
    )
    assert replay.frames == 4  # Discard + retain for reference and signal
    assert replay.scope[1] == 1 and replay.scope[12] == 0
    assert result["nominal_measurements"]["mean_v"] == pytest.approx(26 / 25.6)
    assert result["sample_count"] == 3000
    assert validate_bundle(bundle)["waveform"]["calibrated"] is False
    exports = export_viewers(bundle, tmp_path / "viewers")
    assert exports["status"] == "complete"
    service.driver.config.scope_zero_reference = str(reference)
    replay.scope[4] = 5
    with pytest.raises(ValueError, match="channel settings"):
        await service.command("scope_capture", {"output": tmp_path / "wrong-scale"})
    replay.scope[4] = 6
    (reference / "waveform.bin").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="hash mismatch"):
        await service.command("scope_capture", {"output": tmp_path / "corrupt"})


async def test_short_scope_read_restores_coupling_and_stops(bench, tmp_path, fast):
    replay, service = bench
    replay.override[struct.pack("<BBHBHHB", 1, 10, 0x100, 0x16, 3000, 3000, 20)] = b"short"
    with pytest.raises(ProtocolError, match="Truncated"):
        await service.command("scope_capture", {"output": tmp_path / "failed"})
    assert replay.scope[1] == 1 and replay.scope[12] == 0
    assert not replay.opened


async def test_cancel_waits_for_native_read_before_close(bench):
    replay, service = bench
    started, finish = threading.Event(), threading.Event()
    original_read = replay.read

    def blocked_read(size):
        if replay.tx[-1] == bytes.fromhex("0005010100"):
            started.set()
            assert finish.wait(3)
            assert replay.opened
        return original_read(size)

    replay.read = blocked_read
    task = asyncio.create_task(service.command("dmm_read", {}))
    while not started.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()
    assert replay.opened
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not replay.opened
    assert service.manager.get_lock_info(service.key) is None


async def test_loopback_restores_dmm_on_failure(bench, tmp_path, fast):
    replay, service = bench
    replay.dmm[7] = 10
    async with service.session() as d:
        with pytest.raises(ValueError, match="overload"):
            await loopback(d, output=tmp_path)
        assert replay.awg[8] == 0
        assert replay.dmm[3] == 5


def test_raw_only_export_refuses_voltage_interpretation(tmp_path):
    result = save_scope(
        {
            "raw": bytes([128, 80]) * 3000,
            "discarded": bytes(6000),
            "channel": "CH1",
            "setup": decode_scope(bytes.fromhex(SETUP)),
        },
        tmp_path,
    )
    assert "waveform" not in result
    assert "unavailable" in result["voltage_scaling"]
    assert np.load(tmp_path / "raw-waveform.npz")["adc_codes"].shape == (3000,)


def test_cli_screen_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr("scopeloop.hantek_cli._run", lambda *a, **kw: calls.append((a, kw)))
    result = CliRunner().invoke(app, ["hantek", "screen", "dmm"])
    assert result.exit_code == 0, result.output
    assert calls[0][1]["name"] == "dmm"


def test_cli_invalid_json_does_not_open_usb(tmp_path):
    config = tmp_path / "bench.yaml"
    config.write_text(
        "project: {name: test}\nhardware: {mcu: bench}\n"
        "build: {system: custom}\ninstruments: {hantek: {}}\n"
    )
    settings = tmp_path / "settings.json"
    settings.write_text("{broken")
    result = CliRunner().invoke(
        app, ["hantek", "awg-configure", str(settings), "--config", str(config)]
    )
    assert result.exit_code == 1
    assert "property name" in result.output


async def test_cancel_generator_turns_off_and_records_failure(bench, tmp_path):
    replay, service = bench
    task = asyncio.create_task(
        service.command(
            "awg_run", {"settings": {}, "duration_s": 10, "output": tmp_path / "cancelled"}
        )
    )
    while not replay.awg[8]:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert replay.awg[8] == 0 and not replay.opened
    manifest = json.loads((tmp_path / "cancelled/manifest.json").read_text())
    assert manifest["status"] == "failed"
    assert manifest["error"]["type"] == "CancelledError"


async def test_mcp_discovery_dispatch_and_errors(bench, monkeypatch):
    _, service = bench
    names = [t.name for t in await mcp_server.list_tools()]
    assert len(TOOLS) == 10
    assert all(t.name in names for t in TOOLS)
    monkeypatch.setattr(mcp_server, "_hantek_service", service)
    monkeypatch.setattr(
        mcp_server,
        "_get_config",
        lambda: type(
            "Config",
            (),
            {"instruments": type("Instruments", (), {"hantek": service.driver.config})()},
        )(),
    )
    monkeypatch.setattr(mcp_server, "_session_manager", None)
    result = await mcp_server._handle_tool("scopeloop_hantek_dmm_read", {})
    assert result["value"] == 0.239
    with pytest.raises(ValueError):
        await mcp_server.call_tool("scopeloop_hantek_dmm_configure", {"mode": "invalid"})
