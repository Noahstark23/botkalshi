"""
Tests para el reset del contador consecutive_failures en kalshi_ws.run().

Escenario real del 20-21/05/2026: dos caídas del WS con ~14.5h estables entre
eventos. El contador llegó a 2 en lugar de 1+1 porque el reset solo ocurría
cuando _connect_and_listen() retornaba sin excepción (path raro en producción).

Estrategia de mock: parcheamos _stable_connection directamente (float return)
para controlar si el reset se aplica o no, sin depender de mocking de datetime.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import websockets.exceptions


def make_ws():
    """KalshiWebSocket con settings y signer mockeados."""
    with patch("src.clients.kalshi_ws.get_settings"), patch(
        "src.clients.kalshi_ws.KalshiSigner"
    ):
        from src.clients.kalshi_ws import KalshiWebSocket
        return KalshiWebSocket()


def patched_run_env(ws, *, stable: bool, max_cycles: int):
    """
    Context manager que aplica todos los mocks necesarios para correr run().

    stable=True  → _stable_connection devuelve 90.0 (supera threshold) → reset activo.
    stable=False → _stable_connection devuelve 0.0  (bajo threshold)   → acumula.
    max_cycles   → detiene el loop después de N ciclos vía asyncio.sleep mock.
    """
    cycle = 0

    async def fast_fail():
        nonlocal cycle
        cycle += 1
        raise websockets.exceptions.ConnectionClosedError(None, None)

    async def stop_after_n_cycles(_sleep_arg):
        if cycle >= max_cycles:
            ws._running = False

    ws._connect_and_listen = fast_fail

    stable_val = 90.0 if stable else 0.0

    return (
        patch("src.clients.kalshi_ws._stable_connection", return_value=stable_val),
        patch("src.clients.kalshi_ws.asyncio.sleep", side_effect=stop_after_n_cycles),
    )


@pytest.mark.asyncio
async def test_failure_counter_resets_after_stable_connection():
    """
    3 ciclos con conexión estable (>threshold) antes de cada caída.
    consecutive_failures debe quedar en 1 después de cada ciclo — eventos aislados.
    El log ws.failure_counter.reset debe aparecer una vez por ciclo.
    """
    ws = make_ws()
    logged_failures = []

    def capture_warning(msg, *args, **kwargs):
        if "Consecutive failures" in str(msg):
            import re
            m = re.search(r"Consecutive failures: (\d+)", str(msg))
            if m:
                logged_failures.append(int(m.group(1)))

    reset_logs = []

    def capture_info(msg, *args, **kwargs):
        if "ws.failure_counter.reset" in str(msg):
            reset_logs.append(str(msg))

    stable_patch, sleep_patch = patched_run_env(ws, stable=True, max_cycles=3)
    with stable_patch, sleep_patch, patch(
        "src.clients.kalshi_ws.logger"
    ) as mock_logger:
        mock_logger.warning.side_effect = capture_warning
        mock_logger.info.side_effect = capture_info
        mock_logger.exception = MagicMock()
        mock_logger.critical = MagicMock()
        await ws.run()

    assert logged_failures == [1, 1, 1], (
        f"Each failure should be isolated (counter=1), got: {logged_failures}"
    )
    assert len(reset_logs) == 3, (
        f"Expected 3 reset log lines, got: {reset_logs}"
    )


@pytest.mark.asyncio
async def test_failure_counter_accumulates_on_rapid_failures():
    """
    3 ciclos con caídas rápidas (< threshold, _stable_connection devuelve 0.0).
    consecutive_failures debe acumularse: 1, 2, 3.
    """
    ws = make_ws()
    logged_failures = []

    def capture_warning(msg, *args, **kwargs):
        if "Consecutive failures" in str(msg):
            import re
            m = re.search(r"Consecutive failures: (\d+)", str(msg))
            if m:
                logged_failures.append(int(m.group(1)))

    stable_patch, sleep_patch = patched_run_env(ws, stable=False, max_cycles=3)
    with stable_patch, sleep_patch, patch(
        "src.clients.kalshi_ws.logger"
    ) as mock_logger:
        mock_logger.warning.side_effect = capture_warning
        mock_logger.info = MagicMock()
        mock_logger.exception = MagicMock()
        mock_logger.critical = MagicMock()
        await ws.run()

    assert logged_failures == [1, 2, 3], (
        f"Expected accumulation [1,2,3], got: {logged_failures}"
    )


@pytest.mark.asyncio
async def test_alert_fires_only_on_real_cascade():
    """
    5 fallas rápidas (stable=False) → critical log emitido (cascade real detectada).
    5 fallas estables (stable=True)  → critical log NO emitido (eventos aislados).
    """
    # --- Caso 1: cascade rápida → alert ---
    ws_rapid = make_ws()
    critical_calls_rapid = []

    stable_patch_rapid, sleep_patch_rapid = patched_run_env(
        ws_rapid, stable=False, max_cycles=5
    )
    with stable_patch_rapid, sleep_patch_rapid, patch(
        "src.clients.kalshi_ws.logger"
    ) as mock_logger_r:
        mock_logger_r.warning = MagicMock()
        mock_logger_r.info = MagicMock()
        mock_logger_r.exception = MagicMock()
        mock_logger_r.critical.side_effect = lambda msg, *a, **kw: critical_calls_rapid.append(msg)

        with patch("src.clients.kalshi_ws.asyncio.sleep", new=sleep_patch_rapid.kwargs.get("side_effect")):
            pass  # already patched above via context manager

        await ws_rapid.run()

    assert len(critical_calls_rapid) >= 1, (
        "5 rapid failures must trigger critical log"
    )

    # --- Caso 2: fallas estables → sin alert ---
    ws_stable = make_ws()
    critical_calls_stable = []

    stable_patch_stable, sleep_patch_stable = patched_run_env(
        ws_stable, stable=True, max_cycles=5
    )
    with stable_patch_stable, sleep_patch_stable, patch(
        "src.clients.kalshi_ws.logger"
    ) as mock_logger_s:
        mock_logger_s.warning = MagicMock()
        mock_logger_s.info = MagicMock()
        mock_logger_s.exception = MagicMock()
        mock_logger_s.critical.side_effect = lambda msg, *a, **kw: critical_calls_stable.append(msg)
        await ws_stable.run()

    assert len(critical_calls_stable) == 0, (
        f"Stable-then-fail pattern must NOT trigger critical, got: {critical_calls_stable}"
    )
