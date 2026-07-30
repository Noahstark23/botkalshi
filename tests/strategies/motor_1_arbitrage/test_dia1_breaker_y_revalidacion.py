"""
Los tres fixes del día 1 del mes (incidente 2026-07-30).

El día 1 duró 52 minutos: 3 rollbacks LIMPIOS ($0.21 total, CERO huérfanas) — todos por la
segunda pata FOK rebotando 409 a 33-35ms de la primera — dispararon un breaker hardcodeado
(3/60min) sin ningún resume automático → 12 de 13 horas de bot muerto.

  1. REVALIDACIÓN T-0: el task de ejecución re-detecta del book VIVO y ejecuta el arb
     FRESCO; si el cruce murió en la cola, skip limpio sin órdenes.
  2. BREAKER configurable por settings + conteo separado limpio/abortado + auto-resume
     CONDICIONADO (flag off por default; jamás toca el kill-switch; tope diario = escalada).
  3. El preflight de arranque_mes deja de ser ciego a la pausa runtime (probado en
     tests/scripts/).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.storage.models as models
from src.math.arbitrage import ArbLeg, ArbOpportunity
from src.monitoring.health import BotState
from src.strategies.motor_1_arbitrage.engine import Motor1Engine
from src.strategies.motor_1_arbitrage.executor import ArbitrageExecutor


@pytest.fixture(autouse=True)
def _db_y_botstate(tmp_path, monkeypatch):
    engine = models.create_engine(f"sqlite:///{tmp_path / 'dia1.db'}")
    models.SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(models, "_engine", engine)
    BotState.is_paused = False
    BotState.pause_reason = None
    yield
    BotState.is_paused = False
    BotState.pause_reason = None


def _opp(net: int = 5, count: int = 2) -> ArbOpportunity:
    legs = (
        ArbLeg(
            market_ticker="KXT-1", side="yes", price_cents=45, count=count, available_size=count
        ),
        ArbLeg(market_ticker="KXT-1", side="no", price_cents=50, count=count, available_size=count),
    )
    return ArbOpportunity(
        legs=legs,
        count=count,
        gross_profit_cents=10,
        fees_cents=5,
        net_profit_cents=net,
        edge_pct=2.0,
    )


def _risk_event(event_type: str, minutes_ago: float = 1.0) -> None:
    with models.get_session() as s:
        ev = models.RiskEvent(event_type=event_type, severity="warning", message="test")
        s.add(ev)
        s.commit()
        # backdate manual (triggered_at tiene default)
        ev.triggered_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=minutes_ago)
        s.add(ev)
        s.commit()


def _settings(**overrides):
    s = MagicMock()
    s.MOTOR_1_BREAKER_THRESHOLD = 3
    s.MOTOR_1_BREAKER_WINDOW_MIN = 60.0
    s.MOTOR_1_BREAKER_COUNT_CLEAN_ROLLBACKS = True
    s.MOTOR_1_BREAKER_AUTO_RESUME = False
    s.MOTOR_1_BREAKER_MAX_RESUMES_PER_DAY = 3
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _executor() -> ArbitrageExecutor:
    return ArbitrageExecutor(AsyncMock(), MagicMock())


# =====================================================
# Fix 1 — REVALIDACIÓN T-0 en el engine
# =====================================================


class _EngineHarness(Motor1Engine):
    """Engine con detect controlable y executor espiado."""

    def __init__(self, fresh):
        # No llamamos super().__init__ (requiere manager/settings): seteamos lo mínimo.
        self._fresh = fresh
        self._executor = AsyncMock()
        self._executor.execute = AsyncMock(return_value=True)
        import asyncio

        self._exec_lock = asyncio.Lock()
        self._executing = False
        self._exec_task = None
        self._cooldown_until = {}
        self._streak = {}
        self.settings = MagicMock(MOTOR_1_TICKER_COOLDOWN_SEC=58.0)

    def _detect(self, ticker):
        return self._fresh

    def _update_edge_window_outcome(self, edge_id, filled):
        self.last_outcome = (edge_id, filled)


async def test_revalidacion_cruce_muerto_skip_limpio():
    """El caso del incidente: el arb detectado envejeció en la cola. Re-detect da None →
    CERO órdenes (ni la pata dura se envía), cero rollback, cero breaker."""
    h = _EngineHarness(fresh=None)
    await h._execute_and_record(_opp(), edge_id=7)
    h._executor.execute.assert_not_awaited()
    assert h.last_outcome == (7, False)


async def test_revalidacion_ejecuta_el_arb_fresco():
    """Si el cruce sigue vivo pero CAMBIÓ (count/net), se ejecuta la versión FRESCA del
    book, no la vieja de la cola."""
    fresh = _opp(net=3, count=1)
    h = _EngineHarness(fresh=fresh)
    stale = _opp(net=10, count=5)
    await h._execute_and_record(stale, edge_id=1)
    h._executor.execute.assert_awaited_once_with(fresh)  # el fresco, no el stale


# =====================================================
# Fix 2 — breaker configurable + conteo separado
# =====================================================


async def test_count_clean_false_ignora_rollbacks_limpios():
    """Con COUNT_CLEAN_ROLLBACKS=false, 3 rollbacks LIMPIOS (el caso del día 1: $0.21,
    0 huérfanas) NO pausan el bot. Un abortado en ventana seguiría contando."""
    ex = _executor()
    _risk_event("atomic_rollback", 5)
    _risk_event("atomic_rollback", 10)
    with patch(
        "src.strategies.motor_1_arbitrage.executor.get_settings",
        return_value=_settings(MOTOR_1_BREAKER_COUNT_CLEAN_ROLLBACKS=False),
    ):
        await ex._check_circuit_breaker()  # inserta el 3ro y evalúa
    assert BotState.is_paused is False  # 3 limpios, 0 contados → sin pausa


async def test_count_clean_true_mantiene_comportamiento_historico(monkeypatch):
    import src.strategies.motor_1_arbitrage.executor as mod

    monkeypatch.setattr(mod, "alert_risk_event", AsyncMock())
    ex = _executor()
    _risk_event("atomic_rollback", 5)
    _risk_event("atomic_rollback", 10)
    with patch(
        "src.strategies.motor_1_arbitrage.executor.get_settings",
        return_value=_settings(),
    ):
        await ex._check_circuit_breaker()
    assert BotState.is_paused is True
    assert (BotState.pause_reason or "").startswith("circuit_breaker")


async def test_abortado_siempre_cuenta_aunque_clean_este_off(monkeypatch):
    import src.strategies.motor_1_arbitrage.executor as mod

    monkeypatch.setattr(mod, "alert_risk_event", AsyncMock())
    ex = _executor()
    _risk_event("rollback_aborted_slippage", 5)
    _risk_event("rollback_aborted_slippage", 6)
    with patch(
        "src.strategies.motor_1_arbitrage.executor.get_settings",
        return_value=_settings(
            MOTOR_1_BREAKER_COUNT_CLEAN_ROLLBACKS=False, MOTOR_1_BREAKER_THRESHOLD=3
        ),
    ):
        await ex._check_circuit_breaker()  # inserta 1 limpio (no cuenta) + 2 abortados = 2 < 3
    assert BotState.is_paused is False
    _risk_event("rollback_aborted_slippage", 1)  # 3er abortado
    with patch(
        "src.strategies.motor_1_arbitrage.executor.get_settings",
        return_value=_settings(
            MOTOR_1_BREAKER_COUNT_CLEAN_ROLLBACKS=False, MOTOR_1_BREAKER_THRESHOLD=3
        ),
    ):
        await ex._check_circuit_breaker()
    assert BotState.is_paused is True  # los abortados SIEMPRE cuentan


# =====================================================
# Fix 2b — auto-resume condicionado
# =====================================================


def _pausado_por_breaker():
    BotState.is_paused = True
    BotState.pause_reason = "circuit_breaker: 3+ rollbacks in 60min window"


async def test_auto_resume_off_por_default_no_despausa():
    ex = _executor()
    _pausado_por_breaker()
    with patch("src.strategies.motor_1_arbitrage.executor.get_settings", return_value=_settings()):
        ex._maybe_auto_resume()
    assert BotState.is_paused is True  # flag off (default) → no-op


async def test_auto_resume_ventana_vacia_despausa_y_persiste():
    """El fix del día 1: ventana vacía + 0 abortados + flag on → despausa, con RiskEvent
    de auditoría."""
    ex = _executor()
    _pausado_por_breaker()
    _risk_event("atomic_rollback", minutes_ago=120)  # FUERA de la ventana de 60
    with patch(
        "src.strategies.motor_1_arbitrage.executor.get_settings",
        return_value=_settings(MOTOR_1_BREAKER_AUTO_RESUME=True),
    ):
        ex._maybe_auto_resume()
    assert BotState.is_paused is False
    assert BotState.pause_reason is None
    with models.get_session() as s:
        from sqlmodel import select

        evs = list(
            s.exec(
                select(models.RiskEvent).where(models.RiskEvent.event_type == "breaker_auto_resume")
            )
        )
    assert len(evs) == 1


async def test_auto_resume_bloqueado_por_abortado_en_ventana():
    """CERO excepciones con huérfanas: un abortado en ventana bloquea el auto-resume."""
    ex = _executor()
    _pausado_por_breaker()
    _risk_event("rollback_aborted_slippage", minutes_ago=30)
    with patch(
        "src.strategies.motor_1_arbitrage.executor.get_settings",
        return_value=_settings(MOTOR_1_BREAKER_AUTO_RESUME=True),
    ):
        ex._maybe_auto_resume()
    assert BotState.is_paused is True


async def test_auto_resume_respeta_tope_diario():
    """La escalada: agotado el tope, queda pausado hasta un humano."""
    ex = _executor()
    ex._resumes_today, ex._resumes_day = 3, datetime.now(UTC).date()
    _pausado_por_breaker()
    with patch(
        "src.strategies.motor_1_arbitrage.executor.get_settings",
        return_value=_settings(MOTOR_1_BREAKER_AUTO_RESUME=True),
    ):
        ex._maybe_auto_resume()
    assert BotState.is_paused is True  # tope agotado → humano


async def test_auto_resume_jamas_toca_otras_pausas():
    """INVARIANTE DURA: el kill-switch (u otra pausa) NUNCA se auto-levanta desde acá."""
    ex = _executor()
    BotState.is_paused = True
    BotState.pause_reason = "kill-switch persistente: rollback_aborted_slippage COLSD"
    with patch(
        "src.strategies.motor_1_arbitrage.executor.get_settings",
        return_value=_settings(MOTOR_1_BREAKER_AUTO_RESUME=True),
    ):
        ex._maybe_auto_resume()
    assert BotState.is_paused is True  # intocable
