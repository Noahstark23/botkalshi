#!/usr/bin/env python3
"""
Auditoría READ-ONLY de pérdidas estructurales ("es imposible que hayamos perdido").

Cuantifica contra la DB real los 3 mecanismos de pérdida POR CONSTRUCCIÓN
(ver src/analytics/loss_audit.py):
  1. Fee drag oculto por motor: cuánto MEJOR se ve el PnL de la DB que la realidad
     (fees pre-fix registradas ~100× de menos; Kalshi cobró la real).
  2. Motor 1: cuántos arbs ejecutados eran PERDEDORES DETERMINÍSTICOS al colocarse
     (gross < fees reales) — la firma del fee bug.
  3. Motor 2: la tabla de buckets de edge con PnL corregido por fees reales.

Uso (dentro del container):
    python scripts/audit_perdidas_estructurales.py
    python scripts/audit_perdidas_estructurales.py --since 2026-06-01

Solo SELECT sobre trades. No escribe nada.
"""

from __future__ import annotations

import argparse
from datetime import datetime

from sqlmodel import col, select

from src.analytics.loss_audit import (
    audit_motor1_arbs,
    fee_drag_by_strategy,
    motor2_buckets,
)
from src.analytics.shadow_fee_recalc import FEE_FIX_AT_DEFAULT
from src.storage.models import Trade, get_session


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--since", type=datetime.fromisoformat, default=None)
    ap.add_argument("--fee-fix-at", type=datetime.fromisoformat, default=FEE_FIX_AT_DEFAULT)
    args = ap.parse_args()

    stmt = select(Trade)
    if args.since is not None:
        stmt = stmt.where(col(Trade.placed_at) >= args.since)
    with get_session() as s:
        rows = list(s.exec(stmt))
    print(f"trades analizados: {len(rows)} (fee-fix: {args.fee_fix_at.isoformat()})")

    print("\n== 1. FEE DRAG OCULTO (filas pre-fix: DB mejor que la realidad) ==")
    drag = fee_drag_by_strategy(rows, fee_fix_at=args.fee_fix_at)
    total_drag = 0
    for strategy, agg in sorted(drag.items()):
        total_drag += agg.drag_cents
        print(
            f"  {strategy:22s} filas={agg.rows:4d}  fees_registradas=${agg.recorded_fees_cents / 100:8.2f}"
            f"  fees_reales=${agg.real_fees_cents / 100:8.2f}  drag=${agg.drag_cents / 100:+8.2f}"
        )
    print(f"  {'TOTAL':22s} el PnL de la DB está ${total_drag / 100:+.2f} MEJOR que la realidad")

    print("\n== 2. MOTOR 1 — arbs perdedores determinísticos (gross < fees reales) ==")
    arb = audit_motor1_arbs(rows)
    print(
        f"  pares hedged={arb.groups}  contratos={arb.paired_contracts}  "
        f"gross=${arb.gross_cents / 100:.2f}  fees_reales=${arb.real_fees_cents / 100:.2f}  "
        f"net_real=${arb.net_real_cents / 100:+.2f}"
    )
    print(
        f"  PERDEDORES AL COLOCARSE: {arb.deterministic_losers}/{arb.groups} "
        "(ejecutados como 'ganancia garantizada' por el fee bug)"
    )

    print("\n== 3. MOTOR 2 — buckets de edge, PnL registrado vs con fees reales ==")
    print(f"  {'bucket':8s} {'n':>4s} {'win%':>6s} {'pnl_db':>10s} {'pnl_real':>10s}")
    for b in motor2_buckets(rows, fee_fix_at=args.fee_fix_at):
        print(
            f"  {b.label:8s} {b.n:4d} {b.win_pct:5.1f}% "
            f"${b.pnl_recorded_cents / 100:+9.2f} ${b.pnl_real_cents / 100:+9.2f}"
        )
    print(
        "\nNOTA (cotas honestas): el drag cubre la fee de ENTRADA; salidas/settlements"
        "\npre-fix agregan drag adicional no desglosado por fila → los números reales son"
        "\nAL MENOS así de malos, no mejores."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
