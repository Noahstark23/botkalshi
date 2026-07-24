"""
Diagnóstico read-only de POR QUÉ Motor 2 emite señales=0 — la pregunta "¿dónde muere el funnel?".

Dos modos, ambos SIN escribir nada:

1) DEFAULT (solo DB, cero red): agrega los Motor2FunnelSnapshot que el poller YA persiste en
   cada ciclo live (matched, rejects, best_edge, signals) y emite un VEREDICTO automático usando
   el discriminador clave: `best_net_edge` se actualiza en _signals_for_outcome ANTES del filtro
   de umbral pero DESPUÉS del lookup fair.get(canonical_name(...)). Por lo tanto:

     matched=0  + best=-1      → nada matchea en el path LIVE (timing/scope; mirar rej_*)
     matched>0  + best=-1      → asks=100¢ (book sin lado ask) en todo outcome matcheado
     0 < best ≤ umbral         → mercado EFICIENTE: hay edges pero bajo el umbral (no es bug)
     umbral < best ≤ techo     → BUG en emit/colapso (debería haber señal y no la hubo)
     best > techo (MAX_EDGE)   → edges altos rechazados por anti-fantasma (rej_high en logs)

2) --live: UNA pasada en vivo (mismas fuentes que el runner: get_event de Kalshi + The Odds API)
   con dump VERBOSE por outcome — fair, yes/no ask, ambos edges vía el _net_edge_pct REAL, y el
   veredicto de cada lado. Reusa find_signals con sus params diag y fair_out (cero duplicación
   de fórmulas). Cuesta ~1 ciclo de cuota de API.

Uso (en el contenedor):
    python scripts/diag_motor2_funnel.py                # veredicto desde la DB (gratis)
    python scripts/diag_motor2_funnel.py --last 400     # más ciclos de historia
    python scripts/diag_motor2_funnel.py --live         # pasada en vivo con dump por outcome
"""

from __future__ import annotations

import argparse
import asyncio
import statistics

from sqlmodel import col, select

from src.storage.models import Motor2FunnelSnapshot, get_session
from src.utils.config import get_settings


def _pp(x: float) -> str:
    return f"{x:.2f}pp"


# =====================================================
# Modo DB — agrega los snapshots que el poller ya persiste
# =====================================================


def run_db(last: int) -> int:
    s = get_settings()
    umbral_pp = s.MOTOR_2_MIN_EDGE_PCT
    techo_pp = s.MOTOR_2_MAX_EDGE_PCT

    with get_session() as sess:
        rows = list(
            sess.exec(
                select(Motor2FunnelSnapshot)
                .order_by(col(Motor2FunnelSnapshot.id).desc())
                .limit(last)
            )
        )
    if not rows:
        print("Sin Motor2FunnelSnapshot en la DB — ¿el poller corre con odds LIVE?")
        return 1
    rows.reverse()  # cronológico

    n = len(rows)
    con_senales = sum(1 for r in rows if r.signals > 0)
    edges = [r.best_net_edge_pp for r in rows]
    evaluados = [e for e in edges if e > -1.0]  # -1 = ningún outcome llegó al cálculo de edge
    matched_avg = sum(r.events_matched for r in rows) / n
    started_avg = sum(r.started_skip for r in rows) / n

    print(
        f"{'=' * 68}\nFUNNEL Motor 2 — últimos {n} ciclos (umbral={umbral_pp}pp techo={techo_pp}pp)"
    )
    print(
        f"  señales>0 en {con_senales}/{n} ciclos · matched prom={matched_avg:.1f} · "
        f"odds ya-iniciados prom={started_avg:.1f}"
    )
    print(
        f"  rechazos acumulados: absent={sum(r.reject_absent for r in rows)} "
        f"card={sum(r.reject_cardinality for r in rows)} names={sum(r.reject_names for r in rows)} "
        f"nofair={sum(r.reject_no_fair for r in rows)}"
    )
    if evaluados:
        print(
            f"  best_edge (ciclos con outcomes evaluados: {len(evaluados)}/{n}): "
            f"max={_pp(max(evaluados))} p50={_pp(statistics.median(evaluados))} "
            f"p90={_pp(statistics.quantiles(evaluados, n=10)[8]) if len(evaluados) >= 10 else '—'}"
        )
    else:
        print(f"  best_edge: -1 en {n}/{n} ciclos (NINGÚN outcome llegó al cálculo de edge)")

    # ── Veredicto automático (el discriminador del docstring) ──
    print(f"{'=' * 68}\nVEREDICTO")
    if not evaluados:
        if matched_avg > 0:
            # Auditoría 2026-07-03 (15 agentes, refutación adversarial): el lookup de nombres NO
            # puede fallar tras un match (el reference-set del matcher garantiza fair.get(cn) no
            # None). La ÚNICA causa posible de matched>0 + best=-100.0 exacto es asks=100¢ (lado
            # ask vacío del book): sources.py los deja pasar (truthiness) y _net_edge_pct los
            # colapsa a -1.0, indistinguible del sentinel "nada evaluado".
            print(
                "  🔴 hay eventos matcheados pero NINGÚN outcome dio edge computable →\n"
                "     asks=100¢ (lado ask del book VACÍO en todos los outcomes matcheados):\n"
                "     pasan el filtro de sources y _net_edge_pct los colapsa a -1. No hay contra\n"
                "     quién comprar → sin señal posible. Corré --live para ver los asks reales;\n"
                "     si es constante, los markets matcheados no tienen liquidez del lado ask."
            )
        else:
            print(
                "  🟡 Nada matchea en el path live (matched=0). Mirá los rej_* de arriba y la\n"
                "     línea `motor2.funnel` (rej_date/rej_started/skip_horizon solo están en logs).\n"
                "     Si started_skip domina: es TIMING (partidos ya in-play en horas de juego)."
            )
        return 0

    mx = max(evaluados)
    if mx <= umbral_pp:
        pct_cerca = sum(1 for e in evaluados if e > umbral_pp * 0.66) / len(evaluados) * 100
        print(
            f"  🟢 NO es bug: los edges existen pero nunca superan el umbral (max={_pp(mx)} ≤ "
            f"{umbral_pp}pp).\n     Mercado eficiente pre-match. {pct_cerca:.0f}% de los ciclos "
            f"quedaron a <1/3 del umbral.\n"
            "     Decisión de negocio: bajar MOTOR_2_MIN_EDGE_PCT (más señales, menos margen) o\n"
            "     aceptar pocas señales. OJO: el histórico con este umbral fue -EV hasta los fixes\n"
            "     de sizing/salida — más señales ≠ más ganancia."
        )
    elif mx <= techo_pp:
        print(
            f"  🔴 BUG PROBABLE: hubo edge sobre el umbral (max={_pp(mx)} > {umbral_pp}pp, bajo el\n"
            f"     techo {techo_pp}pp) y aun así señales=0 → el filtro post-edge (colapso por\n"
            "     evento / emit) está tirando señales válidas. Corré --live para reproducirlo."
        )
    else:
        print(
            f"  🟡 Edges ALTOS rechazados por anti-fantasma (max={_pp(mx)} > techo {techo_pp}pp).\n"
            "     Buscá `motor2.signal.rej_high` / `discarded_suspicious` en los logs: si es\n"
            "     constante, las odds están stale/in-play o el consenso está descalibrado."
        )
    return 0


# =====================================================
# Modo --live — una pasada real con dump por outcome
# =====================================================


async def run_live() -> int:
    from src.clients.kalshi_rest import KalshiRestClient
    from src.strategies.motor_2_consensus.detector import _net_edge_pct, find_signals
    from src.strategies.motor_2_consensus.sources import LiveOddsSource, RestKalshiQuoteSource

    s = get_settings()
    if not s.ODDS_API_KEY:
        print("ODDS_API_KEY no configurada — el modo --live necesita odds reales.")
        return 1
    min_edge = s.MOTOR_2_MIN_EDGE_PCT / 100.0
    max_edge = s.MOTOR_2_MAX_EDGE_PCT / 100.0
    series = [x.strip() for x in s.MOTOR2_SERIES.split(",") if x.strip()]
    sports = [x.strip() for x in s.ODDS_API_SPORT_KEYS.split(",") if x.strip()]

    # Universo: mismos eventos open que descubriría data_capture, sin depender del servicio.
    universe: dict[str, set[str]] = {}
    async with KalshiRestClient() as kc:
        for serie in series:
            try:
                resp = await kc.list_events(series_ticker=serie, status="open", limit=200)
            except Exception as e:
                print(f"  ⚠️ list_events({serie}): {type(e).__name__}: {e}")
                continue
            for ev in resp.get("events", []):
                if ev.get("event_ticker"):
                    universe[ev["event_ticker"]] = set()
            await asyncio.sleep(0.3)
    print(f"Universo Kalshi: {len(universe)} eventos open de {series}")

    kalshi_events = await RestKalshiQuoteSource(lambda: universe).fetch()
    odds_events = await LiveOddsSource(sports, regions=s.ODDS_API_REGIONS).fetch()

    diag: dict[str, float] = {}
    fair_out: dict[str, float] = {}
    signals = find_signals(
        kalshi_events,
        odds_events,
        capital_usd=s.ACTIVE_CAPITAL_USD,
        min_edge=min_edge,
        diag=diag,
        max_edge=max_edge,
        fair_out=fair_out,
    )

    print(f"\n{'=' * 68}\nDUMP POR OUTCOME (umbral={min_edge:.3f} techo={max_edge:.3f})")
    for ke in kalshi_events:
        con_fair = [q for q in ke.outcomes if q.market_ticker in fair_out]
        if not con_fair:
            continue  # evento no matcheado/pre-funnel — el porqué está en diag
        print(f"\n  {ke.event_key}")
        for q in ke.outcomes:
            fp = fair_out.get(q.market_ticker)
            if fp is None:
                print(f"    {q.market_ticker:<38} ⚠️ SIN FAIR (lookup de nombre no cruzó)")
                continue
            ye = _net_edge_pct(fp, q.yes_ask_cents)
            ne = _net_edge_pct(1.0 - fp, q.no_ask_cents)

            def v(e: float) -> str:
                if e <= min_edge:
                    return "bajo-umbral"
                if e > max_edge:
                    return "RECHAZO techo"
                return "→ SEÑAL"

            print(
                f"    {q.market_ticker:<38} fair={fp:.4f} "
                f"yes_ask={q.yes_ask_cents}c edge={ye:+.4f} [{v(ye)}] · "
                f"no_ask={q.no_ask_cents}c edge={ne:+.4f} [{v(ne)}]"
            )

    print(f"\n{'=' * 68}\nFUNNEL de la pasada: {{k: int(v) for k, v in diag.items()}}")
    for k, val in sorted(diag.items()):
        print(f"  {k} = {val:.4f}" if k == "best_net_edge" else f"  {k} = {int(val)}")
    print(f"\nSEÑALES EMITIDAS: {len(signals)}")
    for sig in signals:
        print(f"  {sig.market_ticker} {sig.kalshi_side} edge={sig.edge_pct:.4f}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Diagnóstico del funnel de señales de Motor 2.")
    p.add_argument("--last", type=int, default=200, help="Ciclos de DB a agregar (default 200).")
    p.add_argument("--live", action="store_true", help="Una pasada en vivo con dump por outcome.")
    args = p.parse_args()
    if args.live:
        raise SystemExit(asyncio.run(run_live()))
    raise SystemExit(run_db(args.last))


if __name__ == "__main__":
    main()
