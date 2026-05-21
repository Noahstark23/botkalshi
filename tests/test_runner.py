"""Tests del ProductionRunner — reconcile en boot tolerante a fallas (Fase 6)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from loguru import logger

from src.monitoring.health import BotState
from src.runner import ProductionRunner


@pytest.fixture(autouse=True)
def reset_botstate():
    BotState.last_error = None
    BotState.last_error_at = None
    BotState.is_paused = False
    yield
    BotState.last_error = None
    BotState.is_paused = False


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
    with patch(
        "src.runner.KalshiRestClient"
    ) as mock_rest_cls, patch(
        "src.runner.RiskManager"
    ), patch(
        "src.runner.ArbitrageExecutor"
    ) as mock_exec_cls:
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
    with patch(
        "src.runner.KalshiRestClient"
    ) as mock_rest_cls, patch(
        "src.runner.RiskManager"
    ), patch(
        "src.runner.ArbitrageExecutor"
    ) as mock_exec_cls:
        mock_rest_cls.return_value.__aenter__ = AsyncMock(return_value=mock_rest_cls.return_value)
        mock_rest_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        mock_executor = mock_exec_cls.return_value
        mock_executor.initialize = AsyncMock(
            side_effect=RuntimeError("Kalshi unreachable at boot")
        )

        runner = ProductionRunner()

        # NO debe re-raise
        await runner._reconcile_on_boot()

        assert BotState.last_error is not None
        assert "Reconcile" in BotState.last_error or "Kalshi" in BotState.last_error


@pytest.mark.asyncio
async def test_reconcile_network_error_does_not_crash_boot(mock_runner_settings):
    """ConnectionError (red caida) tampoco debe crashear el bot."""
    with patch(
        "src.runner.KalshiRestClient"
    ) as mock_rest_cls, patch(
        "src.runner.RiskManager"
    ), patch(
        "src.runner.ArbitrageExecutor"
    ):
        mock_rest_cls.return_value.__aenter__ = AsyncMock(
            side_effect=ConnectionError("network gone")
        )
        mock_rest_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        runner = ProductionRunner()
        await runner._reconcile_on_boot()

        assert BotState.last_error is not None


# =====================================================
# AC-4: observability logs (detector.wiring_check / detector.disabled_via_config)
# =====================================================


def _capture_logs(records: list[str]):
    """Return a loguru sink that appends formatted messages to `records`."""
    def sink(message):
        records.append(message)
    return sink


@pytest.mark.asyncio
async def test_wiring_check_and_disabled_emitted_when_motor1_off(mock_runner_settings):
    """MOTOR_1_ARBITRAGE_ENABLED=False → both detector.wiring_check and
    detector.disabled_via_config appear in logs."""
    mock_runner_settings.MOTOR_1_ARBITRAGE_ENABLED = False
    mock_runner_settings.DETECTOR_SCAN_INTERVAL_SECONDS = 0.5
    mock_runner_settings.DETECTOR_MAX_POSITION_USD = 50.0

    logs: list[str] = []
    sink_id = logger.add(_capture_logs(logs), format="{message}", level="DEBUG")

    try:
        runner = ProductionRunner()

        with (
            patch("src.runner.init_db"),
            patch("src.runner.BotState"),
            patch("src.runner.alert_startup", new_callable=AsyncMock),
            patch("src.runner.alert_shutdown", new_callable=AsyncMock),
            patch("src.runner.DataCaptureService") as mock_capture_cls,
            patch("src.runner.KalshiRestClient"),
            patch("src.runner.RiskManager"),
            patch("src.runner.ArbitrageExecutor") as mock_exec_cls,
            patch("src.runner.asyncio.wait", new_callable=AsyncMock,
                  return_value=(set(), set())),
        ):
            mock_exec_cls.return_value.initialize = AsyncMock()
            mock_capture = AsyncMock()
            mock_capture.run = AsyncMock(return_value=None)
            mock_capture.stop = AsyncMock()
            mock_capture_cls.return_value = mock_capture
            runner._record_run_start = MagicMock()
            runner._record_run_end = MagicMock()

            await runner.run()

    finally:
        logger.remove(sink_id)

    combined = "\n".join(str(m) for m in logs)
    assert "detector.wiring_check" in combined
    assert "detector.disabled_via_config" in combined


@pytest.mark.asyncio
async def test_wiring_check_emitted_but_not_disabled_when_motor1_on(mock_runner_settings):
    """MOTOR_1_ARBITRAGE_ENABLED=True → detector.wiring_check emitted,
    detector.disabled_via_config NOT emitted."""
    mock_runner_settings.MOTOR_1_ARBITRAGE_ENABLED = True
    mock_runner_settings.USE_ORDERBOOK_MANAGER_V2 = False
    mock_runner_settings.DETECTOR_SCAN_INTERVAL_SECONDS = 0.5
    mock_runner_settings.DETECTOR_MAX_POSITION_USD = 50.0
    mock_runner_settings.MIN_EDGE_PCT = 2.0

    logs: list[str] = []
    sink_id = logger.add(_capture_logs(logs), format="{message}", level="DEBUG")

    try:
        runner = ProductionRunner()

        with (
            patch("src.runner.init_db"),
            patch("src.runner.BotState"),
            patch("src.runner.alert_startup", new_callable=AsyncMock),
            patch("src.runner.alert_shutdown", new_callable=AsyncMock),
            patch("src.runner.DataCaptureService") as mock_capture_cls,
            patch("src.runner.KalshiRestClient") as mock_rest_cls,
            patch("src.runner.RiskManager"),
            patch("src.runner.ArbitrageExecutor") as mock_exec_cls,
            patch("src.runner.asyncio.wait", new_callable=AsyncMock,
                  return_value=(set(), set())),
            patch("src.runner.ArbitrageDetector") as mock_detector_cls,
        ):
            mock_exec_cls.return_value.initialize = AsyncMock()
            mock_rest_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_rest_cls.return_value
            )
            mock_rest_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_capture = AsyncMock()
            mock_capture.run = AsyncMock(return_value=None)
            mock_capture.stop = AsyncMock()
            mock_ws = MagicMock()
            mock_ws.on = MagicMock()
            mock_capture.ws = mock_ws
            mock_capture_cls.return_value = mock_capture
            mock_detector = MagicMock()
            mock_detector.run = AsyncMock()
            mock_detector_cls.return_value = mock_detector
            runner._record_run_start = MagicMock()
            runner._record_run_end = MagicMock()

            await runner.run()

    finally:
        logger.remove(sink_id)

    combined = "\n".join(str(m) for m in logs)
    assert "detector.wiring_check" in combined
    assert "detector.disabled_via_config" not in combined
