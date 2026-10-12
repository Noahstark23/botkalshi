"""Fixed loopback GETs and an allowlisted, ephemeral runtime status snapshot.

No account, balance, credentials, arbitrary URL, redirects, or control commands.
The reader is opt-in and has no network effects until explicitly called.
"""

from __future__ import annotations

import copy
import http.client
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from runtime_telemetry_contract import parse_runtime_telemetry

SCHEMA = "bot-runtime-readonly-v1"
MAX_BYTES = 65536
TIMEOUT_SECONDS = 2
MAX_AGE_MS = 30000
MAX_FUTURE_MS = 5000
ACTIVITY_TTL_SECONDS = 300
ACTIVITY_SCHEMA = "motor-recorded-activity-v1"
ACTIVITY_KINDS = {
    "m6": ("linemove", "desconocido"),
    "rest": ("multi_outcome", "pct"),
    "m8": ("ofi", "zscore"),
    "m9": ("spillover", "cents"),
}
MAX_ACTIVITY_COUNT = 1_000_000_000
MOTOR_KEYS = ("m1", "m2", "m3", "m5", "m6", "rest", "m8", "m9")
FLAG_KEYS = (
    "trading_enabled",
    "bot_paused",
    "capital_paused",
    "capture_running",
    "ws_connected",
)
MOTOR_FIELDS = ("configured", "execution_configured", "running", "runtime_enabled")
_REASONS = {"FRESH", "NOT_CONNECTED", "UNAVAILABLE", "STALE", "INVALID"}


def _time(value):
    return type(value) is int and 946684800000 <= value <= 4102444800000


def _object(value):
    return value if type(value) is dict else {}


def _bool(value):
    return value if type(value) is bool else None


def unknown_activity(reason="UNAVAILABLE"):
    return {
        "schema": ACTIVITY_SCHEMA,
        "available": False,
        "reason": reason
        if type(reason) is str and reason in _REASONS
        else "UNAVAILABLE",
        "generated_at_ms": None,
        "cutoff_utc": None,
        "age_ms": None,
        "counts": {key: None for key in ACTIVITY_KINDS},
    }


def sanitize_activity(value, *, now_ms):
    """Validate recorded-signal counts; zero never means an engine is disabled."""
    if type(value) is not dict or value.get("schema") != ACTIVITY_SCHEMA:
        return unknown_activity("INVALID")
    if value.get("available") is not True:
        return unknown_activity(value.get("reason"))
    generated = value.get("generated_at_ms")
    if not _time(now_ms) or not _time(generated):
        return unknown_activity("INVALID")
    age = now_ms - generated
    if age > ACTIVITY_TTL_SECONDS * 1000:
        return unknown_activity("STALE")
    if age < -MAX_FUTURE_MS:
        return unknown_activity("INVALID")
    cutoff = value.get("cutoff_utc")
    expected = (
        datetime.fromtimestamp(generated / 1000, timezone.utc) - timedelta(days=1)
    ).strftime("%Y-%m-%d")
    if (
        type(cutoff) is not str
        or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cutoff)
        or cutoff != expected
    ):
        return unknown_activity("INVALID")
    counts = value.get("counts")
    if type(counts) is not dict or not set(ACTIVITY_KINDS) <= set(counts):
        return unknown_activity("INVALID")
    if any(
        type(counts[key]) is not int or not 0 <= counts[key] <= MAX_ACTIVITY_COUNT
        for key in ACTIVITY_KINDS
    ):
        return unknown_activity("INVALID")
    return {
        "schema": ACTIVITY_SCHEMA,
        "available": True,
        "reason": "FRESH",
        "generated_at_ms": generated,
        "cutoff_utc": cutoff,
        "age_ms": max(0, age),
        "counts": {key: counts[key] for key in ACTIVITY_KINDS},
    }


def activity_from_response(value, *, now_ms):
    """Only fixed /stats/edges?days=1 kinds; no values, PnL or raw server text."""
    if (
        type(value) is not dict
        or type(value.get("days_requested")) is not int
        or value["days_requested"] != 1
    ):
        return unknown_activity("INVALID")
    kinds = value.get("by_kind")
    if type(kinds) is not dict:
        return unknown_activity("INVALID")
    counts = {}
    for key, (kind, unit) in ACTIVITY_KINDS.items():
        if kind not in kinds:
            # Complete successful grouped query with no kind means zero rows in
            # this window. It provides no evidence of running or configuration.
            counts[key] = 0
            continue
        block = kinds[kind]
        if type(block) is not dict or block.get("unit") != unit:
            return unknown_activity("INVALID")
        counts[key] = block.get("rows_total")
    return sanitize_activity(
        {
            "schema": ACTIVITY_SCHEMA,
            "available": True,
            "generated_at_ms": _iso_ms(value.get("generated_at")),
            "cutoff_utc": value.get("cutoff"),
            "counts": counts,
        },
        now_ms=now_ms,
    )


def unknown_status(now_ms, reason="UNAVAILABLE"):
    """Never retain last-known true flags when a new observation is unavailable."""
    return {
        "schema": SCHEMA,
        "source": "droplet_runtime",
        "checked_at_ms": now_ms if _time(now_ms) else None,
        "source_at_ms": None,
        "age_ms": None,
        "fresh": False,
        "reason": reason
        if type(reason) is str and reason in _REASONS
        else "UNAVAILABLE",
        "environment": None,
        "flags": {key: None for key in FLAG_KEYS},
        "motors": {key: {field: None for field in MOTOR_FIELDS} for key in MOTOR_KEYS},
        "activity": unknown_activity(reason),
        "telemetry": None,
        "telemetry_reason": "UNAVAILABLE",
        "real_controls_available": False,
    }


def _iso_ms(value):
    if type(value) is not str or not 20 <= len(value) <= 40:
        return None
    try:
        moment = datetime.fromisoformat(
            value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else "")
        )
        if moment.tzinfo is None:
            return None
        result = int(moment.timestamp() * 1000)
        return result if _time(result) else None
    except (ValueError, OverflowError, OSError):
        return None


def sanitize_snapshot(value, *, now_ms):
    """Revalidate injected snapshots; only fixed labels, booleans and times escape."""
    if type(value) is not dict or value.get("schema") != SCHEMA or not _time(now_ms):
        return unknown_status(now_ms, "INVALID")
    if value.get("fresh") is not True:
        reason = value.get("reason")
        return unknown_status(now_ms, reason if type(reason) is str else "UNAVAILABLE")
    source_at = value.get("source_at_ms")
    if not _time(source_at):
        return unknown_status(now_ms, "INVALID")
    delta = now_ms - source_at
    if delta > MAX_AGE_MS:
        return unknown_status(now_ms, "STALE")
    if delta < -MAX_FUTURE_MS:
        return unknown_status(now_ms, "INVALID")
    result = unknown_status(now_ms)
    result.update(
        fresh=True, reason="FRESH", source_at_ms=source_at, age_ms=max(0, delta)
    )
    environment = value.get("environment")
    result["environment"] = (
        environment if environment in ("production", "demo") else None
    )
    flags = _object(value.get("flags"))
    result["flags"] = {key: _bool(flags.get(key)) for key in FLAG_KEYS}
    motors = _object(value.get("motors"))
    for key in MOTOR_KEYS:
        raw = _object(motors.get(key))
        result["motors"][key] = {field: _bool(raw.get(field)) for field in MOTOR_FIELDS}
    # The current runtime exposes a task-running signal only for M5. It exposes
    # no specific signal for M6/M8/M9/REST, and no M1 execution flag.
    for key in MOTOR_KEYS:
        if key != "m5":
            result["motors"][key]["running"] = None
            result["motors"][key]["runtime_enabled"] = None
        if key in ("m6", "m8", "m9", "rest"):
            result["motors"][key] = {field: None for field in MOTOR_FIELDS}
    result["motors"]["m1"]["execution_configured"] = None
    result["activity"] = sanitize_activity(value.get("activity"), now_ms=now_ms)
    telemetry = value.get("telemetry")
    parsed = parse_runtime_telemetry(telemetry, now_ms=now_ms)
    result["telemetry_reason"] = parsed["reason"]
    if parsed["available"]:
        # Strict parser has validated every key and primitive. Retain that wire
        # representation so a later card can recheck freshness at its own time.
        result["telemetry"] = copy.deepcopy(telemetry)
    elif telemetry is None and value.get("telemetry_reason") in ("INVALID", "STALE"):
        result["telemetry_reason"] = value["telemetry_reason"]
    return result


def status_from_responses(health, status, *, now_ms):
    """Extract the current runtime schema without retaining raw server values."""
    if type(health) is not dict or type(status) is not dict:
        return unknown_status(now_ms, "INVALID")
    if health.get("status") not in ("healthy", "unhealthy"):
        return unknown_status(now_ms, "UNAVAILABLE")
    source_at = _iso_ms(health.get("timestamp"))
    if source_at is None:
        return unknown_status(now_ms, "INVALID")
    bot = _object(status.get("bot"))
    config = _object(status.get("config"))
    capital = _object(status.get("capital"))
    enabled = _object(config.get("motors_enabled"))
    m5 = _object(bot.get("motor5"))
    result = unknown_status(now_ms)
    result.update(
        fresh=True,
        reason="FRESH",
        source_at_ms=source_at,
        environment=config.get("environment"),
    )
    result["flags"] = {
        "trading_enabled": config.get("trading_enabled"),
        "bot_paused": bot.get("is_paused"),
        "capital_paused": capital.get("is_paused"),
        "capture_running": bot.get("capture_running"),
        "ws_connected": bot.get("ws_connected"),
    }
    mapping = {
        "m1": ("motor_1_arbitrage", None),
        "m2": ("motor_2_sportsbook", "motor_2_execution"),
        "m3": ("motor_3_clv", "motor_3_execution"),
        "m5": ("motor_5_mm", "motor_5_execution"),
    }
    for key, (configured, execution) in mapping.items():
        result["motors"][key]["configured"] = enabled.get(configured)
        result["motors"][key]["execution_configured"] = (
            enabled.get(execution) if execution else None
        )
    result["motors"]["m5"].update(
        running=m5.get("task_running"), runtime_enabled=m5.get("enabled")
    )
    result["telemetry"] = status.get("motor_telemetry")
    return sanitize_snapshot(result, now_ms=now_ms)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("INVALID_RUNTIME_JSON")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("INVALID_RUNTIME_JSON")


class RuntimeStatusReader:
    """Optional callable provider for PaperControl; all destinations are fixed.

    ``reader(now_ms=epoch_ms)`` returns a fresh sanitized snapshot or unknown.
    connection_factory is test injection, never a URL/configuration interface.
    """

    def __init__(self, *, connection_factory=None):
        self._factory = connection_factory or http.client.HTTPConnection
        self._activity_lock = threading.Lock()
        self._activity_last_attempt = None
        self._activity_cache = unknown_activity()

    def _get(self, path):
        if path not in ("/health", "/status", "/stats/edges?days=1"):
            raise ValueError("RUNTIME_RESOURCE_BLOCKED")
        connection = None
        deadline = time.monotonic() + TIMEOUT_SECONDS
        try:
            connection = self._factory("127.0.0.1", 18080, timeout=TIMEOUT_SECONDS)
            connection.request("GET", path, headers={"Accept": "application/json"})
            response = connection.getresponse()
            if response.status != 200 and not (
                path == "/health" and response.status == 503
            ):
                raise ValueError("RUNTIME_RESPONSE_UNAVAILABLE")
            raw = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("RUNTIME_READ_TIMEOUT")
                if getattr(connection, "sock", None) is not None:
                    connection.sock.settimeout(min(TIMEOUT_SECONDS, remaining))
                chunk = response.read1(min(16384, MAX_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > MAX_BYTES:
                    raise ValueError("RUNTIME_RESPONSE_TOO_LARGE")
            parsed = json.loads(
                raw, object_pairs_hook=_unique, parse_constant=_constant
            )
            if response.status == 503:
                if (
                    type(parsed) is not dict
                    or type(parsed.get("detail")) is not dict
                    or parsed["detail"].get("status") != "unhealthy"
                ):
                    raise ValueError("RUNTIME_RESPONSE_UNAVAILABLE")
                return parsed["detail"]
            return parsed
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def _activity(self, now_ms):
        with self._activity_lock:
            now = time.monotonic()
            if self._activity_last_attempt is not None:
                elapsed = now - self._activity_last_attempt
                if elapsed < 0:
                    return unknown_activity("INVALID")
                if elapsed < ACTIVITY_TTL_SECONDS:
                    return sanitize_activity(self._activity_cache, now_ms=now_ms)
            # Budget failures as well as successes; owner refresh spam must not
            # repeatedly run the aggregate. Never fall back to an older success.
            self._activity_last_attempt = now
            self._activity_cache = unknown_activity()
            try:
                self._activity_cache = activity_from_response(
                    self._get("/stats/edges?days=1"), now_ms=now_ms
                )
            except Exception:
                self._activity_cache = unknown_activity()
            return sanitize_activity(self._activity_cache, now_ms=now_ms)

    def __call__(self, *, now_ms):
        if not _time(now_ms):
            return unknown_status(now_ms, "INVALID")
        try:
            health = self._get("/health")
            status = self._get("/status")
            result = status_from_responses(health, status, now_ms=now_ms)
        except Exception:
            # Runtime bodies and exception details may include sensitive data.
            return unknown_status(now_ms, "UNAVAILABLE")
        if result["fresh"]:
            result["activity"] = self._activity(now_ms)
        return result
