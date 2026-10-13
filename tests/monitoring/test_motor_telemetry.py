"""Estado real de componentes, sin inferir permiso o progreso del flag configurado."""

import asyncio
import contextlib
import gc
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from src.monitoring import motor_telemetry as mt
from src.monitoring.health import BotState
from src.runner import ProductionRunner
from src.utils.config import Settings

NOW = 1_791_844_000_000
SHA = "a" * 40


@pytest.fixture
def clock(monkeypatch):
    current = [NOW]
    monkeypatch.setattr(mt, "_now_ms", lambda: current[0])
    return current


@pytest.fixture
def settings(monkeypatch):
    settings = Settings.model_construct(
        SOURCE_COMMIT=SHA,
        KALSHI_ENV="demo",
        KALSHI_API_KEY_ID="fictitious-api-id",
        TRADING_ENABLED=False,
        MOTOR_1_ARBITRAGE_ENABLED=True,
        MOTOR_1_EXECUTION_ENABLED=False,
        MOTOR_2_SPORTSBOOK_ENABLED=True,
        MOTOR_2_ENTRY_EXECUTION_ENABLED=False,
        MOTOR_2_EXECUTION_ENABLED=False,
        MOTOR_3_CLV_ENABLED=True,
        MOTOR_3_EXECUTION_ENABLED=False,
        MOTOR_MM_ENABLED=True,
        MOTOR_MM_EXECUTION_ENABLED=False,
        MOTOR_6_LINEMOVE_ENABLED=True,
        MOTOR_REST_ENABLED=True,
        MOTOR_REST_EXECUTION_ENABLED=False,
        MOTOR_8_OFI_ENABLED=True,
        MOTOR_9_SPILLOVER_ENABLED=True,
        PERSIST_ORDERBOOK_EVENTS=False,
        USE_ORDERBOOK_MANAGER_V2=True,
    )
    monkeypatch.setattr("src.utils.config._settings", settings)
    return settings


@pytest.fixture
def owner(settings, clock):
    return mt.create_telemetry(settings)


class Component:
    def __init__(self):
        self.calls = 0

    @mt.observed_tick
    async def tick(self, outcome="return"):
        self.calls += 1
        if outcome == "exception":
            raise RuntimeError("fictitious sensitive exception")
        if outcome == "cancel":
            raise asyncio.CancelledError()
        return 17

    @mt.observed_tick
    def callback(self, outcome="return"):
        self.calls += 1
        if outcome == "exception":
            raise RuntimeError("fictitious sensitive exception")
        return []


def component_snapshot(owner, name):
    return mt.telemetry_snapshot(owner)["components"][name]


@pytest.mark.parametrize("source", ["", None, 7, True, "abc", "A" * 40, "a" * 41])
def test_bad_identity_does_not_invent_source(settings, source):
    settings.SOURCE_COMMIT = source
    assert mt.create_telemetry(settings) is None
    assert mt.telemetry_snapshot(None) is None


def test_configuration_does_not_assert_instantiation_or_progress(owner):
    snap = owner.snapshot()
    assert snap["motors"]["rest"]["configured"] is True
    assert snap["motors"]["rest"]["execution_entry_configured"] is False
    for name in mt.COMPONENTS:
        assert snap["components"][name]["instantiated"]["value"] is None
        assert snap["components"][name]["task_running"]["value"] is None
        assert snap["components"][name]["tick_returned"]["value"] is None
    for name in ("m6", "m8", "m9"):
        assert snap["motors"][name]["execution_implementation"] == "ABSENT"
    assert "fictitious-api-id" not in json.dumps(snap)
    assert len(json.dumps(snap)) < 65_536


def test_invalid_flags_stay_unknown(settings, clock):
    settings.MOTOR_8_OFI_ENABLED = 1
    owner = mt.create_telemetry(settings)
    assert owner.snapshot()["motors"]["m8"]["configured"] is None


async def test_probe_tracks_exact_task_and_does_not_refresh_progress(owner, clock):
    c = Component()
    ready = asyncio.Event()
    release = asyncio.Event()

    async def worker():
        mt.bind_component(owner, "m1_loop", c)
        await c.tick()
        ready.set()
        await release.wait()

    task = asyncio.create_task(worker())
    await ready.wait()
    first = component_snapshot(owner, "m1_loop")
    assert first["task_running"]["value"] is True
    assert first["tick_returned"]["value"] is True
    clock[0] += mt.MAX_AGE_MS + 1
    later = component_snapshot(owner, "m1_loop")
    assert later["task_running"]["value"] is True
    assert later["tick_returned"]["value"] is None
    assert later["tick_returned"]["freshness"] == "STALE"
    assert later["tick_returned"]["observed_at_ms"] == NOW
    assert c.calls == 1
    release.set()
    await task
    assert component_snapshot(owner, "m1_loop")["task_running"]["value"] is False


@pytest.mark.parametrize("outcome", ["return", "exception", "cancel"])
async def test_tick_preserves_value_exception_and_cancellation(owner, outcome):
    c = Component()
    mt.bind_component(owner, "m2_poll", c)
    if outcome == "return":
        assert await c.tick() == 17
    else:
        error = RuntimeError if outcome == "exception" else asyncio.CancelledError
        with pytest.raises(error):
            await c.tick(outcome)
    snap = component_snapshot(owner, "m2_poll")
    assert snap["tick_returned"]["value"] is (outcome == "return")
    assert c.calls == 1
    assert "sensitive" not in json.dumps(owner.snapshot())


@pytest.mark.parametrize("outcome", ["return", "exception"])
def test_callback_has_no_task_even_after_call(owner, outcome):
    c = Component()
    mt.bind_component(owner, "m8_observer", c)
    if outcome == "return":
        assert c.callback() == []
    else:
        with pytest.raises(RuntimeError):
            c.callback(outcome)
    snap = component_snapshot(owner, "m8_observer")
    assert snap["task_running"] == {
        "value": None,
        "observed_at_ms": None,
        "freshness": "NOT_APPLICABLE",
    }
    assert snap["tick_returned"]["value"] is (outcome == "return")


async def test_retired_object_cannot_refresh_replacement(owner, clock):
    old, new = Component(), Component()
    mt.bind_component(owner, "m1_loop", old)
    await old.tick()
    clock[0] += 10
    mt.bind_component(owner, "m1_loop", new)
    clock[0] += 10
    await old.tick()
    snap = component_snapshot(owner, "m1_loop")
    assert snap["generation"] == 2
    assert snap["tick_returned"]["value"] is None
    await new.tick()
    assert component_snapshot(owner, "m1_loop")["tick_returned"]["value"] is True


def test_bindings_are_bounded_and_do_not_keep_retired_objects(owner):
    for _ in range(300):
        c = Component()
        mt.bind_component(owner, "m8_observer", c)
    assert len(owner.bindings) == 1
    del c
    gc.collect()
    assert component_snapshot(owner, "m8_observer")["instantiated"]["value"] is False
    mt.bind_component(owner, "arbitrary", Component())
    assert len(owner.bindings) == 1


async def test_telemetry_failure_never_swallows_or_repeats_motor_call(owner):
    c = Component()
    mt.bind_component(owner, "m1_loop", c)
    with patch.object(owner.registry, "observe", side_effect=RuntimeError("secret")):
        assert await c.tick() == 17
        with pytest.raises(RuntimeError, match="fictitious sensitive"):
            await c.tick("exception")
    assert c.calls == 2
    with patch.object(owner, "snapshot", side_effect=RuntimeError("secret")):
        assert mt.telemetry_snapshot(owner) is None


async def test_clock_regression_does_not_refresh_ticks(owner, clock):
    c = Component()
    mt.bind_component(owner, "m1_loop", c)
    await c.tick()
    clock[0] -= 1
    assert await c.tick() == 17
    assert mt.telemetry_snapshot(owner) is None
    clock[0] = NOW + mt.MAX_AGE_MS + 1
    assert component_snapshot(owner, "m1_loop")["tick_returned"]["value"] is None


def test_restart_uses_new_identity_and_no_old_observations(settings, owner):
    c = Component()
    mt.bind_component(owner, "m8_observer", c)
    c.callback()
    replacement = mt.create_telemetry(settings)
    assert (
        owner.snapshot()["source"]["instance_id"] != replacement.snapshot()["source"]["instance_id"]
    )
    assert component_snapshot(replacement, "m8_observer")["generation"] == 0
    assert component_snapshot(replacement, "m8_observer")["tick_returned"]["value"] is None


@pytest.mark.parametrize(
    ("method", "class_path", "name"),
    [
        ("_run_motor1_arb", "src.strategies.motor_1_arbitrage.engine.Motor1Engine", "m1_loop"),
        (
            "_run_motor2_shadow",
            "src.strategies.motor_2_consensus.poller.Motor2ShadowPoller",
            "m2_poll",
        ),
        (
            "_run_motor2_exits",
            "src.strategies.motor_2_consensus.exit_engine.Motor2ExitEngine",
            "m2_exit",
        ),
        ("_run_motor3_clv", "src.strategies.motor_3_clv.engine.Motor3Engine", "m3_loop"),
        ("_run_motor5_mm", "src.strategies.motor_5_mm.engine.Motor5Engine", "m5_loop"),
        ("_run_data_capture", "src.runner.DataCaptureService", "capture"),
    ],
)
async def test_runner_binds_constructed_engine_to_actual_worker(settings, method, class_path, name):
    with patch("src.runner.get_settings", return_value=settings):
        runner = ProductionRunner()
    runner._capture = MagicMock()
    c = Component()
    ready, release = asyncio.Event(), asyncio.Event()

    async def run(*_):
        ready.set()
        await release.wait()

    c.run = run
    previous_error = BotState.last_error, BotState.last_error_at
    try:
        with patch("src.runner.asyncio.sleep", new=AsyncMock()), patch(class_path, return_value=c):
            task = asyncio.create_task(getattr(runner, method)())
            await asyncio.wait_for(ready.wait(), timeout=1)
            observed = component_snapshot(runner._motor_telemetry, name)
            assert observed["instantiated"]["value"] is True
            assert observed["task_running"]["value"] is True
            assert observed["tick_returned"]["value"] is None
            release.set()
            await task
        assert component_snapshot(runner._motor_telemetry, name)["task_running"]["value"] is False
    finally:
        release.set()
        if "task" in locals():
            if not task.done():
                task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task
        BotState.last_error, BotState.last_error_at = previous_error


async def test_constructor_failure_never_announces_instance(settings):
    with patch("src.runner.get_settings", return_value=settings):
        runner = ProductionRunner()
    previous = BotState.last_error, BotState.last_error_at
    try:
        with (
            patch("src.runner.asyncio.sleep", new=AsyncMock()),
            patch(
                "src.strategies.motor_3_clv.engine.Motor3Engine", side_effect=RuntimeError("failed")
            ),
            pytest.raises(RuntimeError),
        ):
            await runner._run_motor3_clv()
        assert (
            component_snapshot(runner._motor_telemetry, "m3_loop")["instantiated"]["value"] is None
        )
    finally:
        BotState.last_error, BotState.last_error_at = previous


async def test_real_capture_callback_wiring_reports_returns_without_signals(settings, owner):
    from src.strategies.data_capture import DataCaptureService

    previous = (
        BotState.v2_manager,
        BotState.last_error,
        BotState.last_error_at,
        BotState.last_ws_message,
    )
    with (
        patch("src.strategies.data_capture.get_settings", return_value=settings),
        patch("src.strategies.motor_rest_arb.engine.get_settings", return_value=settings),
        patch("src.strategies.data_capture.KalshiWebSocket") as ws,
        patch("src.storage.models.kill_switch_engaged", return_value=(False, None)),
    ):
        try:
            capture = DataCaptureService()
            mt.bind_component(owner, "capture", capture)
            capture._register_ws_handlers()
            for name in ("rest_observer", "m8_observer", "m9_observer"):
                snap = component_snapshot(owner, name)
                assert snap["instantiated"]["value"] is True
                assert snap["task_running"]["freshness"] == "NOT_APPLICABLE"
                assert snap["tick_returned"]["value"] is None
            callbacks = [
                call.args[1]
                for call in ws.return_value.on.call_args_list
                if call.args[0] == "ticker"
            ]
            await callbacks[-1]({"msg": {}})
            await capture._on_orderbook_delta(
                {"msg": {"market_ticker": "FAKE", "side": "yes", "price": 40, "delta": 1}}
            )
            for name in ("rest_observer", "m8_observer", "m9_observer"):
                assert component_snapshot(owner, name)["tick_returned"]["value"] is True
            returned = component_snapshot(owner, "m8_observer")["tick_returned"]
            await capture._on_orderbook_delta({"msg": {}})
            assert component_snapshot(owner, "m8_observer")["tick_returned"] == returned
            assert capture._rest_engine._executor is None
        finally:
            (
                BotState.v2_manager,
                BotState.last_error,
                BotState.last_error_at,
                BotState.last_ws_message,
            ) = previous


async def test_real_m6_callback_bound_by_runner(settings):
    with patch("src.runner.get_settings", return_value=settings):
        runner = ProductionRunner()
    observer = runner._build_motor6_shadow()
    assert observer.observe([], {}, is_live=False) == []
    snap = component_snapshot(runner._motor_telemetry, "m6_observer")
    assert snap["instantiated"]["value"] is True
    assert snap["tick_returned"]["value"] is True
    assert snap["task_running"]["freshness"] == "NOT_APPLICABLE"


async def test_status_includes_detached_telemetry_without_calls(settings, owner, monkeypatch):
    from src.monitoring.health import status

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    c = Component()
    mt.bind_component(owner, "m8_observer", c)
    monkeypatch.setattr(BotState, "motor_telemetry", owner)
    monkeypatch.setattr(BotState, "v2_manager", None)
    with (
        patch("src.monitoring.health.get_settings", return_value=settings),
        patch("src.monitoring.health.get_session", side_effect=lambda: Session(engine)),
    ):
        response = await status()
        assert (
            response["motor_telemetry"]["components"]["m8_observer"]["instantiated"]["value"]
            is True
        )
        response["motor_telemetry"]["motors"]["m8"]["configured"] = False
        second = await status()
        assert second["motor_telemetry"]["motors"]["m8"]["configured"] is True
        assert c.calls == 0
        with patch.object(owner, "snapshot", side_effect=RuntimeError("fictitious secret")):
            failed = await status()
            assert failed["motor_telemetry"] is None
            assert failed["config"]["trading_enabled"] is False
    engine.dispose()


async def test_actual_cancellation_updates_only_owned_task(owner):
    started = asyncio.Event()

    class Waiting:
        @mt.observed_tick
        async def tick(self):
            started.set()
            await asyncio.Event().wait()

    c = Waiting()

    async def worker():
        mt.bind_component(owner, "m1_loop", c)
        await c.tick()

    task = asyncio.create_task(worker())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    observed = component_snapshot(owner, "m1_loop")
    assert observed["task_running"]["value"] is False
    assert observed["tick_returned"]["value"] is False
    assert component_snapshot(owner, "m2_poll")["task_running"]["value"] is None


@pytest.mark.parametrize(
    ("method", "flag", "name"),
    [
        ("_run_motor1_arb", "MOTOR_1_ARBITRAGE_ENABLED", "m1_loop"),
        ("_run_motor2_shadow", "MOTOR_2_SPORTSBOOK_ENABLED", "m2_poll"),
        ("_run_motor2_exits", "MOTOR_2_SPORTSBOOK_ENABLED", "m2_exit"),
        ("_run_motor3_clv", "MOTOR_3_CLV_ENABLED", "m3_loop"),
        ("_run_motor5_mm", "MOTOR_MM_ENABLED", "m5_loop"),
    ],
)
async def test_disabled_service_never_reports_constructed(settings, method, flag, name):
    setattr(settings, flag, False)
    with patch("src.runner.get_settings", return_value=settings):
        runner = ProductionRunner()
    await getattr(runner, method)()
    observed = component_snapshot(runner._motor_telemetry, name)
    assert observed["generation"] == 0
    assert observed["instantiated"]["value"] is None
    assert observed["tick_returned"]["value"] is None
    assert flag not in runner._motor_telemetry.bindings


async def test_real_m1_and_m2_noop_ticks_are_returns_not_fills(settings, owner):
    from src.strategies.motor_1_arbitrage.engine import Motor1Engine
    from src.strategies.motor_2_consensus.poller import Motor2ShadowPoller

    manager = MagicMock()
    manager.tracked_tickers = []
    m1 = Motor1Engine(manager)
    mt.bind_component(owner, "m1_loop", m1)
    await m1._tick()
    kalshi = AsyncMock()
    kalshi.fetch.return_value = []
    odds = AsyncMock()
    m2 = Motor2ShadowPoller(kalshi, odds)
    mt.bind_component(owner, "m2_poll", m2)
    assert await m2.poll_once() == []
    odds.fetch.assert_not_awaited()
    for name in ("m1_loop", "m2_poll"):
        assert component_snapshot(owner, name)["tick_returned"]["value"] is True
    assert m1._executor is None and m2._executor is None
