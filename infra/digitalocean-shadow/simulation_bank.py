"""Shared SIMULATION_ONLY capital ledger. No real money, account or execution path.

This module is a small, independent SQLite ledger for FICTIONAL test capital shared
across shadow motors during P2. It has no Kalshi client, no account/balance reader, no
`risk-input.json` integration and no order capability — M1/M5/Radar/M2/M3 are wired to
it in a later unit (P3), not here. Every snapshot this module returns is explicit about
that: `mode=SIMULATION_ONLY`, `capital_source=FICTIONAL_TEST_CAPITAL`, and the three
authority fields are always False.

Persistence lives in a SQLite file at a path the caller supplies explicitly (stdlib
`sqlite3` only) using the repo's WAL + busy_timeout + explicit-transaction pattern.
Every public function opens its own short-lived connection and either commits a single
atomic transaction or rolls back — importing this module never opens a DB or a socket.

Contract:
  - `init_bank` requires an explicit fictional capital; repeating it with the SAME
    amount is a no-op, repeating it with a DIFFERENT amount raises (fail-closed —
    capital is never silently redefined and there is no reset).
  - `reserve` requires `idempotency_key`, `origin`, `cycle_id` and `amount_usd`. Replaying
    the same key with the same payload returns the prior result without charging twice;
    replaying the same key with a different payload raises. New reservations are
    admitted only inside a `BEGIN IMMEDIATE` transaction that re-checks
    `sum(active) + amount <= capital`, so concurrent callers can never push
    reserved above capital.
  - `reserve_with_evidence` is `reserve` plus a caller-supplied evidence object
    persisted in the SAME transaction as the reservation row — never two writes,
    never a reservation with no evidence or evidence with no reservation. Replay
    rules are identical to `reserve`, extended to the evidence: the SAME key with
    the SAME (origin, cycle_id, amount_usd, evidence) is a no-op; any field
    differing — including evidence — raises. `reserve` and `reserve_with_evidence`
    share one locked code path, so both are visible through the same
    `active_reservations` (evidence is `None` for reservations made via `reserve`).
  - `release` is idempotent by `idempotency_key`; it frees a reservation and never
    manufactures balance. Releasing an unknown key is a fail-closed error, not a no-op.
  - `revision` increments once per real state change (init, a new reservation, a first
    release, a new accounting event) and never for an idempotent replay.
  - `record_fill` / `record_settlement` (C1/6.4, 2026-09-22) append SIMULATED accounting
    events to `simulation_events`, the ONE source of truth for positions and realized
    P&L. The projection is derived on every read, never stored. Policy in
    `ACCOUNTING_POLICY`: FIFO price lots per (origin, position_key), fees expensed once
    when paid, settlement at 0/100 per YES. Linked reservations are released only when
    the position is flat or settled, in the same transaction as the closing event.
    Snapshots add `realized_pnl_usd`, `capital_usd` (= initial + realized),
    `available_usd` (= capital − reserved) and `positions`; an open position's
    `valuation` is `UNKNOWN_NO_MARK`, never a guessed price.
  - A reservation is an earmark, not a close and not a gain: releasing one never
    changes realized P&L.
"""
from __future__ import annotations

import contextlib
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

SCHEMA_VERSION = "botkalshi-simulation-bank-v1"

_MONEY_RE = re.compile(r"(?:0|[1-9][0-9]{0,9})(?:\.[0-9]{1,2})?")
_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_MAX_MONEY_LEN = 32
_MAX_EVIDENCE_JSON_BYTES = 4_096


class SimulationBankError(Exception):
    """Base error. Every raise here means no partial state was committed."""


class SimulationBankValidationError(SimulationBankError):
    """Malformed input: not the caller's fault to retry without fixing the payload."""


class SimulationBankConflictError(SimulationBankError):
    """An idempotency key (or the bank itself) was reused with a different payload."""


class SimulationBankInsufficientFundsError(SimulationBankError):
    """The reservation would push reserved_usd above the fictional capital."""


class SimulationBankNotInitializedError(SimulationBankError):
    """No bank row exists yet; call init_bank first."""


class SimulationBankIntegrityError(SimulationBankError):
    """Stored accounting events do not replay into a valid state. Fail closed: a
    corrupted history is never silently skipped or partially folded."""


class SimulationBankNotFoundError(SimulationBankError):
    """release() referenced an idempotency_key that was never reserved."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _usd(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}{whole}.{frac:02d}"


def _parse_cents(value: Any, *, field: str) -> int:
    # Fixed-point strings only: rejects floats/bools/NaN/inf/ints outright, and the
    # regex bounds both decimal places (<=2) and digit count (blocks huge strings).
    if not isinstance(value, str) or len(value) > _MAX_MONEY_LEN:
        raise SimulationBankValidationError(
            f"{field} must be a bounded fixed-point USD string"
        )
    if _MONEY_RE.fullmatch(value) is None:
        raise SimulationBankValidationError(
            f"{field} must be a non-negative amount with at most 2 decimals"
        )
    whole, _, frac = value.partition(".")
    cents = int(whole) * 100 + int((frac + "00")[:2])
    if cents <= 0:
        raise SimulationBankValidationError(f"{field} must be greater than zero")
    return cents


def _parse_id(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise SimulationBankValidationError(f"{field} must be a bounded identifier")
    return value


def _canonical_evidence_json(value: Any) -> str:
    if not isinstance(value, dict):
        raise SimulationBankValidationError("evidence must be an object")
    try:
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise SimulationBankValidationError("evidence must be JSON-serializable") from exc
    if len(canonical.encode("utf-8")) > _MAX_EVIDENCE_JSON_BYTES:
        raise SimulationBankValidationError("evidence exceeds bounded size")
    return canonical


def _check_path_safety(db_path: Path) -> None:
    # is_symlink() uses lstat and does not require the link target to exist, so this
    # also catches dangling symlinks — exists() alone would follow the link and miss them.
    if db_path.is_symlink():
        raise SimulationBankValidationError("db path must not be a symlink")
    if db_path.parent.is_symlink():
        raise SimulationBankValidationError("db parent directory must not be a symlink")


def _ensure_schema(con: sqlite3.Connection) -> None:
    # Unchanged P2 shape: an existing P2 DB already has these two tables and
    # CREATE TABLE IF NOT EXISTS is a no-op for it — the evidence column is
    # added below by an explicit, idempotent ALTER so P2 files migrate in
    # place instead of needing a reset.
    con.executescript("""
    CREATE TABLE IF NOT EXISTS simulation_bank (
      id INTEGER PRIMARY KEY CHECK (id = 1),
      initial_capital_cents INTEGER NOT NULL,
      revision INTEGER NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS simulation_reservations (
      idempotency_key TEXT PRIMARY KEY,
      origin TEXT NOT NULL,
      cycle_id TEXT NOT NULL,
      amount_cents INTEGER NOT NULL,
      status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'RELEASED')),
      created_at TEXT NOT NULL,
      released_at TEXT
    );
    -- C1/6.4 (2026-09-22): the ONE source of simulated accounting events. Positions,
    -- realized P&L and capital are DERIVED from these rows on every read (folded in
    -- seq order) and never stored, so there is no second book that can diverge and a
    -- restart is just a re-read. Added with IF NOT EXISTS: an existing bank file gains
    -- the table in place and keeps every prior row.
    CREATE TABLE IF NOT EXISTS simulation_events (
      seq INTEGER PRIMARY KEY AUTOINCREMENT,
      event_key TEXT NOT NULL UNIQUE,
      origin TEXT NOT NULL,
      position_key TEXT NOT NULL,
      kind TEXT NOT NULL CHECK (kind IN ('FILL', 'SETTLEMENT')),
      side TEXT CHECK (side IN ('buy', 'sell')),
      price_cents INTEGER,
      count INTEGER,
      fee_cents INTEGER,
      payout_cents INTEGER,
      reservation_key TEXT,
      evidence_json TEXT,
      created_at TEXT NOT NULL
    );
    """)
    columns = {row[1] for row in con.execute("PRAGMA table_info(simulation_reservations)").fetchall()}
    if "evidence_json" not in columns:
        try:
            con.execute("ALTER TABLE simulation_reservations ADD COLUMN evidence_json TEXT")
        except sqlite3.OperationalError as exc:
            # Two connections can race this check-then-add outside BEGIN IMMEDIATE;
            # the loser's ALTER fails on the column the winner already added, which
            # is the intended outcome, not a real error.
            if "duplicate column name" not in str(exc):
                raise


@contextlib.contextmanager
def _connect(db_path: Path):
    _check_path_safety(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=15, isolation_level=None)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        con.execute("PRAGMA busy_timeout=15000")
        _ensure_schema(con)
        yield con
    finally:
        con.close()


# --------------------------------------------------------------------------------------
# Accounting (C1/6.4). Policy, versioned and explicit — changing any rule below means a new
# ACCOUNTING_POLICY value, never a silent reinterpretation of stored events:
#   - Lots are FIFO by price, per (origin, position_key). A position never nets against
#     another motor's position on the same market: origin is part of the identity.
#   - A fill that opposes the open lots closes them first (FIFO); any remainder opens a
#     lot on the other side (sign reversal). Realized gain per closed contract is
#     (fill_price - lot_price) * lot_sign, in integer cents.
#   - Fees are recognized as realized expense ONCE, when paid, in integer cents. This
#     avoids any fractional allocation of an entry fee across partial exits.
#   - A SETTLEMENT pays 0 or 100 cents per YES contract and closes every open lot.
#   - Reservations linked to a position's fills are released only when the position is
#     flat or settled, in the SAME transaction that records the closing event. A partial
#     close keeps them (conservative: never frees capital before the exit is complete).
#   - Releasing a reservation changes availability, never realized P&L. An open position
#     has no stored mark here, so its valuation is UNKNOWN — never marked at 50 or at a
#     guessed price, and never counted as a gain.
# --------------------------------------------------------------------------------------

ACCOUNTING_POLICY = "FIFO_PRICE_LOTS_FEES_EXPENSED_WHEN_PAID_V1"
_SIDES = ("buy", "sell")
_SETTLEMENT_PAYOUTS = (0, 100)
_MIN_PRICE_CENTS = 1
_MAX_PRICE_CENTS = 99
_MAX_COUNT = 1_000_000
_MAX_FEE_CENTS = 100_000_000


class _Position:
    __slots__ = ("lots", "settled", "realized_cents", "reservation_keys")

    def __init__(self) -> None:
        self.lots: list[list[int]] = []  # [sign (+1 long YES / -1 short YES), price, qty]
        self.settled = False
        self.realized_cents = 0
        self.reservation_keys: list[str] = []

    def net(self) -> int:
        return sum(sign * qty for sign, _price, qty in self.lots)


def _apply_fill(pos: _Position, *, side: str, price: int, count: int, fee: int) -> None:
    if pos.settled:
        raise SimulationBankValidationError("POSITION_SETTLED: no fill after settlement")
    sign = 1 if side == "buy" else -1
    remaining = count
    while remaining and pos.lots and pos.lots[0][0] == -sign:
        lot = pos.lots[0]
        closed = min(remaining, lot[2])
        pos.realized_cents += (price - lot[1]) * closed * lot[0]
        lot[2] -= closed
        remaining -= closed
        if lot[2] == 0:
            pos.lots.pop(0)
    if remaining:
        pos.lots.append([sign, price, remaining])
    pos.realized_cents -= fee


def _apply_settlement(pos: _Position, *, payout: int) -> None:
    if pos.settled:
        raise SimulationBankValidationError("POSITION_SETTLED: already settled")
    if not pos.lots:
        raise SimulationBankValidationError("NOTHING_TO_SETTLE: settlement without an open position")
    for sign, price, qty in pos.lots:
        pos.realized_cents += (payout - price) * qty * sign
    pos.lots = []
    pos.settled = True


_EVENT_COLUMNS = (
    "origin, position_key, kind, side, price_cents, count, fee_cents, payout_cents, "
    "reservation_key, evidence_json"
)


def _apply_row(pos: _Position, kind: str, side, price, count, fee, payout, reservation_key) -> None:
    if kind == "FILL":
        _apply_fill(pos, side=side, price=price, count=count, fee=fee)
        if reservation_key is not None:
            pos.reservation_keys.append(reservation_key)
    else:
        _apply_settlement(pos, payout=payout)


def _fold(con: sqlite3.Connection) -> dict[tuple[str, str], _Position]:
    """Replay every stored event in seq order. Deterministic; raises on a history that
    does not replay (a corrupted row is never skipped)."""
    positions: dict[tuple[str, str], _Position] = {}
    rows = con.execute(
        "SELECT seq, origin, position_key, kind, side, price_cents, count, fee_cents, "
        "payout_cents, reservation_key FROM simulation_events ORDER BY seq"
    ).fetchall()
    for seq, origin, position_key, kind, side, price, count, fee, payout, reservation_key in rows:
        pos = positions.setdefault((origin, position_key), _Position())
        try:
            _apply_row(pos, kind, side, price, count, fee, payout, reservation_key)
        except (SimulationBankValidationError, TypeError) as exc:
            raise SimulationBankIntegrityError(
                f"stored event seq={seq} does not replay: {exc}"
            ) from exc
    return positions


def _realized_cents(positions: dict[tuple[str, str], _Position]) -> int:
    return sum(pos.realized_cents for pos in positions.values())


def _position_view(key: tuple[str, str], pos: _Position) -> dict[str, Any]:
    return {
        "origin": key[0],
        "position_key": key[1],
        "net_contracts": pos.net(),
        "open_lots": [
            {"side": "long_yes" if sign > 0 else "short_yes", "price_cents": price, "count": qty}
            for sign, price, qty in pos.lots
        ],
        "settled": pos.settled,
        "realized_pnl_usd": _usd(pos.realized_cents),
        # No mark is stored here: an open position's value is unknown, not 50 and not 0.
        "valuation": "CLOSED" if not pos.lots else "UNKNOWN_NO_MARK",
    }


def _snapshot(con: sqlite3.Connection) -> dict[str, Any]:
    row = con.execute(
        "SELECT initial_capital_cents, revision, created_at, updated_at "
        "FROM simulation_bank WHERE id = 1"
    ).fetchone()
    if row is None:
        raise SimulationBankNotInitializedError("bank has not been initialized")
    capital_cents, revision, created_at, updated_at = row
    reserved_cents = con.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM simulation_reservations WHERE status = 'ACTIVE'"
    ).fetchone()[0]
    active = con.execute(
        "SELECT idempotency_key, origin, cycle_id, amount_cents, created_at, evidence_json "
        "FROM simulation_reservations WHERE status = 'ACTIVE' "
        "ORDER BY created_at, idempotency_key"
    ).fetchall()
    active_reservations = []
    for key, origin, cycle_id, amount_cents, reserved_at, evidence_json in active:
        active_reservations.append({
            "idempotency_key": key,
            "origin": origin,
            "cycle_id": cycle_id,
            "amount_usd": _usd(amount_cents),
            "created_at": reserved_at,
            # None for rows written by plain reserve() (P2 callers, or the P2
            # rows that predate the evidence_json migration on this file).
            "evidence": json.loads(evidence_json) if evidence_json is not None else None,
        })
    positions = _fold(con)
    realized_cents = _realized_cents(positions)
    # capital = initial fictional capital + realized P&L. Reserved is an earmark on it,
    # not a loss. With no events this is exactly the pre-accounting snapshot.
    capital_now_cents = capital_cents + realized_cents
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "SIMULATION_ONLY",
        "capital_source": "FICTIONAL_TEST_CAPITAL",
        "execution_authorized": False,
        "order_capability_present": False,
        "real_entry_eligible": False,
        "initial_capital_usd": _usd(capital_cents),
        "realized_pnl_usd": _usd(realized_cents),
        "capital_usd": _usd(capital_now_cents),
        "reserved_usd": _usd(reserved_cents),
        "available_usd": _usd(capital_now_cents - reserved_cents),
        "accounting_policy": ACCOUNTING_POLICY,
        "revision": revision,
        "created_at": created_at,
        "updated_at": updated_at,
        "active_reservations": active_reservations,
        "positions": [_position_view(key, positions[key]) for key in sorted(positions)],
    }


def init_bank(db_path: str | Path, *, initial_capital_usd: str) -> dict[str, Any]:
    """Create the shared bank with explicit fictional capital, or confirm it matches.

    Idempotent for the SAME amount (no-op, revision unchanged). A DIFFERENT amount is a
    conflict and blocks (SimulationBankConflictError) — capital is never redefined
    silently and there is no automatic reset.
    """
    path = Path(db_path)
    capital_cents = _parse_cents(initial_capital_usd, field="initial_capital_usd")
    with _connect(path) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            row = con.execute(
                "SELECT initial_capital_cents FROM simulation_bank WHERE id = 1"
            ).fetchone()
            if row is None:
                now = _now()
                con.execute(
                    "INSERT INTO simulation_bank "
                    "(id, initial_capital_cents, revision, created_at, updated_at) "
                    "VALUES (1, ?, 1, ?, ?)",
                    (capital_cents, now, now),
                )
            elif row[0] != capital_cents:
                raise SimulationBankConflictError(
                    "bank already initialized with a different fictional capital"
                )
            result = _snapshot(con)
            con.execute("COMMIT")
            return result
        except Exception:
            con.execute("ROLLBACK")
            raise


def _reserve_locked(
    con: sqlite3.Connection,
    *,
    key: str,
    origin_value: str,
    cycle_value: str,
    amount_cents: int,
    evidence_json: str | None,
) -> str:
    """The one locked reservation code path shared by `reserve` and
    `reserve_with_evidence`. Caller already holds BEGIN IMMEDIATE. Replay of the
    same key compares the FULL payload including evidence_json — for plain
    `reserve` that is always None on both sides, so its replay rule is
    unaffected; for `reserve_with_evidence` a differing evidence is a conflict
    exactly like a differing amount.

    Returns what actually happened, read inside this same transaction:
    "CREATED", "REPLAY_ACTIVE" or "REPLAY_RELEASED". An identical replay of a
    RELEASED key stays a no-op — it is never reactivated — but it must not be
    reported as if it had reserved anything (C1/6.2, 2026-09-22): a caller that
    inferred "reserved" from the absence of an exception reported a released
    reservation as active.
    """
    bank_row = con.execute(
        "SELECT initial_capital_cents FROM simulation_bank WHERE id = 1"
    ).fetchone()
    if bank_row is None:
        raise SimulationBankNotInitializedError("bank has not been initialized")
    capital_cents = bank_row[0]
    existing = con.execute(
        "SELECT origin, cycle_id, amount_cents, evidence_json, status FROM simulation_reservations "
        "WHERE idempotency_key = ?",
        (key,),
    ).fetchone()
    if existing is not None:
        if existing[:4] != (origin_value, cycle_value, amount_cents, evidence_json):
            raise SimulationBankConflictError(
                "idempotency_key already used with a different reservation payload"
            )
        # Idempotent replay: identical payload (evidence included), no new
        # charge, no revision bump — and a RELEASED row stays RELEASED.
        return "REPLAY_ACTIVE" if existing[4] == "ACTIVE" else "REPLAY_RELEASED"
    reserved_cents = con.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM simulation_reservations "
        "WHERE status = 'ACTIVE'"
    ).fetchone()[0]
    # Admission sees capital AFTER realized P&L: a realized loss must shrink what can
    # still be reserved, or it would be invisible to the next risk. No events → the
    # original check exactly.
    capital_cents += _realized_cents(_fold(con))
    if amount_cents > capital_cents - reserved_cents:
        raise SimulationBankInsufficientFundsError(
            "reservation would exceed available capital"
        )
    now = _now()
    con.execute(
        "INSERT INTO simulation_reservations "
        "(idempotency_key, origin, cycle_id, amount_cents, status, created_at, evidence_json) "
        "VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?)",
        (key, origin_value, cycle_value, amount_cents, now, evidence_json),
    )
    con.execute(
        "UPDATE simulation_bank SET revision = revision + 1, updated_at = ? WHERE id = 1",
        (now,),
    )
    return "CREATED"


def reserve(
    db_path: str | Path,
    *,
    idempotency_key: str,
    origin: str,
    cycle_id: str,
    amount_usd: str,
) -> dict[str, Any]:
    """Reserve `amount_usd` against available capital, atomically and idempotently.

    Replaying the same idempotency_key with the identical (origin, cycle_id, amount_usd)
    returns the current snapshot unchanged — no double charge, no revision bump. The same
    key with a different payload raises (never overwritten silently). A brand-new key is
    admitted only inside BEGIN IMMEDIATE after re-checking sum(active) + amount <=
    capital, so concurrent reservations can never push reserved above capital.
    """
    path = Path(db_path)
    key = _parse_id(idempotency_key, field="idempotency_key")
    origin_value = _parse_id(origin, field="origin")
    cycle_value = _parse_id(cycle_id, field="cycle_id")
    amount_cents = _parse_cents(amount_usd, field="amount_usd")
    with _connect(path) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            _reserve_locked(
                con,
                key=key,
                origin_value=origin_value,
                cycle_value=cycle_value,
                amount_cents=amount_cents,
                evidence_json=None,
            )
            result = _snapshot(con)
            con.execute("COMMIT")
            return result
        except Exception:
            con.execute("ROLLBACK")
            raise


def reserve_with_evidence(
    db_path: str | Path,
    *,
    idempotency_key: str,
    origin: str,
    cycle_id: str,
    amount_usd: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """`reserve` plus a caller-supplied evidence object, committed together.

    `evidence` must be a bounded, JSON-serializable object; it is canonicalized
    (sorted keys, no whitespace) before comparison or storage so two evidence
    dicts that differ only in key order are treated as identical. Replay rules
    match `reserve`, extended to evidence: the SAME key with the SAME
    (origin, cycle_id, amount_usd, evidence) is a no-op; ANY differing field —
    including evidence — raises SimulationBankConflictError. The insert and the
    evidence are written by the same statement inside the same BEGIN IMMEDIATE
    as the reservation itself, so there is never a reservation without evidence
    or evidence without a reservation.
    """
    _outcome, snapshot = _reserve_with_evidence(
        db_path,
        idempotency_key=idempotency_key,
        origin=origin,
        cycle_id=cycle_id,
        amount_usd=amount_usd,
        evidence=evidence,
    )
    return snapshot


def reserve_with_evidence_outcome(
    db_path: str | Path,
    *,
    idempotency_key: str,
    origin: str,
    cycle_id: str,
    amount_usd: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """`reserve_with_evidence`, reporting WHAT happened alongside the snapshot.

    Returns {"outcome": "CREATED" | "REPLAY_ACTIVE" | "REPLAY_RELEASED",
    "reservation_active": bool, "snapshot": <same dict reserve_with_evidence returns>}.
    The outcome is read inside the same BEGIN IMMEDIATE as the write (or the
    no-op), so it matches what is persisted — not a guess from the absence of
    an exception. Same replay and conflict rules as `reserve_with_evidence`; a
    RELEASED key is never reactivated.

    A separate function on purpose: `reserve_with_evidence` keeps returning the
    bare snapshot, because callers and tests rely on an identical replay
    returning a snapshot EQUAL to the original one.
    """
    outcome, snapshot = _reserve_with_evidence(
        db_path,
        idempotency_key=idempotency_key,
        origin=origin,
        cycle_id=cycle_id,
        amount_usd=amount_usd,
        evidence=evidence,
    )
    return {"outcome": outcome, "reservation_active": outcome != "REPLAY_RELEASED", "snapshot": snapshot}


def _reserve_with_evidence(
    db_path: str | Path,
    *,
    idempotency_key: str,
    origin: str,
    cycle_id: str,
    amount_usd: str,
    evidence: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    path = Path(db_path)
    key = _parse_id(idempotency_key, field="idempotency_key")
    origin_value = _parse_id(origin, field="origin")
    cycle_value = _parse_id(cycle_id, field="cycle_id")
    amount_cents = _parse_cents(amount_usd, field="amount_usd")
    evidence_json = _canonical_evidence_json(evidence)
    with _connect(path) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            outcome = _reserve_locked(
                con,
                key=key,
                origin_value=origin_value,
                cycle_value=cycle_value,
                amount_cents=amount_cents,
                evidence_json=evidence_json,
            )
            result = _snapshot(con)
            con.execute("COMMIT")
            return outcome, result
        except Exception:
            con.execute("ROLLBACK")
            raise


def release(db_path: str | Path, *, idempotency_key: str) -> dict[str, Any]:
    """Release a reservation by idempotency_key. Idempotent; never manufactures balance.

    An unknown key is a fail-closed error (nothing to release), not a silent no-op. A
    key that is already RELEASED is a true no-op: no state change, no revision bump.
    """
    path = Path(db_path)
    key = _parse_id(idempotency_key, field="idempotency_key")
    with _connect(path) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            bank_row = con.execute(
                "SELECT 1 FROM simulation_bank WHERE id = 1"
            ).fetchone()
            if bank_row is None:
                raise SimulationBankNotInitializedError("bank has not been initialized")
            row = con.execute(
                "SELECT status FROM simulation_reservations WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if row is None:
                raise SimulationBankNotFoundError("unknown idempotency_key")
            if row[0] == "ACTIVE":
                now = _now()
                con.execute(
                    "UPDATE simulation_reservations SET status = 'RELEASED', released_at = ? "
                    "WHERE idempotency_key = ?",
                    (now, key),
                )
                con.execute(
                    "UPDATE simulation_bank SET revision = revision + 1, updated_at = ? "
                    "WHERE id = 1",
                    (now,),
                )
            # else already RELEASED: idempotent no-op, no revision bump.
            result = _snapshot(con)
            con.execute("COMMIT")
            return result
        except Exception:
            con.execute("ROLLBACK")
            raise


# --------------------------------------------------------------------------------------
# Recording accounting events (C1/6.4). See ACCOUNTING_POLICY above.
# --------------------------------------------------------------------------------------


def _parse_int(value: Any, *, field: str, lo: int, hi: int) -> int:
    # type() is int, not isinstance: bool is an int subclass and must never pass.
    if type(value) is not int or not lo <= value <= hi:
        raise SimulationBankValidationError(f"{field} must be an integer in [{lo}, {hi}]")
    return value


def _release_linked_locked(con: sqlite3.Connection, keys: list[str], now: str) -> int:
    """Release the still-ACTIVE reservations among `keys`. Caller holds BEGIN IMMEDIATE
    and owns the revision bump. Already-RELEASED keys are left alone."""
    released = 0
    for key in keys:
        cur = con.execute(
            "UPDATE simulation_reservations SET status = 'RELEASED', released_at = ? "
            "WHERE idempotency_key = ? AND status = 'ACTIVE'",
            (now, key),
        )
        released += cur.rowcount
    return released


def _check_reservation_link(con: sqlite3.Connection, key: str, origin: str) -> None:
    row = con.execute(
        "SELECT origin, status FROM simulation_reservations WHERE idempotency_key = ?", (key,)
    ).fetchone()
    if row is None:
        raise SimulationBankValidationError("UNKNOWN_RESERVATION: fill cites a reservation that does not exist")
    if row[0] != origin:
        # A motor's fill can never be backed by another motor's reservation.
        raise SimulationBankValidationError("RESERVATION_ORIGIN_MISMATCH")
    if row[1] != "ACTIVE":
        raise SimulationBankValidationError("RESERVATION_NOT_ACTIVE: fill cites a released reservation")
    linked = con.execute(
        "SELECT 1 FROM simulation_events WHERE reservation_key = ? LIMIT 1", (key,)
    ).fetchone()
    if linked is not None:
        raise SimulationBankValidationError("RESERVATION_ALREADY_LINKED: one reservation backs one fill")


def _record_event(db_path: str | Path, event: dict[str, Any]) -> dict[str, Any]:
    path = Path(db_path)
    payload = tuple(event[col.strip()] for col in _EVENT_COLUMNS.split(","))
    with _connect(path) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            if con.execute("SELECT 1 FROM simulation_bank WHERE id = 1").fetchone() is None:
                raise SimulationBankNotInitializedError("bank has not been initialized")
            existing = con.execute(
                f"SELECT {_EVENT_COLUMNS} FROM simulation_events WHERE event_key = ?",
                (event["event_key"],),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != payload:
                    raise SimulationBankConflictError(
                        "event_key already used with a different event payload"
                    )
                # Identical replay: no new event, no reservation change, no revision bump.
                positions = _fold(con)
                key = (event["origin"], event["position_key"])
                result = {
                    "outcome": "REPLAY",
                    "position": _position_view(key, positions[key]),
                    "snapshot": _snapshot(con),
                }
                con.execute("COMMIT")
                return result
            if event["kind"] == "FILL" and event["reservation_key"] is not None:
                _check_reservation_link(con, event["reservation_key"], event["origin"])
            # Validate against the CURRENT derived state before writing anything.
            positions = _fold(con)
            key = (event["origin"], event["position_key"])
            if event["kind"] == "SETTLEMENT" and key not in positions:
                raise SimulationBankValidationError("NOTHING_TO_SETTLE: no such position")
            pos = positions.setdefault(key, _Position())
            _apply_row(
                pos,
                event["kind"],
                event["side"],
                event["price_cents"],
                event["count"],
                event["fee_cents"],
                event["payout_cents"],
                event["reservation_key"],
            )
            now = _now()
            con.execute(
                f"INSERT INTO simulation_events (event_key, {_EVENT_COLUMNS}, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event["event_key"], *payload, now),
            )
            if not pos.lots:
                # Flat or settled: the risk earmark ends in the SAME transaction as the
                # closing event. A crash between them cannot leave freed capital with
                # no recorded close, nor a recorded close with its capital still held.
                _release_linked_locked(con, pos.reservation_keys, now)
            con.execute(
                "UPDATE simulation_bank SET revision = revision + 1, updated_at = ? WHERE id = 1",
                (now,),
            )
            result = {
                "outcome": "RECORDED",
                "position": _position_view(key, pos),
                "snapshot": _snapshot(con),
            }
            con.execute("COMMIT")
            return result
        except Exception:
            con.execute("ROLLBACK")
            raise


def record_fill(
    db_path: str | Path,
    *,
    event_key: str,
    origin: str,
    position_key: str,
    side: str,
    price_cents: int,
    count: int,
    fee_cents: int,
    reservation_key: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record one SIMULATED fill. Idempotent by `event_key`.

    `fee_cents` is the fee of THIS fill as given by the caller — this ledger does not
    recompute tariffs (that is the fill reviewer's job). `reservation_key`, if given,
    must be an ACTIVE reservation of the same origin not already backing another fill;
    it is released when the position becomes flat or settles.

    Returns {"outcome": "RECORDED" | "REPLAY", "position": ..., "snapshot": ...}. Any
    invalid input or state raises before anything is written.
    """
    if side not in _SIDES:
        raise SimulationBankValidationError("side must be 'buy' or 'sell'")
    event = {
        "event_key": _parse_id(event_key, field="event_key"),
        "origin": _parse_id(origin, field="origin"),
        "position_key": _parse_id(position_key, field="position_key"),
        "kind": "FILL",
        "side": side,
        "price_cents": _parse_int(price_cents, field="price_cents", lo=_MIN_PRICE_CENTS, hi=_MAX_PRICE_CENTS),
        "count": _parse_int(count, field="count", lo=1, hi=_MAX_COUNT),
        "fee_cents": _parse_int(fee_cents, field="fee_cents", lo=0, hi=_MAX_FEE_CENTS),
        "payout_cents": None,
        "reservation_key": (
            None if reservation_key is None else _parse_id(reservation_key, field="reservation_key")
        ),
        "evidence_json": None if evidence is None else _canonical_evidence_json(evidence),
    }
    return _record_event(db_path, event)


def record_settlement(
    db_path: str | Path,
    *,
    event_key: str,
    origin: str,
    position_key: str,
    payout_cents: int,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record the resolution of a position: 0 or 100 cents per YES contract.

    Closes every open lot and releases the position's linked reservations in the same
    transaction. A settlement of a position with no open lot, a second settlement, or
    a payout other than 0/100 is rejected before anything is written.
    """
    if type(payout_cents) is not int or payout_cents not in _SETTLEMENT_PAYOUTS:
        raise SimulationBankValidationError("payout_cents must be 0 or 100")
    event = {
        "event_key": _parse_id(event_key, field="event_key"),
        "origin": _parse_id(origin, field="origin"),
        "position_key": _parse_id(position_key, field="position_key"),
        "kind": "SETTLEMENT",
        "side": None,
        "price_cents": None,
        "count": None,
        "fee_cents": None,
        "payout_cents": payout_cents,
        "reservation_key": None,
        "evidence_json": None if evidence is None else _canonical_evidence_json(evidence),
    }
    return _record_event(db_path, event)


def get_snapshot(db_path: str | Path) -> dict[str, Any]:
    """Read-only snapshot. Raises SimulationBankNotInitializedError before init_bank."""
    path = Path(db_path)
    with _connect(path) as con:
        return _snapshot(con)
