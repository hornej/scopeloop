"""Tests for the user-facing logic CLI surface."""

from pathlib import Path

from typer.testing import CliRunner

import scopeloop.cli as cli
from scopeloop.config import Config
from scopeloop.logic import EvidenceBundle


class FakeCliService:
    def __init__(self) -> None:
        self.metadata = None

    async def connect(self):
        return {"connected": True, "software": {"logic_app_version": "2.4.46"}}

    async def capture_evidence(self, recipe, metadata, output_root):
        self.metadata = metadata
        return EvidenceBundle(
            bundle_dir=output_root / "bundle",
            manifest_path=output_root / "bundle" / "manifest.json",
            capture_id="capture-1",
            files={},
        )

    async def disconnect(self, close_captures=False):
        return None


def test_logic_cli_lists_full_workflow_commands():
    result = CliRunner().invoke(cli.app, ["logic", "--help"])
    assert result.exit_code == 0
    for command in ("connect", "capture", "decode", "export", "save", "compare"):
        assert command in result.stdout


def test_logic_capture_cli_routes_recipe_and_metadata(monkeypatch, tmp_path: Path):
    service = FakeCliService()
    config = Config.model_validate(
        {
            "project": {"name": "cli-test"},
            "hardware": {"mcu": "esp32"},
            "build": {"system": "esp-idf"},
            "instruments": {"logic_analyzer": {"type": "saleae"}},
        }
    )
    monkeypatch.setattr(cli, "_logic_service", lambda path: (config, service))

    result = CliRunner().invoke(
        cli.app,
        [
            "logic",
            "capture",
            "--recipe",
            "boot",
            "--metadata",
            "serial=unit-1",
            "--metadata",
            "fixture_power=false",
            "--output-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "capture-1" in result.stdout
    assert service.metadata == {"serial": "unit-1", "fixture_power": False}
