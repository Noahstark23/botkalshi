#!/usr/bin/env python3
"""
Auditoría READ-ONLY de rentabilidad: ¿qué motor gana, cuál pierde, y por qué?

Imprime, contra la DB real:
  1. Scoreboard por motor: n, win rate, expectancy por trade, profit factor,
     PnL total y fees reales — la respuesta directa a "¿por qué no gana?".
  2. Serie mensual por motor: ¿la curva cambió tras cada fix (fees 2026-07-01,
     salidas, flat sizing)? El histórico pre-fix tiene pnl SOBREESTIMADO (ver
     audit_perdidas_estructurales.py para el drag exacto).
  3. Buckets por precio de entrada (favorite-longshot: ¿<40c sigue sangrando?).
  4. Granularidad: distribución de contratos por trade y el sobrecosto del ceil
     del fee — trades de 1-5 contratos pagan un "impuesto de redondeo" que un
     edge de 3pp no sobrevive.

Uso (dentro del container, donde vive trades.db):
    python scripts/audit_rentabilidad.py
    python scripts/audit_rentabilidad.py --since 2026-07-01   # solo post fee-fix

Solo SELECT sobre trades. No escribe nada.
"""

from __future__ import annotations

import argparse
from datetime import datetime

from sqlmodel import col, select

from src.analytics.rentabilidad import (
    buckets_por_precio,
    granularidad_fee,
    pnl_mensual,
    resumen_por_motor,
    veredicto,
)
from src.storage.models import Trade, get_session


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--since", type=datetime.fromisoformat, default=None)
    args = ap.parse_args()

    with get_session() as s:
        stmt = select(Trade).where(col(Trade.status) == "settled")
        if args.since:
            stmt = stmt.where(col(Trade.placed_at) >= args.since)
        rows = list(s.exec(stmt))

    print(
        f"\n=== RENTABILIDAD ({len(rows)} filas settled"
        f"{f', desde {args.since:%Y-%m-%d}' if args.since else ''}) ===\n"
    )

    print("-- Por motor --")
    for _, m in sorted(resumen_por_motor(rows).items(), key=lambda kv: kv[1].pnl_cents):
        print(
            f"  {m.strategy:22s} n={m.n:4d}  win={m.win_pct:5.1f}%  "
            f"exp={m.expectancy_cents:+7.1f}c/trade  PF={m.profit_factor:5.2f}  "
            f"pnl=${m.pnl_cents / 100:+9.2f}  fees_reales=${m.fees_reales_cents / 100:8.2f}"
        )

    print("\n-- Mensual (pnl $ por motor; pre 2026-07 el pnl registrado está inflado) --")
    for mes, por_estrategia in pnl_mensual(rows).items():
        detalle = "  ".join(f"{k}={v / 100:+.2f}" for k, v in sorted(por_estrategia.items()))
        print(f"  {mes}: {detalle}")

    print("\n-- Por precio de entrada --")
    for b in buckets_por_precio(rows):
        if b.n:
            print(
                f"  {b.label:8s} n={b.n:4d}  win={b.win_pct:5.1f}%  pnl=${b.pnl_cents / 100:+9.2f}"
            )

    print("\n-- Granularidad (contratos por trade y sobrecosto del ceil del fee) --")
    for g in granularidad_fee(rows):
        if g.n:
            print(
                f"  {g.label:8s} n={g.n:4d}  pnl=${g.pnl_cents / 100:+9.2f}  "
                f"sobrecosto_redondeo=${g.sobrecosto_redondeo_cents / 100:+7.2f}"
            )

    print("\n-- Veredicto --")
    for linea in veredicto(rows).lineas:
        print(f"  * {linea}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
