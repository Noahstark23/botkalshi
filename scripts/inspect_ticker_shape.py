#!/usr/bin/env python3
"""
Auditoría de shape del canal `ticker` en PRODUCCIÓN — antes de encender el shadow.

Propósito: certeza ABSOLUTA de cómo se ven los campos del `ticker` en vivo, en
especial los NOMBRES de los campos de size, de los que depende el filtro de
profundidad del Motor REST. Si los nombres reales no matchean los candidatos del
trigger (`yes_bid_size_fp` / `yes_bid_size` / `yes_bid_qty`), el size lee None →
profundidad insuficiente → el shadow NUNCA dispara y graba 0 EdgeWindows EN
SILENCIO. Este script lo detecta antes de quedar ciego.

Qué hace:
  - se conecta al WS de Kalshi, se suscribe al canal `ticker` de los tickers dados,
  - captura N payloads CRUDOS COMPLETOS,
  - los imprime en consola y los guarda a un .json,
  - reporta las keys observadas y un chequeo explícito de los candidatos de size,
  - se desconecta.

READ-ONLY: solo se suscribe y observa. No pide orderbooks, no coloca órdenes, no
toca V2, no escribe en la DB. No activa MOTOR_REST_ENABLED.

Uso (lo corre el operador, requiere credenciales Kalshi y mercado activo):
    PYTHONPATH=. poetry run python scripts/inspect_ticker_shape.py \\
        --tickers KXNBA-26-XXX KXNBA-26-YYY \\
        --max-messages 10 --out ticker_shapes.json
"""
from __future__ import annotations

import argparse
import asyncio
import json

from src.clients.kalshi_ws import KalshiWebSocket

# Mismos candidatos que usa el trigger (src/strategies/motor_rest_arb/trigger.py).
_SIZE_KEY_CANDIDATES = (
    "yes_bid_size_fp", "yes_bid_size", "yes_bid_qty",
    "yes_ask_size_fp", "yes_ask_size", "yes_ask_qty",
)
_PRICE_KEY_CANDIDATES = ("yes_bid_dollars", "yes_ask_dollars")


def _dump_json(path: str, data: list[dict]) -> None:
    """Escritura síncrona a archivo (fuera del path async)."""
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


async def _main() -> None:
    ap = argparse.ArgumentParser(description="Inspecciona el shape del ticker WS (read-only).")
    ap.add_argument("--tickers", nargs="+", required=True, help="Tickers de un mercado ACTIVO")
    ap.add_argument("--max-messages", type=int, default=10, help="Payloads a capturar (default 10)")
    ap.add_argument("--out", default="ticker_shapes.json", help="Archivo JSON de salida")
    ap.add_argument("--timeout", type=float, default=300.0, help="Timeout total en seg (default 300)")
    args = ap.parse_args()

    ws = KalshiWebSocket()
    captured: list[dict] = []
    done = asyncio.Event()

    async def _on_ticker(msg: dict) -> None:
        captured.append(msg)
        n = len(captured)
        print(f"\n===== ticker #{n} (CRUDO COMPLETO) =====")
        print(json.dumps(msg, indent=2, default=str))
        if n >= args.max_messages:
            done.set()

    ws.on("ticker", _on_ticker)
    ws.queue_subscription(channels=["ticker"], market_tickers=args.tickers)

    print(f"Inspeccionando {args.max_messages} payloads `ticker` de: {', '.join(args.tickers)}")
    print("READ-ONLY — solo observa. No opera, no pide orderbook. Ctrl-C para cortar.\n")

    run_task = asyncio.create_task(ws.run())
    try:
        await asyncio.wait_for(done.wait(), timeout=args.timeout)
    except TimeoutError:
        print(f"\n⚠️  Timeout {args.timeout:.0f}s: solo {len(captured)} payloads capturados.")
    finally:
        await ws.stop()
        run_task.cancel()

    # Guardar a JSON (la captura ya terminó; escritura puntual).
    _dump_json(args.out, captured)
    print(f"\n💾 {len(captured)} payloads guardados en {args.out}")

    # Reporte de shape: keys observadas y chequeo de los candidatos de size.
    print("\n" + "=" * 64)
    print("REPORTE DE SHAPE")
    print("=" * 64)
    if not captured:
        print("Sin payloads — no se pudo auditar. Verificar mercado activo / credenciales.")
        print("=" * 64)
        return

    inner = captured[0].get("msg", captured[0])
    keys = sorted(inner.keys()) if isinstance(inner, dict) else []
    print(f"keys del payload[0].msg: {keys}")

    print("\nPrecios (el trigger necesita estos):")
    for k in _PRICE_KEY_CANDIDATES:
        print(f"  {k:24s} → {'PRESENTE' if k in keys else 'AUSENTE'}")

    print("\nSize (CRÍTICO — de esto depende el filtro de profundidad del shadow):")
    found_size = [k for k in _SIZE_KEY_CANDIDATES if k in keys]
    for k in _SIZE_KEY_CANDIDATES:
        print(f"  {k:24s} → {'PRESENTE' if k in keys else 'ausente'}")

    print("\nVEREDICTO:")
    if found_size:
        print(f"  ✓ El trigger PUEDE leer size con: {found_size}")
        print("    → el filtro de profundidad funcionará en shadow.")
    else:
        other = [k for k in keys if "size" in k.lower() or "qty" in k.lower() or "depth" in k.lower()]
        print("  ✗ NINGÚN candidato de size del trigger aparece en el payload.")
        print(f"    Campos de size/qty observados que NO matchean: {other or '(ninguno)'}")
        print("    → AGREGAR esos nombres a _YES_BID/ASK_SIZE_KEYS en trigger.py ANTES")
        print("       de encender el shadow, o el filtro de profundidad será DECORATIVO")
        print("       y el shadow grabará 0 EdgeWindows en silencio.")
    print("=" * 64 + "\n")


if __name__ == "__main__":
    asyncio.run(_main())
