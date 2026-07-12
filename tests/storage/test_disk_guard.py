"""
DiskGuard — lazo CERRADO de presión de disco (incidente 2026-07-10: el WAL a ~8MB/s llenó
el 96% del disco sin que el bot lo viera).

Verifica: mecanismo (umbral → estado → backpressure de telemetría), control (warn/ok NO
descartan), y fail-safe (la medición es lectura → falla ABIERTA: un error de statvfs
mantiene el último estado conocido y no apaga/enciende nada solo).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.storage.disk_guard import CRITICAL, OK, WARN, DiskGuard


@pytest.fixture(autouse=True)
def reset_guard():
    DiskGuard.reset()
    yield
    DiskGuard.reset()


def _settings(warn_gb: float = 5.0, critical_gb: float = 2.0) -> MagicMock:
    s = MagicMock()
    s.DISK_GUARD_WARN_FREE_GB = warn_gb
    s.DISK_GUARD_CRITICAL_FREE_GB = critical_gb
    s.DATABASE_URL = "sqlite:////app/data/trades.db"
    return s


def _usage(free_gb: float) -> MagicMock:
    u = MagicMock()
    u.free = int(free_gb * 1e9)
    return u


@patch("src.storage.disk_guard.shutil.disk_usage")
@patch("src.storage.disk_guard.get_settings")
def test_critical_sheds_diagnostics(gs, du):
    """MECANISMO: < umbral critical → estado critical y la telemetría se descarta."""
    gs.return_value = _settings()
    du.return_value = _usage(1.5)  # < 2.0 critical
    assert DiskGuard.evaluate() == CRITICAL
    assert DiskGuard.diagnostics_allowed() is False
    assert DiskGuard.snapshot()["free_gb"] == 1.5


@patch("src.storage.disk_guard.shutil.disk_usage")
@patch("src.storage.disk_guard.get_settings")
def test_warn_still_allows_diagnostics(gs, du):
    """CONTROL: warn alerta pero NO descarta — solo critical hace backpressure."""
    gs.return_value = _settings()
    du.return_value = _usage(3.0)  # entre 2.0 y 5.0
    assert DiskGuard.evaluate() == WARN
    assert DiskGuard.diagnostics_allowed() is True


@patch("src.storage.disk_guard.shutil.disk_usage")
@patch("src.storage.disk_guard.get_settings")
def test_ok_and_recovery(gs, du):
    """CONTROL: con aire, ok. Y la recuperación critical→ok reactiva la telemetría."""
    gs.return_value = _settings()
    du.return_value = _usage(1.0)
    assert DiskGuard.evaluate() == CRITICAL
    du.return_value = _usage(20.0)
    assert DiskGuard.evaluate() == OK
    assert DiskGuard.diagnostics_allowed() is True


@patch("src.storage.disk_guard.shutil.disk_usage")
@patch("src.storage.disk_guard.get_settings")
def test_measurement_error_fails_open_keeps_state(gs, du):
    """FAIL-SAFE: la medición es LECTURA → falla abierta. Un error de statvfs no apaga la
    telemetría (desde ok) ni la des-apaga (desde critical): mantiene el estado previo."""
    gs.return_value = _settings()

    # Desde ok: el error NO descarta telemetría.
    du.side_effect = OSError("mount desaparecido")
    assert DiskGuard.evaluate() == OK
    assert DiskGuard.diagnostics_allowed() is True

    # Desde critical real: el error NO "recupera" mágicamente.
    du.side_effect = None
    du.return_value = _usage(1.0)
    assert DiskGuard.evaluate() == CRITICAL
    du.side_effect = OSError("hiccup")
    assert DiskGuard.evaluate() == CRITICAL
    assert DiskGuard.diagnostics_allowed() is False
