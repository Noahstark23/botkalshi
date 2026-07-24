"""
Chunking del get_snapshot de recovery (incidente 2026-07-17).

Causa raíz medida en producción: un get_snapshot de un sid GRANDE (sid=1, 223 tickers) nunca
devolvía un solo snapshot en 30s → timeout_x5 → circuit breaker, mientras sids de 26 y 199
recuperaban bien (umbral de fallo entre 199 y 223). El WS dropea la request masiva entera.

Fix: partir `live` en lotes de RECOVERY_CHUNK_SIZE, cada uno con su req_id/pending set. La
recovery del sid completa cuando TODOS los lotes drenaron — NO cuando se vacía el primero
(ese era el riesgo del cambio: cerrar con la mayoría de books aún stale).
"""

from __future__ import annotations

import pytest

from src.strategies.motor_1_arbitrage.orderbook_manager_v2 import OrderbookManagerV2, SidGapError


def _snapshot(ticker: str, sid: int = 1, seq: int = 1, req_id: int | None = None) -> dict:
    msg: dict = {
        "type": "orderbook_snapshot",
        "sid": sid,
        "seq": seq,
        "msg": {"market_ticker": ticker, "yes_dollars_fp": [], "no_dollars_fp": []},
    }
    if req_id is not None:
        msg["id"] = req_id
    return msg


def _delta(ticker: str, sid: int = 1, seq: int = 2) -> dict:
    return {
        "type": "orderbook_delta",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "price_dollars": "0.4000",
            "delta_fp": "100.00",
            "side": "yes",
        },
    }


class _CountingWs:
    def __init__(self) -> None:
        self._next = 42
        self.commands: list = []

    async def send_command(self, *a, **kw) -> int:
        self.commands.append((a, kw))
        rid = self._next
        self._next += 1
        return rid


async def _enter_recovery(manager: OrderbookManagerV2, tickers: list[str]) -> None:
    """Puebla el sid=1 con `tickers` y dispara un gap → recovery."""
    for i, t in enumerate(tickers, start=1):
        await manager.handle_message(_snapshot(t, sid=1, seq=i))
    with pytest.raises(SidGapError):
        await manager.handle_message(_delta(tickers[0], sid=1, seq=999))
    assert 1 in manager._recovering


def _pending_tickers(manager: OrderbookManagerV2, sid: int = 1) -> set[str]:
    return {
        t for _r, (s, pend) in manager._pending_snapshot_requests.items() if s == sid for t in pend
    }


# ── El chunking parte la request masiva ──────────────────────────────────────


@pytest.mark.asyncio
async def test_large_sid_is_split_into_chunks():
    """MECANISMO: 120 tickers con chunk=50 → 3 lotes (50+50+20), cada uno su req_id. El
    get_snapshot masivo de 120 (que el WS dropea) nunca se emite entero."""
    ws = _CountingWs()
    manager = OrderbookManagerV2(ws, recovery_chunk_size=50)
    tickers = [f"T{i:03d}" for i in range(120)]
    await _enter_recovery(manager, tickers)

    reqs = [(s, pend) for _r, (s, pend) in manager._pending_snapshot_requests.items() if s == 1]
    assert len(reqs) == 3  # 50 + 50 + 20
    assert sorted(len(p) for _s, p in reqs) == [20, 50, 50]
    # Cada send_command pidió ≤50 tickers (ninguna request masiva).
    snapshot_cmds = [kw for _a, kw in ws.commands if kw.get("action") == "get_snapshot"]
    assert all(len(kw["params"]["market_tickers"]) <= 50 for kw in snapshot_cmds)
    assert _pending_tickers(manager) == set(tickers)  # cobertura total, sin perder ninguno


@pytest.mark.asyncio
async def test_recovery_completes_only_when_all_chunks_drain():
    """CONTROL CRÍTICO: la recovery NO debe cerrar al vaciarse el PRIMER lote (dejaría la
    mayoría de books stale). Solo cierra cuando el último ticker del último lote llega."""
    ws = _CountingWs()
    manager = OrderbookManagerV2(ws, recovery_chunk_size=2)
    tickers = ["A", "B", "C", "D", "E"]  # → lotes [A,B] [C,D] [E]
    await _enter_recovery(manager, tickers)
    assert len({r for r, (s, _) in manager._pending_snapshot_requests.items() if s == 1}) == 3

    # Drenar los dos primeros lotes completos → el sid SIGUE en recovery (queda [E]).
    for t in ["A", "B", "C", "D"]:
        await manager.handle_message(_snapshot(t, sid=1, seq=100))
    assert 1 in manager._recovering  # NO cerró: falta E
    assert _pending_tickers(manager) == {"E"}

    # Llega E (el último) → recién ahí cierra y drena.
    await manager.handle_message(_snapshot("E", sid=1, seq=100))
    assert 1 not in manager._recovering
    assert 1 not in manager._pending_snapshot_requests.get(1, ())  # sin req pendientes


@pytest.mark.asyncio
async def test_snapshot_without_id_routes_to_correct_chunk():
    """Ruteo por TICKER (no por req_id): un snapshot sin id (fallback) de un ticker del
    segundo lote debe descontarse del lote correcto, no del primero — si no, ese lote nunca
    se vaciaría y la recovery no cerraría jamás."""
    ws = _CountingWs()
    manager = OrderbookManagerV2(ws, recovery_chunk_size=2)
    tickers = ["A", "B", "C", "D"]  # lotes [A,B] [C,D]
    await _enter_recovery(manager, tickers)

    # Todos los snapshots llegan SIN id (Kalshi puede omitirlo) → van por el fallback,
    # que pasa el PRIMER req_id del sid; el ruteo por ticker debe igual acertar el lote.
    for t in tickers:
        await manager.handle_message(_snapshot(t, sid=1, seq=100))  # sin req_id
    assert 1 not in manager._recovering  # cerró: los 4 se rutearon a su lote correcto
    assert _pending_tickers(manager) == set()


@pytest.mark.asyncio
async def test_single_chunk_is_unchanged_behavior():
    """CONTROL de no-regresión: con live ≤ chunk_size hay UN solo req_id y el flujo es
    idéntico al de antes del chunking (todos los snapshots → cierra)."""
    ws = _CountingWs()
    manager = OrderbookManagerV2(ws, recovery_chunk_size=50)
    tickers = ["A", "B", "C"]
    await _enter_recovery(manager, tickers)
    assert len({r for r, (s, _) in manager._pending_snapshot_requests.items() if s == 1}) == 1

    for t in tickers:
        await manager.handle_message(_snapshot(t, sid=1, seq=100))
    assert 1 not in manager._recovering


@pytest.mark.asyncio
async def test_partial_chunk_failure_reports_real_progress():
    """Con lotes, si unos drenan y otros no, el progress del abort refleja lo REAL
    (recovered parcial), no 0 ni total — insumo del sub-caso settlement/timeout."""
    ws = _CountingWs()
    manager = OrderbookManagerV2(ws, recovery_chunk_size=2)
    tickers = ["A", "B", "C", "D"]  # lotes [A,B] [C,D]
    await _enter_recovery(manager, tickers)

    await manager.handle_message(_snapshot("A", sid=1, seq=100))
    await manager.handle_message(_snapshot("B", sid=1, seq=100))  # lote 1 completo
    # C, D nunca llegan. El progress debe decir recovered=2/4.
    assert manager._recovery_progress(1).startswith("recovered=2/4")
