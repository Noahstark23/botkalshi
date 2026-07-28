"""
Backfill de `trades.fees_cents` para filas settleadas que quedaron en NULL.

POR QUÉ EXISTE (incidente 2026-07-28): `/stats/motors?days=30` reportaba
`fees_usd: 0.00` para motor_1_arbitrage sobre 519 trades settleados. No es que el
motor no pagara comisión: el fee se calculaba, se descontaba del PnL y se tiraba
sin registrar — `_persist_intents` de M1 no lo guardaba (M2 y REST sí) y el
SettlementPoller usaba el fallback recomputado sin persistirlo. Efecto: cualquier
criterio del tipo "PnL/trade > 2 × fee promedio" comparaba contra CERO y aprobaba
al motor automáticamente — justo el criterio de cierre del mes de prueba.

El código ya está arreglado hacia adelante. Este script cubre lo viejo.

QUÉ ESCRIBE: exactamente el mismo valor que el settlement ya usó como fallback
(`kalshi_fee_cents(count, fill_price or price)`), así que **el PnL registrado no
cambia** — solo deja de ser invisible el costo que ya estaba descontado.

SEGURIDAD:
  - DRY-RUN POR DEFECTO. Escribe solo con `--apply`.
  - Nunca pisa un `fees_cents` existente (WHERE fees_cents IS NULL).
  - Nunca toca `pnl_cents`, `status` ni `settled_at`.
  - Hacé backup con la API `.backup()` de SQLite antes de `--apply` (regla del
    pre-flight del vault; `cp` crudo sobre una DB en WAL no sirve).

Uso:
    python -m scripts.backfill_trade_fees                 # dry-run, muestra el plan
    python -m scripts.backfill_trade_fees --strategy motor_1_arbitrage
    python -m scripts.backfill_trade_fees --apply         # escribe
"""

from __future__ import annotations

import argparse
from collections import defaultdict

from sqlmodel import col, select

from src.math.fees import kalshi_fee_cents
from src.storage.models import Trade, get_session


def _fee_for(trade: Trade) -> int:
    """El MISMO cálculo que usó el settlement como fallback (settlement.py)."""
    price = trade.fill_price_cents or trade.price_cents
    count = trade.filled_count if trade.filled_count is not None else trade.count
    if trade.action == "sell":
        price = 100 - price
    return kalshi_fee_cents(count, price)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="escribe (sin esto: dry-run)")
    ap.add_argument("--strategy", default=None, help="limitar a un motor")
    args = ap.parse_args()

    with get_session() as s:
        stmt = select(Trade).where(
            col(Trade.settled_at).is_not(None),
            col(Trade.fees_cents).is_(None),
        )
        if args.strategy:
            stmt = stmt.where(Trade.strategy == args.strategy)
        rows = list(s.exec(stmt))

        if not rows:
            print("Nada que hacer: no hay trades settleados con fees_cents NULL.")
            return 0

        per_motor: dict[str, list[int]] = defaultdict(list)
        for t in rows:
            fee = _fee_for(t)
            per_motor[t.strategy or "?"].append(fee)
            if args.apply:
                t.fees_cents = fee
                s.add(t)

        print(f"{'APLICANDO' if args.apply else 'DRY-RUN'} — {len(rows)} filas\n")
        print(f"{'motor':<24} {'trades':>7} {'fees_usd':>10} {'fee/trade':>10}")
        for motor, fees in sorted(per_motor.items()):
            total = sum(fees) / 100
            print(f"{motor:<24} {len(fees):>7} {total:>10.2f} {total / len(fees):>10.4f}")

        if args.apply:
            s.commit()
            print("\nOK: fees_cents escrito. El PnL registrado NO cambió.")
        else:
            print("\nDry-run: nada escrito. Repetir con --apply (después del backup).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
