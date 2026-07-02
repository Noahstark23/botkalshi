#!/usr/bin/env python3
"""
Smoke test del Motor 5 (plan §5.1, precondición 1 de F3 — EL ORDEN IMPORTA: esto corre
ANTES de girar la llave MOTOR_MM_F3_ACK).

Qué hace (una sola cosa, mínima y reversible):
  1. Coloca UNA quote unilateral post-only de 1 CONTRATO en el ticker que le pases,
     a un precio LEJOS del mercado (para que jamás llene mientras vive).
  2. Espera N segundos (default 10).
  3. La CANCELA y verifica el cancel con get_orders.
  4. Imprime TODAS las respuestas crudas (la evidencia del gate: valida auth, mapeo V2,
     semántica de post_only y cancel en el ambiente real).

Uso (dentro del container, con las llaves del ambiente que toque):
  # Ensayo en demo primero (recomendado):
  KALSHI_ENV=demo python scripts/motor5_smoke_test.py --ticker KXMLBGAME-...-XXX \\
      --price-cents 2 --confirm

  # El smoke real contra producción (requiere TRADING_ENABLED=true en el env):
  python scripts/motor5_smoke_test.py --ticker <mercado de BAJO volumen> \\
      --price-cents 2 --confirm

Guardarraíles:
  - Sin --confirm imprime lo que HARÍA y sale (dry-run por default).
  - count está FIJO en 1 (no es parámetro a propósito).
  - El precio default (2¢) es un bid casi imposible de llenar; aún si llenara, el
    riesgo máximo es 2¢. --price-cents acepta [1,10] — un smoke no cotiza cerca del mid.
  - Capa C del cliente aplica igual: con TRADING_ENABLED=false el buy se bloquea.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

from src.clients.kalshi_rest import KalshiRestClient
from src.utils.config import get_settings


async def run_smoke(ticker: str, price_cents: int, hold_sec: float) -> int:
    settings = get_settings()
    coid = f"m5mm-smoke-{uuid.uuid4()}"
    print(f"ambiente: {settings.KALSHI_ENV}  trading_enabled: {settings.TRADING_ENABLED}")
    print(f"smoke: bid post_only 1 contrato {ticker} @ {price_cents}c coid={coid}")
    async with KalshiRestClient() as client:
        resp = await client.place_order(
            ticker=ticker,
            side="yes",
            action="buy",
            count=1,
            yes_price=price_cents,
            client_order_id=coid,
            time_in_force="gtc",
            post_only=True,
        )
        print("== place_order response (crudo) ==")
        print(json.dumps(resp, indent=2, default=str))
        order = resp.get("order", resp) if isinstance(resp, dict) else {}
        order_id = order.get("order_id") or resp.get("order_id")
        if not order_id:
            print("FALLO: sin order_id en la respuesta — capturar y revisar", file=sys.stderr)
            return 2
        print(f"orden viva {order_id}; esperando {hold_sec}s…")
        await asyncio.sleep(hold_sec)
        cancel_resp = await client.cancel_order(order_id)
        print("== cancel_order response (crudo) ==")
        print(json.dumps(cancel_resp, indent=2, default=str))
        # Verificación independiente: la orden NO debe seguir resting.
        orders = await client.get_orders(ticker=ticker, limit=100)
        mine = [o for o in orders.get("orders", []) if o.get("client_order_id") == coid]
        print("== get_orders (verificación) ==")
        print(json.dumps(mine, indent=2, default=str))
        still_resting = any(str(o.get("status", "")).lower() == "resting" for o in mine)
        if still_resting:
            print("FALLO: la orden sigue RESTING tras el cancel", file=sys.stderr)
            return 3
    print("SMOKE OK: place → resting → cancel verificado. Evidencia arriba (guardarla).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--ticker", required=True, help="market de BAJO volumen para el smoke")
    ap.add_argument(
        "--price-cents",
        type=int,
        default=2,
        help="bid del smoke, lejos del mercado (default 2c; máx 10c)",
    )
    ap.add_argument("--hold-sec", type=float, default=10.0, help="segundos antes del cancel")
    ap.add_argument(
        "--confirm",
        action="store_true",
        help="sin esto es DRY-RUN: imprime el plan y sale sin tocar la API",
    )
    args = ap.parse_args()
    if not (1 <= args.price_cents <= 10):
        print(
            "--price-cents debe estar en [1,10]: un smoke no cotiza cerca del mid", file=sys.stderr
        )
        return 2
    if not args.confirm:
        print(
            f"DRY-RUN (falta --confirm): colocaría bid post_only 1x{args.ticker} "
            f"@{args.price_cents}c, esperaría {args.hold_sec}s y cancelaría."
        )
        return 0
    return asyncio.run(run_smoke(args.ticker, args.price_cents, args.hold_sec))


if __name__ == "__main__":
    raise SystemExit(main())
