"""Socket-level replay of documented and observed SDS1104X-E response shapes."""

import asyncio
from contextlib import asynccontextmanager

import numpy as np
import pytest

from scopeloop.instruments.base import InstrumentError
from scopeloop.instruments.siglent_scope import SiglentSDS1000X
from scopeloop.scpi import integer, number


@asynccontextmanager
async def replay(
    monkeypatch,
    tmp_path,
    *,
    payload=b"\x00\x0a\x0d\xff",
    fresh=True,
    truncate=False,
    prefix=b"C1:WF DAT2,",
    count=4,
    trailer=b"\n\n",
):
    monkeypatch.setenv("SCOPELOOP_LOCK_DIR", str(tmp_path / "locks"))
    commands = []
    connections = set()

    async def serve(reader, writer):
        connections.add(writer)
        mode = "STOP"
        pending = False
        points = 0
        try:
            while line := await reader.readline():
                command = line.decode().strip()
                commands.append(command)
                response = None
                if command == "STOP":
                    mode = "STOP"
                elif command.startswith("TRMD "):
                    mode = command.split()[-1]
                    pending = True
                elif command.startswith("WFSU "):
                    points = int(command.split(",")[3])
                elif command == "INR?":
                    response = f"INR {8193 if pending and fresh else 8192 if pending else 1}"
                elif command == "TRMD?":
                    response = f"TRMD {mode}"
                elif command == "WFSU?":
                    response = f"WFSU SP,1,NP,{points},FP,0"
                elif command == "C1:WF? DAT2":
                    data = payload[:points] if points else payload
                    frame = prefix + b"#9" + f"{len(data):09}".encode() + data + trailer
                    if truncate:
                        frame = frame[:-4]
                    # Deliberately split every header, data and trailer byte.
                    for byte in frame:
                        writer.write(bytes([byte]))
                        await writer.drain()
                        await asyncio.sleep(0)
                    if truncate:
                        break
                else:
                    response = {
                        "*IDN?": "Siglent Technologies,SDS1104X-E,REPLAY,8.2.6.1.37R9",
                        "ACQW?": "ACQW SAMPLING",
                        "TDIV?": "TDIV 2.00E-05S",
                        "TRDL?": "TRDL 1.00E-06S",
                        "SARA?": "SARA 5.00E+08Sa/s",
                        "SANU? C1": f"SANU {count}pts",
                        "BWL?": "BWL C1,ON,C2,OFF",
                        "TRSE?": "TRSE EDGE,SR,C1,HT,OFF",
                        "C1:VDIV?": "C1:VDIV 1.24E+00V",
                        "C1:OFST?": "C1:OFST -2.50E+00V",
                        "C1:ATTN?": "C1:ATTN 10",
                        "C1:CPL?": "C1:CPL D1M",
                        "C1:TRA?": "C1:TRA ON",
                    }.get(command)
                if response is not None:
                    writer.write((response + "\n").encode())
                    await writer.drain()
        finally:
            writer.close()
            connections.discard(writer)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    scope = SiglentSDS1000X("127.0.0.1", server.sockets[0].getsockname()[1], timeout=0.5)
    try:
        async with scope:
            yield scope, commands
    finally:
        server.close()
        await server.wait_closed()
        for writer in list(connections):
            writer.close()
        await asyncio.sleep(0.01)


@pytest.mark.parametrize(
    "reply,prefix,expected",
    [
        ("C1:VDIV 1.24E+00V", "C1:VDIV", 1.24),
        ("TDIV 2.00E-05S", "TDIV", 2e-5),
        ("SARA 5.00E+08Sa/s", "SARA", 5e8),
        ("1 GSa/s", None, 1e9),
        (".5mV", None, 0.0005),
        ("1MHz", None, 1e6),
        ("1mV", None, 0.001),
        ("1MV", None, 1e6),
    ],
)
def test_numeric_shapes(reply, prefix, expected):
    assert number(reply, prefix) == expected


@pytest.mark.parametrize("reply", ["", "****", "nan", "inf", "1e999", "1V garbage", "1quux"])
def test_reject_bad_numbers(reply):
    with pytest.raises(InstrumentError):
        number(reply)


def test_prefixed_inr():
    assert number("C1:VOLT_DIV 500mV", "C1:VDIV") == 0.5
    assert integer("INR 8193", "INR") & 1
    with pytest.raises(InstrumentError):
        integer("INR 1.5", "INR")


async def test_split_transfer_partial_timing_and_no_double_attenuation(monkeypatch, tmp_path):
    async with replay(monkeypatch, tmp_path) as (scope, commands):
        wave = await scope.capture_waveform(points=2)
        assert wave.sample_rate == 5e8
        assert wave.time_offset == pytest.approx(1e-6 - 7 * 2e-5)
        assert np.diff(wave.time_array)[0] == pytest.approx(2e-9)
        assert wave.samples.tolist() == pytest.approx([2.5, 2.996])
        assert wave.metadata["partial_transfer"] is None
        assert wave.metadata["acquired_points"] is None
        assert wave.metadata["freshness"]["events"][0]["inr"] == "INR 8193"
        assert b"#9000000002" in wave.raw_response
        assert "ARM" not in commands
        assert await scope.query("TDIV?") == "TDIV 2.00E-05S"


async def test_stale_inr_and_ready_without_acquisition_do_not_return_wave(monkeypatch, tmp_path):
    async with replay(monkeypatch, tmp_path, fresh=False) as (scope, commands):
        with pytest.raises(InstrumentError, match="No fresh acquisition"):
            await scope.capture_waveform(acquisition="single", timeout=0.12)
        assert "C1:WF? DAT2" not in commands
        assert "TRMD SINGLE" in commands


@pytest.mark.parametrize(
    "kwargs", [{"truncate": True}, {"payload": b""}, {"prefix": b"ERROR "}, {"trailer": b"XX"}]
)
async def test_bad_binary_closes_stream(monkeypatch, tmp_path, kwargs):
    async with replay(monkeypatch, tmp_path, **kwargs) as (scope, _):
        with pytest.raises((InstrumentError, asyncio.IncompleteReadError)):
            await scope.capture_waveform()
        assert not scope.is_connected


async def test_unsupported_sanu_is_never_queried(monkeypatch, tmp_path):
    async with replay(monkeypatch, tmp_path, count=8) as (scope, commands):
        waveform = await scope.capture_waveform()
        assert waveform.record_length == 4
        assert not any(command.startswith("SANU") for command in commands)


async def test_concurrent_query_cannot_split_capture(monkeypatch, tmp_path):
    async with replay(monkeypatch, tmp_path) as (scope, commands):
        wave, result = await asyncio.gather(scope.capture_waveform(), scope.query("TDIV?"))
        assert len(wave.samples) == 4
        assert result == "TDIV 2.00E-05S"
        assert commands[-1] == "TDIV?"


async def test_second_client_refused_before_scpi(monkeypatch, tmp_path):
    async with replay(monkeypatch, tmp_path) as (scope, commands):
        other = SiglentSDS1000X("127.0.0.1", scope.port)
        with pytest.raises(Exception, match="Resource busy"):
            await other.connect()
        assert commands == ["*IDN?"]
