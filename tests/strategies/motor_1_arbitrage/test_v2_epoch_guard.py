"""
Guard de ÉPOCA de la recovery (incidente 2026-07-29 — la tormenta de 6.265 recoveries/día).

Mecanismo medido en producción: los snapshots TARDÍOS de un intento de recovery muerto
(abortado o cerrado) caían al fallback por sid y TACHABAN tickers del intento VIVO con
bases 10-25s viejas. Cada intento "completaba" en ~9.5s consumiendo ~2/3 de snapshots
ajenos (el ciclo real tarda ~13.8s); el drain aplicaba los deltas recientes sobre esas
bases rancias → faltantes de ~575 contratos en los books de temporada → desync →
cuarentena → OTRA recovery. Cadena 1:1: 6.267 qty<0 → 5.999 cuarentenas → 6.275 gaps →
6.265 recoveries, 41% arrancando a <2s de la anterior.

El guard: un snapshot con `id` que NO está pendiente es un eco de intento muerto → se
IGNORA por completo (ni tacha, ni aplica, ni dispara gap). El fallback por sid queda SOLO
para snapshots SIN id (incidente 2026-05-28, que sigue cubierto).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.strategies.motor_1_arbitrage.orderbook_manager_v2 import OrderbookManagerV2, SidGapError

A, B = "KXMLB-26-LAD", "KXMLB-26-NYY"


def _snapshot(ticker: str, *, seq: int, yes_bid: int = 40, req_id: int | None = None) -> dict:
    msg: dict = {
        "type": "orderbook_snapshot",
        "sid": 1,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "yes_dollars_fp": [[f"0.{yes_bid:02d}00", "500.00"]],
            "no_dollars_fp": [["0.4000", "500.00"]],
        },
    }
    if req_id is not None:
        msg["id"] = req_id
    return msg


def _delta(ticker: str, *, seq: int) -> dict:
    return {
        "type": "orderbook_delta",
        "sid": 1,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "price_dollars": "0.4000",
            "delta_fp": "10.00",
            "side": "yes",
        },
    }


@pytest.fixture
def ws() -> AsyncMock:
    w = AsyncMock()
    w.send_command.side_effect = list(range(100, 400))
    return w


async def _into_recovery(manager: OrderbookManagerV2, tickers: list[str]) -> None:
    for i, t in enumerate(tickers, start=1):
        await manager.handle_message(_snapshot(t, seq=i))
    with pytest.raises(SidGapError):
        await manager.handle_message(_delta(tickers[0], seq=999))
    assert 1 in manager._recovering


# =====================================================
# LA TORMENTA: el eco del intento muerto no puede tachar al vivo
# =====================================================


async def test_eco_de_intento_muerto_no_tacha_ni_aplica(ws):
    """El caso de producción: el intento 1 aborta; sus snapshots tardíos llegan durante el
    intento 2. NO deben descontar tickers del intento 2, NO deben aplicar su base vieja, y
    la recovery NO debe cerrar hasta que lleguen los snapshots PROPIOS."""
    manager = OrderbookManagerV2(ws, recovery_timeout_sec=30.0)
    await _into_recovery(manager, [A, B])
    old_req = manager._pending_req_id_for_sid(1)
    assert old_req is not None

    # El intento 1 aborta (timeout simulado) → re-pide con req_id NUEVO.
    manager._recovery_started_at[1] = -1000.0
    await manager.handle_message(_delta(A, seq=1000))  # watchdog dispara el abort
    new_req = manager._pending_req_id_for_sid(1)
    assert new_req is not None and new_req != old_req

    # ECO TARDÍO del intento muerto, con base VIEJA (yes_bid=10, seq viejo).
    await manager.handle_message(_snapshot(A, seq=5, yes_bid=10, req_id=old_req))
    await manager.handle_message(_snapshot(B, seq=6, yes_bid=10, req_id=old_req))

    assert 1 in manager._recovering  # NO cerró con ecos
    pending = {t for _r, (s, p) in manager._pending_snapshot_requests.items() if s == 1 for t in p}
    assert pending == {A, B}  # NADA tachado
    assert manager._stale_snapshots_ignored == 2  # instrumentado
    assert manager.get_top_of_book(A, "yes") is None  # sigue stale: no se aplicó base vieja

    # Llegan los snapshots PROPIOS del intento vivo → recién ahí cierra, con base fresca.
    await manager.handle_message(_snapshot(A, seq=2000, yes_bid=44, req_id=new_req))
    await manager.handle_message(_snapshot(B, seq=2001, yes_bid=44, req_id=new_req))
    assert 1 not in manager._recovering
    top = manager.get_top_of_book(A, "yes")
    assert top is not None and top.best_bid.price_cents == 44  # la base es la FRESCA


async def test_eco_post_cierre_no_dispara_gap_ni_recovery(ws):
    """Amplificador #2 de la tormenta: un eco que aterriza DESPUÉS de cerrar la recovery
    caía en la detección de gap (seq inesperado) y disparaba OTRA recovery. Ignorado."""
    manager = OrderbookManagerV2(ws)
    await _into_recovery(manager, [A])
    req = manager._pending_req_id_for_sid(1)
    await manager.handle_message(_snapshot(A, seq=1500, yes_bid=44, req_id=req))
    assert 1 not in manager._recovering
    sends = ws.send_command.await_count

    # Eco tardío con id desconocido y seq que NO es el esperado → antes: gap → recovery.
    await manager.handle_message(_snapshot(A, seq=800, yes_bid=10, req_id=9999))

    assert ws.send_command.await_count == sends  # CERO recoveries nuevas
    assert 1 not in manager._recovering
    top = manager.get_top_of_book(A, "yes")
    assert top is not None and top.best_bid.price_cents == 44  # el estado fresco quedó intacto
    assert manager._stale_snapshots_ignored == 1


# =====================================================
# CONTROLES de no-regresión
# =====================================================


async def test_fallback_sin_id_sigue_completando(ws):
    """Incidente 2026-05-28: snapshots SIN id (Kalshi puede omitirlo) DEBEN seguir
    completando la recovery vía el fallback por sid — el guard no los toca."""
    manager = OrderbookManagerV2(ws)
    await _into_recovery(manager, [A, B])

    await manager.handle_message(_snapshot(A, seq=1500, yes_bid=44))  # sin req_id
    await manager.handle_message(_snapshot(B, seq=1501, yes_bid=44))  # sin req_id

    assert 1 not in manager._recovering  # completó por fallback, como siempre
    assert manager._stale_snapshots_ignored == 0


async def test_snapshot_del_intento_vivo_ruta_normal(ws):
    """CONTROL: el snapshot con el id CORRECTO del intento vivo se procesa como siempre."""
    manager = OrderbookManagerV2(ws)
    await _into_recovery(manager, [A])
    req = manager._pending_req_id_for_sid(1)
    await manager.handle_message(_snapshot(A, seq=1500, yes_bid=45, req_id=req))
    assert 1 not in manager._recovering
    assert manager._stale_snapshots_ignored == 0
    assert manager._recoveries_completed == 1  # y el cierre ya no es silencioso


async def test_stats_exponen_instrumentacion_del_ciclo(ws):
    """Los contadores del forense: ecos ignorados + recoveries completadas, en stats()
    (y por #196, automáticamente en /status)."""
    manager = OrderbookManagerV2(ws)
    s = manager.stats()
    assert s["stale_snapshots_ignored_total"] == 0
    assert s["recoveries_completed_total"] == 0
