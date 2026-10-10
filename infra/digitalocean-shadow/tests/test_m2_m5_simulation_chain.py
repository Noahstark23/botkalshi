"""Offline wiring checks, never a live runner or profitability backtest.

Synthetic M2 inputs feed the existing detector and M5 F1 tick. The only client
is an in-memory fixture with three read methods. All storage is pytest tmp_path.
The string 'production' below is an existing F1 metric label required by its
validator; it selects no host, account, credentials, executor or real service.
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlmodel import SQLModel, select

import src.storage.models as models
import src.strategies.motor_5_mm.engine as m5_module
from scripts.tablero_gate import tablero
from src.clients.odds_api import Bookmaker, Market, OddsEvent, Outcome
from src.strategies.fair_value_book import FairValueBook
from src.strategies.motor_2_consensus.detector import (
    KalshiEventQuotes,
    KalshiQuote,
    find_signals,
)
from src.strategies.motor_5_mm.engine import Motor5Engine
from src.strategies.motor_5_mm.fee_policy import SeriesFeePolicy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import m5_bank_bridge as bridge  # noqa: E402
import m5_paper_ledger as wallet  # noqa: E402
import paper_engine as paper  # noqa: E402
import simulation_bank as bank  # noqa: E402

NOW = datetime(2026, 10, 9, 16, tzinfo=UTC)
EVENT = "KXMLBGAME-26OCT09PHINYM"
TICKER = f"{EVENT}-PHI"


class MemoryReadClient:
    """No order, cancel, authentication or networking capability."""

    def __init__(self, event=EVENT, fee_type="quadratic_with_maker_fees"):
        self.event = event
        self.fee_type = fee_type
        self.books = {}

    async def get_orderbook(self, ticker):
        return {"orderbook": self.books[ticker]}

    async def get_series(self, series):
        return {"series": {"fee_type": self.fee_type, "fee_multiplier": 0.5}}

    async def get_event(self, event):
        assert event == self.event
        return {"event": {"event_ticker": event, "series_ticker": event.split("-")[0]}}


def inputs():
    teams = ("Philadelphia Phillies", "New York Mets")
    books = tuple(
        Bookmaker(
            key=name,
            title=name,
            last_update=NOW - timedelta(minutes=1),
            markets=(Market("h2h", (Outcome(teams[0], 1.6), Outcome(teams[1], 2.6))),),
        )
        for name in ("synthetic-a", "synthetic-b", "synthetic-c")
    )
    odds = OddsEvent(
        id="synthetic-event",
        sport_key="baseball_mlb",
        commence_time=NOW + timedelta(hours=4),
        home_team=teams[0],
        away_team=teams[1],
        bookmakers=books,
    )
    event = KalshiEventQuotes(
        EVENT,
        (
            KalshiQuote(TICKER, teams[0], 55, 70),
            KalshiQuote(f"{EVENT}-NYM", teams[1], 70, 70),
        ),
    )
    return event, odds


def detect(event, odds):
    fairs, commence, events, provenance, diag = {}, {}, {}, {}, {}
    signals = find_signals(
        [event],
        odds,
        now=NOW,
        capital_usd=100,
        min_books=3,
        max_book_age_min=15,
        min_edge=0.03,
        max_edge=0.15,
        fair_out=fairs,
        fair_commence_out=commence,
        fair_event_out=events,
        fair_provenance_out=provenance,
        diag=diag,
    )
    # Explicit test wiring only. The operational poller continues to reject fake odds.
    FairValueBook.publish(
        fairs,
        now=NOW,
        commence_times=commence,
        event_tickers=events,
        provenances=provenance,
    )
    return signals, fairs, diag


def make_engine(client):
    engine = Motor5Engine(
        trading_enabled=False,
        client_factory=MemoryReadClient,
        fees_as_maker=True,
        exchange_environment="production",
        experiment_label="OFFLINE-SYNTHETIC",
        series_csv="KXMLBGAME",
        require_pregame=True,
        fair_min_books=3,
        fair_max_book_age_min=15,
        fair_odds_regions="us",
        fair_sport_keys_config="baseball_mlb",
        fair_cache_ttl_sec=60,
        max_tickers=2,
    )
    engine._client = client
    engine._fee_policy = SeriesFeePolicy(client)
    assert engine._executor is None and not engine._trading_enabled
    return engine


def top(bid=50, ask=70, depth="1.00"):
    return {"yes": [[bid, "1.00"]], "no": [[100 - ask, depth]]}


@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'shadow.sqlite3'}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(models, "_engine", engine)
    FairValueBook.clear()

    class Clock(datetime):
        current = NOW

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(m5_module, "datetime", Clock)
    yield Clock
    FairValueBook.clear()
    engine.dispose()


def durable_fills():
    with models.get_session() as session:
        return list(session.exec(select(models.MMShadowFill)))


def declared_payload(row):
    fields = (
        "id",
        "side",
        "price_cents",
        "count",
        "fee_effective_cents",
        "fee_model",
        "ticker",
        "experiment_id",
        "metric_version",
        "fee_source",
        "fee_type",
        "fee_multiplier",
    )
    return {
        "schema_version": "botkalshi-m5-fill-input-v1",
        "fill": {field: getattr(row, field) for field in fields},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("payout", [0, 50, 100])
async def test_m2_fair_to_m5_prefunded_wallet_and_settlement(tmp_path, payout):
    event, odds = inputs()
    detect(event, [odds])
    client = MemoryReadClient()
    client.books[TICKER] = top()
    engine = make_engine(client)
    await engine._tick()  # Cálculo de propuesta; todavía cero fills hipotéticos.
    q = engine._live_quotes[TICKER]
    path = tmp_path / "wallet.sqlite3"
    bank.init_bank(path, initial_capital_usd="100.00")
    proposal = {
        "quote_id": "coupled",
        "ticker": TICKER,
        "created_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        "bid_cents": q.bid_cents,
        "ask_cents": q.ask_cents,
        "count": q.size,
        "fee_type": "quadratic_with_maker_fees",
        "fee_multiplier": "0.5",
    }
    book = {
        "ticker": TICKER,
        "observed_at": NOW.isoformat(),
        "yes_bid": 50,
        "yes_ask": 70,
        "bid_depth": "1",
        "ask_depth": "1",
    }
    admitted = wallet.admit_quote(path, proposal, book)
    assert Decimal(admitted["bank"]["reserved_usd"]) > 0
    assert all(leg["filled"] == 0 for leg in admitted["legs"])
    at = (NOW + timedelta(seconds=1)).isoformat()
    crossed = {**book, "observed_at": at, "yes_ask": q.bid_cents - 1}
    wallet.observe(path, "coupled", observation_id="cross", at=at, book=crossed)
    wallet.cancel_quote(path, "coupled", at=at)
    result = wallet.settle_quote(path, "coupled", yes_payout_cents=payout)
    assert result["pnl_cents"] == payout - q.bid_cents - 1
    assert result["bank"]["reserved_usd"] == "0.00"


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_detector_fair_m5_quote_fill_persistence_and_bank_replay(tmp_path, side):
    event, odds = inputs()
    signals, fairs, _ = detect(event, [odds])
    assert len(signals) == 1 and signals[0].market_ticker == TICKER
    assert sum(fairs.values()) == pytest.approx(1)
    client = MemoryReadClient()
    client.books[TICKER] = top()
    engine = make_engine(client)
    await engine._tick()
    quote = engine._live_quotes[TICKER]
    assert quote.fair_prob == fairs[TICKER]
    assert quote.bid_cents < 70 and quote.ask_cents > 50
    assert durable_fills() == []  # publishing/quoting is not a fill
    client.books[TICKER] = (
        top(ask=quote.bid_cents - 1) if side == "buy" else top(bid=quote.ask_cents + 1, ask=80)
    )
    await engine._tick()
    (row,) = durable_fills()
    price = quote.bid_cents if side == "buy" else quote.ask_cents
    assert row.price_cents == price and row.count == 1 and row.side == side
    assert row.crossed_depth == Decimal("1")
    assert row.fair_odds_event_id == odds.id and row.fair_book_count == 3
    assert row.fee_model == "maker" and row.fee_effective_cents == 1
    assert row.event_ticker == EVENT and engine._executor is None

    # Bridge reads a declaration: this test compares it to a temporary durable row;
    # it does not upgrade the bridge's evidence to live/ledger-certified status.
    db = tmp_path / "fictional.sqlite3"
    bank.init_bank(db, initial_capital_usd="100.00")
    first = bridge.apply_m5_fill(db, declared_payload(row), now=NOW)
    again = bridge.apply_m5_fill(db, declared_payload(row), now=NOW)
    assert first["reservation"] == again["reservation"]
    assert first["evidence_state"] == "DECLARED_FILL_NOT_LEDGER_VERIFIED"
    collateral = price if side == "buy" else 100 - price
    assert Decimal(first["reservation"]["reserved_usd"]) == Decimal(collateral + 1) / 100
    restarted = make_engine(client)
    restarted._rehidratar_inventory()
    assert restarted._inventory.net(TICKER) == engine._inventory.net(TICKER)
    assert restarted._inventory.net(TICKER) == (1 if side == "buy" else -1)
    assert len(durable_fills()) == 1
    gate = tablero(str(tmp_path / "shadow.sqlite3"), experiment_id=engine._experiment_id)
    assert "PASA F1-v2" not in gate
    assert "bloqueados" in gate  # a synthetic fill is not evidence of economic readiness


@pytest.mark.parametrize("result,payout", [("yes", 300), ("no", 0)])
def test_actual_m2_signal_partial_fill_and_fictional_settlement(tmp_path, result, payout):
    event, odds = inputs()
    signals, _, _ = detect(event, [odds])
    (signal,) = signals
    db = tmp_path / "fictional.sqlite3"
    bank.init_bank(db, initial_capital_usd="100.00")
    raw = {
        "schema_version": paper.SCHEMA_VERSION_SIGNAL,
        "signal_id": "m2-chain",
        "origin": "M2",
        "cycle_id": "offline-chain",
        "ticker": signal.market_ticker,
        "side": signal.kalshi_side.lower(),
        "limit_price_cents": signal.kalshi_price_cents,
        "count": 5,
        "signal_at": NOW.isoformat(),
    }
    book = {
        "ticker": TICKER,
        "observed_at": NOW.isoformat(),
        "yes_bids": [[50, 1]],
        "no_bids": [[45, 3]],
    }
    fill = paper.process_signal(db, raw, book)
    assert fill["status"] == "PARTIAL_FILL" and fill["filled_count"] == 3
    assert fill["cost_usd"] == "1.65" and fill["fee_usd"] == "0.06"
    paper.settle_market(db, ticker=TICKER, market_result=result)
    snapshot = bank.get_snapshot(db)
    assert Decimal(snapshot["current_capital_usd"]) == Decimal(10000 + payout - 171) / 100
    assert snapshot["reserved_usd"] == "0.00"
    paper.settle_market(db, ticker=TICKER, market_result=result)
    assert bank.get_snapshot(db) == snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["stale", "missing_time", "started", "few_books", "identity", "ambiguous"]
)
async def test_invalid_m2_evidence_never_creates_m5_quotes(case):
    event, odds = inputs()
    if case in {"stale", "missing_time"}:
        update = NOW - timedelta(minutes=16) if case == "stale" else None
        odds = replace(
            odds, bookmakers=tuple(replace(b, last_update=update) for b in odds.bookmakers)
        )
    elif case == "started":
        odds = replace(odds, commence_time=NOW)
    elif case == "few_books":
        odds = replace(odds, bookmakers=odds.bookmakers[:2])
    elif case == "identity":
        event = replace(
            event,
            outcomes=tuple(
                replace(q, outcome_name=event.outcomes[1 - i].outcome_name)
                for i, q in enumerate(event.outcomes)
            ),
        )
    candidates = [odds, replace(odds, id="ambiguous-event")] if case == "ambiguous" else [odds]
    signals, fairs, _ = detect(event, candidates)
    assert signals == [] and fairs == {}
    client = MemoryReadClient()
    client.books[TICKER] = top()
    engine = make_engine(client)
    await engine._tick()
    assert engine._live_quotes == {} and durable_fills() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("depth,touch", [("0.33", False), ("1.00", True)])
async def test_m5_does_not_invent_a_fill_from_touch_or_fractional_depth(depth, touch):
    event, odds = inputs()
    detect(event, [odds])
    client = MemoryReadClient()
    client.books[TICKER] = top()
    engine = make_engine(client)
    await engine._tick()
    bid = engine._live_quotes[TICKER].bid_cents
    client.books[TICKER] = top(ask=bid if touch else bid - 1, depth=depth)
    await engine._tick()
    assert durable_fills() == []


@pytest.mark.asyncio
async def test_unsupported_maker_fee_blocks_even_with_valid_m2_fair():
    event, odds = inputs()
    _, fairs, _ = detect(event, [odds])
    assert fairs
    client = MemoryReadClient(fee_type="quadratic")
    client.books[TICKER] = top()
    engine = make_engine(client)
    await engine._tick()
    assert engine._live_quotes == {} and durable_fills() == []


@pytest.mark.asyncio
async def test_expired_fair_does_not_create_new_quotes(isolation):
    event, odds = inputs()
    detect(event, [odds])
    isolation.current = NOW + timedelta(minutes=11)
    client = MemoryReadClient()
    client.books[TICKER] = top()
    engine = make_engine(client)
    await engine._tick()
    assert engine._live_quotes == {} and durable_fills() == []


@pytest.mark.asyncio
async def test_persist_failure_never_increments_inventory(tmp_path):
    event, odds = inputs()
    detect(event, [odds])
    client = MemoryReadClient()
    client.books[TICKER] = top()
    engine = make_engine(client)
    await engine._tick()
    bid = engine._live_quotes[TICKER].bid_cents
    with sqlite3.connect(tmp_path / "shadow.sqlite3") as con:
        con.execute("""CREATE TRIGGER fail_fill BEFORE INSERT ON mm_shadow_fills
                       BEGIN SELECT RAISE(ABORT, 'injected disk failure'); END""")
    client.books[TICKER] = top(ask=bid - 1)
    with pytest.raises(m5_module.Motor5DataIntegrityError):
        await engine._tick()
    assert engine._inventory.net(TICKER) == 0 and durable_fills() == []
