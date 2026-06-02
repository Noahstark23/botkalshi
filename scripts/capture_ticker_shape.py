#!/usr/bin/env python3
"""
Gate 0 del Motor REST — captura el SHAPE real del mensaje `ticker` del WS de Kalshi.

PROPÓSITO: el trigger del Motor REST (spread del ticker → GET orderbook) asume que
el canal `ticker` trae BBO (yes_bid/yes_ask/no_bid/no_ask). Eso NO está verificado.
Este script se suscribe en SHADOW PURO (solo loggea, no opera, no pide orderbook,
no toca V2) y vuelca N mensajes `ticker` crudos para confirmar qué traen.

ESTO PUEDE MATAR EL DISEÑO: si el `ticker` trae solo last-price (sin BBO), el trigger
por spread no es viable y hay que repensar la detección antes de escribir el motor.

Tras correrlo: guardar 1 mensaje representativo como fixture en
tests/fixtures/ws/ticker_sample.json y diseñar la fórmula del spread sobre el shape real.

READ-ONLY: solo se suscribe y loggea. No coloca órdenes, no pide orderbooks, no
escribe DB, no toca el manager V2 ni flags.

Uso (lo corre el operador, requiere credenciales Kalshi):
    PYTHONPATH=. poetry run python scripts/capture_ticker_shape.py \\
        --tickers KXNBA-26-AAA KXNBA-26-BBB --max-messages 20
"""
from __future__ import annotations

import argparse
import asyncio
import json

from src.clients.kalshi_ws import KalshiWebSocket


async def _main() -> None:
    ap = argparse.ArgumentParser(description="Gate 0: captura shape del ticker WS (read-only).")
    ap.add_argument("--tickers", nargs="+", required=True, help="Tickers a suscribir (ej. NBA en vivo)")
    ap.add_argument("--max-messages", type=int, default=20, help="Mensajes ticker a capturar antes de salir")
    args = ap.parse_args()

    ws = KalshiWebSocket()
    captured: list[dict] = []
    done = asyncio.Event()

    async def _on_ticker(msg: dict) -> None:
        # SHADOW PURO: solo registrar el crudo. Sin trigger, sin REST, sin operar.
        captured.append(msg)
        n = len(captured)
        print(f"\n--- ticker #{n} (crudo) ---")
        print(json.dumps(msg, indent=2, default=str))
        # Reportar las keys del payload interno (lo que el trigger necesitaría).
        inner = msg.get("msg", msg)
        print(f"keys del payload: {sorted(inner.keys()) if isinstance(inner, dict) else type(inner)}")
        if n >= args.max_messages:
            done.set()

    ws.on("ticker", _on_ticker)
    ws.queue_subscription(channels=["ticker"], market_tickers=args.tickers)

    print(f"Gate 0 — capturando {args.max_messages} mensajes `ticker` de: {', '.join(args.tickers)}")
    print("SHADOW PURO: solo log, no opera, no pide orderbook. Ctrl-C para cortar.\n")

    run_task = asyncio.create_task(ws.run())
    try:
        await asyncio.wait_for(done.wait(), timeout=300)
    except TimeoutError:
        print(f"\n⚠️  Timeout 300s: solo se capturaron {len(captured)} mensajes.")
    finally:
        await ws.stop()
        run_task.cancel()

    # Resumen para decidir la viabilidad del trigger.
    print("\n" + "=" * 60)
    print(f"CAPTURADOS: {len(captured)} mensajes ticker")
    if captured:
        inner = captured[0].get("msg", captured[0])
        keys = sorted(inner.keys()) if isinstance(inner, dict) else []
        has_bbo = all(any(k in keys for k in opts) for opts in (
            ("yes_bid", "yes_bid_dollars"), ("yes_ask", "yes_ask_dollars"),
            ("no_bid", "no_bid_dollars"), ("no_ask", "no_ask_dollars"),
        ))
        print(f"keys del payload: {keys}")
        print(f"¿Trae BBO (yes/no bid/ask)? → {'SÍ — trigger por spread VIABLE' if has_bbo else 'NO — repensar detección (trigger por spread NO viable)'}")
        print("\nGuardar 1 mensaje como tests/fixtures/ws/ticker_sample.json para los tests del trigger.")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(_main())
