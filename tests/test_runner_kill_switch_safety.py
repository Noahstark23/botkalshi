"""El arranque no autoriza tareas financieras con pausa durable desconocida."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from functools import partial
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine

import src.storage.models as models
from src.monitoring.health import BotState
from src.runner import ProductionRunner


@pytest.fixture
def boot_context(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'boot.sqlite'}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(models, "get_session", lambda: Session(engine))
    previous = {
        name: getattr(BotState, name)
        for name in (
            "is_paused",
            "pause_reason",
            "last_error",
            "last_error_at",
            "db_initialized",
            "motor_telemetry",
        )
    }
    BotState.is_paused = False
    BotState.pause_reason = None
    settings = MagicMock()
    settings.KALSHI_ENV = "demo"
    settings.ACTIVE_CAPITAL_USD = 300.0
    settings.TRADING_ENABLED = True
    for name in (
        "MOTOR_1_ARBITRAGE_ENABLED",
        "MOTOR_2_SPORTSBOOK_ENABLED",
        "MOTOR_REST_ENABLED",
        "MOTOR_3_CLV_ENABLED",
        "MOTOR_MM_ENABLED",
        "EXPERIMENT_DYNAMIC_MULTI_ENABLED",
        "ANALYST_LOOP_ENABLED",
        "DAILY_PNL_REPORT_ENABLED",
    ):
        setattr(settings, name, False)
    # Salidas habilitadas: no entran en la selección de conciliación de arranque.
    settings.MOTOR_3_CLV_ENABLED = True
    settings.MOTOR_3_EXECUTION_ENABLED = True
    with patch("src.runner.get_settings", return_value=settings):
        yield engine, settings
    for name, value in previous.items():
        setattr(BotState, name, value)
    engine.dispose()


def _snapshot(engine):
    with Session(engine) as session:
        row = session.get(models.OperationalState, "kill_switch")
        return None if row is None else (row.value, row.reason, row.updated_at)


async def _run_isolated(runner):
    """Ejecuta el lifecycle real con tareas/IO ficticios, sin clientes financieros."""
    launched = []

    async def task(name):
        launched.append(name)

    def required(name, _service):
        return asyncio.create_task(task(name), name=name)

    with ExitStack() as stack:
        for name in (
            "_run_health_server",
            "_run_telegram_commands",
            "_run_balance_refresh",
            "_run_memory_monitor",
            "_run_db_maintenance",
            "_run_disk_guard",
        ):
            stack.enter_context(
                patch.object(runner, name, AsyncMock(side_effect=partial(task, name)))
            )
        stack.enter_context(patch.object(runner, "_start_required_service", side_effect=required))
        stack.enter_context(patch.object(runner, "_record_run_start"))
        stack.enter_context(patch.object(runner, "_record_run_end"))
        stack.enter_context(patch("src.runner.init_db"))
        balance = stack.enter_context(
            patch(
                "src.runner.RiskManager.refresh_capital_from_balance",
                new=AsyncMock(return_value=None),
            )
        )
        alert = stack.enter_context(patch("src.runner.alert_startup", new=AsyncMock()))
        stack.enter_context(patch("src.runner.send_alert", new=AsyncMock()))
        result = await runner.run()
    return result, launched, balance, alert


@pytest.mark.asyncio
@pytest.mark.parametrize("trading", [False, True])
@pytest.mark.parametrize("stored", ["clear", "engaged"])
async def test_unreadable_pause_aborts_before_services_and_preserves_record(
    boot_context, trading, stored
):
    engine, settings = boot_context
    settings.TRADING_ENABLED = trading
    with Session(engine) as session:
        session.add(models.OperationalState(key="kill_switch", value=stored, reason="original"))
        session.commit()
    before = _snapshot(engine)
    runner = ProductionRunner()
    # La siguiente lectura sana no debe esconder que el arranque desconocía la pausa.
    with patch(
        "src.runner.kill_switch_engaged",
        side_effect=[OSError("transient read"), (stored == "engaged", "original")],
    ) as read:
        result, launched, balance, alert = await _run_isolated(runner)
    assert result == 1
    assert launched == []
    assert BotState.is_paused
    assert BotState.pause_reason == "KILL_SWITCH_BOOT_UNVERIFIED"
    assert BotState.last_error == "KILL_SWITCH_BOOT_UNVERIFIED"
    balance.assert_not_awaited()
    alert.assert_not_awaited()
    assert read.call_count == 1
    assert _snapshot(engine) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [None, "clear", "engaged"])
async def test_known_pause_allows_observation_boot_without_clearing_it(boot_context, stored):
    engine, _ = boot_context
    if stored is not None:
        with Session(engine) as session:
            session.add(models.OperationalState(key="kill_switch", value=stored, reason="original"))
            session.commit()
    before = _snapshot(engine)
    result, launched, balance, alert = await _run_isolated(ProductionRunner())
    assert result == 0
    assert "capture" in launched and "motor3_clv" in launched
    assert BotState.is_paused is (stored == "engaged")
    balance.assert_awaited_once()
    alert.assert_awaited_once()
    assert _snapshot(engine) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ["", "ENGAGED", "unknown", "cleared", "true"])
async def test_invalid_durable_pause_never_means_clear(boot_context, stored):
    engine, _ = boot_context
    with Session(engine) as session:
        session.add(models.OperationalState(key="kill_switch", value=stored, reason="preserve"))
        session.commit()
    before = _snapshot(engine)
    with pytest.raises(ValueError, match="KILL_SWITCH_STATE_INVALID"):
        models.kill_switch_engaged()
    result, launched, balance, _ = await _run_isolated(ProductionRunner())
    assert result == 1 and launched == [] and BotState.is_paused
    balance.assert_not_awaited()
    assert _snapshot(engine) == before


@pytest.mark.asyncio
async def test_cancelled_pause_read_never_starts_services(boot_context):
    runner = ProductionRunner()
    with (
        patch("src.runner.kill_switch_engaged", side_effect=asyncio.CancelledError),
        pytest.raises(asyncio.CancelledError),
    ):
        await _run_isolated(runner)


@pytest.mark.asyncio
async def test_unreadable_pause_cannot_be_hidden_by_successful_reconcile(boot_context):
    from src.storage.order_reconciliation import ReconciliationResult

    _, settings = boot_context
    settings.MOTOR_2_SPORTSBOOK_ENABLED = True
    runner = ProductionRunner()
    with (
        patch("src.runner.kill_switch_engaged", side_effect=OSError("transient read")),
        patch("src.runner.KalshiRestClient", return_value=AsyncMock()),
        patch(
            "src.runner.reconcile_pending_orders",
            new=AsyncMock(return_value=ReconciliationResult()),
        ) as reconcile,
    ):
        result, launched, _, _ = await _run_isolated(runner)
    assert result == 1 and launched == [] and BotState.is_paused
    reconcile.assert_not_awaited()
