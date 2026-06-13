#!/usr/bin/env python3
"""
Reporte de las señales de consenso (Motor 2) acumuladas en shadow.

Para la decisión "¿enciendo capital?": en vez de mirar 1 fila a ojo, agrega TODAS las
EdgeWindow(kind="consensus") y muestra cuántas superan el umbral, su edge medio, la
distribución y los tickers — con el desglose gross/fee/neto que ahora se persiste.

Uso (en el container de prod, donde vive trades.db):
    python scripts/motor2_consensus_report.py            # umbral 3.0pp (default)
    python scripts/motor2_consensus_report.py --min 4.0  # subí el corte
    python scripts/motor2_consensus_report.py --hours 48 # solo últimas 48h

Read-only: solo SELECT, no toca capital ni escribe nada.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta

from sqlmodel import select

from src.storage.models import EdgeWindow, get_session


def main() -> None:
    ap = argparse.ArgumentParser(description="Reporte de señales consensus de Motor 2")
    ap.add_argument(
        "--min", type=float, default=3.0, help="Umbral de edge neto en pp (default 3.0)"
    )
    ap.add_argument("--hours", type=int, default=None, help="Mirar solo las últimas N horas")
    args = ap.parse_args()
    min_frac = args.min / 100.0

    with get_session() as s:
        rows = list(s.exec(select(EdgeWindow).where(EdgeWindow.kind == "consensus")).all())

    if args.hours is not None:
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=args.hours)
        rows = [r for r in rows if r.created_at and r.created_at >= cutoff]

    total = len(rows)
    above = [r for r in rows if (r.edge_pct or 0) > min_frac]
    print(
        f"=== Motor 2 consensus — corte {args.min:.1f}pp"
        + (f", últimas {args.hours}h" if args.hours else "")
        + " ==="
    )
    print(f"filas consensus totales : {total}")
    print(f"filas > {args.min:.1f}pp      : {len(above)}")
    if not above:
        print("\nSin señales sobre el umbral todavía. (N bajo / edges marginales → NO encender.)")
        return

    edges = sorted((r.edge_pct or 0) * 100 for r in above)
    mean = sum(edges) / len(edges)
    print(f"edge medio (>umbral)    : {mean:.2f}pp")
    print(f"edge min / max          : {edges[0]:.2f}pp / {edges[-1]:.2f}pp")
    # ¿Cuántas son MARGINALES (pegadas al borde, < umbral+0.5pp)? Señal de baja calidad.
    marginal = sum(1 for e in edges if e < args.min + 0.5)
    print(f"marginales (<{args.min + 0.5:.1f}pp)    : {marginal}/{len(above)}")

    print("\nticker                                        side  gross  fee  neto   edge_pp  ts")
    print("-" * 92)
    for r in sorted(above, key=lambda x: x.created_at or datetime.min, reverse=True)[:40]:
        gross = "?" if r.gross_spread_cents is None else f"{r.gross_spread_cents}c"
        fee = "?" if r.fees_cents is None else f"{r.fees_cents}c"
        ts = r.created_at.strftime("%m-%d %H:%M") if r.created_at else "?"
        print(
            f"{r.market_ticker:<45} {'':4} {gross:>5} {fee:>4} "
            f"{r.magnitude_cents:>4}c {(r.edge_pct or 0) * 100:>7.2f}  {ts}"
        )


if __name__ == "__main__":
    main()
