"""Consumidor offline: append, reanudación, errores, evidencia y modo inmutable."""

import copy
import fcntl
import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import football_capture_import as importer  # noqa: E402
import paper_stream as stream  # noqa: E402
import simulation_bank as bank  # noqa: E402

# Fixture ya usada por las pruebas del contrato de fútbol, sin datos ni red reales.
spec = importlib.util.spec_from_file_location(
    "football_fixture", Path(__file__).with_name("test_football_replay_contract.py")
)
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)

T0 = "2026-10-10T12:00:00+00:00"
T1 = "2026-10-10T12:00:01+00:00"


def scenario():
    return [
        {
            "type": "signal",
            "signal": {
                "schema_version": "botkalshi-paper-signal-v1",
                "signal_id": "m2-1",
                "origin": "M2",
                "cycle_id": "c1",
                "ticker": "SIM-STREAM",
                "side": "yes",
                "limit_price_cents": 50,
                "count": 1,
                "signal_at": T0,
            },
            "book": {
                "ticker": "SIM-STREAM",
                "observed_at": T0,
                "yes_bids": [[49, 1]],
                "no_bids": [[50, 1]],
            },
        },
        {
            "type": "m5_quote",
            "quote": {
                "quote_id": "q1",
                "ticker": "SIM-STREAM",
                "created_at": T0,
                "expires_at": "2026-10-10T12:05:00+00:00",
                "bid_cents": 40,
                "ask_cents": 60,
                "count": 2,
                "fee_type": "quadratic_with_maker_fees",
                "fee_multiplier": "1",
            },
            "book": {
                "ticker": "SIM-STREAM",
                "observed_at": T0,
                "yes_bid": 35,
                "yes_ask": 65,
                "bid_depth": "1",
                "ask_depth": "1",
            },
        },
        {
            "type": "m5_observe",
            "quote_id": "q1",
            "observation_id": "o1",
            "at": T1,
            "book": {
                "ticker": "SIM-STREAM",
                "observed_at": T1,
                "yes_bid": 35,
                "yes_ask": 39,
                "bid_depth": "1",
                "ask_depth": "1",
            },
        },
        {"type": "m5_cancel", "quote_id": "q1", "at": T1},
        {"type": "settle", "ticker": "SIM-STREAM", "yes_payout_cents": 50},
        {"type": "m5_settle", "quote_id": "q1", "yes_payout_cents": 50},
    ]


def run(db, events, **kwargs):
    return stream.run_cycle(db, "100.00", "fixture", "OFFLINE_SCENARIO", events, **kwargs)


def capture():
    payload, series, odds = fixtures.fixture("nfl")
    odds["bookmakers"] = []
    return {
        "type": "football_snapshot",
        "payload": payload,
        "series": series,
        "milestones": payload["milestones"],
        "odds": odds,
        "observed_at": "2026-10-10T12:00:00Z",
        "evaluated_at": "2026-10-10T12:00:30Z",
        "partial_universe": False,
        "source_hashes": ["a" * 64],
    }


def test_append_batches_and_restart_share_fictional_capital_without_duplicates(tmp_path):
    db, events = tmp_path / "sim.db", scenario()
    first = run(db, events[:2])
    assert first["bank"]["reserved_usd"] == "2.16"
    second = run(db, events, batch_size=2)
    assert second["completed_events"] == 4 and second["pending_input_events"] == 2
    final = run(db, events)
    assert final["bank"]["current_capital_usd"] == "100.07"  # M2 -2c; M5 +9c
    assert final["bank"]["reserved_usd"] == "0.00"
    assert final["m2"]["by_origin"]["M2"]["realized_pnl_usd"] == "-0.02"
    again = run(db, events)
    assert again["processed_this_cycle"] == 0 and again["bank"] == final["bank"]


@pytest.mark.parametrize("cut", range(1, 7))
def test_crash_after_any_effect_before_checkpoint_recovers_once(tmp_path, monkeypatch, cut):
    db, events = tmp_path / "sim.db", scenario()
    complete = stream._complete

    def crash(con, sequence, result):
        if sequence == cut:
            raise RuntimeError("injected process death")
        return complete(con, sequence, result)

    monkeypatch.setattr(stream, "_complete", crash)
    with pytest.raises(RuntimeError, match="injected"):
        run(db, events)
    with sqlite3.connect(db) as con:
        assert con.execute(
            "SELECT sequence FROM simulation_ingest_events WHERE state='PENDING'"
        ).fetchall() == [(cut,)]
    monkeypatch.setattr(stream, "_complete", complete)
    result = run(db, events)
    assert result["completed_events"] == 6
    assert result["bank"]["current_capital_usd"] == "100.07"
    assert result["bank"]["reserved_usd"] == "0.00"
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT count(*) FROM simulation_realizations").fetchone()[0] == 2


@pytest.mark.parametrize("change", ["rewrite", "truncate", "identity", "mode"])
def test_history_cannot_be_rewritten_or_relabelled(tmp_path, change):
    db, events = tmp_path / "sim.db", scenario()
    run(db, events)
    before = bank.get_snapshot(db)
    if change == "rewrite":
        events[0]["book"]["no_bids"] = [[51, 1]]
    elif change == "truncate":
        events.pop()
    with pytest.raises(stream.StreamError):
        stream.run_cycle(
            db,
            "100.00",
            "different" if change == "identity" else "fixture",
            "CONNECTED_CAPTURE" if change == "mode" else "OFFLINE_SCENARIO",
            [capture()] if change == "mode" else events,
        )
    assert bank.get_snapshot(db) == before


def test_bad_event_stops_following_events_and_retains_reservations(tmp_path):
    db, events = tmp_path / "sim.db", scenario()
    events[2]["book"]["observed_at"] = T0
    with pytest.raises(SystemExit):
        run(db, events)
    assert bank.get_snapshot(db)["reserved_usd"] == "2.16"
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT sequence,state FROM simulation_ingest_events").fetchall() == [
            (1, "DONE"),
            (2, "DONE"),
            (3, "PENDING"),
        ]
    events[2]["book"]["observed_at"] = T1
    with pytest.raises(stream.StreamError, match="HISTORY"):
        run(db, events)  # never silently edit a pending intent to force it through


def test_connected_capture_is_recorded_blocked_without_fictional_trade(tmp_path):
    db = tmp_path / "connected.db"
    result = stream.run_cycle(db, "100.00", "capture", "CONNECTED_CAPTURE", [capture()])
    row = result["captures"][0]
    assert row["status"] == "BLOCKED" and row["signals"] == row["fills"] == 0
    assert row["blockers"] == [
        "KALSHI_RULE_TEXT_NOT_REVIEWED",
        "NO_SPORTSBOOK_QUOTES",
        "NO_USABLE_QUOTES",
        "SPORTSBOOK_SETTLEMENT_RULES_NOT_PROVIDED",
    ]
    assert result["bank"]["available_usd"] == "100.00"
    assert result["bank"]["reserved_usd"] == "0.00"
    assert result["m2"]["by_origin"] == {}
    assert row["evaluated_at"] == capture()["evaluated_at"]


def test_stale_capture_remains_stale_in_diary(tmp_path):
    event = capture()
    event["evaluated_at"] = "2026-10-10T12:30:00Z"
    result = stream.run_cycle(tmp_path / "c.db", "100", "c", "CONNECTED_CAPTURE", [event])
    assert "ODDS_SNAPSHOT_STALE_OR_FUTURE" in result["captures"][0]["blockers"]


def test_observation_gap_reaches_unknown_and_persists_pause_after_restart(tmp_path):
    db, events = tmp_path / "gap.db", scenario()[:3]
    late = "2026-10-10T12:01:01+00:00"
    events[2]["at"] = events[2]["book"]["observed_at"] = late
    result = run(db, events)
    assert result["bank"]["reserved_usd"] == "2.16"
    replay = run(db, events)
    assert replay["bank"] == result["bank"]
    events.append({"type": "m5_cancel", "quote_id": "q1", "at": late})
    with pytest.raises(SystemExit):
        run(db, events)
    assert bank.get_snapshot(db) == result["bank"]


def test_journal_write_failure_cannot_reach_ledger(tmp_path):
    db = tmp_path / "journal-fail.db"
    run(db, [])
    before = bank.get_snapshot(db)
    with sqlite3.connect(db) as con:
        con.execute("""CREATE TRIGGER injected BEFORE INSERT ON simulation_ingest_events
                       BEGIN SELECT RAISE(ABORT, 'injected journal'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        run(db, scenario())
    assert bank.get_snapshot(db) == before


@pytest.mark.parametrize("batch", [True, 0, 101, 1.5])
def test_invalid_batch_limits_never_open_database(tmp_path, batch):
    db = tmp_path / "invalid.db"
    with pytest.raises(stream.StreamError):
        run(db, scenario(), batch_size=batch)
    assert not db.exists()


@pytest.mark.parametrize("event", [scenario()[0], {"type": "m1_book"}, {"type": "POST"}])
def test_capture_cannot_inject_execution_or_unapproved_motor(tmp_path, event):
    with pytest.raises(stream.StreamError):
        stream.run_cycle(tmp_path / "c.db", "100", "c", "CONNECTED_CAPTURE", [event])
    assert not (tmp_path / "c.db").exists()


def test_one_consumer_lock_excludes_concurrent_writer(tmp_path):
    db = tmp_path / "sim.db"
    with open(str(db) + ".ingest.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            run(db, scenario())
    assert not db.exists()


def test_preexisting_money_cannot_be_relabelled_as_connected_capture(tmp_path):
    db = tmp_path / "prior.db"
    bank.init_bank(db, initial_capital_usd="100.00")
    bank.reserve(db, idempotency_key="prior", origin="M2", cycle_id="past", amount_usd="1.00")
    before = bank.get_snapshot(db)
    with pytest.raises(stream.StreamError, match="EMPTY_SIMULATION_LEDGER"):
        stream.run_cycle(db, "100.00", "c", "CONNECTED_CAPTURE", [capture()])
    assert bank.get_snapshot(db) == before


def write_capture(directory):
    c = capture()
    event = {**c["payload"]["event"], "markets": c["payload"]["markets"]}
    items = [
        ("series.json", "demo", "/series/" + c["series"]["ticker"], {"series": c["series"]}),
        ("events.json", "demo", "/events", {"events": [event], "cursor": "next-page"}),
        ("odds.json", "odds", "/v4/sports/americanfootball_nfl/odds", [c["odds"]]),
        ("milestone.json", None, "/milestones", c["milestones"]),
    ]
    records = []
    for name, provider, route, data in items:
        raw = json.dumps(data).encode()
        (directory / name).write_bytes(raw)
        records.append(
            {
                "file": name,
                "provider": provider,
                "path": route,
                "state": "CAPTURED",
                "status": 200,
                "at": c["observed_at"],
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    (directory / "captura.json").write_text(
        json.dumps(
            {
                "status": "COMPLETE",
                "orders": 0,
                "production_accessed": False,
                "requests": records[:-1],
            }
        )
    )
    records[-1]["event_ticker"] = event["event_ticker"]
    (directory / "milestones-captura.json").write_text(
        json.dumps({"scope": "DEMO_PUBLIC_GET_ONLY", "orders": 0, "requests": records[-1:]})
    )


def test_verified_import_preserves_partial_universe_and_original_clock(tmp_path):
    write_capture(tmp_path)
    events = importer.load_capture(tmp_path)
    assert len(events) == 1 and events[0]["partial_universe"]
    assert events[0]["observed_at"] == capture()["observed_at"]
    result = stream.run_cycle(tmp_path / "sim.db", "100", "capture", "CONNECTED_CAPTURE", events)
    assert result["captures"][0]["signals"] == 0


@pytest.mark.parametrize("change", ["hash", "traversal", "symlink", "duplicate", "incomplete"])
def test_import_rejects_tamper_or_unverifiable_sources_before_ledger(tmp_path, change):
    write_capture(tmp_path)
    path = tmp_path / "captura.json"
    manifest = json.loads(path.read_text())
    if change == "hash":
        (tmp_path / "odds.json").write_text("[]")
    elif change == "traversal":
        manifest["requests"][0]["file"] = "../outside.json"
    elif change == "duplicate":
        manifest["requests"].append(copy.deepcopy(manifest["requests"][0]))
    elif change == "incomplete":
        manifest["requests"][0]["state"] = "ATTEMPTED"
    else:
        (tmp_path / "series.json").unlink()
        (tmp_path / "series.json").symlink_to(tmp_path / "events.json")
    path.write_text(json.dumps(manifest))
    with pytest.raises((stream.StreamError, OSError)):
        importer.load_capture(tmp_path)
