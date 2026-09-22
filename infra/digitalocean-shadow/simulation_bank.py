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
from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path
import re
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo

import risk_policy

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
      created_at TEXT NOT NULL,
      -- C2 (2026-09-22): WHEN the fact happened, not when the row was imported. Daily
      -- and weekly periods are derived from this; reprocessing a historical event must
      -- never turn it into today's new risk.
      occurred_at TEXT NOT NULL,
      admission_key TEXT,
      -- A recorded fill that the policy did not cover. Recorded, never erased: hiding an
      -- adverse observation would fabricate a favorable history.
      breach TEXT CHECK (breach IS NULL OR breach IN ('UNADMITTED', 'ADMISSION_EXCEEDED')),
      -- Defense in depth for the recording contract. NOT the guarantee: a file whose
      -- rows escaped these CHECKs is still refused on replay by _validate_stored_row.
      CHECK (
        (kind = 'FILL'
          AND side IN ('buy', 'sell')
          AND typeof(price_cents) = 'integer' AND price_cents BETWEEN 1 AND 99
          AND typeof(count) = 'integer' AND count BETWEEN 1 AND 1000000
          AND typeof(fee_cents) = 'integer' AND fee_cents BETWEEN 0 AND 100000000
          AND payout_cents IS NULL)
        OR
        (kind = 'SETTLEMENT'
          AND side IS NULL AND price_cents IS NULL AND count IS NULL
          AND fee_cents IS NULL AND reservation_key IS NULL
          AND admission_key IS NULL AND breach IS NULL
          AND typeof(payout_cents) = 'integer' AND payout_cents IN (0, 100))
      )
    );
    -- C2/7.2 (2026-09-22): ADMISSION BEFORE QUOTING. One row per proposal identity, with
    -- the decision taken inside the same transaction that reserves its risk. A decision
    -- is final for its key: replaying the identity returns it; it is never re-evaluated
    -- later to slip past a cap that has since freed up, and a new tick cannot mint a new
    -- identity for the same proposal without it being a different proposal.
    CREATE TABLE IF NOT EXISTS simulation_admissions (
      admission_key TEXT PRIMARY KEY,
      origin TEXT NOT NULL,
      thesis_id TEXT NOT NULL,
      position_key TEXT NOT NULL,
      risk_cents INTEGER NOT NULL CHECK (typeof(risk_cents) = 'integer' AND risk_cents > 0),
      proposed_at TEXT NOT NULL,
      risk_day TEXT NOT NULL,
      decision TEXT NOT NULL CHECK (decision IN ('ADMITTED', 'REJECTED')),
      reasons_json TEXT NOT NULL,
      reservation_key TEXT,
      policy_version TEXT NOT NULL,
      evidence_json TEXT,
      created_at TEXT NOT NULL,
      CHECK ((decision = 'ADMITTED') = (reservation_key IS NOT NULL))
    );
    """)
    event_columns = {row[1] for row in con.execute("PRAGMA table_info(simulation_events)")}
    for column in ("occurred_at", "admission_key", "breach"):
        if column not in event_columns:
            # A file created before C2 gains the column in place. Its old rows get NULL
            # occurred_at, which the replay REFUSES (a fact without a date cannot be
            # assigned to a period) — never silently dated "now".
            try:
                con.execute(f"ALTER TABLE simulation_events ADD COLUMN {column} TEXT")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc):
                    raise
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
# Accounting periods (C2, 2026-09-22): daily new-risk and weekly P&L are bucketed by the
# FACT date in this zone — the same zone bank_batch_review already uses. Weeks start
# Monday. Never by import time.
ACCOUNTING_ZONE = ZoneInfo("America/Los_Angeles")
# A NEW proposal must be current. An old one would spend budget on a day it does not
# belong to; a future one is a clock or identity error. A replay of an already-decided
# key is exempt — it returns its stored decision whatever its age.
MAX_PROPOSAL_AGE = timedelta(minutes=5)
MAX_PROPOSAL_FUTURE_SKEW = timedelta(seconds=60)
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

    def worst_case_cents(self) -> int:
        """Maximum further loss of the open lots: a long YES loses its price, a short
        YES loses (100 - price), per contract."""
        return sum((price if sign > 0 else 100 - price) * qty for sign, price, qty in self.lots)


def _opening_risk_cents(pos: _Position, *, side: str, price: int, count: int, fee: int) -> int:
    """Worst-case risk a fill ADDS: only the quantity that opens (after FIFO-closing the
    opposite lots), plus its fee. A pure close adds none. Same formula as the M5 fill
    review (buy: price*count + fee; sell: (100-price)*count + fee)."""
    sign = 1 if side == "buy" else -1
    closable = sum(qty for lot_sign, _p, qty in pos.lots if lot_sign == -sign)
    opening = max(0, count - closable)
    if opening == 0:
        return 0
    return (price if side == "buy" else 100 - price) * opening + fee


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


# Input payload of an event, in order. Replay of an event_key compares exactly these.
_EVENT_FIELDS = (
    "origin",
    "position_key",
    "kind",
    "side",
    "price_cents",
    "count",
    "fee_cents",
    "payout_cents",
    "reservation_key",
    "evidence_json",
    "occurred_at",
    "admission_key",
)
_EVENT_COLUMNS = ", ".join(_EVENT_FIELDS)


def _apply_row(pos: _Position, kind: str, side, price, count, fee, payout, reservation_key) -> None:
    if kind == "FILL":
        _apply_fill(pos, side=side, price=price, count=count, fee=fee)
        # Partial fills of one admitted quote cite the SAME reservation: link it once.
        if reservation_key is not None and reservation_key not in pos.reservation_keys:
            pos.reservation_keys.append(reservation_key)
    else:
        _apply_settlement(pos, payout=payout)


def _parse_moment(value: Any, *, field: str) -> str:
    """An AWARE datetime → canonical UTC ISO string. Naive times are refused: a fact
    whose zone is unknown cannot be placed in a Los Angeles day."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SimulationBankValidationError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(UTC).isoformat()


def _read_moment(text: Any, *, field: str) -> datetime:
    if not isinstance(text, str):
        raise SimulationBankValidationError(f"{field} missing: a fact without a date")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SimulationBankValidationError(f"{field} is not an ISO timestamp") from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise SimulationBankValidationError(f"{field} is not timezone-aware")
    return moment


def _local_day(moment: datetime) -> date:
    return moment.astimezone(ACCOUNTING_ZONE).date()


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


_BREACHES = ("UNADMITTED", "ADMISSION_EXCEEDED")


def _validate_stored_row(
    kind, side, price, count, fee, payout, reservation_key, origin, key,
    occurred_at=None, admission_key=None, breach=None,
):
    """The SAME contract `record_fill` / `record_settlement` enforce, applied on replay.

    CTO review 2026-09-22: `_fold` used to feed stored rows straight into the
    projection, so a stored fee_cents = -1 turned realized -0.01 into +0.01 silently
    (reproduced). Recording validated; replaying did not. A row that would be refused
    today is refused on replay too, whatever the table's CHECKs say or don't say.
    """
    _parse_id(origin, field="origin")
    _parse_id(key, field="position_key")
    _read_moment(occurred_at, field="occurred_at")
    if kind == "FILL":
        if side not in _SIDES:
            raise SimulationBankValidationError("side must be 'buy' or 'sell'")
        _parse_int(price, field="price_cents", lo=_MIN_PRICE_CENTS, hi=_MAX_PRICE_CENTS)
        _parse_int(count, field="count", lo=1, hi=_MAX_COUNT)
        _parse_int(fee, field="fee_cents", lo=0, hi=_MAX_FEE_CENTS)
        if payout is not None:
            raise SimulationBankValidationError("a FILL carries no payout")
        if reservation_key is not None:
            _parse_id(reservation_key, field="reservation_key")
        if admission_key is not None:
            _parse_id(admission_key, field="admission_key")
        if breach is not None and breach not in _BREACHES:
            raise SimulationBankValidationError(f"unknown breach {breach!r}")
    elif kind == "SETTLEMENT":
        if type(payout) is not int or payout not in _SETTLEMENT_PAYOUTS:
            raise SimulationBankValidationError("payout_cents must be 0 or 100")
        extra = (side, price, count, fee, reservation_key, admission_key, breach)
        if any(v is not None for v in extra):
            raise SimulationBankValidationError("a SETTLEMENT carries only its payout")
    else:
        raise SimulationBankValidationError(f"unknown event kind {kind!r}")


def _fold_full(con: sqlite3.Connection) -> tuple[dict[tuple[str, str], _Position], list[dict]]:
    """Replay every stored event in seq order and return (positions, history).

    Deterministic. Each row is validated against the recording contract BEFORE it
    touches the projection; the first invalid row raises, naming its seq, and no state
    (partial or otherwise) is returned. `history` has one entry per event with its
    FACT date, the realized P&L it produced and the risk it opened — the basis of the
    daily and weekly periods."""
    positions: dict[tuple[str, str], _Position] = {}
    history: list[dict] = []
    rows = con.execute(
        "SELECT seq, event_key, origin, position_key, kind, side, price_cents, count, "
        "fee_cents, payout_cents, reservation_key, occurred_at, admission_key, breach "
        "FROM simulation_events ORDER BY seq"
    ).fetchall()
    for (
        seq, event_key, origin, position_key, kind, side, price, count, fee, payout,
        reservation_key, occurred_at, admission_key, breach,
    ) in rows:
        try:
            _validate_stored_row(
                kind, side, price, count, fee, payout, reservation_key, origin,
                position_key, occurred_at, admission_key, breach,
            )
            pos = positions.setdefault((origin, position_key), _Position())
            before = pos.realized_cents
            opening = (
                _opening_risk_cents(pos, side=side, price=price, count=count, fee=fee)
                if kind == "FILL" else 0
            )
            _apply_row(pos, kind, side, price, count, fee, payout, reservation_key)
        except (SimulationBankValidationError, TypeError) as exc:
            raise SimulationBankIntegrityError(
                f"stored event seq={seq} does not replay: {exc}"
            ) from exc
        history.append({
            "seq": seq,
            "event_key": event_key,
            "origin": origin,
            "position_key": position_key,
            "kind": kind,
            "occurred_at": _read_moment(occurred_at, field="occurred_at"),
            "realized_delta_cents": pos.realized_cents - before,
            "opening_risk_cents": opening,
            "reservation_key": reservation_key,
            "admission_key": admission_key,
            "breach": breach,
        })
    return positions, history


def _with_coverage(history: list[dict], amounts: dict[str, int]) -> list[dict]:
    """Each FILL's opening risk split into covered / uncovered, in seq order.

    A reservation covers the CUMULATIVE opening risk of the fills linked to it, up to
    its amount — partial fills of one admitted quote draw from the same budget, never
    twice. Whatever exceeds it (or has no linked reservation) is uncovered."""
    used: dict[str, int] = {}
    out = []
    for h in history:
        opening = h["opening_risk_cents"]
        key = h["reservation_key"]
        covered = 0
        if key is not None and opening:
            covered = min(opening, max(0, amounts.get(key, 0) - used.get(key, 0)))
            used[key] = used.get(key, 0) + covered
        out.append(dict(h, uncovered_risk_cents=opening - covered))
    return out


def _fold(con: sqlite3.Connection) -> dict[tuple[str, str], _Position]:
    return _fold_full(con)[0]


def _realized_cents(positions: dict[tuple[str, str], _Position]) -> int:
    return sum(pos.realized_cents for pos in positions.values())


def _active_reservation_amounts(con: sqlite3.Connection) -> dict[str, int]:
    return dict(con.execute(
        "SELECT idempotency_key, amount_cents FROM simulation_reservations WHERE status = 'ACTIVE'"
    ).fetchall())


def _unreserved_exposure_cents(
    positions: dict[tuple[str, str], _Position], active: dict[str, int]
) -> int:
    """Open worst-case NOT covered by an active reservation linked to the position.

    A fill recorded without admission (a breach) still carries real simulated risk; if
    the admission path ignored it, the next proposal would see capacity that does not
    exist."""
    total = 0
    for pos in positions.values():
        covered = sum(active.get(key, 0) for key in pos.reservation_keys)
        total += max(0, pos.worst_case_cents() - covered)
    return total


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


def _period_view(
    con: sqlite3.Connection,
    positions: dict[tuple[str, str], _Position],
    history: list[dict],
    *,
    capital_cents: int,
    now: datetime,
) -> dict[str, Any]:
    """Everything the admission policy needs, computed ONE way for both admission and
    snapshot. Periods come from FACT dates (occurred_at / proposed_at) in
    ACCOUNTING_ZONE, never from import time."""
    today = _local_day(now)
    week = _week_start(today)
    active = _active_reservation_amounts(con)
    reserved = sum(active.values())
    unreserved = _unreserved_exposure_cents(positions, active)
    realized = _realized_cents(positions)
    week_realized = sum(
        h["realized_delta_cents"] for h in history if _week_start(_local_day(h["occurred_at"])) == week
    )
    admitted_today = con.execute(
        "SELECT COALESCE(SUM(risk_cents), 0) FROM simulation_admissions "
        "WHERE decision = 'ADMITTED' AND risk_day = ?",
        (today.isoformat(),),
    ).fetchone()[0]
    # Risk opened today by fills the policy did NOT admit also consumed today's budget:
    # all of it for an uncovered fill, the excess over its reservation for a fill that
    # outgrew it. An admitted-and-covered fill adds nothing (counted once, at admission).
    amounts = dict(con.execute(
        "SELECT idempotency_key, amount_cents FROM simulation_reservations"
    ).fetchall())
    breached_today = sum(
        h["uncovered_risk_cents"] for h in _with_coverage(history, amounts)
        if _local_day(h["occurred_at"]) == today
    )
    return {
        "today": today,
        "week_start": week,
        "reserved_cents": reserved,
        "unreserved_cents": unreserved,
        "open_risk_cents": reserved + unreserved,
        "realized_cents": realized,
        "week_realized_cents": week_realized,
        "today_new_risk_cents": admitted_today + breached_today,
        "capital_now_cents": capital_cents + realized,
    }


def _cents_to_micros(cents: int) -> int:
    return cents * risk_policy.CENT


def _micros_to_cents(micros: int) -> int:
    # Policy caps are whole cents by construction (risk_policy floors to the cent).
    return micros // risk_policy.CENT


def _snapshot(con: sqlite3.Connection, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    row = con.execute(
        "SELECT initial_capital_cents, revision, created_at, updated_at "
        "FROM simulation_bank WHERE id = 1"
    ).fetchone()
    if row is None:
        raise SimulationBankNotInitializedError("bank has not been initialized")
    capital_cents, revision, created_at, updated_at = row
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
    positions, history = _fold_full(con)
    period = _period_view(con, positions, history, capital_cents=capital_cents, now=now)
    capital_now_cents = period["capital_now_cents"]
    limits = risk_policy.policy_limits(_cents_to_micros(max(0, capital_now_cents)))
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "SIMULATION_ONLY",
        "capital_source": "FICTIONAL_TEST_CAPITAL",
        "execution_authorized": False,
        "order_capability_present": False,
        "real_entry_eligible": False,
        "initial_capital_usd": _usd(capital_cents),
        # capital = initial fictional capital + realized P&L. Reserved is an earmark on
        # it, not a loss. With no events this is exactly the pre-accounting snapshot.
        "realized_pnl_usd": _usd(period["realized_cents"]),
        "capital_usd": _usd(capital_now_cents),
        "reserved_usd": _usd(period["reserved_cents"]),
        "available_usd": _usd(capital_now_cents - period["reserved_cents"]),
        "accounting_policy": ACCOUNTING_POLICY,
        "accounting_zone": str(ACCOUNTING_ZONE),
        "risk_day": period["today"].isoformat(),
        "week_start": period["week_start"].isoformat(),
        "week_realized_pnl_usd": _usd(period["week_realized_cents"]),
        "today_new_risk_usd": _usd(period["today_new_risk_cents"]),
        "open_risk_usd": _usd(period["open_risk_cents"]),
        "unreserved_open_exposure_usd": _usd(period["unreserved_cents"]),
        "risk_policy": {
            "version": risk_policy.POLICY_VERSION,
            **{k: _usd(_micros_to_cents(v)) for k, v in limits.items()},
        },
        "revision": revision,
        "created_at": created_at,
        "updated_at": updated_at,
        "active_reservations": active_reservations,
        "positions": [_position_view(key, positions[key]) for key in sorted(positions)],
        "breaches": [
            {k: h[k] for k in ("event_key", "origin", "position_key", "breach")}
            for h in history
            if h["breach"] is not None
        ],
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


def _check_moment_not_future(moment_text: str, now: datetime, *, field: str) -> None:
    if _read_moment(moment_text, field=field) > now + MAX_PROPOSAL_FUTURE_SKEW:
        raise SimulationBankValidationError(f"{field} is in the future")


def _resolve_admission(con: sqlite3.Connection, event: dict[str, Any]) -> dict[str, Any] | None:
    """The admission a FILL cites, checked against the fill's identity. None if uncited."""
    key = event["admission_key"]
    if key is None:
        return None
    row = con.execute(
        "SELECT origin, position_key, decision, reservation_key, risk_cents "
        "FROM simulation_admissions WHERE admission_key = ?",
        (key,),
    ).fetchone()
    if row is None:
        raise SimulationBankValidationError("UNKNOWN_ADMISSION: fill cites an admission that does not exist")
    origin, position_key, decision, reservation_key, risk_cents = row
    if (origin, position_key) != (event["origin"], event["position_key"]):
        # Contradictory identity is invalid evidence, not a breach to record.
        raise SimulationBankValidationError("ADMISSION_IDENTITY_MISMATCH")
    return {
        "decision": decision,
        "reservation_key": reservation_key,
        "risk_cents": risk_cents,
    }


def _record_event(
    db_path: str | Path, event: dict[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    path = Path(db_path)
    # When the fill arrives by admission, its reservation is DERIVED from the admission
    # and is not part of what the caller said — so replay compares the caller's fields.
    compared = tuple(
        f for f in _EVENT_FIELDS
        if not (f == "reservation_key" and event["admission_key"] is not None)
    )
    payload = tuple(event[f] for f in compared)
    with _connect(path) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            if con.execute("SELECT 1 FROM simulation_bank WHERE id = 1").fetchone() is None:
                raise SimulationBankNotInitializedError("bank has not been initialized")
            existing = con.execute(
                f"SELECT {', '.join(compared)} FROM simulation_events WHERE event_key = ?",
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
                    "snapshot": _snapshot(con, now=now),
                }
                con.execute("COMMIT")
                return result
            _check_moment_not_future(event["occurred_at"], now, field="occurred_at")
            positions, history = _fold_full(con)
            key = (event["origin"], event["position_key"])
            if event["kind"] == "SETTLEMENT" and key not in positions:
                raise SimulationBankValidationError("NOTHING_TO_SETTLE: no such position")
            pos = positions.setdefault(key, _Position())

            linked_key = event["reservation_key"]
            covered_cents: int | None = None
            breach: str | None = None
            if event["kind"] == "FILL":
                admission = _resolve_admission(con, event)
                if admission is not None and linked_key is not None:
                    raise SimulationBankValidationError(
                        "a fill cites either an admission or a reservation, not both"
                    )
                if admission is not None:
                    # Admission path. Its reservation covers the CUMULATIVE opening risk
                    # of the fills citing it, up to what was admitted — while it is still
                    # ACTIVE. A withdrawn/expired quote, or a position already closed
                    # (reservation released), leaves the fill RECORDED but uncovered.
                    if admission["decision"] == "ADMITTED":
                        res = admission["reservation_key"]
                        status = con.execute(
                            "SELECT status FROM simulation_reservations WHERE idempotency_key = ?",
                            (res,),
                        ).fetchone()
                        if status is not None and status[0] == "ACTIVE":
                            already = sum(
                                h["opening_risk_cents"] for h in history
                                if h["admission_key"] == event["admission_key"]
                            )
                            linked_key = res
                            covered_cents = max(0, admission["risk_cents"] - already)
                elif linked_key is not None:
                    # Legacy path: an explicit reservation, fully validated (must exist,
                    # be ACTIVE, same origin, not already backing a fill).
                    _check_reservation_link(con, linked_key, event["origin"])
                    covered_cents = con.execute(
                        "SELECT amount_cents FROM simulation_reservations WHERE idempotency_key = ?",
                        (linked_key,),
                    ).fetchone()[0]
                opening = _opening_risk_cents(
                    pos,
                    side=event["side"],
                    price=event["price_cents"],
                    count=event["count"],
                    fee=event["fee_cents"],
                )
                if opening > 0:
                    if covered_cents is None:
                        # Recorded and flagged, never dropped: dropping an adverse fill
                        # would fabricate a favorable history.
                        had_admission = admission is not None and admission["decision"] == "ADMITTED"
                        breach = "ADMISSION_EXCEEDED" if had_admission else "UNADMITTED"
                    elif opening > covered_cents:
                        breach = "ADMISSION_EXCEEDED"
            _apply_row(
                pos,
                event["kind"],
                event["side"],
                event["price_cents"],
                event["count"],
                event["fee_cents"],
                event["payout_cents"],
                linked_key,
            )
            stored = dict(event, reservation_key=linked_key)
            written = now.astimezone(UTC).isoformat()
            columns = ("event_key", *_EVENT_FIELDS, "breach", "created_at")
            con.execute(
                f"INSERT INTO simulation_events ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                (event["event_key"], *(stored[f] for f in _EVENT_FIELDS), breach, written),
            )
            if not pos.lots:
                # Flat or settled: the risk earmark ends in the SAME transaction as the
                # closing event. A crash between them cannot leave freed capital with
                # no recorded close, nor a recorded close with its capital still held.
                _release_linked_locked(con, pos.reservation_keys, written)
            con.execute(
                "UPDATE simulation_bank SET revision = revision + 1, updated_at = ? WHERE id = 1",
                (written,),
            )
            result = {
                "outcome": "RECORDED",
                "breach": breach,
                "position": _position_view(key, pos),
                "snapshot": _snapshot(con, now=now),
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
    occurred_at: datetime,
    admission_key: str | None = None,
    reservation_key: str | None = None,
    evidence: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record one SIMULATED fill. Idempotent by `event_key`.

    `occurred_at` is WHEN the fill happened (aware); periods come from it, never from
    import time. `fee_cents` is the fee of THIS fill as given — this ledger does not
    recompute tariffs.

    The fill cites the risk that backs it by `admission_key` (C2 path: the admission
    taken BEFORE quoting) or, legacy, by `reservation_key`. The fill never reserves
    again: partial fills of one admitted quote draw CUMULATIVELY from the admitted risk
    (a legacy reservation backs exactly one fill), and the reservation is released when
    the position is flat or settled. A fill whose opening risk has no admission, cites
    a rejected, withdrawn or already-released one, or takes the cumulative total past
    what was admitted is RECORDED with a `breach` flag: adverse observations are never
    erased to keep the history favorable.

    Returns {"outcome": "RECORDED" | "REPLAY", "breach", "position", "snapshot"}. Invalid
    input or contradictory identity raises before anything is written.
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
        "occurred_at": _parse_moment(occurred_at, field="occurred_at"),
        "admission_key": (
            None if admission_key is None else _parse_id(admission_key, field="admission_key")
        ),
    }
    return _record_event(db_path, event, now=now)


def record_settlement(
    db_path: str | Path,
    *,
    event_key: str,
    origin: str,
    position_key: str,
    payout_cents: int,
    occurred_at: datetime,
    evidence: dict[str, Any] | None = None,
    now: datetime | None = None,
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
        "occurred_at": _parse_moment(occurred_at, field="occurred_at"),
        "admission_key": None,
    }
    return _record_event(db_path, event, now=now)


# --------------------------------------------------------------------------------------
# Admission BEFORE quoting (C2/7.2). One economic admission per proposal identity:
# decision + reservation in ONE transaction, against the ONE policy formula
# (risk_policy) and the shared state of every origin (M1, M5, Radar...).
# --------------------------------------------------------------------------------------

_MAX_ADMISSION_KEY_LEN = 120  # room for the "adm:" reservation prefix inside _ID_RE's 128
_MAX_RISK_CENTS = 100_000_000
_ADMISSION_FIELDS = ("origin", "thesis_id", "position_key", "risk_cents", "proposed_at", "evidence_json")


def _admission_limits(
    con: sqlite3.Connection, *, thesis_id: str, at: datetime
) -> dict[str, int]:
    """Policy caps and headrooms at the proposal's FACT time, in cents."""
    capital_cents = con.execute(
        "SELECT initial_capital_cents FROM simulation_bank WHERE id = 1"
    ).fetchone()[0]
    positions, history = _fold_full(con)
    period = _period_view(con, positions, history, capital_cents=capital_cents, now=at)
    limits = risk_policy.policy_limits(_cents_to_micros(max(0, period["capital_now_cents"])))
    caps = {k: _micros_to_cents(v) for k, v in limits.items()}
    thesis_open = con.execute(
        "SELECT COALESCE(SUM(r.amount_cents), 0) FROM simulation_admissions a "
        "JOIN simulation_reservations r ON r.idempotency_key = a.reservation_key "
        "WHERE a.thesis_id = ? AND r.status = 'ACTIVE'",
        (thesis_id,),
    ).fetchone()[0]
    return {
        **caps,
        "thesis_open": thesis_open,
        "open_risk": period["open_risk_cents"],
        "today_new_risk": period["today_new_risk_cents"],
        "week_realized": period["week_realized_cents"],
        "realized": period["realized_cents"],
        "capital_now": period["capital_now_cents"],
        "reserved": period["reserved_cents"],
        "risk_day": period["today"].isoformat(),
    }


def _admission_reasons(lim: dict[str, int], risk: int) -> list[str]:
    weekly_stop = _micros_to_cents(risk_policy.WEEKLY_STOP)
    experiment_stop = _micros_to_cents(risk_policy.EXPERIMENT_STOP)
    reasons = []
    if lim["week_realized"] <= -weekly_stop:
        reasons.append("PAUSED_WEEKLY")
    if lim["realized"] <= -experiment_stop:
        reasons.append("PAUSED_EXPERIMENT")
    # Sized FROM the habitual budget: a proposal above it is refused, never bumped to
    # the unit to make it fit.
    if risk > lim["habitual"]:
        reasons.append("SIZE_ABOVE_HABITUAL")
    if lim["thesis_open"] + risk > lim["max_per_thesis"]:
        reasons.append("THESIS_CAP")
    if lim["open_risk"] + risk > lim["max_open"]:
        reasons.append("OPEN_CAP")
    if lim["today_new_risk"] + risk > lim["max_daily"]:
        reasons.append("DAILY_CAP")
    if experiment_stop + lim["realized"] - lim["open_risk"] - risk < 0:
        reasons.append("EXPERIMENT_CAP")
    if lim["capital_now"] - lim["reserved"] - risk < 0:
        reasons.append("CAPITAL")
    return reasons


def admit_proposal(
    db_path: str | Path,
    *,
    admission_key: str,
    origin: str,
    thesis_id: str,
    position_key: str,
    risk_cents: int,
    proposed_at: datetime,
    evidence: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Decide ONE proposal before it is quoted, and reserve its risk if admitted.

    Read-margin, decide and reserve happen inside ONE BEGIN IMMEDIATE, so two motors can
    never commit the same budget. `risk_cents` is the proposal's worst-case loss
    INCLUSIVE of costs for the size it wants to quote, sized from the habitual budget.

    The decision is final for `admission_key`: an identical replay returns it (outcome
    REPLAY) whatever its age — after a restart too — and never re-evaluates; a changed
    replay is a conflict. A NEW proposal must be current (proposed_at within
    MAX_PROPOSAL_AGE, not in the future) — reprocessing a historical proposal must not
    spend today's budget. Periods use the proposal's FACT date in ACCOUNTING_ZONE.

    Returns {"outcome", "decision": "ADMITTED" | "REJECTED", "reasons", "reservation_key",
    "limits_usd", "snapshot"}. The result never carries authority.
    """
    now = now or datetime.now(UTC)
    key = _parse_id(admission_key, field="admission_key")
    if len(key) > _MAX_ADMISSION_KEY_LEN:
        raise SimulationBankValidationError("admission_key too long")
    fields = {
        "origin": _parse_id(origin, field="origin"),
        "thesis_id": _parse_id(thesis_id, field="thesis_id"),
        "position_key": _parse_id(position_key, field="position_key"),
        "risk_cents": _parse_int(risk_cents, field="risk_cents", lo=1, hi=_MAX_RISK_CENTS),
        "proposed_at": _parse_moment(proposed_at, field="proposed_at"),
        "evidence_json": None if evidence is None else _canonical_evidence_json(evidence),
    }
    path = Path(db_path)
    with _connect(path) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            if con.execute("SELECT 1 FROM simulation_bank WHERE id = 1").fetchone() is None:
                raise SimulationBankNotInitializedError("bank has not been initialized")
            existing = con.execute(
                f"SELECT {', '.join(_ADMISSION_FIELDS)}, decision, reasons_json, reservation_key "
                "FROM simulation_admissions WHERE admission_key = ?",
                (key,),
            ).fetchone()
            if existing is not None:
                if tuple(existing[: len(_ADMISSION_FIELDS)]) != tuple(fields[f] for f in _ADMISSION_FIELDS):
                    raise SimulationBankConflictError(
                        "admission_key already used with a different proposal"
                    )
                decision, reasons_json, reservation_key = existing[len(_ADMISSION_FIELDS):]
                result = {
                    "outcome": "REPLAY",
                    "decision": decision,
                    "reasons": json.loads(reasons_json),
                    "reservation_key": reservation_key,
                    "snapshot": _snapshot(con, now=now),
                }
                con.execute("COMMIT")
                return result
            moment = _read_moment(fields["proposed_at"], field="proposed_at")
            if moment < now - MAX_PROPOSAL_AGE:
                raise SimulationBankValidationError("PROPOSAL_NOT_CURRENT: too old to admit today")
            if moment > now + MAX_PROPOSAL_FUTURE_SKEW:
                raise SimulationBankValidationError("PROPOSAL_IN_FUTURE")
            lim = _admission_limits(con, thesis_id=fields["thesis_id"], at=moment)
            reasons = _admission_reasons(lim, fields["risk_cents"])
            decision = "REJECTED" if reasons else "ADMITTED"
            reservation_key = None
            written = now.astimezone(UTC).isoformat()
            if decision == "ADMITTED":
                reservation_key = f"adm:{key}"
                _reserve_locked(
                    con,
                    key=reservation_key,
                    origin_value=fields["origin"],
                    cycle_value=key,
                    amount_cents=fields["risk_cents"],
                    evidence_json=_canonical_evidence_json(
                        {"admission_key": key, "thesis_id": fields["thesis_id"]}
                    ),
                )  # bumps revision
            else:
                con.execute(
                    "UPDATE simulation_bank SET revision = revision + 1, updated_at = ? WHERE id = 1",
                    (written,),
                )
            con.execute(
                "INSERT INTO simulation_admissions (admission_key, origin, thesis_id, position_key, "
                "risk_cents, proposed_at, risk_day, decision, reasons_json, reservation_key, "
                "policy_version, evidence_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    key, fields["origin"], fields["thesis_id"], fields["position_key"],
                    fields["risk_cents"], fields["proposed_at"], lim["risk_day"], decision,
                    json.dumps(reasons), reservation_key, risk_policy.POLICY_VERSION,
                    fields["evidence_json"], written,
                ),
            )
            result = {
                "outcome": "RECORDED",
                "decision": decision,
                "reasons": reasons,
                "reservation_key": reservation_key,
                "limits_usd": {
                    k: _usd(lim[k])
                    for k in ("unit", "habitual", "max_per_thesis", "max_open", "max_daily",
                              "thesis_open", "open_risk", "today_new_risk")
                },
                "snapshot": _snapshot(con, now=now),
            }
            con.execute("COMMIT")
            return result
        except Exception:
            con.execute("ROLLBACK")
            raise


def withdraw_admission(
    db_path: str | Path, *, admission_key: str, now: datetime | None = None
) -> dict[str, Any]:
    """The quote was withdrawn or expired: release its reservation if still ACTIVE and
    no fill cites it yet. Once a (partial) fill cites it, the reservation stays until
    the position is flat or settled — conservative: the unfilled remainder stays held
    rather than guessing it. Idempotent. Today's consumed daily budget is NOT given back — the
    risk was committed when admitted. A rejected admission has nothing to release."""
    now = now or datetime.now(UTC)
    key = _parse_id(admission_key, field="admission_key")
    path = Path(db_path)
    with _connect(path) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            row = con.execute(
                "SELECT decision, reservation_key FROM simulation_admissions WHERE admission_key = ?",
                (key,),
            ).fetchone()
            if row is None:
                raise SimulationBankNotFoundError("unknown admission_key")
            decision, reservation_key = row
            released = 0
            if decision == "ADMITTED":
                spent = con.execute(
                    "SELECT 1 FROM simulation_events WHERE reservation_key = ? LIMIT 1",
                    (reservation_key,),
                ).fetchone()
                if spent is None:
                    written = now.astimezone(UTC).isoformat()
                    released = _release_linked_locked(con, [reservation_key], written)
                    if released:
                        con.execute(
                            "UPDATE simulation_bank SET revision = revision + 1, updated_at = ? "
                            "WHERE id = 1",
                            (written,),
                        )
            result = {"released": bool(released), "snapshot": _snapshot(con, now=now)}
            con.execute("COMMIT")
            return result
        except Exception:
            con.execute("ROLLBACK")
            raise


def get_snapshot(db_path: str | Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Read-only snapshot. Raises SimulationBankNotInitializedError before init_bank."""
    path = Path(db_path)
    with _connect(path) as con:
        return _snapshot(con, now=now)
