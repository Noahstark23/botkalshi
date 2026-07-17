"""Tests del monitor de memoria (advisory-only)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.monitoring.memory_monitor import MemoryMonitor, read_cgroup_memory

MB = 1024 * 1024


@pytest.fixture
def monitor():
    """MemoryMonitor con umbral 80% y estado limpio (settings stub, sin .env real)."""
    stub = SimpleNamespace(
        MEMORY_MONITOR_THRESHOLD_PCT=80.0,
        MEMORY_MONITOR_INTERVAL_SEC=60,
    )
    with patch("src.monitoring.memory_monitor.get_settings", return_value=stub):
        m = MemoryMonitor()
    return m


async def test_no_alert_below_threshold(monitor):
    """70% < 80% → no alerta, no marca _alerted."""
    with patch("src.monitoring.memory_monitor.send_alert", new=AsyncMock()) as send:
        pct = await monitor._check_once(reader=lambda: (700 * MB, 1000 * MB))
    assert pct == pytest.approx(70.0)
    assert monitor._alerted is False
    send.assert_not_called()


async def test_alert_on_crossing_threshold(monitor):
    """85% >= 80% → alerta urgente y marca _alerted."""
    with patch("src.monitoring.memory_monitor.send_alert", new=AsyncMock()) as send:
        pct = await monitor._check_once(reader=lambda: (850 * MB, 1000 * MB))
    assert pct == pytest.approx(85.0)
    assert monitor._alerted is True
    send.assert_awaited_once()
    assert send.await_args.kwargs.get("urgent") is True


async def test_no_duplicate_alert_while_high(monitor):
    """Edge-triggered: dos lecturas altas seguidas → una sola alerta."""
    with patch("src.monitoring.memory_monitor.send_alert", new=AsyncMock()) as send:
        await monitor._check_once(reader=lambda: (850 * MB, 1000 * MB))
        await monitor._check_once(reader=lambda: (860 * MB, 1000 * MB))
    assert send.await_count == 1


async def test_rearm_only_after_margin(monitor):
    """Tras alertar, no re-arma a 78% (dentro del margen) pero sí a 74%."""
    with patch("src.monitoring.memory_monitor.send_alert", new=AsyncMock()) as send:
        await monitor._check_once(reader=lambda: (850 * MB, 1000 * MB))  # alerta
        await monitor._check_once(reader=lambda: (780 * MB, 1000 * MB))  # 78% → sigue armado
        assert monitor._alerted is True
        await monitor._check_once(reader=lambda: (740 * MB, 1000 * MB))  # 74% → re-arma
        assert monitor._alerted is False
        # Una nueva subida vuelve a alertar (total 2).
        await monitor._check_once(reader=lambda: (850 * MB, 1000 * MB))
    assert send.await_count == 2


async def test_none_sample_is_noop(monitor):
    """Sin límite legible (None) → no alerta, devuelve None."""
    with patch("src.monitoring.memory_monitor.send_alert", new=AsyncMock()) as send:
        pct = await monitor._check_once(reader=lambda: None)
    assert pct is None
    send.assert_not_called()


def test_read_cgroup_memory_unlimited_returns_none(tmp_path):
    """Un límite astronómico (sin cap real) se trata como None."""
    current = tmp_path / "memory.current"
    maxf = tmp_path / "memory.max"
    current.write_text("123456")
    maxf.write_text(str(1 << 63))  # > _UNLIMITED_THRESHOLD_BYTES
    with (
        patch("src.monitoring.memory_monitor._V2_CURRENT", current),
        patch("src.monitoring.memory_monitor._V2_MAX", maxf),
    ):
        assert read_cgroup_memory() is None


def test_read_cgroup_memory_v2_ok(tmp_path):
    """v2 con límite real → (uso, límite)."""
    current = tmp_path / "memory.current"
    maxf = tmp_path / "memory.max"
    current.write_text(str(512 * MB))
    maxf.write_text(str(1024 * MB))
    with (
        patch("src.monitoring.memory_monitor._V2_CURRENT", current),
        patch("src.monitoring.memory_monitor._V2_MAX", maxf),
    ):
        assert read_cgroup_memory() == (512 * MB, 1024 * MB)


def test_read_cgroup_memory_v2_max_literal(tmp_path):
    """v2 reporta 'max' (sin límite) → None."""
    current = tmp_path / "memory.current"
    maxf = tmp_path / "memory.max"
    current.write_text(str(512 * MB))
    maxf.write_text("max")
    with (
        patch("src.monitoring.memory_monitor._V2_CURRENT", current),
        patch("src.monitoring.memory_monitor._V2_MAX", maxf),
    ):
        assert read_cgroup_memory() is None
