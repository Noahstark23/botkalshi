"""Perfil50 offline: DB temporal, sin claves ni clientes HTTP ni órdenes."""

import asyncio
import multiprocessing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from freezegun import freeze_time
from sqlmodel import create_engine, select

import src.storage.models as models
from src.math.arbitrage import detect_binary_arb
from src.monitoring.health import BotState
from src.risk.commitments import lifetime_committed_cents, reserve_m1_intents
from src.risk.experiment_policy import limits_for_bank50, limits_for_capital
from src.risk.manager import RiskManager
from src.storage.quantities import quantity_fields, resize_filled
from src.utils.config import Settings

NOW = datetime(2026, 10, 7, 16, tzinfo=UTC)
START = NOW - timedelta(hours=1)


def intent(coid="a", *, price=40, count=1, ticker="EVENT-A"):
    return models.Trade(
        client_order_id=coid,
        ticker=ticker,
        side="yes",
        action="buy",
        strategy="motor_1_arbitrage",
        price_cents=price,
        status="pending",
        placed_at=NOW,
        **quantity_fields(count),
    )


def bounds(**overrides):
    return {
        "max_open_cents": 300,
        "event_cap_cents": 100,
        "max_daily_cents": 300,
        "experiment_start": START,
        "max_lifetime_cents": 5000,
        **overrides,
    }


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/bank50.db", connect_args={"timeout": 5})
    models.SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(models, "_engine", engine)
    monkeypatch.setattr(BotState, "is_paused", False)
    monkeypatch.setattr(BotState, "pause_reason", None)
    monkeypatch.setattr(RiskManager, "_check_lock", asyncio.Lock())
    monkeypatch.setattr(RiskManager, "_cached_capital_usd", 50.0)
    monkeypatch.setattr(RiskManager, "_last_raw_balance_usd", 50.0)
    monkeypatch.setattr(RiskManager, "_last_balance_at", NOW.replace(tzinfo=None))
    settings = Settings.model_construct(
        KALSHI_ENV="demo",
        TRADING_ENABLED=False,
        EXPERIMENT_BANK50_ENABLED=True,
        EXPERIMENT_BANK200_ENABLED=False,
        EXPERIMENT_START_AT=START.isoformat(),
        EXPERIMENT_BALANCE_MAX_AGE_SEC=60,
        BALANCE_MAX_AGE_SECONDS=60,
        ACTIVE_CAPITAL_USD=50,
        CAPITAL_CAP_USD=50,
        CAPITAL_FLOOR_USD=40.5,
        CAPITAL_SAFETY_FACTOR_PCT=90,
        CAPITAL_SMOOTHING_PCT=0,
        DYNAMIC_CAPITAL_ENABLED=True,
        MAX_TRADE_SIZE_USD=1,
        MAX_TRADE_SIZE_PCT=3,
        MAX_SIMULTANEOUS_EXPOSURE_PCT=10,
        MAX_EVENT_DIRECTIONAL_EXPOSURE_USD=1,
        MAX_DAILY_LOSS_FLOOR_USD=0,
        MAX_WEEKLY_LOSS_FLOOR_USD=0,
        MAX_MONTHLY_LOSS_FLOOR_USD=0,
        DAILY_STOP_ENTRIES_ONLY=False,
    )
    monkeypatch.setattr("src.risk.manager.get_settings", lambda: settings)
    monkeypatch.setattr("src.risk.manager.alert_risk_event", AsyncMock())
    denied = Mock(side_effect=AssertionError("offline: no network"))
    for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection"):
        monkeypatch.setattr(target, denied)
    monkeypatch.setattr("src.clients.kalshi_rest.KalshiRestClient.__init__", denied)
    with freeze_time(NOW, real_asyncio=True):
        yield settings, engine, tmp_path / "bank50.db"
    engine.dispose()
    denied.assert_not_called()


def lifetime(now=NOW):
    with models.get_session() as session:
        return lifetime_committed_cents(session, now=now)


def seed_history():
    # 50*99c + ceil(0.07*50*.99*.01*100)=4c de fee calculada.
    assert reserve_m1_intents([intent("old", price=99, count=50)], now=NOW - timedelta(days=2))
    with models.get_session() as session:
        row = session.exec(select(models.Trade)).one()
        row.status = "cancelled"
        session.add(row)
        session.commit()
    assert lifetime() == 4954


def test_policy50_allows_integer_pair_and_never_expands_with_deposits():
    for capital in (45, 50, 5000):
        policy = limits_for_bank50(capital)
        assert policy.max_per_operation_usd == Decimal("1.00")
        assert policy.max_open_usd == policy.max_daily_new_risk_usd == Decimal("3.00")
        assert policy.capital_usd <= 50
    assert limits_for_bank50("1.99").max_per_operation_usd == Decimal("0.05")
    # Bank200 mantiene exactamente su regla porcentual previa.
    assert limits_for_capital(45).max_per_operation_usd == Decimal("0.45")
    assert limits_for_capital(180).max_daily_new_risk_usd == Decimal("5.40")


def test_review_template_never_arms_orders_or_acknowledges_funding():
    path = Path(__file__).parents[2] / "infra/pilot-bank50/review.env.example"
    values = dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if line and not line.startswith("#")
    )
    assert values["TRADING_ENABLED"] == "false"
    execution_keys = {key for key in values if key.endswith("EXECUTION_ENABLED")}
    assert len(execution_keys) == 6
    assert all(values[key] == "false" for key in execution_keys)
    assert values["EXPERIMENT_BANK_CONFIRMED_USD"] == "0"
    assert values["EXPERIMENT_START_AT"] == ""


async def test_50_cash_sizing_accepts_one_synthetic_pair_instead_of_zero(sandbox):
    opportunity = detect_binary_arb("EVENT-A", 45, 10, 45, 10, max_count=5)
    assert opportunity is not None
    risk = RiskManager()
    assert risk.effective_capital_usd() == 45
    decision = await risk.check_pre_trade(opportunity)
    assert decision.approved and decision.max_allowed_count == 1
    assert risk.m1_reservation_limits() == bounds(experiment_start=START.replace(tzinfo=None))


@pytest.mark.parametrize("raw,cached,age", [(50, 50, 61), (44, 50, 0), (None, 50, 0)])
def test_profile50_rejects_stale_missing_or_below_floor_cash(
    sandbox, monkeypatch, raw, cached, age
):
    monkeypatch.setattr(RiskManager, "_last_raw_balance_usd", raw)
    monkeypatch.setattr(RiskManager, "_cached_capital_usd", cached)
    monkeypatch.setattr(
        RiskManager, "_last_balance_at", (NOW - timedelta(seconds=age)).replace(tzinfo=None)
    )
    assert not RiskManager().can_open_new_positions()


@pytest.mark.parametrize("price,accepted", [(44, True), (45, False)])
def test_historical_budget_accepts_exact_5000_rejects_5001_atomically(sandbox, price, accepted):
    seed_history()
    if accepted:
        assert reserve_m1_intents([intent("new", price=price)], limits=bounds(), now=NOW)
        assert lifetime() == 5000
    else:
        with pytest.raises(ValueError, match="histórico"):
            reserve_m1_intents([intent("new", price=price)], limits=bounds(), now=NOW)
        assert lifetime() == 4954
        with models.get_session() as session:
            assert len(list(session.exec(select(models.Trade)))) == 1


@pytest.mark.parametrize("status", ["cancelled", "filled", "settled", "error", "pending"])
def test_partial_terminal_and_restarts_never_replenish_historical_budget(sandbox, status):
    reserve_m1_intents([intent()], limits=bounds(), now=NOW)
    with models.get_session() as session:
        row = session.exec(select(models.Trade)).one()
        resize_filled(row, Fraction(1, 4))
        row.status = status
        session.add(row)
        session.commit()
    sandbox[1].dispose()
    assert lifetime(NOW + timedelta(days=40)) == 42


def test_moving_start_cannot_replenish_history(sandbox):
    seed_history()
    with pytest.raises(ValueError, match="histórico"):
        reserve_m1_intents([intent("new", price=45)], limits=bounds(experiment_start=NOW), now=NOW)
    assert lifetime() == 4954


def test_replay_does_not_consume_again_and_modified_replay_fails(sandbox):
    reserve_m1_intents([intent()], limits=bounds(), now=NOW)
    assert not reserve_m1_intents([intent()], limits=bounds(), now=NOW + timedelta(days=1))
    with pytest.raises(ValueError, match="replay cambia"):
        reserve_m1_intents([intent(price=41)], limits=bounds(), now=NOW)
    assert lifetime() == 42


def test_preexisting_legacy_trade_cannot_hide_before_start(sandbox):
    with models.get_session() as session:
        row = intent()
        row.placed_at = NOW - timedelta(days=50)
        row.status = "cancelled"
        session.add(row)
        session.commit()
    with pytest.raises(ValueError, match="compromiso original"):
        reserve_m1_intents([intent("new")], limits=bounds(), now=NOW)


@pytest.mark.parametrize("cap", [None, True, 0, -1, 5001, 50.0])
def test_invalid_lifetime_limit_never_persists(sandbox, cap):
    with pytest.raises(ValueError, match="límite histórico"):
        reserve_m1_intents([intent()], limits=bounds(max_lifetime_cents=cap), now=NOW)
    assert lifetime() == 0


def _reserve_worker(path, coid, barrier, results):
    models._engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 5})
    barrier.wait(5)
    try:
        reserve_m1_intents([intent(coid, ticker=f"{coid}-A")], limits=bounds(), now=NOW)
        results.put("reserved")
    except ValueError as exc:
        results.put(str(exc))
    finally:
        models._engine.dispose()


def test_two_processes_cannot_spend_same_lifetime_remainder(sandbox):
    seed_history()
    ctx = multiprocessing.get_context("fork")
    barrier, results = ctx.Event(), ctx.Queue()
    processes = [
        ctx.Process(target=_reserve_worker, args=(str(sandbox[2]), str(i), barrier, results))
        for i in range(2)
    ]
    for process in processes:
        process.start()
    barrier.set()
    try:
        outcomes = [results.get(timeout=8) for _ in processes]
        assert outcomes.count("reserved") == 1
        assert sum("histórico" in result for result in outcomes) == 1
    finally:
        for process in processes:
            process.join(8)
            if process.is_alive():
                process.terminate()
                process.join(2)
        results.close()
    assert all(p.exitcode == 0 for p in processes)
    assert lifetime() == 4996


@pytest.mark.parametrize("field", ["settled_at", "pnl_cents"])
def test_unknown_settlement_blocks_instead_of_counting_zero(sandbox, field):
    reserve_m1_intents([intent()], limits=bounds(), now=NOW)
    with models.get_session() as session:
        row = session.exec(select(models.Trade)).one()
        row.status, row.settled_at, row.pnl_cents = "settled", NOW, 0
        setattr(row, field, None)
        session.add(row)
        session.commit()
    assert RiskManager()._experiment_accounting_snapshot() == {
        "valid": False,
        "reason": "settlement sin fecha/PnL verificable",
    }


async def test_explicit_zero_pnl_valid_and_lifetime_stop_durable(sandbox):
    seed_history()
    reserve_m1_intents([intent("new", price=44)], limits=bounds(), now=NOW)
    with models.get_session() as session:
        for row in session.exec(select(models.Trade)):
            row.status, row.settled_at, row.pnl_cents = "settled", NOW, 0
            session.add(row)
        session.commit()
    risk = RiskManager()
    assert risk._experiment_accounting_snapshot()["valid"]
    assert "histórico" in await risk._check_experiment_stops()
    assert BotState.is_paused
    sandbox[1].dispose()
    assert models.kill_switch_engaged()[0] is True


@pytest.mark.parametrize("pnl,expected", [(-300, "semanal"), (-500, "acumulada")])
async def test_50_realized_loss_stops_are_durable(sandbox, pnl, expected):
    reserve_m1_intents([intent()], limits=bounds(), now=NOW)
    with models.get_session() as session:
        row = session.exec(select(models.Trade)).one()
        row.status, row.settled_at, row.pnl_cents = "settled", NOW, pnl
        session.add(row)
        session.commit()
    assert expected in await RiskManager()._check_experiment_stops()
    sandbox[1].dispose()
    assert models.kill_switch_engaged()[0] is True
