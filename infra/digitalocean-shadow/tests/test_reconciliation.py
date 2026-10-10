"""Conciliación read-only: posiciones/fills/fees del exchange ↔ ledger trades."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE.parents[1]))

import reconcile_account  # noqa: E402
import reconciliation as rc  # noqa: E402

A = "KXMLBGAME-26OCT09NYYBOS-NYY"
B = "KXMLBGAME-26OCT09NYMPHI-NYM"


def _account(positions=(), fills=()):
    return {
        "schema_version": "botkalshi-account-snapshot-v1",
        "observed_at": "2026-10-09T12:00:00+00:00",
        "balance_usd": "100.00",
        "positions": list(positions),
        "fills": list(fills),
    }


def _pos(ticker, qty):
    return {"ticker": ticker, "position_fp": str(qty), "position": qty}


def _row(coid, ticker, side, count, *, status="filled", action="buy", oid=None, **kw):
    r = {
        "client_order_id": coid,
        "kalshi_order_id": oid,
        "ticker": ticker,
        "side": side,
        "action": action,
        "count": count,
        "filled_count": None,
        "status": status,
        "strategy": "motor_1_arbitrage",
        "closed_by_clv": 0,
        "fill_price_cents": None,
        "fees_cents": None,
        "filled_at": "2026-10-09T11:00:00",
    }
    r.update(kw)
    return r


def _fill(oid, ticker, count, price, fee, *, taker=True, tid=None, created="2026-10-09T10:00:00Z"):
    return {
        "trade_id": tid or f"t-{oid}",
        "order_id": oid,
        "ticker": ticker,
        "side": "yes",
        "action": "buy",
        "count_fp": f"{count}.00",
        "price_dollars": f"0.{price:02d}00",
        "fee_cost_dollars": fee,
        "is_taker": taker,
        "created_time": created,
    }


def test_clean_book_reconciles():
    rows = [_row("c1", A, "yes", 3, oid="o1")]
    acct = _account([_pos(A, 3)], [_fill("o1", A, 3, 40, "0.0504")])
    rep = rc.reconcile(acct, rows)
    assert rep["status"] == "RECONCILED", rep["blockers"]
    assert rep["execution_authorized"] is False


def test_hedged_yes_no_nets_like_exchange():
    rows = [_row("c1", A, "yes", 2), _row("c2", A, "no", 2)]
    assert rc.reconcile(_account(), rows)["position_discrepancies"] == []


def test_exchange_only_orphan_is_flagged():
    """El caso 2026-07-07: exposición real que el ledger no conoce."""
    rep = rc.reconcile(_account([_pos(B, -5)]), [])
    assert rep["status"] == "NOT_RECONCILED"
    assert rep["position_discrepancies"] == [
        {
            "ticker": B,
            "kind": "EXCHANGE_ONLY",
            "exchange_position": "-5",
            "ledger_position": "0",
            "diff": "-5",
        }
    ]


def test_ledger_only_and_qty_mismatch():
    rows = [_row("c1", A, "yes", 3), _row("c2", B, "yes", 4, filled_count=2)]
    rep = rc.reconcile(_account([_pos(B, 3)]), rows)
    kinds = {d["ticker"]: d["kind"] for d in rep["position_discrepancies"]}
    assert kinds == {A: "LEDGER_ONLY", B: "QTY_MISMATCH"}


def test_closed_by_clv_settled_cancelled_are_not_open():
    rows = [
        _row("c1", A, "yes", 3, closed_by_clv=1),
        _row("c2", A, "yes", 3, status="settled"),
        _row("c3", A, "yes", 3, status="cancelled"),
        _row("c4", A, "yes", 3, action="sell"),
    ]
    assert rc.reconcile(_account(), rows)["position_discrepancies"] == []


def test_pending_rows_block_certification():
    rep = rc.reconcile(_account(), [_row("c1", A, "yes", 1, status="pending")])
    assert "PENDING_ORDERS_UNKNOWN_STATE" in rep["blockers"]


def test_fill_discrepancies():
    rows = [_row("c1", A, "yes", 3, oid="o1"), _row("c2", A, "yes", 1, oid="o2")]
    fills = [_fill("o1", A, 2, 40, "0.0336"), _fill("o9", A, 1, 40, "0.0168")]
    rep = rc.reconcile(_account([_pos(A, 4)], fills), rows)
    kinds = {d["order_id"]: d["kind"] for d in rep["fill_discrepancies"]}
    assert kinds == {
        "o1": "FILL_COUNT_MISMATCH",
        "o9": "EXCHANGE_FILL_UNKNOWN",
        "o2": "LEDGER_FILL_UNCONFIRMED",
    }


def test_ledger_fill_older_than_snapshot_window_is_not_flagged():
    rows = [_row("c1", A, "yes", 1, oid="old", filled_at="2026-09-01T00:00:00")]
    fills = [_fill("o1", A, 1, 40, "0.0168", created="2026-10-09T10:00:00Z")]
    rep = rc.reconcile(
        _account([_pos(A, 1)], fills), rows + [_row("c2", A, "yes", 0, oid="o1", filled_count=1)]
    )
    assert all(d["order_id"] != "old" for d in rep["fill_discrepancies"])


# --- validación empírica de fees ------------------------------------------------


@pytest.mark.parametrize(
    ("count", "price", "fee", "verdict"),
    [
        (1, 50, "0.0175", "MATCH_EXACT"),  # schedule centicent
        (1, 50, "0.0200", "MATCH_CONSERVATIVE"),  # ceil al centavo (producción)
        (100, 50, "1.7500", "MATCH_BOTH"),
        (1, 50, "0.0300", "MATCH_NEITHER"),
    ],
)
def test_fee_verdicts(count, price, fee, verdict):
    assert rc.validate_fill_fee(_fill("o", A, count, price, fee))["verdict"] == verdict


def test_fee_unexplained_blocks():
    rows = [_row("c1", A, "yes", 1, oid="o1")]
    rep = rc.reconcile(_account([_pos(A, 1)], [_fill("o1", A, 1, 50, "0.0300")]), rows)
    assert "FEE_MODEL_UNEXPLAINED" in rep["blockers"]


def test_maker_fill_uses_series_multiplier():
    # KXMLBGAME maker M=0.5 desde 2026-08-07 (repo): 0.0175×0.5×100×0.25 = 0.21875 → 0.2188
    f = _fill("o", A, 100, 50, "0.2188", taker=False)
    out = rc.validate_fill_fee(f)
    assert out["fee_multiplier"] == "1/2" and out["verdict"] == "MATCH_EXACT"


def test_subpenny_or_fractional_fill_not_evaluated():
    f = _fill("o", A, 1, 50, "0.0175")
    f["price_dollars"] = "0.5050"
    assert rc.validate_fill_fee(f)["verdict"] == "NOT_EVALUATED"
    g = _fill("o", A, 1, 50, "0.0175")
    g["count_fp"] = "1.50"
    assert rc.validate_fill_fee(g)["verdict"] == "NOT_EVALUATED"


def test_wrong_schema_rejected():
    with pytest.raises(rc.ReconciliationInputError):
        rc.reconcile({"schema_version": "x"}, [])


# --- CLI + lectura de SQLite en modo ro -----------------------------------------


def _make_db(path: Path, rows):
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, client_order_id TEXT, kalshi_order_id TEXT, "
        "ticker TEXT, side TEXT, action TEXT, count INTEGER, filled_count INTEGER, status TEXT, "
        "strategy TEXT, closed_by_clv INTEGER, fill_price_cents INTEGER, fees_cents INTEGER, "
        "filled_at TEXT, price_cents INTEGER, placed_at TEXT)"
    )
    for r in rows:
        cols = list(r)
        con.execute(
            f"INSERT INTO trades ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            list(r.values()),
        )
    con.commit()
    con.close()


def test_cli_exit_codes_and_db_untouched():
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "trades.db"
        _make_db(db, [_row("c1", A, "yes", 3, oid="o1")])
        before = db.read_bytes()
        acct = Path(d) / "a.json"
        acct.write_text(json.dumps(_account([_pos(A, 3)], [_fill("o1", A, 3, 40, "0.0504")])))
        assert (
            reconcile_account.main(
                ["--account", str(acct), "--db", str(db), "--out", str(Path(d) / "o.json")]
            )
            == 0
        )
        acct.write_text(json.dumps(_account([_pos(A, 9)])))
        assert reconcile_account.main(["--account", str(acct), "--db", str(db)]) == 3
        assert (
            reconcile_account.main(["--account", str(acct), "--db", str(Path(d) / "nope.db")]) == 2
        )
        assert db.read_bytes() == before


def test_load_ledger_is_readonly():
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "trades.db"
        _make_db(db, [])
        rows = rc.load_ledger_rows(db)
        assert rows == []
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        with pytest.raises(sqlite3.OperationalError):
            con.execute("INSERT INTO trades (ticker) VALUES ('x')")
        con.close()


@pytest.mark.parametrize("field", ["positions", "fills"])
def test_missing_collection_is_not_an_empty_account(field):
    account = _account()
    del account[field]
    with pytest.raises(rc.ReconciliationInputError):
        rc.reconcile(account, [])


def test_duplicate_position_cannot_hide_an_orphan():
    with pytest.raises(rc.ReconciliationInputError):
        rc.reconcile(_account([_pos(A, 9), _pos(A, 1)]), [_row("c", A, "yes", 1)])


def test_duplicate_fill_cannot_manufacture_matching_quantity():
    fill = _fill("o", A, 1, 50, "0.0175")
    with pytest.raises(rc.ReconciliationInputError):
        rc.reconcile(_account([_pos(A, 2)], [fill, fill]), [_row("c", A, "yes", 2, oid="o")])


def test_fractional_fill_not_evaluated_cannot_certify_reconciliation():
    fill = _fill("o", A, 1, 50, "0.0175")
    fill["count_fp"] = "0.50"
    assert rc.reconcile(_account(fills=[fill]), [])["status"] == "NOT_RECONCILED"


def test_empty_fill_window_does_not_confirm_ledger_fill():
    assert (
        rc.reconcile(_account([_pos(A, 1)]), [_row("c", A, "yes", 1, oid="o")])["status"]
        == "NOT_RECONCILED"
    )


def test_fractional_ledger_column_is_not_silently_dropped():
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "trades.db"
        _make_db(db, [_row("c", A, "yes", 1, filled_count=0)])
        with sqlite3.connect(db) as con:
            con.execute("ALTER TABLE trades ADD COLUMN filled_count_fp TEXT")
            con.execute("UPDATE trades SET filled_count_fp='0.50'")
        with pytest.raises(rc.ReconciliationInputError, match="fraccionaria"):
            rc.reconcile(_account(), rc.load_ledger_rows(db))
