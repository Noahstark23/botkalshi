"""
Higiene de recovery del OrderbookManagerV2 (fix 2026-07-10).

Incidente: al arranque, el sid=1 (242 tickers) contenía un mercado en settlement
(KXMENWORLDCUP-26-ES) cuyo snapshot NUNCA llegó → timeout ×5 → recovery DESHABILITADA con
un CRITICAL engañoso `recovered=242/242`. Dos problemas:

  1. LOG FALSO: `_disable_recovery` recomputaba el progress DESPUÉS de que el caller
     (_abort_and_restart_recovery) ya había hecho _cleanup_recovery → pending=0 →
     'recovered=total/total'. El progress real (parcial) hay que pasarlo desde el caller.
  2. SETTLEMENT ESCALADO A CRITICAL: 1-2 mercados cerrados atascando el sid disparaban 5
     reintentos y un CRITICAL, cuando lo correcto es marcarlos dead y recuperar el resto.

(La hipótesis previa de 'desalineación req_id' está refutada: el router ya rutea snapshots
sin id / con id que no matchea por ticker/sid — ver handle_message fallback.)
"""

from __future__ import annotations

import time

import pytest
from loguru import logger

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
    """WS fake: send_command devuelve req_ids incrementales (42, 43, 44, …)."""

    def __init__(self) -> None:
        self._next = 42
        self.commands: list = []

    async def send_command(self, *a, **kw) -> int:
        self.commands.append((a, kw))
        rid = self._next
        self._next += 1
        return rid


def _capture(level: str) -> tuple[list[str], int]:
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level=level)
    return lines, sink


async def _enter_recovery_multi(manager: OrderbookManagerV2, tickers: list[str]) -> int:
    """Establece un sid con varios tickers y dispara recovery. Devuelve el req_id emitido."""
    for i, t in enumerate(tickers, start=1):
        await manager.handle_message(_snapshot(t, sid=1, seq=i))
    with pytest.raises(SidGapError):
        await manager.handle_message(_delta(tickers[0], sid=1, seq=99))  # gap → recovery
    assert 1 in manager._recovering
    return next(r for r, (s, _) in manager._pending_snapshot_requests.items() if s == 1)


# =====================================================
# (a) Happy path: todos los snapshots llegan → cierra, 0 disable, 0 CRITICAL
# =====================================================


async def test_all_snapshots_arrive_closes_sid_no_disable_no_critical():
    ws = _CountingWs()
    manager = OrderbookManagerV2(ws)
    rid = await _enter_recovery_multi(manager, ["A", "B", "C"])

    crit, sink = _capture("CRITICAL")
    try:
        for t in ["A", "B", "C"]:
            await manager.handle_message(_snapshot(t, sid=1, seq=100, req_id=rid))
    finally:
        logger.remove(sink)

    assert 1 not in manager._recovering  # cerró
    assert 1 not in manager._recovery_disabled_sids  # no se deshabilitó
    assert crit == []  # cero CRITICAL


# =====================================================
# (b) Settlement parcial: los atascados → dead, el resto recupera, INFO (no CRITICAL, no 5×)
# =====================================================


async def test_settled_ticker_stuck_marked_dead_and_rest_recovers():
    ws = _CountingWs()
    manager = OrderbookManagerV2(ws, recovery_timeout_sec=30.0)
    rid = await _enter_recovery_multi(manager, ["A", "B", "SETTLED"])

    # A y B recuperan; SETTLED nunca manda snapshot (mercado cerrado).
    await manager.handle_message(_snapshot("A", sid=1, seq=100, req_id=rid))
    await manager.handle_message(_snapshot("B", sid=1, seq=101, req_id=rid))
    assert manager._recovery_progress(1).startswith("recovered=2/3")  # parcial real

    info, isink = _capture("INFO")
    crit, csink = _capture("CRITICAL")
    try:
        manager._recovery_started_at[1] = time.monotonic() - 100.0  # forzar timeout
        await manager.handle_message(_delta("A", sid=1, seq=200))  # dispara el watchdog
        # Retry: _start_recovery pidió solo {A, B} (SETTLED ya es dead). Llegan por fallback.
        await manager.handle_message(_snapshot("A", sid=1, seq=300))
        await manager.handle_message(_snapshot("B", sid=1, seq=301))
    finally:
        logger.remove(isink)
        logger.remove(csink)

    assert "SETTLED" in manager._dead_tickers  # el atascado quedó marcado dead
    assert 1 not in manager._recovering  # el resto recuperó → cerró
    assert 1 not in manager._recovery_disabled_sids  # NO se deshabilitó
    assert manager._recovery_failures_by_sid.get(1, 0) == 0  # counter reseteado (recovery OK)
    assert crit == []  # SIN CRITICAL por 1 mercado settled
    assert any("recovery_stuck_marked_dead" in m for m in info)


# =====================================================
# (c) Feed muerto (recovered=0): escala a disable + CRITICAL (anti-OOM intacto)
# =====================================================


async def test_dead_feed_recovered_zero_escalates_to_critical_disable():
    ws = _CountingWs()
    manager = OrderbookManagerV2(ws, recovery_timeout_sec=30.0)
    await _enter_recovery_multi(manager, ["A", "B"])  # ningún snapshot de recovery llegará

    crit, csink = _capture("CRITICAL")
    try:
        # 5 timeouts consecutivos SIN ningún snapshot recuperado → circuit breaker.
        for seq in range(200, 260, 10):
            manager._recovery_started_at[1] = time.monotonic() - 100.0
            await manager.handle_message(_delta("A", sid=1, seq=seq))
            if 1 in manager._recovery_disabled_sids:
                break
    finally:
        logger.remove(csink)

    assert 1 in manager._recovery_disabled_sids  # deshabilitado (anti-OOM)
    assert (
        "A" not in manager._dead_tickers and "B" not in manager._dead_tickers
    )  # NO se matan (recovered=0)
    disabled = next(m for m in crit if "v2.recovery_disabled" in m)
    assert "recovered=0/2" in disabled  # progress REAL, no el artefacto 2/2


# =====================================================
# (d) El log de disable reporta el progress REAL (no el artefacto post-cleanup)
# =====================================================


async def test_disable_log_reports_real_progress_not_postcleanup_artifact():
    """El bug del ticket: `recovered=242/242` en el CRITICAL era basura (progress recomputado
    tras el cleanup → pending=0). Con progreso real 0/2 el disable debe decir 0/2, no 2/2."""
    ws = _CountingWs()
    manager = OrderbookManagerV2(ws, recovery_timeout_sec=30.0)
    await _enter_recovery_multi(manager, ["A", "B"])

    crit, csink = _capture("CRITICAL")
    try:
        for seq in range(200, 260, 10):
            manager._recovery_started_at[1] = time.monotonic() - 100.0
            await manager.handle_message(_delta("A", sid=1, seq=seq))
            if 1 in manager._recovery_disabled_sids:
                break
    finally:
        logger.remove(csink)

    disabled = next(m for m in crit if "v2.recovery_disabled" in m)
    assert "recovered=0/2" in disabled
    assert "recovered=2/2" not in disabled  # el artefacto NO debe aparecer
