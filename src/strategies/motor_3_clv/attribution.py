"""Una sola atribución de BUYs para reservas, cierre y fuentes de permisos.

No acredita cartera completa, órdenes terminales ni permisos financieros.
"""

from fractions import Fraction

from sqlmodel import Session, col, select

from src.storage.models import Trade
from src.storage.quantities import open_quantity
from src.storage.read_scope import financial_read_scope
from src.strategies.motor_3_clv.orphans import motor1_orphan_buys


def attributable_buys(
    s: Session,
    ticker: str,
    side: str,
    *,
    entry_origin: tuple[str, ...],
    include_motor1_orphans: bool,
) -> list[Trade]:
    """Misma política del executor: origins explícitos, huérfanas M1 opcionales, FIFO.

    El caller posee la transacción y sus escrituras. Este selector también se usa
    durante el cierre, por eso no impone un scope exclusivamente de lectura a
    callers que ya modificaron la SELL. Para gates usar attributable_quantity.
    Una cantidad M1 desconocida en cualquiera de los lados impide determinar
    orfandad: se rechaza el ticker antes de poder retirar una pata de un hedge.
    """
    buys = [
        b
        for b in s.exec(
            select(Trade).where(
                Trade.ticker == ticker,
                Trade.side == side,
                Trade.action == "buy",
                Trade.status == "filled",
                col(Trade.strategy).in_(entry_origin),
            )
        )
        if not b.closed_by_clv and open_quantity(b) > 0
    ]
    if include_motor1_orphans:
        m1_all_sides = [
            b
            for b in s.exec(
                select(Trade).where(
                    Trade.ticker == ticker,
                    Trade.action == "buy",
                    Trade.status == "filled",
                    Trade.strategy == "motor_1_arbitrage",
                )
            )
            if not b.closed_by_clv and open_quantity(b) > 0
        ]
        buys.extend(o for o in motor1_orphan_buys(m1_all_sides) if o.side == side)
    buys.sort(key=lambda b: b.placed_at)
    return buys


def attributable_quantity(
    session: Session,
    ticker: str,
    side: str,
    *,
    entry_origin: tuple[str, ...],
    include_motor1_orphans: bool,
) -> Fraction:
    """Cantidad local atribuible en la Session prestada, sin flush/commit/red.

    Un fill desconocido se propaga como error; jamás se transforma en cero.
    Cero sólo significa ausencia de inventario local seleccionado por la política,
    no cuenta vacía ni ausencia de pending/resting/órdenes históricas. El caller
    debe mantener el lock y validar esas fuentes por separado antes de autorizar.
    """
    with financial_read_scope(session):
        return sum(
            (
                open_quantity(row)
                for row in attributable_buys(
                    session,
                    ticker,
                    side,
                    entry_origin=entry_origin,
                    include_motor1_orphans=include_motor1_orphans,
                )
            ),
            Fraction(0),
        )
