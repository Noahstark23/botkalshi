"""
Inspector read-only: enumera TODAS las patas de un evento vía get_event.

POR QUÉ EXISTE: el scanner de voids (scan_settled_voids.py) consulta con filtro
`status=settled` y puede ver un grupo INCOMPLETO si alguna pata quedó fuera del
filtro. Un "cero ganadores" así es ambiguo: void real (mundo B) vs grupo parcial
donde el ganador (yes) es una pata no capturada. `get_event` devuelve la lista
CANÓNICA de markets del evento (sin filtro de status) → zanja la ambigüedad.

Read-only: solo GET /events/{event_ticker}. NO toca capital.

Uso (dentro del container Coolify, con las env vars del bot):
    python scripts/inspect_event_legs.py KXWCGROUPWIN-26K
    python scripts/inspect_event_legs.py EVENTO1 EVENTO2 ...
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter

from src.clients.kalshi_rest import KalshiRestClient


async def inspect(event_tickers: list[str]) -> int:
    """Lista todas las patas de cada evento + veredicto void/limpio/incompleto.

    Exit 0 siempre (diagnóstico).
    """
    async with KalshiRestClient() as client:
        for et in event_tickers:
            print(f"\n{'=' * 64}\nEvento: {et}")
            try:
                resp = await client.get_event(et)
            except Exception as e:
                print(f"  ❌ get_event falló: {type(e).__name__}: {e}")
                continue

            markets = resp.get("markets", []) if isinstance(resp, dict) else []
            if not markets:
                print("  ⚠️  get_event no devolvió markets (¿evento inexistente?)")
                continue

            results: Counter[str] = Counter()
            for m in markets:
                ticker = m.get("ticker", "<sin-ticker>")
                status = str(m.get("status", "<none>"))
                result = str(m.get("result", "<none>")).lower()
                results[result] += 1
                print(f"  {ticker:<40} status={status:<12} result={result!r}")

            n = len(markets)
            winners = results.get("yes", 0)
            non_yes_no = sum(v for k, v in results.items() if k not in ("yes", "no"))
            print(f"  ── {n} patas | resultados={dict(results)}")
            if non_yes_no:
                print("  → ⚠️  hay result fuera de yes/no (posible void tipo (C) / pendiente).")
            elif winners == 1:
                print("  → ✅ GRUPO LIMPIO: exactamente 1 ganador. El '🚩 cero ganadores' del")
                print("       scanner era un FALSO POSITIVO por grupo incompleto (filtro status).")
            elif winners == 0:
                print("  → 🚩 VOID REAL (mundo B): grupo COMPLETO con cero ganadores → refund.")
            else:
                print(f"  → ⚠️  {winners} ganadores: NO es mutuamente excluyente — revisar.")

    return 0


def main() -> None:
    tickers = sys.argv[1:]
    if not tickers:
        print("Uso: python scripts/inspect_event_legs.py <EVENT_TICKER> [<EVENT_TICKER> ...]")
        print("     ej. python scripts/inspect_event_legs.py KXWCGROUPWIN-26K")
        raise SystemExit(1)
    raise SystemExit(asyncio.run(inspect(tickers)))


if __name__ == "__main__":
    main()
