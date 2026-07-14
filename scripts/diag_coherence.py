#!/usr/bin/env python3
"""
Estudio de COHERENCIA cross-market (paso 0 del candidato "Motor 7") — READ-ONLY.

TESIS A VALIDAR: entre mercados de Kalshi ligados por IMPLICACIÓN LÓGICA (campeón ⟹
llega a X etapa), los precios deben ser monótonos: P(campeón) ≤ P(etapa). Una violación
no es una correlación rota — es una incoherencia lógica tradeable con payout acotado
(comprar YES de la etapa + NO del campeón garantiza cobro ≥ $1 por par). Este script
MIDE si esas violaciones existen hoy y si sobreviven a spread + fees. No es la versión
"correlación estadística" (esa falla el paso 1 del repo: no hay series históricas
suficientes); es la versión determinística.

READ-ONLY TOTAL: usa la API PÚBLICA de market-data de Kalshi (sin API key, sin firmar,
sin tocar la DB ni el bot). Correr donde haya salida a internet (el container sirve):

    python scripts/diag_coherence.py
    python scripts/diag_coherence.py --series KXMENWORLDCUP,KXWCSTAGE  # universo custom

QUÉ MIRAR EN LA SALIDA:
  - "HARD": violación fillable AL BID/ASK actual con neto post-fee > 0 → tesis del
    Motor 7 CON datos (raro; el mercado suele ser coherente).
  - "soft": incoherencia a precio MEDIO (no fillable) → interesante solo si es frecuente
    y grande (indica que ventanas fillables pueden aparecer en momentos de movimiento).
  - Ni soft ni hard → veredicto honesto: no hay tesis; archivar la idea cuesta $0.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import httpx

sys.path.insert(0, ".")
from src.math.fees import kalshi_fee_cents  # noqa: E402 (fórmula OFICIAL post 2026-07-01)

BASE = "https://api.elections.kalshi.com/trade-api/v2"
# Cadenas de implicación por default (Mundial): el CAMPEÓN necesariamente "llega a" toda
# etapa de KXWCSTAGE. OJO: campeón NO implica ganar el grupo (se avanza como segundo) —
# KXWCGROUPWIN se lista solo como contexto, sin flag de violación.
DEFAULT_IMPLICANT = ["KXMENWORLDCUP", "KXMWORLDCUP"]  # A (lo más específico)
DEFAULT_IMPLIED = ["KXWCSTAGE"]  # B (A ⟹ B)
DEFAULT_CONTEXT = ["KXWCGROUPWIN"]  # solo se imprime (no hay implicación válida)


def fetch_series(client: httpx.Client, series: str) -> list[dict[str, Any]]:
    """Todos los markets open de una serie (paginado por cursor). Best-effort."""
    out: list[dict[str, Any]] = []
    cursor = None
    for _ in range(20):  # tope de páginas (anti-loop)
        params: dict[str, Any] = {"series_ticker": series, "status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        r = client.get(f"{BASE}/markets", params=params, timeout=20)
        if r.status_code != 200:
            print(f"  ⚠️ {series}: HTTP {r.status_code} — {r.text[:120]}")
            return out
        data = r.json()
        out.extend(data.get("markets", []))
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


def _team_of(ticker: str) -> str:
    """Código de equipo = último segmento del ticker (convención de las series del Mundial)."""
    return ticker.rsplit("-", 1)[-1]


def _mid(m: dict[str, Any]) -> float | None:
    bid, ask = m.get("yes_bid") or 0, m.get("yes_ask") or 0
    if not (1 <= bid <= 99 and 1 <= ask <= 99):
        return None
    return (bid + ask) / 2.0


def evaluate_pair(champ: dict[str, Any], stage: dict[str, Any]) -> dict[str, Any] | None:
    """Evalúa la implicación campeón ⟹ etapa para un equipo. Devuelve el veredicto del par.

    HARD (arb real): yes_ask(etapa) < yes_bid(campeón) → comprar YES etapa + NO campeón
    cuesta < $1 con payout garantizado ≥ $1. Neto post-fee = 100 − costo − fees.
    soft: mid(campeón) > mid(etapa) — incoherencia no fillable (spread la tapa).
    """
    c_bid, s_ask = champ.get("yes_bid") or 0, stage.get("yes_ask") or 0
    c_mid, s_mid = _mid(champ), _mid(stage)
    if c_mid is None or s_mid is None:
        return None
    res: dict[str, Any] = {
        "team": _team_of(champ["ticker"]),
        "champ": champ["ticker"],
        "stage": stage["ticker"],
        "c_bid": c_bid,
        "s_ask": s_ask,
        "c_mid": c_mid,
        "s_mid": s_mid,
        "hard": False,
        "soft": c_mid > s_mid,
        "net_cents": None,
    }
    if 1 <= s_ask <= 99 and 1 <= c_bid <= 99 and s_ask < c_bid:
        no_champ_ask = 100 - c_bid  # comprar NO del campeón al bid del YES
        cost = s_ask + no_champ_ask
        fees = kalshi_fee_cents(1, s_ask) + kalshi_fee_cents(1, no_champ_ask)
        res["hard"] = True
        res["net_cents"] = 100 - cost - fees
    return res


def main() -> int:
    p = argparse.ArgumentParser(description="Estudio de coherencia cross-market (read-only).")
    p.add_argument("--series", help="Series implicante,implicada (override; coma-separadas)")
    args = p.parse_args()

    implicant, implied = DEFAULT_IMPLICANT, DEFAULT_IMPLIED
    if args.series:
        parts = args.series.split(",")
        implicant, implied = [parts[0]], parts[1:] or DEFAULT_IMPLIED

    print("=" * 72)
    print("ESTUDIO DE COHERENCIA (campeón ⟹ etapa) — read-only, API pública")
    print("=" * 72)
    with httpx.Client() as client:
        champ_markets: list[dict[str, Any]] = []
        for s in implicant:
            ms = fetch_series(client, s)
            print(f"{s}: {len(ms)} markets open")
            champ_markets.extend(ms)
        stage_markets: list[dict[str, Any]] = []
        for s in implied:
            ms = fetch_series(client, s)
            print(f"{s}: {len(ms)} markets open")
            stage_markets.extend(ms)
        for s in DEFAULT_CONTEXT if not args.series else []:
            ms = fetch_series(client, s)
            print(f"{s} (contexto, sin implicación válida): {len(ms)} markets")

    pairs = soft = hard = 0
    results: list[dict[str, Any]] = []
    for champ in champ_markets:
        team = _team_of(champ["ticker"])
        # En KXWCSTAGE el equipo puede NO ser el último segmento (ahí va la etapa) →
        # matchear por presencia del código en el ticker. El reporte imprime ambos
        # tickers: cualquier falso positivo de match se VE a ojo antes de decidir nada.
        candidates = [m for m in stage_markets if team in m["ticker"].split("-", 1)[-1]]
        for stage in candidates:
            res = evaluate_pair(champ, stage)
            if res is None:
                continue
            pairs += 1
            soft += res["soft"]
            hard += res["hard"]
            if res["soft"] or res["hard"]:
                results.append(res)

    print(f"\nPares evaluados: {pairs}  ·  soft (mid incoherente): {soft}  ·  HARD: {hard}")
    for r in sorted(results, key=lambda x: (not x["hard"], -(x["net_cents"] or -999))):
        tag = f"🔴 HARD net={r['net_cents']}¢/par" if r["hard"] else "🟠 soft"
        print(
            f"  {tag}  {r['team']}: campeón bid={r['c_bid']} mid={r['c_mid']:.1f} "
            f"vs etapa ask={r['s_ask']} mid={r['s_mid']:.1f}  ({r['stage']})"
        )

    print("\nVEREDICTO:")
    if hard:
        print("  🔴 Hay violaciones FILLABLES post-fee → tesis del Motor 7 CON datos.")
        print("     Siguiente paso: F1 shadow según el patrón de escalabilidad del repo.")
    elif soft:
        print("  🟠 Incoherencias a mid pero el spread las tapa → repetir el estudio en")
        print("     momentos de movimiento (post-partido) antes de decidir.")
    else:
        print("  🟢 Mercado coherente: no hay tesis hoy. Archivar la idea costó $0.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
