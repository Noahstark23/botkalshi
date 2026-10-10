"""Liquidaciones sintéticas con formato REST; nunca observaciones de cuenta."""

import copy
import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import m5_paper_ledger as m5  # noqa: E402
import paper_engine as m2  # noqa: E402
import paper_settlement_snapshot as settlement  # noqa: E402
import paper_stream as stream  # noqa: E402
import simulation_bank as bank  # noqa: E402

TICKER = "KXNFLGAME-26OCT11PHIJAC-JAC"
T0 = "2026-10-10T12:00:00Z"
T1 = "2026-10-10T12:00:01Z"
T2 = "2026-10-10T12:00:02Z"
RULES = "SYNTHETIC TEST RULES"


def observation(target="M2", payout=37):
    return {
        "type": "settlement_snapshot",
        "target": target,
        "ticker": TICKER,
        "rules_sha256": hashlib.sha256((RULES + "\n").encode()).hexdigest(),
        "observed_at": "2026-10-10T16:00:00Z",
        **({"quote_id": "q"} if target == "M5" else {}),
        "market": {
            "ticker": TICKER,
            "event_ticker": TICKER.rsplit("-", 1)[0],
            "market_type": "binary",
            "rules_primary": RULES,
            "rules_secondary": "",
            "status": "finalized",
            "result": {100: "yes", 0: "no"}.get(payout, "scalar"),
            "settlement_value_dollars": f"{payout / 100:.4f}",
            "settlement_ts": "2026-10-10T15:00:00Z",
            "notional_value_dollars": "1.0000",
            "settlement_bounds_type": "default",
            "last_price_dollars": "0.9900",  # Nunca se usa como pago.
        },
    }


def initial_events():
    events = []
    for side in ("yes", "no"):
        events.append(
            {
                "type": "signal",
                "signal": {
                    "schema_version": m2.SCHEMA_VERSION_SIGNAL,
                    "signal_id": side,
                    "origin": "M2",
                    "cycle_id": "settlement",
                    "ticker": TICKER,
                    "side": side,
                    "limit_price_cents": 60,
                    "count": 3,
                    "signal_at": T0,
                },
                "book": {
                    "ticker": TICKER,
                    "observed_at": T0,
                    "yes_bids": [[40, 2]],
                    "no_bids": [[60, 2]],
                },
            }
        )
    book = {
        "ticker": TICKER,
        "observed_at": T0,
        "yes_bid": 20,
        "yes_ask": 80,
        "bid_depth": "1",
        "ask_depth": "1",
    }
    events += [
        {
            "type": "m5_quote",
            "quote": {
                "quote_id": "q",
                "ticker": TICKER,
                "created_at": T0,
                "expires_at": "2026-10-10T12:05:00Z",
                "bid_cents": 30,
                "ask_cents": 70,
                "count": 2,
                "fee_type": "quadratic_with_maker_fees",
                "fee_multiplier": "1",
            },
            "book": book,
        },
        {
            "type": "m5_observe",
            "quote_id": "q",
            "observation_id": "buy",
            "at": T1,
            "book": {**book, "observed_at": T1, "yes_ask": 29},
        },
        {
            "type": "m5_observe",
            "quote_id": "q",
            "observation_id": "sell",
            "at": T2,
            "book": {**book, "observed_at": T2, "yes_bid": 71},
        },
        {"type": "m5_cancel", "quote_id": "q", "at": T2},
    ]
    return events


def cycle(db, events, mode="OFFLINE_SCENARIO"):
    return stream.run_cycle(db, "100.00", "settlement-test", mode, events)


@pytest.mark.parametrize("payout", [0, 1, 37, 50, 99, 100])
def test_shared_partial_fills_and_final_payout_conserve_cash_on_replay(tmp_path, payout):
    db = tmp_path / "paper.db"
    events = initial_events()
    before = cycle(db, events)
    assert before["bank"]["reserved_usd"] == "3.32"
    events += [observation("M2", payout), observation("M5", payout)]
    result = cycle(db, events)
    assert result["bank"]["reserved_usd"] == "0.00"
    # M2 paired buys: -8c fees; M5 synthetic crossed quotes: +38c. Not measured edge.
    assert result["bank"]["current_capital_usd"] == "100.30"
    assert result["m5"]["unsettled_positions"] == {}
    with sqlite3.connect(db) as con:
        table = "paper_settlements" if payout in (0, 100) else "paper_partial_settlements"
        values = dict(con.execute(f"SELECT signal_id,payout_cents FROM {table}"))
        assert values == {"yes": 2 * payout, "no": 2 * (100 - payout)}
        rows = con.execute(
            "SELECT result_json FROM simulation_ingest_events WHERE sequence>6"
        ).fetchall()
        assert all('"account_reconciled": false' in row[0] for row in rows)
    replay = cycle(db, events)
    assert replay["processed_this_cycle"] == 0
    assert replay["bank"] == result["bank"]


@pytest.mark.parametrize("target", ["M2", "M5"])
def test_crash_between_money_and_checkpoint_restarts_without_double_credit(
    tmp_path, monkeypatch, target
):
    db = tmp_path / "paper.db"
    events = initial_events()
    cycle(db, events)
    events.append(observation(target))
    original = stream._complete

    def crash(*args):
        raise RuntimeError("injected checkpoint crash")

    monkeypatch.setattr(stream, "_complete", crash)
    with pytest.raises(RuntimeError, match="checkpoint crash"):
        cycle(db, events)
    before = bank.get_snapshot(db)
    monkeypatch.setattr(stream, "_complete", original)
    result = cycle(db, events)
    assert result["bank"] == before
    assert result["processed_this_cycle"] == 1


@pytest.mark.parametrize(
    "status", ["active", "closed", "determined", "disputed", "amended", "settled", ""]
)
def test_nonfinal_result_never_enters_journal_or_releases_reserves(tmp_path, status):
    db = tmp_path / "paper.db"
    events = initial_events()
    before = cycle(db, events)
    event = observation()
    event["market"]["status"] = status
    with pytest.raises(settlement.SettlementSnapshotError, match="NOT_FINALIZED"):
        cycle(db, [*events, event])
    assert bank.get_snapshot(db) == before["bank"]
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT count(*) FROM simulation_ingest_events").fetchone()[0] == 6


@pytest.mark.parametrize(
    "value", [None, True, 0.5, 50, "NaN", "Infinity", "-0.1", "1.0100", "5e-1", "0.3060", "0.50000"]
)
def test_missing_malformed_or_subcent_payout_is_never_inferred_or_rounded(value):
    event = observation()
    event["market"]["settlement_value_dollars"] = value
    with pytest.raises(settlement.SettlementSnapshotError):
        settlement.review_snapshot(event)


@pytest.mark.parametrize(
    "field,value",
    [
        ("result", "yes"),
        ("result", "no"),
        ("result", ""),
        ("settlement_value", 50),
        ("settlement_value", True),
        ("notional_value_dollars", "0.5000"),
        ("settlement_bounds_type", "floor"),
        ("settlement_floor_dollars", "0.1000"),
        ("settlement_ts", None),
        ("settlement_ts", "2026-10-10T15:00:00"),
        ("settlement_ts", "2026-10-11T15:00:00Z"),
        ("rules_secondary", "changed"),
        ("rules_primary", None),
        ("ticker", TICKER + "X"),
        ("event_ticker", "WRONG"),
        ("market_type", "scalar"),
    ],
)
def test_conflicts_and_incomplete_identity_rejected(field, value):
    event = observation()
    event["market"][field] = value
    with pytest.raises(settlement.SettlementSnapshotError):
        settlement.review_snapshot(event)


def test_scalar_zero_and_one_remain_valid_and_no_payment_uses_last_price():
    for payout in [0, 37, 50, 100]:
        event = observation(payout=payout)
        event["market"]["result"] = "scalar"
        result = settlement.review_snapshot(event)
        assert result["yes_payout_cents"] == payout
        assert result["no_payout_cents"] == 100 - payout
        assert not result["account_reconciled"] and not result["execution_authorized"]


def test_shared_conflicting_payout_preserves_m5_reserve(tmp_path):
    db = tmp_path / "paper.db"
    events = [*initial_events(), observation("M2", 37)]
    cycle(db, events)
    before = bank.get_snapshot(db)
    with pytest.raises(bank.SimulationBankConflictError):
        cycle(db, [*events, observation("M5", 50)])
    assert bank.get_snapshot(db) == before
    assert m5.snapshot(db, "q")["state"] == "CANCELED"


def test_settlement_snapshot_does_not_cancel_open_or_unknown_m5(tmp_path):
    for unknown in [False, True]:
        db = tmp_path / f"unknown-{unknown}.db"
        events = initial_events()[:-1]
        if unknown:
            late = copy.deepcopy(events[-1])
            late.update(observation_id="gap", at="2026-10-10T12:02:00Z")
            late["book"]["observed_at"] = late["at"]
            events.append(late)
        cycle(db, events)
        before = bank.get_snapshot(db)
        with pytest.raises(m5.LedgerError, match="fully observed"):
            cycle(db, [*events, observation("M5")])
        assert bank.get_snapshot(db) == before
        assert m5.snapshot(db, "q")["state"] == ("UNKNOWN" if unknown else "OPEN")


@pytest.mark.parametrize("target", ["M2", "M5"])
def test_backdated_settlement_cannot_credit_later_activity(tmp_path, target):
    db = tmp_path / "paper.db"
    events = initial_events()
    before = cycle(db, events)
    event = observation(target)
    event["market"]["settlement_ts"] = "2026-10-10T11:00:00Z"
    with pytest.raises(settlement.SettlementSnapshotError, match="ACTIVITY_AFTER"):
        cycle(db, [*events, event])
    assert bank.get_snapshot(db) == before["bank"]


def test_connected_audit_mode_never_accepts_accounting_events(tmp_path):
    with pytest.raises(stream.StreamError, match="MODE_CONFLICT"):
        cycle(tmp_path / "capture.db", [observation()], mode="CONNECTED_CAPTURE")


def test_m5_cannot_credit_another_ticker_and_m2_cannot_claim_another_motor(tmp_path):
    db = tmp_path / "paper.db"
    cycle(db, initial_events())
    before = bank.get_snapshot(db)
    event = observation("M5")
    other = "KXNFLGAME-26OCT11PHIJAC-PHI"
    event["ticker"] = event["market"]["ticker"] = other
    with pytest.raises(settlement.SettlementSnapshotError, match="QUOTE_TICKER"):
        settlement.apply_to_paper(db, event)
    assert bank.get_snapshot(db) == before
    with sqlite3.connect(db) as con:
        con.execute("UPDATE paper_fills SET origin='M1'")
    with pytest.raises(settlement.SettlementSnapshotError, match="SCOPE_UNKNOWN"):
        settlement.apply_to_paper(db, observation("M2"))
    assert bank.get_snapshot(db) == before
