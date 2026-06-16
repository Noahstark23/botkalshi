"""
Diagnóstico read-only del matching de Motor 2: por qué Kalshi↔Odds API no emparejan.

POR QUÉ: el funnel mostró matched=0 con rej_absent=N (rej_names=0). OJO — el detector
clasifica 'absent' cuando el overlap de nombres CANÓNICOS es 0 contra TODO odds event
pre-match (detector.py:216). Eso NO descarta nombres: si Kalshi dice "Yankees" y Odds
API "New York Yankees", el overlap EXACTO es 0 → se clasifica 'absent', no 'names'. Y
'absent' también lo causan timing (juego ya iniciado → filtrado pre-match) y scope
(juego no está en el feed). Este script separa los tres casos:

  - NAMING (full-vs-short): overlap exacto 0 pero los nombres comparten TOKENS
    (ej. "yankees" ∈ "new york yankees") → fix = alias/normalización.
  - TIMING: solo hay match con eventos odds YA INICIADOS (post-commence).
  - SCOPE: el partido no aparece en el feed odds de ninguna forma.

Read-only. Kalshi: list_events/get_event. Odds: The Odds API (baseball_mlb default).

Uso (en Coolify):
    python scripts/diag_motor2_match.py
    python scripts/diag_motor2_match.py --series KXMLBGAME --sport baseball_mlb
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from src.clients.kalshi_rest import KalshiRestClient
from src.clients.odds_api import OddsAPIClient, OddsEvent
from src.strategies.motor_2_consensus.detector import _h2h_outcome_names
from src.strategies.motor_2_consensus.matcher import canonical_name
from src.utils.config import get_settings


def _tokens(canon_names: set[str]) -> set[str]:
    """Tokens individuales de un set de nombres canónicos (para overlap laxo full-vs-short)."""
    return {tok for n in canon_names for tok in n.split() if tok}


async def _kalshi_events(client: KalshiRestClient, series: str) -> list[tuple[str, list[str]]]:
    """[(event_ticker, [outcome names crudos])] de la serie (open)."""
    out: list[tuple[str, list[str]]] = []
    resp = await client.list_events(series_ticker=series, status="open", limit=200)
    for ev in resp.get("events", []):
        et = ev.get("event_ticker")
        if not et:
            continue
        try:
            detail = await client.get_event(et)
        except Exception as e:
            print(f"  ⚠️ get_event({et}): {type(e).__name__}: {e}")
            continue
        names = [
            m.get("yes_sub_title") or m.get("yes_subtitle")
            for m in detail.get("markets", [])
            if (m.get("yes_sub_title") or m.get("yes_subtitle"))
        ]
        if names:
            out.append((et, names))
        await asyncio.sleep(0.3)
    return out


async def run(series: str, sport: str) -> int:
    s = get_settings()
    now = datetime.now(UTC)

    async with KalshiRestClient() as kc:
        kalshi = await _kalshi_events(kc, series)
    async with OddsAPIClient() as oc:
        odds: list[OddsEvent] = await oc.get_odds(sport, regions=s.ODDS_API_REGIONS)

    # ── Lado Kalshi ──
    print(f"\n{'=' * 66}\nKALSHI ({series}): {len(kalshi)} eventos")
    for et, names in kalshi:
        canon = sorted(canonical_name(n) for n in names)
        print(f"  {et}\n     nombres={names}  canónico={canon}")

    # ── Lado Odds API ──
    pre = [oe for oe in odds if oe.commence_time > now]
    print(
        f"\n{'=' * 66}\nODDS API ({sport}): {len(odds)} eventos ({len(pre)} pre-match, "
        f"{len(odds) - len(pre)} ya iniciados)"
    )
    for oe in odds:
        onames = _h2h_outcome_names(oe)
        tag = "pre-match" if oe.commence_time > now else "INICIADO"
        print(
            f"  {oe.commence_time:%m-%d %H:%M}Z [{tag}] {onames} "
            f"canónico={sorted(canonical_name(n) for n in onames)}"
        )

    # ── Cruce: por qué cada evento Kalshi no matchea ──
    odds_canon = [
        (oe, {canonical_name(n) for n in _h2h_outcome_names(oe)}, oe.commence_time > now)
        for oe in odds
        if _h2h_outcome_names(oe)
    ]
    print(f"\n{'=' * 66}\nCRUCE — diagnóstico por evento Kalshi")
    verdicts: dict[str, int] = {"MATCH": 0, "NAMING": 0, "TIMING": 0, "SCOPE": 0}
    for et, names in kalshi:
        k_canon = {canonical_name(n) for n in names}
        k_tok = _tokens(k_canon)
        best_exact_pre = max((len(k_canon & oc) for oe, oc, p in odds_canon if p), default=0)
        # token-overlap (full-vs-short) contra pre-match
        tok_pre = [(oe, oc) for oe, oc, p in odds_canon if p and (k_tok & _tokens(oc))]
        # exact/token overlap contra YA INICIADOS (timing)
        any_started = [
            oe for oe, oc, p in odds_canon if not p and (k_canon & oc or k_tok & _tokens(oc))
        ]
        if best_exact_pre == len(k_canon) and best_exact_pre > 0:
            verdict = "MATCH"
        elif tok_pre and best_exact_pre < len(k_canon):
            verdict = "NAMING"
        elif any_started:
            verdict = "TIMING"
        else:
            verdict = "SCOPE"
        verdicts[verdict] += 1
        line = f"  [{verdict}] {et} canónico={sorted(k_canon)}"
        if verdict == "NAMING":
            ex = tok_pre[0][1]
            line += f"  ↔ odds={sorted(ex)} (comparten tokens, NO exacto → alias/normalización)"
        elif verdict == "TIMING":
            line += "  (solo matchea con juegos YA iniciados → esperar pre-match / horario)"
        elif verdict == "SCOPE":
            line += "  (sin overlap en ningún odds → no está en el feed)"
        print(line)

    print(f"\n{'=' * 66}\nVEREDICTO: {verdicts}")
    if verdicts["NAMING"]:
        print("  → Predomina NAMING: full-vs-short. Fix = alias MLB / normalización por token.")
    elif verdicts["TIMING"]:
        print("  → Predomina TIMING: esperar al horario de juegos MLB (noche US) y re-mirar.")
    elif verdicts["SCOPE"]:
        print(
            "  → Predomina SCOPE: los partidos Kalshi no están en el feed odds — revisar sport/fecha."
        )
    elif verdicts["MATCH"]:
        print(
            "  → Hay MATCHes: el matching funciona; señales=0 sería por edge/no_fair, no por matching."
        )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Diagnóstico read-only del matching de Motor 2.")
    p.add_argument("--series", default="KXMLBGAME", help="Serie Kalshi (default KXMLBGAME).")
    p.add_argument("--sport", default="baseball_mlb", help="sport_key de The Odds API.")
    args = p.parse_args()
    raise SystemExit(asyncio.run(run(args.series, args.sport)))


if __name__ == "__main__":
    main()
