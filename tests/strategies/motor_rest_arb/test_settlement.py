"""
Tests del settlement (PR-B) — el invariante central: la pérdida fantasma es IMPOSIBLE.

Un arb both-FILL se settlea con ambas patas en una transacción o con ninguna. Cubre:
atomicidad, resolución parcial (nada se settlea), crash a mitad del commit (nada queda
a medias), idempotencia del poller, re-entrega del mismo settlement, pata expuesta
única, PnL por lado, y el supervisor del loop (fallo de fuente no mata el poller).

La DB temporal la monta el conftest del paquete (autouse).
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import patch

import pytest
from sqlmodel import select

import src.storage.models as models
from src.strategies.motor_rest_arb.settlement import (
    MarketResolution,
    SettlementPoller,
    _leg_pnl_cents,
)


class FakeSettlementSource:
    """Fuente fake: dict ticker→result. None si no está; lanza si ticker en raise_on."""

    def __init__(self, resolutions: dict[str, str] | None = None):
        self.resolutions = resolutions or {}
        self.raise_on: set[str] = set()
        self.calls: list[str] = []

    async def get_resolution(self, ticker: str) -> MarketResolution | None:
        self.calls.append(ticker)
        if ticker in self.raise_on:
            raise RuntimeError(f"source down for {ticker}")
        result = self.resolutions.get(ticker)
        return MarketResolution(ticker=ticker, result=result) if result else None


def _mk_trade(
    *, ticker: str, side: str, arb_id: str, status: str = "filled",
    price: int = 40, count: int = 5, fees: int = 4,
) -> str:
    """Inserta una fila Trade con la convención de A.1; devuelve el coid."""
    coid = f"{arb_id}-{side}"
    with models.get_session() as s:
        s.add(models.Trade(
            client_order_id=coid, ticker=ticker, side=side, action="buy",
            count=count, price_cents=price, fill_price_cents=price, fees_cents=fees,
            strategy="motor_rest_arb", status=status, notes=f"arb_id={arb_id}",
        ))
        s.commit()
    return coid


def _all_trades() -> dict[str, models.Trade]:
    with models.get_session() as s:
        return {t.client_order_id: t for t in s.exec(select(models.Trade)).all()}


def _arb(yes_ticker: str = "KXWC-ARG", no_ticker: str = "KXWC-ARG") -> str:
    """Arb both-FILL estándar: yes a 40¢ y no a 45¢, 5 contratos."""
    arb_id = str(uuid.uuid4())
    _mk_trade(ticker=yes_ticker, side="yes", arb_id=arb_id, price=40, fees=4)
    _mk_trade(ticker=no_ticker, side="no", arb_id=arb_id, price=45, fees=5)
    return arb_id


@pytest.mark.asyncio
async def test_both_legs_settle_atomically_with_correct_pnl():
    """Ambos mercados resueltos → AMBAS patas settled en el mismo tick, PnL por lado."""
    arb_id = _arb()
    source = FakeSettlementSource({"KXWC-ARG": "yes"})  # resolvió YES

    settled = await SettlementPoller(source).settle_once()

    assert settled == 2
    trades = _all_trades()
    yes, no = trades[f"{arb_id}-yes"], trades[f"{arb_id}-no"]
    assert yes.status == "settled" and no.status == "settled"
    assert yes.pnl_cents == (100 - 40) * 5 - 4   # ganadora: +296
    assert no.pnl_cents == -45 * 5 - 5           # perdedora: −230
    assert yes.settled_at is not None and yes.settled_at.tzinfo is None  # naive UTC
    assert "settled_by_poller" in (yes.notes or "")


@pytest.mark.asyncio
async def test_partial_resolution_settles_nothing():
    """Huérfana imposible: si una pata no tiene resolución, NINGUNA del grupo se settlea."""
    arb_id = _arb(yes_ticker="KXWC-A", no_ticker="KXWC-B")  # patas en mercados distintos
    source = FakeSettlementSource({"KXWC-A": "yes"})  # B aún sin resolver

    settled = await SettlementPoller(source).settle_once()

    assert settled == 0
    trades = _all_trades()
    assert trades[f"{arb_id}-yes"].status == "filled"   # ni siquiera la resuelta
    assert trades[f"{arb_id}-no"].status == "filled"
    assert trades[f"{arb_id}-yes"].pnl_cents is None


@pytest.mark.asyncio
async def test_crash_mid_commit_leaves_no_half_settled_group():
    """Crash en el commit de escritura → transacción no aplicada: ambas siguen filled."""
    arb_id = _arb()
    source = FakeSettlementSource({"KXWC-ARG": "yes"})
    poller = SettlementPoller(source)

    real_get_session = models.get_session
    calls = {"n": 0}

    def flaky_session():
        calls["n"] += 1
        s = real_get_session()
        if calls["n"] >= 2:  # 1ª = lectura; 2ª = la transacción de escritura
            def _boom() -> None:
                raise RuntimeError("db crash mid-commit")
            s.commit = _boom  # type: ignore[method-assign]
        return s

    with patch(
        "src.strategies.motor_rest_arb.settlement.get_session", side_effect=flaky_session
    ), pytest.raises(RuntimeError, match="mid-commit"):
        await poller.settle_once()

    trades = _all_trades()
    assert trades[f"{arb_id}-yes"].status == "filled"
    assert trades[f"{arb_id}-no"].status == "filled"
    assert all(t.pnl_cents is None for t in trades.values())


@pytest.mark.asyncio
async def test_poller_is_idempotent_and_redelivery_is_noop():
    """2º tick con la MISMA resolución re-entregada → 0 settled, nada cambia."""
    arb_id = _arb()
    source = FakeSettlementSource({"KXWC-ARG": "yes"})
    poller = SettlementPoller(source)

    assert await poller.settle_once() == 2
    first = {c: (t.status, t.pnl_cents, t.settled_at) for c, t in _all_trades().items()}

    assert await poller.settle_once() == 0  # idempotente: ya no hay filled
    second = {c: (t.status, t.pnl_cents, t.settled_at) for c, t in _all_trades().items()}
    assert first == second
    assert f"{arb_id}-yes" in first  # sanity


@pytest.mark.asyncio
async def test_single_exposed_leg_settles_alone():
    """La pata expuesta de un kill-switch (única filled del arb) se settlea sola."""
    arb_id = str(uuid.uuid4())
    _mk_trade(ticker="KXWC-MEX", side="yes", arb_id=arb_id, price=40, fees=4)
    # La pata hermana quedó cancelled (KILL) — no espera resolución.
    _mk_trade(ticker="KXWC-MEX", side="no", arb_id=arb_id, status="cancelled", price=45, fees=5)
    source = FakeSettlementSource({"KXWC-MEX": "no"})  # la expuesta PERDIÓ

    settled = await SettlementPoller(source).settle_once()

    assert settled == 1
    trades = _all_trades()
    assert trades[f"{arb_id}-yes"].status == "settled"
    assert trades[f"{arb_id}-yes"].pnl_cents == -40 * 5 - 4  # pérdida → el stop-loss la ve
    assert trades[f"{arb_id}-no"].status == "cancelled"      # intacta


@pytest.mark.asyncio
async def test_source_failure_mid_group_settles_nothing_and_propagates():
    """La fuente explota en la 2ª pata → nada se escribió (resolución completa ANTES de DB)."""
    arb_id = _arb(yes_ticker="KXWC-A", no_ticker="KXWC-B")
    source = FakeSettlementSource({"KXWC-A": "yes", "KXWC-B": "no"})
    source.raise_on = {"KXWC-B"}

    with pytest.raises(RuntimeError, match="source down"):
        await SettlementPoller(source).settle_once()

    trades = _all_trades()
    assert all(t.status == "filled" for t in trades.values())
    assert arb_id  # sanity


@pytest.mark.asyncio
async def test_run_supervisor_survives_source_failures():
    """El loop NO muere ante fallos del tick: registra en BotState y sigue."""
    _arb(yes_ticker="KXWC-X", no_ticker="KXWC-X")
    source = FakeSettlementSource({"KXWC-X": "yes"})
    source.raise_on = {"KXWC-X"}  # todos los ticks fallan
    poller = SettlementPoller(source)
    poller.POLL_INTERVAL_SEC = 0.01

    stop = asyncio.Event()
    with patch("src.monitoring.health.BotState.record_error") as mock_rec:
        task = asyncio.create_task(poller.run(stop))
        await asyncio.sleep(0.05)  # varios ticks fallidos
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)  # el loop terminó limpio, sin morir antes

    mock_rec.assert_called()
    assert "settlement" in mock_rec.call_args.args[0]


def test_leg_pnl_uses_fill_price_and_recomputes_missing_fees():
    """PnL usa fill_price_cents (real) y recomputa fees si la fila no los trae."""
    t = models.Trade(
        client_order_id="x", ticker="T", side="yes", action="buy", count=10,
        price_cents=50, fill_price_cents=47, fees_cents=None,
        strategy="motor_rest_arb", status="filled",
    )
    pnl = _leg_pnl_cents(t, "yes")
    from src.math.fees import kalshi_fee_cents
    assert pnl == (100 - 47) * 10 - kalshi_fee_cents(10, 47)
