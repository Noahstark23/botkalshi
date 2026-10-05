"""Producción no abre riesgo con saldo ausente, inválido o de una lectura vieja."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.risk.manager import RiskManager


@pytest.fixture
def settings(monkeypatch):
    config = SimpleNamespace(
        KALSHI_ENV="production",
        DYNAMIC_CAPITAL_ENABLED=True,
        EXPERIMENT_BANK200_ENABLED=False,
        BALANCE_MAX_AGE_SECONDS=600,
        EXPERIMENT_BALANCE_MAX_AGE_SEC=600,
        CAPITAL_SAFETY_FACTOR_PCT=90.0,
        CAPITAL_FLOOR_USD=100.0,
        CAPITAL_CAP_USD=200.0,
        ACTIVE_CAPITAL_USD=200.0,
        CAPITAL_SMOOTHING_PCT=0.05,
    )
    monkeypatch.setattr("src.risk.manager.get_settings", lambda: config)
    monkeypatch.setattr(RiskManager, "_cached_capital_usd", None)
    monkeypatch.setattr(RiskManager, "_last_raw_balance_usd", None)
    monkeypatch.setattr(RiskManager, "_last_balance_at", None)
    monkeypatch.setattr(RiskManager, "_check_capital_drift", AsyncMock())
    monkeypatch.setattr(RiskManager, "log_stop_loss_status", lambda: None)
    return config


def observed(cash=200.0, age=0):
    RiskManager._cached_capital_usd = cash
    RiskManager._last_raw_balance_usd = cash
    RiskManager._last_balance_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=age)


@pytest.mark.parametrize("dynamic", [True, False])
def test_production_without_observed_cash_blocks_entries(settings, dynamic):
    settings.DYNAMIC_CAPITAL_ENABLED = dynamic
    assert RiskManager().can_open_new_positions() is False


@pytest.mark.parametrize("age", [601, -60])
def test_stale_or_future_balance_blocks_entries(settings, age):
    observed(age=age)
    assert RiskManager().can_open_new_positions() is False


@pytest.mark.parametrize("cash", [float("nan"), float("inf"), -1, 0])
def test_invalid_cash_blocks_entries(settings, cash):
    observed(cash=cash)
    assert RiskManager().can_open_new_positions() is False


def test_fresh_funds_above_floor_allow_capital_gate(settings):
    observed()
    assert RiskManager().can_open_new_positions() is True


def test_raw_withdrawal_cannot_hide_behind_smoothing(settings):
    observed(cash=115)
    RiskManager._last_raw_balance_usd = 110
    assert RiskManager().can_open_new_positions() is False  # 99 < piso 100


def test_live_sizing_uses_lower_raw_cash(settings):
    observed(cash=200)
    RiskManager._last_raw_balance_usd = 195
    assert RiskManager().effective_capital_usd() == pytest.approx(175.5)


def test_static_live_sizing_still_respects_available_cash(settings):
    settings.DYNAMIC_CAPITAL_ENABLED = False
    observed(cash=150)
    assert RiskManager().effective_capital_usd() == pytest.approx(135)


def test_later_deposit_does_not_raise_approved_cap(settings):
    observed(cash=1000)
    assert RiskManager().effective_capital_usd() == 200


@pytest.mark.asyncio
async def test_bad_cash_response_cannot_refresh_old_timestamp(settings):
    observed(age=601)
    previous = RiskManager._last_balance_at
    await RiskManager.refresh_capital_from_balance(
        client_factory=factory({"balance": 20000, "balance_dollars": "NaN"})
    )
    assert RiskManager._last_balance_at == previous
    assert RiskManager().can_open_new_positions() is False


def test_degraded_status_reports_paused(settings):
    with patch.object(RiskManager, "_get_effective_capital_usd", side_effect=ValueError):
        assert RiskManager.capital_status()["is_paused"] is True


def factory(payload=None, error=None):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get_balance = AsyncMock(return_value=payload, side_effect=error)
    return lambda: client


@pytest.mark.asyncio
async def test_static_production_refreshes_real_balance(settings):
    settings.DYNAMIC_CAPITAL_ENABLED = False
    await RiskManager.refresh_capital_from_balance(client_factory=factory({"balance": 20000}))
    assert RiskManager._last_raw_balance_usd == 200


@pytest.mark.asyncio
async def test_precise_dollars_preserved(settings):
    result = await RiskManager.refresh_capital_from_balance(
        client_factory=factory({"balance_dollars": "49.9825", "balance": 4998})
    )
    assert result == 49.9825


@pytest.mark.asyncio
async def test_failed_refresh_does_not_extend_freshness(settings):
    observed(age=601)
    previous = RiskManager._last_balance_at
    await RiskManager.refresh_capital_from_balance(client_factory=factory(error=TimeoutError()))
    assert RiskManager._last_balance_at == previous
    assert RiskManager().can_open_new_positions() is False
