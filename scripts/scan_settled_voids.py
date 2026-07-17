"""
Scanner read-only: busca VOIDS reales en el universo settled de una serie.

POR QUÉ EXISTE (Fase 2 del go-live): `settlement.py` no maneja voids — `_leg_pnl_cents`
solo tiene rama ganadora/perdedora (no refund) y `get_resolution` mapea solo
`result ∈ {yes,no}` sin mirar `status`. El gap es real pero su FIX depende de cómo
Kalshi codifica un void:
  (A) status distinto ('voided'/'cancelled') con result='no'  → fix por status
  (B) status='finalized' + TODAS las patas del evento en 'no'  → fix por grupo (sin ganador)

Este script escanea los markets settled de una serie y FLAGEA cualquier evento que
NO sea un 1X2 limpio (exactamente un ganador), para anclar el diseño con evidencia
real en vez de suposiciones. Si no aparece NINGÚN void en todo el universo settled,
el fix puede esperar y diseñarse defensivamente (mundo B, el más conservador).

Read-only: solo GET /events y GET /markets. NO toca capital. Importa el cliente
productivo a propósito (mismo criterio que inspect_settlement_shape.py).

Uso (dentro del container Coolify, con las env vars del bot):
    python scripts/scan_settled_voids.py
    python scripts/scan_settled_voids.py --series KXWCGAME KXWCGROUPWIN

NOTA: el filtro ?status= solo acepta unopened/open/closed/settled. 'finalized' es el
valor del CAMPO status de un market resuelto, NO un filtro válido (→ 400). Un void cae
igual bajo 'settled'; su status/result reales se leen del objeto y los flagea el scanner.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter

from src.clients.kalshi_rest import KalshiRestClient

_PAUSE_SEC = 0.4  # cortesía anti rate-limit entre eventos


async def _all_events(client: KalshiRestClient, series: str, status: str) -> list[str]:
    """Todos los event_tickers de una serie+status, paginando por cursor."""
    out: list[str] = []
    cursor: str | None = None
    while True:
        resp = await client.list_events(
            series_ticker=series, status=status, limit=200, cursor=cursor
        )
        for ev in resp.get("events", []):
            et = ev.get("event_ticker")
            if et:
                out.append(et)
        cursor = resp.get("cursor") or None
        if not cursor:
            return out


async def _event_markets(client: KalshiRestClient, event_ticker: str) -> list[dict]:
    """TODAS las patas del evento vía get_event (lista canónica, SIN filtro de status).

    Clave del hardening: `list_markets(status=settled)` devuelve un grupo PARCIAL (solo
    las patas ya resueltas). Un grupo a medio jugar parecería entonces 'cero ganadores'
    → falso positivo de void (fue justo el caso KXWCGROUPWIN-26K: 2/6 patas finalized,
    4 active con result=''). get_event trae las N patas reales, activas incluidas.
    """
    resp = await client.get_event(event_ticker)
    return resp.get("markets", []) if isinstance(resp, dict) else []


async def scan(series_list: list[str], statuses: list[str]) -> int:
    """Escanea y reporta voids/anomalías. Exit 0 siempre (es diagnóstico)."""
    flagged: list[str] = []
    total_events = 0
    in_progress = 0  # grupos a medio resolver (saltados, NO son voids)
    seen_statuses: Counter[str] = Counter()

    async with KalshiRestClient() as client:
        for series in series_list:
            event_tickers: dict[str, None] = {}  # dedup preservando orden
            for status in statuses:
                try:
                    for et in await _all_events(client, series, status):
                        event_tickers.setdefault(et, None)
                except Exception as e:
                    print(f"  ⚠️  list_events({series}, {status}): {type(e).__name__}: {e}")

            print(f"\n{'=' * 64}\nSerie {series}: {len(event_tickers)} eventos settled/finalized")
            for et in event_tickers:
                total_events += 1
                await asyncio.sleep(_PAUSE_SEC)
                try:
                    markets = await _event_markets(client, et)
                except Exception as e:
                    print(f"  ⚠️  get_event({et}): {type(e).__name__}: {e}")
                    continue
                if not markets:
                    continue

                results = Counter(str(m.get("result", "")).strip().lower() for m in markets)
                statuses_here = {str(m.get("status", "")).strip().lower() for m in markets}
                seen_statuses.update(statuses_here)

                # COMPUERTA DE COMPLETITUD: una pata sin resolver (result vacío) = grupo EN
                # CURSO, no un void. Juzgar ganadores sobre un grupo parcial fue exactamente
                # el falso positivo de KXWCGROUPWIN-26K → se saltea, no se flagea.
                if results.get("", 0):
                    in_progress += 1
                    continue

                winners = results.get("yes", 0)
                non_yes_no = sum(v for k, v in results.items() if k not in ("yes", "no"))

                # Grupo COMPLETO y limpio = exactamente 1 ganador, todo yes/no.
                if winners == 1 and non_yes_no == 0:
                    continue

                # ── ANOMALÍA real sobre un grupo COMPLETO ──
                tag = []
                if winners == 0:
                    tag.append("CERO ganadores en grupo COMPLETO (void real → mundo B)")
                elif winners > 1:
                    tag.append(f"{winners} ganadores (NO mutuamente excluyente)")
                if non_yes_no:
                    tag.append(f"{non_yes_no} result fuera de yes/no (posible void tipo C)")
                line = f"  🚩 {et}: {dict(results)} | status={statuses_here} → {'; '.join(tag)}"
                print(line)
                flagged.append(line)

    print(f"\n{'=' * 64}\nRESUMEN")
    print(f"  eventos escaneados : {total_events}")
    print(f"  grupos en curso    : {in_progress} (saltados — NO son voids)")
    print(f"  statuses vistos    : {dict(seen_statuses)}")
    print(f"  eventos flageados  : {len(flagged)}")
    if not flagged:
        print("\n✅ Ningún void/anomalía en todo el universo settled.")
        print("   → El gap de settlement es teórico HOY. El fix puede diseñarse")
        print("     defensivamente (mundo B, refund a nivel grupo sin ganador) sin urgencia.")
    else:
        print("\n⚠️  Hay eventos flageados (arriba). Pégalos: distinguen mundo (A) vs (B)")
        print("   y anclan el fix de settlement con evidencia real.")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Scanner read-only de voids en settled.")
    p.add_argument("--series", nargs="+", default=["KXWCGAME"], help="Series a escanear.")
    # OJO: el FILTRO ?status= de /events y /markets solo acepta unopened/open/closed/settled.
    # 'finalized' es el valor del CAMPO status de un market resuelto, NO un filtro válido
    # (→ 400 bad_request). Un void igual cae bajo el filtro 'settled'; su status/result reales
    # se leen del objeto market y los flagea la lógica de anomalías.
    p.add_argument(
        "--statuses", nargs="+", default=["settled"], help="Statuses de FILTRO (no 'finalized')."
    )
    args = p.parse_args()
    raise SystemExit(asyncio.run(scan(args.series, args.statuses)))


if __name__ == "__main__":
    main()
