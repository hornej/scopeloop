"""Ownership and handle cleanup when a blocking Logic RPC outlives its caller."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from scopeloop.instruments import saleae


class BlockedRpc:
    def __init__(self, result=None):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.result = result

    def __call__(self, *args, **kwargs):
        self.entered.set()
        assert self.release.wait(3), "test must release the blocking RPC"
        self.finished.set()
        return self.result


class Capture:
    def __init__(self):
        self.stopped = False
        self.closed = False

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


async def entered(rpc):
    assert await asyncio.to_thread(rpc.entered.wait, 2)


@pytest.fixture
def analyzer(monkeypatch, tmp_path):
    monkeypatch.setenv("SCOPELOOP_LOCK_DIR", str(tmp_path / "locks"))
    driver = saleae.SaleaeLogicAnalyzer()
    driver._lease.acquire()
    driver._connected = True
    driver._device = SimpleNamespace(device_id="FAKE")
    yield driver
    driver._lease.release()


async def cancel_while_blocked(driver, operation, rpc):
    task = asyncio.create_task(operation)
    await entered(rpc)
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()  # A second cancellation must not escape worker cleanup.
    await asyncio.sleep(0.01)
    assert not task.done()
    assert driver._lock._owner is task
    assert not rpc.finished.is_set()
    assert driver._lease._file is not None
    rpc.release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert rpc.finished.is_set()
    assert driver._lock._owner is None


@pytest.mark.parametrize("triggered", [False, True])
async def test_cancelled_start_retains_ownership_and_disposes_capture(analyzer, triggered):
    capture = Capture()
    start = BlockedRpc(capture)
    analyzer._manager = SimpleNamespace(start_capture=start)
    operation = (
        analyzer.capture_with_trigger([0], trigger_channel=0)
        if triggered
        else analyzer.capture(1.0, [0])
    )
    await cancel_while_blocked(analyzer, operation, start)
    assert capture.stopped and capture.closed


async def test_cancelled_connect_closes_manager_before_releasing_lease(monkeypatch, tmp_path):
    monkeypatch.setenv("SCOPELOOP_LOCK_DIR", str(tmp_path))
    closed = threading.Event()
    manager = SimpleNamespace(close=closed.set)
    connect = BlockedRpc(manager)
    monkeypatch.setattr(saleae.Manager, "connect", connect)
    driver = saleae.SaleaeLogicAnalyzer()
    await cancel_while_blocked(driver, driver.connect(), connect)
    assert closed.is_set()
    assert driver._manager is None and not driver.is_connected
    assert driver._lease._file is None


async def test_cancelled_device_enumeration_settles_before_manager_close(monkeypatch, tmp_path):
    monkeypatch.setenv("SCOPELOOP_LOCK_DIR", str(tmp_path))
    closed = threading.Event()
    devices = BlockedRpc([])
    manager = SimpleNamespace(close=closed.set, get_devices=devices)
    monkeypatch.setattr(saleae.Manager, "connect", lambda **kwargs: manager)
    driver = saleae.SaleaeLogicAnalyzer()
    await cancel_while_blocked(driver, driver.connect(), devices)
    assert closed.is_set() and driver._manager is None
    assert driver._lease._file is None


async def test_repeated_cancellation_cannot_escape_orphan_capture_cleanup(analyzer):
    closed = threading.Event()
    stop = BlockedRpc()
    capture = SimpleNamespace(stop=stop, close=closed.set)
    start = BlockedRpc(capture)
    analyzer._manager = SimpleNamespace(start_capture=start)
    task = asyncio.create_task(analyzer.capture(1, [0]))
    await entered(start)
    task.cancel()
    start.release.set()
    await entered(stop)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done() and analyzer._lock._owner is task
    assert analyzer._lease._file is not None
    stop.release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert closed.is_set()


async def test_cancelled_load_closes_only_loaded_handle(analyzer, tmp_path):
    capture = Capture()
    load = BlockedRpc(capture)
    analyzer._manager = SimpleNamespace(load_capture=load)
    await cancel_while_blocked(analyzer, analyzer.load_capture(tmp_path / "copy.sal"), load)
    assert capture.closed
    assert not capture.stopped


@pytest.mark.parametrize("operation", ["save", "export", "close"])
async def test_cancelled_capture_mutation_keeps_ownership_until_finished(
    analyzer, tmp_path, operation
):
    rpc = BlockedRpc()
    capture = SimpleNamespace(
        _capture=SimpleNamespace(save_capture=rpc, export_raw_data_csv=rpc, close=rpc),
        digital_channels=[0],
        analog_channels=[],
    )
    actions = {
        "save": lambda: analyzer.save_capture(capture, tmp_path / "copy.sal"),
        "export": lambda: analyzer.export_raw_csv(capture, tmp_path / "raw"),
        "close": lambda: analyzer.close_capture(capture),
    }
    await cancel_while_blocked(analyzer, actions[operation](), rpc)


async def test_cancelled_disconnect_clears_state_after_close_settles(analyzer):
    close = BlockedRpc()
    analyzer._manager = SimpleNamespace(close=close)
    await cancel_while_blocked(analyzer, analyzer.disconnect(), close)
    assert not analyzer.is_connected and analyzer._manager is None
    assert analyzer._lease._file is None


async def test_failed_capture_wait_disposes_owned_capture(analyzer, monkeypatch):
    capture = Capture()
    analyzer._manager = SimpleNamespace(start_capture=lambda **kwargs: capture)

    async def timeout(*args, **kwargs):
        raise saleae.CaptureTimeoutError("no trigger")

    monkeypatch.setattr(analyzer, "_wait_for_capture", timeout)
    with pytest.raises(saleae.CaptureTimeoutError):
        await analyzer.capture_with_trigger([0], trigger_channel=0)
    assert capture.stopped and capture.closed


async def test_repeated_cancellation_waits_for_stop_and_wait_workers(analyzer):
    stop = BlockedRpc()
    wait = BlockedRpc()
    capture = SimpleNamespace(
        capture_id=1,
        manager=SimpleNamespace(stub=SimpleNamespace(WaitCapture=wait)),
        stop=stop,
    )
    task = asyncio.create_task(analyzer._wait_for_capture(capture, 2, None, "waiting"))
    await entered(wait)
    task.cancel()
    await entered(stop)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    stop.release.set()
    await asyncio.sleep(0.01)
    assert not task.done()
    wait.release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert stop.finished.is_set() and wait.finished.is_set()


@pytest.mark.parametrize("triggered", [False, True])
async def test_progress_failure_drains_wait_before_closing_capture(analyzer, triggered):
    wait, stop = BlockedRpc(), BlockedRpc()
    closed = threading.Event()
    capture = SimpleNamespace(
        capture_id=1,
        manager=SimpleNamespace(stub=SimpleNamespace(WaitCapture=wait)),
        stop=stop,
        close=closed.set,
    )
    analyzer._manager = SimpleNamespace(start_capture=lambda **kwargs: capture)

    def failed_progress(update):
        if "elapsed_seconds" in update:
            raise OSError("progress sink unavailable")

    task = asyncio.create_task(
        analyzer.capture_with_trigger([0], trigger_channel=0, progress_callback=failed_progress)
        if triggered
        else analyzer.capture(1, [0], progress_callback=failed_progress)
    )
    await entered(wait)
    await entered(stop)
    assert not task.done() and analyzer._lock._owner is task
    assert not closed.is_set()
    stop.release.set()
    await asyncio.sleep(0.01)
    assert not task.done() and not closed.is_set()
    assert analyzer._lock._owner is task and analyzer._lease._file is not None
    wait.release.set()
    with pytest.raises(OSError, match="progress sink unavailable"):
        await asyncio.wait_for(task, 2)
    assert wait.finished.is_set() and closed.is_set()
    assert analyzer._lock._owner is None


@pytest.mark.parametrize("triggered", [False, True])
async def test_completion_callback_failure_disposes_capture(analyzer, monkeypatch, triggered):
    capture = Capture()
    analyzer._manager = SimpleNamespace(start_capture=lambda **kwargs: capture)

    async def completed(*args, **kwargs):
        pass

    def failed_progress(update):
        if update["state"] == "complete":
            raise OSError("progress sink unavailable")

    monkeypatch.setattr(analyzer, "_wait_for_capture", completed)
    with pytest.raises(OSError, match="progress sink unavailable"):
        if triggered:
            await analyzer.capture_with_trigger(
                [0], trigger_channel=0, progress_callback=failed_progress
            )
        else:
            await analyzer.capture(1, [0], progress_callback=failed_progress)
    assert capture.stopped and capture.closed
