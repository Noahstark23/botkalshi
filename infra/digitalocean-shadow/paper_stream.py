"""Consumo incremental de un JSONL local, sin red ni ejecutores.

Cada llamada procesa como máximo 100 eventos nuevos; admite un historial de hasta
1.000 eventos/10 MB. El productor sólo agrega líneas completas. Repetir la llamada
reanuda desde el diario SQLite. Una caída entre el efecto contable y el checkpoint
repite el evento pendiente por su identidad original, usando la idempotencia del
ledger. No se salta un error ni se avanza sobre un evento desconocido.

CONNECTED_CAPTURE sólo audita capturas de fútbol y registra sus bloqueos. No
genera propuestas. OFFLINE_SCENARIO acepta propuestas M2/M5 ficticias explícitas.
Cada DB queda ligada a un único stream/modo para no confundir estas evidencias.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import stat
from pathlib import Path

import football_replay_contract as football
import football_settlement_review as settlement_review
import m5_paper_ledger as m5
import paper_engine
import paper_replay
import paper_settlement_snapshot
import simulation_bank as bank
from connected_book_review import inspect_book

MAX_BYTES = 10_000_000
MAX_EVENTS = 1000
MAX_BATCH = 100
MODES = {"CONNECTED_CAPTURE", "OFFLINE_SCENARIO"}
FIELDS = {
    "signal": {"type", "signal", "book"},
    "settle": {"type", "ticker", "result", "yes_payout_cents"},
    "m5_quote": {"type", "quote", "book"},
    "m5_observe": {"type", "quote_id", "observation_id", "at", "book"},
    "m5_cancel": {"type", "quote_id", "at"},
    "m5_settle": {"type", "quote_id", "yes_payout_cents"},
    "settlement_snapshot": {
        "type",
        "target",
        "ticker",
        "rules_sha256",
        "market",
        "observed_at",
        "quote_id",
    },
    "football_snapshot": {
        "type",
        "payload",
        "series",
        "milestones",
        "odds",
        "observed_at",
        "evaluated_at",
        "source_hashes",
        "partial_universe",
        "orderbooks",
    },
}


class StreamError(ValueError):
    """El flujo queda detenido en su último checkpoint durable."""


def read_json(path: Path):
    """Lectura acotada de archivo regular; nunca sigue enlaces ni acepta FIFO."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise StreamError("INPUT_FILE_INVALID")
        raw = stream.read(MAX_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(raw) > MAX_BYTES or (info.st_size, info.st_mtime_ns) != (
        after.st_size,
        after.st_mtime_ns,
    ):
        raise StreamError("INPUT_CHANGED_OR_TOO_LARGE")
    return raw


def _canonical(event, mode):
    if not isinstance(event, dict) or event.get("type") not in FIELDS:
        raise StreamError("EVENT_TYPE_INVALID")
    kind = event["type"]
    if set(event) - FIELDS[kind]:
        raise StreamError("EVENT_FIELDS_INVALID")
    if (kind == "football_snapshot") != (mode == "CONNECTED_CAPTURE"):
        raise StreamError("EVIDENCE_MODE_CONFLICT")
    if kind == "settlement_snapshot":
        # No journaling de una liquidación no final: no deja una línea PENDING
        # irreversible por confundir determinación/revisión con pago definitivo.
        paper_settlement_snapshot.review_snapshot(event)
    if kind == "signal" and (
        not isinstance(event.get("signal"), dict) or event["signal"].get("origin") != "M2"
    ):
        raise StreamError("ONLY_M2_M5_SCENARIOS")
    raw = json.dumps(event, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(raw.encode()) > 1_000_000:
        raise StreamError("EVENT_TOO_LARGE")
    return raw


def _schema(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS simulation_ingest_stream (
      id INTEGER PRIMARY KEY CHECK (id=1), stream_id TEXT NOT NULL, mode TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS simulation_ingest_events (
      sequence INTEGER PRIMARY KEY, payload_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL,
      state TEXT NOT NULL CHECK (state IN ('PENDING','DONE')), result_json TEXT
    );
    """)


def _dispatch(db, capital, event):
    if event["type"] == "settlement_snapshot":
        return paper_settlement_snapshot.apply_to_paper(db, event)
    if event["type"] != "football_snapshot":
        return paper_replay.run(db, capital, [json.dumps(event)])
    hashes = event["source_hashes"]
    if (
        not isinstance(hashes, list)
        or not 1 <= len(hashes) <= 64
        or any(
            not isinstance(h, str) or len(h) != 64 or any(c not in "0123456789abcdef" for c in h)
            for h in hashes
        )
    ):
        raise StreamError("SOURCE_HASHES_INVALID")
    if type(event["partial_universe"]) is not bool:
        raise StreamError("UNIVERSE_SCOPE_INVALID")
    contract = football.inspect_event(
        event["payload"], event["series"], milestones=event["milestones"]
    )
    identity = football.compare_odds(
        contract, event["odds"], observed_at=event["observed_at"], now=event["evaluated_at"]
    )
    books = football.inspect_odds_books(
        contract, event["odds"], observed_at=event["observed_at"], now=event["evaluated_at"]
    )
    rules = settlement_review.review_capture_rules(event["payload"], event["odds"])
    blockers = list(identity["blockers"])
    blockers.extend(rules["blockers"])
    if not books["usable_quote_data_count"]:
        blockers.append("NO_USABLE_QUOTES")
    orderbooks = {}
    if "orderbooks" in event:
        raw_books = event["orderbooks"]
        if not isinstance(raw_books, dict) or set(raw_books) != set(contract["canonical_outcomes"]):
            raise StreamError("ORDERBOOK_TICKER_SCOPE_INVALID")
        for ticker, raw in raw_books.items():
            if not isinstance(raw, dict) or set(raw) != {"payload", "received_at"}:
                raise StreamError("ORDERBOOK_RECORD_INVALID")
            orderbooks[ticker] = inspect_book(
                raw["payload"], received_at=raw["received_at"], evaluated_at=event["evaluated_at"]
            )
            blockers.extend(orderbooks[ticker]["blockers"])
    return {
        "status": "BLOCKED",
        "contract": contract,
        "identity": identity,
        "quote_data": books,
        "settlement_review": rules,
        "orderbooks": orderbooks,
        "blockers": sorted(set(blockers)),
        "evaluated_at": event["evaluated_at"],
        "source_hashes": hashes,
        "partial_universe": event["partial_universe"],
        "evidence": "RECORDED_CAPTURE_CLOCK_NOT_LIVE",
        "signals": 0,
        "fills": 0,
        "execution_authorized": False,
    }


def _complete(con, sequence, result):
    con.execute(
        "UPDATE simulation_ingest_events SET state='DONE', result_json=? WHERE sequence=?",
        (json.dumps(result, sort_keys=True, allow_nan=False), sequence),
    )


def run_cycle(
    db: Path, capital: str, stream_id: str, mode: str, events: list, *, batch_size: int = MAX_BATCH
):
    """Un ciclo acotado; valida el prefijo entero antes de nuevos efectos."""
    bank._parse_id(stream_id, field="stream_id")
    if mode not in MODES or type(batch_size) is not int or not 1 <= batch_size <= MAX_BATCH:
        raise StreamError("MODE_OR_BATCH_INVALID")
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise StreamError("EVENT_COUNT_INVALID")
    canonical = [_canonical(event, mode) for event in events]
    if sum(len(raw.encode()) for raw in canonical) > MAX_BYTES:
        raise StreamError("INPUT_TOO_LARGE")
    hashes = [hashlib.sha256(raw.encode()).hexdigest() for raw in canonical]
    db = Path(db)
    bank._check_path_safety(db)
    db.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(db) + ".ingest.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        bank.init_bank(db, initial_capital_usd=capital)
        with bank._connect(db) as con:
            _schema(con)
            identity = con.execute("SELECT stream_id,mode FROM simulation_ingest_stream").fetchone()
            if identity is not None and identity != (stream_id, mode):
                raise StreamError("STREAM_IDENTITY_CONFLICT")
            if identity is None:
                for table in (
                    "paper_fills",
                    "m5_paper_quotes",
                    "simulation_reservations",
                    "simulation_realizations",
                ):
                    if (
                        con.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                        ).fetchone()
                        and con.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                    ):
                        raise StreamError("NEW_STREAM_REQUIRES_EMPTY_SIMULATION_LEDGER")
            con.execute(
                "INSERT OR IGNORE INTO simulation_ingest_stream VALUES (1,?,?)", (stream_id, mode)
            )
            rows = con.execute(
                "SELECT sequence,payload_sha256,state FROM simulation_ingest_events "
                "ORDER BY sequence"
            ).fetchall()
            for n, (sequence, digest, state) in enumerate(rows, start=1):
                if sequence != n or n > len(hashes) or digest != hashes[n - 1]:
                    raise StreamError("HISTORY_REWRITTEN_OR_TRUNCATED")
                if state == "PENDING" and n != len(rows):
                    raise StreamError("CHECKPOINT_INCONSISTENT")
            completed = sum(r[2] == "DONE" for r in rows)
            end = min(len(events), completed + batch_size)
            for index in range(completed, end):
                # Journal fsync (SQLite FULL) antes del ledger. Después de una caída,
                # se exige exactamente este payload y se recupera por la misma ID.
                con.execute(
                    "INSERT OR IGNORE INTO simulation_ingest_events VALUES (?,?,?,'PENDING',NULL)",
                    (index + 1, hashes[index], canonical[index]),
                )
                result = _dispatch(db, capital, events[index])
                _complete(con, index + 1, result)
            captures = [
                json.loads(row[0])
                for row in con.execute(
                    "SELECT result_json FROM simulation_ingest_events WHERE state='DONE'"
                )
                if mode == "CONNECTED_CAPTURE"
            ]
        return {
            "mode": "SIMULATION_ONLY",
            "evidence_mode": mode,
            "processed_this_cycle": end - completed,
            "completed_events": end,
            "pending_input_events": len(events) - end,
            "captures": captures,
            "bank": bank.get_snapshot(db),
            "m2": paper_engine.summary(db),
            "m5": m5.summary(db),
            "execution_authorized": False,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--capital", required=True)
    parser.add_argument("--stream-id", required=True)
    parser.add_argument("--mode", choices=sorted(MODES), required=True)
    args = parser.parse_args()
    try:
        raw = read_json(args.input)
        if raw and not raw.endswith(b"\n"):
            raise StreamError("INCOMPLETE_LAST_LINE")
        events = [json.loads(line) for line in raw.splitlines()]
        result = run_cycle(args.db, args.capital, args.stream_id, args.mode, events)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        bank.SimulationBankError,
        paper_engine.PaperEngineError,
        m5.LedgerError,
        SystemExit,
    ) as error:
        print(json.dumps({"status": "STOPPED", "error_type": type(error).__name__}))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
