"""
Guard de CONFIANZA del book por ticker (forense 2026-08-06) — el fix de plata de M1.

La evidencia: 44 intentos consecutivos con CERO arbitrajes completados. El 53% que
tocó el exchange murió limpio (pata dura FOK-kill, gratis); el 47% llenó la dura y la
"fácil" NO EXISTÍA — FOK sin volumen en 141ms desde la detección, 9¢ de vacío al
deshacer. La prueba pericial del evento 21:02: el edge de 3.19% y un `book_incoherent
cruce=11¢` 0.4s antes eran EL MISMO BOOK. M1 no pierde carreras: le compra a
profundidad fantasma de books recién re-baseados (el empalme pierde también removals
→ book INFLADO sin desincronizar — la mitad silenciosa que ningún detector ve).

El guard: un ticker con incidente PROPIO reciente (desync, incoherencia, desync en
drain, clamp del empalme) no ejecuta hasta llevar MOTOR_1_BOOK_TRUST_SEC sin
incidentes. La detección y el EdgeWindow se graban igual (shadow intacto); el techo
anti-fantasma no cubre este caso porque el cruce fantasma de 2-5¢ cae DENTRO de la
banda plausible.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.strategies.motor_1_arbitrage.engine import Motor1Engine
from src.strategies.motor_1_arbitrage.orderbook import OrderbookDesyncError
from src.strategies.motor_1_arbitrage.orderbook_manager_v2 import OrderbookManagerV2


def _snapshot(ticker: str, sid: int = 1, seq: int = 1) -> dict:
    return {
        "type": "orderbook_snapshot",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "yes_dollars_fp": [["0.50", "100.00"]],
            "no_dollars_fp": [],
        },
    }


def _delta(ticker: str, seq: int, sid: int = 1, delta: str = "10.00") -> dict:
    return {
        "type": "orderbook_delta",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "price_dollars": "0.50",
            "delta_fp": delta,
            "side": "yes",
        },
    }


# =====================================================
# El manager registra incidentes por ticker
# =====================================================


async def test_desync_registra_incidente_del_ticker():
    ws = AsyncMock()
    ws.send_command.side_effect = list(range(42, 400))
    manager = OrderbookManagerV2(ws, seam_grace_sec=0.0)
    await manager.handle_message(_snapshot("TICK", seq=1))
    assert manager.book_incident_age("TICK") is None  # book limpio: sin incidentes

    with pytest.raises(OrderbookDesyncError):
        await manager.handle_message(_delta("TICK", seq=2, delta="-500.00"))

    edad = manager.book_incident_age("TICK")
    assert edad is not None and edad < 1.0  # incidente recién registrado


async def test_clamp_del_empalme_registra_incidente():
    ws = AsyncMock()
    ws.send_command.side_effect = list(range(42, 400))
    manager = OrderbookManagerV2(ws)  # gracia default: el underflow clampea
    await manager.handle_message(_snapshot("TICK", seq=1))

    await manager.handle_message(_delta("TICK", seq=2, delta="-500.00"))  # clamp, no raise

    assert manager.stats()["seam_clamps_total"] == 1
    edad = manager.book_incident_age("TICK")
    assert edad is not None and edad < 1.0  # el clamp también es incidente


async def test_ticker_ajeno_no_hereda_incidentes():
    ws = AsyncMock()
    ws.send_command.side_effect = list(range(42, 400))
    manager = OrderbookManagerV2(ws, seam_grace_sec=0.0)
    await manager.handle_message(_snapshot("SANO", seq=1))
    await manager.handle_message(_snapshot("ROTO", seq=2))

    with pytest.raises(OrderbookDesyncError):
        await manager.handle_message(_delta("ROTO", seq=3, delta="-500.00"))

    assert manager.book_incident_age("ROTO") is not None
    assert manager.book_incident_age("SANO") is None  # el incidente es POR ticker


# =====================================================
# El engine corta SOLO la ejecución (el shadow sigue)
# =====================================================


def _engine_con_arb(trust_sec: float, incidente_hace: float | None):
    """Engine LIVE con un arb detectable en 'TICK' y el incidente configurado."""
    manager = MagicMock()
    manager.tracked_tickers = ["TICK"]
    top_yes = MagicMock()
    top_yes.best_bid.price_cents = 60  # no_ask_synth = 40
    top_yes.best_bid.size = 50
    top_no = MagicMock()
    top_no.best_bid.price_cents = 45  # yes_ask_synth = 55 → 55+40=95 < 100: arb
    top_no.best_bid.size = 50
    manager.get_top_of_book.side_effect = lambda t, side: top_yes if side == "yes" else top_no
    manager.book_incident_age.return_value = None if incidente_hace is None else incidente_hace

    settings = MagicMock()
    settings.TRADING_ENABLED = True
    settings.MIN_EDGE_PCT = 1.0
    settings.MOTOR_1_EXECUTION_EDGE_PCT = 1.0
    settings.MOTOR_1_MAX_EDGE_PCT = 10.0
    settings.MOTOR_1_CONFIRM_TICKS = 1
    settings.MOTOR_1_TICKER_COOLDOWN_SEC = 60.0
    settings.MOTOR_1_BOOK_TRUST_SEC = trust_sec

    with patch("src.strategies.motor_1_arbitrage.engine.get_settings", return_value=settings):
        engine = Motor1Engine(manager, executor=AsyncMock())
    engine.settings = settings
    engine._record_edge_window = MagicMock(return_value=7)  # aisla la DB del test
    return engine


async def test_incidente_reciente_corta_la_ejecucion_pero_no_el_shadow():
    """EL CASO 21:02: incidente 0.4s antes → el edge se DETECTA y graba, pero NO se
    ejecuta — la huérfana de 80¢→71¢ no habría existido."""
    engine = _engine_con_arb(trust_sec=60.0, incidente_hace=0.4)

    await engine._tick()

    assert engine._signals_seen == 1  # la señal se vio
    engine._record_edge_window.assert_called_once()  # y el shadow la grabó
    assert engine._skips_untrusted == 1  # pero la ejecución se cortó
    assert engine._exec_task is None  # sin orden lanzada


async def test_book_con_historia_limpia_ejecuta():
    """CONTROL: sin incidentes registrados (None) el guard no frena nada."""
    engine = _engine_con_arb(trust_sec=60.0, incidente_hace=None)

    await engine._tick()

    assert engine._skips_untrusted == 0
    assert engine._exec_task is not None  # la ejecución se lanzó
    engine._exec_task.cancel()


async def test_incidente_viejo_ya_no_frena():
    """CONTROL: pasado el período de confianza, el ticker vuelve al ruedo solo."""
    engine = _engine_con_arb(trust_sec=60.0, incidente_hace=61.0)

    await engine._tick()

    assert engine._skips_untrusted == 0
    assert engine._exec_task is not None
    engine._exec_task.cancel()


async def test_trust_cero_desactiva_el_guard():
    """CONTROL de config: MOTOR_1_BOOK_TRUST_SEC=0 = comportamiento pre-fix."""
    engine = _engine_con_arb(trust_sec=0.0, incidente_hace=0.4)

    await engine._tick()

    assert engine._skips_untrusted == 0
    assert engine._exec_task is not None
    engine._exec_task.cancel()
