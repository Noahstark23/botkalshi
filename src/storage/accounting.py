"""Rol contable terminal explícito; ausencia legacy no demuestra PnL cero."""

from src.storage.models import Trade

REALIZED_PNL = "realized_pnl"
EXIT_ALLOCATED = "exit_allocated"
_EXIT_STRATEGIES = {
    "motor_1_arbitrage",
    "motor_2_consensus",
    "motor_3_clv",
    "motor_rest_arb",
}


def settled_pnl_cents(trade: Trade) -> int:
    """Aporte al PnL sin inferir el rol desde notes, action o un NULL aislado.

    Las ventas de salida guardan el PnL en las BUY cerradas; las ventas de M5
    son entradas y sí portan su propio PnL. Una marca contradictoria bloquea.
    """
    if trade.status != "settled":
        raise ValueError("UNKNOWN_SETTLEMENT_STATUS")
    if trade.accounting_role == REALIZED_PNL:
        if (
            trade.action not in ("buy", "sell")
            or (trade.action == "sell" and trade.strategy != "motor_5_mm")
            or type(trade.pnl_cents) is not int
        ):
            raise ValueError("UNKNOWN_SETTLEMENT_PNL")
        return trade.pnl_cents
    if trade.accounting_role == EXIT_ALLOCATED:
        if (
            trade.action != "sell"
            or trade.strategy not in _EXIT_STRATEGIES
            or trade.pnl_cents is not None
            or trade.pnl_usd_fp is not None
        ):
            raise ValueError("UNKNOWN_SETTLEMENT_ALLOCATION")
        return 0
    raise ValueError("UNKNOWN_SETTLEMENT_ROLE")


def stamp_accounting_role(trade: Trade, role: str) -> None:
    """Sólo escritores contables, dentro del commit del cierre; nunca backfill."""
    if trade.accounting_role not in (None, role):
        raise ValueError("CONTRADICTORY_ACCOUNTING_ROLE")
    trade.accounting_role = role
    settled_pnl_cents(trade)
