"""
Calibración del umbral de take-profit (Motor 3, FASE 1) — SNAPSHOT aproximado.

Cruza las posiciones abiertas (portfolio_positions) con el ÚLTIMO market_snapshot de cada
ticker y, para varios umbrales (90/85/80¢), cuenta cuántas cerraría el take-profit y estima
el PnL realizado de esos cierres usando el entry real (trades.fill_price_cents, pata BUY
filled, FIFO — igual criterio que Motor3ExitExecutor._settle_originals).

⚠️ APROXIMADO, NO es la fuente de verdad. `market_snapshots` se captura cada ~5 min; Motor 3
evalúa el take-profit contra el ORDERBOOK LIVE (engine._current_bid → get_orderbook), no contra
este snapshot. Sirve para una primera intuición del umbral. **La calibración REAL = acumular y
analizar las líneas `[MOTOR 3 TP SHADOW]` de los logs** (esas sí reflejan el bid live que
disparó la lógica). Read-only: NO escribe nada.

Uso:
    python scripts/calibrar_take_profit.py
"""

from __future__ import annotations

from sqlmodel import col, select

from src.storage.models import MarketSnapshot, PortfolioPosition, Trade, get_session

# Mismas estrategias de origen que _settle_originals: las patas BUY que un exit cerraría.
ORIGIN = ("motor_2_consensus", "motor_rest_arb")
THRESHOLDS = (90, 85, 80)


def _entry_bid_cents(s, ticker: str, side: str) -> int | None:
    """Entry real = primer BUY filled (FIFO por placed_at) del mismo ticker+side. None si no hay."""
    buy = s.exec(
        select(Trade)
        .where(
            Trade.ticker == ticker,
            Trade.side == side,
            Trade.action == "buy",
            Trade.status == "filled",
            col(Trade.strategy).in_(ORIGIN),
        )
        .order_by(col(Trade.placed_at))
    ).first()
    if buy is None:
        return None
    return buy.fill_price_cents or buy.price_cents


def _latest_bid_cents(s, ticker: str, side: str) -> int | None:
    """Bid del lado abierto en el ÚLTIMO snapshot del ticker (yes_bid/no_bid). None si no hay."""
    snap = s.exec(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == ticker)
        .order_by(col(MarketSnapshot.captured_at).desc())
    ).first()
    if snap is None:
        return None
    return snap.yes_bid if side == "yes" else snap.no_bid


def run() -> int:
    rows: list[tuple[str, str, int, int | None, int | None]] = []
    with get_session() as s:
        positions = list(s.exec(select(PortfolioPosition)))
        for p in positions:
            bid = _latest_bid_cents(s, p.ticker, p.side)
            entry = _entry_bid_cents(s, p.ticker, p.side)
            rows.append((p.ticker, p.side, p.count, bid, entry))

    print(f"Posiciones abiertas: {len(rows)}\n")
    print(f"{'ticker':<32} {'side':<4} {'count':>6} {'bid':>5} {'entry':>6} {'pnl_si_sale':>12}")
    print("-" * 72)
    for ticker, side, count, bid, entry in rows:
        pnl = (
            f"${count * (bid - entry) / 100:>10.2f}"
            if bid is not None and entry is not None
            else f"{'—':>11}"
        )
        bid_s = str(bid) if bid is not None else "—"
        entry_s = str(entry) if entry is not None else "—"
        print(f"{ticker:<32} {side:<4} {count:>6} {bid_s:>5} {entry_s:>6} {pnl:>12}")

    print(f"\n{'umbral':>6} {'cerraría':>9} {'pnl_estimado':>14}")
    print("-" * 32)
    for thr in THRESHOLDS:
        hit = [(c, b, e) for _, _, c, b, e in rows if b is not None and b >= thr]
        pnl_cents = sum(c * (b - e) for c, b, e in hit if e is not None)
        print(f"{thr:>5}c {len(hit):>9} {f'${pnl_cents / 100:.2f}':>14}")

    print(
        "\n⚠️  Snapshot aproximado (market_snapshots ~cada 5min). La calibración REAL sale de "
        "acumular las líneas [MOTOR 3 TP SHADOW] de los logs (bid live)."
    )
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
