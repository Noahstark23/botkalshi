"""Dinero ficticio: reserva previa, dos lados, reinicio y fallos de disco."""

from __future__ import annotations

import ast
import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import m5_paper_ledger as m5  # noqa: E402
import paper_engine as m2  # noqa: E402
import paper_replay  # noqa: E402
import simulation_bank as bank  # noqa: E402

T0 = "2026-10-09T16:00:00+00:00"
T1 = "2026-10-09T16:00:01+00:00"
T2 = "2026-10-09T16:00:02+00:00"


def quote(**extra):
    return dict(
        quote_id="q1",
        ticker="SIM-TEST",
        created_at=T0,
        expires_at="2026-10-09T16:05:00+00:00",
        bid_cents=40,
        ask_cents=60,
        count=2,
        fee_type="quadratic_with_maker_fees",
        fee_multiplier="1",
        **extra,
    )


def book(at=T0, **extra):
    return {
        "ticker": "SIM-TEST",
        "observed_at": at,
        "yes_bid": 35,
        "yes_ask": 65,
        "bid_depth": "1.00",
        "ask_depth": "1.00",
        **extra,
    }


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "fictional.sqlite3"
    bank.init_bank(path, initial_capital_usd="100.00")
    return path


def buy_fill(db, q=None):
    m5.admit_quote(db, q or quote(), book())
    return m5.observe(db, "q1", observation_id="t1", at=T1, book=book(T1, yes_ask=39))


def test_reserves_both_sides_before_any_fill(db):
    r = m5.admit_quote(db, quote(), book())
    assert r["bank"]["reserved_usd"] == "1.64"  # 2*(40+1), twice
    assert all(leg["filled"] == 0 for leg in r["legs"])
    assert not r["execution_authorized"] and not r["order_capability_present"]
    assert m5.admit_quote(db, quote(), book()) == r


def test_two_legs_admitted_atomically_when_second_cannot_fit(tmp_path):
    db = tmp_path / "small.sqlite3"
    bank.init_bank(db, initial_capital_usd="1.00")
    before = bank.get_snapshot(db)
    with pytest.raises(bank.SimulationBankInsufficientFundsError):
        m5.admit_quote(db, quote(), book())
    assert bank.get_snapshot(db) == before
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT count(*) FROM m5_paper_legs").fetchone()[0] == 0


@pytest.mark.parametrize("payout,expected", [(0, "99.59"), (50, "100.09"), (100, "100.59")])
def test_partial_buy_cancel_settle_and_restart(db, payout, expected):
    buy_fill(db)
    canceled = m5.cancel_quote(db, "q1", at=T1)
    assert canceled["bank"]["reserved_usd"] == "0.82"  # buy retains conservative bound
    result = m5.settle_quote(db, "q1", yes_payout_cents=payout)
    assert result["bank"]["current_capital_usd"] == expected
    assert result["bank"]["reserved_usd"] == "0.00"
    assert m5.snapshot(db, "q1")["state"] == "SETTLED"
    assert m5.settle_quote(db, "q1", yes_payout_cents=payout) == result


@pytest.mark.parametrize("payout,expected", [(0, "100.59"), (50, "100.09"), (100, "99.59")])
def test_partial_sell_settlement_complement(db, payout, expected):
    m5.admit_quote(db, quote(), book())
    m5.observe(db, "q1", observation_id="t1", at=T1, book=book(T1, yes_bid=61))
    m5.cancel_quote(db, "q1", at=T1)
    assert (
        m5.settle_quote(db, "q1", yes_payout_cents=payout)["bank"]["current_capital_usd"]
        == expected
    )


def test_both_sides_fill_then_settle_net_of_fees(db):
    buy_fill(db)
    m5.observe(db, "q1", observation_id="t2", at=T2, book=book(T2, yes_bid=61))
    m5.cancel_quote(db, "q1", at=T2)
    assert m5.settle_quote(db, "q1", yes_payout_cents=100)["pnl_cents"] == 18


def test_cancel_without_fill_frees_all_and_never_reopens(db):
    m5.admit_quote(db, quote(), book())
    canceled = m5.cancel_quote(db, "q1", at=T0)
    assert canceled["bank"]["reserved_usd"] == "0.00"
    assert m5.admit_quote(db, quote(), book())["state"] == "CANCELED"
    assert m5.settle_quote(db, "q1", yes_payout_cents=100)["pnl_cents"] == 0


def test_fragmented_fills_charge_once_and_keep_within_reserved_bound(db):
    first = buy_fill(db)
    assert m5.observe(db, "q1", observation_id="t1", at=T1, book=book(T1, yes_ask=39)) == first
    second = m5.observe(db, "q1", observation_id="t2", at=T2, book=book(T2, yes_ask=39))
    assert second["legs"][0]["filled"] == 2 and second["legs"][0]["fee_cents"] == 2
    m5.cancel_quote(db, "q1", at=T2)
    assert m5.settle_quote(db, "q1", yes_payout_cents=0)["bank"]["current_capital_usd"] == "99.18"


@pytest.mark.parametrize("change", [{"yes_ask": 40}, {"yes_ask": 39, "ask_depth": "0.33"}])
def test_touch_and_fractional_depth_cannot_manufacture_fill(db, change):
    m5.admit_quote(db, quote(), book())
    result = m5.observe(db, "q1", observation_id="t1", at=T1, book=book(T1, **change))
    assert all(leg["filled"] == 0 for leg in result["legs"])


@pytest.mark.parametrize("late", ["2026-10-09T16:01:01+00:00", "2026-10-09T16:06:00+00:00"])
def test_gap_or_expiry_preserves_reserves_and_pauses_durably(db, late):
    m5.admit_quote(db, quote(), book())
    result = m5.observe(db, "q1", observation_id="late", at=late, book=book(late))
    assert result["state"] == "UNKNOWN" and result["paused"]
    assert m5.snapshot(db, "q1")["bank"]["reserved_usd"] == "1.64"
    q = {**quote(), "quote_id": "q2", "ticker": "SIM-OTHER"}
    with pytest.raises(m5.LedgerError, match="paused"):
        m5.admit_quote(db, q, book(ticker="SIM-OTHER"))
    with pytest.raises(m5.LedgerError):
        m5.cancel_quote(db, "q1", at=late)
    with pytest.raises(m5.LedgerError):
        m5.settle_quote(db, "q1", yes_payout_cents=0)


def test_observation_conflict_and_stale_sequence_are_rejected(db):
    buy_fill(db)
    before = m5.snapshot(db, "q1")
    with pytest.raises(m5.LedgerError, match="conflict"):
        m5.observe(db, "q1", observation_id="t1", at=T1, book=book(T1))
    with pytest.raises(m5.LedgerError):
        m5.observe(db, "q1", observation_id="old", at=T0, book=book())
    with pytest.raises(m5.LedgerError):
        m5.observe(db, "q1", observation_id="same-book", at=T2, book=book(T1))
    assert m5.snapshot(db, "q1") == before


@pytest.mark.parametrize(
    "target,action",
    [
        ("m5_paper_quotes", "admit"),
        ("m5_paper_observations", "observe"),
        ("m5_paper_settlements", "settle"),
    ],
)
def test_persistence_failure_rolls_back_all_related_money(db, target, action):
    if action != "admit":
        buy_fill(db)
    if action == "settle":
        m5.cancel_quote(db, "q1", at=T1)
    before = bank.get_snapshot(db)
    with sqlite3.connect(db) as con:
        # Bootstrap new tables without a reservation before injecting admission failure.
        if action == "admit":
            with m5._transaction(db):
                pass
        con.execute(
            f"CREATE TRIGGER fail BEFORE INSERT ON {target} BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        if action == "admit":
            m5.admit_quote(db, quote(), book())
        elif action == "observe":
            m5.observe(db, "q1", observation_id="t2", at=T2, book=book(T2, yes_ask=39))
        else:
            m5.settle_quote(db, "q1", yes_payout_cents=0)
    assert bank.get_snapshot(db) == before
    if action == "observe":
        assert m5.snapshot(db, "q1")["legs"][0]["filled"] == 1


def m2_signal():
    return {
        "schema_version": m2.SCHEMA_VERSION_SIGNAL,
        "signal_id": "s1",
        "origin": "M2",
        "cycle_id": "c1",
        "ticker": "SIM-TEST",
        "side": "yes",
        "count": 1,
        "limit_price_cents": 50,
        "signal_at": T0,
    }


def m2_book():
    return {"ticker": "SIM-TEST", "observed_at": T0, "yes_bids": [[49, 1]], "no_bids": [[50, 1]]}


def test_m2_and_m5_compete_for_same_capital_under_concurrency(tmp_path):
    db = tmp_path / "shared.sqlite3"
    bank.init_bank(db, initial_capital_usd="1.00")
    q = {**quote(), "count": 1}

    def submit_m5():
        try:
            return m5.admit_quote(db, q, book())
        except bank.SimulationBankInsufficientFundsError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(submit_m5)
        b = pool.submit(m2.process_signal, db, m2_signal(), m2_book())
        r5, r2 = a.result(), b.result()
    assert (r5 is not None) != (r2["status"] == "FILLED")
    assert bank.get_snapshot(db)["reserved_usd"] in {"0.82", "0.52"}


def test_shared_settlement_prevents_contradictory_results_and_new_quotes(db):
    m2.process_signal(db, m2_signal(), m2_book())
    buy_fill(db)
    m5.cancel_quote(db, "q1", at=T1)
    m2.settle_market(db, ticker="SIM-TEST", market_result="yes")
    with pytest.raises(bank.SimulationBankConflictError):
        m5.settle_quote(db, "q1", yes_payout_cents=0)
    m5.settle_quote(db, "q1", yes_payout_cents=100)
    assert bank.get_snapshot(db)["current_capital_usd"] == "101.07"
    with pytest.raises(m5.LedgerError, match="settled"):
        m5.admit_quote(db, {**quote(), "quote_id": "after"}, book())


def test_m5_tie_cannot_silently_become_m2_loss(db):
    buy_fill(db)
    m5.cancel_quote(db, "q1", at=T1)
    m5.settle_quote(db, "q1", yes_payout_cents=50)
    with pytest.raises(bank.SimulationBankConflictError):
        m2.settle_market(db, ticker="SIM-TEST", market_result="no")
    with pytest.raises(m2.PaperEngineError, match="liquidado"):
        m2.process_signal(db, m2_signal(), m2_book())


def test_general_quadratic_maker_fee_model_and_unsupported_schedule(db):
    q = {**quote(), "fee_type": "quadratic"}
    assert m5.admit_quote(db, q, book())["bank"]["reserved_usd"] == "1.60"
    with pytest.raises(m5.LedgerError, match="fee"):
        m5.admit_quote(db, {**quote(), "fee_type": "flat"}, book())


def test_replay_cannot_certify_quote_whose_reservation_was_released(db):
    m5.admit_quote(db, quote(), book())
    bank.release(db, idempotency_key="m5paper:q1:buy")
    with pytest.raises(m5.LedgerError, match="disagree"):
        m5.admit_quote(db, quote(), book())


@pytest.mark.parametrize(
    "field,value",
    [
        ("count", True),
        ("count", 0),
        ("count", 101),
        ("fee_multiplier", "NaN"),
        ("fee_multiplier", True),
        ("bid_cents", 65),
        ("ask_cents", 35),
    ],
)
def test_invalid_quote_never_reserves(db, field, value):
    before = bank.get_snapshot(db)
    with pytest.raises(m5.LedgerError):
        m5.admit_quote(db, {**quote(), field: value}, book())
    assert bank.get_snapshot(db) == before


def test_cancellation_without_final_observation_cannot_free_reserve(db):
    m5.admit_quote(db, quote(), book())
    with pytest.raises(m5.LedgerError, match="observation"):
        m5.cancel_quote(db, "q1", at=T1)
    assert bank.get_snapshot(db)["reserved_usd"] == "1.64"


def test_no_client_executor_or_network_imports():
    source = Path(m5.__file__).read_text()
    for node in ast.walk(ast.parse(source)):
        names = (
            [a.name for a in node.names]
            if isinstance(node, ast.Import)
            else [node.module]
            if isinstance(node, ast.ImportFrom)
            else []
        )
        assert all(
            name.split(".")[0]
            in {
                "__future__",
                "contextlib",
                "json",
                "datetime",
                "decimal",
                "pathlib",
                "simulation_bank",
                "paper_fees",
            }
            for name in names
        )


def test_shared_replay_restart_is_idempotent_and_reports_loss(tmp_path):
    events = [
        {"type": "signal", "signal": m2_signal(), "book": m2_book()},
        {"type": "m5_quote", "quote": quote(), "book": book()},
        {
            "type": "m5_observe",
            "quote_id": "q1",
            "observation_id": "o1",
            "at": T1,
            "book": book(T1, yes_ask=39),
        },
        {"type": "m5_cancel", "quote_id": "q1", "at": T1},
        {"type": "settle", "ticker": "SIM-TEST", "result": "no"},
        {"type": "m5_settle", "quote_id": "q1", "yes_payout_cents": 0},
    ]
    db = tmp_path / "replay.sqlite3"
    lines = [json.dumps(event) for event in events]
    first = paper_replay.run(db, "100.00", lines)
    second = paper_replay.run(db, "100.00", lines)
    assert first == second
    assert first["bank"]["current_capital_usd"] == "99.07"
    assert first["bank"]["reserved_usd"] == "0.00"
    assert first["m5_summary"]["realized_pnl_cents"] == -41
    assert first["m5_summary"]["unsettled_positions"] == {}


def test_summary_reports_gross_unsettled_positions_even_if_net_is_zero(db):
    buy_fill(db)
    m5.observe(db, "q1", observation_id="t2", at=T2, book=book(T2, yes_bid=61))
    result = m5.summary(db)
    assert result["unsettled_positions"] == {"SIM-TEST": {"buy": 1, "sell": 1, "net_yes": 0}}
    assert result["reserved_cents"] == 164
    assert result["hypothetical_fees_cents"] == 2
    assert result["realized_pnl_cents"] == 0


def test_exact_expiry_cannot_generate_a_fill(db):
    m5.admit_quote(db, {**quote(), "expires_at": T1}, book())
    result = m5.observe(db, "q1", observation_id="t1", at=T1, book=book(T1, yes_ask=39))
    assert result["state"] == "UNKNOWN"
    assert m5.summary(db)["filled_contracts"] == 0
    assert m5.summary(db)["reserved_cents"] == 164


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM m5_paper_legs WHERE side='sell'",
        "UPDATE m5_paper_legs SET capacity=0,filled=0,fees=0 WHERE side='sell'",
    ],
)
def test_incomplete_ledger_is_not_reported_as_funded(db, statement):
    m5.admit_quote(db, quote(), book())
    with sqlite3.connect(db) as con:
        con.execute(statement)
    with pytest.raises(m5.LedgerError):
        m5.summary(db)


def test_conflicting_cancel_after_settlement_is_rejected(db):
    buy_fill(db)
    m5.cancel_quote(db, "q1", at=T1)
    m5.settle_quote(db, "q1", yes_payout_cents=100)
    with pytest.raises(m5.LedgerError, match="timestamp"):
        m5.cancel_quote(db, "q1", at=T2)


def test_replay_rejects_non_object_with_line_number(tmp_path):
    with pytest.raises(SystemExit, match="línea 1"):
        paper_replay.run(tmp_path / "bad.sqlite3", "100", ["[]"])
