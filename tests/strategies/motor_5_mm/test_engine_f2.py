"""
Motor5Engine en modo F2 (executor presente): gates de kill-switch/quotes_paused,
sync de quotes reales, retiro de tickers y fills reales aplicados UNA sola vez.
"""

from __future__ import annotations

import pytest
from sqlmodel import select

from src.storage.models import (
    MMShadowFill,
    Trade,
    clear_kill_switch,
    engage_kill_switch,
    get_session,
    set_mm_quotes_paused,
)
from src.strategies.fair_value_book import FairValueBook
from src.strategies.motor_5_mm.engine import Motor5Engine
from src.strategies.motor_5_mm.reconciler import MMReconcileReport


class _ReadOnlyClient:
    def __init__(self):
        self.books: dict[str, dict] = {}

    async def get_orderbook(self, ticker: str) -> dict:
        book = self.books.get(ticker)
        if book is None:
            raise RuntimeError("book no disponible")
        return {"orderbook": book}


class _FakeExecutor:
    def __init__(self):
        self.synced: list = []
        self.retired: list[str] = []
        self.cancel_all_calls: list[str] = []
        self.corrupted: set[str] = set()

    async def sync_quotes(self, quote):
        self.synced.append(quote)
        return "synced"

    async def retire_ticker(self, ticker):
        self.retired.append(ticker)

    async def cancel_all(self, reason):
        self.cancel_all_calls.append(reason)
        return 2

    def live_coids(self) -> set[str]:
        return set()


class _FakeReconciler:
    def __init__(self):
        self.calls = 0
        self.report = MMReconcileReport()
        self.live_coids_seen: list[set[str] | None] = []

    async def reconcile(self, live_coids=None):
        self.calls += 1
        self.live_coids_seen.append(live_coids)
        return self.report


def _book(yes_bid: int, yes_ask: int) -> dict:
    return {"yes": [[yes_bid, 100]], "no": [[100 - yes_ask, 100]]}


def _live_engine(client) -> tuple[Motor5Engine, _FakeExecutor, _FakeReconciler]:
    eng = Motor5Engine(max_tickers=2, trading_enabled=True)
    eng._client = client
    ex, rec = _FakeExecutor(), _FakeReconciler()
    eng._executor = ex
    eng._reconciler = rec
    return eng, ex, rec


@pytest.fixture(autouse=True)
def _clean_flags():
    yield
    clear_kill_switch()
    set_mm_quotes_paused(False)


@pytest.mark.asyncio
async def test_live_tick_syncs_quotes_and_skips_shadow_fills():
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng, ex, rec = _live_engine(client)
    await eng._tick()
    await eng._tick()  # 2do tick: en modo live NO hay fills shadow aunque el book cruce
    client.books["T-A"] = _book(40, 44)
    await eng._tick()
    assert len(ex.synced) >= 2
    assert rec.calls == 3  # reconcile en cada tick
    with get_session() as s:
        assert list(s.exec(select(MMShadowFill))) == []  # la inferencia shadow queda apagada


@pytest.mark.asyncio
async def test_kill_switch_cancels_all_once_and_stops_quoting():
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng, ex, rec = _live_engine(client)
    engage_kill_switch("test panic")
    await eng._tick()
    await eng._tick()
    assert ex.cancel_all_calls == ["kill_switch: test panic"]  # UNA vez, no spam
    assert ex.synced == []  # cero quotes con el switch puesto


@pytest.mark.asyncio
async def test_quotes_paused_stops_new_quotes_but_keeps_reconciling():
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng, ex, rec = _live_engine(client)
    set_mm_quotes_paused(True, "revision manual")
    await eng._tick()
    assert ex.synced == [] and ex.cancel_all_calls == []
    assert rec.calls == 1  # sigue gestionando: el reconcile corre igual


@pytest.mark.asyncio
async def test_reconciler_corruption_propagates_to_executor():
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng, ex, rec = _live_engine(client)
    rec.report.corrupted_tickers.add("T-A")
    await eng._tick()
    assert ex.corrupted == {"T-A"}


@pytest.mark.asyncio
async def test_ticker_leaving_universe_retires_real_quotes():
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng, ex, rec = _live_engine(client)
    await eng._tick()
    FairValueBook.clear()  # el fair desaparece
    await eng._tick()
    assert "T-A" in ex.retired


@pytest.mark.asyncio
async def test_shutdown_cancels_all_quotes():
    """P0 auditoría 2026-07-07: al apagarse (stop_event), el engine cancela TODAS las
    quotes resting — antes sobrevivían al proceso como exposición sin gestor."""
    import asyncio
    from unittest.mock import patch

    class _Factory:
        async def __aenter__(self):
            return _ReadOnlyClient()

        async def __aexit__(self, *a):
            return False

    fake_ex = _FakeExecutor()
    stop = asyncio.Event()
    stop.set()  # apagado inmediato: cero ticks, pero el cancel-all de salida corre igual
    eng = Motor5Engine(trading_enabled=True, client_factory=_Factory)
    with (
        patch("src.strategies.motor_5_mm.engine.Motor5Executor", return_value=fake_ex),
        patch("src.strategies.motor_5_mm.engine.MMReconciler"),
    ):
        await eng.run(stop)
    assert fake_ex.cancel_all_calls == ["shutdown"]


@pytest.mark.asyncio
async def test_shutdown_in_shadow_mode_has_no_executor_and_no_crash():
    """CONTROL: en F1 shadow no hay executor → el shutdown no toca nada ni crashea."""
    import asyncio

    class _Factory:
        async def __aenter__(self):
            return _ReadOnlyClient()

        async def __aexit__(self, *a):
            return False

    stop = asyncio.Event()
    stop.set()
    eng = Motor5Engine(trading_enabled=False, client_factory=_Factory)
    await eng.run(stop)  # sin excepción = ok


@pytest.mark.asyncio
async def test_live_tick_passes_live_coids_to_reconciler():
    """El reconcile recibe las quotes vivas del executor (base del barrido de
    huérfanas): tras un restart _live está vacío → set() explícito, no None."""
    client = _ReadOnlyClient()
    eng, ex, rec = _live_engine(client)
    await eng._tick()
    assert rec.live_coids_seen == [set()]


@pytest.mark.asyncio
async def test_settled_fills_applied_once_to_inventory():
    """La verdad del reconciler (fila filled) muta el inventario UNA vez por coid —
    ticks posteriores no re-aplican (sin doble conteo)."""
    client = _ReadOnlyClient()
    eng, ex, rec = _live_engine(client)
    with get_session() as s:
        s.add(
            Trade(
                client_order_id="m5mm-fill1",
                ticker="T-A",
                side="yes",
                action="buy",
                count=10,
                price_cents=47,
                strategy="motor_5_mm",
                status="filled",
                filled_count=6,
            )
        )
        s.commit()
    await eng._tick()
    assert eng._inventory.net("T-A") == 6
    await eng._tick()
    await eng._tick()
    assert eng._inventory.net("T-A") == 6  # idempotente
