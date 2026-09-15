"""botkalshi autonomous research collector (shadow-only).

Reads public Kalshi market data and, optionally, The Odds API. It has no authenticated
Kalshi client, no order methods, no account/balance access, and no execution path.
Outputs durable SQLite observations plus a sanitized JSON packet for external review.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import ssl
import sys
import time
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, build_opener
import uuid

KALSHI_ORIGIN = "https://api.elections.kalshi.com/trade-api/v2"
ODDS_ORIGIN = "https://api.the-odds-api.com/v4"
DATA_DIR = Path(os.environ.get("BOTKALSHI_RESEARCH_DATA", "/var/lib/botkalshi-research"))
SERIES = os.environ.get("BOTKALSHI_SERIES", "KXMLBGAME")
POLL_SECONDS = max(30, int(os.environ.get("BOTKALSHI_POLL_SECONDS", "60")))
ODDS_POLL_SECONDS = max(300, int(os.environ.get("BOTKALSHI_ODDS_POLL_SECONDS", "300")))
MAX_MARKETS = min(100, max(1, int(os.environ.get("BOTKALSHI_MAX_MARKETS", "40"))))
MAX_BODY = 4_000_000
SENSITIVE_NAMES = (
    "KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY", "KALSHI_PRIVATE_KEY_PATH",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN",
)
EXECUTION_NAMES = (
    "TRADING_ENABLED", "MOTOR_MM_EXECUTION_ENABLED", "MOTOR_1_EXECUTION_ENABLED",
    "MOTOR_2_EXECUTION_ENABLED", "MOTOR_2_ENTRY_EXECUTION_ENABLED",
    "MOTOR_3_EXECUTION_ENABLED", "MOTOR_REST_EXECUTION_ENABLED",
)

class ResearchError(RuntimeError):
    pass

def utc_now() -> str:
    return datetime.now(UTC).isoformat()

def safety_check(env: dict[str, str] | os._Environ[str] | None = None) -> None:
    env = os.environ if env is None else env
    for name in SENSITIVE_NAMES:
        if env.get(name, "").strip():
            raise ResearchError(f"{name} must not be present in shadow-only service")
    for name in EXECUTION_NAMES:
        if env.get(name, "").strip().lower() not in ("", "0", "false", "off", "no"):
            raise ResearchError(f"{name} must remain false")

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ResearchError("redirect blocked")

class Reader:
    def __init__(self):
        self.opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))

    def get_json(self, origin: str, path: str, params: dict[str, str | int] | None = None) -> dict | list:
        parsed = urlsplit(origin)
        if parsed.scheme != "https" or parsed.hostname not in {"api.elections.kalshi.com", "api.the-odds-api.com"}:
            raise ResearchError("origin not allowlisted")
        if not path.startswith("/") or ".." in path or "//" in path:
            raise ResearchError("invalid path")
        url = origin + path
        if params:
            url += "?" + urlencode(params)
        try:
            with self.opener.open(url, timeout=15) as response:
                raw = response.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise ResearchError("response too large")
            return json.loads(raw)
        except HTTPError as exc:
            raise ResearchError(f"provider HTTP {exc.code}") from None
        except (URLError, TimeoutError, OSError, ValueError, UnicodeError) as exc:
            raise ResearchError(f"provider read failed: {type(exc).__name__}") from None

def open_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DATA_DIR / "research.sqlite3", timeout=15)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")
    con.execute("PRAGMA busy_timeout=15000")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS kalshi_snapshots (
      observed_at TEXT NOT NULL,
      ticker TEXT NOT NULL,
      event_ticker TEXT,
      market_status TEXT,
      orderbook_json TEXT NOT NULL,
      PRIMARY KEY (observed_at, ticker)
    );
    CREATE TABLE IF NOT EXISTS odds_snapshots (
      observed_at TEXT NOT NULL,
      event_id TEXT NOT NULL,
      sport_key TEXT NOT NULL,
      payload_json TEXT NOT NULL,
      PRIMARY KEY (observed_at, event_id)
    );
    CREATE TABLE IF NOT EXISTS cycles (
      cycle_id TEXT PRIMARY KEY,
      observed_at TEXT NOT NULL,
      kalshi_markets INTEGER NOT NULL,
      odds_events INTEGER,
      odds_status TEXT NOT NULL,
      packet_path TEXT NOT NULL,
      error TEXT
    );
    """)
    return con

def numeric_levels(book: object, max_levels: int = 20) -> dict[str, list[list[str]]]:
    if not isinstance(book, dict):
        return {}
    out: dict[str, list[list[str]]] = {}
    for side in ("yes", "no", "yes_dollars", "no_dollars"):
        rows = book.get(side)
        if not isinstance(rows, list):
            continue
        clean: list[list[str]] = []
        for row in rows[:max_levels]:
            if not isinstance(row, list) or len(row) != 2:
                continue
            a, b = str(row[0]), str(row[1])
            if re.fullmatch(r"\d+(?:\.\d+)?", a) and re.fullmatch(r"\d+(?:\.\d+)?", b):
                clean.append([a, b])
        out[side] = clean
    return out

def collect_kalshi(reader: Reader) -> list[dict]:
    body = reader.get_json(KALSHI_ORIGIN, "/markets", {"series_ticker": SERIES, "status": "open", "limit": MAX_MARKETS})
    if not isinstance(body, dict) or not isinstance(body.get("markets"), list):
        raise ResearchError("unexpected Kalshi markets response")
    markets = body["markets"][:MAX_MARKETS]
    result: list[dict] = []
    for market in markets:
        if not isinstance(market, dict):
            continue
        ticker = market.get("ticker")
        if not isinstance(ticker, str) or not re.fullmatch(r"[A-Z0-9-]{3,128}", ticker):
            continue
        ob = reader.get_json(KALSHI_ORIGIN, f"/markets/{ticker}/orderbook", {"depth": 20})
        if not isinstance(ob, dict):
            continue
        book = ob.get("orderbook_fp", ob.get("orderbook", {}))
        result.append({
            "ticker": ticker,
            "event_ticker": market.get("event_ticker") if isinstance(market.get("event_ticker"), str) else None,
            "status": market.get("status") if isinstance(market.get("status"), str) else None,
            "close_time": market.get("close_time") if isinstance(market.get("close_time"), str) else None,
            "levels": numeric_levels(book),
        })
    return result

def sanitize_odds_event(event: object) -> dict | None:
    if not isinstance(event, dict):
        return None
    eid = event.get("id")
    if not isinstance(eid, str):
        return None
    books = []
    for book in event.get("bookmakers", []) if isinstance(event.get("bookmakers"), list) else []:
        if not isinstance(book, dict):
            continue
        markets = []
        for market in book.get("markets", []) if isinstance(book.get("markets"), list) else []:
            if not isinstance(market, dict):
                continue
            outcomes = []
            for outcome in market.get("outcomes", []) if isinstance(market.get("outcomes"), list) else []:
                if not isinstance(outcome, dict):
                    continue
                name, price = outcome.get("name"), outcome.get("price")
                if isinstance(name, str) and isinstance(price, (int, float)):
                    outcomes.append({"name": name[:100], "price": price, "point": outcome.get("point")})
            markets.append({"key": market.get("key"), "outcomes": outcomes})
        books.append({"key": book.get("key"), "last_update": book.get("last_update"), "markets": markets})
    return {
        "id": eid,
        "sport_key": event.get("sport_key"),
        "commence_time": event.get("commence_time"),
        "home_team": event.get("home_team"),
        "away_team": event.get("away_team"),
        "bookmakers": books,
    }

def collect_odds(reader: Reader, api_key: str) -> list[dict]:
    body = reader.get_json(ODDS_ORIGIN, "/sports/baseball_mlb/odds", {
        "apiKey": api_key, "regions": "us", "markets": "h2h,totals", "oddsFormat": "decimal"
    })
    if not isinstance(body, list):
        raise ResearchError("unexpected Odds API response")
    return [x for item in body if (x := sanitize_odds_event(item)) is not None]

def atomic_write(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        f.write("\n")
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)

def cycle(reader: Reader, con: sqlite3.Connection, odds_cache: tuple[float, list[dict], str]) -> tuple[float, list[dict], str]:
    safety_check()
    cycle_id, observed = uuid.uuid4().hex, utc_now()
    kalshi = collect_kalshi(reader)
    last_odds_at, odds_events, odds_status = odds_cache
    odds_key = os.environ.get("ODDS_API_KEY", "").strip()
    now = time.monotonic()
    if odds_key and now - last_odds_at >= ODDS_POLL_SECONDS:
        try:
            odds_events = collect_odds(reader, odds_key)
            odds_status = "AVAILABLE"
            last_odds_at = now
        except ResearchError as exc:
            odds_status = f"ERROR:{exc}"
            odds_events = []
            last_odds_at = now
    elif not odds_key:
        odds_status = "NOT_CONFIGURED"
        odds_events = []

    with con:
        for m in kalshi:
            con.execute("INSERT INTO kalshi_snapshots VALUES (?, ?, ?, ?, ?)", (
                observed, m["ticker"], m["event_ticker"], m["status"], json.dumps(m["levels"], separators=(",", ":"))
            ))
        for e in odds_events:
            con.execute("INSERT OR REPLACE INTO odds_snapshots VALUES (?, ?, ?, ?)", (
                observed, e["id"], str(e.get("sport_key") or ""), json.dumps(e, separators=(",", ":"))
            ))

    packet = {
        "schema_version": "botkalshi-research-packet-v1",
        "packet_id": cycle_id,
        "generated_at": observed,
        "mode": "SHADOW_READONLY",
        "execution_authorized": False,
        "kalshi": {"series": SERIES, "market_count": len(kalshi), "markets": kalshi},
        "sportsbook": {"provider": "The Odds API", "status": odds_status, "event_count": len(odds_events), "events": odds_events},
        "assessment": None,
        "note": "Observation packet only. Not a bet, order, balance, fill or validated edge."
    }
    packet_path = DATA_DIR / "packets" / "latest.json"
    atomic_write(packet_path, packet)
    with con:
        con.execute("INSERT INTO cycles VALUES (?, ?, ?, ?, ?, ?, NULL)", (
            cycle_id, observed, len(kalshi), len(odds_events) if odds_status == "AVAILABLE" else None,
            odds_status, str(packet_path)
        ))
    atomic_write(DATA_DIR / "health.json", {
        "running": True, "updated_at": utc_now(), "mode": "SHADOW_READONLY",
        "last_cycle_id": cycle_id, "kalshi_markets": len(kalshi), "odds_status": odds_status,
        "execution_authorized": False,
    })
    return last_odds_at, odds_events, odds_status

def main() -> int:
    try:
        safety_check()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        stop = False
        def halt(signum, frame):
            nonlocal stop; stop = True
        signal.signal(signal.SIGTERM, halt); signal.signal(signal.SIGINT, halt)
        reader = Reader()
        with contextlib.closing(open_db()) as con:
            odds_cache: tuple[float, list[dict], str] = (0.0, [], "NOT_CONFIGURED")
            while not stop:
                started = time.monotonic()
                try:
                    odds_cache = cycle(reader, con, odds_cache)
                except ResearchError as exc:
                    atomic_write(DATA_DIR / "health.json", {
                        "running": True, "updated_at": utc_now(), "mode": "SHADOW_READONLY",
                        "last_error": str(exc), "execution_authorized": False,
                    })
                elapsed = time.monotonic() - started
                remaining = max(1.0, POLL_SECONDS - elapsed)
                deadline = time.monotonic() + remaining
                while not stop and time.monotonic() < deadline:
                    time.sleep(min(1.0, deadline - time.monotonic()))
        atomic_write(DATA_DIR / "health.json", {
            "running": False, "updated_at": utc_now(), "mode": "SHADOW_READONLY",
            "execution_authorized": False,
        })
        return 0
    except (ResearchError, sqlite3.Error, OSError, ValueError) as exc:
        print(f"collector stopped safely: {exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
