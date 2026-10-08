"""M1/M2/M5 compiten por un presupuesto SQLite; no API ni dinero real."""

import multiprocessing
from datetime import timedelta
from fractions import Fraction
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event
from sqlmodel import create_engine, select

import src.storage.models as models
from src.monitoring.health import BotState
from src.risk.commitments import daily_committed_cents, reserve_trade_intents
from src.risk.manager import RiskManager
from src.storage.quantities import quantity_fields, resize_filled
from src.strategies.motor_2_consensus.detector import ConsensusSignal
from src.strategies.motor_2_consensus.executor import Motor2Executor
from src.strategies.motor_5_mm.executor import LiveOrder, Motor5Executor
from src.strategies.motor_5_mm.fee_policy import EffectiveFeeObservation
from src.strategies.motor_5_mm.incidents import incident_status
from src.strategies.motor_5_mm.quoter import QuoteSet
from tests.risk.test_experiment_dynamic_m1 import NOW, START, sandbox  # noqa: F401


@pytest.fixture
def multi(sandbox, monkeypatch):  # noqa: F811 — fixture importada deliberadamente
    settings, engine, path = sandbox
    settings.EXPERIMENT_DYNAMIC_M1_ENABLED = False
    settings.EXPERIMENT_DYNAMIC_MULTI_ENABLED = True
    settings.UNREALIZED_STOP_ENABLED = True
    monkeypatch.setattr(RiskManager, "_marks", {})
    yield settings, engine, path


def ticker(event="A", outcome="PHI"):
    return f"KXMLBGAME-26OCT07{event}-{outcome}"


def row(coid, strategy="motor_1_arbitrage", *, event="A", action="buy", price=40, count=1):
    return models.Trade(
        client_order_id=coid,
        ticker=ticker(event),
        strategy=strategy,
        side="yes",
        action=action,
        price_cents=price,
        status="pending",
        placed_at=NOW,
        **quantity_fields(count),
    )


def fee(t, **overrides):
    data = {
        "series_ticker": "KXMLBGAME",
        "event_ticker": t.rsplit("-", 1)[0],
        "fee_type": "quadratic_with_maker_fees",
        "multiplier": Fraction(1, 2),
        "observed_at": NOW,
    }
    return EffectiveFeeObservation(**{**data, **overrides})


def bounds(**overrides):
    return {
        "multi_profile": True,
        "max_open_cents": 300,
        "max_daily_cents": 300,
        "max_operation_cents": 100,
        "event_cap_cents": 100,
        "experiment_start": START,
        **overrides,
    }


def reserve(*trades, limits=None, observations=None):
    return reserve_trade_intents(
        list(trades),
        limits=limits or bounds(),
        now=NOW,
        fee_observations=observations
        if observations is not None
        else {t.ticker: fee(t.ticker) for t in trades},
    )


def spent():
    with models.get_session() as session:
        return daily_committed_cents(session, start=START, now=NOW)


@pytest.mark.parametrize(
    "strategy,action,price,risk",
    [
        ("motor_1_arbitrage", "buy", 40, 40),
        ("motor_2_consensus", "buy", 40, 40),
        ("motor_5_mm", "buy", 40, 40),
        ("motor_5_mm", "sell", 30, 70),
    ],
)
def test_all_entries_get_immutable_original_commitment(multi, strategy, action, price, risk):
    assert reserve(row("one", strategy, action=action, price=price))
    with models.get_session() as session:
        trade = session.exec(select(models.Trade)).one()
        commitment = session.exec(select(models.RiskCommitment)).one()
        assert trade.commitment_origin_id == commitment.client_order_id == "one"
        assert commitment.risk_cents == risk
        initial = spent()
        resize_filled(trade, Fraction(1, 4))
        trade.status, trade.fees_cents = "filled", 1
        session.add(trade)
        session.commit()
    multi[1].dispose()
    assert spent() == initial
    assert not reserve(row("one", strategy, action=action, price=price))
    with pytest.raises(ValueError, match="replay cambia"):
        reserve(row("one", strategy, action=action, price=price + 1))


def test_global_budget_is_shared_and_cancellation_never_refunds_day(multi):
    for idx, (strategy, action, price) in enumerate(
        [
            ("motor_1_arbitrage", "buy", 95),
            ("motor_2_consensus", "buy", 95),
            ("motor_5_mm", "sell", 5),
        ]
    ):
        reserve(row(str(idx), strategy, event=str(idx), action=action, price=price))
    assert spent() == 288
    with pytest.raises(ValueError, match="global"):
        reserve(row("too-open", event="Z", price=14))
    with models.get_session() as session:
        for trade in session.exec(select(models.Trade)):
            trade.status = "cancelled"
            session.add(trade)
        session.commit()
    reserve(row("last12", event="Z", price=11))
    assert spent() == 300
    with pytest.raises(ValueError, match="diario"):
        reserve(row("over-day", "motor_5_mm", event="Y", price=1))
    assert spent() == 300


def test_event_risk_sums_distinct_outcomes_and_strategies(multi):
    reserve(row("m2", "motor_2_consensus", price=60))
    proposed = row("mm", "motor_5_mm", action="sell", price=50)
    proposed.ticker = ticker("A", "NYM")
    with pytest.raises(ValueError, match="evento"):
        reserve(proposed)
    assert spent() == 62


@pytest.mark.parametrize("state", ["pending", "filled"])
def test_same_ticker_cannot_be_taken_from_another_motor(multi, state):
    reserve(row("m1", price=10))
    if state == "filled":
        with models.get_session() as session:
            trade = session.exec(select(models.Trade)).one()
            resize_filled(trade, 1)
            trade.status = state
            session.add(trade)
            session.commit()
    with pytest.raises(ValueError, match="otro motor"):
        reserve(row("mm", "motor_5_mm", action="sell", price=90))
    assert spent() == 11


def test_same_m5_owner_can_reserve_both_sides(multi):
    reserve(row("bid", "motor_5_mm", price=45))
    reserve(row("ask", "motor_5_mm", action="sell", price=55))
    assert spent() == 94


def test_unattributed_portfolio_and_stale_ownership_block(multi):
    with models.get_session() as session:
        session.add(
            models.PortfolioPosition(
                ticker=ticker(),
                side="yes",
                count=1,
                exposure_cents=10,
                synced_at=NOW.replace(tzinfo=None),
            )
        )
        session.commit()
    with pytest.raises(ValueError, match="propiedad"):
        reserve(row("mm", "motor_5_mm", action="sell", price=90))
    assert spent() == 0


def test_m2_one_per_event_is_checked_inside_transaction(multi):
    reserve(row("m2", "motor_2_consensus", price=10))
    other = row("m2-again", "motor_2_consensus", price=10)
    other.ticker = ticker("A", "NYM")
    with pytest.raises(ValueError, match="ya tiene entrada"):
        reserve(other)
    assert spent() == 11


@pytest.mark.parametrize(
    "change", ["missing", "override", "stale", "future", "wrong-event", "outside-universe"]
)
def test_unverified_fee_or_universe_cannot_create_intent(multi, change):
    trade = row("blocked", "motor_2_consensus")
    observations = {trade.ticker: fee(trade.ticker)}
    if change == "missing":
        observations = {}
    elif change == "override":
        observations[trade.ticker] = fee(trade.ticker, multiplier=Fraction(2))
    elif change in {"stale", "future"}:
        observations[trade.ticker] = fee(
            trade.ticker, observed_at=NOW + timedelta(seconds=1 if change == "future" else -31)
        )
    elif change == "wrong-event":
        observations[trade.ticker] = fee(trade.ticker, event_ticker="OTHER")
    else:
        trade.ticker = "KXNFLGAME-26OCT07A-A"
    with pytest.raises(ValueError, match="fee"):
        reserve(trade, observations=observations)
    assert spent() == 0


def test_ninth_ticker_blocked_despite_remaining_budget(multi):
    for i in range(8):
        reserve(row(str(i), event=str(i), price=1))
    with pytest.raises(ValueError, match="ocho tickers"):
        reserve(row("nine", event="nine", price=1))
    assert spent() == 16


@pytest.mark.parametrize(
    "kind", ["durable", "runtime", "weekly", "cumulative", "unknown-settlement"]
)
def test_pause_and_loss_are_rechecked_inside_reservation(multi, kind):
    with models.get_session() as session:
        if kind == "durable":
            session.add(
                models.OperationalState(key="kill_switch", value="engaged", reason="fixture")
            )
        elif kind == "runtime":
            BotState.is_paused = True
        else:
            loss = row("history", price=1)
            loss.placed_at = START - timedelta(days=10)
            loss.status = "settled"
            loss.settled_at = (NOW - timedelta(days=8 if kind == "cumulative" else 0)).replace(
                tzinfo=None
            )
            loss.pnl_cents = -500 if kind == "cumulative" else -300
            if kind == "unknown-settlement":
                loss.settled_at = None
            session.add(loss)
        session.commit()
    with pytest.raises(ValueError):
        reserve(row("blocked", "motor_2_consensus"))
    with models.get_session() as session:
        assert not list(session.exec(select(models.RiskCommitment)))


def test_failed_commit_rolls_back_trade_and_budget_together(multi):
    def fail(*args):
        if args[2].startswith("INSERT INTO risk_commitments"):
            raise RuntimeError("fixture disk failure")

    event.listen(multi[1], "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="disk"):
            reserve(row("mm", "motor_5_mm"))
    finally:
        event.remove(multi[1], "before_cursor_execute", fail)
    with models.get_session() as session:
        assert not list(session.exec(select(models.Trade)))
        assert not list(session.exec(select(models.RiskCommitment)))


def _worker(path, strategy, event_key, limits, start, results):
    models._engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 5})
    start.wait(5)
    try:
        reserve(row(strategy, strategy, event=event_key, price=40), limits=limits)
        results.put("reserved")
    except ValueError as exc:
        results.put(str(exc))
    finally:
        models._engine.dispose()


@pytest.mark.parametrize("gate", ["global", "daily", "ownership"])
def test_competing_motor_processes_cannot_double_spend(multi, gate):
    limits = bounds(
        max_open_cents=60 if gate == "global" else 300,
        max_daily_cents=60 if gate == "daily" else 300,
    )
    ctx = multiprocessing.get_context("fork")
    start, results = ctx.Event(), ctx.Queue()
    workers = [
        ctx.Process(
            target=_worker,
            args=(
                str(multi[2]),
                strategy,
                "A" if gate == "ownership" else str(i),
                limits,
                start,
                results,
            ),
        )
        for i, strategy in enumerate(("motor_2_consensus", "motor_5_mm"))
    ]
    for worker in workers:
        worker.start()
    start.set()
    try:
        outcomes = [results.get(timeout=8) for _ in workers]
        assert outcomes.count("reserved") == 1, outcomes
    finally:
        for worker in workers:
            worker.join(8)
            if worker.is_alive():
                worker.terminate()
                worker.join(2)
        results.close()
    assert all(worker.exitcode == 0 for worker in workers)
    assert spent() == 42


async def test_reserved_daily_budget_does_not_cancel_resting_quotes(multi):
    for i in range(3):
        reserve(row(f"prior{i}", event=f"P{i}", price=85))
        with models.get_session() as session:
            previous = session.exec(
                select(models.Trade).where(models.Trade.client_order_id == f"prior{i}")
            ).one()
            previous.status, previous.filled_count, previous.filled_count_fp = (
                "cancelled",
                0,
                "0.00",
            )
            session.add(previous)
            session.commit()
    reserve(row("mm", "motor_5_mm", price=40))
    with models.get_session() as session:
        trade = session.exec(select(models.Trade).where(models.Trade.client_order_id == "mm")).one()
        trade.filled_count, trade.filled_count_fp = 0, "0.00"
        session.add(trade)
        session.commit()
    risk = RiskManager()
    assert "diario" in await risk._check_experiment_stops()
    assert await risk.resting_quote_block_reason() is None
    with models.get_session() as session:
        loss = row("loss", event="L")
        loss.placed_at = START - timedelta(days=1)
        loss.status, loss.pnl_cents, loss.settled_at = "settled", -300, NOW.replace(tzinfo=None)
        session.add(loss)
        session.commit()
    assert "semanal" in await risk.resting_quote_block_reason()
    assert BotState.is_paused


async def test_m5_reservation_failure_never_places_or_latches_fake_corruption(multi):
    reserve(row("m1", price=10))
    client = AsyncMock()
    executor = Motor5Executor(client, risk=RiskManager(), max_exposure_usd=3)
    # The entry gate is independent from the transactional ownership check.
    multi[0].UNREALIZED_STOP_ENABLED = False
    assert await executor._place(ticker(), "sell", 90, 1, fee_observation=fee(ticker())) is None
    client.place_order.assert_not_called()
    assert not executor.corrupted
    assert incident_status() == (False, None)
    assert spent() == 11


async def test_real_m2_partial_then_m5_intent_share_original_budget(multi, monkeypatch):
    calls = []

    async def fake_place(**kwargs):
        # Transporte ficticio; ambos registros ya existen antes del supuesto POST.
        with models.get_session() as session:
            trade = session.exec(
                select(models.Trade).where(
                    models.Trade.client_order_id == kwargs["client_order_id"]
                )
            ).one()
            commitment = session.get(models.RiskCommitment, trade.commitment_origin_id)
            assert commitment and commitment.count_fp == "1.00"
        calls.append(kwargs)
        return {
            "order": {
                "order_id": f"fake-{len(calls)}",
                "client_order_id": kwargs["client_order_id"],
                "ticker": kwargs["ticker"],
                "side": "yes",
                "action": kwargs["action"],
                "status": "executed" if len(calls) == 1 else "resting",
                "fill_count_fp": "0.25" if len(calls) == 1 else "0.00",
                "remaining_count_fp": "0.00" if len(calls) == 1 else "1.00",
            }
        }

    client = AsyncMock()
    client.place_order.side_effect = fake_place
    executor = Motor2Executor(client, RiskManager())
    monkeypatch.setattr(executor, "_current_ask", AsyncMock(return_value=(40, 1)))
    monkeypatch.setattr(
        "src.strategies.motor_5_mm.fee_policy.SeriesFeePolicy.observe",
        AsyncMock(return_value=fee(ticker())),
    )
    signal = ConsensusSignal(
        market_ticker=ticker(),
        event_key=ticker().rsplit("-", 1)[0],
        kalshi_side="YES",
        odds_api_fair_prob=0.5,
        kalshi_price_cents=40,
        edge_pct=0.05,
        recommended_size_usd=0.4,
    )
    outcome = await executor.execute(signal)
    assert outcome.filled_count == Fraction(1, 4)
    assert spent() == 42  # .25 ejecutado no devuelve el compromiso original de 1.
    RiskManager.record_mark(ticker(), "yes", 40, ask_cents=60, observed_at=NOW, fee_multiplier=0.5)
    maker = Motor5Executor(client, RiskManager(), max_exposure_usd=3)
    result = await maker._place(ticker("B"), "sell", 60, 1, fee_observation=fee(ticker("B")))
    assert result is not None and len(calls) == 2
    assert spent() == 84
    with models.get_session() as session:
        assert len(list(session.exec(select(models.RiskCommitment)))) == 2


async def test_m2_bad_fee_never_reaches_reservation_or_transport(multi, monkeypatch):
    client = AsyncMock()
    executor = Motor2Executor(client, RiskManager())
    monkeypatch.setattr(executor, "_current_ask", AsyncMock(return_value=(40, 1)))
    monkeypatch.setattr(
        "src.strategies.motor_5_mm.fee_policy.SeriesFeePolicy.observe",
        AsyncMock(return_value=fee(ticker(), multiplier=Fraction(2))),
    )
    signal = ConsensusSignal(
        market_ticker=ticker(),
        event_key=ticker().rsplit("-", 1)[0],
        kalshi_side="YES",
        odds_api_fair_prob=0.5,
        kalshi_price_cents=40,
        edge_pct=0.05,
        recommended_size_usd=0.4,
    )
    result = await executor.execute(signal)
    assert result.reason == "multi_fee_unverified" and not result.placed
    client.place_order.assert_not_called()
    assert spent() == 0


@pytest.mark.parametrize("invalid", [False, True])
async def test_unchanged_m5_quote_retires_on_uncovered_fee_and_keeps_partial_floor(multi, invalid):
    reserve(row("mm", "motor_5_mm", price=40))
    with models.get_session() as session:
        trade = session.exec(select(models.Trade)).one()
        trade.kalshi_order_id, trade.filled_count, trade.filled_count_fp = "oid", 0, "0.00"
        session.add(trade)
        session.commit()
    client = AsyncMock()
    client.cancel_order.return_value = {
        "order": {
            "order_id": "oid",
            "client_order_id": "mm",
            "status": "canceled",
            "fill_count_fp": "0.25",
            "remaining_count_fp": "0.00",
        }
    }
    executor = Motor5Executor(client, RiskManager(), max_exposure_usd=3)
    executor._live[ticker()] = {"buy": LiveOrder("mm", "oid", "buy", 40, 1)}
    quote = QuoteSet(ticker(), 0.5, 40, None, 1)
    outcome = await executor.sync_quotes(
        quote, fee_observation=fee(ticker(), multiplier=Fraction(2) if invalid else Fraction(1, 2))
    )
    assert outcome == ("risk_blocked" if invalid else "synced")
    client.place_order.assert_not_called()
    assert spent() == 42
    with models.get_session() as session:
        trade = session.exec(select(models.Trade)).one()
        assert trade.status == ("filled" if invalid else "pending")
        assert trade.filled_count_fp == ("0.25" if invalid else "0.00")
    assert client.cancel_order.await_count == int(invalid)
    assert bool(executor.live_coids()) is not invalid
