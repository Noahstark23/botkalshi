"""ACK/GET cero ficticios no liberan SELL ajenos: executors reales + SQLite.

Sin red, cuenta ni runner completo. Reutiliza el laboratorio aislado de salidas;
las contradicciones se inyectan en la respuesta, no en el validador contable.
"""

from fractions import Fraction

import pytest

import src.storage.models as models
from src.monitoring.health import BotState
from src.storage.order_reconciliation import reconcile_pending_orders
from src.storage.reductions import persist_reduction
from tests.integration.test_async_exit_capture import Exchange, execute, ledger, seed
from tests.integration.test_async_exit_capture import lab as lab

MODES = [
    ("m1", "motor_1_arbitrage"),
    ("rest", "motor_rest_arb"),
    ("m3", "motor_2_consensus"),
]
CONTRADICTIONS = [
    ("ticker", "FOREIGN"),
    ("side", "no"),
    ("action", "buy"),
    ("client_order_id", "FOREIGN"),
    ("outcome_side", "yes"),
    ("book_side", "bid"),
    ("subaccount_number", 1),
    ("subaccount_number", False),
    ("subaccount_number", None),
    ("order_id", ""),
    ("order_id", True),
]


class IdentityExchange(Exchange):
    def __init__(self, change=None, *, legacy=False):
        super().__init__(sold=Fraction(0))
        self.change, self.legacy = change, legacy

    def order(self, coid):
        order = super().order(coid)
        if self.legacy:
            for key in (
                "ticker",
                "side",
                "action",
                "outcome_side",
                "book_side",
                "subaccount_number",
            ):
                del order[key]
        if self.change is not None:
            key, value = self.change
            order[key] = value
        return order


@pytest.mark.parametrize("mode,strategy", MODES)
@pytest.mark.parametrize("change", CONTRADICTIONS)
async def test_zero_ack_identity_conflict_keeps_pending_and_prevents_resell(mode, strategy, change):
    seed(strategy)
    client = IdentityExchange(change)
    outcome = await execute(mode, client)
    buy, sell = ledger()
    assert buy.status == "filled" and buy.pnl_usd_fp is None
    assert sell.status == "pending" and sell.sale_proceeds_usd_fp is None
    assert BotState.is_paused and models.kill_switch_engaged()[0]
    assert client.reads == [] and len(client.posts) == 1
    if mode == "m3":
        assert not outcome.filled and outcome.sell_price_cents is None
        assert outcome.reason == "order_identity_unconfirmed"
    await execute(mode, client)
    assert len(client.posts) == 1


@pytest.mark.parametrize("mode,strategy", MODES)
async def test_zero_ack_legacy_omissions_remain_compatible(mode, strategy):
    seed(strategy)
    client = IdentityExchange(legacy=True)
    await execute(mode, client)
    buy, sell = ledger()
    assert buy.status == "filled" and sell.status == "cancelled"
    assert client.reads == [] and len(client.posts) == 1


@pytest.mark.parametrize("strategy", ["motor_1_arbitrage", "motor_rest_arb"])
@pytest.mark.parametrize("change", [c for c in CONTRADICTIONS if c[0] != "client_order_id"])
async def test_shared_zero_get_identity_conflict_cannot_release_reduction(strategy, change):
    seed(strategy)
    persist_reduction("buy", "sell", 1, 60)
    client = IdentityExchange(change)
    client.orders = [client.order("sell")]
    result = await reconcile_pending_orders(client, strategies=(strategy,))
    assert result.resolved == 0 and result.unresolved == ("sell",)
    buy, sell = ledger()
    assert buy.status == "filled" and sell.status == "pending"
    assert BotState.is_paused and models.kill_switch_engaged()[0]
    assert client.reads == [] and client.posts == []


@pytest.mark.parametrize("strategy", ["motor_1_arbitrage", "motor_rest_arb"])
async def test_shared_zero_get_legacy_omissions_remain_compatible(strategy):
    seed(strategy)
    persist_reduction("buy", "sell", 1, 60)
    client = IdentityExchange(legacy=True)
    client.orders = [client.order("sell")]
    result = await reconcile_pending_orders(client, strategies=(strategy,))
    assert result.resolved == 1 and not result.unresolved
    buy, sell = ledger()
    assert buy.status == "filled" and sell.status == "cancelled"
    assert not BotState.is_paused and not models.kill_switch_engaged()[0]
    assert client.reads == [] and client.posts == []
