"""Puente offline de capturas US500 a sesiones paper y colector público acotado.

No opera, no autentica y nunca fabrica profundidad a partir de velas o trades.
Cada GET consume presupuesto durable antes de comenzar; una interrupción pausa.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import closing, contextmanager
from decimal import Decimal
from pathlib import Path

from us500_engine import (
    HOSTS,
    MAX_BYTES,
    MAX_PRICE,
    DataError,
    body_digest,
    decode,
    normalize,
    number,
    save_new,
    timestamp,
)
from us500_engine import (
    SCHEMA as SNAPSHOT_SCHEMA,
)
from us500_ledger import LedgerError
from us500_runner import MAX_FRAMES, frame_observation, validate_session
from us500_runner import SCHEMA as SESSION_SCHEMA

COLLECTION_SCHEMA = "us500-public-collection-v1"
MAX_GETS = 128
MAX_PACKAGE_BYTES = 64_000_000
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
COST_FIELDS = {
    "from_ms",
    "at_ms",
    "funding_cashflow_usd",
    "infrastructure_cost_usd",
    "provenance_note",
}
TABLES = {"us500_collection_meta", "us500_capture", "us500_request"}


def _id(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise DataError("INVALID_COLLECTION_IDENTIFIER")
    return value


def _json(value):
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise DataError("INVALID_SESSION_JSON") from exc


def _parse(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DataError("DUPLICATE_SESSION_JSON_KEY")
            result[key] = value
        return result

    def reject(value):
        raise DataError("NONFINITE_SESSION_JSON")

    try:
        return json.loads(
            raw, object_pairs_hook=unique, parse_float=Decimal, parse_constant=reject
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise DataError("INVALID_SESSION_JSON") from exc


def load_package(path):
    with Path(path).open("rb") as source:
        raw = source.read(MAX_PACKAGE_BYTES + 1)
    if len(raw) > MAX_PACKAGE_BYTES:
        raise DataError("SESSION_PACKAGE_TOO_LARGE")
    return _parse(raw)


def _snapshot_times(snapshot):
    try:
        observations = snapshot["observations"]
        if not isinstance(observations, dict) or set(observations) != {
            "market",
            "orderbook",
        }:
            raise DataError("MARKET_AND_ORDERBOOK_REQUIRED")
        starts, ends = [], []
        for resource in ("market", "orderbook"):
            observation = observations[resource]
            if not isinstance(observation, dict):
                raise DataError("MALFORMED_SESSION_OBSERVATION")
            starts.append(timestamp(observation["requested_at_ms"]))
            ends.append(timestamp(observation["received_at_ms"]))
        return min(starts), max(ends)
    except (KeyError, TypeError, IndexError) as exc:
        raise DataError("MALFORMED_SESSION_SNAPSHOT") from exc


def _metadata(snapshot):
    try:
        market = snapshot["observations"]["market"]["body"]["market"]
        identifier = market["market_id"]
        if not isinstance(identifier, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", identifier
        ):
            raise DataError("MARKET_ID_REQUIRED")
        version, index = market["market_version"], market["exchange_index"]
        if (
            type(version) is not int
            or version < 1
            or type(index) is not int
            or not 0 <= index < 2**31
        ):
            raise DataError("INVALID_MARKET_VERSION_OR_EXCHANGE")
        if market["asset_class"] != "Indices":
            raise DataError("US500_INDEX_METADATA_REQUIRED")
        return {
            "environment": snapshot["environment"],
            "ticker": snapshot["ticker"],
            "market_id": identifier,
            "market_version": version,
            "exchange_index": index,
            "asset_class": "Indices",
            "tick_size": market["tick_size"],
            "contract_size": market["contract_size"],
            "underlying_multiplier": market["underlying_multiplier"],
            "fractional_trading_enabled": market["fractional_trading_enabled"],
        }
    except (KeyError, TypeError, IndexError) as exc:
        raise DataError("INSTRUMENT_METADATA_REQUIRED") from exc


def _check_mark(snapshot, at):
    try:
        mark = snapshot["observations"]["market"]["body"]["market"][
            "liquidation_mark_price"
        ]
        price = number(mark["price"], positive=True)
        age = at - timestamp(mark["ts_ms"])
        if price >= MAX_PRICE or not 0 <= age <= 5000:
            raise DataError("MISSING_OR_STALE_SESSION_MARK")
    except (KeyError, TypeError, IndexError) as exc:
        raise DataError("LIQUIDATION_MARK_REQUIRED") from exc


def _valid_snapshot(snapshot):
    earliest, at = _snapshot_times(snapshot)
    observation = normalize(snapshot, at_ms=at)
    if not observation["payload_integrity_checked"]:
        raise DataError("SNAPSHOT_DIGEST_REQUIRED")
    if (
        snapshot.get("authenticated") is not False
        or snapshot.get("atomic_snapshot") is not False
    ):
        raise DataError("ANONYMOUS_NONATOMIC_CAPTURE_REQUIRED")
    for claim in (
        "source_authenticity_verified",
        "strategy_validated",
        "execution_available",
    ):
        if snapshot.get(claim, False) is not False:
            raise DataError("UNSUPPORTED_SNAPSHOT_AUTHORITY_CLAIM")
    _check_mark(snapshot, at)
    metadata = _metadata(snapshot)
    return earliest, at, observation, metadata


def build_session(snapshots, ledger_config, strategy, interval_costs, run_id):
    """Congela entradas explícitas; comprueba integridad, identidad, fechas y gaps.

    interval_costs usa exactamente COST_FIELDS. Primer from_ms=at_ms; después,
    from_ms es el at_ms anterior. Ceros de funding/costos nunca se completan solos.
    """
    _id(run_id)
    if not isinstance(snapshots, list) or not 1 <= len(snapshots) <= MAX_FRAMES:
        raise DataError("INVALID_SESSION_SNAPSHOT_COUNT")
    if not isinstance(interval_costs, list) or len(interval_costs) != len(snapshots):
        raise DataError("ONE_EXPLICIT_COST_PER_INTERVAL_REQUIRED")
    frames, previous_at, previous_metadata, previous_synthetic = [], None, None, None
    for source_snapshot, supplied in zip(snapshots, interval_costs):
        snapshot = copy.deepcopy(source_snapshot)
        if (
            not isinstance(snapshot, dict)
            or "session_interval_cost_evidence" in snapshot
        ):
            raise DataError("INVALID_OR_ALREADY_ASSEMBLED_SNAPSHOT")
        earliest, at, observation, metadata = _valid_snapshot(snapshot)
        if previous_at is not None:
            if at <= previous_at or earliest <= previous_at:
                raise DataError("REUSED_OR_OVERLAPPING_CAPTURE")
            if (
                not isinstance(ledger_config, dict)
                or type(ledger_config.get("max_gap_ms")) is not int
            ):
                raise DataError("EXPLICIT_MAX_GAP_REQUIRED")
            if at - previous_at > ledger_config["max_gap_ms"]:
                raise DataError("SESSION_OBSERVATION_GAP")
            if metadata != previous_metadata:
                raise DataError("INSTRUMENT_METADATA_CHANGED")
            if observation["synthetic"] != previous_synthetic:
                raise DataError("MIXED_SYNTHETIC_AND_OBSERVED_SNAPSHOTS")
        if not isinstance(supplied, dict) or set(supplied) != COST_FIELDS:
            raise DataError("EXPLICIT_INTERVAL_COST_EVIDENCE_REQUIRED")
        cost = copy.deepcopy(supplied)
        if timestamp(cost["at_ms"]) != at or timestamp(cost["from_ms"]) != (
            previous_at if previous_at is not None else at
        ):
            raise DataError("INTERVAL_COST_TIME_MISMATCH")
        funding = number(cost["funding_cashflow_usd"])
        infrastructure = number(cost["infrastructure_cost_usd"])
        if infrastructure < 0:
            raise DataError("NEGATIVE_INFRASTRUCTURE_COST")
        if previous_at is None and funding != 0:
            raise DataError("INITIAL_INTERVAL_FUNDING_MUST_BE_EXPLICIT_ZERO")
        if (
            not isinstance(cost["provenance_note"], str)
            or not 1 <= len(cost["provenance_note"]) <= 1000
        ):
            raise DataError("INTERVAL_COST_PROVENANCE_REQUIRED")
        snapshot["session_interval_cost_evidence"] = cost
        frames.append(
            {
                "at_ms": at,
                "snapshot": snapshot,
                "funding_cashflow_usd": cost["funding_cashflow_usd"],
                "infrastructure_cost_usd": cost["infrastructure_cost_usd"],
            }
        )
        previous_at, previous_metadata, previous_synthetic = (
            at,
            metadata,
            observation["synthetic"],
        )
    session = {
        "schema": SESSION_SCHEMA,
        "run_id": run_id,
        "mode": "paper",
        "ledger": copy.deepcopy(ledger_config),
        "strategy": copy.deepcopy(strategy),
        "frames": frames,
        "source_authenticity_verified": False,
        "strategy_validated": False,
        "execution_available": False,
        "assembly": {
            "schema": "us500-session-assembly-v1",
            "instrument_metadata": copy.deepcopy(previous_metadata),
            "instrument_metadata_sha256": body_digest(previous_metadata),
            "synthetic_input": previous_synthetic,
            "observation_claim": "synthetic"
            if previous_synthetic
            else "observed_unverified_origin",
            "funding_and_costs": "explicit_caller_inputs_not_account_statements",
        },
    }
    try:
        validate_session(session)
        identity = None
        for frame in frames:
            _, identity, _ = frame_observation(frame, session["ledger"], identity)
    except LedgerError as exc:
        raise DataError("INVALID_SESSION_LEDGER_CONFIG") from exc
    return session


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DataError("COLLECTION_REDIRECT_REJECTED")


class SessionCollector:
    """Un snapshot por llamada; cap global fijado en el journal, sin reintentos.

    Repetir un observation_id completado devuelve la misma observación guardada.
    Un GET reservado sin resultado confirmado pausa tras reinicio y no se reenvía.
    """

    def __init__(
        self,
        journal,
        *,
        collection_id,
        environment="public",
        max_gets=8,
        transport=None,
        clock_ms=None,
    ):
        self.path = Path(journal)
        if str(journal) == ":memory:" or self.path.is_symlink():
            raise DataError("DURABLE_COLLECTION_PATH_REQUIRED")
        if environment not in HOSTS:
            raise DataError("INVALID_COLLECTION_ENVIRONMENT")
        if type(max_gets) is not int or not 1 <= max_gets <= MAX_GETS:
            raise DataError("INVALID_GLOBAL_GET_BUDGET")
        self.config = {
            "schema": COLLECTION_SCHEMA,
            "collection_id": _id(collection_id),
            "environment": environment,
            "max_gets": max_gets,
        }
        self.transport = (
            transport
            if transport is not None
            else urllib.request.build_opener(
                urllib.request.ProxyHandler({}), _NoRedirect()
            )
        )
        self.clock_ms = (
            clock_ms if clock_ms is not None else lambda: time.time_ns() // 1_000_000
        )
        with self._connection():
            pass

    @contextmanager
    def _connection(self):
        if self.path.is_symlink():
            raise DataError("DURABLE_COLLECTION_PATH_REQUIRED")
        lockpath = Path(str(self.path) + ".lock")
        fd = os.open(lockpath, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DataError("COLLECTION_BUSY") from exc
            if self.path.exists():
                try:
                    with closing(
                        sqlite3.connect(
                            f"{self.path.resolve().as_uri()}?mode=ro", uri=True
                        )
                    ) as existing:
                        names = {
                            r[0]
                            for r in existing.execute(
                                "SELECT name FROM sqlite_master WHERE type='table'"
                            )
                        }
                        if names != TABLES:
                            raise DataError("JOURNAL_NOT_OWNED_BY_COLLECTOR")
                        config = _parse(
                            existing.execute(
                                "SELECT config FROM us500_collection_meta WHERE id=1"
                            ).fetchone()[0]
                        )
                        if config != self.config:
                            raise DataError("COLLECTION_CONFIG_CHANGED")
                except (sqlite3.Error, TypeError, IndexError) as exc:
                    raise DataError("INVALID_COLLECTION_JOURNAL") from exc
            else:
                self._initialize()
            with closing(sqlite3.connect(self.path)) as connection:
                connection.execute("PRAGMA synchronous=FULL")
                reserved = connection.execute(
                    "SELECT reserved_gets FROM us500_collection_meta WHERE id=1"
                ).fetchone()[0]
                request_count = connection.execute(
                    "SELECT COUNT(*) FROM us500_request"
                ).fetchone()[0]
                if (
                    type(reserved) is not int
                    or reserved != request_count
                    or not 0 <= reserved <= self.config["max_gets"]
                ):
                    raise DataError("GLOBAL_GET_ACCOUNTING_CONFLICT")
                pending = connection.execute(
                    "SELECT COUNT(*) FROM us500_capture WHERE status='IN_PROGRESS'"
                ).fetchone()[0]
                if pending:
                    with connection:
                        connection.execute(
                            "UPDATE us500_collection_meta SET status='PAUSED', reason='INTERRUPTED_CAPTURE_NO_RETRY' WHERE id=1"
                        )
                        connection.execute(
                            "UPDATE us500_capture SET status='UNKNOWN', reason='INTERRUPTED_CAPTURE_NO_RETRY' WHERE status='IN_PROGRESS'"
                        )
                yield connection
        finally:
            os.close(fd)

    def _initialize(self):
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".us500-collection-", dir=self.path.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with closing(sqlite3.connect(temporary)) as connection:
                connection.executescript("""
                    CREATE TABLE us500_collection_meta(id INTEGER PRIMARY KEY CHECK(id=1), config TEXT NOT NULL, status TEXT NOT NULL, reason TEXT, reserved_gets INTEGER NOT NULL);
                    CREATE TABLE us500_capture(observation_id TEXT PRIMARY KEY, status TEXT NOT NULL, snapshot TEXT, snapshot_hash TEXT, reason TEXT);
                    CREATE TABLE us500_request(request_id INTEGER PRIMARY KEY, observation_id TEXT NOT NULL, resource TEXT NOT NULL, status TEXT NOT NULL, observation TEXT, reason TEXT);
                """)
                connection.execute(
                    "INSERT INTO us500_collection_meta VALUES(1,?,'OPEN',NULL,0)",
                    (_json(self.config),),
                )
                connection.commit()
            with temporary.open("rb") as source:
                os.fsync(source.fileno())
            os.link(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def _pause(self, connection, observation_id, reason):
        with connection:
            connection.execute(
                "UPDATE us500_collection_meta SET status='PAUSED',reason=? WHERE id=1",
                (reason,),
            )
            connection.execute(
                "UPDATE us500_capture SET status='FAILED',reason=? WHERE observation_id=?",
                (reason, observation_id),
            )

    def _get(self, connection, observation_id, resource):
        if resource not in ("market", "orderbook"):
            raise DataError("COLLECTION_RESOURCE_NOT_ALLOWED")
        with connection:
            used = connection.execute(
                "SELECT reserved_gets FROM us500_collection_meta WHERE id=1"
            ).fetchone()[0]
            if used >= self.config["max_gets"]:
                raise DataError("GLOBAL_GET_BUDGET_EXHAUSTED")
            connection.execute(
                "UPDATE us500_collection_meta SET reserved_gets=reserved_gets+1 WHERE id=1"
            )
            row = connection.execute(
                "INSERT INTO us500_request(observation_id,resource,status) VALUES(?,?,'RESERVED')",
                (observation_id, resource),
            )
            request_id = row.lastrowid
        host, ticker = HOSTS[self.config["environment"]]
        suffix = "" if resource == "market" else "/orderbook?depth=20"
        url = f"{host}/trade-api/v2/margin/markets/{ticker}{suffix}"
        started = timestamp(self.clock_ms())
        request = urllib.request.Request(
            url, headers={"Accept": "application/json"}, method="GET"
        )
        try:
            with self.transport.open(request, timeout=5) as response:
                if response.status != 200:
                    raise DataError("COLLECTION_NON_200_RESPONSE")
                raw = response.read(MAX_BYTES + 1)
            body = decode(raw)
            observation = {
                "url": url,
                "http_status": 200,
                "requested_at_ms": started,
                "received_at_ms": timestamp(self.clock_ms()),
                "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "canonical_body_sha256": body_digest(body),
                "body": body,
            }
        except urllib.error.HTTPError as exc:
            error = DataError(f"COLLECTION_HTTP_{exc.code}")
        except (OSError, urllib.error.URLError):
            error = DataError("COLLECTION_NETWORK_ERROR")
        except DataError as exc:
            error = exc
        else:
            with connection:
                connection.execute(
                    "UPDATE us500_request SET status='RESPONSE_SAVED',observation=? WHERE request_id=?",
                    (_json(observation), request_id),
                )
            return observation
        with connection:
            connection.execute(
                "UPDATE us500_request SET status='FAILED',reason=? WHERE request_id=?",
                (str(error), request_id),
            )
        raise error

    def capture_next(self, observation_id):
        _id(observation_id)
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT status,snapshot,snapshot_hash FROM us500_capture WHERE observation_id=?",
                (observation_id,),
            ).fetchone()
            if existing and existing[0] == "COMPLETE":
                saved = _parse(existing[1])
                if body_digest(saved) != existing[2]:
                    raise DataError("SAVED_SNAPSHOT_DIGEST_MISMATCH")
                _valid_snapshot(saved)
                return saved
            status, reason, used = connection.execute(
                "SELECT status,reason,reserved_gets FROM us500_collection_meta WHERE id=1"
            ).fetchone()
            count = connection.execute("SELECT COUNT(*) FROM us500_request").fetchone()[
                0
            ]
            if used != count or not 0 <= used <= self.config["max_gets"]:
                raise DataError("GLOBAL_GET_ACCOUNTING_CONFLICT")
            if status != "OPEN" or existing:
                raise DataError("COLLECTION_STOPPED")
            if self.config["max_gets"] - used < 2:
                with connection:
                    connection.execute(
                        "UPDATE us500_collection_meta SET status='EXHAUSTED',reason='INSUFFICIENT_BUDGET_FOR_SNAPSHOT' WHERE id=1"
                    )
                raise DataError("INSUFFICIENT_BUDGET_FOR_SNAPSHOT")
            with connection:
                connection.execute(
                    "INSERT INTO us500_capture(observation_id,status) VALUES(?,'IN_PROGRESS')",
                    (observation_id,),
                )
            try:
                observations = {
                    "market": self._get(connection, observation_id, "market")
                }
                try:
                    market = observations["market"]["body"]["market"]
                    if market["ticker"] != HOSTS[self.config["environment"]][1]:
                        raise DataError("COLLECTION_MARKET_TICKER_MISMATCH")
                    if (
                        market["status"] != "active"
                        or market["schedule"]["is_open"] is not True
                    ):
                        raise DataError("COLLECTION_MARKET_CLOSED")
                except (KeyError, TypeError, IndexError) as exc:
                    raise DataError("MALFORMED_COLLECTION_MARKET") from exc
                observations["orderbook"] = self._get(
                    connection, observation_id, "orderbook"
                )
                snapshot = {
                    "schema": SNAPSHOT_SCHEMA,
                    "environment": self.config["environment"],
                    "ticker": HOSTS[self.config["environment"]][1],
                    "synthetic": False,
                    "authenticated": False,
                    "atomic_snapshot": False,
                    "source_authenticity_verified": False,
                    "observations": observations,
                }
                _valid_snapshot(snapshot)
                prior = connection.execute(
                    "SELECT snapshot FROM us500_capture WHERE status='COMPLETE' ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
                if prior:
                    prior_snapshot = _parse(prior[0])
                    earliest, at = _snapshot_times(snapshot)
                    _, prior_at = _snapshot_times(prior_snapshot)
                    if earliest <= prior_at or at <= prior_at:
                        raise DataError("REUSED_OR_OVERLAPPING_CAPTURE")
                    if _metadata(prior_snapshot) != _metadata(snapshot):
                        raise DataError("INSTRUMENT_METADATA_CHANGED")
                with connection:
                    connection.execute(
                        "UPDATE us500_capture SET status='COMPLETE',snapshot=?,snapshot_hash=? WHERE observation_id=?",
                        (_json(snapshot), body_digest(snapshot), observation_id),
                    )
                return snapshot
            except DataError as exc:
                self._pause(connection, observation_id, str(exc))
                raise

    def export(self):
        with self._connection() as connection:
            status, reason, used = connection.execute(
                "SELECT status,reason,reserved_gets FROM us500_collection_meta WHERE id=1"
            ).fetchone()
            captures = []
            for (
                identifier,
                state,
                snapshot,
                digest,
                capture_reason,
            ) in connection.execute(
                "SELECT observation_id,status,snapshot,snapshot_hash,reason FROM us500_capture ORDER BY rowid"
            ):
                parsed = _parse(snapshot) if snapshot is not None else None
                if parsed is not None and body_digest(parsed) != digest:
                    raise DataError("SAVED_SNAPSHOT_DIGEST_MISMATCH")
                captures.append(
                    {
                        "observation_id": identifier,
                        "status": state,
                        "snapshot": parsed,
                        "snapshot_sha256": digest,
                        "reason": capture_reason,
                    }
                )
            requests = [
                {
                    "request_id": row[0],
                    "observation_id": row[1],
                    "resource": row[2],
                    "status": row[3],
                    "observation": _parse(row[4]) if row[4] else None,
                    "reason": row[5],
                }
                for row in connection.execute(
                    "SELECT request_id,observation_id,resource,status,observation,reason FROM us500_request ORDER BY request_id"
                )
            ]
            return {
                **self.config,
                "status": status,
                "stop_reason": reason,
                "reserved_gets": used,
                "remaining_gets": self.config["max_gets"] - used,
                "captures": captures,
                "requests": requests,
                "source_authenticity_verified": False,
                "strategy_validated": False,
                "execution_available": False,
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    assemble = commands.add_parser("assemble")
    assemble.add_argument("--input", type=Path, required=True)
    assemble.add_argument("--output", type=Path, required=True)
    capture = commands.add_parser("capture-one")
    capture.add_argument("--journal", type=Path, required=True)
    capture.add_argument("--collection-id", required=True)
    capture.add_argument("--observation-id", required=True)
    capture.add_argument("--environment", choices=tuple(HOSTS), default="public")
    capture.add_argument("--max-gets", type=int, default=8)
    capture.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.output.exists() or args.output.is_symlink():
            raise DataError("OUTPUT_ALREADY_EXISTS")
        if args.command == "assemble":
            package = load_package(args.input)
            if not isinstance(package, dict) or set(package) != {
                "snapshots",
                "ledger_config",
                "strategy",
                "interval_costs",
                "run_id",
            }:
                raise DataError("INVALID_ASSEMBLY_PACKAGE")
            result = build_session(**package)
        else:
            collector = SessionCollector(
                args.journal,
                collection_id=args.collection_id,
                environment=args.environment,
                max_gets=args.max_gets,
            )
            try:
                collector.capture_next(args.observation_id)
            except DataError as exc:
                result = collector.export()
                result["attempt_error"] = str(exc)
                save_new(args.output, result)
                return 2
            result = collector.export()
        save_new(args.output, result)
        print(
            json.dumps(
                {
                    "saved": str(args.output),
                    "execution_available": False,
                    "strategy_validated": False,
                }
            )
        )
        return 0
    except (DataError, LedgerError, OSError, sqlite3.Error, TypeError) as exc:
        print(
            json.dumps(
                {
                    "error": str(exc)
                    if isinstance(exc, DataError)
                    else "SESSION_LOCAL_ERROR"
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
