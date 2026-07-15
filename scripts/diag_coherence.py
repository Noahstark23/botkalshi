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
    python scripts/diag_coherence.py --ladder                          # escaleras ordinales
    python scripts/diag_coherence.py --ladder --series KXFIFATOTAL     # escalera custom

MODO ESCALERA (--ladder, propuesta "Motor 10" 2026-07-13): dentro de un mismo evento
ordinal (totales de goles, strikes), X>alto ⟹ X>bajo, así que P(alto) ≤ P(bajo).
⚠️ La pata correcta del arb (la propuesta original venía INVERTIDA y perdía todo en el
bucket del medio): comprar YES del strike BAJO + NO del strike ALTO → payout garantizado
≥ $1 (medio paga $2). HARD si ask_yes(bajo) + ask_no(alto) + fees < 100¢.

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
# Escaleras ordinales default (--ladder): series con strikes numéricos del MISMO evento.
DEFAULT_LADDER_SERIES = ["KXFIFATOTAL", "KXWCTEAMGOALS"]


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


def _strike_of(m: dict[str, Any]) -> float | None:
    """Strike numérico del market: floor_strike de la API si viene; si no, el último
    segmento numérico del ticker (ej. ...-T2.5 → 2.5). None = no ordenable."""
    fs = m.get("floor_strike")
    if isinstance(fs, (int, float)):
        return float(fs)
    tail = m.get("ticker", "").rsplit("-", 1)[-1].lstrip("TABOU")
    try:
        return float(tail)
    except ValueError:
        return None


def evaluate_ladder_pair(weak: dict[str, Any], strong: dict[str, Any]) -> dict[str, Any] | None:
    """Par ordinal del MISMO evento: weak=strike bajo, strong=strike alto (alto ⟹ bajo).

    HARD (arb real, PATAS CORRECTAS): comprar YES(bajo) + NO(alto). Payout: X>alto → 100;
    medio → 200; X≤bajo → 100 (garantizado ≥100). Costo = ask_yes(bajo) + ask_no(alto);
    neto = 100 − costo − fees > 0 → violación fillable.
    soft: mid_yes(alto) > mid_yes(bajo) — monotonía rota a mid (el spread la tapa).
    """
    w_mid, s_mid = _mid(weak), _mid(strong)
    if w_mid is None or s_mid is None:
        return None
    w_ask = weak.get("yes_ask") or 0
    s_no_ask = strong.get("no_ask") or 0
    res: dict[str, Any] = {
        "event": weak.get("event_ticker", "?"),
        "weak": weak["ticker"],
        "strong": strong["ticker"],
        "w_ask": w_ask,
        "s_no_ask": s_no_ask,
        "hard": False,
        "soft": s_mid > w_mid,
        "net_cents": None,
    }
    if 1 <= w_ask <= 99 and 1 <= s_no_ask <= 99:
        cost = w_ask + s_no_ask
        fees = kalshi_fee_cents(1, w_ask) + kalshi_fee_cents(1, s_no_ask)
        net = 100 - cost - fees
        if net > 0:
            res["hard"] = True
            res["net_cents"] = net
    return res


def run_ladder_study(series_list: list[str]) -> int:
    """Estudio de monotonía en escaleras ordinales: agrupa por evento, ordena por strike,
    evalúa TODOS los pares ordenados (bajo, alto) del evento."""
    print("=" * 72)
    print("ESTUDIO DE ESCALERAS ORDINALES (X>alto ⟹ X>bajo) — read-only, API pública")
    print("=" * 72)
    with httpx.Client() as client:
        markets: list[dict[str, Any]] = []
        for s in series_list:
            ms = fetch_series(client, s)
            print(f"{s}: {len(ms)} markets open")
            markets.extend(ms)

    by_event: dict[str, list[tuple[float, dict[str, Any]]]] = {}
    unstrikeable = 0
    for m in markets:
        strike = _strike_of(m)
        if strike is None:
            unstrikeable += 1
            continue
        by_event.setdefault(m.get("event_ticker", m["ticker"]), []).append((strike, m))
    if unstrikeable:
        print(f"(sin strike parseable: {unstrikeable} markets — no entran a la escalera)")

    pairs = soft = hard = 0
    results: list[dict[str, Any]] = []
    for _event, rungs in by_event.items():
        rungs.sort(key=lambda x: x[0])
        for i in range(len(rungs)):
            for j in range(i + 1, len(rungs)):
                res = evaluate_ladder_pair(rungs[i][1], rungs[j][1])
                if res is None:
                    continue
                pairs += 1
                soft += res["soft"]
                hard += res["hard"]
                if res["soft"] or res["hard"]:
                    results.append(res)

    print(
        f"\nEventos con escalera: {len(by_event)}  ·  Pares: {pairs}  ·  soft: {soft}  ·  HARD: {hard}"
    )
    for r in sorted(results, key=lambda x: (not x["hard"], -(x["net_cents"] or -999)))[:30]:
        tag = f"🔴 HARD net={r['net_cents']}¢/par" if r["hard"] else "🟠 soft"
        print(
            f"  {tag}  {r['event']}: YES({r['weak']})@{r['w_ask']} + NO({r['strong']})@{r['s_no_ask']}"
        )

    print("\nVEREDICTO:")
    if pairs == 0:
        print("  ⚪ NO EVALUABLE: 0 pares — revisar series/strikes (tickers impresos arriba).")
        for m in markets[:20]:
            print(f"    {m['ticker']} floor_strike={m.get('floor_strike')}")
    elif hard:
        print("  🔴 Violaciones de monotonía FILLABLES post-fee → tesis Motor 10 CON datos.")
    elif soft:
        print("  🟠 Monotonía rota a mid pero el spread la tapa → repetir en movimiento.")
    else:
        print("  🟢 Escaleras coherentes hoy — sin tesis (archivar costó $0).")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Estudio de coherencia cross-market (read-only).")
    p.add_argument("--series", help="Series implicante,implicada (override; coma-separadas)")
    p.add_argument("--ladder", action="store_true", help="Modo escaleras ordinales (Motor 10)")
    args = p.parse_args()

    if args.ladder:
        series = args.series.split(",") if args.series else DEFAULT_LADDER_SERIES
        return run_ladder_study(series)

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
    if pairs == 0:
        # 0 pares = NO se midió nada — jamás concluir "coherente" (defecto detectado en la
        # primera corrida real: 4 campeón × 5 etapa → 0 matcheos). Imprimir los tickers
        # permite distinguir a ojo "no hay solapamiento de equipos" de "el matching por
        # código de equipo no encaja con la estructura real de la serie".
        print("  ⚪ NO EVALUABLE: 0 pares matcheados — esto NO es evidencia de coherencia.")
        print("  Tickers campeón:", sorted(m["ticker"] for m in champ_markets))
        print("  Tickers etapa:  ", sorted(m["ticker"] for m in stage_markets))
        print("  Revisar solapamiento de equipos / estructura del ticker de etapa.")
        return 0
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
