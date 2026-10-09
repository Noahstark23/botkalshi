"""Respuestas GET sintéticas explícitas para regresiones de executors anteriores.

No lee el ledger ni reemplaza el collector. Cada escenario suministra cantidad,
lado, precio y fee independientes; paginación y parser reales siguen ejecutándose.
"""

from collections import deque
from fractions import Fraction
from unittest.mock import AsyncMock


def wire_exit_fills(client, observations):
    """Observaciones ordenadas: (side, count, price_cents, fee_cents)."""
    pending = deque(observations)

    async def get_fills(*, order_id, ticker, subaccount, limit, **_):
        assert subaccount == 0 and limit == 100
        side, count, price, fee = pending.popleft()
        units = Fraction(count) * 100
        assert units.denominator == 1
        return {
            "fills": [
                {
                    "fill_id": f"fixture-{order_id}",
                    "order_id": order_id,
                    "ticker": ticker,
                    "side": side,
                    "action": "sell",
                    "subaccount_number": 0,
                    "count_fp": f"{int(units) // 100}.{int(units) % 100:02d}",
                    f"{side}_price_dollars": f"{price // 100}.{price % 100:02d}",
                    "fee_cost": f"{fee // 100}.{fee % 100:02d}",
                }
            ],
            "cursor": "",
        }

    client.get_fills = AsyncMock(side_effect=get_fills)
    client.get_historical_fills = AsyncMock(return_value={"fills": [], "cursor": ""})
