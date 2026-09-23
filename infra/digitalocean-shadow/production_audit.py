"""Read-only AUDIT projection of the production bot's real trade history (S2).

Encargo ASTRA-SUPERVISION-20260923. The production ledger (`trades` in the bot's
SQLite) is the source of truth; this module only READS it (sqlite `mode=ro`), projects
its state into stable audit events, compares, and REPORTS. It has no financial
authority: it never writes the source, never approves or places anything, never touches
the fictional simulation bank (its USD 200 is not money and is never mixed with this).

Projection. A `trades` row mutates over its life (pending → filled → settled, pnl and
fees filled in later), so the audit derives one event per TRANSITION it observes:
PLACED, FILLED, SETTLED, CANCELLED, ERROR. The identity of an event is
sha256(origin | client_order_id | transition): replaying the same source, restarting the
auditor or losing its cursor can never duplicate an event, a cost or a result.

Diagnostics, never silent corrections:
  - CONTRADICTION: an event already recorded is seen with a different payload (the first
    one is kept; both fingerprints are reported), or a terminal trade moves again;
  - STATUS_REGRESSION: a settled trade is later seen as filled/pending;
  - FEE_UNKNOWN: a filled/settled trade with no recorded fee — the past is NOT
    recomputed with today's fee schedule (fees changed on 2026-08-07);
  - INCOMPATIBLE_ROW / INCOMPATIBLE_STATUS: a row that cannot be mapped faithfully
    (missing fill price, unknown status, invalid types) is reported, not invented;
  - ID_GAP: trade ids missing from the source (rows deleted or a partial copy);
  - CURSOR_AHEAD_OF_SOURCE / SOURCE_CHANGED: the source was replaced or truncated
    under the auditor — its cursor is kept, not reset, and the cohort is flagged;
  - SUM_MISMATCH: realized P&L in the audit ≠ realized P&L in the source.
Cursors are isolated by (origin, cohort = strategy, version). Every pass writes the
audit state in ONE transaction: a failure leaves nothing half-written and is reported.
Stdlib only (runs under the droplet's system python3). No network.

Usage (read-only on the source; the audit DB is the auditor's own file):
    python3 production_audit.py --source /path/to/trades.db --state audit.sqlite3
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_REPORT = "botkalshi-production-audit-v1"
PROJECTION_VERSION = "trades-projection-v1"
ORIGIN = "production.trades"
DOMAIN = "PRODUCTION_REAL_READONLY"
_TERMINAL = frozenset({"settled", "cancelled", "error"})
_KNOWN_STATUSES = frozenset({"pending", "filled", "settled", "cancelled", "error"})
_REQUIRED_COLUMNS = (
    "id",
    "client_order_id",
    "ticker",
    "side",
    "action",
    "count",
    "price_cents",
    "strategy",
    "status",
    "fill_price_cents",
    "fees_cents",
    "pnl_cents",
    "closed_by_clv",
    "filled_count",
    "placed_at",
    "filled_at",
    "settled_at",
)
MAX_ROWS_PER_PASS = 50_000
MAX_DIAGNOSTIC_SAMPLES = 20


class AuditError(Exception):
    """The pass could not be completed; nothing was committed."""


def _now() -> datetime:
    return datetime.now(UTC)


def _is_int(value: object) -> bool:
    return type(value) is int


def _event_key(client_order_id: str, transition: str) -> str:
    digest = hashlib.sha256(f"{ORIGIN}|{client_order_id}|{transition}".encode()).hexdigest()
    return digest[:40]


def _fingerprint(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()[:24]


def _state_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS audit_meta (
          key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_cursor (
          origin TEXT NOT NULL, cohort TEXT NOT NULL, version TEXT NOT NULL,
          last_id INTEGER NOT NULL, updated_at TEXT NOT NULL,
          PRIMARY KEY (origin, cohort, version)
        );
        CREATE TABLE IF NOT EXISTS audit_events (
          event_key TEXT PRIMARY KEY, origin TEXT NOT NULL, cohort TEXT NOT NULL,
          version TEXT NOT NULL, client_order_id TEXT NOT NULL, trade_id INTEGER NOT NULL,
          transition TEXT NOT NULL, occurred_at TEXT, payload_json TEXT NOT NULL,
          fingerprint TEXT NOT NULL, first_seen_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_trade_state (
          client_order_id TEXT PRIMARY KEY, cohort TEXT NOT NULL, trade_id INTEGER NOT NULL,
          last_status TEXT NOT NULL, row_fingerprint TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_diagnostics (
          diag_key TEXT PRIMARY KEY, kind TEXT NOT NULL, cohort TEXT, detail_json TEXT NOT NULL,
          first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, count INTEGER NOT NULL
        );
        """
    )


def _open_source(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise AuditError("SOURCE_MISSING")
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        columns = {row[1] for row in con.execute("PRAGMA table_info(trades)")}
    except sqlite3.Error as exc:
        raise AuditError(f"SOURCE_UNREADABLE:{type(exc).__name__}") from exc
    missing = [c for c in _REQUIRED_COLUMNS if c not in columns]
    if missing:
        con.close()
        raise AuditError(f"SOURCE_SCHEMA_INCOMPATIBLE:{','.join(missing)}")
    return con


def _transitions(row: dict[str, Any]) -> tuple[list[tuple[str, str | None, dict]], list[tuple]]:
    """(events, diagnostics) this row implies. Never invents a missing value."""
    events: list[tuple[str, str | None, dict]] = []
    diags: list[tuple] = []
    coid, status = row["client_order_id"], row["status"]
    for field in ("count", "price_cents"):
        if not _is_int(row[field]):
            diags.append(("INCOMPATIBLE_ROW", f"{coid}|{field}", {"field": field}))
            return events, diags
    events.append(
        (
            "PLACED",
            row["placed_at"],
            {k: row[k] for k in ("ticker", "side", "action", "count", "price_cents", "strategy")},
        )
    )
    if status not in _KNOWN_STATUSES:
        diags.append(("INCOMPATIBLE_STATUS", f"{coid}|{status}", {"status": str(status)[:20]}))
        return events, diags
    if status in ("filled", "settled"):
        if not _is_int(row["fill_price_cents"]):
            diags.append(
                (
                    "INCOMPATIBLE_ROW",
                    f"{coid}|fill_price_cents",
                    {"field": "fill_price_cents", "status": status},
                )
            )
        else:
            fee = row["fees_cents"]
            if fee is None:
                diags.append(("FEE_UNKNOWN", coid, {"status": status}))
            elif not _is_int(fee) or fee < 0:
                diags.append(("INCOMPATIBLE_ROW", f"{coid}|fees_cents", {"field": "fees_cents"}))
                fee = None
            filled = row["filled_count"] if row["filled_count"] is not None else row["count"]
            events.append(
                (
                    "FILLED",
                    row["filled_at"],
                    {
                        "fill_price_cents": row["fill_price_cents"],
                        "filled_count": filled if _is_int(filled) else None,
                        "fees_cents": fee,
                        "fee_state": "RECORDED" if fee is not None else "UNKNOWN",
                    },
                )
            )
    if status == "settled":
        pnl = row["pnl_cents"]
        if not _is_int(pnl):
            diags.append(("INCOMPATIBLE_ROW", f"{coid}|pnl_cents", {"field": "pnl_cents"}))
        else:
            events.append(
                (
                    "SETTLED",
                    row["settled_at"],
                    {
                        "pnl_cents": pnl,
                        "closed_by_clv": bool(row["closed_by_clv"]),
                    },
                )
            )
    if status in ("cancelled", "error"):
        events.append((status.upper(), None, {}))
    return events, diags


SCAN_COHORT = "__scan__"  # pagination cursor over NEW rows (by id)
SWEEP_COHORT = "__sweep__"  # rotating re-verification cursor over ALL rows
OPEN_COHORT = "__open__"  # rotating cursor over already-seen non-terminal rows


def _cursor(con: sqlite3.Connection, name: str) -> int:
    row = con.execute(
        "SELECT last_id FROM audit_cursor WHERE origin = ? AND cohort = ? AND version = ?",
        (ORIGIN, name, PROJECTION_VERSION),
    ).fetchone()
    return int(row[0]) if row else 0


def _rows(src: sqlite3.Connection, where: str, params: tuple, limit: int) -> list[dict]:
    return [
        dict(zip(_REQUIRED_COLUMNS, r, strict=True))
        for r in src.execute(
            f"SELECT {', '.join(_REQUIRED_COLUMNS)} FROM trades WHERE {where} ORDER BY id LIMIT ?",
            (*params, limit),
        )
    ]


def audit_pass(
    source: str | Path,
    state: str | Path,
    *,
    now: datetime | None = None,
    max_rows: int = MAX_ROWS_PER_PASS,
) -> dict[str, Any]:
    """One read-only, BOUNDED audit pass. Returns the report; raises AuditError when the
    source cannot be read or the state cannot be written (nothing is committed then).

    Each pass reads at most `max_rows` rows from each of three pages:
      - NEW rows after the persisted scan cursor (id > cursor) — the cursor advances, so a
        backlog larger than one page is consumed across passes, never re-read from row 1;
      - already-seen rows still NON-terminal (pending/filled), which mutate over time;
      - a rotating SWEEP over every row, so a terminal row rewritten later is eventually
        re-verified (contradiction / regression) without scanning the whole table at once.
    Identities are stable, so overlapping pages or a restart never duplicate events."""
    if not (isinstance(max_rows, int) and max_rows >= 1):
        raise AuditError("max_rows must be a positive integer")
    now = (now or _now()).astimezone(UTC)
    stamp = now.isoformat()
    state_path = Path(state)
    con = sqlite3.connect(state_path, timeout=15, isolation_level=None)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        _state_schema(con)
        scan, sweep, open_cursor = (
            _cursor(con, SCAN_COHORT),
            _cursor(con, SWEEP_COHORT),
            _cursor(con, OPEN_COHORT),
        )
        open_ids = [
            r[0]
            for r in con.execute(
                "SELECT trade_id FROM audit_trade_state WHERE last_status NOT IN "
                "('settled','cancelled','error') AND trade_id > ? ORDER BY trade_id LIMIT ?",
                (open_cursor, max_rows + 1),
            )
        ]
    finally:
        con.close()
    open_more = len(open_ids) > max_rows
    open_ids = open_ids[:max_rows]

    src = _open_source(Path(source))
    try:
        new_rows = _rows(src, "id > ?", (scan,), max_rows + 1)
        partial = len(new_rows) > max_rows
        new_rows = new_rows[:max_rows]
        open_rows = (
            _rows(src, f"id IN ({','.join('?' for _ in open_ids)})", tuple(open_ids), max_rows)
            if open_ids
            else []
        )
        sweep_rows = _rows(src, "id > ?", (sweep,), max_rows)
        backlog = (
            src.execute(
                "SELECT COUNT(*) FROM trades WHERE id > ?", (new_rows[-1]["id"],)
            ).fetchone()[0]
            if partial
            else 0
        )
        source_sums = dict(
            src.execute(
                "SELECT strategy, COALESCE(SUM(pnl_cents), 0) FROM trades "
                "WHERE status = 'settled' GROUP BY strategy"
            ).fetchall()
        )
        source_max = dict(src.execute("SELECT strategy, MAX(id) FROM trades GROUP BY strategy"))
        source_max_id = src.execute("SELECT COALESCE(MAX(id), 0) FROM trades").fetchone()[0]
        first_coid = src.execute(
            "SELECT client_order_id FROM trades ORDER BY id LIMIT 1"
        ).fetchone()
    except sqlite3.Error as exc:
        raise AuditError(f"SOURCE_UNREADABLE:{type(exc).__name__}") from exc
    finally:
        src.close()

    diagnostics: list[tuple[str, str, str | None, dict]] = []  # (kind, key, cohort, detail)
    ids = [r["id"] for r in new_rows]
    if ids:
        expected_from = scan + 1 if scan else ids[0]
        missing = sorted(set(range(expected_from, ids[-1] + 1)) - set(ids))
        if missing:
            diagnostics.append(
                (
                    "ID_GAP",
                    f"ids>{scan}",
                    None,
                    {
                        "missing_count": len(missing),
                        "sample": missing[:MAX_DIAGNOSTIC_SAMPLES],
                    },
                )
            )
    if scan > source_max_id:
        diagnostics.append(
            (
                "CURSOR_AHEAD_OF_SOURCE",
                SCAN_COHORT,
                None,
                {
                    "cursor": scan,
                    "source_max": source_max_id,
                },
            )
        )
    if partial:
        diagnostics.append(
            ("PARTIAL_PASS", "rows", None, {"max_rows": max_rows, "backlog_rows": backlog})
        )

    # One row object per id: overlapping pages never project the same row twice.
    by_id = {r["id"]: r for r in (*sweep_rows, *open_rows, *new_rows)}
    rows = [by_id[i] for i in sorted(by_id)]
    cursors = {
        # The scan cursor never moves backwards (a replaced source is flagged, not reset).
        SCAN_COHORT: max(scan, ids[-1]) if ids else scan,
        SWEEP_COHORT: sweep_rows[-1]["id"] if len(sweep_rows) == max_rows else 0,
        OPEN_COHORT: open_ids[-1] if open_more else 0,
    }
    con = sqlite3.connect(state_path, timeout=15, isolation_level=None)
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            report = _apply(
                con, rows, source_sums, source_max, first_coid, diagnostics, stamp, backlog=partial
            )
            for name, value in cursors.items():
                con.execute(
                    "INSERT INTO audit_cursor VALUES (?,?,?,?,?) ON CONFLICT(origin, cohort, version) "
                    "DO UPDATE SET last_id=excluded.last_id, updated_at=excluded.updated_at",
                    (ORIGIN, name, PROJECTION_VERSION, value, stamp),
                )
            con.execute("COMMIT")
        except Exception as exc:
            con.execute("ROLLBACK")
            raise AuditError(f"STATE_WRITE_FAILED:{type(exc).__name__}") from exc
    finally:
        con.close()
    report.update(
        partial=partial,
        backlog_rows=backlog,
        source_rows=len(rows),
        scan_cursor=cursors[SCAN_COHORT],
        sweep_cursor=cursors[SWEEP_COHORT],
    )
    return report


def _apply(
    con, rows, source_sums, source_max, first_coid, diagnostics, stamp, *, backlog: bool
) -> dict[str, Any]:
    meta = dict(con.execute("SELECT key, value FROM audit_meta"))
    identity = first_coid[0] if first_coid else ""
    if meta.get("source_identity") not in (None, identity):
        diagnostics.append(
            (
                "SOURCE_CHANGED",
                "identity",
                None,
                {
                    "was": meta["source_identity"][:64],
                    "now": identity[:64],
                },
            )
        )
    elif "source_identity" not in meta and identity:
        con.execute("INSERT INTO audit_meta VALUES ('source_identity', ?)", (identity,))

    cursors = dict(
        con.execute(
            "SELECT cohort, last_id FROM audit_cursor WHERE origin = ? AND version = ?",
            (ORIGIN, PROJECTION_VERSION),
        )
    )
    max_by_cohort: dict[str, int] = {}
    new_events = replayed = 0
    for row in rows:
        cohort = row["strategy"] if isinstance(row["strategy"], str) else "UNKNOWN"
        max_by_cohort[cohort] = max(max_by_cohort.get(cohort, 0), row["id"])
        prior = con.execute(
            "SELECT last_status, row_fingerprint FROM audit_trade_state WHERE client_order_id = ?",
            (row["client_order_id"],),
        ).fetchone()
        row_fp = _fingerprint({k: row[k] for k in _REQUIRED_COLUMNS})
        if prior is not None and prior[1] == row_fp:
            replayed += 1
            continue  # unchanged since last pass: nothing new to project
        events, diags = _transitions(row)
        for kind, key, detail in diags:
            diagnostics.append((kind, key, cohort, detail))
        if prior is not None:
            was, now_status = prior[0], row["status"]
            if was == "settled" and now_status != "settled":
                diagnostics.append(
                    (
                        "STATUS_REGRESSION",
                        row["client_order_id"],
                        cohort,
                        {"from": was, "to": str(now_status)[:20]},
                    )
                )
            elif was in _TERMINAL and now_status == was:
                diagnostics.append(
                    (
                        "CONTRADICTION",
                        f"{row['client_order_id']}|terminal-row",
                        cohort,
                        {"status": was, "reason": "TERMINAL_ROW_CHANGED"},
                    )
                )
        for transition, occurred, payload in events:
            key = _event_key(row["client_order_id"], transition)
            fp = _fingerprint(payload)
            stored = con.execute(
                "SELECT fingerprint FROM audit_events WHERE event_key = ?", (key,)
            ).fetchone()
            if stored is None:
                con.execute(
                    "INSERT INTO audit_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        key,
                        ORIGIN,
                        cohort,
                        PROJECTION_VERSION,
                        row["client_order_id"],
                        row["id"],
                        transition,
                        occurred,
                        json.dumps(payload, sort_keys=True),
                        fp,
                        stamp,
                    ),
                )
                new_events += 1
            elif stored[0] != fp:
                # Keep the first observation; report the contradiction with both prints.
                diagnostics.append(
                    (
                        "CONTRADICTION",
                        key,
                        cohort,
                        {
                            "transition": transition,
                            "stored": stored[0],
                            "seen": fp,
                        },
                    )
                )
        con.execute(
            "INSERT INTO audit_trade_state VALUES (?,?,?,?,?,?) ON CONFLICT(client_order_id) "
            "DO UPDATE SET last_status=excluded.last_status, row_fingerprint=excluded.row_fingerprint, "
            "updated_at=excluded.updated_at",
            (row["client_order_id"], cohort, row["id"], str(row["status"]), row_fp, stamp),
        )

    special = (SCAN_COHORT, SWEEP_COHORT, OPEN_COHORT)
    cohorts = sorted((set(cursors) | set(max_by_cohort)) - set(special))
    cohort_report = {}
    for cohort in cohorts:
        previous = cursors.get(cohort, 0)
        # Highest id ever projected for this cohort: a pass that only revisits old rows
        # does not move it back. Ahead of the SOURCE's own max ⇒ the source moved under us.
        cursor = max(previous, max_by_cohort.get(cohort, 0))
        src_max = source_max.get(cohort)
        if previous and (src_max is None or src_max < previous):
            diagnostics.append(
                (
                    "CURSOR_AHEAD_OF_SOURCE",
                    cohort,
                    cohort,
                    {
                        "cursor": previous,
                        "source_max": src_max,
                    },
                )
            )
        con.execute(
            "INSERT INTO audit_cursor VALUES (?,?,?,?,?) ON CONFLICT(origin, cohort, version) "
            "DO UPDATE SET last_id=excluded.last_id, updated_at=excluded.updated_at",
            (ORIGIN, cohort, PROJECTION_VERSION, cursor, stamp),
        )
        audit_sum, settled = con.execute(
            "SELECT COALESCE(SUM(json_extract(payload_json, '$.pnl_cents')), 0), COUNT(*) "
            "FROM audit_events WHERE cohort = ? AND transition = 'SETTLED' AND version = ?",
            (cohort, PROJECTION_VERSION),
        ).fetchone()
        src_sum = int(source_sums.get(cohort, 0) or 0)
        if audit_sum != src_sum:
            # With rows still unread the difference is expected, not a contradiction.
            kind = "SUM_PENDING_BACKLOG" if backlog else "SUM_MISMATCH"
            diagnostics.append(
                (
                    kind,
                    cohort,
                    cohort,
                    {
                        "audit_cents": audit_sum,
                        "source_cents": src_sum,
                    },
                )
            )
        cohort_report[cohort] = {
            "cursor_last_id": cursor,
            "settled_events": settled,
            "realized_pnl_cents_audit": audit_sum,
            "realized_pnl_cents_source": src_sum,
        }

    for kind, key, cohort, detail in diagnostics:
        diag_key = hashlib.sha256(f"{kind}|{key}".encode()).hexdigest()[:32]
        con.execute(
            "INSERT INTO audit_diagnostics VALUES (?,?,?,?,?,?,1) ON CONFLICT(diag_key) DO UPDATE "
            "SET last_seen_at=excluded.last_seen_at, count=count+1, detail_json=excluded.detail_json",
            (diag_key, kind, cohort, json.dumps(detail, sort_keys=True), stamp, stamp),
        )
    kinds: dict[str, int] = {}
    for kind, *_ in diagnostics:
        kinds[kind] = kinds.get(kind, 0) + 1
    return {
        "schema_version": SCHEMA_REPORT,
        "domain": DOMAIN,
        "authority": "NONE",
        "origin": ORIGIN,
        "version": PROJECTION_VERSION,
        "audited_at": stamp,
        "new_events": new_events,
        "unchanged_rows": replayed,
        "cohorts": cohort_report,
        "diagnostics": kinds,
        "status": "CLEAN" if not diagnostics else "DIAGNOSTICS",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True, type=Path, help="trades.db (opened mode=ro)")
    parser.add_argument("--state", required=True, type=Path, help="the auditor's own SQLite")
    args = parser.parse_args(argv)
    try:
        report = audit_pass(args.source, args.state)
    except AuditError as exc:
        print(json.dumps({"schema_version": SCHEMA_REPORT, "status": "ERROR", "error": str(exc)}))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
