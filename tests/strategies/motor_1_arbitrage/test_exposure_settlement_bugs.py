"""
Bugs verificados en producción 2026-07-02 (auditoría del operador, evidencia en DB):

Un par de arbitraje hedged de Motor 1 (243×243 contratos, evento del 30-jun, riesgo
neto ~$0) copaba $235 de los $253 del cap de exposición simultánea y estrangulaba a
Motor 2 (headroom $0.02 → 0 contratos). Dos causas raíz independientes:

  BUG A — productor/consumidor: el RiskManager netea arbs hedged SOLO si la fila trae
  'arb_id=' en notes (manager.py); el executor de Motor 1 escribía notes=None → el
  hedge contaba su notional BRUTO completo como exposición direccional.

  BUG B — Motor 1 no estaba en SettlementPoller.STRATEGIES: sus filas filled JAMÁS
  entraban a la query de settlement → un evento resuelto el 30-jun seguía 'filled'
  (= exposición permanente) dos días después.

Con B arreglado, las filas stale se settlean solas al primer tick (la resolución es
por ticker vía get_market — no necesita kalshi_order_id). Con A, los hedges nuevos se
netean en vivo aunque el mercado tarde en resolver.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlmodel import select

from src.math.arbitrage import detect_binary_arb
from src.storage.models import Trade, get_session
from src.strategies.motor_rest_arb.settlement import (
    MarketResolution,
    SettlementPoller,
    arb_group_key,
)


@pytest.fixture(autouse=True)
def _db(tmp_db_engine):
    """Todos estos tests tocan la tabla Trade → SQLite temporal (fixture del paquete)."""
    yield


class _FakeSource:
    """Resolución fija por ticker (None = aún no resuelve)."""

    def __init__(self, resolutions: dict[str, str | None]):
        self._res = resolutions

    async def get_resolution(self, ticker: str):
        r = self._res.get(ticker)
        return MarketResolution(ticker=ticker, result=r) if r else None


_UUID = "12345678-1234-4123-8123-123456789abc"  # arb_id con forma uuid4 (regex de 36)


def _leg(coid: str, side: str, price: int, count: int = 243, notes: str | None = None) -> Trade:
    return Trade(
        client_order_id=coid,
        ticker="KXMLBGAME-26JUN301940TBKC-TB",
        side=side,
        action="buy",
        count=count,
        price_cents=price,
        strategy="motor_1_arbitrage",
        status="filled",
        notes=notes,
    )


# =====================================================
# BUG B — settlement de Motor 1
# =====================================================


def test_motor_1_is_in_settlement_strategies():
    """La causa del par stale del 30-jun: motor_1_arbitrage fuera de la lista."""
    assert "motor_1_arbitrage" in SettlementPoller.STRATEGIES


@pytest.mark.asyncio
async def test_stale_motor1_pair_settles_by_ticker_without_order_id():
    """Las 60 filas reales: notes=None, kalshi_order_id=None. Con el mercado resuelto,
    el poller las settlea igual (resolución por TICKER) y libera la exposición."""
    with get_session() as s:
        s.add(_leg("aaaa-legacy-yes", "yes", 40))
        s.add(_leg("bbbb-legacy-no", "no", 57))
        s.commit()
    poller = SettlementPoller(_FakeSource({"KXMLBGAME-26JUN301940TBKC-TB": "yes"}))
    settled = await poller.settle_once()
    assert settled == 2
    with get_session() as s:
        rows = list(s.exec(select(Trade)))
    assert all(r.status == "settled" and r.pnl_cents is not None for r in rows)
    # yes ganadora: (100-40)*243 - fees ; no perdedora: -57*243 - fees
    by_side = {r.side: r for r in rows}
    assert by_side["yes"].pnl_cents > 0 > by_side["no"].pnl_cents


@pytest.mark.asyncio
async def test_unresolved_market_keeps_group_waiting():
    with get_session() as s:
        s.add(_leg("cccc-wait-yes", "yes", 40))
        s.commit()
    poller = SettlementPoller(_FakeSource({"KXMLBGAME-26JUN301940TBKC-TB": None}))
    assert await poller.settle_once() == 0
    with get_session() as s:
        assert s.exec(select(Trade)).first().status == "filled"


# =====================================================
# BUG A — arb_id en notes → netting del RiskManager
# =====================================================


@pytest.mark.asyncio
async def test_persist_intents_writes_shared_arb_id_in_notes():
    """El executor escribe 'arb_id=<uuid>' COMPARTIDO en ambas patas al persistir el
    intent (el mismo patrón que consume manager.py / arb_group_key)."""
    from src.strategies.motor_1_arbitrage.executor import ArbitrageExecutor

    opp = detect_binary_arb("T-ARB", 40, 300, 45, 300)
    assert opp is not None
    ex = ArbitrageExecutor.__new__(ArbitrageExecutor)  # sin __init__ (no client/risk)
    ex._persist_intents(opp, 10, ["coid-a", "coid-b"])
    with get_session() as s:
        rows = list(s.exec(select(Trade)))
    assert len(rows) == 2
    keys = {arb_group_key(r) for r in rows}
    assert len(keys) == 1  # MISMO grupo (netting + settlement atómico)
    assert all("arb_id=" in (r.notes or "") for r in rows)


def test_hedged_pair_with_arb_id_nets_to_zero_exposure():
    """El caso de producción, arreglado: par hedged 243×243 con arb_id en notes →
    exposición ~$0 (neteo), no $235 — Motor 2 recupera su headroom."""
    from src.risk.manager import RiskManager

    with get_session() as s:
        s.add(_leg("dddd-net-yes", "yes", 40, notes=f"arb_id={_UUID}"))
        s.add(_leg("eeee-net-no", "no", 57, notes=f"arb_id={_UUID}"))
        s.commit()
    with patch.object(RiskManager, "__init__", lambda self: None):
        rm = RiskManager()
    assert rm._get_current_exposure_usd() == pytest.approx(0.0)


def test_hedged_pair_without_arb_id_counts_gross_regression():
    """Pin del comportamiento legacy (las filas viejas SIN notes): cuentan bruto —
    por eso el settlement (BUG B) es el que libera lo ya atrapado."""
    from src.risk.manager import RiskManager

    with get_session() as s:
        s.add(_leg("ffff-old-yes", "yes", 40, count=243))
        s.add(_leg("gggg-old-no", "no", 57, count=243))
        s.commit()
    with patch.object(RiskManager, "__init__", lambda self: None):
        rm = RiskManager()
    assert rm._get_current_exposure_usd() == pytest.approx((40 + 57) * 243 / 100.0)
