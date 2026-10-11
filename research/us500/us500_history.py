"""Bounded anonymous US500 Perps history capture and offline evidence verification.

Only public trades, one-minute candles, and historical funding rates are supported.
No orders, accounts, authentication, credentials, or background collection.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from us500_engine import (
    HOSTS,
    MAX_BYTES,
    MAX_PRICE,
    DataError,
    body_digest,
    decode,
    number,
    save_new,
    timestamp,
)

SCHEMA = "us500-public-history-v1"
RESOURCES = ("trades", "candles", "funding")
MAX_PAGES = 20
MAX_DATASET_BYTES = 160_000_000
DAY_MS = 86_400_000
MINUTE_MS = 60_000
RFC3339 = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z"
)


def _range(start_ms, end_ms):
    start, end = timestamp(start_ms), timestamp(end_ms)
    if end < start or end - start > 366 * DAY_MS:
        raise DataError("INVALID_HISTORY_RANGE")
    return start, end


def _budget(value):
    if type(value) is not int or not 1 <= value <= MAX_PAGES:
        raise DataError("INVALID_PAGE_BUDGET")
    return value


def _cursor(value):
    if (
        not isinstance(value, str)
        or len(value) > 4096
        or any(ord(c) < 32 for c in value)
    ):
        raise DataError("INVALID_CURSOR")
    return value


def _request_url(environment, resource, start_ms, end_ms, cursor=""):
    if environment not in HOSTS or resource not in RESOURCES:
        raise DataError("RESOURCE_OR_ENVIRONMENT_NOT_ALLOWED")
    start, end = _range(start_ms, end_ms)
    host, ticker = HOSTS[environment]
    # REST uses Unix seconds. Widen to include both exact millisecond boundaries,
    # then filter returned records against the requested millisecond interval.
    low, high = start // 1000 - 1, (end + 999) // 1000 + 1
    if resource == "trades":
        path = "/trade-api/v2/margin/trades"
        query = {"ticker": ticker, "min_ts": low, "max_ts": high, "limit": 1000}
        if cursor:
            query["cursor"] = _cursor(cursor)
    elif resource == "candles":
        path = f"/trade-api/v2/margin/markets/{ticker}/candlesticks"
        query = {
            "start_ts": low,
            "end_ts": high,
            "period_interval": 1,
            "include_latest_before_start": "false",
        }
    else:
        path = "/trade-api/v2/margin/funding_rates/historical"
        query = {"ticker": ticker, "start_ts": low, "end_ts": high}
    if resource != "trades" and cursor:
        raise DataError("UNSUPPORTED_CURSOR")
    return host + path + "?" + urllib.parse.urlencode(query)


def _rfc_ms(value):
    if not isinstance(value, str) or not RFC3339.fullmatch(value):
        raise DataError("INVALID_HISTORY_TIMESTAMP")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        elapsed = parsed.astimezone(timezone.utc) - datetime(
            1970, 1, 1, tzinfo=timezone.utc
        )
        result = (
            elapsed.days * DAY_MS
            + elapsed.seconds * 1000
            + elapsed.microseconds // 1000
        )
        return timestamp(result)
    except (ValueError, OverflowError) as exc:
        raise DataError("INVALID_HISTORY_TIMESTAMP") from exc


def _price(value):
    result = number(value, positive=True)
    if result >= MAX_PRICE:
        raise DataError("HISTORY_PRICE_OUTSIDE_DOMAIN")
    return result


def _nonnegative(value):
    result = number(value)
    if result < 0:
        raise DataError("NEGATIVE_HISTORY_QUANTITY")
    return result


def _funding_rate(value):
    # OpenAPI defines funding_rate as a JSON number. decode parses it directly
    # to Decimal, so an IEEE binary float must never pass through this path.
    if isinstance(value, Decimal) or type(value) is int:
        result = Decimal(value)
    elif isinstance(value, str):
        result = number(value)
    else:
        raise DataError("INVALID_FUNDING_RATE")
    if not result.is_finite() or abs(result) > 1 or result.as_tuple().exponent < -30:
        raise DataError("FUNDING_RATE_OUTSIDE_RESEARCH_DOMAIN")
    return result


def _ohlc(value):
    if value is None:
        return {key: None for key in ("open", "high", "low", "close")}
    if not isinstance(value, dict):
        raise DataError("INVALID_CANDLE_OHLC")
    result = {}
    for key in ("open", "high", "low", "close"):
        raw = value.get(key)
        result[key] = None if raw is None else _price(raw)
    available = [v for v in result.values() if v is not None]
    if available and len(available) != 4:
        raise DataError("PARTIAL_CANDLE_OHLC")
    if (
        available
        and not result["low"]
        <= min(result["open"], result["close"])
        <= max(result["open"], result["close"])
        <= result["high"]
    ):
        raise DataError("INCONSISTENT_CANDLE_OHLC")
    return result


def _records(body, resource, ticker):
    if not isinstance(body, dict):
        raise DataError("INVALID_HISTORY_BODY")
    key = {"trades": "trades", "candles": "candlesticks", "funding": "funding_rates"}[
        resource
    ]
    rows = body.get(key)
    if not isinstance(rows, list) or len(rows) > 10000:
        raise DataError("INVALID_HISTORY_ROWS")
    if resource == "candles" and body.get("ticker") != ticker:
        raise DataError("HISTORY_TICKER_MISMATCH")
    records = []
    try:
        for row in rows:
            if not isinstance(row, dict):
                raise DataError("INVALID_HISTORY_ROW")
            if resource == "trades":
                if row["ticker"] != ticker:
                    raise DataError("HISTORY_TICKER_MISMATCH")
                identifier = row["trade_id"]
                if (
                    not isinstance(identifier, str)
                    or not identifier
                    or len(identifier) > 256
                ):
                    raise DataError("INVALID_TRADE_ID")
                side = row["taker_side"]
                if side not in ("bid", "ask"):
                    raise DataError("INVALID_TRADE_SIDE")
                record = {
                    "id": identifier,
                    "timestamp_ms": _rfc_ms(row["created_time"]),
                    "ticker": ticker,
                    "price_usd_per_api_unit": _price(row["price"]),
                    "quantity_api_units": number(row["count"], positive=True),
                    "taker_side": side,
                }
            elif resource == "candles":
                ts = row["end_period_ts"]
                if type(ts) is not int:
                    raise DataError("INVALID_CANDLE_TIMESTAMP")
                ts = timestamp(ts * 1000)
                if ts % MINUTE_MS:
                    raise DataError("UNALIGNED_CANDLE_TIMESTAMP")
                record = {
                    "id": str(ts),
                    "timestamp_ms": ts,
                    "ticker": ticker,
                    "bid": _ohlc(row.get("bid")),
                    "ask": _ohlc(row.get("ask")),
                    "price": _ohlc(row.get("price")),
                    "volume_api_units": _nonnegative(row["volume"]),
                    "open_interest_api_units": None
                    if row.get("open_interest") is None
                    else _nonnegative(row["open_interest"]),
                }
            else:
                if row["market_ticker"] != ticker:
                    raise DataError("HISTORY_TICKER_MISMATCH")
                ts = _rfc_ms(row["funding_time"])
                record = {
                    "id": str(ts),
                    "timestamp_ms": ts,
                    "ticker": ticker,
                    "funding_rate": _funding_rate(row["funding_rate"]),
                    "mark_price_usd_per_api_unit": _price(row["mark_price"]),
                }
            records.append(record)
    except (KeyError, TypeError, IndexError) as exc:
        raise DataError("MALFORMED_HISTORY_RECORD") from exc
    next_cursor = _cursor(body.get("cursor")) if resource == "trades" else ""
    if resource != "trades" and body.get("cursor") not in (None, ""):
        raise DataError("UNEXPECTED_HISTORY_PAGINATION")
    return records, next_cursor


def _windows(start_ms, end_ms, resource):
    if resource == "trades":
        return [(start_ms, end_ms)]
    return [
        (start, min(start + DAY_MS - 1, end_ms))
        for start in range(start_ms, end_ms + 1, DAY_MS)
    ]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DataError("REDIRECT_REJECTED")


class PublicHistoryReader:
    """One bounded capture using allowlisted public resources and a shared budget."""

    def __init__(self, environment="public", max_pages=3, transport=None):
        if environment not in HOSTS:
            raise DataError("INVALID_ENVIRONMENT")
        self.environment = environment
        self.max_pages = _budget(max_pages)
        self.requests = 0
        self.used = False
        self.transport = (
            transport
            if transport is not None
            else urllib.request.build_opener(
                urllib.request.ProxyHandler({}), _NoRedirect()
            )
        )

    def _get(self, resource, start_ms, end_ms, cursor):
        url = _request_url(self.environment, resource, start_ms, end_ms, cursor)
        if self.requests >= self.max_pages:
            raise DataError("HISTORY_REQUEST_BUDGET_EXHAUSTED")
        self.requests += 1
        started = time.time_ns() // 1_000_000
        request = urllib.request.Request(
            url, headers={"Accept": "application/json"}, method="GET"
        )
        try:
            with self.transport.open(request, timeout=5) as response:
                if response.status != 200:
                    raise DataError("NON_200_HISTORY_RESPONSE")
                raw = response.read(MAX_BYTES + 1)
            body = decode(raw)
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise DataError(f"HISTORY_HTTP_{code}") from None
        except (OSError, urllib.error.URLError):
            raise DataError("HISTORY_NETWORK_UNAVAILABLE") from None
        return {
            "url": url,
            "window_start_ms": start_ms,
            "window_end_ms": end_ms,
            "cursor_in": cursor,
            "requested_at_ms": started,
            "received_at_ms": time.time_ns() // 1_000_000,
            "http_status": 200,
            "raw_base64": base64.b64encode(raw).decode("ascii"),
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "canonical_body_sha256": body_digest(body),
            "body": body,
        }

    def capture(self, resource, start_ms, end_ms):
        if resource not in RESOURCES:
            raise DataError("HISTORY_RESOURCE_NOT_ALLOWED")
        start, end = _range(start_ms, end_ms)
        if self.used:
            raise DataError("HISTORY_READER_ALREADY_USED")
        self.used = True
        result = {
            "schema": SCHEMA,
            "environment": self.environment,
            "ticker": HOSTS[self.environment][1],
            "resource": resource,
            "requested_range_ms": {"start": start, "end": end},
            "period_minutes": 1 if resource == "candles" else None,
            "max_pages": self.max_pages,
            "requests_made": 0,
            "pages": [],
            "capture_stop_reason": None,
            "authenticated": False,
            "execution_available": False,
            "source_authenticity_verified": False,
        }
        seen_records, cursors = {}, set()
        try:
            for window_start, window_end in _windows(start, end, resource):
                cursor = ""
                while True:
                    page = self._get(resource, window_start, window_end, cursor)
                    result["pages"].append(page)
                    rows, following = _records(page["body"], resource, result["ticker"])
                    for row in rows:
                        if not window_start <= row["timestamp_ms"] <= window_end:
                            continue
                        previous = seen_records.get(row["id"])
                        if previous is not None and previous != row:
                            raise DataError("CONFLICTING_HISTORY_RECORD")
                        seen_records[row["id"]] = row
                    if not following:
                        break
                    if following in cursors or following == cursor:
                        raise DataError("REPEATED_HISTORY_CURSOR")
                    if not rows:
                        raise DataError("EMPTY_HISTORY_PAGE_WITH_CURSOR")
                    cursors.add(following)
                    cursor = following
        except DataError as exc:
            result["capture_stop_reason"] = str(exc)
        result["requests_made"] = self.requests
        result["analysis"] = analyze_history(result)
        return result


def _page_body(page):
    if not isinstance(page, dict) or page.get("http_status") != 200:
        raise DataError("INVALID_HISTORY_PAGE")
    try:
        encoded = page["raw_base64"]
        if not isinstance(encoded, str) or len(encoded) > ((MAX_BYTES + 2) // 3) * 4:
            raise DataError("INVALID_HISTORY_RAW_BYTES")
        raw = base64.b64decode(encoded, validate=True)
        if hashlib.sha256(raw).hexdigest() != page["raw_sha256"]:
            raise DataError("HISTORY_RAW_DIGEST_MISMATCH")
        body = decode(raw)
        digest = body_digest(body)
        if (
            digest != page["canonical_body_sha256"]
            or body_digest(page["body"]) != digest
        ):
            raise DataError("HISTORY_BODY_DIGEST_MISMATCH")
        if timestamp(page["received_at_ms"]) < timestamp(page["requested_at_ms"]):
            raise DataError("INVALID_HISTORY_CAPTURE_CLOCK")
        return body
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        if isinstance(exc, DataError):
            raise
        raise DataError("MALFORMED_HISTORY_PAGE") from exc


def _candle_gaps(records, start, end):
    first = ((start + MINUTE_MS - 1) // MINUTE_MS) * MINUTE_MS
    last = (end // MINUTE_MS) * MINUTE_MS
    expected = max(0, (last - first) // MINUTE_MS + 1)
    cursor, missing, gaps, gap_count = first, 0, [], 0
    for observed in [r["timestamp_ms"] for r in records] + [last + MINUTE_MS]:
        if observed > cursor:
            count = (observed - cursor) // MINUTE_MS
            missing += count
            gap_count += 1
            if len(gaps) < 100:
                gaps.append(
                    {
                        "start_ms": cursor,
                        "end_ms": observed - MINUTE_MS,
                        "missing_candles": count,
                    }
                )
        cursor = observed + MINUTE_MS
    return {
        "expected_minute_boundaries": expected,
        "missing_candles": missing,
        "gap_ranges": gaps,
        "gap_range_count": gap_count,
        "gap_ranges_truncated": gap_count > len(gaps),
        "calendar_verified": False,
        "interpretation": "Missing rows may be closure, no activity, or missing data; not imputed.",
    }


def analyze_history(dataset):
    """Re-derive records and coverage from retained bytes; never trust stored analysis."""
    report = {
        "schema": "us500-history-analysis-v1",
        "valid_evidence": False,
        "retrieval_complete": False,
        "history_sufficient": False,
        "source_authenticity_verified": False,
        "strategy_validated": False,
        "execution_available": False,
        "errors": [],
        "records": [],
    }
    try:
        if not isinstance(dataset, dict) or dataset.get("schema") != SCHEMA:
            raise DataError("INVALID_HISTORY_SCHEMA")
        environment, resource = dataset["environment"], dataset["resource"]
        if environment not in HOSTS or resource not in RESOURCES:
            raise DataError("RESOURCE_OR_ENVIRONMENT_NOT_ALLOWED")
        ticker = HOSTS[environment][1]
        if dataset["ticker"] != ticker or dataset.get("authenticated") is not False:
            raise DataError("HISTORY_PROVENANCE_MISMATCH")
        start, end = _range(
            dataset["requested_range_ms"]["start"], dataset["requested_range_ms"]["end"]
        )
        maximum = _budget(dataset["max_pages"])
        pages = dataset["pages"]
        if not isinstance(pages, list) or len(pages) > maximum:
            raise DataError("INVALID_HISTORY_PAGE_COUNT")
        if (
            type(dataset["requests_made"]) is not int
            or not len(pages) <= dataset["requests_made"] <= maximum
        ):
            raise DataError("INVALID_HISTORY_REQUEST_COUNT")
        if dataset.get("capture_stop_reason") is None and dataset[
            "requests_made"
        ] != len(pages):
            raise DataError("UNACCOUNTED_HISTORY_REQUEST")
        if dataset.get("period_minutes") != (1 if resource == "candles" else None):
            raise DataError("INVALID_HISTORY_PERIOD")
        windows = _windows(start, end, resource)
        window_index, cursor = 0, ""
        seen_cursors, unique = set(), {}
        duplicates, empty_pages, excluded = 0, 0, 0
        for page in pages:
            if window_index >= len(windows):
                raise DataError("UNEXPECTED_EXTRA_HISTORY_PAGE")
            window_start, window_end = windows[window_index]
            if (
                not isinstance(page, dict)
                or page.get("window_start_ms") != window_start
                or page.get("window_end_ms") != window_end
                or page.get("cursor_in") != cursor
            ):
                raise DataError("HISTORY_PAGE_SEQUENCE_MISMATCH")
            if page.get("url") != _request_url(
                environment, resource, window_start, window_end, cursor
            ):
                raise DataError("HISTORY_SOURCE_MISMATCH")
            rows, following = _records(_page_body(page), resource, ticker)
            empty_pages += not rows
            for row in rows:
                if not window_start <= row["timestamp_ms"] <= window_end:
                    excluded += 1
                    continue
                previous = unique.get(row["id"])
                if previous is not None:
                    if previous != row:
                        raise DataError("CONFLICTING_HISTORY_RECORD")
                    duplicates += 1
                unique[row["id"]] = row
            if following:
                if following in seen_cursors or following == cursor:
                    raise DataError("REPEATED_HISTORY_CURSOR")
                if not rows:
                    raise DataError("EMPTY_HISTORY_PAGE_WITH_CURSOR")
                seen_cursors.add(following)
                cursor = following
            else:
                window_index += 1
                cursor = ""
        records = sorted(unique.values(), key=lambda r: (r["timestamp_ms"], r["id"]))
        report.update(
            valid_evidence=True,
            records=records,
            resource=resource,
            ticker=ticker,
            requested_range_ms={"start": start, "end": end},
            observed_range_ms=None
            if not records
            else {
                "start": records[0]["timestamp_ms"],
                "end": records[-1]["timestamp_ms"],
            },
            record_count=len(records),
            identical_duplicates=duplicates,
            excluded_outside_window=excluded,
            empty_pages=empty_pages,
            completed_windows=window_index,
            requested_windows=len(windows),
            pagination_exhausted=window_index == len(windows),
            request_count=dataset["requests_made"],
        )
        stop = dataset.get("capture_stop_reason")
        if stop is not None:
            report["errors"].append(
                stop
                if isinstance(stop, str) and re.fullmatch(r"[A-Z_0-9]{1,80}", stop)
                else "UNVERIFIED_CAPTURE_FAILURE"
            )
        if window_index != len(windows):
            report["errors"].append("HISTORY_REQUEST_NOT_EXHAUSTED")
        report["retrieval_complete"] = (
            window_index == len(windows) and not report["errors"]
        )
        report["coverage_state"] = "EMPTY" if not records else "OBSERVED_RECORDS_ONLY"
        if not report["retrieval_complete"]:
            report["coverage_state"] = "INCOMPLETE"
        report["coverage_note"] = (
            "HTTP success and exhausted pagination do not prove retention, complete market history, or an independent sample."
        )
        if resource == "candles":
            report["cadence"] = _candle_gaps(records, start, end)
            report["candles_without_trade_prices"] = sum(
                r["price"]["close"] is None for r in records
            )
        else:
            report["cadence"] = {
                "expected_cadence_verified": False,
                "gap_count": None,
                "interpretation": "No documented guaranteed row cadence; gaps cannot be inferred from silence.",
            }
    except (DataError, KeyError, TypeError, IndexError, ValueError) as exc:
        report["valid_evidence"] = False
        report["retrieval_complete"] = False
        report["coverage_state"] = "INCOMPLETE"
        report["errors"].append(
            str(exc) if isinstance(exc, DataError) else "MALFORMED_HISTORY_DATASET"
        )
    return report


def load_history(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DataError("DUPLICATE_HISTORY_JSON_KEY")
            result[key] = value
        return result

    def reject_constant(value):
        raise DataError("NONFINITE_HISTORY_JSON")

    with Path(path).open("rb") as source:
        raw = source.read(MAX_DATASET_BYTES + 1)
    if len(raw) > MAX_DATASET_BYTES:
        raise DataError("HISTORY_DATASET_TOO_LARGE")
    try:
        return json.loads(
            raw,
            object_pairs_hook=unique,
            parse_float=Decimal,
            parse_constant=reject_constant,
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise DataError("INVALID_HISTORY_JSON") from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture-history")
    capture.add_argument("--environment", choices=tuple(HOSTS), default="public")
    capture.add_argument("--resource", choices=RESOURCES, required=True)
    capture.add_argument("--start-ms", type=int, required=True)
    capture.add_argument("--end-ms", type=int, required=True)
    capture.add_argument("--max-pages", type=int, default=3)
    capture.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify", aliases=["analyze"])
    verify.add_argument("--input", type=Path, required=True)
    verify.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.output.exists() or args.output.is_symlink():
            raise DataError("OUTPUT_ALREADY_EXISTS")
        if args.command == "capture-history":
            result = PublicHistoryReader(args.environment, args.max_pages).capture(
                args.resource, args.start_ms, args.end_ms
            )
            analysis = result["analysis"]
        else:
            result = analysis = analyze_history(load_history(args.input))
        save_new(args.output, result)
        print(
            json.dumps(
                {
                    "saved": str(args.output),
                    "retrieval_complete": analysis["retrieval_complete"],
                    "history_sufficient": False,
                    "execution_available": False,
                }
            )
        )
        return 0 if analysis["valid_evidence"] and analysis["retrieval_complete"] else 2
    except (DataError, OSError, TypeError) as exc:
        print(
            json.dumps(
                {
                    "error": str(exc)
                    if isinstance(exc, DataError)
                    else "HISTORY_LOCAL_IO_ERROR"
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
