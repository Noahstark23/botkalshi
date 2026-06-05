#!/usr/bin/env python3
"""
Fogueo del sensor de fill — manda UNA orden fill_or_kill REAL en cuenta DEMO y
captura la respuesta cruda, para validar _create_order_filled del RestExecutor
contra la API viva (no solo la doc). Gate irreducible del checklist de activación.

QUÉ MIRAR en la respuesta: ¿los campos vienen como `fill_count`/`remaining_count`
(int) o `fill_count_fp`/`remaining_count_fp` (string fixed-point)? Eso confirma o
ajusta el parser del executor antes de que toque capital.

SEGURIDAD:
  - Solo corre si KALSHI_ENV=demo (guard explícito; aborta en producción).
  - Orden inofensiva: 1 contrato YES a 50¢, FOK (se cancela sola si no cruza).
  - READ-impact mínimo: en demo, capital de juguete.

Uso (en tu máquina, con .env de DEMO y KALSHI_ENV=demo):
    PYTHONPATH=. python scripts/test_demo_fok.py
    PYTHONPATH=. python scripts/test_demo_fok.py --ticker KXNBA-26-LAL --price 50
"""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from src.clients.kalshi_rest import KalshiRestClient
from src.utils.config import get_settings


async def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Fogueo FOK en demo (captura respuesta cruda).")
    ap.add_argument("--ticker", help="Ticker activo de DEMO (si se omite, se pide por input).")
    ap.add_argument("--price", type=int, default=50, help="yes_price en cents (default 50).")
    ap.add_argument("--count", type=int, default=1, help="Contratos (default 1).")
    return ap.parse_args()


async def run(args: argparse.Namespace, ticker: str) -> int:
    settings = get_settings()

    # GUARD DE SEGURIDAD: nunca correr contra producción.
    if settings.KALSHI_ENV != "demo":
        print(f"❌ ABORTADO: KALSHI_ENV={settings.KALSHI_ENV!r}, se requiere 'demo'.")
        print("   Poné KALSHI_ENV=demo en tu .env antes de correr este fogueo.")
        return 1
    print(f"✓ Entorno: DEMO ({settings.rest_url})")
    print(f"\nEnviando FOK: buy {args.count} YES {ticker} @ {args.price}¢ (fill_or_kill)...")

    # El cliente se usa como async context manager (inicializa el httpx client).
    async with KalshiRestClient() as client:
        try:
            response = await client.place_order(
                ticker=ticker,
                side="yes",            # requerido (keyword-only)
                action="buy",
                count=args.count,
                order_type="limit",    # el parámetro real es order_type (no 'type')
                yes_price=args.price,
                client_order_id=str(uuid.uuid4()),
                time_in_force="fill_or_kill",
            )
        except Exception as e:
            print("\n❌ Error al enviar la orden.")
            print(f"   Detalle: {type(e).__name__}: {e}")
            print("   Revisá: ticker válido y activo en demo, llaves DEMO en .env, KALSHI_ENV=demo.")
            return 1

    print("\n" + "=" * 64)
    print("🎯 RESPUESTA CRUDA — copiá TODO lo que está debajo de esta línea:")
    print("=" * 64)
    print(json.dumps(response, indent=2, default=str))
    print("=" * 64)

    # Ayuda de lectura: marcar qué nombre de campo apareció.
    order = response.get("order", response) if isinstance(response, dict) else {}
    print("\nCampos del sensor encontrados:")
    for k in ("fill_count", "fill_count_fp", "remaining_count", "remaining_count_fp", "status"):
        if k in order:
            print(f"  ✓ {k} = {order[k]!r}")
    print("\nPegame el bloque JSON de arriba en el chat para cerrar el gate del sensor.")
    return 0


if __name__ == "__main__":
    _args = _parse_args()
    # input() fuera del contexto async (no bloquear el event loop).
    _ticker = (_args.ticker or input("Ingresá un ticker activo de DEMO (ej. KXNBA-26-LAL): ")).strip()
    if not _ticker:
        print("❌ Ticker vacío.")
        raise SystemExit(1)
    raise SystemExit(asyncio.run(run(_args, _ticker)))
