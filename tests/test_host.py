"""Tests for instrument host environment checks."""

from pathlib import Path

from scopeloop import host as host_module
from scopeloop.config import Config
from scopeloop.host import collect_host_status, format_bytes


def test_format_bytes():
    assert format_bytes(0) == "0 B"
    assert format_bytes(1024) == "1.0 KB"
    assert format_bytes(1024 * 1024) == "1.0 MB"


def test_collect_host_status_without_config(tmp_path: Path):
    status = collect_host_status(data_dir=tmp_path)

    assert status.mode == "local"
    assert status.host is None
    assert status.port == 8765
    assert status.data_dir == str(tmp_path)
    assert status.storage.exists is True
    assert any(check.name == "Python" and check.available for check in status.checks)


def test_config_runtime_defaults():
    config = Config.model_validate(
        {
            "project": {"name": "test"},
            "hardware": {"mcu": "esp32"},
            "build": {"system": "esp-idf"},
        }
    )

    assert config.runtime.mode == "local"
    assert config.runtime.host is None
    assert config.runtime.port == 8765
    assert config.runtime.data_dir == "~/ScopeLoop"


def test_windows_app_candidates(monkeypatch):
    monkeypatch.setattr(host_module.platform, "system", lambda: "Windows")

    saleae_paths = [path.as_posix() for path in host_module._saleae_app_candidates()]
    pico_paths = [path.as_posix() for path in host_module._picoscope_app_candidates()]

    assert "C:/Program Files/Logic/Logic.exe" in saleae_paths
    assert "C:/Program Files/Pico Technology/PicoScope 7 T&M Stable/PicoScope.exe" in pico_paths
