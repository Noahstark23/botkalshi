"""Tests del ProductionRunner — reconcile en boot tolerante a fallas (Fase 6)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine

from src.monitoring.health import BotState
from src.runner import ProductionRunner


@pytest.fixture(autouse=True)
def reset_botstate():
    BotState.last_error = None
    BotState.last_error_at = None
    BotState.is_paused = False
    BotState.pause_reason = None
    yield
    BotState.last_error = None
    BotState.is_paused = False
    BotState.pause_reason = None


@pytest.fixture
def mock_runner_settings():
    """Parchea get_settings en runner para evitar validación de env vars."""
    with patch("src.runner.get_settings") as m:
        s = MagicMock()
        s.KALSHI_ENV = "demo"
        s.ACTIVE_CAPITAL_USD = 300.0
        s.TRADING_ENABLED = False
        s.MOTOR_1_ARBITRAGE_ENABLED = False
        s.MOTOR_2_SPORTSBOOK_ENABLED = False
        s.MOTOR_3_CLV_ENABLED = False
        s.HEALTH_HOST = "0.0.0.0"
        s.HEALTH_PORT = 8080
        m.return_value = s
        yield s


@pytest.mark.asyncio
async def test_reconcile_on_boot_calls_initialize(mock_runner_settings):
    """Durante boot, ArbitrageExecutor.initialize() debe ser invocado."""
    with (
        patch("src.runner.KalshiRestClient") as mock_rest_cls,
        patch("src.runner.RiskManager"),
        patch("src.runner.ArbitrageExecutor") as mock_exec_cls,
    ):
        mock_rest_cls.return_value.__aenter__ = AsyncMock(return_value=mock_rest_cls.return_value)
        mock_rest_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        mock_executor = mock_exec_cls.return_value
        mock_executor.initialize = AsyncMock()

        runner = ProductionRunner()
        await runner._reconcile_on_boot()

        mock_executor.initialize.assert_called_once()


@pytest.mark.asyncio
async def test_reconcile_failure_does_not_crash_boot(mock_runner_settings):
    """
    Si ArbitrageExecutor.initialize() lanza excepcion, _reconcile_on_boot
    debe capturarla, registrarla en BotState.last_error, y NO re-levantar.

    Un Kalshi flap al arranque NO debe impedir que el bot arranque.
    """
    with (
        patch("src.runner.KalshiRestClient") as mock_rest_cls,
        patch("src.runner.RiskManager"),
        patch("src.runner.ArbitrageExecutor") as mock_exec_cls,
    ):
        mock_rest_cls.return_value.__aenter__ = AsyncMock(return_value=mock_rest_cls.return_value)
        mock_rest_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        mock_executor = mock_exec_cls.return_value
        mock_executor.initialize = AsyncMock(side_effect=RuntimeError("Kalshi unreachable at boot"))

        runner = ProductionRunner()

        # NO debe re-raise
        await runner._reconcile_on_boot()

        assert BotState.last_error is not None
        assert "Reconcile" in BotState.last_error or "Kalshi" in BotState.last_error


@pytest.mark.parametrize(
    ("trading", "execution", "expected"),
    [
        (True, True, True),  # ambos on → Motor 3 VENDE
        (True, False, False),  # trading global on pero execution off → SHADOW (el caso clave)
        (False, True, False),  # execution on pero trading global off → shadow
        (False, False, False),
    ],
)
@pytest.mark.asyncio
async def test_motor3_execution_requires_both_flags(
    mock_runner_settings, trading, execution, expected
):
    """
    Motor 3 solo vende con TRADING_ENABLED **y** MOTOR_3_EXECUTION_ENABLED ambos True.
    El caso central: con el trading global ON (Motor 2 live) pero execution OFF, Motor 3
    corre en SHADOW (detecta + loguea, jamás vende) → se puede validar sin riesgo.
    """
    s = mock_runner_settings
    s.MOTOR_3_CLV_ENABLED = True
    s.TRADING_ENABLED = trading
    s.MOTOR_3_EXECUTION_ENABLED = execution
    s.MOTOR_3_TAKE_PROFIT_ENABLED = False
    s.MOTOR_3_TAKE_PROFIT_CENTS = 90

    mock_engine = MagicMock()
    mock_engine.run = AsyncMock()
    with (
        patch(
            "src.strategies.motor_3_clv.engine.Motor3Engine", return_value=mock_engine
        ) as mock_cls,
        patch("src.runner.asyncio.sleep", new=AsyncMock()),
    ):
        runner = ProductionRunner()
        await runner._run_motor3_clv()

    # trading_enabled mantiene el consenso de 2 flags; el take-profit se pasa aparte.
    assert mock_cls.call_args.kwargs["trading_enabled"] is expected
    mock_engine.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_motor3_take_profit_flags_passed_to_engine(mock_runner_settings):
    """FASE 1: los flags de take-profit (ENABLED + umbral) se cablean al Motor3Engine."""
    s = mock_runner_settings
    s.MOTOR_3_CLV_ENABLED = True
    s.TRADING_ENABLED = False  # shadow
    s.MOTOR_3_EXECUTION_ENABLED = False
    s.MOTOR_3_TAKE_PROFIT_ENABLED = True
    s.MOTOR_3_TAKE_PROFIT_CENTS = 85

    mock_engine = MagicMock()
    mock_engine.run = AsyncMock()
    with (
        patch(
            "src.strategies.motor_3_clv.engine.Motor3Engine", return_value=mock_engine
        ) as mock_cls,
        patch("src.runner.asyncio.sleep", new=AsyncMock()),
    ):
        runner = ProductionRunner()
        await runner._run_motor3_clv()

    kwargs = mock_cls.call_args.kwargs
    assert kwargs["take_profit_enabled"] is True
    assert kwargs["tp_threshold"] == 85
    assert kwargs["trading_enabled"] is False  # shadow: detecta+loguea, no vende


@pytest.mark.asyncio
async def test_motor1_arb_noop_when_disabled(mock_runner_settings):
    """MOTOR_1_ARBITRAGE_ENABLED=False → _run_motor1_arb retorna sin tocar nada."""
    mock_runner_settings.MOTOR_1_ARBITRAGE_ENABLED = False
    runner = ProductionRunner()
    with patch("src.runner.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        await runner._run_motor1_arb()
    mock_sleep.assert_not_called()  # retornó antes de cualquier sleep


@pytest.mark.asyncio
async def test_motor1_arb_shadow_runs_without_executor(mock_runner_settings):
    """
    MOTOR_1_ARBITRAGE_ENABLED=True + TRADING_ENABLED=False → engine corre en SHADOW:
    NO se construye ArbitrageExecutor ni KalshiRestClient, executor=None.
    """
    mock_runner_settings.MOTOR_1_ARBITRAGE_ENABLED = True
    mock_runner_settings.TRADING_ENABLED = False
    runner = ProductionRunner()
    runner._capture = MagicMock()
    runner._capture._v2_manager = MagicMock()

    fake_engine = MagicMock()
    fake_engine.run = AsyncMock()
    with (
        patch("src.runner.asyncio.sleep", new=AsyncMock()),
        patch("src.runner.KalshiRestClient") as mock_client,
        patch(
            "src.strategies.motor_1_arbitrage.engine.Motor1Engine", return_value=fake_engine
        ) as mock_eng_cls,
    ):
        await runner._run_motor1_arb()

    mock_client.assert_not_called()  # shadow: sin cliente REST de órdenes
    fake_engine.run.assert_awaited_once()
    # Motor1Engine se construyó sin executor (Capa A).
    assert "executor" not in mock_eng_cls.call_args.kwargs


@pytest.mark.parametrize(
    ("trading", "execution", "expects_executor"),
    [
        (True, True, True),  # ambos on → M1 coloca arbs
        (True, False, False),  # el caso clave: trading global on (p.ej. para que M3 venda)
        # pero la entrada de M1 off → SHADOW, jamás compra
        (False, True, False),  # entrada on pero muro global off → shadow
        (False, False, False),
    ],
)
@pytest.mark.asyncio
async def test_motor1_entry_requires_both_flags(
    mock_runner_settings, trading, execution, expects_executor
):
    """
    Capa A por motor (auditoría 2026-07-07, P1): el ArbitrageExecutor solo se construye
    con TRADING_ENABLED **y** MOTOR_1_EXECUTION_ENABLED ambos True. Antes, prender el
    trading global para cualquier otro motor armaba también las compras de M1.
    """
    s = mock_runner_settings
    s.MOTOR_1_ARBITRAGE_ENABLED = True
    s.TRADING_ENABLED = trading
    s.MOTOR_1_EXECUTION_ENABLED = execution
    runner = ProductionRunner()
    runner._capture = MagicMock()
    runner._capture._v2_manager = MagicMock()

    fake_engine = MagicMock()
    fake_engine.run = AsyncMock()
    with (
        patch("src.runner.asyncio.sleep", new=AsyncMock()),
        patch("src.runner.KalshiRestClient") as mock_client,
        patch("src.runner.ArbitrageExecutor") as mock_exec_cls,
        patch("src.runner.RiskManager"),
        patch(
            "src.strategies.motor_1_arbitrage.engine.Motor1Engine", return_value=fake_engine
        ) as mock_eng_cls,
    ):
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client.return_value)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
        await runner._run_motor1_arb()

    fake_engine.run.assert_awaited_once()
    if expects_executor:
        assert mock_eng_cls.call_args.kwargs["executor"] is mock_exec_cls.return_value
    else:
        assert "executor" not in mock_eng_cls.call_args.kwargs  # Capa A: sin executor
        mock_client.assert_not_called()  # ni siquiera se abre el cliente de órdenes


@pytest.mark.parametrize(
    ("trading", "entry", "expects_executor"),
    [
        (True, True, True),
        (True, False, False),  # el caso clave: trading on para M3, apuestas de M2 off
        (False, True, False),
        (False, False, False),
    ],
)
@pytest.mark.asyncio
async def test_motor2_entry_requires_both_flags(
    mock_runner_settings, trading, entry, expects_executor
):
    """El Motor2Executor de ENTRADA solo se inyecta con TRADING_ENABLED **y**
    MOTOR_2_ENTRY_EXECUTION_ENABLED (distinto de MOTOR_2_EXECUTION_ENABLED, que gatea
    las ventas del brazo de salida)."""
    s = mock_runner_settings
    s.MOTOR_2_SPORTSBOOK_ENABLED = True
    s.TRADING_ENABLED = trading
    s.MOTOR_2_ENTRY_EXECUTION_ENABLED = entry
    s.MOTOR2_SERIES = "KXMLBGAME"
    s.ODDS_API_KEY = ""  # fixture fake (no toca red)
    s.MOTOR_2_MIN_EDGE_PCT = 3.0
    s.MOTOR_2_MAX_EDGE_PCT = 8.0
    s.MOTOR_2_MAX_STAKE_PCT = 1.0
    s.MOTOR_2_MIN_ENTRY_CENTS = 10
    s.MOTOR_2_UNDERDOG_FILTER_ENABLED = True
    s.MOTOR_2_ONE_BET_PER_EVENT = True
    runner = ProductionRunner()
    runner._capture = MagicMock()

    fake_poller = MagicMock()
    fake_poller.run = AsyncMock()
    with (
        patch("src.runner.asyncio.sleep", new=AsyncMock()),
        patch("src.runner.KalshiRestClient") as mock_client,
        patch("src.runner.RiskManager"),
        patch("src.strategies.motor_2_consensus.executor.Motor2Executor") as mock_exec_cls,
        patch(
            "src.strategies.motor_2_consensus.poller.Motor2ShadowPoller",
            return_value=fake_poller,
        ) as mock_poller_cls,
    ):
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client.return_value)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
        await runner._run_motor2_shadow()

    fake_poller.run.assert_awaited_once()
    if expects_executor:
        assert mock_poller_cls.call_args.kwargs["executor"] is mock_exec_cls.return_value
    else:
        assert "executor" not in mock_poller_cls.call_args.kwargs
        mock_client.assert_not_called()


@pytest.mark.asyncio
async def test_motor1_arb_aborts_when_manager_missing(mock_runner_settings):
    """Sin OrderbookManagerV2 en el capture → log + return (no crash, no engine)."""
    mock_runner_settings.MOTOR_1_ARBITRAGE_ENABLED = True
    runner = ProductionRunner()
    runner._capture = MagicMock()
    runner._capture._v2_manager = None

    with (
        patch("src.runner.asyncio.sleep", new=AsyncMock()),
        patch("src.strategies.motor_1_arbitrage.engine.Motor1Engine") as mock_eng_cls,
    ):
        await runner._run_motor1_arb()

    mock_eng_cls.assert_not_called()


def test_task_list_includes_motor1_arb_only_when_enabled(mock_runner_settings):
    """El task motor1_arb se agrega SII MOTOR_1_ARBITRAGE_ENABLED — invariante de seguridad."""
    import inspect

    import src.runner as runner_mod

    src = inspect.getsource(runner_mod.ProductionRunner.run)
    # El append está gateado por el flag (no incondicional).
    assert "if self.settings.MOTOR_1_ARBITRAGE_ENABLED:" in src
    assert 'name="motor1_arb"' in src


@pytest.mark.asyncio
async def test_reconcile_network_error_does_not_crash_boot(mock_runner_settings):
    """ConnectionError (red caida) tampoco debe crashear el bot."""
    with (
        patch("src.runner.KalshiRestClient") as mock_rest_cls,
        patch("src.runner.RiskManager"),
        patch("src.runner.ArbitrageExecutor"),
    ):
        mock_rest_cls.return_value.__aenter__ = AsyncMock(
            side_effect=ConnectionError("network gone")
        )
        mock_rest_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        runner = ProductionRunner()
        await runner._reconcile_on_boot()

        assert BotState.last_error is not None


@pytest.mark.asyncio
async def test_rehydrate_kill_switch_restores_pause_from_db(mock_runner_settings, monkeypatch):
    """
    FASE 0.2: un kill-switch persistente (OperationalState 'engaged') restaura la pausa
    al boot. Confirma que la supervivencia a reinicios vive en OperationalState — el
    RiskEvent de FASE 0.2 es SOLO auditoría, no el mecanismo de restore.
    """
    import src.storage.models as models

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(models, "get_session", lambda: Session(engine))

    models.engage_kill_switch("Stop-Loss Diario superado: PnL=$-130.00")

    runner = ProductionRunner()
    with patch("src.runner.send_alert", new_callable=AsyncMock) as mock_alert:
        await runner._rehydrate_kill_switch()

    assert BotState.is_paused is True
    assert BotState.pause_reason is not None
    assert "Stop-Loss Diario" in BotState.pause_reason
    mock_alert.assert_awaited_once()


@pytest.mark.asyncio
async def test_rehydrate_kill_switch_noop_when_not_engaged(mock_runner_settings, monkeypatch):
    """Sin kill-switch engaged → boot NO queda pausado (no falso-positivo)."""
    import src.storage.models as models

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(models, "get_session", lambda: Session(engine))

    runner = ProductionRunner()
    with patch("src.runner.send_alert", new_callable=AsyncMock) as mock_alert:
        await runner._rehydrate_kill_switch()

    assert BotState.is_paused is False
    mock_alert.assert_not_awaited()
