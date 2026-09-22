"""Producer of M5 research inputs: fee schedules and fairs, each with its ORIGINAL date.

Writes `m5/inputs.json` (schema `botkalshi-m5-research-inputs-v1`), which
`m5_research_sim` validates fail-closed. Nothing here quotes, reserves or places
anything. Two sources, both public-data reads through the allowlisted `collector.Reader`:

FEE — Kalshi's public API, per series and per event, plus scheduled changes:
  - `GET /series/{series}`             → base `fee_type` / `fee_multiplier`;
  - `GET /events/{event}`              → `fee_type_override` / `fee_multiplier_override`
                                         (both or neither — a partial override blocks);
  - `GET /series/fee_changes?series_ticker=…&show_historical=true` → scheduled changes.
  The series/event shapes are the ones `src/strategies/motor_5_mm/fee_policy.py` already
  pins (verified against the API on 2026-08-22). The fee-changes shape
  (`series_fee_change_arr[].{series_ticker, fee_type, fee_multiplier, scheduled_ts}`) is
  from Kalshi's public docs and was NOT observed from this session — its egress to
  api.elections.kalshi.com is denied. Anything else fails CLOSED (FEE_CHANGES_UNREADABLE),
  so a wrong assumption blocks M5 visibly instead of pricing with a guessed fee.
  Each entry carries `observed_at` = the instant its read returned, and the window of
  the official schedule (`effective_from` / `effective_until` from the changes).

FAIR — The Odds API, as a bounded PILOT only (owner decision required):
  - off unless BOTKALSHI_ODDS_PILOT_ENABLED=true, a key FILE the owner provisioned
    (BOTKALSHI_ODDS_PILOT_KEY_FILE, 0600, never the ODDS_API_KEY env var: the collector
    polls with that one every 300 s for h2h+totals, ~576 credits/day), and a budget
    BOTKALSHI_ODDS_PILOT_BUDGET ≤ 10 credits in total;
  - one request shape: MLB, `markets=h2h`, `regions=us` → 1 credit per the provider's
    published "markets × regions" rule; the `x-requests-last` header is recorded, and a
    call that cost more than that halts the pilot;
  - the credit is RESERVED in a persisted ledger BEFORE the request: a crash can never
    push usage past the budget;
  - the fair is M2's own computation (`src.strategies.motor_2_consensus.consensus`:
    median of books, no-vig, min 3 books, 15-min book freshness) over the LAST fetched
    odds; its `observed_at` is the OLDEST accepted book's `last_update` — never the fetch
    or export time. With a pilot of ≤10 calls, fairs exist only briefly after each call:
    M5 stays BLOCKED_NO_FAIR the rest of the time, which is the honest state.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any

import collector
import m5_research_sim
import repo_root  # noqa: F401 — puts the repo root on sys.path (system python3)

from src.strategies.motor_2_consensus.consensus import (
    consensus_fair_probs,
    h2h_outcome_names,
    select_candidate,
)
from src.strategies.motor_2_consensus.matcher import (
    canonical_name,
    match_outcomes,
    series_sport_compatible,
)

SCHEMA_LEDGER = "botkalshi-odds-pilot-ledger-v1"
FEE_TYPE = m5_research_sim.FEE_TYPE
FEE_CHANGES_WIRE = "DOCUMENTED_NOT_OBSERVED_IN_SESSION"
ODDS_SPORT = "baseball_mlb"
PILOT_PARAMS = {"regions": "us", "markets": "h2h", "oddsFormat": "decimal", "dateFormat": "iso"}
PILOT_COST_PER_CALL = 1  # markets (1) × regions (1)
MAX_PILOT_BUDGET = 10
MIN_PILOT_INTERVAL_SEC = 1_800  # the published pilot limit: at most one call every 30 min
CONSENSUS_MIN_BOOKS = 3  # M5 F1-v2 contract: MOTOR_2_MIN_BOOKS >= 3
CONSENSUS_MAX_BOOK_AGE_MIN = 15.0  # M5 F1-v2 contract: 0 < MOTOR_2_MAX_BOOK_AGE_MIN <= 15
FEE_REFRESH_SEC = 600
FEE_KEEP_SEC = 2 * 3_600  # older evidence can no longer verify any moment (TTL 1 h)
MAX_FEE_EVENTS_PER_CYCLE = 12
MAX_KEY_BYTES = 128
USAGE_HEADERS = ("x-requests-last", "x-requests-used", "x-requests-remaining")


class InputsSourceError(Exception):
    """A source could not be read or did not have the pinned shape: fail closed."""


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _aware(value: object) -> datetime | None:
    return m5_research_sim._aware(value)


def _multiplier_text(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise InputsSourceError("fee_multiplier missing or not a number")
    try:
        fraction = Fraction(str(value))
    except (ValueError, ZeroDivisionError) as exc:
        raise InputsSourceError("fee_multiplier unreadable") from exc
    if not 0 < fraction <= 10:
        raise InputsSourceError("fee_multiplier out of range")
    return str(fraction)


def _timestamp(value: object) -> datetime:
    if isinstance(value, bool):
        raise InputsSourceError("scheduled_ts unreadable")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    moment = _aware(value)
    if moment is None:
        raise InputsSourceError("scheduled_ts unreadable")
    return moment


# --------------------------------------------------------------------------------------
# Fee (Kalshi public API).
# --------------------------------------------------------------------------------------


def read_series_schedule(reader: Any, series: str) -> dict[str, str]:
    body = reader.get_json(collector.KALSHI_ORIGIN, f"/series/{series}")
    raw = body.get("series") if isinstance(body, dict) else None
    if not isinstance(raw, dict):
        raise InputsSourceError("SERIES_SHAPE")
    if raw.get("fee_type") != FEE_TYPE:
        raise InputsSourceError("UNSUPPORTED_FEE_TYPE")
    return {"fee_type": FEE_TYPE, "fee_multiplier": _multiplier_text(raw.get("fee_multiplier"))}


def read_fee_changes(reader: Any, series: str) -> list[dict[str, Any]]:
    body = reader.get_json(
        collector.KALSHI_ORIGIN,
        "/series/fee_changes",
        {"series_ticker": series, "show_historical": "true"},
    )
    rows = body.get("series_fee_change_arr") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise InputsSourceError("FEE_CHANGES_UNREADABLE")
    changes = []
    for row in rows:
        if not isinstance(row, dict) or row.get("series_ticker") != series:
            raise InputsSourceError("FEE_CHANGES_UNREADABLE")
        if row.get("fee_type") != FEE_TYPE:
            raise InputsSourceError("UNSUPPORTED_FEE_TYPE_SCHEDULED")
        try:
            changes.append(
                {
                    "fee_type": FEE_TYPE,
                    "fee_multiplier": _multiplier_text(row.get("fee_multiplier")),
                    "scheduled_at": _timestamp(row.get("scheduled_ts")),
                }
            )
        except InputsSourceError as exc:
            raise InputsSourceError("FEE_CHANGES_UNREADABLE") from exc
    return sorted(changes, key=lambda c: c["scheduled_at"])


def read_event_override(reader: Any, event_ticker: str, series: str) -> dict[str, str] | None:
    body = reader.get_json(collector.KALSHI_ORIGIN, f"/events/{event_ticker}")
    raw = body.get("event") if isinstance(body, dict) else None
    if not isinstance(raw, dict):
        raise InputsSourceError("EVENT_SHAPE")
    if raw.get("event_ticker") != event_ticker or raw.get("series_ticker") != series:
        raise InputsSourceError("EVENT_IDENTITY_MISMATCH")
    kind, mult = raw.get("fee_type_override"), raw.get("fee_multiplier_override")
    if kind is None and mult is None:
        return None
    if kind is None or mult is None:
        raise InputsSourceError("PARTIAL_OVERRIDE")
    if kind != FEE_TYPE:
        raise InputsSourceError("UNSUPPORTED_OVERRIDE_TYPE")
    return {"fee_type": FEE_TYPE, "fee_multiplier": _multiplier_text(mult)}


def fee_entries(
    *,
    series: str,
    event_ticker: str,
    base: dict[str, str],
    changes: list[dict[str, Any]],
    override: dict[str, str] | None,
    observed_at: datetime,
) -> list[dict[str, Any]]:
    """Entries for ONE event, each valid only inside the official schedule's window."""
    past = [c for c in changes if c["scheduled_at"] <= observed_at]
    future = [c for c in changes if c["scheduled_at"] > observed_at]
    until = future[0]["scheduled_at"] if future else None
    common = {
        "event_ticker": event_ticker,
        "series_ticker": series,
        "observed_at": observed_at.isoformat(),
    }
    if override is not None:
        # An override's own start is not published here: it proves only moments at or
        # after this read (no effective_from), and not past the next series change.
        return [
            {
                **common,
                **override,
                "source": "event_override",
                "effective_from": None,
                "effective_until": until.isoformat() if until else None,
            }
        ]
    if past and (past[-1]["fee_type"], past[-1]["fee_multiplier"]) != (
        base["fee_type"],
        base["fee_multiplier"],
    ):
        raise InputsSourceError("SCHEDULE_DISAGREES_WITH_SERIES")
    out = [
        {
            **common,
            **base,
            "source": "series",
            "effective_from": past[-1]["scheduled_at"].isoformat() if past else None,
            "effective_until": until.isoformat() if until else None,
        }
    ]
    for i, change in enumerate(future):
        nxt = future[i + 1]["scheduled_at"] if i + 1 < len(future) else None
        out.append(
            {
                **common,
                "fee_type": change["fee_type"],
                "fee_multiplier": change["fee_multiplier"],
                "source": "series",
                "effective_from": change["scheduled_at"].isoformat(),
                "effective_until": nxt.isoformat() if nxt else None,
            }
        )
    return out


def produce_fees(
    reader: Any,
    *,
    markets: list[dict],
    previous: list[dict],
    now: datetime,
    clock=_now_utc,
) -> tuple[list[dict], dict[str, Any]]:
    """Refresh fee evidence for the captured events; keep older evidence only while it
    can still verify a moment (so pending crossings can be resolved with it)."""
    status: dict[str, Any] = {"fee_changes_wire": FEE_CHANGES_WIRE, "events": {}, "series": {}}
    keep = [
        e
        for e in previous
        if isinstance(e, dict)
        and (t := _aware(e.get("observed_at"))) is not None
        and now - t <= timedelta(seconds=FEE_KEEP_SEC)
    ]
    fresh_events = {
        e["event_ticker"]
        for e in keep
        if now - _aware(e["observed_at"]) < timedelta(seconds=FEE_REFRESH_SEC)
    }
    wanted = sorted(
        {
            m["event_ticker"]
            for m in markets
            if isinstance(m.get("event_ticker"), str)
            and m.get("status") in m5_research_sim._OPEN_STATUSES
        }
        - fresh_events
    )[:MAX_FEE_EVENTS_PER_CYCLE]
    by_series: dict[str, list[str]] = {}
    for event in wanted:
        by_series.setdefault(event.split("-", 1)[0], []).append(event)
    new: list[dict] = []
    for series, events in sorted(by_series.items()):
        try:
            base = read_series_schedule(reader, series)
            changes = read_fee_changes(reader, series)
            status["series"][series] = "OK"
        except (InputsSourceError, collector.ResearchError) as exc:
            status["series"][series] = str(exc)
            for event in events:
                status["events"][event] = f"SERIES_{exc}"
            continue
        for event in events:
            try:
                override = read_event_override(reader, event, series)
                observed = clock()  # the instant THIS read returned; never re-dated
                new.extend(
                    fee_entries(
                        series=series,
                        event_ticker=event,
                        base=base,
                        changes=changes,
                        override=override,
                        observed_at=observed,
                    )
                )
                status["events"][event] = "OK"
            except (InputsSourceError, collector.ResearchError) as exc:
                status["events"][event] = str(exc)
    return keep + new, status


# --------------------------------------------------------------------------------------
# Odds pilot (The Odds API, owner-authorized, budgeted).
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PilotConfig:
    key_file: Path
    budget: int
    min_interval_sec: int


def pilot_config(
    env: dict[str, str] | os._Environ[str] | None = None,
) -> tuple[PilotConfig | None, str]:
    env = os.environ if env is None else env
    if env.get("BOTKALSHI_ODDS_PILOT_ENABLED", "").strip().lower() not in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return None, "PILOT_DISABLED"
    key_file = env.get("BOTKALSHI_ODDS_PILOT_KEY_FILE", "").strip()
    if not key_file:
        return None, "OWNER_KEY_FILE_NOT_PROVISIONED"
    try:
        budget = int(env.get("BOTKALSHI_ODDS_PILOT_BUDGET", "0"))
        interval = int(env.get("BOTKALSHI_ODDS_PILOT_MIN_INTERVAL_SEC", "1800"))
    except ValueError:
        return None, "PILOT_CONFIG_INVALID"
    if not 1 <= budget <= MAX_PILOT_BUDGET:
        return None, "PILOT_BUDGET_OUT_OF_RANGE"
    if interval < MIN_PILOT_INTERVAL_SEC:
        return None, "PILOT_INTERVAL_TOO_SHORT"
    return PilotConfig(Path(key_file), budget, interval), "CONFIGURED"


def _read_key(path: Path) -> str:
    """The owner's key file: a plain file, not a symlink, readable by its owner only.
    Its content never leaves this function except inside the request."""
    try:
        info = path.lstat()
    except OSError as exc:
        raise InputsSourceError("KEY_FILE_MISSING") from exc
    if not stat.S_ISREG(info.st_mode):
        raise InputsSourceError("KEY_FILE_NOT_REGULAR")
    if info.st_mode & 0o077:
        raise InputsSourceError("KEY_FILE_PERMISSIONS")
    data = path.read_bytes()
    if not 0 < len(data) <= MAX_KEY_BYTES:
        raise InputsSourceError("KEY_FILE_SIZE")
    key = data.decode("ascii", "strict").strip()
    if not key.isalnum():
        raise InputsSourceError("KEY_FILE_FORMAT")
    return key


def _load_ledger(path: Path, budget: int) -> dict[str, Any]:
    if path.exists():
        ledger = json.loads(path.read_text(encoding="utf-8"))
        if ledger.get("schema_version") != SCHEMA_LEDGER:
            raise InputsSourceError("LEDGER_SCHEMA")
        return ledger
    return {
        "schema_version": SCHEMA_LEDGER,
        "budget_credits": budget,
        "reserved_credits": 0,
        "halted": None,
        "calls": [],
    }


def maybe_fetch_odds(
    reader: Any,
    *,
    data_dir: Path,
    config: PilotConfig | None,
    config_status: str,
    wanted: bool,
    now: datetime,
) -> dict[str, Any]:
    """At most ONE budgeted call, only when there is a pre-match Kalshi MLB event to price.
    Returns the pilot status (never the key)."""
    if config is None:
        return {"status": config_status}
    if not wanted:
        return {"status": "NOT_NEEDED"}
    ledger_path = data_dir / "m5" / "odds-pilot-ledger.json"
    ledger = _load_ledger(ledger_path, config.budget)
    # The budget in force is the SMALLER of the ledger's and the config's: raising the
    # env later cannot re-open a spent pilot without a new ledger decided by the owner.
    budget = min(int(ledger["budget_credits"]), config.budget)
    if ledger.get("halted"):
        return {"status": "HALTED", "reason": ledger["halted"], **_usage(ledger, budget)}
    if ledger["reserved_credits"] + PILOT_COST_PER_CALL > budget:
        return {"status": "BUDGET_EXHAUSTED", **_usage(ledger, budget)}
    last = _aware(ledger["calls"][-1]["reserved_at"]) if ledger["calls"] else None
    if last is not None and now - last < timedelta(seconds=config.min_interval_sec):
        return {"status": "WAITING_INTERVAL", **_usage(ledger, budget)}
    key = _read_key(config.key_file)
    # Reserve BEFORE the request and persist it: a crash after this line still counts.
    ledger["reserved_credits"] += PILOT_COST_PER_CALL
    call = {"reserved_at": now.isoformat(), "status": "RESERVED"}
    ledger["calls"].append(call)
    collector.atomic_write(ledger_path, ledger)
    try:
        body, headers = reader.get_json_with_headers(
            collector.ODDS_ORIGIN,
            f"/sports/{ODDS_SPORT}/odds",
            {"apiKey": key, **PILOT_PARAMS},
            header_names=USAGE_HEADERS,
        )
    except collector.ResearchError as exc:
        call.update(status="ERROR", error=str(exc)[:120])
        collector.atomic_write(ledger_path, ledger)
        return {"status": "ERROR", "error": call["error"], **_usage(ledger, budget)}
    finally:
        del key
    call.update(status="OK", **{h.replace("-", "_"): headers.get(h) for h in USAGE_HEADERS})
    last_cost = headers.get("x-requests-last")
    if last_cost is not None and (not last_cost.isdigit() or int(last_cost) > PILOT_COST_PER_CALL):
        ledger["halted"] = f"UNEXPECTED_COST:{last_cost}"
    if not isinstance(body, list):
        call["status"] = "SHAPE_ERROR"
        collector.atomic_write(ledger_path, ledger)
        return {"status": "SHAPE_ERROR", **_usage(ledger, budget)}
    events = [x for item in body if (x := collector.sanitize_odds_event(item)) is not None]
    collector.atomic_write(
        data_dir / "m5" / "odds-latest.json",
        {
            "fetched_at": now.isoformat(),
            "sport": ODDS_SPORT,
            "params": PILOT_PARAMS,
            "events": events,
        },
    )
    collector.atomic_write(ledger_path, ledger)
    return {"status": "FETCHED", "events": len(events), **_usage(ledger, budget)}


def _usage(ledger: dict[str, Any], budget: int) -> dict[str, Any]:
    return {
        "reserved_credits": ledger["reserved_credits"],
        "budget_credits": budget,
        "calls": len(ledger["calls"]),
    }


# --------------------------------------------------------------------------------------
# Fair (M2's computation over the last fetched odds).
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Outcome:
    name: str
    price: float


@dataclass(frozen=True)
class _Market:
    key: str
    outcomes: tuple[_Outcome, ...]


@dataclass(frozen=True)
class _Book:
    key: str
    last_update: datetime | None
    markets: tuple[_Market, ...]


@dataclass(frozen=True)
class _Event:
    id: str
    sport_key: str
    commence_time: datetime
    bookmakers: tuple[_Book, ...]


def _event(raw: dict, fetched_at: datetime) -> _Event | None:
    commence = _aware(raw.get("commence_time"))
    if commence is None or not isinstance(raw.get("sport_key"), str):
        return None
    books = []
    for book in raw.get("bookmakers") or []:
        update = _aware(book.get("last_update"))
        if update is not None and update > fetched_at + timedelta(seconds=60):
            continue  # a line dated after its own download is not evidence
        markets = tuple(
            _Market(
                str(m.get("key")),
                tuple(_Outcome(o["name"], float(o["price"])) for o in m.get("outcomes") or []),
            )
            for m in book.get("markets") or []
        )
        books.append(_Book(str(book.get("key")), update, markets))
    return _Event(str(raw["id"]), raw["sport_key"], commence, tuple(books))


def produce_fairs(
    odds_doc: dict | None, *, markets: list[dict], now: datetime
) -> tuple[list[dict], dict]:
    diag: dict[str, Any] = {"events": {}}
    if not isinstance(odds_doc, dict):
        diag["status"] = "NO_ODDS"
        return [], diag
    fetched_at = _aware(odds_doc.get("fetched_at"))
    if fetched_at is None:
        diag["status"] = "ODDS_WITHOUT_DATE"
        return [], diag
    odds = [e for raw in odds_doc.get("events") or [] if (e := _event(raw, fetched_at))]
    by_event: dict[str, list[dict]] = {}
    for m in markets:
        if isinstance(m.get("event_ticker"), str):
            by_event.setdefault(m["event_ticker"], []).append(m)
    fairs: list[dict] = []
    for event_key, rows in sorted(by_event.items()):
        names = [r.get("yes_sub_title") for r in rows]
        if len(rows) < 2 or not all(isinstance(n, str) and n for n in names):
            diag["events"][event_key] = "NO_OUTCOME_NAMES"
            continue
        candidates = [
            oe
            for oe in odds
            if series_sport_compatible(event_key, oe.sport_key)
            and match_outcomes(names, h2h_outcome_names(oe)) is not None
        ]
        chosen, why = select_candidate(event_key, candidates, now)
        if chosen is None:
            diag["events"][event_key] = f"NO_MATCH:{why or 'absent'}"
            continue
        fair, stats = consensus_fair_probs(
            chosen,
            min_books=CONSENSUS_MIN_BOOKS,
            max_book_age_min=CONSENSUS_MAX_BOOK_AGE_MIN,
            now=now,
        )
        if not fair or stats["oldest_book_update"] is None:
            diag["events"][event_key] = f"NO_FAIR:{stats['reason'] or 'undated'}"
            continue
        for row, name in zip(rows, names, strict=True):
            prob = fair.get(canonical_name(name))
            text = None if prob is None else f"{prob:.4f}"
            if text is None or not 0 < Fraction(text) < 1:
                continue
            fairs.append(
                {
                    "ticker": row["ticker"],
                    "event_ticker": event_key,
                    "fair_prob": text,
                    "source": "m2-consensus-v1",
                    "observed_at": stats["oldest_book_update"].isoformat(),
                    "commence_time": chosen.commence_time.isoformat(),
                    "odds_event_id": chosen.id,
                    "bookmaker_keys": list(stats["bookmaker_keys"]),
                    "odds_fetched_at": fetched_at.isoformat(),
                }
            )
        diag["events"][event_key] = f"FAIR:{stats['n_books']}_books"
    diag["status"] = "OK"
    return fairs, diag


# --------------------------------------------------------------------------------------
# One producer pass per research cycle.
# --------------------------------------------------------------------------------------


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def produce(
    reader: Any,
    *,
    data_dir: Path,
    packet: dict,
    now: datetime | None = None,
    env: dict[str, str] | None = None,
    clock=_now_utc,
) -> dict[str, Any]:
    now = (now or clock()).astimezone(UTC)
    kalshi = packet.get("kalshi") if isinstance(packet, dict) else None
    markets = [m for m in (kalshi or {}).get("markets") or [] if isinstance(m, dict)]
    inputs_path = data_dir / "m5" / "inputs.json"
    previous = _read_json(inputs_path) or {}
    fees, fee_status = produce_fees(
        reader, markets=markets, previous=previous.get("fees") or [], now=now, clock=clock
    )
    config, config_status = pilot_config(env)
    wanted = any(m.get("status") in m5_research_sim._OPEN_STATUSES for m in markets)
    try:
        pilot = maybe_fetch_odds(
            reader,
            data_dir=data_dir,
            config=config,
            config_status=config_status,
            wanted=wanted,
            now=now,
        )
    except (InputsSourceError, OSError, ValueError) as exc:
        pilot = {"status": "PILOT_ERROR", "error": str(exc)[:120]}
    fairs, fair_diag = produce_fairs(
        _read_json(data_dir / "m5" / "odds-latest.json"), markets=markets, now=now
    )
    document = {
        "schema_version": m5_research_sim.SCHEMA_INPUT,
        "generated_at": now.isoformat(),
        "fairs": fairs,
        "fees": fees,
        "producer": {"fee": fee_status, "odds_pilot": pilot, "fair": fair_diag},
    }
    collector.atomic_write(inputs_path, document)
    return document["producer"]
