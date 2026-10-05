"""La cartera incompleta nunca aparenta cero riesgo ni ganancias."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.analytics.portfolio_report import collect_portfolio, json_safe

NOW = datetime(2026, 9, 30, 1, tzinfo=UTC)


def client():
    return SimpleNamespace(
        get_balance=AsyncMock(
            return_value={"balance": 4, "portfolio_value": 0, "updated_ts": int(NOW.timestamp())}
        ),
        get_positions=AsyncMock(return_value={"market_positions": [], "cursor": ""}),
        get_orders=AsyncMock(return_value={"orders": [], "cursor": ""}),
        get_fills=AsyncMock(return_value={"fills": [], "cursor": ""}),
        get_settlements=AsyncMock(return_value={"settlements": [], "cursor": ""}),
    )


def position(ticker="TEST-A", quantity="0.42", exposure="0.0420"):
    return {"ticker": ticker, "position_fp": quantity, "market_exposure_dollars": exposure}


async def collect(c, **kwargs):
    return await collect_portfolio(
        c, environment="production", source_fingerprint="f" * 64, now=NOW, **kwargs
    )


async def test_empty_account_is_readable_but_never_authorizes_trading():
    c = client()
    report = await collect(c, subaccount=3)
    assert report["status"] == "OK"
    assert report["balance"]["cash_usd"] == Decimal("0.04")
    assert report["totals"]["nonzero_positions"] == 0
    assert report["pnl_net_usd"] is None
    assert report["authorizes_trading"] is False
    for method in vars(c).values():
        for call in method.await_args_list:
            assert call.kwargs["subaccount"] == 3
    assert c.get_orders.await_args.kwargs["status"] == "resting"
    assert c.get_fills.await_args.kwargs["min_ts"] == int(NOW.timestamp()) - 86400


async def test_fractional_position_on_later_page_and_exact_currency():
    c = client()
    c.get_positions.side_effect = [
        {"market_positions": [position("A", "0", "0")], "cursor": "next"},
        {"market_positions": [position("B", "-0.42", "0.0042")], "cursor": ""},
    ]
    r = await collect(c, limit=1)
    assert r["status"] == "OK"
    assert r["totals"]["nonzero_positions"] == 1
    assert r["totals"]["market_exposure_usd"] == Decimal("0.0042")
    assert c.get_positions.await_args_list[1].kwargs["cursor"] == "next"
    assert json_safe(r)["positions"]["rows"][1]["quantity"] == "-0.42"


@pytest.mark.parametrize(
    "bad", [None, True, "NaN", "Infinity", "bad", "1e99999", "0.0000000000001"]
)
async def test_bad_quantity_is_unknown_not_zero(bad):
    c = client()
    c.get_positions.return_value = {"market_positions": [position(quantity=bad)], "cursor": ""}
    r = await collect(c)
    assert r["status"] == "ATENCION"
    assert r["totals"]["nonzero_positions"] is None
    assert r["totals"]["market_exposure_usd"] is None


@pytest.mark.parametrize(
    "page",
    [
        {"market_positions": []},
        {"market_positions": [], "cursor": None},
        {"cursor": ""},
        {"market_positions": None, "cursor": ""},
        {"market_positions": [None], "cursor": ""},
    ],
)
async def test_missing_page_contract_never_certifies_empty_account(page):
    c = client()
    c.get_positions.return_value = page
    r = await collect(c)
    assert r["status"] == "ATENCION"
    assert r["totals"]["nonzero_positions"] is None


async def test_repeated_cursor_is_bounded_and_does_not_double_count():
    c = client()
    c.get_positions.side_effect = [
        {"market_positions": [position("A")], "cursor": "same"},
        {"market_positions": [position("B")], "cursor": "same"},
    ]
    r = await collect(c, limit=1)
    assert c.get_positions.await_count == 2
    assert r["positions"]["complete"] is False
    assert r["totals"]["market_exposure_usd"] is None


async def test_duplicate_identity_and_page_budget_are_not_silently_accepted():
    c = client()
    c.get_positions.side_effect = [
        {"market_positions": [position()], "cursor": "next"},
        {"market_positions": [position()], "cursor": ""},
    ]
    assert (await collect(c, limit=1))["status"] == "ATENCION"
    c = client()
    c.get_orders.return_value = {"orders": [], "cursor": "next"}
    r = await collect(c, max_pages=1)
    assert c.get_orders.await_count == 1
    assert r["totals"]["resting_orders"] is None


def fill(identifier="f1", count="10.25", fee="0.0175"):
    return {
        "fill_id": identifier,
        "order_id": "o1",
        "ticker": "TEST-A",
        "side": "yes",
        "count_fp": count,
        "yes_price_dollars": "0.5055",
        "fee_cost": fee,
        "created_time": NOW.isoformat(),
    }


async def test_fill_fee_is_total_in_dollars_and_not_multiplied_by_fractional_count():
    c = client()
    c.get_fills.side_effect = [
        {"fills": [fill()], "cursor": "next"},
        {"fills": [fill("f2", "0.10", "0.0003")], "cursor": ""},
    ]
    r = await collect(c, limit=1)
    assert r["status"] == "OK"
    assert r["totals"]["fill_fees_usd"] == Decimal("0.0178")
    assert r["fills"]["rows"][0]["quantity"] == Decimal("10.25")
    assert r["fills"]["rows"][0]["price_usd"] == Decimal("0.5055")
    assert r["pnl_net_usd"] is None


@pytest.mark.parametrize(
    "change",
    [
        {"fee_cost": None},
        {"fee_cost": "NaN"},
        {"count_fp": "0"},
        {"side": "unknown"},
        {"outcome_side": "no"},
        {"yes_price_dollars": "1.01"},
        {"created_time": "2020-01-01T00:00:00Z"},
    ],
)
async def test_invalid_fill_does_not_publish_fee_total(change):
    c = client()
    c.get_fills.return_value = {"fills": [{**fill(), **change}], "cursor": ""}
    r = await collect(c)
    assert r["status"] == "ATENCION"
    assert r["totals"]["fill_fees_usd"] is None


async def test_fractional_resting_order_on_second_page_is_visible():
    c = client()
    row = {
        "order_id": "o1",
        "ticker": "A",
        "status": "resting",
        "side": "no",
        "remaining_count_fp": "0.42",
        "no_price_dollars": "0.5055",
    }
    c.get_orders.side_effect = [{"orders": [], "cursor": "next"}, {"orders": [row], "cursor": ""}]
    r = await collect(c)
    assert r["status"] == "OK"
    assert r["totals"]["resting_orders"] == 1
    assert r["orders"]["rows"][0]["remaining_quantity"] == Decimal("0.42")


async def test_failure_and_balance_change_are_attention_without_exposing_secret():
    c = client()
    c.get_fills.side_effect = RuntimeError("SECRET_TOKEN_BODY")
    r = await collect(c)
    assert r["status"] == "ATENCION"
    assert "SECRET_TOKEN_BODY" not in str(r)
    assert r["totals"]["fill_fees_usd"] is None
    c = client()
    c.get_balance.side_effect = [
        {"balance": 4, "portfolio_value": 0, "updated_ts": int(NOW.timestamp())},
        {"balance": 5, "portfolio_value": 0, "updated_ts": int(NOW.timestamp())},
    ]
    r = await collect(c)
    assert r["status"] == "ATENCION"
    assert r["balance_unchanged"] is False


async def test_timeout_never_returns_healthy():
    import asyncio

    c = client()

    async def slow(**kwargs):
        await asyncio.sleep(1)

    c.get_balance.side_effect = slow
    r = await collect(c, timeout_seconds=0.001)
    assert r["status"] == "ATENCION"
    assert r["totals"]["nonzero_positions"] is None
