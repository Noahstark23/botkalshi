"""
Reporte read-only de EdgeWindows del Motor REST (binary + multi_outcome).

POR QUÉ EXISTE (Fase 2 del go-live, camino Motor REST): el Analyst Loop valida
Motor 2 (kind='consensus') con su embudo de sportsbook, pero NO mira las ventanas
del Motor REST (kind binary/multi_outcome). Este reporte cubre ese hueco: cuántos
edges netos post-fee se acumularon, su distribución, y —lo decisivo— cuántos
CRUZARÍAN el umbral de ejecución (MOTOR_REST_EXECUTION_EDGE_PCT) si TRADING_ENABLED.
Responde la pregunta de Fase 2: ¿el shadow captura edges reales y ejecutables?

UNIDADES (auditado contra engine.py:256 y _record_edge_window): EdgeWindow.edge_pct
del Motor REST está en las MISMAS unidades que MOTOR_REST_EXECUTION_EDGE_PCT — el
engine compara `opp.edge_pct < umbral` directo. NO se multiplica por 100 (ese ×100
es solo para kind='consensus', que guarda fracción). magnitude_cents = edge NETO
post-fee en centavos. Las ventanas pre-P3 (kind NULL) quedan fuera a propósito.

Read-only: solo lee la tabla edge_windows. NO toca capital ni red a Kalshi.

Uso (dentro del container Coolify):
    python scripts/report_edge_windows.py
    python scripts/report_edge_windows.py --hours 168              # últimos 7 días
    python scripts/report_edge_windows.py --breakdown             # triage de ejecutables
"""

from __future__ import annotations

import argparse
import statistics
from datetime import UTC, datetime, timedelta

from sqlmodel import col, select

from src.storage.models import EdgeWindow, get_session
from src.utils.config import get_settings

_REST_KINDS = ("binary", "multi_outcome")


def _fmt(vals: list[float], unit: str = "") -> str:
    """Resumen estadístico de una lista (— si vacía)."""
    if not vals:
        return "—"
    return (
        f"n={len(vals)} min={min(vals):.2f}{unit} med={statistics.median(vals):.2f}{unit} "
        f"media={statistics.fmean(vals):.2f}{unit} max={max(vals):.2f}{unit}"
    )


def _breakdown(exec_rows: list[EdgeWindow], exec_thresh: float) -> None:
    """Triage de los ejecutables: banda de edge (fantasmas) + concentración por evento.

    La sospecha de fantasma crece con el edge — un 1X2 real (3 outcomes ~100¢) rara vez
    pasa de pocos %. Un edge alto implica patas que NO suman ~100 (stale / equipo
    eliminado a ~0¢ / mercado por resolver). El desglose por evento revela si los
    ejecutables están repartidos o concentrados en un puñado de partidos.
    """
    print(f"\n{'-' * 64}\nDESGLOSE DE EJECUTABLES (edge_pct ≥ {exec_thresh}%), n={len(exec_rows)}")

    bands = [
        (exec_thresh, 3.0, "arb plausible"),
        (3.0, 10.0, "revisar"),
        (10.0, 50.0, "⚠️  sospechoso"),
        (50.0, float("inf"), "🚩 casi seguro fantasma (pata ~0¢ / mercado por resolver)"),
    ]
    print("\n  Por banda de edge_pct:")
    for lo, hi, label in bands:
        n = sum(1 for r in exec_rows if lo <= r.edge_pct < hi)
        rng = f">{lo:.0f}%" if hi == float("inf") else f"{lo:.1f}–{hi:.0f}%"
        print(f"    {rng:<9}: {n:>5}  {label}")

    by_event: dict[str, list[float]] = {}
    for r in exec_rows:
        by_event.setdefault(r.market_ticker, []).append(r.edge_pct)
    print(f"\n  Eventos distintos con ≥1 ejecutable: {len(by_event)}")
    print("  Top 10 por nº de ventanas ejecutables (concentración):")
    print(f"    {'evento':<34} {'windows':>7} {'best%':>7} {'med%':>7}")
    top = sorted(by_event.items(), key=lambda kv: len(kv[1]), reverse=True)[:10]
    for ev, edges in top:
        print(f"    {ev[:34]:<34} {len(edges):>7} {max(edges):>6.1f} {statistics.median(edges):>6.1f}")


def report(hours: int | None, breakdown: bool) -> int:
    """Imprime el reporte. Exit 0 siempre (diagnóstico)."""
    s = get_settings()
    exec_thresh = s.MOTOR_REST_EXECUTION_EDGE_PCT

    with get_session() as session:
        stmt = select(EdgeWindow).where(col(EdgeWindow.kind).in_(_REST_KINDS))
        if hours is not None:
            since = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=hours)
            stmt = stmt.where(col(EdgeWindow.created_at) >= since)
        rows = list(session.exec(stmt))

    window_desc = f"últimas {hours}h" if hours is not None else "histórico completo"
    print(f"\n{'=' * 64}\nMotor REST — EdgeWindows ({window_desc})")

    if not rows:
        print("\n  (sin EdgeWindows en la ventana)")
        print(f"\n{'=' * 64}\nVEREDICTO")
        print("  🟡 El shadow del Motor REST aún NO capturó ningún edge.")
        print("     Esperar volumen — los arbs 1X2 aparecen cerca de los kickoffs.")
        print("     Re-correr este reporte tras una ventana de partidos.")
        return 0

    by_kind = {k: 0 for k in _REST_KINDS}
    for r in rows:
        by_kind[r.kind] = by_kind.get(r.kind, 0) + 1
    times = [r.created_at for r in rows if r.created_at]
    span = f"{min(times)} → {max(times)}" if times else "—"

    edge_pcts = [r.edge_pct for r in rows if r.edge_pct is not None]
    net_cents = [float(r.magnitude_cents) for r in rows if r.magnitude_cents is not None]
    gross_cents = [float(r.gross_spread_cents) for r in rows if r.gross_spread_cents is not None]
    fees = [float(r.fees_cents) for r in rows if r.fees_cents is not None]
    counts = [float(r.count) for r in rows if r.count is not None]
    exec_rows = [r for r in rows if r.edge_pct is not None and r.edge_pct >= exec_thresh]
    would_execute = [r.edge_pct for r in exec_rows]

    print(f"  total            : {len(rows)}  ({by_kind})")
    print(f"  ventana temporal : {span}")
    print(f"  edge_pct (%)     : {_fmt(edge_pcts, '%')}")
    print(f"  edge neto (¢)    : {_fmt(net_cents, 'c')}")
    print(f"  spread bruto (¢) : {_fmt(gross_cents, 'c')}")
    print(f"  fees (¢)         : {_fmt(fees, 'c')}")
    print(f"  contratos/count  : {_fmt(counts)}")
    if gross_cents and net_cents:
        gmean, nmean = statistics.fmean(gross_cents), statistics.fmean(net_cents)
        bite = (1 - nmean / gmean) * 100 if gmean else 0.0
        print(f"  mordida de fees  : bruto≈{gmean:.2f}c → neto≈{nmean:.2f}c "
              f"({bite:.0f}% se lo comen las fees)")

    # Estados post-trade: en shadow deben estar todos en default (jamás se ejecutó).
    executed = [r for r in rows if r.leg_states]
    if executed:
        print(f"  ⚠️  {len(executed)} ventanas con leg_states poblado (¿ejecución real?) — revisar.")

    print(f"\n  Umbral de EJECUCIÓN: edge_pct ≥ {exec_thresh}% (MOTOR_REST_EXECUTION_EDGE_PCT)")
    print(f"  → EJECUTABLES (cruzarían el umbral): {len(would_execute)} de {len(edge_pcts)}")
    if would_execute:
        print(f"     best ejecutable: {max(would_execute):.2f}%")

    print(f"\n{'=' * 64}\nVEREDICTO")
    if not would_execute:
        print(f"  🟠 Captura edges pero NINGUNO cruza el umbral de ejecución ({exec_thresh}%).")
        print("     Las fees se comen el spread / mercado eficiente — el canary no dispararía hoy.")
        print("     Útil igual: confirma que el detector vive. Seguir acumulando.")
    else:
        print(f"  🟢 {len(would_execute)} edges cruzan el umbral de edge.")
        print("     OJO: es una COTA SUPERIOR — 'cruzó el umbral' ≠ 'ejecutaría'. Los")
        print("     guardarraíles de tiempo-de-ejecución (quote stale, mercado resuelto,")
        print("     check_pre_trade) podrían rechazar parte. Y la COLA alta (edge_pct grande)")
        print("     es casi seguro fantasma (pata ~0¢ / equipo eliminado), no arb real.")
        print("     ANTES de encender: correr con --breakdown (descartar fantasmas y")
        print("     concentración), revisar consistencia varios días, y cerrar el void.")
        if breakdown:
            _breakdown(exec_rows, exec_thresh)
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Reporte read-only de EdgeWindows del Motor REST.")
    p.add_argument(
        "--hours", type=int, default=None, help="Ventana en horas (default: histórico completo)."
    )
    p.add_argument(
        "--breakdown",
        action="store_true",
        help="Desglosar ejecutables por banda de edge (fantasmas) y por evento (concentración).",
    )
    args = p.parse_args()
    raise SystemExit(report(args.hours, args.breakdown))


if __name__ == "__main__":
    main()
