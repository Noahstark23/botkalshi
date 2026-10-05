"""Publicación privada y atómica; ningún reporte viejo o roto se sirve como OK."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from src.monitoring.operator_report_files import publish_report, read_report

NOW = datetime(2026, 9, 30, 1, tzinfo=UTC)


def report():
    balance = {
        "cash_usd": Decimal("0.04"),
        "portfolio_value_usd": "0",
        "updated_at": NOW.isoformat(),
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
        "collection_started_at": (NOW - timedelta(seconds=1)).isoformat(),
        "collection_finished_at": NOW.isoformat(),
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


def test_atomic_export_permissions_and_exact_decimal(tmp_path):
    path = tmp_path / "report.json"
    publish_report(path, report())
    assert path.stat().st_mode & 0o777 == 0o600
    r = read_report(path, now=NOW + timedelta(seconds=10))
    assert r["status"] == "OK"
    assert r["balance"]["cash_usd"] == "0.04"
    assert r["export_age_seconds"] == 10
    assert list(tmp_path.glob(".operator-report-*")) == []


@pytest.mark.parametrize("seconds", [601, -6])
def test_old_or_future_export_returns_unknown_without_old_cash(tmp_path, seconds):
    path = tmp_path / "report.json"
    publish_report(path, report())
    r = read_report(path, now=NOW + timedelta(seconds=seconds))
    assert r["status"] == "ATENCION"
    assert "balance" not in r


def test_failed_replace_keeps_previous_report_and_cleans_temporary_file(tmp_path):
    path = tmp_path / "report.json"
    publish_report(path, report())
    before = path.read_bytes()
    with (
        patch("src.monitoring.operator_report_files.os.replace", side_effect=OSError),
        pytest.raises(OSError),
    ):
        publish_report(path, {**report(), "status": "ATENCION"})
    assert path.read_bytes() == before
    assert list(tmp_path.glob(".operator-report-*")) == []


@pytest.mark.parametrize(
    "mutation",
    [
        "broken",
        "bad_total",
        "missing_resource",
        "missing_scope",
        "nan_cash",
        "changed",
        "wrong_version",
        "fake_complete",
    ],
)
def test_malformed_snapshot_does_not_survive_as_healthy(tmp_path, mutation):
    path = tmp_path / "report.json"
    r = report()
    if mutation == "broken":
        path.write_text('{"API_KEY":"secret",broken')
    else:
        if mutation == "bad_total":
            r["totals"]["resting_orders"] = 1
        elif mutation == "missing_resource":
            r.pop("positions")
        elif mutation == "missing_scope":
            r.pop("scope")
        elif mutation == "nan_cash":
            r["balance"]["cash_usd"] = "NaN"
        elif mutation == "changed":
            r["balance_after"]["cash_usd"] = "0.05"
        elif mutation == "wrong_version":
            r["schema_version"] = True
        else:
            r["orders"]["complete"] = False
        publish_report(path, r)
    result = read_report(path, now=NOW)
    assert result["status"] == "ATENCION"
    assert "secret" not in str(result)
    assert "balance" not in result


def test_absent_report_does_not_claim_zero_exposure(tmp_path):
    r = read_report(tmp_path / "missing", now=NOW)
    assert r["status"] == "ATENCION"
    assert "totals" not in r
