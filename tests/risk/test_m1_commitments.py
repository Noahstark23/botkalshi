"""Presupuesto inmutable y admisión M1 antes de red, también entre procesos."""

import multiprocessing
import sqlite3
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import event
from sqlmodel import create_engine, select

import src.storage.models as models
from src.math.arbitrage import detect_binary_arb
from src.monitoring.health import BotState
from src.risk.commitments import daily_committed_cents, reserve_m1_intents
from src.risk.manager import RiskManager
from src.storage.quantities import quantity_fields, resize_filled
from src.storage.reductions import finalize_reduction, persist_reduction
from src.strategies.motor_1_arbitrage.executor import ArbitrageExecutor
from src.utils.config import Settings

NOW = datetime(2026, 10, 6, 16, tzinfo=UTC)
START = NOW - timedelta(hours=1)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "budget.db"
    engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 5})
    models.SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(models, "_engine", engine)
    monkeypatch.setattr(BotState, "is_paused", False)
    deny = Mock(side_effect=AssertionError("offline: no red"))
    monkeypatch.setattr("socket.socket.connect", deny)
    monkeypatch.setattr("socket.socket.connect_ex", deny)
    monkeypatch.setattr("socket.create_connection", deny)
    yield path, engine
    engine.dispose()


def intent(coid="a", *, ticker="EVENT-YES", count=1, price=40):
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


def limits(**overrides):
    return {
        "max_open_cents": 600,
        "event_cap_cents": 200,
        "max_daily_cents": 600,
        "experiment_start": START,
        **overrides,
    }


def daily(now=NOW):
    with models.get_session() as session:
        return daily_committed_cents(session, start=START, now=now)


@pytest.mark.parametrize("status", ["cancelled", "filled", "settled", "error"])
def test_original_budget_survives_terminal_partial_cancel_and_restart(ledger, status):
    assert reserve_m1_intents([intent(count=2)], limits=limits(), now=NOW)
    assert daily() == 84  # 80c riesgo original + 4c reserva de fee.
    with models.get_session() as session:
        row = session.exec(select(models.Trade)).one()
        resize_filled(row, Fraction(1, 4))
        row.status, row.fees_cents = status, 0
        session.add(row)
        session.commit()
    ledger[1].dispose()  # El resultado no depende de caché ni count actual.
    assert daily() == 84
    assert daily(NOW + timedelta(days=1)) == 0


def test_split_preserves_origin_without_new_budget(ledger):
    reserve_m1_intents([intent()], limits=limits(), now=NOW)
    with models.get_session() as session:
        row = session.exec(select(models.Trade)).one()
        resize_filled(row, 1)
        row.status = "filled"
        row.fees_cents = 2
        session.add(row)
        session.commit()
    persist_reduction("a", "sell", Fraction(1, 4), 40)
    finalize_reduction("sell", Fraction(1, 4))
    assert daily() == 42
    with models.get_session() as session:
        buys = list(session.exec(select(models.Trade).where(models.Trade.action == "buy")))
        assert len(buys) == 2 and {b.commitment_origin_id for b in buys} == {"a"}
        assert len(list(session.exec(select(models.RiskCommitment)))) == 1


def test_replay_after_quantity_changed_is_idempotent_but_changed_request_rejected(ledger):
    assert reserve_m1_intents([intent()], limits=limits(), now=NOW)
    with models.get_session() as session:
        row = session.exec(select(models.Trade)).one()
        resize_filled(row, Fraction(1, 4))
        row.status = "filled"
        session.add(row)
        session.commit()
    assert not reserve_m1_intents([intent()], limits=limits(), now=NOW)
    with pytest.raises(ValueError, match="replay cambia"):
        reserve_m1_intents([intent(price=41)], limits=limits(), now=NOW)
    assert daily() == 42


def test_legacy_after_start_blocks_instead_of_inventing_original_amount(ledger):
    with models.get_session() as session:
        session.add(intent())
        session.commit()
    with pytest.raises(ValueError, match="compromiso original"):
        reserve_m1_intents([intent("new")], limits=limits(), now=NOW)
    with models.get_session() as session:
        assert len(list(session.exec(select(models.Trade)))) == 1
        assert not list(session.exec(select(models.RiskCommitment)))


def test_event_cap_includes_proposed_risk_and_fee_at_equality(ledger):
    assert reserve_m1_intents([intent()], limits=limits(event_cap_cents=84), now=NOW)
    assert reserve_m1_intents([intent("b")], limits=limits(event_cap_cents=84), now=NOW)
    with pytest.raises(ValueError, match="evento"):
        reserve_m1_intents([intent("c", price=1)], limits=limits(event_cap_cents=84), now=NOW)
    assert daily() == 84


@pytest.mark.parametrize("gate", ["global", "event"])
def test_disjoint_ledger_and_portfolio_exposures_are_added(ledger, gate):
    reserve_m1_intents([intent(ticker="E-A")], limits=limits(), now=NOW)
    with models.get_session() as session:
        session.add(
            models.PortfolioPosition(
                ticker="E-B" if gate == "event" else "F-B",
                side="yes",
                exposure_cents=40,
                **quantity_fields(1),
            )
        )
        session.commit()
    bounds = limits(
        max_open_cents=100 if gate == "global" else 600,
        event_cap_cents=100 if gate == "event" else 200,
    )
    with pytest.raises(ValueError, match="global|evento"):
        reserve_m1_intents([intent("b", ticker="E-C")], limits=bounds, now=NOW)
    assert daily() == 42


@pytest.mark.parametrize("status", ["filled", "pending"])
def test_overlap_only_for_same_confirmed_identity_never_pending(ledger, status):
    reserve_m1_intents([intent()], limits=limits(), now=NOW)
    with models.get_session() as session:
        row = session.exec(select(models.Trade)).one()
        if status == "filled":
            resize_filled(row, 1)
            row.status = "filled"
            session.add(row)
        session.add(
            models.PortfolioPosition(
                ticker="EVENT-YES",
                side="yes",
                exposure_cents=40,
                **quantity_fields(1),
            )
        )
        session.commit()
    if status == "filled":
        assert reserve_m1_intents([intent("b")], limits=limits(max_open_cents=84), now=NOW)
    else:
        with pytest.raises(ValueError, match="global"):
            reserve_m1_intents([intent("b")], limits=limits(max_open_cents=84), now=NOW)


def test_rebuild_copies_commitments_and_split_references(ledger, monkeypatch):
    import scripts.rebuild_db as rebuild
    from src.storage.maintenance import _RETENTION_DAYS

    reserve_m1_intents([intent()], limits=limits(), now=NOW)
    with models.get_session() as session:
        row = session.exec(select(models.Trade)).one()
        resize_filled(row, 1)
        row.status = "filled"
        row.fees_cents = 2
        session.add(row)
        session.commit()
    persist_reduction("a", "rebuild-sell", Fraction(1, 4), 40)
    finalize_reduction("rebuild-sell", Fraction(1, 4))
    assert "risk_commitments" not in _RETENTION_DAYS
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{ledger[0]}")
    assert rebuild.main() == 0
    with sqlite3.connect(f"{ledger[0]}.rebuilt") as db:
        assert db.execute(
            "SELECT risk_cents + fee_reserve_cents FROM risk_commitments"
        ).fetchone() == (42,)
        assert db.execute(
            "SELECT count(*) FROM trades t JOIN risk_commitments c ON t.commitment_origin_id=c.client_order_id"
        ).fetchone() == (2,)
        indices = {row[1] for row in db.execute("PRAGMA index_list('risk_commitments')")}
        assert "ix_risk_commitments_created_at" in indices


def test_cancel_does_not_restore_daily_budget(ledger):
    reserve_m1_intents([intent()], limits=limits(max_daily_cents=42), now=NOW)
    with models.get_session() as session:
        row = session.exec(select(models.Trade)).one()
        row.status = "cancelled"
        session.add(row)
        session.commit()
    with pytest.raises(ValueError, match="diario"):
        reserve_m1_intents([intent("b")], limits=limits(max_daily_cents=42), now=NOW)


def test_failure_between_trade_and_commitment_rolls_back_entire_group(ledger):
    def fail(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO risk_commitments"):
            raise RuntimeError("crash durante persistencia")

    event.listen(ledger[1], "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="crash"):
            reserve_m1_intents([intent("a"), intent("b")], limits=limits(), now=NOW)
    finally:
        event.remove(ledger[1], "before_cursor_execute", fail)
    with models.get_session() as session:
        assert not list(session.exec(select(models.Trade)))
        assert not list(session.exec(select(models.RiskCommitment)))


def _worker(path, coid, ticker, bounds, start, results):
    models._engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 5})
    start.wait(5)
    try:
        result = reserve_m1_intents([intent(coid, ticker=ticker)], limits=bounds, now=NOW)
        results.put("reserved" if result else "replay")
    except ValueError as exc:
        results.put(str(exc))
    finally:
        models._engine.dispose()


@pytest.mark.parametrize("gate", ["daily", "event", "global"])
def test_two_processes_cannot_spend_same_remaining_budget(ledger, gate):
    bounds = limits(
        max_daily_cents=60 if gate == "daily" else 600,
        event_cap_cents=60 if gate == "event" else 200,
        max_open_cents=60 if gate == "global" else 600,
    )
    ctx = multiprocessing.get_context("fork")
    start, results = ctx.Event(), ctx.Queue()
    workers = [
        ctx.Process(
            target=_worker,
            args=(
                str(ledger[0]),
                str(i),
                "E-A" if gate == "event" else f"E{i}-A",
                bounds,
                start,
                results,
            ),
        )
        for i in range(2)
    ]
    for worker in workers:
        worker.start()
    start.set()
    try:
        outcomes = [results.get(timeout=8) for _ in workers]
        assert outcomes.count("reserved") == 1
        assert len([x for x in outcomes if x.startswith("reserva M1 excede")]) == 1
    finally:
        for worker in workers:
            worker.join(8)
            if worker.is_alive():
                worker.terminate()
                worker.join(2)
        results.close()
    assert all(worker.exitcode == 0 for worker in workers)
    assert daily() == 42


async def test_executor_event_rejection_never_calls_order_api(ledger, monkeypatch):
    settings = Settings.model_construct(
        TRADING_ENABLED=True,
        KALSHI_ENV="demo",
        DYNAMIC_CAPITAL_ENABLED=False,
        ACTIVE_CAPITAL_USD=200,
        MAX_EVENT_DIRECTIONAL_EXPOSURE_USD=0.88,
        MAX_TRADE_SIZE_PCT=1,
        MAX_TRADE_SIZE_USD=2,
    )
    monkeypatch.setattr("src.risk.manager.get_settings", lambda: settings)
    monkeypatch.setattr("src.strategies.motor_1_arbitrage.executor.get_settings", lambda: settings)
    risk = RiskManager()
    client = AsyncMock()
    client.get_available_balance_usd.return_value = 200
    opp = detect_binary_arb("EVENT-A", 40, 10, 45, 10, max_count=1)
    assert opp is not None
    assert not await ArbitrageExecutor(client, risk).execute(opp)
    client.place_order.assert_not_called()
    with models.get_session() as session:
        assert not list(session.exec(select(models.Trade)))
        assert not list(session.exec(select(models.RiskCommitment)))
