"""
Inspect raw WebSocket messages from Kalshi.

Standalone diagnostic tool. NO importa del bot productivo para evitar
acoplamiento. Usa el mismo signing pattern (RSA-PSS) inline.

Uso tipico (dentro del container o local con .env):
    python scripts/inspect_ws.py
    python scripts/inspect_ws.py --tickers KXMLB-26-LAD,KXMLB-26-NYY --verbose
    python scripts/inspect_ws.py --channels orderbook_delta --max-messages 50 --duration 30
    python scripts/inspect_ws.py --channels ticker,trade

CANALES VÁLIDOS de Kalshi WS v2: orderbook_delta, ticker, trade, fill, market_lifecycle.
NOTA: "orderbook_snapshot" NO es un canal — es un TIPO DE MENSAJE que Kalshi envía
al suscribirse a `orderbook_delta` (el snapshot inicial). Suscribirse a él devuelve
code 8 "Unknown channel name". El script valida esto y rechaza canales desconocidos.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlencode

import httpx
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # Container usa env vars directamente, no .env

# Series que refleja TARGET_SERIES_PREFIXES del bot productivo
_DEFAULT_SERIES = ["KXMLB", "KXNBA"]


# =====================================================
# Signing (replicado de src/auth/signer.py sin importarlo)
# =====================================================

def _load_private_key(path: str):
    return serialization.load_pem_private_key(Path(path).read_bytes(), password=None)


def _sign(private_key, method: str, path: str, api_key_id: str) -> dict[str, str]:
    timestamp_ms = str(int(time.time() * 1000))
    msg = f"{timestamp_ms}{method.upper()}{path}".encode()
    sig = private_key.sign(
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("ascii"),
    }


# =====================================================
# Discovery via REST
# =====================================================

async def discover_tickers(
    private_key,
    api_key_id: str,
    rest_base: str,
    series_prefixes: list[str] | None = None,
    max_tickers: int = 50,
) -> list[str]:
    """Descubre tickers activos replicando la logica de data_capture._discover_markets."""
    if series_prefixes is None:
        series_prefixes = _DEFAULT_SERIES

    tickers: list[str] = []

    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
        for prefix in series_prefixes:
            print(f"  [discovery] GET /events?series_ticker={prefix}")
            params = {"series_ticker": prefix, "limit": 20, "status": "open"}
            qs = urlencode(sorted(params.items()))
            sign_path = f"/trade-api/v2/events?{qs}"
            headers = _sign(private_key, "GET", sign_path, api_key_id)

            try:
                resp = await client.get(f"{rest_base}/events", params=params, headers=headers)
                resp.raise_for_status()
                events = resp.json().get("events", [])
            except Exception as e:
                print(f"  [discovery] ❌ {prefix}: {type(e).__name__}: {e}")
                continue

            for event in events[:5]:  # max 5 eventos por serie para no generar burst
                event_ticker = event.get("event_ticker")
                if not event_ticker:
                    continue

                event_path = f"/trade-api/v2/events/{event_ticker}"
                event_headers = _sign(private_key, "GET", event_path, api_key_id)

                try:
                    await asyncio.sleep(1.0)  # pausa anti-429
                    evt_resp = await client.get(
                        f"{rest_base}/events/{event_ticker}", headers=event_headers
                    )
                    if evt_resp.status_code != 200:
                        continue
                    markets = evt_resp.json().get("markets", [])
                    for m in markets:
                        if m.get("status") in ("open", "active") and m.get("ticker"):
                            tickers.append(m["ticker"])
                            if len(tickers) >= max_tickers:
                                return tickers
                except Exception as e:
                    print(f"  [discovery] ⚠️ {event_ticker}: {e}")

            await asyncio.sleep(1.0)  # pausa entre prefixes

    return tickers


# =====================================================
# WS Inspector
# =====================================================

async def inspect_ws(
    ws_url: str,
    ws_path: str,
    api_key_id: str,
    private_key,
    tickers: list[str],
    channels: list[str],
    duration: int,
    max_messages: int,
    verbose: bool,
) -> None:
    headers = _sign(private_key, "GET", ws_path, api_key_id)

    print(f"[connect] Connecting to {ws_url}")
    print(f"[connect]   tickers : {len(tickers)}")
    print(f"[connect]   channels: {channels}")
    print()

    msg_types_seen: Counter[str] = Counter()
    msg_counter = 0
    start = time.time()

    try:
        async with websockets.connect(
            ws_url,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
            max_size=2**22,
        ) as ws:
            print("[connect] Connected. Sending subscription...")

            sub_cmd = {
                "id": 1,
                "cmd": "subscribe",
                "params": {
                    "channels": channels,
                    "market_tickers": tickers,
                },
            }
            await ws.send(json.dumps(sub_cmd))
            sample = tickers[:5]
            print(
                f"[subscribe] channels={channels} markets={len(tickers)}"
                f" sample={sample}{'...' if len(tickers) > 5 else ''}"
            )
            print()

            deadline = start + duration

            while msg_counter < max_messages and time.time() < deadline:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 30.0))
                except TimeoutError:
                    break

                msg_counter += 1
                raw_str = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw

                try:
                    msg = json.loads(raw_str)
                except json.JSONDecodeError:
                    print(f"[recv {msg_counter:4d}] ❌ Invalid JSON: {raw_str[:200]}")
                    continue

                msg_type = msg.get("type", "<no_type>")
                msg_types_seen[msg_type] += 1

                if msg_type == "error":
                    print(f"[recv {msg_counter:4d}] ❌ ERROR from Kalshi: {json.dumps(msg)}")
                elif verbose:
                    print(f"[recv {msg_counter:4d}] type={msg_type} body={json.dumps(msg)[:500]}")
                else:
                    msg_body = msg.get("msg", {})
                    ticker = msg_body.get("market_ticker", "")
                    extra = ""
                    if msg_type == "orderbook_delta":
                        extra = (
                            f" price={msg_body.get('price')}"
                            f" side={msg_body.get('side')}"
                            f" delta={msg_body.get('delta')}"
                            f" seq={msg_body.get('seq', '?')}"
                        )
                    elif msg_type == "orderbook_snapshot":
                        yes_levels = len(msg_body.get("yes", []))
                        no_levels = len(msg_body.get("no", []))
                        extra = f" yes_levels={yes_levels} no_levels={no_levels} seq={msg_body.get('seq', '?')}"
                    elif msg_type == "ticker":
                        extra = f" price={msg_body.get('price')}"
                    elif msg_type == "subscribed":
                        ticker = ""
                        subs = msg.get("msg", {})
                        extra = f" {json.dumps(subs)[:120]}"
                    print(f"[recv {msg_counter:4d}] type={msg_type} ticker={ticker}{extra}")

    except Exception as e:
        print(f"\n[error] {type(e).__name__}: {e}")
        return

    duration_actual = time.time() - start

    print()
    print(f"[stats] Listened {duration_actual:.1f}s, received {msg_counter} messages")
    print()
    print("=" * 60)
    print("SUMMARY — Message types observed:")
    if msg_types_seen:
        for mtype, count in msg_types_seen.most_common():
            bar = "  ← !!" if mtype == "error" else ""
            print(f"  {mtype:30s} {count:4d}{bar}")
    else:
        print("  (none)")
    print()

    print("=" * 60)
    print("DIAGNOSTIC:")
    delta_count = msg_types_seen.get("orderbook_delta", 0)
    snapshot_count = msg_types_seen.get("orderbook_snapshot", 0)
    error_count = msg_types_seen.get("error", 0)
    subscribed_count = msg_types_seen.get("subscribed", 0)

    if error_count > 0:
        print(f"  ❌ Kalshi returned {error_count} error message(s). Subscription likely rejected.")
        print("     Action: check channel names, auth credentials, and account permissions.")
    elif delta_count > 0:
        print(f"  ✅ orderbook_delta received: {delta_count} messages")
        print("     The subscription IS working. Bug is in _on_orderbook_delta handler or DB write.")
        print("     Check: msg.get('msg') structure, OrderbookEvent fields, get_session() commits.")
    elif snapshot_count > 0 and delta_count == 0:
        print(f"  ⚠️  orderbook_snapshot messages received ({snapshot_count}), but ZERO orderbook_delta.")
        print("     (orderbook_snapshot es el snapshot inicial que llega al suscribir a")
        print("      orderbook_delta — recibirlo confirma que la suscripción al canal funciona.)")
        print("     Possible causes:")
        print("       - Markets are open but truly inactive (no trades = no deltas)")
        print("     Try: --duration 300 to wait longer for activity")
    elif subscribed_count > 0 and msg_counter <= subscribed_count:
        print(f"  ⚠️  Subscription confirmed but NO market data after {duration_actual:.0f}s.")
        print("     Markets may be truly inactive right now.")
        print("     Try: --duration 300 or run during active trading hours (US daytime).")
    elif msg_counter == 0:
        print("  ❌ Zero messages received. Possible causes:")
        print("     - Subscription silently rejected (no 'subscribed' confirm received)")
        print("     - All channel names unrecognized by Kalshi")
        print("     - Auth failure (check key/signature)")
        print("     Try: --verbose to see raw messages, --channels ticker (broad channel)")
    else:
        print(f"  ⚠️  {msg_counter} messages received but no orderbook traffic.")
        print(f"     Channels tried: {channels}")
        print("     Try: --channels orderbook_delta,ticker,trade --duration 300")


# =====================================================
# Main
# =====================================================

async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect raw Kalshi WS messages — standalone diagnostic tool"
    )
    parser.add_argument(
        "--tickers",
        default=None,
        help="Comma-separated tickers. Default: auto-discover via REST (mirrors production).",
    )
    parser.add_argument(
        "--channels",
        default="orderbook_delta,ticker,trade",
        help="Comma-separated channels. Default: orderbook_delta,ticker,trade",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=60,
        help="How long to listen in seconds. Default: 60",
    )
    parser.add_argument(
        "--max-messages",
        type=int,
        default=200,
        help="Stop after receiving this many messages. Default: 200",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print full message bodies (default: compact one-liners)",
    )
    args = parser.parse_args()

    api_key_id = os.getenv("KALSHI_API_KEY_ID")
    private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    ws_url = os.getenv(
        "KALSHI_WS_URL", "wss://api.elections.kalshi.com/trade-api/ws/v2"
    )
    rest_base = os.getenv(
        "KALSHI_API_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2"
    )
    ws_path = "/trade-api/ws/v2"

    if not api_key_id or not private_key_path:
        print("ERROR: missing KALSHI_API_KEY_ID or KALSHI_PRIVATE_KEY_PATH env vars")
        return 1

    if not Path(private_key_path).exists():  # noqa: ASYNC240 — script de diagnóstico, check puntual al inicio
        print(f"ERROR: private key not found: {private_key_path}")
        return 1

    print("=== Kalshi WS Inspector ===")
    print(f"WS host  : {ws_url}")
    print(f"REST host: {rest_base}")
    print(f"API key  : {api_key_id[:12]}...")
    print(f"Channels : {args.channels}")
    print(f"Duration : {args.duration}s | Max msgs: {args.max_messages}")
    print()

    try:
        private_key = _load_private_key(private_key_path)
    except Exception as e:
        print(f"ERROR loading private key: {e}")
        return 1

    channels = [c.strip() for c in args.channels.split(",") if c.strip()]

    # Validar canales contra los nombres reales de Kalshi WS v2 ANTES de suscribir,
    # para no generar code 8 "Unknown channel name" (falso positivo del diagnóstico).
    # "orderbook_snapshot" es un TIPO DE MENSAJE, NO un canal → se rechaza explícito.
    valid_channels = {"orderbook_delta", "ticker", "trade", "fill", "market_lifecycle"}
    invalid = [c for c in channels if c not in valid_channels]
    if invalid:
        print(f"ERROR: canal(es) inválido(s): {invalid}")
        if "orderbook_snapshot" in invalid:
            print("  'orderbook_snapshot' NO es un canal — es el snapshot inicial que llega")
            print("  al suscribir a 'orderbook_delta'. Usá --channels orderbook_delta.")
        print(f"  Canales válidos: {sorted(valid_channels)}")
        return 1

    if args.tickers:
        tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
        print(f"Using provided tickers: {len(tickers)}")
    else:
        print("Discovering tickers via REST (mirrors production discovery)...")
        try:
            tickers = await discover_tickers(private_key, api_key_id, rest_base)
            print(f"Discovered: {len(tickers)} tickers")
            if tickers:
                print(f"Sample    : {tickers[:5]}")
        except Exception as e:
            print(f"ERROR discovering tickers: {type(e).__name__}: {e}")
            return 1

    if not tickers:
        print("ERROR: no tickers found. Pass --tickers manually or check REST connectivity.")
        return 1

    print()
    await inspect_ws(
        ws_url=ws_url,
        ws_path=ws_path,
        api_key_id=api_key_id,
        private_key=private_key,
        tickers=tickers,
        channels=channels,
        duration=args.duration,
        max_messages=args.max_messages,
        verbose=args.verbose,
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
