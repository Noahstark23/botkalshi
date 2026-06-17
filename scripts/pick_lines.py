"""
Hace que el BOT elija las líneas de UN partido (Motor 2), usando la API real.

Reusa el detector de Motor 2 (find_signals): trae el market 1X2 de Kalshi del partido +
el consenso de casas (The Odds API), saca la probabilidad justa (no-vig), y por cada
outcome calcula el edge NETO post-comisión. Recomienda apostar el lado cuyo edge supera
el umbral (MOTOR_2_MIN_EDGE_PCT), con sizing ¼ Kelly cap 5% (= la decisión real del bot).

Read-only: NO coloca órdenes (find_signals es analítico). Usa los umbrales/capital del
config vigente. Guardarraíl pre-match incluido (no evalúa partidos ya iniciados).

Uso (en Coolify, con ODDS_API_KEY seteada):
    python scripts/pick_lines.py --team austria
    python scripts/pick_lines.py --team jordan --series KXWCGAME --sport soccer_fifa_world_cup
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from src.clients.kalshi_rest import KalshiRestClient
from src.strategies.motor_2_consensus.detector import (
    _consensus_fair_probs,
    _h2h_outcome_names,
    _net_edge_pct,
    find_signals,
)
from src.strategies.motor_2_consensus.matcher import canonical_name, outcomes_match
from src.strategies.motor_2_consensus.sources import LiveOddsSource, _parse_event_quotes
from src.utils.config import get_settings


async def _find_kalshi_event(client: KalshiRestClient, series: str, team: str):
    """KalshiEventQuotes del evento ABIERTO de la serie cuyos outcomes incluyen `team`."""
    resp = await client.list_events(series_ticker=series, status="open", limit=200)
    for ev in resp.get("events", []):
        et = ev.get("event_ticker")
        if not et:
            continue
        detail = await client.get_event(et)
        eq = _parse_event_quotes(et, detail.get("markets", []))
        if eq is not None and any(
            team.lower() in canonical_name(q.outcome_name) for q in eq.outcomes
        ):
            return eq
        await asyncio.sleep(0.2)
    return None


async def run(series: str, sport: str, team: str) -> int:
    s = get_settings()
    capital = s.ACTIVE_CAPITAL_USD
    min_edge = s.MOTOR_2_MIN_EDGE_PCT / 100.0
    now = datetime.now(UTC)

    async with KalshiRestClient() as kc:
        eq = await _find_kalshi_event(kc, series, team)
    if eq is None:
        print(
            f"❌ No encontré un evento {series} ABIERTO con '{team}'. ¿Ya empezó o no está listado?"
        )
        return 1
    odds = await LiveOddsSource([sport], regions=s.ODDS_API_REGIONS).fetch()

    print(f"\n{'=' * 64}\nPartido Kalshi: {eq.event_key}")
    print(f"  capital=${capital:.0f}  umbral_edge={min_edge * 100:.1f}pp  (config vigente)")
    print("  outcomes Kalshi (ask):")
    for q in eq.outcomes:
        print(f"    {q.outcome_name:<20} yes_ask={q.yes_ask_cents}c")

    # Emparejar contra el odds event pre-match (cardinalidad + nombres exactos).
    k_names = [q.outcome_name for q in eq.outcomes]
    matched = next(
        (
            oe
            for oe in odds
            if oe.commence_time > now
            and outcomes_match(k_names, _h2h_outcome_names(oe)) is not None
        ),
        None,
    )
    if matched is None:
        started = any(
            oe.commence_time <= now and outcomes_match(k_names, _h2h_outcome_names(oe)) is not None
            for oe in odds
        )
        print(
            "\n⚠️  Sin consenso pre-match usable: "
            + (
                "el partido YA EMPEZÓ (edge inválido in-play)."
                if started
                else "no está en el feed de odds o los nombres no matchean."
            )
        )
        return 2

    # Desglose por outcome (la "vista" del bot) + las señales oficiales (lo que ELIGE).
    fair = _consensus_fair_probs(matched)
    print(f"\n  Consenso de casas ({sport}): {matched.commence_time:%Y-%m-%d %H:%M}Z")
    print(f"  {'outcome':<20} {'fair%':>7} {'kalshi¢':>8} {'edge_neto':>10}")
    for q in eq.outcomes:
        cn = canonical_name(q.outcome_name)
        fp = fair.get(cn)
        if fp is None:
            print(f"    {q.outcome_name:<20} {'—':>7} {q.yes_ask_cents:>7}c {'(sin fair)':>10}")
            continue
        net = _net_edge_pct(fp, q.yes_ask_cents)
        flag = "  ← BET" if net > min_edge else ""
        print(
            f"    {q.outcome_name:<20} {fp * 100:>6.1f}% {q.yes_ask_cents:>7}c {net * 100:>8.1f}pp{flag}"
        )

    signals = find_signals([eq], odds, capital_usd=capital, min_edge=min_edge, now=now)

    print(f"\n{'=' * 64}\nLÍNEAS QUE EL BOT ELIGE ({len(signals)}):")
    if not signals:
        print("  (ninguna) — ningún lado supera el umbral; mercado eficiente vs el consenso.")
    for sig in signals:
        contratos = int(sig.recommended_size_usd * 100 / sig.kalshi_price_cents)
        print(
            f"  ✅ {sig.kalshi_side} {sig.market_ticker} @ {sig.kalshi_price_cents}c | "
            f"fair={sig.odds_api_fair_prob * 100:.1f}% edge={sig.edge_pct * 100:.1f}pp | "
            f"size=${sig.recommended_size_usd:.2f} (~{contratos} contratos, ¼Kelly cap 5%/$200)"
        )
    print("\n(SHADOW: esto es lo que APOSTARÍA; no coloca órdenes — TRADING_ENABLED gobierna eso.)")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="El bot elige las líneas de un partido (Motor 2).")
    p.add_argument("--team", required=True, help="Filtro de equipo (ej. austria, jordan).")
    p.add_argument("--series", default="KXWCGAME", help="Serie Kalshi (default KXWCGAME 1X2).")
    p.add_argument("--sport", default="soccer_fifa_world_cup", help="sport_key de The Odds API.")
    args = p.parse_args()
    raise SystemExit(asyncio.run(run(args.series, args.sport, args.team)))


if __name__ == "__main__":
    main()
