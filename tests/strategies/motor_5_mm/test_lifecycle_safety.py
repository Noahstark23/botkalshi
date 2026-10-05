"""Cierres excepcionales M5: red falsa y DB temporal, sin credenciales."""

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select

from src.storage.models import MMExperimentRun, get_session
from src.strategies.motor_5_mm.engine import Motor5DataIntegrityError, Motor5Engine
from src.strategies.motor_5_mm.incidents import incident_status
from src.strategies.motor_5_mm.reconciler import MMReconcileReport


class _Client:
    def __init__(self):
        self.open = False

    async def __aenter__(self):
        self.open = True
        return self

    async def __aexit__(self, *_):
        self.open = False


class _Executor:
    def __init__(self, client, *, stuck=False, failure=None):
        self.client = client
        self.stuck = stuck
        self.failure = failure
        self.corrupted = set()
        self.cancel_calls = []

    async def cancel_all(self, reason):
        assert self.client.open, "cleanup debe ocurrir antes de cerrar HTTP"
        self.cancel_calls.append(reason)
        if self.failure:
            raise self.failure
        return 0

    def live_coids(self):
        return {"m5mm-stuck"} if self.stuck else set()

    def forget_confirmed(self, _):
        pass


def _run_status():
    with get_session() as session:
        return session.exec(select(MMExperimentRun)).one().status


def _setup(*, failure=None, stuck=False):
    client = _Client()
    executor = _Executor(client, failure=failure, stuck=stuck)
    engine = Motor5Engine(trading_enabled=True, client_factory=lambda: client)
    reconciler = AsyncMock()
    reconciler.reconcile.return_value = MMReconcileReport(complete=True)
    return engine, client, executor, reconciler


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "original",
    [
        Motor5DataIntegrityError("fatal original"),
        RuntimeError("original"),
        asyncio.CancelledError(),
    ],
)
async def test_exceptional_exit_cancels_with_client_open_and_preserves_original(original):
    engine, client, executor, reconciler = _setup()
    engine._tick = AsyncMock(side_effect=original)
    with (
        patch("src.strategies.motor_5_mm.engine.Motor5Executor", return_value=executor),
        patch("src.strategies.motor_5_mm.engine.MMReconciler", return_value=reconciler),
        pytest.raises(type(original)) as caught,
    ):
        await engine.run(asyncio.Event())
    assert caught.value is original
    assert executor.cancel_calls == ["shutdown"]
    assert not client.open
    assert _run_status() != "clean"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure, stuck", [(RuntimeError("cancel failed"), False), (None, True)])
async def test_uncertain_normal_shutdown_never_marks_run_clean(failure, stuck):
    engine, _, executor, reconciler = _setup(failure=failure, stuck=stuck)
    stop = asyncio.Event()
    stop.set()
    with (
        patch("src.strategies.motor_5_mm.engine.Motor5Executor", return_value=executor),
        patch("src.strategies.motor_5_mm.engine.MMReconciler", return_value=reconciler),
        pytest.raises(Motor5DataIntegrityError),
    ):
        await engine.run(stop)
    assert _run_status() != "clean"
    assert incident_status()[0]


@pytest.mark.asyncio
async def test_cleanup_failure_preserves_original_fatal_exception():
    engine, _, executor, reconciler = _setup(failure=RuntimeError("secondary"))
    original = Motor5DataIntegrityError("original")
    engine._tick = AsyncMock(side_effect=original)
    with (
        patch("src.strategies.motor_5_mm.engine.Motor5Executor", return_value=executor),
        patch("src.strategies.motor_5_mm.engine.MMReconciler", return_value=reconciler),
        pytest.raises(Motor5DataIntegrityError) as caught,
    ):
        await engine.run(asyncio.Event())
    assert caught.value is original
    assert incident_status()[0]


@pytest.mark.asyncio
async def test_shutdown_timeout_is_bounded_and_remains_unclean():
    engine, client, executor, reconciler = _setup()
    engine.SHUTDOWN_CANCEL_TIMEOUT_SEC = 0.02
    cancelled = asyncio.Event()

    async def slow_cancel(_):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    executor.cancel_all = slow_cancel
    stop = asyncio.Event()
    stop.set()
    started = time.monotonic()
    with (
        patch("src.strategies.motor_5_mm.engine.Motor5Executor", return_value=executor),
        patch("src.strategies.motor_5_mm.engine.MMReconciler", return_value=reconciler),
        pytest.raises(Motor5DataIntegrityError),
    ):
        await engine.run(stop)
    await asyncio.sleep(0)
    assert time.monotonic() - started < 1
    assert cancelled.is_set()
    assert not client.open
    assert _run_status() != "clean"
    assert incident_status()[0]


@pytest.mark.asyncio
async def test_cancellation_during_cleanup_is_delayed_until_cleanup_then_propagated():
    engine, client, executor, reconciler = _setup()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def cancel(_):
        entered.set()
        await release.wait()
        assert client.open
        return 0

    executor.cancel_all = cancel
    stop = asyncio.Event()
    stop.set()
    with (
        patch("src.strategies.motor_5_mm.engine.Motor5Executor", return_value=executor),
        patch("src.strategies.motor_5_mm.engine.MMReconciler", return_value=reconciler),
    ):
        run = asyncio.create_task(engine.run(stop))
        await entered.wait()
        run.cancel("operator shutdown")
        await asyncio.sleep(0)
        assert not run.done()
        run.cancel("second shutdown request")
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await run
    assert not client.open
    assert _run_status() != "clean"
    assert incident_status()[0]


@pytest.mark.asyncio
async def test_clean_shutdown_does_not_create_incident():
    engine, _, executor, reconciler = _setup()
    stop = asyncio.Event()
    stop.set()
    with (
        patch("src.strategies.motor_5_mm.engine.Motor5Executor", return_value=executor),
        patch("src.strategies.motor_5_mm.engine.MMReconciler", return_value=reconciler),
    ):
        await engine.run(stop)
    assert _run_status() == "clean"
    assert incident_status() == (False, None)
