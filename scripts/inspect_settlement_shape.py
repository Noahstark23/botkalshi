"""
Smoke read-only del SHAPE de `result` para el settlement del Motor REST.

POR QUÉ EXISTE (Fase 1 del go-live): `KalshiSettlementSource.get_resolution`
(settlement.py) decide si un mercado RESOLVIÓ leyendo `get_market(ticker).result`
∈ {"yes","no"}. El diseño dejó un `[verificar en smoke]` residual: confirmar el
shape EXACTO de ese campo contra un mercado del Mundial YA RESUELTO antes de
confiar los stop-losses a la cadena de settlement. Este script cierra ese gate.

A DIFERENCIA de los otros scripts (standalone, sin importar el bot), este SÍ
importa el adaptador productivo a propósito: el objetivo es validar el código
EXACTO que correrá en prod, no una reimplementación que podría divergir.

Read-only: solo GET /markets/{ticker}. NO toca capital, NO coloca órdenes.

Uso (dentro del container Coolify, con las env vars del bot):
    python scripts/inspect_settlement_shape.py KXWCGAME-26JUN15XXXYYY-XXX
    python scripts/inspect_settlement_shape.py TICKER1 TICKER2 ...

Cómo elegir tickers: un partido del Mundial YA jugado/resuelto. Si no tienes uno
a mano, corre primero `Discovery por serie` en los logs y toma un KXWCGAME-* de
un evento cuyo kickoff ya pasó.
"""

from __future__ import annotations

import asyncio
import json
import sys

from src.clients.kalshi_rest import KalshiRestClient
from src.strategies.motor_rest_arb.settlement import KalshiSettlementSource


async def inspect(tickers: list[str]) -> int:
    """Imprime el shape crudo de `result` y el veredicto del adaptador real.

    Devuelve exit code: 0 si al menos un ticker resolvió a yes/no (shape OK),
    2 si ninguno resolvió (revisar ticker o shape del campo).
    """
    resolved_any = False
    async with KalshiRestClient() as client:
        source = KalshiSettlementSource(client)
        for ticker in tickers:
            print(f"\n{'=' * 60}\nTicker: {ticker}")
            try:
                resp = await client.get_market(ticker)
            except Exception as e:
                print(f"  ❌ get_market falló: {type(e).__name__}: {e}")
                continue

            market = resp.get("market", resp) if isinstance(resp, dict) else {}
            # Lo crítico: el nombre/tipo/valor exacto del campo de resolución.
            result_raw = market.get("result", "<AUSENTE>")
            status = market.get("status", "<AUSENTE>")
            print(f"  status           = {status!r}")
            print(f"  result (crudo)   = {result_raw!r}  (tipo: {type(result_raw).__name__})")
            # Campos candidatos por si el nombre difiere del esperado.
            candidatos = {
                k: market[k]
                for k in ("result", "settlement_value", "settled_result", "outcome")
                if k in market
            }
            print(f"  campos-resolución presentes = {candidatos}")

            # Veredicto del CÓDIGO REAL de producción:
            resolution = await source.get_resolution(ticker)
            if resolution is None:
                print("  → KalshiSettlementSource.get_resolution: None (NO resuelto / shape no matchea)")
            else:
                print(f"  → KalshiSettlementSource.get_resolution: {resolution.result!r} ✅")
                resolved_any = True

    print(f"\n{'=' * 60}")
    if resolved_any:
        print("✅ Shape CONFIRMADO: el adaptador real detecta yes/no en un mercado resuelto.")
        print("   El gate [verificar en smoke] del settlement queda cerrado.")
        return 0
    print("⚠️  Ningún ticker resolvió a yes/no. O el partido aún no resolvió, o el")
    print("   campo `result` tiene otro nombre — revisa 'campos-resolución presentes'")
    print("   arriba y, si difiere, ajusta KalshiSettlementSource.get_resolution.")
    return 2


def main() -> None:
    tickers = sys.argv[1:]
    if not tickers:
        print("Uso: python scripts/inspect_settlement_shape.py <TICKER> [<TICKER> ...]")
        print("     (un mercado del Mundial YA resuelto, ej. un KXWCGAME-* ya jugado)")
        raise SystemExit(1)
    raise SystemExit(asyncio.run(inspect(tickers)))


if __name__ == "__main__":
    main()
