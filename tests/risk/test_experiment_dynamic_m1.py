"""Capital dinámico con controles M1 durables; SQLite temporal y red bloqueada."""

import asyncio
import multiprocessing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from unittest.mock import AsyncMock, Mock

import pytest
from freezegun import freeze_time
from sqlmodel import create_engine, select

import src.storage.models as models
from src.math.arbitrage import detect_binary_arb
from src.monitoring.health import BotState
from src.risk.commitments import daily_committed_cents, lifetime_committed_cents, reserve_m1_intents
from src.risk.experiment_policy import limits_for_bank50, limits_for_dynamic_m1
from src.risk.manager import RiskManager
from src.storage.quantities import quantity_fields, resize_filled
from src.utils.config import Settings

NOW = datetime(2026, 10, 7, 16, tzinfo=UTC)
START = NOW - timedelta(hours=1)


def intent(coid, *, price=45, count=1, ticker="EVENT-A"):
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


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    path = tmp_path / "dynamic.db"
    engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 5})
    models.SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(models, "_engine", engine)
    monkeypatch.setattr(BotState, "is_paused", False)
    monkeypatch.setattr(BotState, "pause_reason", None)
    monkeypatch.setattr(RiskManager, "_check_lock", asyncio.Lock())
    monkeypatch.setattr(RiskManager, "_cached_capital_usd", 100.43)
    monkeypatch.setattr(RiskManager, "_last_raw_balance_usd", 100.43)
    monkeypatch.setattr(RiskManager, "_last_balance_at", NOW.replace(tzinfo=None))
    settings = Settings.model_construct(
        KALSHI_ENV="production",
        TRADING_ENABLED=False,
        EXPERIMENT_DYNAMIC_M1_ENABLED=True,
        EXPERIMENT_START_AT=START.isoformat(),
        EXPERIMENT_BALANCE_MAX_AGE_SEC=60,
        BALANCE_MAX_AGE_SECONDS=60,
        BALANCE_REFRESH_SECONDS=30,
        ACTIVE_CAPITAL_USD=17,
        CAPITAL_CAP_USD=5000,
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
    denied = Mock(side_effect=AssertionError("DYNAMIC_M1_OFFLINE_ONLY"))
    for target in (
        "socket.socket.connect",
        "socket.socket.connect_ex",
        "socket.create_connection",
        "socket.getaddrinfo",
    ):
        monkeypatch.setattr(target, denied)
    monkeypatch.setattr("src.clients.kalshi_rest.KalshiRestClient.__init__", denied)
    with freeze_time(NOW, real_asyncio=True):
        yield settings, engine, path
    engine.dispose()
    denied.assert_not_called()


def totals():
    with models.get_session() as session:
        return (
            daily_committed_cents(session, start=START, now=NOW),
            lifetime_committed_cents(session, now=NOW),
        )


def test_policy_has_no_fifty_cap_but_absolute_limits_never_grow():
    for capital in (45, 90.387, 1000, 5000):
        policy = limits_for_dynamic_m1(capital)
        assert policy.capital_usd == Decimal(str(capital)).quantize(
            Decimal(".01"), rounding="ROUND_DOWN"
        )
        assert policy.max_per_operation_usd == Decimal("1")
        assert policy.max_open_usd == policy.max_daily_new_risk_usd == Decimal("3")
    assert limits_for_bank50(1000).capital_usd == 50
    assert limits_for_dynamic_m1(10).max_per_operation_usd == Decimal(".30")


@pytest.mark.parametrize("cash", [49.49, 100.43, 100.5187, 1000])
async def test_cash_drives_capital_not_reference_or_legacy_ack(sandbox, monkeypatch, cash):
    settings, _, _ = sandbox
    monkeypatch.setattr(RiskManager, "_cached_capital_usd", cash)
    monkeypatch.setattr(RiskManager, "_last_raw_balance_usd", cash)
    settings.EXPERIMENT_BANK_CONFIRMED_USD = 49.49
    risk = RiskManager()
    assert risk.effective_capital_usd() == pytest.approx(cash * 0.9)
    decision = await risk.check_pre_trade(detect_binary_arb("E-A", 45, 10, 45, 10))
    assert decision.approved and decision.max_allowed_count == 1
    limits = risk.m1_reservation_limits()
    assert limits["max_open_cents"] == limits["max_daily_cents"] == 300
    assert limits["event_cap_cents"] == 100
    assert "max_lifetime_cents" not in limits


@pytest.mark.parametrize("raw,cached,age", [(None, None, 0), (44, 1000, 0), (100, 100, 61)])
async def test_missing_stale_or_withdrawn_cash_never_uses_fallback(
    sandbox, monkeypatch, raw, cached, age
):
    sandbox[0].ACTIVE_CAPITAL_USD = 4000
    monkeypatch.setattr(RiskManager, "_cached_capital_usd", cached)
    monkeypatch.setattr(RiskManager, "_last_raw_balance_usd", raw)
    monkeypatch.setattr(
        RiskManager, "_last_balance_at", (NOW - timedelta(seconds=age)).replace(tzinfo=None)
    )
    risk = RiskManager()
    assert not risk.can_open_new_positions()
    decision = await risk.check_pre_trade(detect_binary_arb("E-A", 45, 10, 45, 10))
    assert not decision.approved


def test_cash_expiry_boundary_and_deposit_keep_absolute_caps(sandbox, monkeypatch):
    risk = RiskManager()
    for cash, expected in ((100.43, 90.387), (100.5187, 90.46683), (1000, 900), (49.49, 44.541)):
        monkeypatch.setattr(RiskManager, "_cached_capital_usd", cash)
        monkeypatch.setattr(RiskManager, "_last_raw_balance_usd", cash)
        monkeypatch.setattr(
            RiskManager, "_last_balance_at", (NOW - timedelta(seconds=60)).replace(tzinfo=None)
        )
        assert risk.can_open_new_positions()
        assert risk.effective_capital_usd() == pytest.approx(expected)
        policy = risk._experiment_limits(risk.effective_capital_usd())
        assert policy.max_per_operation_usd == 1
        assert policy.max_open_usd == policy.max_daily_new_risk_usd == 3
    monkeypatch.setattr(
        RiskManager, "_last_balance_at", (NOW - timedelta(seconds=60.001)).replace(tzinfo=None)
    )
    assert not risk.can_open_new_positions()
    with pytest.raises(ValueError, match="capital confirmado"):
        risk.m1_reservation_limits()


def test_history_above_fifty_preserved_but_no_longer_blocks_admission(sandbox):
    reserve_m1_intents([intent("old", price=99, count=100)], now=NOW - timedelta(days=2))
    with models.get_session() as session:
        row = session.exec(select(models.Trade)).one()
        row.status = "cancelled"
        session.add(row)
        session.commit()
    assert totals() == (0, 9907)
    risk = RiskManager()
    assert reserve_m1_intents([intent("new")], limits=risk.m1_reservation_limits(), now=NOW)
    assert totals() == (47, 9954)
    assert risk._experiment_accounting_snapshot()["lifetime_committed_cents"] is None


def test_daily_three_atomic_survives_cancel_partial_restart_and_replay(sandbox):
    limits = RiskManager().m1_reservation_limits()
    assert reserve_m1_intents([intent("old", price=99, count=3)], now=NOW)
    assert totals() == (298, 298)
    with models.get_session() as session:
        row = session.exec(select(models.Trade)).one()
        resize_filled(row, Fraction(1, 4))
        row.status = "cancelled"
        session.add(row)
        session.commit()
    sandbox[1].dispose()
    assert not reserve_m1_intents([intent("old", price=99, count=3)], limits=limits, now=NOW)
    assert reserve_m1_intents([intent("last", price=1)], limits=limits, now=NOW)
    assert totals() == (300, 300)
    with pytest.raises(ValueError, match="diario"):
        reserve_m1_intents([intent("over", price=1, ticker="OTHER-A")], limits=limits, now=NOW)
    assert totals() == (300, 300)


def test_next_day_restores_daily_allowance_without_erasing_history(sandbox):
    limits = RiskManager().m1_reservation_limits()
    reserve_m1_intents([intent("old", price=99, count=3)], now=NOW)
    with models.get_session() as session:
        row = session.exec(select(models.Trade)).one()
        row.status = "cancelled"
        session.add(row)
        session.commit()
    tomorrow = NOW + timedelta(days=1)
    sandbox[1].dispose()
    reserve_m1_intents([intent("tomorrow")], limits=limits, now=tomorrow)
    with models.get_session() as session:
        assert daily_committed_cents(session, start=START, now=tomorrow) == 47
        assert lifetime_committed_cents(session, now=tomorrow) == 345
        assert len(list(session.exec(select(models.RiskCommitment)))) == 2


def _worker(path, coid, limits, start, results):
    models._engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 5})
    start.wait(5)
    try:
        reserve_m1_intents([intent(coid, price=44, ticker=f"{coid}-A")], limits=limits, now=NOW)
        results.put("reserved")
    except ValueError as exc:
        results.put(str(exc))
    finally:
        models._engine.dispose()


def test_two_processes_cannot_spend_same_daily_remainder(sandbox):
    reserve_m1_intents([intent("old", price=49, count=5)], now=NOW)
    with models.get_session() as session:
        row = session.exec(select(models.Trade)).one()
        row.status = "cancelled"
        session.add(row)
        session.commit()
    assert totals() == (254, 254)  # 245c gross + 9c de reserva de fee; quedan46c.
    ctx = multiprocessing.get_context("fork")
    start, results = ctx.Event(), ctx.Queue()
    workers = [
        ctx.Process(
            target=_worker,
            args=(str(sandbox[2]), str(i), RiskManager().m1_reservation_limits(), start, results),
        )
        for i in range(2)
    ]
    for worker in workers:
        worker.start()
    start.set()
    try:
        outcomes = [results.get(timeout=8) for _ in workers]
        assert outcomes.count("reserved") == 1
        assert sum("diario" in value for value in outcomes) == 1
    finally:
        for worker in workers:
            worker.join(8)
            if worker.is_alive():
                worker.terminate()
                worker.join(2)
        results.close()
    assert all(worker.exitcode == 0 for worker in workers)
    assert totals() == (300, 300)


@pytest.mark.parametrize("pnl,days,expected", [(-300, 0, "semanal"), (-500, 10, "acumulada")])
async def test_stops_keep_three_and_five_despite_deposit_start_or_restart(
    sandbox, monkeypatch, pnl, days, expected
):
    reserve_m1_intents([intent("settled")], now=NOW - timedelta(days=days))
    with models.get_session() as session:
        row = session.exec(select(models.Trade)).one()
        row.status, row.settled_at, row.pnl_cents = "settled", NOW - timedelta(days=days), pnl
        session.add(row)
        session.commit()
    sandbox[0].EXPERIMENT_START_AT = NOW.isoformat()
    monkeypatch.setattr(RiskManager, "_cached_capital_usd", 5000)
    monkeypatch.setattr(RiskManager, "_last_raw_balance_usd", 5000)
    assert expected in await RiskManager()._check_experiment_stops()
    sandbox[1].dispose()
    assert models.kill_switch_engaged()[0]


@pytest.mark.parametrize("field", ["pnl_cents", "settled_at"])
def test_incomplete_settlement_still_blocks(sandbox, field):
    reserve_m1_intents([intent("settled")], now=NOW)
    with models.get_session() as session:
        row = session.exec(select(models.Trade)).one()
        row.status, row.settled_at, row.pnl_cents = "settled", NOW, 0
        setattr(row, field, None)
        session.add(row)
        session.commit()
    assert not RiskManager()._experiment_accounting_snapshot()["valid"]
