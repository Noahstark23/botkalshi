"""
Motor 5 F2 — executor (intents pre-red, corrupted-no-opera, cancel_all) y reconciler
(pending↔get_orders, fantasmas, fills parciales). Cliente FAKE con contabilidad de
llamadas: acá SÍ existen place/cancel — lo que se pinea es el protocolo alrededor.
"""

from __future__ import annotations

import pytest
from sqlmodel import select

from src.clients.kalshi_rest import KalshiClientError, KalshiServerError
from src.storage.models import Trade, get_session
from src.strategies.motor_5_mm.executor import COID_PREFIX, Motor5Executor
from src.strategies.motor_5_mm.quoter import QuoteSet
from src.strategies.motor_5_mm.reconciler import MMReconciler


@pytest.fixture(autouse=True)
def _clear_locks():
    Motor5Executor._locks.clear()
    yield
    Motor5Executor._locks.clear()


class _FakeClient:
    def __init__(self):
        self.placed: list[dict] = []
        self.cancelled: list[str] = []
        self.batch_cancelled: list[list[str]] = []
        self.place_error: Exception | None = None
        self.cancel_error: Exception | None = None
        self.orders_response: dict = {"orders": []}
        self._seq = 0

    async def place_order(self, **kw):
        if self.place_error is not None:
            raise self.place_error
        self._seq += 1
        self.placed.append(kw)
        return {"order": {"order_id": f"K{self._seq}", "fill_count": "0.00"}}

    async def cancel_order(self, order_id: str):
        if self.cancel_error is not None:
            raise self.cancel_error
        self.cancelled.append(order_id)
        return {"order": {"order_id": order_id, "fill_count": "0.00"}}

    async def batch_cancel_orders(self, ids: list[str]):
        self.batch_cancelled.append(ids)
        return {"ids": ids}

    async def get_orders(self, **kw):
        return self.orders_response


def _quote(bid=47, ask=53, size=10) -> QuoteSet:
    return QuoteSet(ticker="T", fair_prob=0.5, bid_cents=bid, ask_cents=ask, size=size)


def _rows() -> list[Trade]:
    with get_session() as s:
        return list(s.exec(select(Trade)))


# =====================================================
# Executor
# =====================================================


@pytest.mark.asyncio
async def test_sync_places_both_sides_with_intent_pre_red():
    client = _FakeClient()
    ex = Motor5Executor(client)
    assert await ex.sync_quotes(_quote()) == "synced"
    assert len(client.placed) == 2
    assert all(kw["post_only"] and kw["time_in_force"] == "gtc" for kw in client.placed)
    rows = _rows()
    assert len(rows) == 2 and all(r.status == "pending" for r in rows)
    assert all(r.client_order_id.startswith(COID_PREFIX) for r in rows)
    assert all(r.kalshi_order_id for r in rows)  # id de Kalshi persistido post-place


@pytest.mark.asyncio
async def test_unchanged_quote_keeps_resting_order():
    """Quote idéntica al tick siguiente → NO cancel+create (conserva prioridad de cola)."""
    client = _FakeClient()
    ex = Motor5Executor(client)
    await ex.sync_quotes(_quote())
    await ex.sync_quotes(_quote())
    assert len(client.placed) == 2 and client.cancelled == []


@pytest.mark.asyncio
async def test_changed_price_cancels_then_places():
    client = _FakeClient()
    ex = Motor5Executor(client)
    await ex.sync_quotes(_quote())
    await ex.sync_quotes(_quote(bid=46, ask=53))  # solo el bid cambió
    assert len(client.cancelled) == 1 and len(client.placed) == 3


@pytest.mark.asyncio
async def test_deterministic_reject_marks_cancelled_not_corrupted():
    """4xx (p.ej. post_only cruzaría): la orden NO existe → fila cancelled, sin corrupción."""
    client = _FakeClient()
    client.place_error = KalshiClientError(400, "rejected", "", "post_only_would_cross")
    ex = Motor5Executor(client)
    await ex.sync_quotes(_quote())
    assert "T" not in ex.corrupted
    assert all(r.status == "cancelled" for r in _rows())


@pytest.mark.asyncio
async def test_ambiguous_error_leaves_pending_and_corrupts():
    """5xx/timeout: la orden PUDO entrar → fila queda pending + ticker corrupto (solo
    el reconciler resuelve; jamás re-colocar a ciegas)."""
    client = _FakeClient()
    client.place_error = KalshiServerError(500, "boom")
    ex = Motor5Executor(client)
    await ex.sync_quotes(_quote())
    assert "T" in ex.corrupted
    assert any(r.status == "pending" for r in _rows())


@pytest.mark.asyncio
async def test_corrupted_ticker_does_not_quote():
    client = _FakeClient()
    ex = Motor5Executor(client)
    ex.corrupted.add("T")
    assert await ex.sync_quotes(_quote()) == "corrupted"
    assert client.placed == []  # estado corrupto no opera


@pytest.mark.asyncio
async def test_cancel_partial_fill_records_filled_count():
    """La orden se llenó PARCIALMENTE antes del cancel → fila filled con filled_count."""
    client = _FakeClient()
    ex = Motor5Executor(client)
    await ex.sync_quotes(_quote())

    async def cancel_with_fill(order_id: str):
        return {"order": {"order_id": order_id, "fill_count": "4.00"}}

    client.cancel_order = cancel_with_fill
    await ex.sync_quotes(_quote(bid=46, ask=54))  # ambos precios cambian → 2 cancels
    filled = [r for r in _rows() if r.status == "filled"]
    assert filled and all(r.filled_count == 4 for r in filled)


@pytest.mark.asyncio
async def test_cancel_all_uses_batch_and_marks_rows():
    client = _FakeClient()
    ex = Motor5Executor(client)
    await ex.sync_quotes(_quote())
    n = await ex.cancel_all("kill_switch")
    assert n == 2
    assert len(client.batch_cancelled) == 1 and len(client.batch_cancelled[0]) == 2
    assert all(r.status == "cancelled" for r in _rows())
    assert "T" in ex.corrupted  # todo queda en duda hasta el próximo reconcile


# =====================================================
# Reconciler
# =====================================================


def _pending_row(coid: str, ticker: str = "T", count: int = 10) -> None:
    with get_session() as s:
        s.add(
            Trade(
                client_order_id=coid,
                ticker=ticker,
                side="yes",
                action="buy",
                count=count,
                price_cents=47,
                strategy="motor_5_mm",
                status="pending",
            )
        )
        s.commit()


@pytest.mark.asyncio
async def test_reconcile_resolves_executed_and_cancelled():
    client = _FakeClient()
    _pending_row("m5mm-aaa")
    _pending_row("m5mm-bbb")
    client.orders_response = {
        "orders": [
            {
                "client_order_id": "m5mm-aaa",
                "order_id": "K1",
                "status": "executed",
                "fill_count": "10.00",
                "ticker": "T",
            },
            {
                "client_order_id": "m5mm-bbb",
                "order_id": "K2",
                "status": "canceled",
                "fill_count": "0.00",
                "ticker": "T",
            },
        ]
    }
    report = await MMReconciler(client).reconcile()
    assert report.resolved_filled == 1 and report.resolved_cancelled == 1
    assert report.discrepancies == 0
    by_coid = {r.client_order_id: r for r in _rows()}
    assert by_coid["m5mm-aaa"].status == "filled" and by_coid["m5mm-aaa"].filled_count == 10
    assert by_coid["m5mm-bbb"].status == "cancelled"


@pytest.mark.asyncio
async def test_reconcile_partial_then_cancel_is_filled():
    client = _FakeClient()
    _pending_row("m5mm-ccc")
    client.orders_response = {
        "orders": [
            {
                "client_order_id": "m5mm-ccc",
                "order_id": "K3",
                "status": "canceled",
                "fill_count": "3.00",
                "ticker": "T",
            },
        ]
    }
    report = await MMReconciler(client).reconcile()
    assert report.resolved_filled == 1
    row = _rows()[0]
    assert row.status == "filled" and row.filled_count == 3


@pytest.mark.asyncio
async def test_reconcile_unknown_pending_corrupts_ticker():
    """pending sin orden en la API → discrepancia irresoluble → ticker corrupto."""
    client = _FakeClient()
    _pending_row("m5mm-ddd", ticker="T-X")
    report = await MMReconciler(client).reconcile()
    assert report.discrepancies == 1 and "T-X" in report.corrupted_tickers


@pytest.mark.asyncio
async def test_reconcile_phantom_resting_is_cancelled():
    """Orden resting con NUESTRO prefijo sin fila pending (DB la perdió) → se cancela."""
    client = _FakeClient()
    client.orders_response = {
        "orders": [
            {"client_order_id": "m5mm-eee", "order_id": "K9", "status": "resting", "ticker": "T-Y"},
        ]
    }
    report = await MMReconciler(client).reconcile()
    assert report.phantom_cancelled == 1 and "T-Y" in report.corrupted_tickers
    assert client.cancelled == ["K9"]


@pytest.mark.asyncio
async def test_reconcile_foreign_orders_untouched():
    """Órdenes de otros motores (sin prefijo m5mm-) JAMÁS se tocan."""
    client = _FakeClient()
    client.orders_response = {
        "orders": [
            {"client_order_id": "arb-123", "order_id": "K7", "status": "resting", "ticker": "T-Z"},
        ]
    }
    report = await MMReconciler(client).reconcile()
    assert report.phantom_cancelled == 0 and client.cancelled == []


# =====================================================
# Huérfanas resting (P0 auditoría 2026-07-07): resting real
# que este proceso no gestiona → cancelar, no cotizar encima
# =====================================================


def _resting_order(coid: str, oid: str = "K10", ticker: str = "T", fill: str = "0.00") -> dict:
    return {
        "client_order_id": coid,
        "order_id": oid,
        "status": "resting",
        "fill_count": fill,
        "ticker": ticker,
    }


@pytest.mark.asyncio
async def test_reconcile_orphan_resting_is_cancelled():
    """Tras un restart el executor arranca con _live vacío: la resting del proceso
    anterior (pending en DB + resting en la API) se CANCELA en el primer reconcile."""
    client = _FakeClient()
    _pending_row("m5mm-fff")
    client.orders_response = {"orders": [_resting_order("m5mm-fff")]}
    report = await MMReconciler(client).reconcile(live_coids=set())
    assert report.orphan_cancelled == 1 and report.resolved_cancelled == 1
    assert client.cancelled == ["K10"]
    row = _rows()[0]
    assert row.status == "cancelled" and "orphan_resting_cancelled" in (row.notes or "")


@pytest.mark.asyncio
async def test_reconcile_orphan_partial_fill_recorded_from_cancel_response():
    """El fill parcial que ganó la carrera al cancel queda registrado (fila filled con
    filled_count) → _apply_settled_fills lo lleva al inventario."""
    client = _FakeClient()
    _pending_row("m5mm-ggg")
    client.orders_response = {"orders": [_resting_order("m5mm-ggg")]}

    async def cancel_with_fill(order_id: str):
        return {"order": {"order_id": order_id, "fill_count": "4.00"}}

    client.cancel_order = cancel_with_fill
    report = await MMReconciler(client).reconcile(live_coids=set())
    assert report.orphan_cancelled == 1 and report.resolved_filled == 1
    row = _rows()[0]
    assert row.status == "filled" and row.filled_count == 4


@pytest.mark.asyncio
async def test_reconcile_live_quote_is_not_orphan():
    """CONTROL: una resting que el executor SÍ gestiona (coid en live_coids) queda
    intacta — still_resting, cero cancels."""
    client = _FakeClient()
    _pending_row("m5mm-hhh")
    client.orders_response = {"orders": [_resting_order("m5mm-hhh")]}
    report = await MMReconciler(client).reconcile(live_coids={"m5mm-hhh"})
    assert report.orphan_cancelled == 0 and report.still_resting == 1
    assert client.cancelled == []
    assert _rows()[0].status == "pending"


@pytest.mark.asyncio
async def test_reconcile_without_live_coids_keeps_legacy_behavior():
    """CONTROL: live_coids=None (caller legacy/diagnóstico) → sin info no se cancela."""
    client = _FakeClient()
    _pending_row("m5mm-iii")
    client.orders_response = {"orders": [_resting_order("m5mm-iii")]}
    report = await MMReconciler(client).reconcile()
    assert report.orphan_cancelled == 0 and report.still_resting == 1
    assert client.cancelled == []


@pytest.mark.asyncio
async def test_reconcile_orphan_cancel_error_corrupts_ticker():
    """FAIL-SAFE: si el cancel de la huérfana falla, jamás se asume 'cancelada' — la
    fila sigue pending y el ticker queda corrupto (no se cotiza encima)."""
    client = _FakeClient()
    _pending_row("m5mm-jjj", ticker="T-ORF")
    client.orders_response = {"orders": [_resting_order("m5mm-jjj", ticker="T-ORF")]}
    client.cancel_error = RuntimeError("timeout")
    report = await MMReconciler(client).reconcile(live_coids=set())
    assert report.orphan_cancelled == 0 and report.discrepancies == 1
    assert "T-ORF" in report.corrupted_tickers
    assert _rows()[0].status == "pending"


# =====================================================
# Canary cap F3 (techo duro del costo abierto del MM)
# =====================================================


@pytest.mark.asyncio
async def test_canary_cap_blocks_order_over_ceiling():
    """Con $100 de cap y $95 ya abiertos, una quote de $10 NO se coloca."""
    client = _FakeClient()
    _pending_row("m5mm-open1", ticker="T-OTRO", count=200)  # 200×47c = $94 abiertos
    ex = Motor5Executor(client, max_exposure_usd=100.0)
    outcome = await ex.sync_quotes(_quote(bid=50, ask=None, size=20))  # $10 el bid
    assert outcome == "risk_blocked"
    assert client.placed == []


@pytest.mark.asyncio
async def test_canary_cap_allows_within_ceiling():
    client = _FakeClient()
    ex = Motor5Executor(client, max_exposure_usd=100.0)
    outcome = await ex.sync_quotes(_quote(bid=47, ask=53, size=10))  # ~$4.7+$5.3
    assert outcome == "synced" and len(client.placed) == 2


@pytest.mark.asyncio
async def test_canary_cap_none_means_no_own_ceiling():
    """Demo (F2): sin cap propio — manda solo el headroom global si hay RiskManager."""
    client = _FakeClient()
    _pending_row("m5mm-open2", ticker="T-OTRO", count=10_000)
    ex = Motor5Executor(client, max_exposure_usd=None)
    assert await ex.sync_quotes(_quote()) == "synced"
