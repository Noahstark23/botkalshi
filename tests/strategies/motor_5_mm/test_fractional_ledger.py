"""Contrato de cantidades M5: SQLite temporal y respuestas de exchange falsas."""

from datetime import UTC, datetime, timedelta
from fractions import Fraction

import pytest
from sqlmodel import select

from src.analytics.analyst_loop import aggregate_mm
from src.risk.exposure import UnknownExposureError, exposure_count, open_risk_cents
from src.storage.models import MMFunnelSnapshot, MMQuote, Trade, get_session
from src.strategies.motor_5_mm.engine import Motor5DataIntegrityError, Motor5Engine
from src.strategies.motor_5_mm.incidents import incident_status
from src.strategies.motor_5_mm.ledger import confirmed_fill, record_fill
from src.strategies.motor_5_mm.order_state import COUNT_QUARANTINE_NOTE
from src.strategies.motor_5_mm.quoter import QuoteSet, compute_quote
from src.strategies.motor_5_mm.reconciler import MMReconciler
from src.strategies.motor_rest_arb.settlement import MarketResolution, SettlementPoller


def _row(*, action="buy", status="pending", count=10):
    return Trade(
        client_order_id="m5mm-fraction",
        kalshi_order_id="fraction-order",
        ticker="KXMLBGAME-TEST-YES",
        side="yes",
        action=action,
        count=count,
        price_cents=47,
        strategy="motor_5_mm",
        status=status,
    )


def _insert(*, count="1.23", status="filled", action="buy"):
    row = _row(status=status, action=action)
    record_fill(row, Fraction(count))
    with get_session() as session:
        session.add(row)
        session.commit()


def _read():
    with get_session() as session:
        return session.exec(select(Trade)).one()


def _update(count, *, status="pending"):
    with get_session() as session:
        row = session.exec(select(Trade)).one()
        record_fill(row, Fraction(count))
        row.status = status
        session.add(row)
        session.commit()


@pytest.mark.parametrize("action, risk", [("buy", 58), ("sell", 66)])
def test_fractional_terminal_fill_survives_sqlite_and_reserves_rounded_up_risk(action, risk):
    _insert(action=action)
    row = _read()
    assert row.filled_count is None
    assert row.filled_count_fp == "1.23"
    assert confirmed_fill(row) == exposure_count(row) == Fraction(123, 100)
    assert open_risk_cents(row) == risk


def test_pending_fractional_fill_keeps_entire_intent_reserved():
    _insert(status="pending")
    assert open_risk_cents(_read()) == 470


def test_audit_and_fee_repair_read_fractional_count_without_full_order_fallback():
    from scripts.backfill_trade_fees import _fee_for
    from src.analytics.loss_audit import real_entry_fee_cents

    _insert(count="0.23")
    assert _fee_for(_read()) == real_entry_fee_cents(_read()) == 1


@pytest.mark.parametrize(
    "fp,legacy", [("0.001", None), ("NaN", None), ("1.23", 1), ("11.00", None)]
)
def test_bad_persisted_count_never_authorizes_headroom(fp, legacy):
    row = _row(status="filled")
    row.filled_count_fp, row.filled_count = fp, legacy
    with pytest.raises((UnknownExposureError, ValueError)):
        open_risk_cents(row)


@pytest.mark.parametrize("action, sign", [("buy", 1), ("sell", -1)])
def test_fractional_progress_restart_and_settlement_do_not_duplicate_inventory(action, sign):
    _insert(count="0.25", status="pending", action=action)
    engine = Motor5Engine(trading_enabled=True)
    assert engine._apply_settled_fills() == 1
    assert engine._inventory.net("KXMLBGAME-TEST-YES") == sign * Fraction(1, 4)
    assert engine._apply_settled_fills() == 0
    _update("0.75")
    assert engine._apply_settled_fills() == 1
    assert engine._inventory.net("KXMLBGAME-TEST-YES") == sign * Fraction(3, 4)
    _update("1.23", status="filled")
    assert engine._apply_settled_fills() == 1
    assert engine._inventory.net("KXMLBGAME-TEST-YES") == sign * Fraction(123, 100)
    restarted = Motor5Engine(trading_enabled=True)
    assert restarted._apply_settled_fills() == 1
    assert restarted._apply_settled_fills() == 0
    assert restarted._inventory.positions == engine._inventory.positions
    _update("1.23", status="settled")
    assert restarted._apply_settled_fills() == 0
    assert restarted._inventory.total_abs_contracts() == 0
    assert restarted._live_fill_state == {}


@pytest.mark.parametrize("mutation", ["regression", "deleted", "identity", "cancelled"])
def test_inventory_keeps_prior_snapshot_and_blocks_on_corrupt_history(mutation):
    _insert(count="1.23", status="pending")
    engine = Motor5Engine(trading_enabled=True)
    engine._apply_settled_fills()
    with get_session() as session:
        row = session.exec(select(Trade)).one()
        if mutation == "deleted":
            session.delete(row)
        else:
            if mutation == "regression":
                row.filled_count_fp = "0.23"
            elif mutation == "identity":
                row.action = "sell"
            else:
                row.status = "cancelled"
            session.add(row)
        session.commit()
    with pytest.raises(Motor5DataIntegrityError):
        engine._apply_settled_fills()
    assert engine._inventory.net("KXMLBGAME-TEST-YES") == Fraction(123, 100)
    assert incident_status()[0]


@pytest.mark.asyncio
async def test_fractional_reconcile_cancel_race_and_restart_preserve_highest_fill():
    _insert(count="0.00", status="pending")

    class Client:
        async def get_all_orders(self, **_):
            return [
                {
                    "order_id": "fraction-order",
                    "client_order_id": "m5mm-fraction",
                    "ticker": "KXMLBGAME-TEST-YES",
                    "action": "buy",
                    "side": "yes",
                    "status": "resting",
                    "fill_count_fp": "1.23",
                }
            ]

        async def cancel_order(self, _):
            return {
                "order": {
                    "order_id": "fraction-order",
                    "client_order_id": "m5mm-fraction",
                    "status": "canceled",
                    "fill_count_fp": "1.37",
                    "remaining_count_fp": "0.00",
                    "initial_count_fp": "10.00",
                }
            }

    report = await MMReconciler(Client()).reconcile(live_coids={"m5mm-fraction"})
    assert report.complete and report.still_resting == 1
    assert _read().filled_count_fp == "1.23"
    assert COUNT_QUARANTINE_NOTE not in (_read().notes or "")
    engine = Motor5Engine(trading_enabled=True)
    assert engine._apply_settled_fills() == 1
    assert engine._inventory.net("KXMLBGAME-TEST-YES") == Fraction(123, 100)
    report = await MMReconciler(Client()).reconcile(live_coids=set())
    assert report.complete and report.resolved_filled == 1
    assert _read().filled_count_fp == "1.37"
    restarted = Motor5Engine(trading_enabled=True)
    restarted._apply_settled_fills()
    assert restarted._inventory.net("KXMLBGAME-TEST-YES") == Fraction(137, 100)


@pytest.mark.parametrize("inventory", [Fraction(99, 100), Fraction(-99, 100), Fraction(1, 2)])
def test_whole_contract_quotes_cannot_overrun_fractional_inventory_headroom(inventory):
    quote, _ = compute_quote(
        "T",
        0.5,
        half_spread_cents=3,
        size_contracts=10,
        inventory_contracts=inventory,
        max_inventory_contracts=1,
        best_yes_bid=40,
        best_yes_ask=60,
    )
    assert quote is not None
    if quote.bid_cents is not None:
        assert inventory + quote.size <= 1
    if quote.ask_cents is not None:
        assert inventory - quote.size >= -1


@pytest.mark.asyncio
@pytest.mark.parametrize("result,exact,cents", [("yes", "0.6519", 65), ("no", "-0.5781", -58)])
async def test_settlement_preserves_exact_fractional_pnl_and_conservative_legacy(
    result, exact, cents
):
    _insert()
    with get_session() as session:
        row = session.exec(select(Trade)).one()
        row.fees_cents = 0  # dato explícito del fixture, aislar la cantidad del modelo de fee
        session.add(row)
        session.commit()

    class Source:
        async def get_resolution(self, ticker):
            return MarketResolution(ticker, result)

    assert await SettlementPoller(Source()).settle_once() == 1
    row = _read()
    assert row.status == "settled" and row.filled_count_fp == "1.23"
    assert row.pnl_usd_fp == exact and row.pnl_cents == cents
    assert await SettlementPoller(Source()).settle_once() == 0


def test_fractional_inventory_is_persisted_exactly_and_reported_without_legacy_rounding():
    _insert(count="0.23")
    engine = Motor5Engine(trading_enabled=True)
    engine._apply_settled_fills()
    engine._persist_quote(
        QuoteSet("KXMLBGAME-TEST-YES", 0.5, 47, 53, 1), fv_age_sec=0, yes_bid=40, yes_ask=60
    )
    counters = dict.fromkeys(
        (
            "quoted",
            "skip_no_book",
            "skip_unprofitable",
            "skip_degenerate",
            "skip_fair_range",
            "skip_fee_policy",
            "skip_pregame",
            "fills",
        ),
        0,
    )
    engine._persist_snapshot(0, counters, 0)
    with get_session() as session:
        quote = session.exec(select(MMQuote)).one()
        snapshot = session.exec(select(MMFunnelSnapshot)).one()
        assert quote.inventory_fp == snapshot.inventory_abs_fp == "0.23"
        assert quote.inventory == snapshot.inventory_abs == 1  # cota legacy, no valor exacto
        assert (
            aggregate_mm(session, datetime.now(UTC) - timedelta(hours=1)).inventory_abs_last == 0.23
        )


def test_terminal_fill_edit_does_not_replace_prior_inventory():
    _insert(count="0.23")
    engine = Motor5Engine(trading_enabled=True)
    engine._apply_settled_fills()
    _update("0.75", status="filled")  # edición indebida de un fill terminal
    with pytest.raises(Motor5DataIntegrityError):
        engine._apply_settled_fills()
    assert engine._inventory.net("KXMLBGAME-TEST-YES") == Fraction(23, 100)


def test_invalid_second_row_does_not_publish_half_an_inventory():
    _insert()
    with get_session() as session:
        bad = _row(status="filled")
        bad.client_order_id = "m5mm-bad"
        bad.filled_count_fp = "11.00"
        session.add(bad)
        session.commit()
    engine = Motor5Engine(trading_enabled=True)
    with pytest.raises(Motor5DataIntegrityError):
        engine._apply_settled_fills()
    assert engine._inventory.total_abs_contracts() == 0


@pytest.mark.asyncio
async def test_full_tick_quotes_using_partial_fraction_before_new_orders():
    from src.strategies.fair_value_book import FairValueBook

    _insert(count="0.00", status="pending")

    class Client:
        async def get_all_orders(self, **_):
            return [
                {
                    "order_id": "fraction-order",
                    "client_order_id": "m5mm-fraction",
                    "ticker": "KXMLBGAME-TEST-YES",
                    "action": "buy",
                    "side": "yes",
                    "status": "resting",
                    "fill_count_fp": "0.99",
                }
            ]

        async def get_orderbook(self, _):
            return {"orderbook": {"yes": [[40, 100]], "no": [[40, 100]]}}

    class Executor:
        corrupted = set()

        def __init__(self):
            self.quotes = []

        def managed_coids(self):
            return {"m5mm-fraction"}

        def forget_confirmed(self, _):
            pass

        async def sync_quotes(self, quote):
            self.quotes.append(quote)
            return "synced"

    client, executor = Client(), Executor()
    engine = Motor5Engine(trading_enabled=True, max_inventory_contracts=1)
    engine._client, engine._executor = client, executor
    engine._reconciler = MMReconciler(client)
    FairValueBook.publish({"KXMLBGAME-TEST-YES": 0.5})
    await engine._tick()
    assert engine._inventory.net("KXMLBGAME-TEST-YES") == Fraction(99, 100)
    assert len(executor.quotes) == 1
    assert executor.quotes[0].bid_cents is None
    assert executor.quotes[0].ask_cents is not None
    assert executor.quotes[0].size == 1


@pytest.mark.asyncio
async def test_new_fractional_support_does_not_clear_old_quarantine():
    _insert(count="0.50", status="pending")
    with get_session() as session:
        row = session.exec(select(Trade)).one()
        row.notes = COUNT_QUARANTINE_NOTE
        session.add(row)
        session.commit()

    class Client:
        async def get_all_orders(self, **_):
            return [
                {
                    "order_id": "fraction-order",
                    "client_order_id": "m5mm-fraction",
                    "ticker": "KXMLBGAME-TEST-YES",
                    "action": "buy",
                    "side": "yes",
                    "status": "canceled",
                    "fill_count_fp": "0.50",
                }
            ]

    report = await MMReconciler(Client()).reconcile(live_coids=set())
    assert not report.complete
    assert _read().status == "pending"
    assert incident_status()[0]


def test_ledger_row_budget_blocks_instead_of_truncating_exposure():
    _insert()
    engine = Motor5Engine(trading_enabled=True)
    engine.MAX_LIVE_LEDGER_ROWS = 0
    with pytest.raises(Motor5DataIntegrityError):
        engine._apply_settled_fills()
    assert engine._inventory.total_abs_contracts() == 0
