"""M5 research REST — simulated market making INSIDE the research service.

Decided by the owner/CTO on 2026-09-22 ("Simular M5 en research"): the research service
runs M5's PURE quoting and fill rules over the books it already captures, and every
quote passes through the shared simulated bank BEFORE it can fill. The Coolify bot is
not touched, no bank is exposed between hosts, and this module has no order client, no
account access and no network of its own (test-guarded).

Cohort: `m5-research-rest-v1` — REST books captured once per research cycle (~60 s),
NOT the WebSocket feed. Its fills are coarser than the Coolify M5 shadow and are never
pooled with it: origin M5, position keys prefixed `m5r1:`, cohort in every evidence.

Order per cycle (CTO, 2026-09-22), each step fail-closed:
  1. validate the new capture (packet id + the M1 strict book review of THIS cycle);
  2. evaluate the previously ACTIVE quotes against that observation;
  3. record their fills/gaps atomically in the bank (inventory = the bank's position);
  4. validate fair, fee and book for new proposals;
  5. compute an INACTIVE candidate (`compute_quote`) and its risk including costs;
  6. admit it (`admit_proposal`: decision + reservation + persisted quote evidence in
     one transaction);
  7. activate it only if admission, reservation and data are still current;
  8. its fills are evaluated only against LATER observations (next cycles).

What is reused, not copied: `review_m1_book` (strict fixed-point book validation,
freshness, integer-cent precision), `compute_quote` (half-spread, inventory skew,
post-only emulation, maker profitability at size), `fills_for_quote` (strict cross,
BBO depth cap), `kalshi_maker_fee_cents` (official maker fee, ceil per fill).

Inputs that research does NOT have are EXPLICIT and verifiable: the fair (source, time,
event, kickoff) and the effective fee schedule of each event come from an inputs file.
Missing or stale → BLOCKED_NO_FAIR / BLOCKED_NO_FEE. No mid as a silent fair, no
invented fee, no paid API. `FairValueBook` is process memory of the Coolify bot;
importing it here would connect nothing.

Risk of a bilateral quote: ONE admission covers BOTH sides. Its bound is
`size × max over quoted sides of (per-contract worst case + 1-contract maker fee)`.
The 1-contract fee bounds the sum of per-fill ceilings (the worst split is one contract
per fill). Within one quote the opening contracts never exceed `size` (every contract
beyond a side's opening closes the other), so the full habitual budget is never granted
again per side.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any

import repo_root  # noqa: F401 — puts the repo root on sys.path (system python3)
import simulation_bank as bank

from src.math.fees import kalshi_maker_fee_cents
from src.strategies.motor_5_mm.quoter import QuoteSet, compute_quote
from src.strategies.motor_5_mm.shadow_fill import fills_for_quote

SCHEMA_INPUT = "botkalshi-m5-research-inputs-v1"
SCHEMA_OUTPUT = "botkalshi-m5-research-cycle-v1"
COHORT = "m5-research-rest-v1"
ORIGIN = "M5"
POSITION_PREFIX = "m5r1:"
FEE_TYPE = "quadratic_with_maker_fees"
_OPEN_STATUSES = frozenset({"open", "active"})
_FEE_SOURCES = frozenset({"series", "event_override"})
_MAX_INPUT_BYTES = 262_144
_MAX_INPUT_ENTRIES = 500
_TICKER_RE = re.compile(r"[A-Z0-9-]{3,100}")
_SOURCE_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_PROB_RE = re.compile(r"0\.[0-9]{1,4}")
_MULT_RE = re.compile(r"(?:[0-9]{1,3}(?:\.[0-9]{1,4})?|[0-9]{1,3}/[1-9][0-9]{0,3})")
_PACKET_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
MAX_INPUT_FUTURE_SKEW = timedelta(seconds=60)


@dataclass(frozen=True)
class Params:
    """F1-v2 parameters of the Coolify M5 (src/utils/config.py), pinned for the cohort.

    Changing one is a NEW cohort, not a tweak: every admission stores them."""

    half_spread_cents: int = 3
    size_contracts: int = 1
    max_inventory_contracts: int = 50
    edge_skew_cents: int = 0
    observable_count_cap: int = 1
    fair_ttl_sec: int = 360
    fee_ttl_sec: int = 3_600
    kickoff_buffer_sec: int = 120
    quote_ttl_sec: int = 600
    jump_retreat_cents: int = 5
    max_tickers: int = 10
    max_observation_gap_sec: int = 180


DEFAULT_PARAMS = Params()


class _BlockedError(Exception):
    def __init__(self, *reasons: str) -> None:
        super().__init__(",".join(reasons))
        self.reasons = list(reasons)


# --------------------------------------------------------------------------------------
# Inputs: explicit, verifiable, fail-closed.
# --------------------------------------------------------------------------------------


def _aware(text: object) -> datetime | None:
    if not isinstance(text, str) or not text or len(text) > 40:
        return None
    raw = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    return moment.astimezone(UTC)


def load_inputs(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Read the inputs file. (None, reason) when absent, oversized or not JSON."""
    try:
        if path.is_symlink():
            return None, "INPUTS_SYMLINK"
        data = path.read_bytes()
    except FileNotFoundError:
        return None, "NO_INPUTS_FILE"
    except OSError:
        return None, "INPUTS_UNREADABLE"
    if len(data) > _MAX_INPUT_BYTES:
        return None, "INPUTS_TOO_LARGE"
    try:
        raw = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError):
        return None, "INPUTS_NOT_JSON"
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_INPUT:
        return None, "INPUTS_WRONG_SCHEMA"
    return raw, None


def _reject_constant(name: str) -> None:
    raise ValueError(f"non-finite JSON constant {name}")


def _index(raw: dict[str, Any] | None, field: str, key: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    items = raw.get(field) if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return out
    for item in items[:_MAX_INPUT_ENTRIES]:
        if isinstance(item, dict) and isinstance(item.get(key), str):
            out.setdefault(item[key], []).append(item)
    return out


def _resolve_fair(entries: list[dict] | None, *, market: dict, now: datetime, p: Params) -> dict:
    if not entries:
        raise _BlockedError("BLOCKED_NO_FAIR")
    if len(entries) > 1:
        raise _BlockedError("BLOCKED_NO_FAIR", "AMBIGUOUS_FAIR")
    item = entries[0]
    prob = item.get("fair_prob")
    if not isinstance(prob, str) or not _PROB_RE.fullmatch(prob) or Fraction(prob) <= 0:
        raise _BlockedError("BLOCKED_NO_FAIR", "INVALID_FAIR_PROB")
    source = item.get("source")
    if not isinstance(source, str) or not _SOURCE_RE.fullmatch(source):
        raise _BlockedError("BLOCKED_NO_FAIR", "INVALID_FAIR_SOURCE")
    if item.get("event_ticker") != market["event_ticker"]:
        raise _BlockedError("BLOCKED_NO_FAIR", "FAIR_EVENT_MISMATCH")
    observed = _aware(item.get("observed_at"))
    if observed is None:
        raise _BlockedError("BLOCKED_NO_FAIR", "INVALID_FAIR_OBSERVED_AT")
    if observed > now + MAX_INPUT_FUTURE_SKEW:
        raise _BlockedError("BLOCKED_NO_FAIR", "FAIR_IN_FUTURE")
    if now - observed > timedelta(seconds=p.fair_ttl_sec):
        raise _BlockedError("BLOCKED_NO_FAIR", "FAIR_STALE")
    kickoff = _aware(item.get("commence_time"))
    if kickoff is None:
        raise _BlockedError("BLOCKED_NO_KICKOFF")
    return {
        "fair_prob": prob,
        "source": source,
        "observed_at": observed.isoformat(),
        "commence_time": kickoff.isoformat(),
    }


def _resolve_fee(entries: list[dict] | None, *, market: dict, now: datetime, p: Params) -> dict:
    """The effective schedule of THIS event: an event-level entry is required, because a
    series schedule alone cannot rule out an event override (fee_policy, 2026-08-22)."""
    if not entries:
        raise _BlockedError("BLOCKED_NO_FEE")
    if len(entries) > 1:
        raise _BlockedError("BLOCKED_NO_FEE", "AMBIGUOUS_FEE")
    item = entries[0]
    if item.get("fee_type") != FEE_TYPE:
        raise _BlockedError("BLOCKED_NO_FEE", "UNSUPPORTED_FEE_TYPE")
    if item.get("source") not in _FEE_SOURCES:
        raise _BlockedError("BLOCKED_NO_FEE", "INVALID_FEE_SOURCE")
    series = market["ticker"].split("-", 1)[0]
    if item.get("series_ticker") != series:
        raise _BlockedError("BLOCKED_NO_FEE", "FEE_SERIES_MISMATCH")
    text = item.get("fee_multiplier")
    if not isinstance(text, str) or not _MULT_RE.fullmatch(text):
        raise _BlockedError("BLOCKED_NO_FEE", "INVALID_FEE_MULTIPLIER")
    multiplier = Fraction(text)
    if not 0 < multiplier <= 10:
        raise _BlockedError("BLOCKED_NO_FEE", "INVALID_FEE_MULTIPLIER")
    observed = _aware(item.get("observed_at"))
    if observed is None:
        raise _BlockedError("BLOCKED_NO_FEE", "INVALID_FEE_OBSERVED_AT")
    if observed > now + MAX_INPUT_FUTURE_SKEW:
        raise _BlockedError("BLOCKED_NO_FEE", "FEE_IN_FUTURE")
    if now - observed > timedelta(seconds=p.fee_ttl_sec):
        raise _BlockedError("BLOCKED_NO_FEE", "FEE_STALE")
    return {
        "fee_type": FEE_TYPE,
        "fee_multiplier": str(multiplier),
        "source": item["source"],
        "series_ticker": series,
        "event_ticker": market["event_ticker"],
        "observed_at": observed.isoformat(),
    }


# --------------------------------------------------------------------------------------
# Capture: the M1 strict review of THIS cycle is the only book M5 sees.
# --------------------------------------------------------------------------------------


def _cents(price_usd: str) -> int:
    value = Fraction(price_usd) * 100
    if value.denominator != 1:  # review_m1_book already refuses these; never round
        raise _BlockedError("BLOCKED_BOOK", "UNSUPPORTED_PRECISION")
    return int(value)


def _book_from_review(review: dict | None, *, packet_id: str) -> dict:
    if not isinstance(review, dict):
        raise _BlockedError("BLOCKED_BOOK", "NO_BOOK_REVIEW")
    if review.get("cycle_id") != packet_id:
        raise _BlockedError("BLOCKED_BOOK", "REVIEW_OF_ANOTHER_CYCLE")
    if review.get("status") != "NO_ARBITRAGE":
        # NO_ARBITRAGE is the only status in which review_m1_book certified a fresh,
        # two-sided, uncrossed book at integer-cent precision with integer depth.
        codes = review.get("reason_codes") if isinstance(review.get("reason_codes"), list) else []
        raise _BlockedError("BLOCKED_BOOK", str(review.get("status")), *[str(c) for c in codes])
    observed = _aware(review.get("book_observed_at"))
    if observed is None:
        raise _BlockedError("BLOCKED_BOOK", "INVALID_BOOK_OBSERVED_AT")
    bid, ask = review["best_yes_bid"], review["yes_ask"]
    bid_depth = Fraction(bid["quantity_contracts"])
    ask_depth = Fraction(ask["quantity_contracts"])
    if bid_depth.denominator != 1 or ask_depth.denominator != 1:
        raise _BlockedError("BLOCKED_BOOK", "UNSUPPORTED_PRECISION")
    return {
        "observed_at": observed,
        "best_yes_bid": _cents(bid["price_usd"]),
        "best_yes_ask": _cents(ask["price_usd"]),
        "bid_depth": int(bid_depth),
        "ask_depth": int(ask_depth),
    }


def _validate_capture(packet: object, m1_review: object) -> tuple[str, dict, dict]:
    if not isinstance(packet, dict):
        raise _BlockedError("BLOCKED_CAPTURE", "NO_PACKET")
    packet_id = packet.get("packet_id")
    if not isinstance(packet_id, str) or not _PACKET_ID_RE.fullmatch(packet_id):
        raise _BlockedError("BLOCKED_CAPTURE", "INVALID_PACKET_ID")
    if _aware(packet.get("generated_at")) is None:
        raise _BlockedError("BLOCKED_CAPTURE", "INVALID_GENERATED_AT")
    if not isinstance(m1_review, dict) or m1_review.get("cycle_id") != packet_id:
        raise _BlockedError("BLOCKED_CAPTURE", "M1_REVIEW_NOT_OF_THIS_CYCLE")
    kalshi = packet.get("kalshi")
    raw_markets = kalshi.get("markets") if isinstance(kalshi, dict) else None
    if not isinstance(raw_markets, list):
        raise _BlockedError("BLOCKED_CAPTURE", "NO_MARKETS")
    markets: dict[str, dict] = {}
    for market in raw_markets:
        if not isinstance(market, dict):
            continue
        ticker = market.get("ticker")
        event = market.get("event_ticker")
        if not isinstance(ticker, str) or not _TICKER_RE.fullmatch(ticker):
            continue
        if not isinstance(event, str) or not _TICKER_RE.fullmatch(event):
            continue
        if ticker in markets:
            markets[ticker] = {"duplicate": True}
            continue
        markets[ticker] = {
            "ticker": ticker,
            "event_ticker": event,
            "status": market.get("status"),
            "close_time": _aware(market.get("close_time")),
        }
    reviews: dict[str, dict] = {}
    for review in m1_review.get("reviews") or []:
        if isinstance(review, dict) and isinstance(review.get("ticker"), str):
            reviews.setdefault(review["ticker"], review)
    return packet_id, markets, reviews


def _market_open(market: dict | None, now: datetime) -> bool:
    return (
        isinstance(market, dict)
        and not market.get("duplicate")
        and market.get("status") in _OPEN_STATUSES
        and market.get("close_time") is not None
        and now < market["close_time"]
    )


# --------------------------------------------------------------------------------------
# Risk and identity.
# --------------------------------------------------------------------------------------


def _per_contract_risk(bid: int | None, ask: int | None, multiplier: Fraction) -> int:
    sides = []
    if bid is not None:
        sides.append(bid + kalshi_maker_fee_cents(1, bid, fee_multiplier=multiplier))
    if ask is not None:
        sides.append(100 - ask + kalshi_maker_fee_cents(1, ask, fee_multiplier=multiplier))
    return max(sides)


def admission_key_for(ticker: str, packet_id: str) -> str:
    """One identity per (cohort, market, generating observation). A replay of the same
    cycle after a restart produces the same key → the stored decision, never a new one."""
    digest = hashlib.sha256(f"{COHORT}|{ticker}|{packet_id}".encode()).hexdigest()
    return f"m5r1:q:{digest[:40]}"


def _usd_to_cents(text: str) -> int:
    whole, _, frac = text.partition(".")
    return int(whole) * 100 + int((frac + "00")[:2])


# --------------------------------------------------------------------------------------
# The cycle.
# --------------------------------------------------------------------------------------


def _live_quotes(db: Path) -> list[dict]:
    return [
        q
        for q in bank.list_admissions(db, origin=ORIGIN)
        if q["position_key"].startswith(POSITION_PREFIX)
        and isinstance(q["evidence"], dict)
        and q["evidence"].get("cohort") == COHORT
        and q["decision"] == "ADMITTED"
        and q["activated_at"] is not None
        and q["withdrawn_at"] is None
    ]


def _evaluate_quote(
    db: Path,
    q: dict,
    *,
    packet_id: str,
    generated_at: datetime,
    reviews: dict,
    now: datetime,
    p: Params,
) -> dict:
    ev = q["evidence"]
    ticker = ev["ticker"]
    row: dict[str, Any] = {"admission_key": q["admission_key"], "ticker": ticker}
    if q["activation_observation"] == packet_id:
        row["status"] = "SKIPPED_GENERATING_OBSERVATION"
        return row
    try:
        book = _book_from_review(reviews.get(ticker), packet_id=packet_id)
    except _BlockedError as gap:
        observed = generated_at
        result = bank.record_quote_observation(
            db,
            admission_key=q["admission_key"],
            observation_id=packet_id,
            observed_at=observed,
            outcome="GAP",
            detail={"reasons": gap.reasons},
            now=now,
        )
        row.update(status="GAP", reasons=gap.reasons, outcome=result["outcome"])
        return row
    last_seen = max(
        filter(None, (_aware(q["activation_observed_at"]), _aware(q["last_observed_at"])))
    )
    detail: dict[str, Any] = {}
    gap_sec = (book["observed_at"] - last_seen).total_seconds()
    if gap_sec > p.max_observation_gap_sec:
        # Fills in the unobserved interval are unknowable: flagged, never invented.
        detail = {"uncertain": True, "unobserved_interval_sec": int(gap_sec)}
    multiplier = Fraction(ev["fee"]["fee_multiplier"])
    fills = []
    for side, price_key in (("buy", "bid_cents"), ("sell", "ask_cents")):
        price = ev[price_key]
        remaining = ev["size"] - q["filled"][side]
        if price is None or remaining <= 0:
            continue
        one_side = QuoteSet(
            ticker=ticker,
            fair_prob=float(Fraction(ev["fair"]["fair_prob"])),
            bid_cents=price if side == "buy" else None,
            ask_cents=price if side == "sell" else None,
            size=remaining,
        )
        for fill in fills_for_quote(
            one_side,
            best_yes_bid=book["best_yes_bid"],
            best_yes_ask=book["best_yes_ask"],
            best_yes_bid_depth=book["bid_depth"],
            best_yes_ask_depth=book["ask_depth"],
            observable_count_cap=p.observable_count_cap,
        ):
            fills.append(
                {
                    "side": fill.side,
                    "price_cents": fill.price_cents,
                    "count": fill.count,
                    "fee_cents": kalshi_maker_fee_cents(
                        fill.count, fill.price_cents, fee_multiplier=multiplier
                    ),
                }
            )
            detail.setdefault("rules", []).append(fill.rule)
    result = bank.record_quote_observation(
        db,
        admission_key=q["admission_key"],
        observation_id=packet_id,
        observed_at=book["observed_at"],
        outcome="EVALUATED",
        fills=fills,
        detail=detail,
        now=now,
    )
    row.update(
        status="EVALUATED",
        fills=fills,
        breaches=result["breaches"],
        outcome=result["outcome"],
        **({"uncertain": True} if detail.get("uncertain") else {}),
    )
    row["_book"] = book
    return row


def _withdraw_reason(
    q: dict, *, book: dict | None, market: dict | None, fair_ok: bool, now: datetime, p: Params
) -> str | None:
    ev = q["evidence"]
    sides = [s for s, k in (("buy", "bid_cents"), ("sell", "ask_cents")) if ev[k] is not None]
    if all(ev["size"] - q["filled"][s] <= 0 for s in sides):
        return "FULLY_FILLED"
    activated = _aware(q["activation_observed_at"])
    if now - activated >= timedelta(seconds=p.quote_ttl_sec):
        return "QUOTE_TTL"
    kickoff = _aware(ev["fair"]["commence_time"])
    if now >= kickoff - timedelta(seconds=p.kickoff_buffer_sec):
        return "PREGAME_CUTOFF"
    if market is not None and not _market_open(market, now):
        return "MARKET_NOT_OPEN"
    if not fair_ok:
        return "FAIR_UNAVAILABLE"
    if book is not None and p.jump_retreat_cents > 0:
        mid_x2 = book["best_yes_bid"] + book["best_yes_ask"]
        if abs(mid_x2 - ev["book"]["mid_x2"]) >= 2 * p.jump_retreat_cents:
            return "MARK_JUMP"
    return None


def run_cycle(
    *,
    bank_db: str | Path,
    packet: object,
    m1_review: object,
    inputs: dict[str, Any] | None,
    inputs_problem: str | None = None,
    now: datetime | None = None,
    params: Params = DEFAULT_PARAMS,
) -> dict[str, Any]:
    """One M5 research cycle. Returns the cycle report; all state lives in the bank."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    db = Path(bank_db)
    report: dict[str, Any] = {
        "schema_version": SCHEMA_OUTPUT,
        "cohort": COHORT,
        "origin": ORIGIN,
        "mode": "SIMULATION_ONLY",
        "data_source": "REST_BOOK_PER_RESEARCH_CYCLE",
        "execution_authorized": False,
        "order_capability_present": False,
        "real_entry_eligible": False,
        "evaluated_at": now.isoformat(),
        "params": asdict(params),
        "inputs_problem": inputs_problem,
        "evaluations": [],
        "withdrawals": [],
        "proposals": [],
        "blocked": {},
        "errors": [],
    }
    # 1. Capture and bank.
    try:
        packet_id, markets, reviews = _validate_capture(packet, m1_review)
    except _BlockedError as exc:
        report.update(status=exc.reasons[0], reasons=exc.reasons)
        return report
    report["cycle_id"] = packet_id
    generated_at = _aware(packet["generated_at"])
    if not db.exists():
        report.update(status="BLOCKED_NO_BANK", reasons=["BANK_NOT_FOUND"])
        return report
    try:
        snapshot = bank.get_snapshot(db, now=now)
    except bank.SimulationBankError as exc:
        report.update(status="BLOCKED_NO_BANK", reasons=[type(exc).__name__])
        return report

    fairs = _index(inputs, "fairs", "ticker")
    fees = _index(inputs, "fees", "event_ticker")

    # An admission that holds a reservation but was never activated (activation refused,
    # or a crash between admission and activation) is not a quote: release its risk,
    # without giving back the daily budget. Its own generating cycle is left alone —
    # that is a replay in progress, which re-activates it idempotently below.
    for q in bank.list_admissions(db, origin=ORIGIN):
        ev = q["evidence"] if isinstance(q["evidence"], dict) else {}
        if (
            ev.get("cohort") == COHORT
            and q["decision"] == "ADMITTED"
            and q["activated_at"] is None
            and q["withdrawn_at"] is None
            and ev.get("generating_observation") != packet_id
        ):
            try:
                done = bank.withdraw_admission(
                    db, admission_key=q["admission_key"], reason="NEVER_ACTIVATED", now=now
                )
            except bank.SimulationBankError as exc:
                report["errors"].append({"admission_key": q["admission_key"], "error": str(exc)})
                continue
            report["withdrawals"].append(
                {
                    "admission_key": q["admission_key"],
                    "ticker": ev.get("ticker"),
                    "reason": "NEVER_ACTIVATED",
                    "reservation_released": done["released"],
                }
            )

    # 2-3. Previously active quotes against THIS observation; then withdrawals.
    live = _live_quotes(db)
    books: dict[str, dict] = {}
    for q in live:
        try:
            row = _evaluate_quote(
                db,
                q,
                packet_id=packet_id,
                generated_at=generated_at,
                reviews=reviews,
                now=now,
                p=params,
            )
        except bank.SimulationBankError as exc:
            report["errors"].append({"admission_key": q["admission_key"], "error": str(exc)})
            continue
        if "_book" in row:
            books[q["admission_key"]] = row.pop("_book")
        report["evaluations"].append(row)
    withdrawn_tickers: set[str] = set()
    for q in _live_quotes(db):
        ticker = q["evidence"]["ticker"]
        market = markets.get(ticker)
        try:
            _resolve_fair(
                fairs.get(ticker),
                market={"event_ticker": q["evidence"]["event_ticker"]},
                now=now,
                p=params,
            )
            fair_ok = True
        except _BlockedError:
            fair_ok = False
        reason = _withdraw_reason(
            q, book=books.get(q["admission_key"]), market=market, fair_ok=fair_ok, now=now, p=params
        )
        if reason is None:
            continue
        try:
            done = bank.withdraw_admission(
                db, admission_key=q["admission_key"], reason=reason, now=now
            )
        except bank.SimulationBankError as exc:
            report["errors"].append({"admission_key": q["admission_key"], "error": str(exc)})
            continue
        withdrawn_tickers.add(ticker)
        report["withdrawals"].append(
            {
                "admission_key": q["admission_key"],
                "ticker": ticker,
                "reason": reason,
                "reservation_released": done["released"],
            }
        )

    # 4-7. New proposals, at most one live quote per market, never re-quoted in the
    # cycle that withdrew it (the jump/expiry that retired it is this very observation).
    live_tickers = {q["evidence"]["ticker"] for q in _live_quotes(db)}
    slots = params.max_tickers - len(live_tickers)
    for ticker in sorted(markets):
        if ticker in live_tickers or ticker in withdrawn_tickers:
            continue
        try:
            proposal = _propose(
                db,
                ticker=ticker,
                market=markets[ticker],
                packet_id=packet_id,
                review=reviews.get(ticker),
                fairs=fairs,
                fees=fees,
                inputs_problem=inputs_problem,
                slots=slots,
                now=now,
                p=params,
            )
        except _BlockedError as exc:
            report["blocked"][ticker] = exc.reasons
            continue
        except bank.SimulationBankError as exc:
            report["errors"].append({"ticker": ticker, "error": str(exc)})
            continue
        report["proposals"].append(proposal)
        if proposal.get("active"):
            slots -= 1

    snapshot = bank.get_snapshot(db, now=now)
    report["bank"] = {
        k: snapshot[k]
        for k in (
            "capital_usd",
            "realized_pnl_usd",
            "reserved_usd",
            "available_usd",
            "open_risk_usd",
            "today_new_risk_usd",
            "week_realized_pnl_usd",
            "risk_day",
        )
    }
    report["bank"]["m5_positions"] = [
        pos
        for pos in snapshot["positions"]
        if pos["origin"] == ORIGIN and pos["position_key"].startswith(POSITION_PREFIX)
    ]
    report["bank"]["m5_breaches"] = [
        b
        for b in snapshot["breaches"]
        if b["origin"] == ORIGIN and b["position_key"].startswith(POSITION_PREFIX)
    ]
    report["status"] = "OK" if not report["errors"] else "OK_WITH_ERRORS"
    return report


def _propose(
    db: Path,
    *,
    ticker: str,
    market: dict,
    packet_id: str,
    review: dict | None,
    fairs: dict,
    fees: dict,
    inputs_problem: str | None,
    slots: int,
    now: datetime,
    p: Params,
) -> dict:
    if slots <= 0:
        raise _BlockedError("MAX_TICKERS")
    if market.get("duplicate"):
        raise _BlockedError("BLOCKED_CAPTURE", "DUPLICATE_MARKET")
    if not _market_open(market, now):
        raise _BlockedError("MARKET_NOT_OPEN")
    position_key = f"{POSITION_PREFIX}{ticker}"
    if inputs_problem is not None:
        raise _BlockedError("BLOCKED_NO_FAIR", inputs_problem)
    # 4. Validate fair, fee and book.
    fair = _resolve_fair(fairs.get(ticker), market=market, now=now, p=p)
    kickoff = _aware(fair["commence_time"])
    if now >= kickoff - timedelta(seconds=p.kickoff_buffer_sec):
        raise _BlockedError("PREGAME_CUTOFF")
    fee = _resolve_fee(fees.get(market["event_ticker"]), market=market, now=now, p=p)
    book = _book_from_review(review, packet_id=packet_id)
    multiplier = Fraction(fee["fee_multiplier"])
    # 5. Inactive candidate, sized FROM the habitual budget (never bumped to fit).
    snapshot = bank.get_snapshot(db, now=now)
    inventory = next(
        (
            pos["net_contracts"]
            for pos in snapshot["positions"]
            if pos["origin"] == ORIGIN and pos["position_key"] == position_key
        ),
        0,
    )
    fair_prob = float(Fraction(fair["fair_prob"]))

    def quote(size: int) -> QuoteSet:
        candidate, skip = compute_quote(
            ticker,
            fair_prob,
            half_spread_cents=p.half_spread_cents,
            size_contracts=size,
            inventory_contracts=inventory,
            max_inventory_contracts=p.max_inventory_contracts,
            best_yes_bid=book["best_yes_bid"],
            best_yes_ask=book["best_yes_ask"],
            edge_skew_cents=p.edge_skew_cents,
            fees_as_maker=True,
            fee_multiplier=multiplier,
        )
        if candidate is None:
            raise _BlockedError("QUOTE_SKIPPED", str(skip))
        return candidate

    candidate = quote(p.size_contracts)
    per_contract = _per_contract_risk(candidate.bid_cents, candidate.ask_cents, multiplier)
    habitual = _usd_to_cents(snapshot["risk_policy"]["habitual"])
    size = min(p.size_contracts, habitual // per_contract)
    if size < 1:
        raise _BlockedError("SIZE_ABOVE_HABITUAL_AT_ONE_CONTRACT")
    if size != p.size_contracts:
        candidate = quote(size)  # profitability re-checked AT the admissible size
    risk = size * per_contract
    key = admission_key_for(ticker, packet_id)
    evidence = {
        "cohort": COHORT,
        "ticker": ticker,
        "event_ticker": market["event_ticker"],
        "bid_cents": candidate.bid_cents,
        "ask_cents": candidate.ask_cents,
        "size": size,
        "risk_bound": "size*max(side worst case + 1-contract maker fee)",
        "fair": fair,
        "fee": fee,
        "generating_observation": packet_id,
        "book": {
            "observed_at": book["observed_at"].isoformat(),
            "best_yes_bid": book["best_yes_bid"],
            "best_yes_ask": book["best_yes_ask"],
            "mid_x2": book["best_yes_bid"] + book["best_yes_ask"],
        },
        "params": asdict(p),
    }
    # 6. Admission + reservation + persisted quote, one transaction.
    decision = bank.admit_proposal(
        db,
        admission_key=key,
        origin=ORIGIN,
        thesis_id=market["event_ticker"],
        position_key=position_key,
        risk_cents=risk,
        proposed_at=book["observed_at"],
        evidence=evidence,
        now=now,
    )
    row = {
        "admission_key": key,
        "ticker": ticker,
        "decision": decision["decision"],
        "outcome": decision["outcome"],
        "reasons": decision["reasons"],
        "risk_usd": f"{risk // 100}.{risk % 100:02d}",
        "bid_cents": candidate.bid_cents,
        "ask_cents": candidate.ask_cents,
        "size": size,
        "active": False,
    }
    if decision["decision"] != "ADMITTED" or not decision["reservation_active"]:
        return row
    # 7. Active only if admission, reservation and data are still current.
    activation = bank.activate_admission(
        db,
        admission_key=key,
        observation_id=packet_id,
        observed_at=book["observed_at"],
        now=now,
    )
    row["active"] = activation["active"]
    row["activation"] = (
        activation["outcome"] if activation["reason"] is None else activation["reason"]
    )
    if not activation["active"]:
        # Admitted but not executable: its reservation must not linger.
        bank.withdraw_admission(db, admission_key=key, reason="NOT_ACTIVATED", now=now)
    return row
