"""Fallos inyectados sobre SQLite temporal: evidencia ficticia, nunca órdenes."""

from __future__ import annotations

import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paper_engine as pe  # noqa: E402
import simulation_bank as bank  # noqa: E402

TICKER = "SIM-TEST-YES"
T0 = "2026-10-09T12:00:00+00:00"


def signal(**changes):
    return {
        "schema_version": pe.SCHEMA_VERSION_SIGNAL,
        "signal_id": "recovery-1",
        "origin": "M2",
        "cycle_id": "cycle-1",
        "ticker": TICKER,
        "side": "yes",
        "limit_price_cents": 50,
        "count": 10,
        "signal_at": T0,
        **changes,
    }


def book(**changes):
    return {
        "ticker": TICKER,
        "observed_at": T0,
        "yes_bids": [[49, 10]],
        "no_bids": [[50, 10]],
        **changes,
    }


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "paper.sqlite3"
    bank.init_bank(path, initial_capital_usd="100.00")
    with pe._connect(path):
        pass
    return path


def test_fill_insert_failure_rolls_back_its_reservation(db):
    with sqlite3.connect(db) as con:
        con.execute("""CREATE TRIGGER fail_fill BEFORE INSERT ON paper_fills
                       BEGIN SELECT RAISE(ABORT, 'injected fill failure'); END""")
    before = bank.get_snapshot(db)
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        pe.process_signal(db, signal(), book())
    assert bank.get_snapshot(db) == before
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT count(*) FROM paper_fills").fetchone()[0] == 0


def test_release_failure_rolls_back_settlement_and_can_retry(db):
    pe.process_signal(db, signal(), book())
    before = bank.get_snapshot(db)
    with sqlite3.connect(db) as con:
        con.execute("""CREATE TRIGGER fail_release BEFORE UPDATE ON simulation_reservations
                       BEGIN SELECT RAISE(ABORT, 'injected release failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        pe.settle_market(db, ticker=TICKER, market_result="no")
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT count(*) FROM paper_settlements").fetchone()[0] == 0
        assert con.execute("SELECT settled FROM paper_fills").fetchone()[0] == 0
        con.execute("DROP TRIGGER fail_release")
    assert bank.get_snapshot(db) == before
    assert len(pe.settle_market(db, ticker=TICKER, market_result="no")) == 1


@pytest.mark.parametrize("result,available", [("no", "94.82"), ("yes", "104.82")])
def test_settlement_changes_available_capital_once_and_survives_reopen(db, result, available):
    pe.process_signal(db, signal(), book())
    pe.settle_market(db, ticker=TICKER, market_result=result)
    snapshot = bank.get_snapshot(db)
    assert snapshot["initial_capital_usd"] == "100.00"
    assert snapshot["available_usd"] == available
    assert snapshot["reserved_usd"] == "0.00"
    pe.settle_market(db, ticker=TICKER, market_result=result)
    assert bank.get_snapshot(db) == snapshot
    if result == "no":
        with pytest.raises(bank.SimulationBankInsufficientFundsError):
            bank.reserve(
                db, idempotency_key="after-loss", origin="M5", cycle_id="c2", amount_usd="95.00"
            )


@pytest.mark.parametrize(
    "change",
    [
        {"limit_price_cents": 51},
        {"origin": "M1"},
        {"cycle_id": "cycle-other"},
        {"signal_at": "2026-10-09T12:00:01+00:00"},
    ],
)
def test_idempotency_compares_entire_signal(db, change):
    pe.process_signal(db, signal(), book())
    with pytest.raises(pe.PaperEngineError, match="payload"):
        pe.process_signal(db, signal(**change), book())


def test_concurrent_identical_signal_charges_once(db):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: pe.process_signal(db, signal(), book()), range(2)))
    assert all(result["status"] == "FILLED" for result in results)
    assert bank.get_snapshot(db)["reserved_usd"] == "5.18"
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT count(*) FROM paper_fills").fetchone()[0] == 1


@pytest.mark.parametrize("age", [float("nan"), float("inf"), True, -1, "10"])
def test_invalid_book_age_limit_is_not_evidence(db, age):
    with pytest.raises(pe.PaperEngineError):
        pe.process_signal(db, signal(), book(max_book_age_sec=age))
    assert bank.get_snapshot(db)["reserved_usd"] == "0.00"


def test_conflicting_settlement_is_rejected(db):
    pe.process_signal(db, signal(), book())
    pe.settle_market(db, ticker=TICKER, market_result="yes")
    with pytest.raises(pe.PaperEngineError):
        pe.settle_market(db, ticker=TICKER, market_result="no")


def test_new_signal_for_settled_market_is_rejected(db):
    pe.process_signal(db, signal(), book())
    pe.settle_market(db, ticker=TICKER, market_result="yes")
    with pytest.raises(pe.PaperEngineError):
        pe.process_signal(db, signal(signal_id="late-signal"), book())


def test_legacy_settlement_without_bank_realization_is_not_silently_reused(db):
    pe.process_signal(db, signal(), book())
    pe.settle_market(db, ticker=TICKER, market_result="no")
    with sqlite3.connect(db) as con:
        con.execute("DELETE FROM simulation_realizations")
    with pytest.raises(pe.PaperEngineError, match="legacy"):
        pe.process_signal(
            db, signal(signal_id="s2", ticker="SIM-OTHER-YES"), book(ticker="SIM-OTHER-YES")
        )


def test_released_legacy_reservation_cannot_back_new_fill(db):
    pe.process_signal(db, signal(), book())
    with sqlite3.connect(db) as con:
        con.execute("DELETE FROM paper_fills")
    bank.release(db, idempotency_key="paper:recovery-1")
    with pytest.raises(pe.PaperEngineError, match="liberada"):
        pe.process_signal(db, signal(), book())
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT count(*) FROM paper_fills").fetchone()[0] == 0
