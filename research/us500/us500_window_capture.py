"""Bounded public US500 window capture, with one durable attempt per time slot.

No account credentials, order methods, retry loop, trading, or scheduler installation.
The CLI performs one step; an external timer may invoke it with the same plan.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path

from us500_engine import (
    HOSTS,
    MAX_BYTES,
    MAX_PRICE,
    SCHEMA,
    DataError,
    PublicReader,
    body_digest,
    decode,
    instrument_metadata,
    json_text,
    normalize,
    number,
    timestamp,
)

PLAN_SCHEMA = "us500-public-window-plan-v1"
MAX_PLAN_BYTES = 16_384
MAX_SLOTS = 1_561
MAX_STORAGE_BYTES = 256 * 1024 * 1024
# A compact capture holds two bounded public response bodies plus small envelopes.
MAX_CAPTURE_BYTES = 2 * MAX_BYTES + 64_000
DATABASE_BYTES = 4 * 1024 * 1024
# Keep room for SQLite and its rollback journal, as well as fixed local metadata.
INTERNAL_STORAGE_RESERVE = 2 * DATABASE_BYTES + 64_000
CADENCE_MS = 60_000
PLAN_FIELDS = {
    "schema",
    "collection_id",
    "environment",
    "ticker",
    "start_ms",
    "end_ms",
    "deadline_ms",
    "cadence_ms",
    "max_slots",
    "max_gets",
    "max_storage_bytes",
}
TABLES = {"window_meta", "window_slots"}
_KNOWN_ERRORS = {
    "PUBLIC_DATA_UNAVAILABLE",
    "NON_200_RESPONSE",
    "RESPONSE_TOO_LARGE",
    "INVALID_JSON",
    "DUPLICATE_JSON_KEY",
    "NONFINITE_JSON",
    "JSON_OBJECT_REQUIRED",
    "REDIRECT_REJECTED",
    "INVALID_WINDOW_MARKET",
    "INVALID_WINDOW_SOURCE",
    "INVALID_CAPTURE_CLOCK",
    "BODY_DIGEST_MISMATCH",
    "CAPTURE_OUTSIDE_SLOT",
    "INSTRUMENT_CHANGED_DURING_WINDOW",
    "INVALID_EPOCH_MILLISECONDS",
    "MISSING_OR_STALE_WINDOW_MARK",
    "DEADLINE_REACHED",
    "STORAGE_LIMIT",
    "INVALID_CANONICAL_BODY",
    "INVALID_JSON_SERIALIZATION",
    "LOCAL_STORAGE_ERROR",
    "SLOT_TIME_EXPIRED",
}


def safe_error(error):
    text = str(error) if isinstance(error, DataError) else ""
    if text in _KNOWN_ERRORS or re.fullmatch(r"HTTP_[45][0-9]{2}", text):
        return text
    return "WINDOW_CAPTURE_FAILED"


def validate_plan(plan):
    if type(plan) is not dict or set(plan) != PLAN_FIELDS:
        raise DataError("INVALID_WINDOW_PLAN_FIELDS")
    if (
        plan["schema"] != PLAN_SCHEMA
        or plan["environment"] != "public"
        or plan["ticker"] != HOSTS["public"][1]
        or not isinstance(plan["collection_id"], str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", plan["collection_id"])
    ):
        raise DataError("INVALID_PUBLIC_WINDOW_PLAN")
    for key in ("start_ms", "end_ms", "deadline_ms"):
        timestamp(plan[key])
    for key in ("cadence_ms", "max_slots", "max_gets", "max_storage_bytes"):
        if type(plan[key]) is not int:
            raise DataError("INVALID_WINDOW_PLAN_INTEGER")
    start, end, deadline = plan["start_ms"], plan["end_ms"], plan["deadline_ms"]
    if (
        plan["cadence_ms"] != CADENCE_MS
        or start % CADENCE_MS
        or end % CADENCE_MS
        or not start < end <= deadline <= start + 26 * 60 * 60 * 1000
    ):
        raise DataError("INVALID_WINDOW_TIME_RANGE")
    slots = (end - start) // CADENCE_MS
    if not 1 <= slots <= MAX_SLOTS or plan["max_slots"] != slots:
        raise DataError("INVALID_WINDOW_SLOT_BUDGET")
    if plan["max_gets"] != 2 * slots:
        raise DataError("INVALID_WINDOW_GET_BUDGET")
    if not (
        INTERNAL_STORAGE_RESERVE + MAX_CAPTURE_BYTES
        <= plan["max_storage_bytes"]
        <= MAX_STORAGE_BYTES
    ):
        raise DataError("INVALID_WINDOW_STORAGE_BUDGET")
    return json.loads(json.dumps(plan, sort_keys=True))


def validate_snapshot(snapshot, *, at_ms):
    """Classify a retained capture; no missing book/mark is fabricated."""
    try:
        observation = normalize(snapshot, at_ms=at_ms)
        if not observation["payload_integrity_checked"]:
            raise DataError("BODY_DIGEST_MISMATCH")
        metadata = instrument_metadata(snapshot)
        mark = snapshot["observations"]["market"]["body"]["market"][
            "liquidation_mark_price"
        ]
        price = number(mark["price"], positive=True)
        age = at_ms - timestamp(mark["ts_ms"])
        if price >= MAX_PRICE or not 0 <= age <= 5000:
            raise DataError("MISSING_OR_STALE_WINDOW_MARK")
        return {"usable": True, "reason": None, "metadata": metadata}
    except (DataError, KeyError, TypeError, IndexError):
        # Rejected bodies remain in evidence; this public status contains no raw data.
        return {"usable": False, "reason": "INVALID_SNAPSHOT", "metadata": None}


class WindowCapture:
    """One immutable public plan, exclusive directory, no backfill or retry.

    Reserved GET budget is never refunded, including a slot that uses only one GET.
    An interrupted slot becomes UNKNOWN on reopening; only later slots may proceed.
    end_ms is exclusive: expected slot starts lie in [start_ms, end_ms).
    """

    def __init__(self, directory, plan, *, reader_factory=None, clock_ms=None):
        self.plan = validate_plan(plan)
        self.plan_hash = body_digest(self.plan)
        self.directory = Path(directory).absolute()
        self.reader_factory = reader_factory or (lambda: PublicReader("public"))
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._db = None
        self._lock = None
        if self.directory.is_symlink():
            raise DataError("WINDOW_DIRECTORY_SYMLINK")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self._lock = os.open(
                self.directory / "window.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            try:
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DataError("WINDOW_BUSY") from exc
            self._check_files()
            self.path = self.directory / "window.sqlite"
            if not self.path.exists():
                self._initialize()
            with closing(
                sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)
            ) as check:
                tables = {
                    r[0]
                    for r in check.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if tables != TABLES:
                    raise DataError("FOREIGN_WINDOW_DATABASE")
                row = check.execute(
                    "SELECT plan, plan_sha256 FROM window_meta WHERE id=1"
                ).fetchone()
                if (
                    not row
                    or row[0] != json_text(self.plan, sort_keys=True)
                    or row[1] != self.plan_hash
                ):
                    raise DataError("WINDOW_PLAN_CONFLICT")
            self._db = sqlite3.connect(self.path, isolation_level=None, timeout=5)
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute(f"PRAGMA max_page_count={DATABASE_BYTES // 4096}")
            with self._transaction():
                self._db.execute(
                    "UPDATE window_slots SET status='INTERRUPTED_UNKNOWN', reason='INTERRUPTED_CAPTURE_NO_RETRY' WHERE status='IN_PROGRESS'"
                )
            self._check_evidence_or_stop()
        except BaseException:
            self.close()
            raise

    def _check_files(self):
        for path in self.directory.iterdir():
            if path.is_symlink() or not path.is_file():
                raise DataError("INVALID_WINDOW_FILE")
            if path.name not in {
                "window.lock",
                "window.sqlite",
                "window.sqlite-journal",
                "status.json",
                "status.pending",
            } and not re.fullmatch(r"slot-[0-9]{4}\.json", path.name):
                raise DataError("FOREIGN_WINDOW_DIRECTORY")
            maximum = (
                MAX_PLAN_BYTES
                if path.name in {"status.json", "status.pending"}
                else DATABASE_BYTES
                if path.name == "window.sqlite"
                else DATABASE_BYTES + 32_000
                if path.name == "window.sqlite-journal"
                else 0
                if path.name == "window.lock"
                else MAX_CAPTURE_BYTES
            )
            if path.stat().st_size > maximum:
                raise DataError("STORAGE_LIMIT")

    def _initialize(self):
        if any(p.name != "window.lock" for p in self.directory.iterdir()):
            raise DataError("FOREIGN_WINDOW_DIRECTORY")
        descriptor, name = tempfile.mkstemp(prefix=".window-init-", dir=self.directory)
        os.close(descriptor)
        temporary = Path(name)
        try:
            with closing(sqlite3.connect(temporary)) as db:
                db.execute("PRAGMA page_size=4096")
                db.execute("PRAGMA synchronous=FULL")
                db.execute(
                    "CREATE TABLE window_meta(id INTEGER PRIMARY KEY CHECK(id=1), plan TEXT NOT NULL, plan_sha256 TEXT NOT NULL, reserved_gets INTEGER NOT NULL, attempted_gets INTEGER NOT NULL, status TEXT NOT NULL, reason TEXT, metadata_sha256 TEXT, last_now_ms INTEGER)"
                )
                db.execute(
                    "CREATE TABLE window_slots(slot INTEGER PRIMARY KEY, at_ms INTEGER NOT NULL, status TEXT NOT NULL, reason TEXT, capture_sha256 TEXT, capture_bytes INTEGER NOT NULL DEFAULT 0)"
                )
                db.execute(
                    "INSERT INTO window_meta VALUES(1,?,?,0,0,'ACTIVE',NULL,NULL,NULL)",
                    (json_text(self.plan, sort_keys=True), self.plan_hash),
                )
                db.executemany(
                    "INSERT INTO window_slots(slot,at_ms,status) VALUES(?,?,'WAITING')",
                    [
                        (i, self.plan["start_ms"] + i * CADENCE_MS)
                        for i in range(self.plan["max_slots"])
                    ],
                )
                db.commit()
            with temporary.open("rb") as saved:
                os.fsync(saved.fileno())
            os.link(temporary, self.path)
            self._sync_directory()
        finally:
            temporary.unlink(missing_ok=True)

    def _sync_directory(self):
        descriptor = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @contextmanager
    def _transaction(self):
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def close(self):
        if self._db is not None:
            self._db.close()
            self._db = None
        if self._lock is not None:
            os.close(self._lock)
            self._lock = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def _storage_available(self):
        self._check_files()
        captures = sum(p.stat().st_size for p in self.directory.glob("slot-*.json"))
        return (
            captures + INTERNAL_STORAGE_RESERVE + MAX_CAPTURE_BYTES
            <= self.plan["max_storage_bytes"]
        )

    def _terminal(self, reason):
        with self._transaction():
            self._db.execute(
                "UPDATE window_meta SET status='INCOMPLETE', reason=? WHERE id=1",
                (reason,),
            )
            self._db.execute(
                "UPDATE window_slots SET status='NOT_COLLECTED', reason=? WHERE status='WAITING'",
                (reason,),
            )

    def _attempt_get(self, reader, resource, slot_at):
        now = timestamp(self.clock_ms())
        if now >= self.plan["deadline_ms"]:
            raise DataError("DEADLINE_REACHED")
        if not slot_at <= now < slot_at + CADENCE_MS:
            raise DataError("SLOT_TIME_EXPIRED")
        with self._transaction():
            self._db.execute(
                "UPDATE window_meta SET attempted_gets=attempted_gets+1 WHERE id=1"
            )
        # There is deliberately no open SQLite transaction across HTTP.
        return reader.get(resource)

    def _validate_market(self, observation, slot_at):
        try:
            if (
                observation["url"]
                != f"{HOSTS['public'][0]}/trade-api/v2/margin/markets/{self.plan['ticker']}"
                or type(observation["http_status"]) is not int
                or observation["http_status"] != 200
            ):
                raise DataError("INVALID_WINDOW_SOURCE")
            if observation["canonical_body_sha256"] != body_digest(observation["body"]):
                raise DataError("BODY_DIGEST_MISMATCH")
            start, end = (
                timestamp(observation["requested_at_ms"]),
                timestamp(observation["received_at_ms"]),
            )
            if not slot_at <= start <= end < slot_at + CADENCE_MS:
                raise DataError("CAPTURE_OUTSIDE_SLOT")
            if end - start > 5000:
                raise DataError("INVALID_CAPTURE_CLOCK")
            market = observation["body"]["market"]
            if (
                market["ticker"] != self.plan["ticker"]
                or type(market["schedule"]["is_open"]) is not bool
            ):
                raise DataError("INVALID_WINDOW_MARKET")
            snapshot = self._snapshot({"market": observation})
            tick = number(market["tick_size"], positive=True)
            number(market["contract_size"], positive=True)
            number(market["underlying_multiplier"], positive=True)
            if (
                tick >= MAX_PRICE
                or type(market["fractional_trading_enabled"]) is not bool
            ):
                raise DataError("INVALID_WINDOW_MARKET")
            metadata_hash = body_digest(instrument_metadata(snapshot))
            old = self._db.execute(
                "SELECT metadata_sha256 FROM window_meta WHERE id=1"
            ).fetchone()[0]
            if old is not None and old != metadata_hash:
                raise DataError("INSTRUMENT_CHANGED_DURING_WINDOW")
            with self._transaction():
                self._db.execute(
                    "UPDATE window_meta SET metadata_sha256=? WHERE id=1",
                    (metadata_hash,),
                )
            return market["schedule"]["is_open"]
        except (KeyError, TypeError, IndexError) as exc:
            raise DataError("INVALID_WINDOW_MARKET") from exc

    def _snapshot(self, observations):
        return {
            "schema": SCHEMA,
            "environment": "public",
            "ticker": self.plan["ticker"],
            "synthetic": False,
            "authenticated": False,
            "atomic_snapshot": False,
            "source_authenticity_verified": False,
            "strategy_validated": False,
            "execution_available": False,
            "observations": observations,
        }

    def _save(self, slot, record):
        encoded = (json_text(record, sort_keys=True, ensure_ascii=False) + "\n").encode(
            "utf-8"
        )
        if len(encoded) > MAX_CAPTURE_BYTES:
            raise DataError("STORAGE_LIMIT")
        path = self.directory / f"slot-{slot:04d}.json"
        with path.open("xb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        self._sync_directory()
        return hashlib.sha256(encoded).hexdigest(), len(encoded)

    def _verify_completed_files(self):
        for slot, digest, size in self._db.execute(
            "SELECT slot,capture_sha256,capture_bytes FROM window_slots WHERE capture_sha256 IS NOT NULL"
        ):
            path = self.directory / f"slot-{slot:04d}.json"
            if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
                raise DataError("WINDOW_EVIDENCE_CHANGED")
            with path.open("rb") as source:
                raw = source.read(MAX_CAPTURE_BYTES + 1)
            if (
                len(raw) > MAX_CAPTURE_BYTES
                or hashlib.sha256(raw).hexdigest() != digest
            ):
                raise DataError("WINDOW_EVIDENCE_CHANGED")

    def _check_evidence_or_stop(self):
        try:
            self._verify_completed_files()
            return True
        except (DataError, OSError):
            self._terminal("EVIDENCE_INTEGRITY_FAILURE")
            return False

    def step(self, now_ms):
        now = timestamp(now_ms)
        if not self._check_evidence_or_stop():
            return self.snapshot()
        metadata = self._db.execute(
            "SELECT status,last_now_ms FROM window_meta WHERE id=1"
        ).fetchone()
        if metadata[0] != "ACTIVE":
            return self.snapshot()
        if metadata[1] is not None and now < metadata[1]:
            self._terminal("CLOCK_REGRESSION")
            return self.snapshot()
        with self._transaction():
            self._db.execute("UPDATE window_meta SET last_now_ms=? WHERE id=1", (now,))
            self._db.execute(
                "UPDATE window_slots SET status='INTERRUPTED_UNKNOWN',reason='INTERRUPTED_CAPTURE_NO_RETRY' WHERE status='IN_PROGRESS'"
            )
        if now < self.plan["start_ms"]:
            return self.snapshot()
        if now >= self.plan["end_ms"] or now >= self.plan["deadline_ms"]:
            with self._transaction():
                self._db.execute(
                    "UPDATE window_slots SET status='MISSED',reason='MISSED_SLOT_NO_BACKFILL' WHERE status='WAITING'"
                )
            self._terminal(
                "WINDOW_ENDED" if now < self.plan["deadline_ms"] else "DEADLINE_REACHED"
            )
            return self.snapshot()
        slot = (now - self.plan["start_ms"]) // CADENCE_MS
        with self._transaction():
            self._db.execute(
                "UPDATE window_slots SET status='MISSED', reason='MISSED_SLOT_NO_BACKFILL' WHERE slot<? AND status='WAITING'",
                (slot,),
            )
        prior = self._db.execute(
            "SELECT status,at_ms FROM window_slots WHERE slot=?", (slot,)
        ).fetchone()
        if prior[0] != "WAITING":
            return self.snapshot()
        if not self._storage_available():
            self._terminal("STORAGE_LIMIT")
            return self.snapshot()
        with self._transaction():
            reserved = self._db.execute(
                "SELECT reserved_gets FROM window_meta WHERE id=1"
            ).fetchone()[0]
            if reserved + 2 > self.plan["max_gets"]:
                raise DataError("WINDOW_BUDGET_EXHAUSTED")
            self._db.execute(
                "UPDATE window_meta SET reserved_gets=reserved_gets+2 WHERE id=1"
            )
            self._db.execute(
                "UPDATE window_slots SET status='IN_PROGRESS' WHERE slot=?", (slot,)
            )
        observations = {}
        status, reason, terminal = "INVALID", None, None
        try:
            reader = self.reader_factory()
            observations["market"] = self._attempt_get(reader, "market", prior[1])
            if not self._validate_market(observations["market"], prior[1]):
                status, reason, terminal = "CLOSED", "MARKET_CLOSED", "MARKET_CLOSED"
            else:
                observations["orderbook"] = self._attempt_get(
                    reader, "orderbook", prior[1]
                )
                ends = [timestamp(o["received_at_ms"]) for o in observations.values()]
                starts = [
                    timestamp(o["requested_at_ms"]) for o in observations.values()
                ]
                if min(starts) < prior[1] or max(ends) >= prior[1] + CADENCE_MS:
                    raise DataError("CAPTURE_OUTSIDE_SLOT")
                checked = validate_snapshot(
                    self._snapshot(observations), at_ms=max(ends)
                )
                status, reason = (
                    ("USABLE", None)
                    if checked["usable"]
                    else ("INVALID", checked["reason"])
                )
        except Exception as exc:
            status, reason = "INVALID", safe_error(exc)
            if reason == "DEADLINE_REACHED":
                terminal = reason
        record = {
            "schema": "us500-window-slot-v1",
            "plan_sha256": self.plan_hash,
            "slot": slot,
            "slot_at_ms": prior[1],
            "status": status,
            "reason": reason,
            "snapshot": self._snapshot(observations),
        }
        try:
            digest, size = self._save(slot, record)
        except (OSError, DataError):
            with self._transaction():
                self._db.execute(
                    "UPDATE window_slots SET status='INTERRUPTED_UNKNOWN', reason='EVIDENCE_WRITE_FAILED_NO_RETRY' WHERE slot=?",
                    (slot,),
                )
            self._terminal("LOCAL_STORAGE_ERROR")
            return self.snapshot()
        with self._transaction():
            self._db.execute(
                "UPDATE window_slots SET status=?,reason=?,capture_sha256=?,capture_bytes=? WHERE slot=?",
                (status, reason, digest, size, slot),
            )
        if terminal:
            self._terminal(terminal)
        elif slot == self.plan["max_slots"] - 1:
            with self._transaction():
                usable = self._db.execute(
                    "SELECT count(*) FROM window_slots WHERE status='USABLE'"
                ).fetchone()[0]
                self._db.execute(
                    "UPDATE window_meta SET status=?,reason='ALL_SLOTS_FINISHED' WHERE id=1",
                    ("COMPLETE" if usable == self.plan["max_slots"] else "INCOMPLETE",),
                )
        return self.snapshot()

    def snapshot(self):
        integrity = self._check_evidence_or_stop()
        row = self._db.execute(
            "SELECT reserved_gets,attempted_gets,status,reason,metadata_sha256 FROM window_meta WHERE id=1"
        ).fetchone()
        counts = dict(
            self._db.execute("SELECT status,count(*) FROM window_slots GROUP BY status")
        )
        full = integrity and counts.get("USABLE", 0) == self.plan["max_slots"]
        return {
            "schema": "us500-public-window-status-v1",
            "collection_id": self.plan["collection_id"],
            "plan_sha256": self.plan_hash,
            "status": row[2],
            "reason": row[3],
            "expected_slots": self.plan["max_slots"],
            "slot_counts": counts,
            "reserved_gets": row[0],
            "attempted_gets": row[1],
            "unused_reserved_gets": row[0] - row[1],
            "max_gets": self.plan["max_gets"],
            "instrument_metadata_sha256": row[4],
            "full_planned_slot_coverage": full,
            "retained_artifacts_integrity_checked": integrity,
            "slot_counts_meaning": "classification_when_saved",
            "continuous_market_history_verified": False,
            "source_authenticity_verified": False,
            "independence_verified": False,
            "strategy_validated": False,
            "execution_available": False,
            "actual_orders": 0,
        }

    def write_status(self, *, at_ms):
        """Atomic bounded status for read-only observers, while holding this lock."""
        status = {**self.snapshot(), "status_written_at_ms": timestamp(at_ms)}
        encoded = (json_text(status, sort_keys=True) + "\n").encode("utf-8")
        if len(encoded) > MAX_PLAN_BYTES:
            raise DataError("STORAGE_LIMIT")
        self._check_files()
        temporary = self.directory / "status.pending"
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self.directory / "status.json")
        self._sync_directory()
        return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--once", action="store_true", required=True)
    args = parser.parse_args()
    try:
        with args.plan.open("rb") as source:
            raw = source.read(MAX_PLAN_BYTES + 1)
        if len(raw) > MAX_PLAN_BYTES:
            raise DataError("WINDOW_PLAN_TOO_LARGE")
        with WindowCapture(args.directory, decode(raw)) as collector:
            result = collector.step(time.time_ns() // 1_000_000)
            result = collector.write_status(at_ms=time.time_ns() // 1_000_000)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] in {"ACTIVE", "COMPLETE"} else 2
    except (DataError, OSError, sqlite3.Error):
        print(json.dumps({"error": "WINDOW_STEP_FAILED", "execution_available": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
