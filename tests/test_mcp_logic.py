"""Tests that MCP logic handlers route to the shared functional workflow."""

import asyncio
from pathlib import Path

import pytest

import scopeloop.mcp_server as mcp_server
from scopeloop.config import Config
from scopeloop.logic import EvidenceBundle
from scopeloop.resources import TaskRLock


class FakeLogicService:
    async def connect(self):
        return {"connected": True}

    async def capture_evidence(self, recipe, metadata, output_root):
        return EvidenceBundle(
            bundle_dir=output_root / "bundle",
            manifest_path=output_root / "bundle" / "manifest.json",
            capture_id="capture-1",
            files={"capture.sal": {"sha256": "abc", "bytes": 3}},
        )

    async def decode_uart(self, capture_id, uart, output):
        return output.resolve()

    async def save_capture(self, capture_id, output):
        return output.resolve()

    async def export_capture(self, capture_id, output_dir, digital, analog):
        return [output_dir.resolve() / "digital.csv"]


@pytest.fixture
def mcp_logic(monkeypatch):
    config = Config.model_validate(
        {
            "project": {"name": "mcp-test"},
            "hardware": {"mcu": "esp32"},
            "build": {"system": "esp-idf"},
            "runtime": {"data_dir": "./data"},
            "instruments": {"logic_analyzer": {"type": "saleae"}},
        }
    )
    monkeypatch.setattr(mcp_server, "_config", config)
    monkeypatch.setattr(mcp_server, "_logic_service", FakeLogicService())


@pytest.mark.asyncio
async def test_mcp_logic_connect_and_capture_are_implemented(mcp_logic, tmp_path: Path):
    assert await mcp_server._handle_logic_connect() == {"connected": True}

    result = await mcp_server._handle_logic_capture(
        {"recipe": "boot", "metadata": {"serial": "one"}, "output_root": str(tmp_path)}
    )

    assert result["capture_id"] == "capture-1"
    assert result["bundle_dir"] == str(tmp_path / "bundle")


@pytest.mark.asyncio
async def test_mcp_logic_decode_save_and_export_are_implemented(mcp_logic, tmp_path: Path):
    decoded = await mcp_server._handle_logic_decode(
        {
            "protocol": "UART",
            "capture_id": "capture-1",
            "channel": 3,
            "baud_rate": 115200,
            "output": str(tmp_path / "uart.csv"),
        }
    )
    saved = await mcp_server._handle_logic_save(
        {"capture_id": "capture-1", "output": str(tmp_path / "copy.sal")}
    )
    exported = await mcp_server._handle_logic_export(
        {"capture_id": "capture-1", "output_dir": str(tmp_path / "raw")}
    )

    assert decoded["decoded_csv"].endswith("uart.csv")
    assert saved["saved_capture"].endswith("copy.sal")
    assert exported["raw_csv"][0].endswith("digital.csv")


@pytest.mark.asyncio
async def test_init_waits_for_logic_operation_and_closes_owned_captures(monkeypatch, tmp_path):
    started, release = asyncio.Event(), asyncio.Event()
    closed = []

    class ConnectedService:
        async def connect(self):
            started.set()
            await release.wait()
            return {"connected": True}

        async def disconnect(self, close_captures=False):
            closed.append(close_captures)

    monkeypatch.setattr(mcp_server, "_logic_lifecycle_lock", TaskRLock())
    monkeypatch.setattr(mcp_server, "_logic_service", ConnectedService())
    monkeypatch.setattr(mcp_server, "_config", None)
    capture = asyncio.create_task(mcp_server._handle_tool("scopeloop_logic_connect", {}))
    await started.wait()
    replacement = asyncio.create_task(
        mcp_server._handle_tool(
            "scopeloop_init", {"project_name": "replacement", "path": str(tmp_path)}
        )
    )
    await asyncio.sleep(0)
    assert not replacement.done()
    assert not (tmp_path / "scopeloop.yaml").exists()
    release.set()
    await capture
    assert (await replacement)["success"]
    assert closed == [True]
    assert mcp_server._logic_service is None
    assert mcp_server._config.project.name == "replacement"
