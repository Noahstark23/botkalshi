"""Una tarea requerida caída debe terminar el proceso con error y limpiar sus hermanas."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.monitoring.health import BotState
from src.runner import ProductionRunner
from src.utils.config import Settings


@pytest.fixture
def runner():
    settings = Settings.model_construct(
        TRADING_ENABLED=False,
        MOTOR_1_ARBITRAGE_ENABLED=True,
        MOTOR_2_SPORTSBOOK_ENABLED=True,
        MOTOR_3_CLV_ENABLED=True,
        MOTOR_MM_ENABLED=False,
        ANALYST_LOOP_ENABLED=False,
        DAILY_PNL_REPORT_ENABLED=False,
    )
    with patch("src.runner.get_settings", return_value=settings):
        instance = ProductionRunner()
    instance._capture = MagicMock()
    previous = (
        BotState.last_error,
        BotState.last_error_at,
        BotState.db_initialized,
        BotState.is_paused,
        BotState.pause_reason,
    )
    yield instance
    (
        BotState.last_error,
        BotState.last_error_at,
        BotState.db_initialized,
        BotState.is_paused,
        BotState.pause_reason,
    ) = previous


@pytest.mark.parametrize(
    ("method", "engine_path"),
    [
        ("_run_settlement", "src.runner.SettlementPoller"),
        ("_run_motor3_clv", "src.strategies.motor_3_clv.engine.Motor3Engine"),
        ("_run_motor1_arb", "src.strategies.motor_1_arbitrage.engine.Motor1Engine"),
        ("_run_motor2_exits", "src.strategies.motor_2_consensus.exit_engine.Motor2ExitEngine"),
    ],
)
async def test_terminal_errors_reach_supervisor(runner, method, engine_path):
    engine = MagicMock()
    engine.run = AsyncMock(side_effect=RuntimeError("fake terminal failure"))
    with (
        patch("src.runner.asyncio.sleep", new=AsyncMock()),
        patch("src.runner.KalshiRestClient", return_value=AsyncMock()),
        patch(engine_path, return_value=engine),
        pytest.raises(RuntimeError, match="fake terminal failure"),
    ):
        await getattr(runner, method)()
    assert "fake terminal failure" in BotState.last_error


@pytest.mark.parametrize("outcome", ["error", "unexpected_return", "shutdown"])
@pytest.mark.parametrize("motor1_enabled", [False, True])
@pytest.mark.parametrize("motor2_enabled", [False, True])
async def test_run_detects_required_task_end_and_cleans_siblings(
    runner, outcome, motor1_enabled, motor2_enabled
):
    """Ejercita el supervisor real; todos los servicios externos son dobles offline."""
    runner.settings.MOTOR_1_ARBITRAGE_ENABLED = motor1_enabled
    runner.settings.MOTOR_2_SPORTSBOOK_ENABLED = motor2_enabled
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def capture():
        started.set()
        try:
            await runner._stop_event.wait()
        finally:
            cleaned.set()

    async def motor3():
        await started.wait()
        if outcome == "error":
            raise RuntimeError("fake terminal failure")
        if outcome == "shutdown":
            runner._stop_event.set()

    async def idle():
        await runner._stop_event.wait()

    with (
        patch("src.runner.init_db"),
        patch("src.runner.alert_startup", new=AsyncMock()),
        patch(
            "src.runner.RiskManager.refresh_capital_from_balance", new=AsyncMock(return_value=None)
        ),
        patch.object(runner, "_rehydrate_kill_switch", new=AsyncMock()),
        patch.object(runner, "_reconcile_on_boot", new=AsyncMock()),
        patch.object(runner, "_record_run_start"),
        patch.object(runner, "_record_run_end") as record_end,
        patch.object(runner, "_run_health_server", side_effect=idle),
        patch.object(runner, "_run_data_capture", side_effect=capture),
        patch.object(runner, "_run_settlement", side_effect=idle),
        patch.object(runner, "_run_motor1_arb", side_effect=idle) as motor1,
        patch.object(runner, "_run_motor2_shadow", side_effect=idle) as motor2_fair,
        patch.object(runner, "_run_motor2_exits", side_effect=idle) as motor2_exits,
        patch.object(runner, "_run_telegram_commands", new=AsyncMock()),
        patch.object(runner, "_run_balance_refresh", side_effect=idle),
        patch.object(runner, "_run_memory_monitor", new=AsyncMock()),
        patch.object(runner, "_run_db_maintenance", side_effect=idle),
        patch.object(runner, "_run_disk_guard", side_effect=idle),
        patch.object(runner, "_run_motor3_clv", side_effect=motor3),
    ):
        try:
            exit_code = await asyncio.wait_for(runner.run(), timeout=1)
        finally:
            # También libera tareas si se ejecuta contra la versión defectuosa.
            runner._stop_event.set()
        assert exit_code == (0 if outcome == "shutdown" else 1)
        assert cleaned.is_set()
        assert motor1.await_count == int(motor1_enabled)
        assert motor2_fair.await_count == int(motor2_enabled)
        assert motor2_exits.await_count == int(motor2_enabled)
        if outcome != "shutdown":
            assert "motor3_clv" in record_end.call_args.kwargs["crash_reason"]
            assert BotState.is_paused


async def test_shutdown_during_startup_does_not_create_capture(runner):
    runner._capture = None
    delayed = asyncio.Event()
    release = asyncio.Event()

    async def delay(_seconds):
        delayed.set()
        await release.wait()

    with (
        patch("src.runner.asyncio.sleep", side_effect=delay),
        patch("src.runner.DataCaptureService") as capture_class,
        patch("src.runner.alert_shutdown", new=AsyncMock()),
        patch.object(runner, "_record_run_end"),
    ):
        task = asyncio.create_task(runner._run_data_capture())
        await asyncio.wait_for(delayed.wait(), timeout=1)
        await runner.shutdown(reason="test")
        release.set()
        await asyncio.wait_for(task, timeout=1)
    capture_class.assert_not_called()
