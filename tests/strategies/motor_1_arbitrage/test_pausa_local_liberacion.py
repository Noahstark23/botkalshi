"""
SALIDA de la pausa local por huérfana (2026-08-06) — regla 11 de desarrollo-bot,
violada por la propia pausa proporcional de #200.

Caso real del 05-ago: rollback abortado por slippage 11.2% a las 21:02:18 → pausa
local; M3 vendió la huérfana a las 21:04:40 (+4¢ — la política proporcional pasó su
primera prueba en producción); la pausa siguió 5.5 HORAS citando una huérfana
inexistente — 27 intentos rechazados, 479 señales tiradas, y el n del mes sesgado.

Condición de liberación (fail-closed en cada pata): gracia mínima cumplida (el
PortfolioPoller tuvo que haber VISTO la posición — una tabla sin sincronizar parece
flat) + NO queda fila en portfolio_positions para el ticker de la huérfana. Solo toca
la pausa LOCAL: el kill-switch global sigue siendo de clear_kill_switch.py.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.monitoring.health import BotState
from src.strategies.motor_1_arbitrage.executor import ArbitrageExecutor


@pytest.fixture(autouse=True)
def reset_botstate():
    BotState.motor1_local_pause = None
    yield
    BotState.motor1_local_pause = None


def _executor_pausado(hace_seg: float = 300.0) -> ArbitrageExecutor:
    ex = ArbitrageExecutor(AsyncMock(), MagicMock())
    ex._motor_paused = True
    ex._motor_pause_reason = "rollback_aborted_slippage: TORHOU-TOR — pata huérfana"
    ex._motor_pause_orphan_ticker = "TORHOU-TOR"
    ex._motor_paused_at_mono = time.monotonic() - hace_seg
    BotState.motor1_local_pause = ex._motor_pause_reason
    return ex


def _db_devuelve(fila):
    """Mockea get_session → session.exec(...).first() == fila."""
    session = MagicMock()
    session.exec.return_value.first.return_value = fila
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


async def test_huerfana_cerrada_libera_la_pausa():
    """EL CASO DEL 05-AGO: la huérfana ya no está en portfolio_positions → la pausa se
    libera en el mismo intento, con rastro persistente y visible en /status."""
    ex = _executor_pausado()
    with (
        patch(
            "src.strategies.motor_1_arbitrage.executor.get_session",
            side_effect=[_db_devuelve(None), _db_devuelve(None)],
        ),
        patch(
            "src.strategies.motor_1_arbitrage.executor.alert_risk_event", new=AsyncMock()
        ) as alerta,
    ):
        liberada = await ex._maybe_release_local_pause()

    assert liberada is True
    assert ex._motor_paused is False
    assert ex._motor_pause_reason is None
    assert BotState.motor1_local_pause is None  # el freno visible también se limpia
    alerta.assert_awaited_once()


async def test_huerfana_abierta_mantiene_la_pausa():
    """CONTROL: mientras la fila exista en portfolio_positions, el motivo está vigente."""
    ex = _executor_pausado()
    with patch(
        "src.strategies.motor_1_arbitrage.executor.get_session",
        return_value=_db_devuelve(MagicMock()),  # la posición sigue abierta
    ):
        assert await ex._maybe_release_local_pause() is False
    assert ex._motor_paused is True
    assert BotState.motor1_local_pause is not None


async def test_error_de_db_mantiene_la_pausa():
    """FAIL-CLOSED: soltar un freno exige certeza — un error de lectura no libera."""
    ex = _executor_pausado()
    with patch(
        "src.strategies.motor_1_arbitrage.executor.get_session",
        side_effect=RuntimeError("db caída"),
    ):
        assert await ex._maybe_release_local_pause() is False
    assert ex._motor_paused is True


async def test_gracia_minima_no_cumplida_no_libera():
    """CONTROL: antes de LOCAL_PAUSE_MIN_SEC no se libera aunque la tabla esté vacía —
    el poller (ciclo 60s) tiene que haber podido VER la posición; una tabla aún no
    sincronizada se leería como flat y liberaría en falso."""
    ex = _executor_pausado(hace_seg=10.0)
    with patch("src.strategies.motor_1_arbitrage.executor.get_session") as gs:
        assert await ex._maybe_release_local_pause() is False
    gs.assert_not_called()  # ni siquiera consulta la DB
    assert ex._motor_paused is True


async def test_pausa_sin_metadata_solo_la_libera_un_humano():
    """CONTROL: una pausa local sin ticker registrado (estado legacy) no se auto-libera."""
    ex = _executor_pausado()
    ex._motor_pause_orphan_ticker = None
    assert await ex._maybe_release_local_pause() is False
    assert ex._motor_paused is True


async def test_no_toca_el_kill_switch_global():
    """LÍNEA ROJA: la liberación es de la pausa LOCAL — is_paused global y el kill-switch
    persistente quedan intactos (solo clear_kill_switch.py los levanta)."""
    ex = _executor_pausado()
    BotState.is_paused = True
    BotState.pause_reason = "kill_switch: huérfana grande"
    with (
        patch(
            "src.strategies.motor_1_arbitrage.executor.get_session",
            side_effect=[_db_devuelve(None), _db_devuelve(None)],
        ),
        patch("src.strategies.motor_1_arbitrage.executor.alert_risk_event", new=AsyncMock()),
    ):
        await ex._maybe_release_local_pause()

    assert BotState.is_paused is True  # el global NO se tocó
    assert BotState.pause_reason == "kill_switch: huérfana grande"
    BotState.is_paused = False
    BotState.pause_reason = None
