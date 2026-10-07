"""MTM exacto y cobertura completa; sólo SQLite temporal y marks sintéticos."""

from datetime import UTC, datetime, timedelta
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, create_engine

import src.risk.manager as manager
from src.monitoring.health import BotState
from src.risk.manager import RiskManager
from src.storage.models import Trade

NOW = datetime(2026, 10, 7, 16, 0)


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.replace(tzinfo=tz)


@pytest.fixture
def risk(tmp_path, monkeypatch):
    database = create_engine(f"sqlite:///{tmp_path / 'mtm.db'}")
    SQLModel.metadata.create_all(database)
    monkeypatch.setattr(manager, "get_session", lambda: Session(database))
    monkeypatch.setattr(manager, "datetime", FrozenDatetime)
    config = SimpleNamespace(
        UNREALIZED_STOP_ENABLED=True,
        UNREALIZED_MARK_TTL_SEC=60,
        MAX_UNREALIZED_LOSS_PCT=10,
        MAX_UNREALIZED_LOSS_FLOOR_USD=0,
    )
    monkeypatch.setattr(manager, "get_settings", lambda: config)
    monkeypatch.setattr(RiskManager, "_get_effective_capital_usd", lambda self: 200)
    monkeypatch.setattr(RiskManager, "_marks", {})
    monkeypatch.setattr(RiskManager, "_unrealized_alert_date", None)
    monkeypatch.setattr(BotState, "is_paused", False)
    result = RiskManager()
    yield result
    database.dispose()


def insert(key="one", **changes):
    data = {
        "client_order_id": key,
        "ticker": "T",
        "side": "yes",
        "action": "buy",
        "count": 100,
        "filled_count": 100,
        "price_cents": 80,
        "fees_cents": 0,
        "strategy": "motor_5_mm",
        "status": "filled",
        "placed_at": NOW - timedelta(hours=1),
        "filled_at": NOW - timedelta(hours=1),
    }
    data.update(changes)
    row = Trade(**data)
    with manager.get_session() as session:
        session.add(row)
        session.commit()


def mark(bid=10, ask=11, *, ticker="T", side="yes", **kwargs):
    RiskManager.record_mark(
        ticker,
        side,
        bid,
        ask_cents=ask,
        observed_at=NOW,
        fee_multiplier=kwargs.pop("fee_multiplier", 1),
        **kwargs,
    )


@pytest.mark.parametrize(
    "side,action,entry,bid,ask",
    [
        ("yes", "buy", 80, 10, 11),
        ("yes", "sell", 20, 89, 90),
        ("no", "buy", 80, 89, 90),
        ("no", "sell", 20, 10, 11),
    ],
)
def test_both_sides_and_directions_include_executed_loss_and_close_fee(
    risk, side, action, entry, bid, ask
):
    insert(side=side, action=action, price_cents=entry, fees_cents=7)
    mark(bid, ask)
    assert risk._unrealized_snapshot()["pnl_cents"] == -7070  # -7000 -7 -63
    assert "latente" in risk._check_unrealized_stop()
    assert BotState.is_paused is False


@pytest.mark.parametrize("status", ["pending", "filled", "cancelled", "error"])
def test_explicit_partial_fill_is_valued_in_every_unsettled_status(risk, status):
    insert(status=status, count=100, filled_count=2, fees_cents=1)
    mark()
    snapshot = risk._unrealized_snapshot()
    assert snapshot["net_contracts"] == {"T": Fraction(2)}
    assert snapshot["pnl_cents"] == -143  # -140 -entry1 -close2


def test_fractional_partial_and_subcent_fees_never_round_to_zero(risk):
    insert(
        count=10,
        filled_count=None,
        filled_count_fp="0.25",
        status="pending",
        fill_cost_usd_fp="0.200000",
        fees_usd_fp="0.001000",
        fees_cents=0,
    )
    mark()
    snapshot = risk._unrealized_snapshot()
    assert snapshot["net_contracts"]["T"] == Fraction(1, 4)
    assert snapshot["pnl_cents"] == Fraction(-93, 5)  # -18.6 cents, includes 1c close
    risk.settings.MAX_UNREALIZED_LOSS_PCT = 0
    risk.settings.MAX_UNREALIZED_LOSS_FLOOR_USD = 0.186
    assert "$-0.19" in risk._check_unrealized_stop()
    risk.settings.MAX_UNREALIZED_LOSS_FLOOR_USD = 0.187
    assert risk._check_unrealized_stop() is None


def test_six_decimal_costs_are_preserved(risk):
    insert(
        count=1,
        filled_count=None,
        filled_count_fp="0.01",
        fill_cost_usd_fp="0.008001",
        fees_usd_fp="0.000001",
        fees_cents=0,
    )
    mark()
    assert risk._unrealized_snapshot()["pnl_cents"] == Fraction(-17002, 10000)


def test_confirmed_zero_fill_needs_no_mark_or_fee(risk):
    insert(status="pending", filled_count=0, fees_cents=None)
    assert risk._unrealized_snapshot()["pnl_cents"] == 0
    assert risk._check_unrealized_stop() is None


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"filled_count": None}, "QUANTITY_UNKNOWN"),
        ({"filled_count": 101}, "fill excede"),
        ({"filled_count": 1, "filled_count_fp": "0.25"}, "contradictoria"),
        ({"filled_count": None, "filled_count_fp": "NaN"}, "inválida"),
        ({"fees_cents": None}, "MISSING_ENTRY_FEES"),
        ({"fees_cents": -1}, "INVALID_ENTRY_FEES"),
        ({"fees_usd_fp": "0.019", "fees_cents": 0}, "CONTRADICTORY_ENTRY_FEES"),
        ({"fill_cost_usd_fp": "100.01"}, "INVALID_ENTRY_COST"),
        ({"fill_cost_usd_fp": "NaN"}, "no representable"),
        ({"fees_usd_fp": "0.0000001"}, "no representable"),
        ({"action": "unknown"}, "INVALID_DIRECTION"),
    ],
)
def test_unknown_or_contradictory_execution_blocks_additional_risk(risk, changes, reason):
    insert(**changes)
    mark()
    assert reason in risk._check_unrealized_stop()
    assert risk._unrealized_snapshot()["pnl_cents"] is None


def test_observed_fill_price_overrides_limit(risk):
    insert(fill_price_cents=20, price_cents=80)
    mark(30, 31)
    assert risk._unrealized_snapshot()["pnl_cents"] == 853  # gain1000 -close147
    assert risk._unrealized_snapshot()["limit_price_rows"] == 0


def test_aggregate_cost_has_priority_over_rounded_fill_price(risk):
    insert(fill_price_cents=80, fill_cost_usd_fp="20.250000")
    mark(30, 31)
    assert risk._unrealized_snapshot()["pnl_cents"] == 828


def test_unobserved_price_is_exposed_as_conservative_limit_bound(risk):
    insert()
    mark()
    assert risk._unrealized_snapshot()["limit_price_rows"] == 1


def test_close_fee_uses_verified_multiplier_once_for_actual_net_quantity(risk):
    insert(price_cents=40)
    mark(50, 51, fee_multiplier=2)
    assert risk._unrealized_snapshot()["pnl_cents"] == 650  # 1000 -350


@pytest.mark.parametrize("missing", ["bid", "fee", "mark"])
def test_incomplete_mark_is_not_silently_skipped(risk, missing):
    insert()
    if missing == "bid":
        mark(None, 11)
    elif missing == "fee":
        mark(fee_multiplier=None)
    assert "valoración completa" in risk._check_unrealized_stop()


def test_sell_requires_ask_not_bid(risk):
    insert(action="sell", price_cents=20)
    mark(89, None)
    assert "MISSING_CLOSE_SIDE" in risk._check_unrealized_stop()
    mark(None, 90)
    assert risk._unrealized_snapshot()["pnl_cents"] == -7063


def test_no_bid_can_price_yes_short_without_inventing_yes_ask(risk):
    insert(action="sell", price_cents=20)
    mark(10, None, side="no")
    assert risk._unrealized_snapshot()["pnl_cents"] == -7063


@pytest.mark.parametrize("age", [61, -1])
def test_stale_or_future_mark_blocks(risk, age):
    insert()
    RiskManager.record_mark(
        "T",
        "yes",
        10,
        ask_cents=11,
        fee_multiplier=1,
        observed_at=(NOW - timedelta(seconds=age)).replace(tzinfo=UTC),
    )
    assert "valoración completa" in risk._check_unrealized_stop()


def test_mark_exactly_at_ttl_is_usable_and_newer_observation_cannot_regress(risk):
    insert()
    mark()
    old = NOW - timedelta(seconds=60)
    RiskManager.record_mark("T", "yes", 50, fee_multiplier=1, observed_at=old)
    assert RiskManager._marks[("T", "yes")].bid_cents == 10
    RiskManager._marks.clear()
    RiskManager.record_mark("T", "yes", 10, fee_multiplier=1, observed_at=old)
    assert risk._unrealized_snapshot()["pnl_cents"] == -7063


@pytest.mark.parametrize(
    "bid,ask,multiplier",
    [(None, None, 1), (True, 11, 1), (20, 10, 1), (0, 11, 1), (10, 11, "NaN"), (10, 11, 0)],
)
def test_invalid_or_empty_book_invalidates_previous_mark(risk, bid, ask, multiplier):
    insert()
    mark()
    mark(bid, ask, fee_multiplier=multiplier)
    assert ("T", "yes") not in RiskManager._marks
    assert "valoración completa" in risk._check_unrealized_stop()


def test_binary_hedge_has_locked_pnl_without_liquidating_covered_legs(risk):
    insert("yes", price_cents=60, fees_cents=5)
    insert("no", side="no", price_cents=35, fees_cents=5)
    assert risk._unrealized_snapshot()["pnl_cents"] == 490
    assert risk._unrealized_snapshot()["covered"] == 0
    assert risk._check_unrealized_stop() is None


def test_partially_hedged_binary_marks_only_exact_residual(risk):
    insert("yes", price_cents=60, fees_cents=2)
    insert("no", side="no", count=80, filled_count=80, price_cents=35, fees_cents=3)
    mark()
    snap = risk._unrealized_snapshot()
    assert snap["net_contracts"] == {"T": Fraction(20)}
    assert snap["pnl_cents"] == -618


def test_buy_and_sell_same_contract_do_not_pay_close_fees_twice(risk):
    insert("buy", price_cents=40, fees_cents=5)
    insert("sell", action="sell", price_cents=60, fees_cents=5)
    assert risk._unrealized_snapshot()["pnl_cents"] == 1990
    assert risk._check_unrealized_stop() is None


def test_locked_loss_is_not_hidden_by_hedge_tag(risk):
    insert("yes", price_cents=80, fees_cents=1, notes="arb_id=any")
    insert("no", side="no", price_cents=80, fees_cents=1, notes="arb_id=any")
    assert risk._unrealized_snapshot()["pnl_cents"] == -6002
    assert "latente" in risk._check_unrealized_stop()


def test_orphan_with_arb_id_requires_a_mark(risk):
    insert(notes="arb_id=00000000-0000-0000-0000-000000000000")
    assert "MISSING_MARK" in risk._check_unrealized_stop()
    mark()
    assert "latente" in risk._check_unrealized_stop()


def test_different_tickers_cannot_cover_each_other(risk):
    insert("buy", ticker="A")
    insert("sell", ticker="B", action="sell")
    assert "MISSING_MARK" in risk._check_unrealized_stop()


def test_settled_and_clv_closed_positions_are_not_counted_again(risk):
    insert("settled", status="settled", fees_cents=None)
    insert("closed", closed_by_clv=True, fees_cents=None)
    assert risk._unrealized_snapshot()["pnl_cents"] == 0


def test_db_failure_is_soft_block_not_kill_switch(risk):
    with patch.object(manager, "get_session", side_effect=RuntimeError("unavailable")):
        assert "ledger falló" in risk._check_unrealized_stop()
    assert BotState.is_paused is False


def test_disabled_gate_preserves_default_without_reading_database(risk):
    risk.settings.UNREALIZED_STOP_ENABLED = False
    with patch.object(manager, "get_session", side_effect=AssertionError("must not read")):
        assert risk._check_unrealized_stop() is None


def test_public_quote_mtm_gate_matches_admission_gate(risk):
    insert()
    assert risk.unrealized_entry_block_reason() == risk._check_unrealized_stop()
    mark()
    assert risk.unrealized_entry_block_reason() == risk._check_unrealized_stop()


def test_public_quote_cash_gate_never_uses_demo_fallback(risk):
    with patch.object(risk, "_confirmed_quote_capital_usd", return_value=None):
        assert risk.can_quote_new_positions() is False
    with patch.object(risk, "_confirmed_quote_capital_usd", return_value=200):
        assert risk.can_quote_new_positions() is True
