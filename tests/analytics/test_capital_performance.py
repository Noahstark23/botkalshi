"""Aportes no son ganancias y las fees ya reflejadas en cash no se descuentan dos veces."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest

from src.analytics.capital_performance import period_performance
from src.analytics.portfolio_report import json_safe


def snapshot(cash, day):
    balance = {
        "cash_usd": cash,
        "portfolio_value_usd": "0",
        "updated_at": f"2026-09-{day:02}T00:00:00Z",
    }
    return {
        "schema_version": 1,
        "status": "OK",
        "read_only": True,
        "authorizes_trading": False,
        "errors": [],
        "balance_unchanged": True,
        "scope": {
            "environment": "production",
            "subaccount": 0,
            "source_fingerprint": "f" * 64,
            "exchange_indexes": "all",
        },
        "balance": balance,
        "balance_after": deepcopy(balance),
        "collection_started_at": f"2026-09-{day:02}T00:00:00Z",
        "collection_finished_at": f"2026-09-{day:02}T00:00:01Z",
        "totals": {
            "nonzero_positions": 0,
            "market_exposure_usd": "0",
            "resting_orders": 0,
            "fill_fees_usd": "0",
            "settlement_revenue_usd": "0",
        },
        **{
            name: {"complete": True, "rows": [], "pages": 1, "error": None}
            for name in ("positions", "orders", "fills", "settlements")
        },
    }


def document(first="200", last="420"):
    return {
        "opening": snapshot(first, 1),
        "closing": snapshot(last, 29),
        "cash_flows_complete": True,
        "boundary_activity_excluded": True,
        "cash_flows": [
            {"id": "d1", "kind": "deposit", "amount_usd": "200", "at": "2026-09-15T12:00:00Z"}
        ],
    }


def test_monthly_deposit_is_excluded_and_external_costs_reduce_profit_once():
    doc = document()
    doc["cash_flows"].append(
        {
            "id": "host",
            "kind": "external_cost",
            "amount_usd": "6",
            "paid_outside_account": True,
            "at": "2026-09-20T00:00:00Z",
        }
    )
    r = period_performance(doc)
    assert r["status"] == "OK"
    assert r["account_net_pnl_usd"] == Decimal("20")
    assert r["project_pnl_usd"] == Decimal("14")
    assert r["fees_already_reflected_in_cash"] is True
    assert r["authorizes_trading"] is False


def test_fresh_deposit_cannot_hide_loss_and_withdrawals_are_not_loss():
    r = period_performance(document("200", "390"))
    assert r["project_pnl_usd"] == Decimal("-10")
    doc = document("200", "320")
    doc["cash_flows"].append(
        {"id": "w1", "kind": "withdrawal", "amount_usd": "100", "at": "2026-09-20T00:00:00Z"}
    )
    assert period_performance(doc)["account_net_pnl_usd"] == Decimal("20")


def test_subcent_precision_survives_json_serialization():
    r = period_performance(document("200.0000", "400.0178"))
    assert json_safe(r)["project_pnl_usd"] == "0.0178"


@pytest.mark.parametrize(
    "target,value",
    [
        ("cash_flows_complete", False),
        ("cash_flows_complete", None),
        ("cash_flows", None),
    ],
)
def test_missing_flow_reconciliation_leaves_pnl_unknown(target, value):
    doc = document()
    doc[target] = value
    r = period_performance(doc)
    assert r["status"] == "ATENCION"
    assert r["project_pnl_usd"] is None


@pytest.mark.parametrize(
    "change",
    [
        {"amount_usd": None},
        {"amount_usd": True},
        {"amount_usd": "NaN"},
        {"amount_usd": "-1"},
        {"amount_usd": "0"},
        {"kind": "guess"},
        {"at": "2026-08-31T00:00:00Z"},
        {"at": "2026-09-15T00:00:00"},
        {"kind": "external_cost", "paid_outside_account": False},
    ],
)
def test_bad_flows_do_not_produce_a_profit_claim(change):
    doc = document()
    doc["cash_flows"][0].update(change)
    r = period_performance(doc)
    assert r["status"] == "ATENCION"
    assert r["account_net_pnl_usd"] is None


def test_duplicate_flow_id_is_not_counted_twice():
    doc = document()
    doc["cash_flows"].append(deepcopy(doc["cash_flows"][0]))
    assert period_performance(doc)["status"] == "ATENCION"


@pytest.mark.parametrize(
    "mutation",
    [
        "source",
        "exposure",
        "pending_value",
        "resting",
        "fraction_hidden",
        "incomplete",
        "changed",
        "time",
    ],
)
def test_uncertain_valuation_or_scope_requires_more_evidence(mutation):
    doc = document()
    s = doc["closing"]
    if mutation == "source":
        s["scope"]["subaccount"] = 1
    elif mutation == "exposure":
        s["totals"]["market_exposure_usd"] = "0.01"
    elif mutation == "pending_value":
        s["balance"]["portfolio_value_usd"] = "0.42"
    elif mutation == "resting":
        s["orders"]["rows"] = [{"order_id": "o1"}]
    elif mutation == "fraction_hidden":
        s["positions"]["rows"] = [{"quantity": "0.42", "market_exposure_usd": "0"}]
    elif mutation == "incomplete":
        s["fills"]["complete"] = False
    elif mutation == "changed":
        s["balance_unchanged"] = False
    else:
        s["collection_started_at"] = doc["opening"]["collection_finished_at"]
    r = period_performance(doc)
    assert r["status"] == "ATENCION"
    assert r["project_pnl_usd"] is None


@pytest.mark.parametrize(
    "bad",
    [None, [], {}, {"cash_flows_complete": True}, {"cash_flows_complete": True, "opening": []}],
)
def test_malformed_documents_do_not_crash_or_claim_zero_profit(bad):
    r = period_performance(bad)
    assert r["status"] == "ATENCION"
    assert r["project_pnl_usd"] is None


def test_boundary_activity_attestation_is_required():
    doc = document()
    doc.pop("boundary_activity_excluded")
    assert period_performance(doc)["status"] == "ATENCION"


@pytest.mark.parametrize("at", ["2026-09-01T00:00:00.5Z", "2026-09-29T00:00:00.5Z"])
def test_deposit_during_either_snapshot_cannot_appear_as_profit(at):
    doc = document()
    doc["cash_flows"][0]["at"] = at
    r = period_performance(doc)
    assert r["status"] == "ATENCION"
    assert r["account_net_pnl_usd"] is None
