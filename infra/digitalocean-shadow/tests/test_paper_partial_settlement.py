"""Pagos parciales ficticios: complemento, conservación, rollback y reinicio."""

import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import m5_paper_ledger as m5  # noqa: E402
import paper_engine as pe  # noqa: E402
import paper_replay  # noqa: E402
import simulation_bank as bank  # noqa: E402

TICKER = "SIM-PARTIAL"
T0 = "2026-10-10T12:00:00+00:00"


def signal(side="yes"):
    return {
        "schema_version": pe.SCHEMA_VERSION_SIGNAL,
        "signal_id": side,
        "origin": "M2",
        "cycle_id": "partial",
        "ticker": TICKER,
        "side": side,
        "limit_price_cents": 60,
        "count": 3,
        "signal_at": T0,
    }


def book():
    return {"ticker": TICKER, "observed_at": T0, "yes_bids": [[40, 2]], "no_bids": [[60, 2]]}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "fictional.sqlite3"
    bank.init_bank(path, initial_capital_usd="100.00")
    return path


@pytest.mark.parametrize("payout", [0, 1, 37, 50, 99, 100])
@pytest.mark.parametrize("side", ["yes", "no"])
def test_partial_fill_pays_only_filled_quantity_and_complement_once(db, payout, side):
    fill = pe.process_signal(db, signal(side), book())
    assert fill["status"] == "PARTIAL_FILL" and fill["filled_count"] == 2
    out = pe.settle_market(db, ticker=TICKER, yes_payout_cents=payout)
    unit = payout if side == "yes" else 100 - payout
    cost = 80 if side == "yes" else 120
    expected_pnl = 2 * unit - cost - 4  # explicit conservative fee for 2 @ 40/60
    assert out == [
        {"signal_id": side, "payout_usd": bank._usd(2 * unit), "pnl_usd": bank._usd(expected_pnl)}
    ]
    snapshot = bank.get_snapshot(db)
    assert snapshot["current_capital_usd"] == bank._usd(10000 + expected_pnl)
    assert snapshot["reserved_usd"] == "0.00"
    assert pe.summary(db)["by_origin"]["M2"]["realized_pnl_usd"] == bank._usd(expected_pnl)
    assert pe.summary(db)["by_origin"]["M2"]["settled"] == 1
    assert pe.settle_market(db, ticker=TICKER, yes_payout_cents=payout) == []
    assert bank.get_snapshot(db) == snapshot
    with pytest.raises((pe.PaperEngineError, bank.SimulationBankConflictError)):
        pe.settle_market(db, ticker=TICKER, yes_payout_cents=(payout + 1) % 101)


def test_both_sides_at_tie_return_contract_payout_not_stake(db):
    for side in ("yes", "no"):
        pe.process_signal(db, signal(side), book())
    out = pe.settle_market(db, ticker=TICKER, yes_payout_cents=50)
    assert {r["signal_id"]: r["pnl_usd"] for r in out} == {"yes": "0.16", "no": "-0.24"}
    assert bank.get_snapshot(db)["current_capital_usd"] == "99.92"


@pytest.mark.parametrize("value", [True, False, "50", 50.0, None, -1, 101])
def test_invalid_partial_value_never_changes_money(db, value):
    pe.process_signal(db, signal(), book())
    before = bank.get_snapshot(db)
    with pytest.raises(pe.PaperEngineError):
        pe.settle_market(db, ticker=TICKER, yes_payout_cents=value)
    assert bank.get_snapshot(db) == before


def test_two_result_forms_rejected_even_if_equivalent(db):
    with pytest.raises(pe.PaperEngineError):
        pe.settle_market(db, ticker=TICKER, market_result="yes", yes_payout_cents=100)


@pytest.mark.parametrize("table", ["paper_partial_settlements", "simulation_realizations"])
def test_failure_after_first_side_rolls_back_entire_market(db, table):
    for side in ("yes", "no"):
        pe.process_signal(db, signal(side), book())
    before = bank.get_snapshot(db)
    with sqlite3.connect(db) as con:
        con.execute(f"""CREATE TRIGGER injected BEFORE INSERT ON {table}
                        WHEN (SELECT count(*) FROM {table}) = 1
                        BEGIN SELECT RAISE(ABORT, 'injected'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        pe.settle_market(db, ticker=TICKER, yes_payout_cents=37)
    assert bank.get_snapshot(db) == before
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT count(*) FROM simulation_market_results").fetchone()[0] == 0
        assert con.execute("SELECT sum(settled) FROM paper_fills").fetchone()[0] == 0
        con.execute("DROP TRIGGER injected")
    assert len(pe.settle_market(db, ticker=TICKER, yes_payout_cents=37)) == 2


def test_concurrent_settlement_and_replay_realize_only_once(db):
    pe.process_signal(db, signal(), book())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: pe.settle_market(db, ticker=TICKER, yes_payout_cents=50), range(2))
        )
    assert sorted(map(len, results)) == [0, 1]
    before = bank.get_snapshot(db)
    paper_replay.run(
        db, "100.00", [json.dumps({"type": "settle", "ticker": TICKER, "yes_payout_cents": 50})]
    )
    assert bank.get_snapshot(db) == before


def test_additive_schema_preserves_legacy_binary_settlement(db):
    pe.process_signal(db, signal(), book())
    pe.settle_market(db, ticker=TICKER, market_result="yes")
    with sqlite3.connect(db) as con:
        old_rows = con.execute("SELECT * FROM paper_settlements").fetchall()
        con.execute("DROP TABLE paper_partial_settlements")  # emulate old schema
    before = bank.get_snapshot(db)
    assert pe.settle_market(db, ticker=TICKER, yes_payout_cents=100) == []
    assert bank.get_snapshot(db) == before
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT * FROM paper_settlements").fetchall() == old_rows


def test_partial_record_still_blocks_admission_if_shared_index_is_missing(db):
    pe.process_signal(db, signal(), book())
    pe.settle_market(db, ticker=TICKER, yes_payout_cents=50)
    with sqlite3.connect(db) as con:
        con.execute("DELETE FROM simulation_market_results")
    with pytest.raises(pe.PaperEngineError, match="liquidado"):
        pe.process_signal(db, {**signal(), "signal_id": "late"}, book())
    with bank._connect(db) as con:
        assert bank._market_payout(con, TICKER) == 50
    with pytest.raises(pe.PaperEngineError, match="contradictorio"):
        pe.settle_market(db, ticker=TICKER, market_result="no")


@pytest.mark.parametrize(
    "query",
    [
        "UPDATE paper_partial_settlements SET payout_cents=999",
        "UPDATE paper_partial_settlements SET pnl_cents=999",
        "UPDATE paper_fills SET settled=0",
        "UPDATE simulation_reservations SET status='ACTIVE'",
        "DELETE FROM simulation_realizations",
    ],
)
def test_inconsistent_partial_accounting_is_not_a_valid_summary(db, query):
    pe.process_signal(db, signal(), book())
    pe.settle_market(db, ticker=TICKER, yes_payout_cents=50)
    with sqlite3.connect(db) as con:
        con.execute(query)
    with pytest.raises(pe.PaperEngineError):
        pe.summary(db)


@pytest.mark.parametrize("first", ["M2", "M5"])
def test_shared_market_payout_must_match_between_motors(db, first):
    pe.process_signal(db, signal(), book())
    q = {
        "quote_id": "q",
        "ticker": TICKER,
        "created_at": T0,
        "expires_at": "2026-10-10T12:05:00+00:00",
        "bid_cents": 30,
        "ask_cents": 70,
        "count": 1,
        "fee_type": "quadratic",
        "fee_multiplier": "1",
    }
    b = {
        "ticker": TICKER,
        "observed_at": T0,
        "yes_bid": 20,
        "yes_ask": 80,
        "bid_depth": "1",
        "ask_depth": "1",
    }
    m5.admit_quote(db, q, b)
    m5.cancel_quote(db, "q", at=T0)

    def a(p):
        return pe.settle_market(db, ticker=TICKER, yes_payout_cents=p)

    def b(p):
        return m5.settle_quote(db, "q", yes_payout_cents=p)

    settle_first, settle_second = (a, b) if first == "M2" else (b, a)
    settle_first(37)
    before = bank.get_snapshot(db)
    with pytest.raises(bank.SimulationBankConflictError):
        settle_second(50)
    assert bank.get_snapshot(db) == before
    settle_second(37)
    with pytest.raises(pe.PaperEngineError):
        pe.process_signal(db, {**signal(), "signal_id": "late"}, book())
