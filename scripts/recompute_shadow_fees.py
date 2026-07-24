#!/usr/bin/env python3
"""
Re-cálculo READ-ONLY del shadow histórico con la fee corregida (fix 0dbf9b7, 2026-07-01).

Pregunta que responde (gate de canary): ¿cuánto del edge/arb grabado en EdgeWindow
ANTES del fix de fees sobrevive con la comisión real? Las ventanas viejas no grabaron
precios por pata, así que el resultado son COTAS (ver src/analytics/shadow_fee_recalc.py):
el veredicto por fila usa la cota conservadora (fee máxima compatible con lo grabado).

Uso (dentro del container / VPS, misma DB que el bot):
    python scripts/recompute_shadow_fees.py
    python scripts/recompute_shadow_fees.py --since 2026-06-01 --min-edge-pp 3.0
    python scripts/recompute_shadow_fees.py --fee-fix-at 2026-07-01T21:02:37

No escribe nada: solo SELECT sobre edge_windows + reporte a stdout.
"""

from __future__ import annotations

import argparse
from datetime import datetime

from sqlmodel import col, select

from src.analytics.shadow_fee_recalc import (
    FEE_FIX_AT_DEFAULT,
    ArbRecalc,
    recompute_shadow,
)
from src.storage.models import EdgeWindow, get_session


def _print_arb(label: str, a: ArbRecalc) -> None:
    print(f"\n[{label}] post_fix={a.post_fix} (fee correcta, no se tocan)  pre_fix={a.pre_fix}")
    if not a.pre_fix:
        return
    print(f"  grabadas positivas:            {a.pre_recorded_positive}")
    print(f"  siguen positivas (conservador): {a.pre_still_positive_conservative}")
    print(f"  FANTASMA probable (≤0 c/fee max): {a.pre_phantom_conservative}")
    print(f"  irrecuperables (sin gross/count): {a.pre_uncorrectable}")
    print(
        f"  neto grabado (solo corregibles): {a.pre_recorded_net_correctable_cents}c  →  "
        f"corregido conservador: {a.pre_corrected_net_conservative_total_cents}c"
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--since", type=datetime.fromisoformat, default=None)
    ap.add_argument("--until", type=datetime.fromisoformat, default=None)
    ap.add_argument(
        "--fee-fix-at",
        type=datetime.fromisoformat,
        default=FEE_FIX_AT_DEFAULT,
        help=f"cutover del deploy del fix (naive UTC; default {FEE_FIX_AT_DEFAULT.isoformat()})",
    )
    ap.add_argument("--min-edge-pp", type=float, default=3.0, help="umbral de señal consensus (pp)")
    args = ap.parse_args()

    stmt = select(EdgeWindow)
    if args.since is not None:
        stmt = stmt.where(col(EdgeWindow.created_at) >= args.since)
    if args.until is not None:
        stmt = stmt.where(col(EdgeWindow.created_at) <= args.until)
    with get_session() as s:
        rows = list(s.exec(stmt))

    r = recompute_shadow(rows, fee_fix_at=args.fee_fix_at, min_edge_pp=args.min_edge_pp)

    print(f"edge_windows analizadas: {len(rows)} (cutover fee-fix: {args.fee_fix_at.isoformat()})")
    c = r.consensus
    print(
        f"\n[consensus] post_fix={c.post_fix} (inconsistentes={c.inconsistent_post})  "
        f"pre_fix={c.pre_fix}"
    )
    if c.pre_fix:
        print(
            f"  > {args.min_edge_pp}pp como se grabaron: {c.pre_kept_optimistic}  →  "
            f"con fee corregida (conservador): {c.pre_kept_conservative}"
        )
        print(
            f"  edge medio grabado: {c.pre_mean_recorded_pp:.2f}pp  →  "
            f"conservador: {c.pre_mean_conservative_pp:.2f}pp"
        )
    _print_arb("binary", r.binary)
    _print_arb("multi_outcome", r.multi_outcome)
    if r.skipped_other_kind:
        print(f"\nkind desconocido (saltadas): {r.skipped_other_kind}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
