"""US500 research core: public observations and explicit offline cost scenarios.

No account access, order routing, strategy signals, leverage or margin simulation.
All financial input is decimal text. Prices are already USD per API unit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from decimal import ROUND_CEILING, Decimal, localcontext
from pathlib import Path

SCHEMA = "us500-public-snapshot-v1"
HOSTS = {
    "public": ("https://external-api.kalshi.com", "KXUS500PERP"),
    "demo": ("https://external-api.demo.kalshi.co", "KXUS500PERP1"),
}
MAX_BYTES = 2_000_000
MAX_AGE_MS = 5_000  # Local research policy, not a venue guarantee.
MAX_PRICE = Decimal(1000000000)  # Reject sentinel/unsupported domain.
DECIMAL_TEXT = re.compile(r"-?(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,12})?\Z")


class DataError(ValueError):
    """A bounded, non-payload-bearing validation error."""


def number(value: object, *, positive: bool = False) -> Decimal:
    if not isinstance(value, str) or not DECIMAL_TEXT.fullmatch(value):
        raise DataError("INVALID_DECIMAL_TEXT")
    result = Decimal(value)
    if positive and result <= 0:
        raise DataError("NONPOSITIVE_VALUE")
    return result


def timestamp(value: object) -> int:
    if type(value) is not int or not 946684800000 <= value <= 4102444800000:
        raise DataError("INVALID_EPOCH_MILLISECONDS")
    return value


def _unique(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise DataError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise DataError("NONFINITE_JSON")


def decode(raw: bytes) -> dict:
    if len(raw) > MAX_BYTES:
        raise DataError("RESPONSE_TOO_LARGE")
    try:
        result = json.loads(
            raw,
            object_pairs_hook=_unique,
            parse_float=Decimal,
            parse_constant=_reject_constant,
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise DataError("INVALID_JSON") from exc
    if not isinstance(result, dict):
        raise DataError("JSON_OBJECT_REQUIRED")
    return result


def save_new(path: Path, value: dict) -> None:
    # Exclusive create: never replace existing evidence or a symlink target.
    with path.open("x", encoding="utf-8") as output:
        json.dump(
            value, output, indent=2, ensure_ascii=False, default=str, allow_nan=False
        )
        output.write("\n")


def body_digest(body: dict) -> str:
    try:
        canonical = json.dumps(
            body, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise DataError("INVALID_CANONICAL_BODY") from exc
    return hashlib.sha256(canonical).hexdigest()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DataError("REDIRECT_REJECTED")


class PublicReader:
    """At most two anonymous GET requests to fixed, documented public resources."""

    def __init__(self, environment: str = "public"):
        if environment not in HOSTS:
            raise DataError("INVALID_ENVIRONMENT")
        self.environment = environment
        self.host, self.ticker = HOSTS[environment]
        self.requests = 0
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    def get(self, resource: str) -> dict:
        if resource not in ("market", "orderbook"):
            raise DataError("RESOURCE_NOT_ALLOWED")
        if self.requests >= 2:
            raise DataError("REQUEST_BUDGET_EXHAUSTED")
        suffix = "" if resource == "market" else "/orderbook?depth=20"
        url = f"{self.host}/trade-api/v2/margin/markets/{self.ticker}{suffix}"
        self.requests += 1
        started = time.time_ns() // 1_000_000
        req = urllib.request.Request(
            url, headers={"Accept": "application/json"}, method="GET"
        )
        try:
            with self.opener.open(req, timeout=5) as response:
                raw = response.read(MAX_BYTES + 1)
                status = response.status
            if status != 200:
                raise DataError("NON_200_RESPONSE")
            body = decode(raw)
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise DataError(f"HTTP_{code}") from None
        except (OSError, urllib.error.URLError) as exc:
            raise DataError("PUBLIC_DATA_UNAVAILABLE") from exc
        return {
            "url": url,
            "http_status": status,
            "requested_at_ms": started,
            "received_at_ms": time.time_ns() // 1_000_000,
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "canonical_body_sha256": body_digest(body),
            "body": body,
        }

    def capture(self) -> dict:
        observations = {name: self.get(name) for name in ("market", "orderbook")}
        return {
            "schema": SCHEMA,
            "environment": self.environment,
            "synthetic": False,
            "ticker": self.ticker,
            "observations": observations,
            "atomic_snapshot": False,
            "authenticated": False,
        }


def _price(value: object) -> Decimal:
    result = number(value, positive=True)
    if result >= MAX_PRICE:
        raise DataError("PRICE_OUTSIDE_RESEARCH_DOMAIN")
    return result


def _levels(raw: object, tick: Decimal, fractional: bool, descending: bool) -> list:
    if not isinstance(raw, list) or not raw or len(raw) > 1000:
        raise DataError("EMPTY_OR_INVALID_BOOK_SIDE")
    levels = []
    seen = set()
    with localcontext() as ctx:
        ctx.prec = 64
        for item in raw:
            if not isinstance(item, list) or len(item) != 2:
                raise DataError("INVALID_BOOK_LEVEL")
            price, quantity = _price(item[0]), number(item[1], positive=True)
            if price % tick:
                raise DataError("OFF_TICK_PRICE")
            if quantity % Decimal("0.01"):
                raise DataError("OFF_API_QUANTITY_STEP")
            if not fractional and quantity != quantity.to_integral_value():
                raise DataError("FRACTIONAL_QUANTITY_UNSUPPORTED")
            if price in seen:
                raise DataError("DUPLICATE_BOOK_PRICE")
            seen.add(price)
            levels.append((price, quantity))
    return sorted(levels, reverse=descending)


def normalize(snapshot: dict, *, at_ms: int) -> dict:
    """Validate at an explicit observation time; never substitute today's time in replay."""
    now = timestamp(at_ms)
    try:
        env = snapshot["environment"]
        if snapshot["schema"] != SCHEMA or env not in HOSTS:
            raise DataError("INVALID_SNAPSHOT_SCHEMA")
        if type(snapshot["synthetic"]) is not bool:
            raise DataError("MISSING_PROVENANCE")
        ticker = HOSTS[env][1]
        if snapshot["ticker"] != ticker:
            raise DataError("TICKER_MISMATCH")
        observations = snapshot["observations"]
        received = []
        integrity_checked = True
        for name in ("market", "orderbook"):
            obs = observations[name]
            if not isinstance(obs, dict):
                raise DataError("MALFORMED_OBSERVATION")
            digest = obs.get("canonical_body_sha256")
            if digest is None:
                integrity_checked = False
            elif digest != body_digest(obs["body"]):
                raise DataError("BODY_DIGEST_MISMATCH")
            if obs["http_status"] != 200:
                raise DataError("INCOMPLETE_CAPTURE")
            start, end = (
                timestamp(obs["requested_at_ms"]),
                timestamp(obs["received_at_ms"]),
            )
            if not start <= end <= now:
                raise DataError("INVALID_CAPTURE_CLOCK")
            if end - start > MAX_AGE_MS or now - end > MAX_AGE_MS:
                raise DataError("STALE_OR_SLOW_CAPTURE")
            expected = f"{HOSTS[env][0]}/trade-api/v2/margin/markets/{ticker}"
            if name == "orderbook":
                expected += "/orderbook?depth=20"
            if obs["url"] != expected:
                raise DataError("SOURCE_MISMATCH")
            received.append(end)
        if max(received) - min(received) > MAX_AGE_MS:
            raise DataError("NONCONTEMPORANEOUS_CAPTURE")
        market = observations["market"]["body"]["market"]
        if market["ticker"] != ticker:
            raise DataError("MARKET_TICKER_MISMATCH")
        if market["status"] != "active":
            raise DataError("MARKET_NOT_ACTIVE")
        if market["schedule"]["is_open"] is not True:
            raise DataError("MARKET_CLOSED")
        tick = _price(market["tick_size"])
        size = number(market["contract_size"], positive=True)
        multiplier = number(market["underlying_multiplier"], positive=True)
        fractional = market["fractional_trading_enabled"]
        if type(fractional) is not bool:
            raise DataError("INVALID_FRACTIONAL_FLAG")
        book = observations["orderbook"]["body"]["orderbook"]
        bids = _levels(book["bids"], tick, fractional, True)
        asks = _levels(book["asks"], tick, fractional, False)
        if bids[0][0] >= asks[0][0]:
            raise DataError("LOCKED_OR_CROSSED_BOOK")
        reference, reference_age, basis = None, None, None
        reference_state = "MISSING"
        if market.get("reference_price"):
            ref = market["reference_price"]
            reference = _price(ref["price"])
            reference_age = now - timestamp(ref["ts_ms"])
            if reference_age < 0:
                raise DataError("FUTURE_REFERENCE")
            reference_state = "FRESH" if reference_age <= 60000 else "STALE"
        with localcontext() as ctx:
            ctx.prec = 64
            mid = (bids[0][0] + asks[0][0]) / 2
            spread = (asks[0][0] - bids[0][0]) / mid * 10000
            if reference_state == "FRESH":
                basis = (mid - reference) / reference * 10000
        return {
            "ticker": ticker,
            "environment": env,
            "synthetic": snapshot["synthetic"],
            "payload_integrity_checked": integrity_checked,
            "source_authenticity_verified": False,
            "observed_at_ms": max(received),
            "tick_size": tick,
            "contract_size": size,
            "underlying_multiplier": multiplier,
            "fractional_trading_enabled": fractional,
            "bids": bids,
            "asks": asks,
            "mid_usd_per_api_unit": mid,
            "spread_bps": spread,
            "reference_state": reference_state,
            "reference_age_ms": reference_age,
            "basis_bps": basis,
        }
    except (KeyError, TypeError, IndexError) as exc:
        raise DataError("MALFORMED_SNAPSHOT") from exc


def assess(snapshot: dict, *, at_ms: int) -> dict:
    result = {
        "schema": "us500-analysis-v1",
        "evaluated_at_ms": at_ms,
        "execution_available": False,
        "strategy_validated": False,
        "atomic_snapshot": False,
        "orderbook_exchange_timestamp_available": False,
        "source_authenticity_verified": False,
    }
    try:
        result["observation"] = normalize(snapshot, at_ms=at_ms)
        result["data_state"] = "VALID_FOR_DESCRIPTIVE_ANALYSIS"
    except DataError as exc:
        result.update(data_state="INCOMPLETE_OR_UNUSABLE", reason=str(exc))
    return result


def _walk(levels: list, quantity: Decimal) -> Decimal:
    left, notional = quantity, Decimal(0)
    for price, available in levels:
        used = min(left, available)
        notional += used * price
        left -= used
        if left == 0:
            return notional
    raise DataError("INSUFFICIENT_VISIBLE_DEPTH")


def simulate_round_trip(
    entry: dict,
    exit_snapshot: dict,
    *,
    direction: str,
    quantity: str,
    fee_bps_per_leg: str,
    fee_quantum_usd: str,
    slippage_bps_per_leg: str,
    funding_cashflow_usd: str,
) -> dict:
    """Hypothetical fully closed linear position, never a strategy or actual fill.

    Funding is a required signed scenario input: positive received, negative paid.
    It is NOT inferred to be zero. Fees are taker-style scenario assumptions.
    """
    try:
        entry_time = max(
            timestamp(entry["observations"][name]["received_at_ms"])
            for name in ("market", "orderbook")
        )
        exit_time = max(
            timestamp(exit_snapshot["observations"][name]["received_at_ms"])
            for name in ("market", "orderbook")
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise DataError("MALFORMED_SCENARIO") from exc
    a = normalize(entry, at_ms=entry_time)
    b = normalize(exit_snapshot, at_ms=exit_time)
    if exit_time <= entry_time:
        raise DataError("NONINCREASING_SCENARIO_TIME")
    for key in (
        "ticker",
        "environment",
        "synthetic",
        "tick_size",
        "contract_size",
        "underlying_multiplier",
        "fractional_trading_enabled",
    ):
        if a[key] != b[key]:
            raise DataError("INCOMPATIBLE_SCENARIO_SNAPSHOTS")
    if direction not in ("long", "short"):
        raise DataError("INVALID_DIRECTION")
    qty = number(quantity, positive=True)
    if qty % Decimal("0.01"):
        raise DataError("OFF_API_QUANTITY_STEP")
    if not a["fractional_trading_enabled"] and qty != qty.to_integral_value():
        raise DataError("FRACTIONAL_QUANTITY_UNSUPPORTED")
    fee = number(fee_bps_per_leg)
    slip = number(slippage_bps_per_leg)
    if not 0 <= fee <= 10000 or not 0 <= slip < 10000:
        raise DataError("INVALID_SCENARIO_COST")
    quantum = number(fee_quantum_usd, positive=True)
    funding = number(funding_cashflow_usd)
    with localcontext() as ctx:
        ctx.prec = 80
        entry_notional = _walk(a["asks"] if direction == "long" else a["bids"], qty)
        exit_notional = _walk(b["bids"] if direction == "long" else b["asks"], qty)
        entry_slip, exit_slip = (
            entry_notional * slip / 10000,
            exit_notional * slip / 10000,
        )
        # Adverse slippage changes modeled fill and the fee-bearing notional.
        entry_fill = entry_notional + (
            entry_slip if direction == "long" else -entry_slip
        )
        exit_fill = exit_notional + (-exit_slip if direction == "long" else exit_slip)
        gross_at_book = (exit_notional - entry_notional) * (
            1 if direction == "long" else -1
        )
        fees = sum(
            (n * fee / 10000 / quantum).to_integral_value(rounding=ROUND_CEILING)
            * quantum
            for n in (entry_fill, exit_fill)
        )
        slippage_total = entry_slip + exit_slip
        gross = gross_at_book - slippage_total
        net = gross - fees + funding
    return {
        "schema": "us500-hypothetical-round-trip-v1",
        "direction": direction,
        "synthetic_input": a["synthetic"],
        "quantity_api_units": qty,
        "entry_notional_usd": entry_fill,
        "exit_notional_usd": exit_fill,
        "gross_at_observed_book_usd": gross_at_book,
        "slippage_cost_usd": slippage_total,
        "fees_usd": fees,
        "funding_cashflow_usd": funding,
        "net_before_margin_and_infrastructure_usd": net,
        "assumptions": {
            "fee_bps_per_leg": fee,
            "fee_quantum_usd": quantum,
            "slippage_bps_per_leg": slip,
            "funding_provided_by_caller": True,
            "usd_price_per_api_unit": True,
            "contract_size_applied_again": False,
            "full_visible_depth_available": True,
        },
        "actual_fill": False,
        "margin_and_liquidation_modeled": False,
        "strategy_validated": False,
        "execution_available": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser(
        "capture", help="Two anonymous public GETs, then exit"
    )
    capture.add_argument("--environment", choices=tuple(HOSTS), default="public")
    capture.add_argument("--output", type=Path, required=True)
    analyze = commands.add_parser("analyze", help="Offline descriptive analysis")
    analyze.add_argument("--input", type=Path, required=True)
    analyze.add_argument("--at-ms", type=int, required=True)
    analyze.add_argument("--output", type=Path, required=True)
    scenario = commands.add_parser(
        "scenario", help="Offline explicitly assumed round trip"
    )
    scenario.add_argument("--input", type=Path, required=True)
    scenario.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.output.exists() or args.output.is_symlink():
            raise DataError("OUTPUT_ALREADY_EXISTS")
        if args.command == "capture":
            result = PublicReader(args.environment).capture()
        elif args.command == "analyze":
            result = assess(decode(args.input.read_bytes()), at_ms=args.at_ms)
        else:
            data = decode(args.input.read_bytes())
            result = simulate_round_trip(
                data["entry"], data["exit"], **data["assumptions"]
            )
        save_new(args.output, result)
    except (DataError, OSError, KeyError, TypeError) as exc:
        print(
            json.dumps(
                {
                    "error": str(exc)
                    if isinstance(exc, DataError)
                    else "LOCAL_IO_OR_SCHEMA_ERROR"
                }
            )
        )
        return 2
    print(json.dumps({"saved": str(args.output), "execution_available": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
