"""Soporte Demo sólo de lectura: tarifas, trazabilidad y reinicio sintético."""

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlmodel import SQLModel, select

import src.storage.models as models
import src.strategies.motor_5_mm.engine as engine_module
from src.strategies.fair_value_book import FairProvenance, FairValueBook
from src.strategies.motor_5_mm.engine import Motor5DataIntegrityError, Motor5Engine
from src.strategies.motor_5_mm.fee_policy import (
    SeriesFeePolicy,
    UnsupportedSeriesFeeError,
)

NOW = datetime(2026, 10, 10, 17, tzinfo=UTC)
EVENT = "KXNFLGAME-26OCT11PHIJAC"
TICKER = EVENT + "-JAC"
SIMULATION_ROOT = str(Path(__file__).resolve().parents[3] / "infra/digitalocean-shadow")


class ReadOnlyFixture:
    """Sin red, credenciales, place/cancel ni métodos genéricos de HTTP."""

    def __init__(self, series="KXNFLGAME", event=EVENT):
        self.series = {"ticker": series, "fee_type": "quadratic", "fee_multiplier": 1}
        self.event = {"event_ticker": event, "series_ticker": series}
        self.book = {"yes_dollars": [["0.50", "1.25"]], "no_dollars": [["0.30", "1.25"]]}
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get_series(self, ticker):
        self.calls.append(("series", ticker))
        return {"series": dict(self.series)}

    async def get_event(self, ticker):
        self.calls.append(("event", ticker))
        return {"event": dict(self.event)}

    async def get_orderbook(self, ticker):
        self.calls.append(("book", ticker))
        return {"orderbook_fp": self.book}


def make_engine(client, **changes):
    kwargs = {
        "client_factory": lambda: client,
        "exchange_environment": "demo",
        "demo_readonly_validation": True,
        "demo_football_fee_validation": True,
        "fees_as_maker": True,
        "trading_enabled": False,
        "series_csv": "KXNFLGAME",
        "require_pregame": True,
        "fair_min_books": 3,
        "fair_max_book_age_min": 15,
        "fair_odds_regions": "us",
        "fair_sport_keys_config": "americanfootball_nfl",
        "fair_cache_ttl_sec": 60,
        "experiment_label": "OFFLINE-SYNTHETIC-DEMO",
    }
    kwargs.update(changes)
    engine = Motor5Engine(**kwargs)
    engine._client = client
    engine._fee_policy = SeriesFeePolicy(
        client, demo_readonly_validation=True, series_ttl_sec=0, event_ttl_sec=0
    )
    return engine


def publish_synthetic_fair():
    FairValueBook.publish(
        {TICKER: 0.60},
        now=NOW,
        commence_times={TICKER: NOW + timedelta(days=1)},
        event_tickers={TICKER: EVENT},
        provenances={
            TICKER: FairProvenance(
                event_ticker=EVENT,
                odds_event_id="SYNTHETIC-NOT-OBSERVED",
                sport_key="americanfootball_nfl",
                bookmaker_keys=("fixture-a", "fixture-b", "fixture-c"),
                oldest_book_update=NOW,
                newest_book_update=NOW,
                min_books=3,
                max_book_age_min=15,
            )
        },
    )


@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    db_engine = create_engine(f"sqlite:///{tmp_path / 'demo-test.sqlite3'}")
    SQLModel.metadata.create_all(db_engine)
    monkeypatch.setattr(models, "_engine", db_engine)
    FairValueBook.clear()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(engine_module, "datetime", Clock)
    yield
    FairValueBook.clear()
    db_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "series,event", [("KXNFLGAME", EVENT), ("KXNCAAFGAME", "KXNCAAFGAME-26OCT10UGAALA")]
)
async def test_demo_quadratic_preserves_source_multiplier_but_charges_zero_maker(series, event):
    client = ReadOnlyFixture(series, event)
    observed = await SeriesFeePolicy(client, demo_readonly_validation=True).observe(
        event + "-X", event_ticker=event
    )
    assert observed.multiplier == 1 and observed.base_multiplier == 1
    assert observed.maker_multiplier == 0
    assert observed.fee_type == "quadratic"
    # El default operativo sigue rechazando estas mismas respuestas Demo.
    with pytest.raises(UnsupportedSeriesFeeError):
        await SeriesFeePolicy(client).observe(event + "-X", event_ticker=event)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fee_type,expected", [("quadratic", 0), ("quadratic_with_maker_fees", Fraction(1, 2))]
)
async def test_demo_event_override_uses_its_effective_maker_schedule(fee_type, expected):
    client = ReadOnlyFixture()
    client.event.update(fee_type_override=fee_type, fee_multiplier_override="0.5")
    observed = await SeriesFeePolicy(client, demo_readonly_validation=True).observe(
        TICKER, event_ticker=EVENT
    )
    assert observed.source == "event_override" and observed.multiplier == Fraction(1, 2)
    assert observed.maker_multiplier == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target,changes",
    [
        ("series", {"ticker": "KXWRONG"}),
        ("series", {"fee_type": "quadratic_with_maker_fees"}),
        ("series", {"fee_multiplier": 0.5}),
        ("series", {"fee_multiplier": "NaN"}),
        ("event", {"fee_type_override": "flat", "fee_multiplier_override": 1}),
        ("event", {"fee_type_override": "quadratic"}),
        ("event", {"event_ticker": "KXNFLGAME-WRONG"}),
    ],
)
async def test_demo_unexpected_identity_base_or_partial_override_still_rejects(target, changes):
    client = ReadOnlyFixture()
    getattr(client, target).update(changes)
    with pytest.raises(UnsupportedSeriesFeeError):
        await SeriesFeePolicy(client, demo_readonly_validation=True).observe(
            TICKER, event_ticker=EVENT
        )


@pytest.mark.asyncio
async def test_demo_new_or_unreviewed_series_is_rejected_before_read():
    client = ReadOnlyFixture("KXMLBGAME", "KXMLBGAME-UNKNOWN")
    with pytest.raises(UnsupportedSeriesFeeError, match="sin aserción"):
        await SeriesFeePolicy(client, demo_readonly_validation=True).observe(
            "KXMLBGAME-X-Y", event_ticker="KXMLBGAME-X"
        )
    assert client.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"trading_enabled": True},
        {"exchange_environment": "production"},
        {"fees_as_maker": False},
        {"series_csv": "*"},
        {"series_csv": "KXMLBGAME"},
        {"demo_readonly_validation": "true"},
        {"demo_readonly_validation": False},
        {"demo_football_fee_validation": "true"},
    ],
)
def test_demo_mode_cannot_enable_orders_or_mix_environment(changes):
    with pytest.raises(ValueError):
        make_engine(ReadOnlyFixture(), **changes)


def test_demo_requires_explicit_client_and_distinct_metric():
    with pytest.raises(ValueError, match="cliente de lectura"):
        Motor5Engine(
            series_csv="KXNFLGAME",
            demo_readonly_validation=True,
            demo_football_fee_validation=True,
            fees_as_maker=True,
        )
    demo = make_engine(ReadOnlyFixture())
    production = Motor5Engine(
        series_csv="KXNFLGAME", fees_as_maker=True, exchange_environment="production"
    )
    assert demo.F1_METRIC_VERSION == "demo-readonly-v2-fee-schedule"
    assert production.F1_METRIC_VERSION == "f1-v2-bbo-depth"
    assert demo._experiment_id != production._experiment_id
    assert demo._executor is None and not demo._trading_enabled


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("maker_override", [False, True])
async def test_quote_synthetic_fill_persistence_and_restart_keep_fee_and_cash(side, maker_override):
    client = ReadOnlyFixture()
    if maker_override:
        client.event.update(
            fee_type_override="quadratic_with_maker_fees", fee_multiplier_override=1
        )
    engine = make_engine(client)
    publish_synthetic_fair()
    await engine._tick()
    quote = engine._live_quotes[TICKER]
    if side == "buy":
        client.book["no_dollars"] = [[str((101 - quote.bid_cents) / 100), "1.25"]]
    else:
        client.book["yes_dollars"] = [[str((quote.ask_cents + 1) / 100), "1.25"]]
        client.book["no_dollars"] = [["0.20", "1.25"]]
    await engine._tick()
    with models.get_session() as s:
        (row,) = list(s.exec(select(models.MMShadowFill)))
    assert row.fee_effective_cents == (1 if maker_override else 0)
    assert row.fee_cents > 0 and row.fee_multiplier == 1
    assert row.exchange_environment == "demo" and row.metric_version == engine.F1_METRIC_VERSION
    expected = asdict(engine._inventory.positions[TICKER])
    restarted = make_engine(client)
    restarted._rehidratar_inventory()
    assert asdict(restarted._inventory.positions[TICKER]) == expected
    assert engine._executor is None and restarted._executor is None
    with models.get_session() as s:
        assert len(list(s.exec(select(models.MMShadowFill)))) == 1

    # Simular otra cohorte en la misma DB de pruebas: su markout no pertenece a Demo.
    with models.get_session() as s:
        other = models.MMShadowFill(
            **{
                **row.model_dump(exclude={"id"}),
                "exchange_environment": "production",
                "metric_version": "f1-v2-bbo-depth",
                "experiment_id": "OTHER-COHORT",
            }
        )
        s.add(other)
        s.commit()
    restarted._rehidratar_markouts()
    assert [r["id"] for r in restarted._markouts_pendientes] == [row.id]


@pytest.mark.asyncio
async def test_fee_change_while_quote_is_exposed_invalidates_before_inferring_fill():
    client = ReadOnlyFixture()
    engine = make_engine(client)
    publish_synthetic_fair()
    await engine._tick()
    assert TICKER in engine._live_quotes
    client.event.update(fee_type_override="quadratic_with_maker_fees", fee_multiplier_override=1)
    await engine._tick()
    assert TICKER not in engine._live_quotes
    with models.get_session() as s:
        assert list(s.exec(select(models.MMShadowFill))) == []


@pytest.mark.asyncio
async def test_startup_wires_demo_fee_policy_without_executor(monkeypatch):
    import asyncio

    client = ReadOnlyFixture()
    engine = make_engine(client)
    publish_synthetic_fair()
    stop = asyncio.Event()
    original = engine._tick

    async def once():
        await original()
        stop.set()

    monkeypatch.setattr(engine, "_tick", once)
    await engine.run(stop)
    assert engine._executor is None and not engine._trading_enabled
    assert any(kind == "event" for kind, _ in client.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("fee_type", None),
        ("fee_effective_cents", 2),
        ("exchange_environment", "production"),
    ],
)
async def test_corrupt_demo_provenance_is_not_rehydrated_as_paid_maker(field, value):
    client = ReadOnlyFixture()
    engine = make_engine(client)
    publish_synthetic_fair()
    await engine._tick()
    quote = engine._live_quotes[TICKER]
    client.book["no_dollars"] = [[str((101 - quote.bid_cents) / 100), "1.25"]]
    await engine._tick()
    with models.get_session() as s:
        row = s.exec(select(models.MMShadowFill)).one()
        setattr(row, field, value)
        s.add(row)
        s.commit()
    with pytest.raises(Motor5DataIntegrityError, match="procedencia"):
        make_engine(client)._rehidratar_inventory()


@pytest.mark.asyncio
async def test_demo_quote_prefunded_in_shared_wallet_then_partial_cancel_settle(tmp_path):
    import sys
    from decimal import Decimal

    sys.path.insert(0, SIMULATION_ROOT)
    import m5_paper_ledger as wallet
    import simulation_bank as bank

    client = ReadOnlyFixture()
    engine = make_engine(client)
    publish_synthetic_fair()
    await engine._tick()
    q = engine._live_quotes[TICKER]
    db = tmp_path / "shared-fictional.sqlite3"
    bank.init_bank(db, initial_capital_usd="100.00")
    quote = {
        "quote_id": "demo-free-maker",
        "ticker": TICKER,
        "created_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        "bid_cents": q.bid_cents,
        "ask_cents": q.ask_cents,
        "count": q.size,
        "fee_type": "quadratic",
        "fee_multiplier": "1",
    }
    book = {
        "ticker": TICKER,
        "observed_at": NOW.isoformat(),
        "yes_bid": 50,
        "yes_ask": 70,
        "bid_depth": "1.25",
        "ask_depth": "1.25",
    }
    admitted = wallet.admit_quote(db, quote, book)
    assert admitted["bank"]["reserved_usd"] == str(Decimal(q.bid_cents + 100 - q.ask_cents) / 100)
    assert all(leg["filled"] == 0 for leg in admitted["legs"])
    # Sólo cruza el bid: exposición parcial de una quote bilateral, todavía reservada.
    at = (NOW + timedelta(seconds=1)).isoformat()
    crossed = {**book, "observed_at": at, "yes_ask": q.bid_cents - 1}
    partial = wallet.observe(db, quote["quote_id"], observation_id="cross", at=at, book=crossed)
    assert sum(leg["filled"] for leg in partial["legs"]) == 1
    assert partial["bank"]["reserved_usd"] == admitted["bank"]["reserved_usd"]
    canceled = wallet.cancel_quote(db, quote["quote_id"], at=at)
    assert Decimal(canceled["bank"]["reserved_usd"]) == Decimal(q.bid_cents) / 100
    settled = wallet.settle_quote(db, quote["quote_id"], yes_payout_cents=50)
    assert settled["pnl_cents"] == 50 - q.bid_cents
    assert settled["bank"]["reserved_usd"] == "0.00"
    repeated = wallet.settle_quote(db, quote["quote_id"], yes_payout_cents=50)
    assert repeated["bank"] == settled["bank"]
