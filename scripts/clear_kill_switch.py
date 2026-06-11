"""
Despausa MANUAL del kill-switch persistente del Motor REST.

Único camino para limpiar la pausa (nada automático lo hace). Antes de limpiar,
VERIFICA que no haya posiciones abiertas en Kalshi — si las hay, ABORTA y te dice
cuáles cerrar a mano primero. Tras limpiar, hay que REDEPLOYAR para que la Capa A
vuelva a construir el executor.

Uso (en el host):
    docker exec -it kalshi-bot python -m scripts.clear_kill_switch
"""
from __future__ import annotations

import asyncio

from src.clients.kalshi_rest import KalshiRestClient
from src.storage import models


def _position_count(p: dict) -> int:
    raw = p.get("position")
    try:
        return int(raw) if raw is not None else 0
    except (ValueError, TypeError):
        return 0


async def _open_positions() -> list[dict]:
    async with KalshiRestClient() as client:
        resp = await client.get_positions()
        positions = resp.get("market_positions", resp.get("positions", []))
    return [p for p in positions if _position_count(p) != 0]


def main() -> int:
    engaged, reason = models.kill_switch_engaged()
    if not engaged:
        print("Kill-switch NO engaged. Nada que limpiar.")
        return 0

    print(f"Kill-switch ENGAGED. Razón: {reason}")
    print("Verificando posiciones abiertas en Kalshi...")
    open_positions = asyncio.run(_open_positions())

    if open_positions:
        print(f"\n⛔ ABORT: hay {len(open_positions)} posición(es) abierta(s). "
              "Cerralas a mano (Kalshi UI) ANTES de limpiar el kill-switch:")
        for p in open_positions:
            print(f"  - {p.get('ticker')}: position={p.get('position')}")
        return 1

    print("✅ Posiciones en cero confirmado.")
    confirm = input("Escribí 'CLEAR' para despausar el kill-switch: ").strip()
    if confirm != "CLEAR":
        print("Cancelado (no se escribió CLEAR). Kill-switch sigue engaged.")
        return 1

    models.clear_kill_switch()
    print("🔓 Kill-switch LIMPIADO. REDEPLOYÁ para reactivar el trading "
          "(la Capa A reconstruye el executor solo si NO está engaged).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
