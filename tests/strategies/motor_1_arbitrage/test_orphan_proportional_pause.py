"""
Respuesta PROPORCIONAL a la pata huérfana de Motor 1 (2026-07-29).

Evidencia que la motiva: en 21 días hubo 3 rollbacks abortados por slippage (25% de los 12
partial fills), TODOS de 1 contrato (~$0.47), y cada uno disparó el kill-switch GLOBAL
persistente que paró el bot 14+ horas y exigió clear manual. El guard nació del incidente
2026-07-07 (~$135): a esa escala correcto, a esta desproporcionado.

Con `MOTOR_1_PROPORTIONAL_ORPHAN_PAUSE=true`:
  - huérfana < MOTOR_1_ORPHAN_KILL_SWITCH_USD → pausa SOLO Motor 1 (runtime), M3 la gestiona
  - huérfana >= umbral                        → kill-switch global (comportamiento histórico)
  - N abortos en 24h                          → escala al global aunque sean chicos
El RiskEvent y la alerta se emiten en AMBOS caminos (rastro forense intacto).
Default OFF: ablandar una capa de seguridad es decisión explícita del operador.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import select

from src.math.arbitrage import ArbLeg
from src.monitoring.health import BotState
from src.risk.manager import RiskManager
from src.storage.models import RiskEvent, get_session, kill_switch_engaged
from src.strategies.motor_1_arbitrage.executor import ArbitrageExecutor

LEG = ArbLeg(
    market_ticker="KXMLBGAME-26JUL282140COLSD-COL",
    side="no",
    price_cents=47,
    count=1,
    available_size=100,
)


@pytest.fixture(autouse=True)
def _clean_state(tmp_db_engine):
    """tmp_db_engine (conftest del paquete): SQLite temporal — este path graba RiskEvent y
    el kill-switch en DB, y cada test necesita empezar de cero."""
    BotState.is_paused = False
    BotState.pause_reason = None
    BotState.motor1_local_pause = None
    yield
    BotState.is_paused = False
    BotState.pause_reason = None
    BotState.motor1_local_pause = None


@pytest.fixture
def executor() -> ArbitrageExecutor:
    client = MagicMock()
    rm = MagicMock(spec=RiskManager)
    return ArbitrageExecutor(client, rm)


def _settings(*, proportional: bool, umbral: float = 5.0, escalate: int = 3) -> MagicMock:
    s = MagicMock()
    s.MOTOR_1_PROPORTIONAL_ORPHAN_PAUSE = proportional  # bool REAL (el path exige `is True`)
    s.MOTOR_1_ORPHAN_KILL_SWITCH_USD = umbral
    s.MOTOR_1_ORPHAN_ESCALATE_COUNT = escalate
    return s


async def _abort(executor: ArbitrageExecutor, settings: MagicMock, *, remaining: int) -> None:
    with (
        patch("src.strategies.motor_1_arbitrage.executor.get_settings", return_value=settings),
        patch(
            "src.strategies.motor_1_arbitrage.executor.alert_risk_event",
            new=AsyncMock(),
        ),
    ):
        await executor._pause_on_aborted_rollback(LEG, 10.6, remaining)


def _risk_events(event_type: str) -> list[RiskEvent]:
    with get_session() as s:
        return list(s.exec(select(RiskEvent).where(RiskEvent.event_type == event_type)))


# =====================================================
# Default OFF: el comportamiento histórico NO cambia
# =====================================================


async def test_default_off_mantiene_kill_switch_global(executor):
    """CONTROL CRÍTICO: sin el flag, una huérfana de 1 contrato sigue disparando el
    kill-switch global — el comportamiento del incidente 2026-07-07, intacto."""
    await _abort(executor, _settings(proportional=False), remaining=1)

    assert BotState.is_paused is True
    engaged, reason = kill_switch_engaged()
    assert engaged is True
    assert "rollback_aborted_slippage" in (reason or "")
    assert executor._motor_paused is False  # no usa la pausa local


# =====================================================
# Con el flag: huérfana chica → pausa SOLO Motor 1
# =====================================================


async def test_huerfana_chica_pausa_solo_motor_1(executor):
    """El caso de los 3 incidentes: 1 contrato a 47¢ = $0.47 < $5 → Motor 1 pausado,
    el bot sigue vivo y sin kill-switch global."""
    await _abort(executor, _settings(proportional=True), remaining=1)

    assert executor._motor_paused is True
    assert "$0.47" in (executor._motor_pause_reason or "")
    assert BotState.is_paused is False  # el bot NO se para
    # VISIBLE en /status: una pausa que no se ve es la misma clase de bug que las alertas
    # mudas (2026-07-25) y los contadores sin exponer (#196).
    assert BotState.motor1_local_pause is not None
    assert "$0.47" in BotState.motor1_local_pause
    engaged, _ = kill_switch_engaged()
    assert engaged is False  # sin clear manual
    # El rastro forense se graba igual (insumo de la escalada)
    eventos = _risk_events("rollback_aborted_slippage")
    assert len(eventos) == 1
    assert eventos[0].severity == "warning"  # no critical: es proporcional


async def test_motor_pausado_no_ejecuta(executor):
    """La pausa local tiene que CORTAR de verdad: execute() sale antes de tocar riesgo o red."""
    await _abort(executor, _settings(proportional=True), remaining=1)
    assert executor._motor_paused is True

    opp = MagicMock()
    result = await executor.execute(opp)

    assert result is False
    executor.risk_manager.check_pre_trade.assert_not_called()  # cortó ANTES del risk check


# =====================================================
# Con el flag: huérfana GRANDE → kill-switch global igual
# =====================================================


async def test_huerfana_grande_dispara_global(executor):
    """20 contratos a 47¢ = $9.40 >= $5 → el kill-switch global sigue siendo la respuesta."""
    await _abort(executor, _settings(proportional=True), remaining=20)

    assert BotState.is_paused is True
    engaged, _ = kill_switch_engaged()
    assert engaged is True
    assert executor._motor_paused is False
    assert _risk_events("rollback_aborted_slippage")[0].severity == "critical"


# =====================================================
# Anti-acumulación: la N-ésima chica escala al global
# =====================================================


async def test_repeticion_escala_al_global(executor):
    """Una huérfana chica REPETIDA no es un accidente: es un mercado roto. Con
    escalate=3, la tercera dispara el kill-switch global aunque sea de $0.47."""
    s = _settings(proportional=True, escalate=3)

    await _abort(executor, s, remaining=1)  # 1ª: local
    assert BotState.is_paused is False
    executor._motor_paused = False  # simula el redeploy que limpia la pausa runtime

    await _abort(executor, s, remaining=1)  # 2ª: local
    assert BotState.is_paused is False
    executor._motor_paused = False

    await _abort(executor, s, remaining=1)  # 3ª: ESCALA
    assert BotState.is_paused is True
    engaged, _ = kill_switch_engaged()
    assert engaged is True
    assert len(_risk_events("rollback_aborted_slippage")) == 3


async def test_conteo_falla_escala_al_global(executor):
    """FAIL-CLOSED: si no se puede contar los abortos previos (DB caída), se asume lo peor
    y se escala al global — 'no sé' nunca debe ablandar el freno."""
    with patch.object(executor, "_recent_aborted_rollbacks", return_value=10**6):
        await _abort(executor, _settings(proportional=True), remaining=1)

    assert BotState.is_paused is True
    engaged, _ = kill_switch_engaged()
    assert engaged is True


async def test_settings_incompleto_cae_al_global_fail_closed(executor):
    """BUG CAZADO EN QA (2026-07-29): el path leía el flag por TRUTHINESS, y un settings
    mockeado/incompleto devuelve un atributo truthy → tomaba la rama BLANDA. Eso es
    fail-OPEN en un control de seguridad. Con `is True`, cualquier cosa que no sea un
    True explícito de Pydantic cae al kill-switch global histórico."""
    s = MagicMock()  # todos los atributos truthy, ninguno es un bool real
    await _abort(executor, s, remaining=1)

    assert BotState.is_paused is True
    engaged, _ = kill_switch_engaged()
    assert engaged is True
    assert executor._motor_paused is False


async def test_settings_que_explota_cae_al_global_fail_closed(executor):
    """BUG (b) CAZADO EN QA (2026-07-29): la lectura de settings en el path de pausa puede
    EXPLOTAR (el executor también vive en contextos sin env completo, ej. el reconcile de
    boot) y la excepción la traga el loop de rollback → la pausa NO ocurriría, dejando el
    bot operando tras una huérfana. Todo el cómputo va envuelto: si falla, kill-switch
    GLOBAL."""
    with (
        patch(
            "src.strategies.motor_1_arbitrage.executor.get_settings",
            side_effect=RuntimeError("Settings no carga (falta KALSHI_API_KEY_ID)"),
        ),
        patch("src.strategies.motor_1_arbitrage.executor.alert_risk_event", new=AsyncMock()),
    ):
        await executor._pause_on_aborted_rollback(LEG, 88.9, 10)

    assert BotState.is_paused is True  # la pausa OCURRE pese al fallo de config
    engaged, _ = kill_switch_engaged()
    assert engaged is True
    assert executor._motor_paused is False
