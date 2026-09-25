import asyncio
import json
import shutil
import subprocess
import sys
from types import SimpleNamespace
from zipfile import ZipFile

import numpy as np
import pytest

from scopeloop.devices import DeviceInfo, DeviceManager, DeviceMatch
from scopeloop.resources import (
    ClientPriority,
    HostLease,
    LockMode,
    LockTimeoutError,
    ResourceError,
    ResourceManager,
)
from scopeloop.saleae_labels import label_and_verify, make_labeled_copy, sha256, verify_labels
from scopeloop.scope_evidence import ScopeRecipe, capture_scope_evidence, waveform_metrics
from tests.test_siglent_replay import replay


async def test_priority_observe_and_same_client_other_task_cannot_preempt():
    manager = ResourceManager()

    async def connect():
        return object()

    async def disconnect(connection):
        pass

    await manager.register("scope", "scope", connect, disconnect)
    async with manager.lock("scope", "one"):
        async with manager.lock("scope", "one"):
            assert manager.is_locked("scope")
        assert manager.is_locked("scope")

        async def contender(mode, client):
            with pytest.raises(LockTimeoutError):
                async with manager.lock("scope", client, mode, ClientPriority.SYSTEM, 0.01):
                    pytest.fail("must not preempt")
            with pytest.raises(ResourceError):
                await manager.disconnect("scope")

        await asyncio.gather(
            contender(LockMode.OBSERVE, "two"), contender(LockMode.CONTROL_TRANSFER, "one")
        )
    assert not manager.is_locked("scope")


def test_host_lease_cross_process_and_crash_release(tmp_path):
    key = "scope:test"
    owner = HostLease(key, tmp_path)
    owner.acquire()
    code = (
        "from pathlib import Path; from scopeloop.resources import HostLease; "
        f"HostLease({key!r}, Path({str(tmp_path)!r})).acquire()"
    )
    busy = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=5)
    assert busy.returncode != 0 and b"Resource busy" in busy.stderr
    owner.release()
    free = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=5)
    assert free.returncode == 0
    owner.acquire()  # the child exited without explicitly releasing
    owner.release()


async def test_diagnostics_distinguish_absent_ambiguous_failed(monkeypatch):
    manager = DeviceManager()

    async def devices():
        return [DeviceInfo("COM1", serial="a"), DeviceInfo("COM2", serial="b")]

    monkeypatch.setattr(manager, "list_devices", devices)
    assert (await manager.diagnose(DeviceMatch(serial="missing")))["status"] == "absent"
    assert (await manager.diagnose(DeviceMatch()))["status"] == "ambiguous"
    assert (await manager.find_device(DeviceMatch(serial="b"))).port == "COM2"
    with pytest.raises(ValueError, match="Ambiguous"):
        await manager.find_device(DeviceMatch())

    async def failed():
        raise PermissionError("USB inventory denied")

    monkeypatch.setattr(manager, "list_devices", failed)
    assert (await manager.diagnose(DeviceMatch()))["status"] == "enumeration_failed"


def sal(path):
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "meta.json",
            json.dumps(
                {
                    "data": {
                        "rowsSettings": [
                            {
                                "type": "channel",
                                "name": "Channel 0",
                                "channel": {"deviceChannel": 0, "type": kind},
                            }
                            for kind in ("Digital", "Analog")
                        ]
                    }
                }
            ),
        )
        archive.writestr("digital.bin", b"raw evidence")


async def test_native_labels_preserve_original_and_verify_resaved_copy(tmp_path):
    source = tmp_path / "capture.sal"
    sal(source)
    original = sha256(source)
    names = {"digital": {"0": {"name": "RESETn"}}, "analog": {"0": "RESETn"}}

    class Logic:
        async def load_capture(self, path):
            return path

        async def save_capture(self, capture, path):
            shutil.copyfile(capture, path)

        async def close_capture(self, capture):
            pass

    result = await label_and_verify(Logic(), source, names)
    assert result["native_reopen_verified"]
    assert sha256(source) == original
    verify_labels(tmp_path / result["verified_capture"], names)
    with pytest.raises(FileExistsError):
        make_labeled_copy(source, source, names)
    with pytest.raises(ValueError, match="share a name"):
        make_labeled_copy(
            source, tmp_path / "bad.sal", {"digital": {"0": "A"}, "analog": {"0": "B"}}
        )
    with pytest.raises(ValueError, match="missing"):
        verify_labels(source, {"digital": {"3": "missing"}})


async def test_lost_labels_after_logic_resave_are_not_success(tmp_path):
    source = tmp_path / "capture.sal"
    sal(source)

    class Logic:
        async def load_capture(self, path):
            return path

        async def save_capture(self, capture, path):
            sal(path)  # emulate an incompatible Logic version dropping the labels

        async def close_capture(self, capture):
            pass

    with pytest.raises(ValueError, match="mismatch"):
        await label_and_verify(Logic(), source, {"digital": {"0": "VOUT"}})


async def test_evidence_hashes_and_noise_floor(monkeypatch, tmp_path):
    base = {
        "channel": "CH1",
        "signal": "rail",
        "probe": "10x passive",
        "connection": "test point",
        "ground": "spring",
        "limitations": ["No calibrated external reference"],
    }
    async with replay(monkeypatch, tmp_path) as (scope, _):
        baseline = tmp_path / "noise"
        result = await capture_scope_evidence(
            scope, ScopeRecipe(kind="noise_floor", **base), baseline
        )
        assert result["measurements"]["ac_rms_v"] < result["measurements"]["total_rms_v"]
        for name, digest in result["files"].items():
            assert sha256(baseline / name) == digest
        recipe = ScopeRecipe(kind="ripple", noise_floor_manifest=baseline / "manifest.json", **base)
        ripple = await capture_scope_evidence(scope, recipe, tmp_path / "ripple")
        assert ripple["noise_floor"]["measurements"] == result["measurements"]
        (baseline / "waveform.bin").write_bytes(b"tampered")
        with pytest.raises(ValueError, match="hash mismatch"):
            await capture_scope_evidence(scope, recipe, tmp_path / "failed")
        assert json.loads((tmp_path / "failed/manifest.json").read_text())["status"] == "failed"


def test_load_step_windows_and_observation_limit():
    recipe = ScopeRecipe(
        kind="load_step",
        signal="rail",
        probe="10x",
        connection="TP",
        ground="spring",
        limitations=[],
        before_window=(-0.002, -0.001),
        after_window=(0.003, 0.004),
        settling_band_v=0.1,
    )
    wave = SimpleNamespace(
        samples=np.array([5.0, 5.0, 4.0, 4.8, 5.0, 5.0, 5.0]), time_array=np.arange(-2, 5) * 0.001
    )
    step = waveform_metrics(wave, recipe)["load_step"]
    assert step["undershoot_v"] == 1
    assert step["settled_by_s"] == pytest.approx(0.002)
    recipe.after_window = (0.1, 0.2)
    with pytest.raises(ValueError, match="outside"):
        waveform_metrics(wave, recipe)
