"""Paper engine: señal → fill simulado → reserva → settlement → PnL. Sin órdenes."""

from __future__ import annotations

import ast
import json
import socket
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE.parents[1]))

import paper_engine as pe  # noqa: E402
import paper_replay  # noqa: E402
import simulation_bank as bank  # noqa: E402

T0 = "2026-10-09T12:00:00+00:00"
T5 = "2026-10-09T12:00:05+00:00"
T60 = "2026-10-09T12:01:00+00:00"
TICKER = "KXMLBGAME-26OCT09NYYBOS-NYY"


def _signal(**over):
    s = {
        "schema_version": pe.SCHEMA_VERSION_SIGNAL,
        "signal_id": "s1",
        "origin": "M2",
        "cycle_id": "c1",
        "ticker": TICKER,
        "side": "yes",
        "limit_price_cents": 45,
        "count": 10,
        "signal_at": T5,
    }
    s.update(over)
    return s


def _book(**over):
    b = {
        "ticker": TICKER,
        "observed_at": T0,
        "yes_bids": [[50, 20]],
        "no_bids": [[57, 4], [55, 3], [50, 100]],  # asks YES: 43x4, 45x3, 50x100
    }
    b.update(over)
    return b


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "paper.sqlite3"
        bank.init_bank(path, initial_capital_usd="100.00")
        yield path


# --- modelo de fill -------------------------------------------------------------


def test_walks_depth_until_limit_and_reports_partial(db):
    r = pe.process_signal(db, _signal(), _book())
    assert r["status"] == "PARTIAL_FILL"
    assert r["filled_count"] == 7
    assert r["legs"] == [{"price_cents": 43, "count": 4}, {"price_cents": 45, "count": 3}]
    assert r["cost_usd"] == "3.07"  # 4×43 + 3×45 = 307¢
    # ceil por nivel: fee(4@43)=7¢, fee(3@45)=6¢ ; exacto 0.068628→0.0687 + 0.051975→0.0520
    assert r["fee_usd"] == "0.13"
    assert r["fee_exact_usd"] == "0.1207"
    assert r["max_loss_usd"] == "3.20"


def test_full_fill_buy_no_crosses_yes_bids(db):
    r = pe.process_signal(db, _signal(side="no", limit_price_cents=50, count=5), _book())
    assert r["status"] == "FILLED"
    assert r["legs"] == [{"price_cents": 50, "count": 5}]


def test_limit_below_best_ask_is_no_fill_and_reserves_nothing(db):
    r = pe.process_signal(db, _signal(limit_price_cents=42), _book())
    assert r["status"] == "NO_FILL" and r["filled_count"] == 0
    assert bank.get_snapshot(db)["reserved_usd"] == "0.00"


@pytest.mark.parametrize("signal_at", [T60, "2026-10-09T11:59:59+00:00"])
def test_stale_or_future_book_rejected(db, signal_at):
    r = pe.process_signal(db, _signal(signal_at=signal_at), _book())
    assert r["status"] == "REJECTED_STALE_BOOK"
    assert bank.get_snapshot(db)["reserved_usd"] == "0.00"


def test_book_for_other_ticker_is_error(db):
    with pytest.raises(pe.PaperEngineError):
        pe.process_signal(db, _signal(), _book(ticker="KXMLBGAME-26OCT09NYYBOS-BOS"))


# --- banco ficticio / idempotencia ---------------------------------------------


def test_fill_reserves_cost_plus_fee_in_simulation_bank(db):
    pe.process_signal(db, _signal(), _book())
    snap = bank.get_snapshot(db)
    assert snap["reserved_usd"] == "3.20"
    assert snap["active_reservations"][0]["origin"] == "PAPER_M2"


def test_replay_same_signal_does_not_charge_twice(db):
    pe.process_signal(db, _signal(), _book())
    r2 = pe.process_signal(db, _signal(), _book())
    assert r2["replayed"] is True
    assert bank.get_snapshot(db)["reserved_usd"] == "3.20"


def test_signal_id_reused_with_other_payload_raises(db):
    pe.process_signal(db, _signal(), _book())
    with pytest.raises(pe.PaperEngineError):
        pe.process_signal(db, _signal(side="no"), _book())


def test_insufficient_fictional_funds_rejects_without_fill():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "p.sqlite3"
        bank.init_bank(path, initial_capital_usd="1.00")
        r = pe.process_signal(path, _signal(), _book())
        assert r["status"] == "REJECTED_INSUFFICIENT_FUNDS"
        assert r["filled_count"] == 0
        assert bank.get_snapshot(path)["reserved_usd"] == "0.00"


# --- settlement / PnL ----------------------------------------------------------


def test_settlement_win_and_loss_realize_pnl_and_release(db):
    pe.process_signal(db, _signal(), _book())  # yes, 7 contratos, costo 307 fee 13
    pe.process_signal(
        db, _signal(signal_id="s2", side="no", limit_price_cents=50, count=2), _book()
    )
    out = {s["signal_id"]: s for s in pe.settle_market(db, ticker=TICKER, market_result="yes")}
    assert out["s1"]["pnl_usd"] == "3.80"  # 700 − 307 − 13
    assert out["s2"]["pnl_usd"] == "-1.04"  # 0 − 100 − 4
    assert bank.get_snapshot(db)["reserved_usd"] == "0.00"
    summ = pe.summary(db)
    assert summ["by_origin"]["M2"]["realized_pnl_usd"] == "2.76"
    assert summ["open_exposure_usd"] == "0.00"
    # idempotente
    assert pe.settle_market(db, ticker=TICKER, market_result="yes") == []


# --- contrato de señal ---------------------------------------------------------


@pytest.mark.parametrize(
    "over",
    [
        {"origin": "M7"},
        {"side": "maybe"},
        {"limit_price_cents": 0},
        {"limit_price_cents": 45.0},
        {"count": 0},
        {"signal_at": "2026-10-09T12:00:05"},  # naive → rechazado
        {"ticker": "bad ticker"},
        {"extra": 1},
    ],
)
def test_invalid_signal_fails_closed(db, over):
    with pytest.raises(pe.PaperEngineError):
        pe.process_signal(db, _signal(**over), _book())
    with pe._connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0] == 0


# --- replay + adaptador M1 -----------------------------------------------------


def test_replay_m1_book_runs_real_detector_and_settles(db):
    arb_book = {
        "ticker": "KXMLBGAME-26OCT09NYMPHI-NYM",
        "observed_at": T0,
        "yes_bids": [[55, 50]],  # ask NO = 45
        "no_bids": [[58, 50]],  # ask YES = 42 → 42+45 = 87 < 100
    }
    lines = [
        json.dumps(
            {"type": "m1_book", "cycle_id": "c9", "signal_at": T5, "max_count": 1, "book": arb_book}
        ),
        json.dumps({"type": "settle", "ticker": arb_book["ticker"], "result": "no"}),
    ]
    with tempfile.TemporaryDirectory() as d:
        report = paper_replay.run(Path(d) / "r.sqlite3", "100.00", lines)
    assert report["fills_by_status"] == {"FILLED": 2}
    m1 = report["summary"]["by_origin"]["M1"]
    # gana NO: +100 − 42 − 45 − fee(1@42)=2 − fee(1@45)=2 = +9¢
    assert m1["realized_pnl_usd"] == "0.09"
    assert report["bank"]["reserved_usd"] == "0.00"


def test_replay_no_arb_book_emits_nothing(db):
    flat = {"ticker": TICKER, "observed_at": T0, "yes_bids": [[48, 10]], "no_bids": [[50, 10]]}
    line = json.dumps({"type": "m1_book", "cycle_id": "c1", "signal_at": T5, "book": flat})
    with tempfile.TemporaryDirectory() as d:
        report = paper_replay.run(Path(d) / "r.sqlite3", "100.00", [line])
    assert report["fills_by_status"] == {}


def test_replay_bad_line_aborts_with_line_number():
    with tempfile.TemporaryDirectory() as d, pytest.raises(SystemExit, match="línea 1"):
        paper_replay.run(Path(d) / "r.sqlite3", "100.00", ['{"type": "nope"}'])


# --- guards estructurales: incapaz de operar -----------------------------------

_FORBIDDEN_IMPORTS = (
    "src.clients",
    "src.risk",
    "src.strategies",
    "httpx",
    "requests",
    "websockets",
    "aiohttp",
)


@pytest.mark.parametrize(
    "module", ["paper_engine.py", "paper_fees.py", "paper_replay.py", "reconciliation.py"]
)
def test_module_cannot_place_orders(module):
    tree = ast.parse((BASE / module).read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
        elif isinstance(node, ast.Attribute) and node.attr in (
            "place_order",
            "cancel_order",
            "create_order",
        ):
            pytest.fail(f"{module} referencia {node.attr}")
    bad = [n for n in names if n.startswith(_FORBIDDEN_IMPORTS)]
    assert bad == [], f"{module} importa {bad}"


def test_engine_opens_no_sockets(db):
    with patch.object(socket, "socket", side_effect=AssertionError("red prohibida")):
        pe.process_signal(db, _signal(), _book())
        pe.settle_market(db, ticker=TICKER, market_result="no")


def test_every_result_declares_no_authority(db):
    r = pe.process_signal(db, _signal(), _book())
    assert r["mode"] == "SIMULATION_ONLY"
    assert r["execution_authorized"] is False
    assert r["order_capability_present"] is False
    assert r["real_entry_eligible"] is False
