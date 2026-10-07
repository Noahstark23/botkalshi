"""Contrato de reserva para tests de executor que sustituyen el RiskManager."""

from unittest.mock import AsyncMock

from src.risk.manager import TradeDecision


def wire_reservation_fake(risk):
    risk.m1_reservation_limits.return_value = {
        "max_open_cents": 10_000_000,
        "event_cap_cents": 10_000_000,
    }

    async def reserve(opportunity, persist):
        decision = await risk.check_pre_trade(opportunity)
        if decision.approved and persist(decision):
            return decision
        return TradeDecision(False, "reserva rechazada", 0)

    risk.check_and_reserve = AsyncMock(side_effect=reserve)
