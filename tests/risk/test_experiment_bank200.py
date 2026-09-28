from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from src.risk.experiment_policy import limits_for_capital
from src.risk.manager import RiskManager
from src.utils.config import Settings


def test_policy_at_reference_bank() -> None:
    p = limits_for_capital(200)
    assert p.unit_usd == Decimal("2.00")
    assert p.habitual_usd == Decimal("1.00")
    assert p.max_per_operation_usd == Decimal("2.00")
    assert p.max_per_thesis_usd == Decimal("2.00")
    assert p.max_open_usd == Decimal("6.00")
    assert p.max_daily_new_risk_usd == Decimal("6.00")


def test_policy_shrinks_with_capital_and_never_grows_on_profit() -> None:
    down = limits_for_capital(190)
    up = limits_for_capital(350)
    assert down.unit_usd == Decimal("1.90")
    assert down.habitual_usd == Decimal("0.95")
    assert down.max_open_usd == Decimal("5.70")
    assert up.unit_usd == Decimal("2.00")
    assert up.max_open_usd == Decimal("6.00")


def _safe_env(monkeypatch: pytest.MonkeyPatch, key: Path) -> None:
    values = {
        "KALSHI_ENV": "production",
        "KALSHI_API_KEY_ID": "test-id-12345",
        "KALSHI_PRIVATE_KEY_PATH": str(key),
        "EXPERIMENT_BANK200_ENABLED": "true",
        "ACTIVE_CAPITAL_USD": "200",
        "DYNAMIC_CAPITAL_ENABLED": "true",
        "CAPITAL_CAP_USD": "200",
        "CAPITAL_FLOOR_USD": "180",
        "CAPITAL_SMOOTHING_PCT": "0",
        "MAX_TRADE_SIZE_USD": "2",
        "MAX_TRADE_SIZE_PCT": "1",
        "MAX_SIMULTANEOUS_EXPOSURE_PCT": "3",
        "MAX_EVENT_DIRECTIONAL_EXPOSURE_USD": "2",
        "MOTOR_2_ENTRY_EXECUTION_ENABLED": "false",
        "MOTOR_REST_EXECUTION_ENABLED": "false",
        "MOTOR_MM_EXECUTION_ENABLED": "false",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_production_bank200_rejects_old_live_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = tmp_path / "key.pem"
    key.write_text("dummy")
    _safe_env(monkeypatch, key)
    monkeypatch.setenv("ACTIVE_CAPITAL_USD", "400")
    with pytest.raises(ValidationError, match="ACTIVE_CAPITAL_USD=200"):
        Settings()


def test_production_bank200_safe_profile_loads_unarmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = tmp_path / "key.pem"
    key.write_text("dummy")
    _safe_env(monkeypatch, key)
    s = Settings()
    assert s.EXPERIMENT_BANK200_ENABLED is True
    assert s.TRADING_ENABLED is False


def test_production_bank200_requires_timestamp_when_armed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = tmp_path / "key.pem"
    key.write_text("dummy")
    _safe_env(monkeypatch, key)
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("MOTOR_1_ARBITRAGE_ENABLED", "true")
    monkeypatch.setenv("MOTOR_1_EXECUTION_ENABLED", "true")
    with pytest.raises(ValidationError, match="EXPERIMENT_START_AT"):
        Settings()


def test_experiment_requires_fresh_real_balance() -> None:
    settings = MagicMock()
    settings.EXPERIMENT_BANK200_ENABLED = True
    settings.EXPERIMENT_BALANCE_MAX_AGE_SEC = 600
    settings.DYNAMIC_CAPITAL_ENABLED = True
    settings.CAPITAL_SAFETY_FACTOR_PCT = 100.0
    settings.CAPITAL_CAP_USD = 200.0
    settings.CAPITAL_FLOOR_USD = 180.0

    with patch("src.risk.manager.get_settings", return_value=settings):
        rm = RiskManager()

    RiskManager._cached_capital_usd = None
    RiskManager._last_balance_at = None
    assert rm.can_open_new_positions() is False

    RiskManager._cached_capital_usd = 200.0
    RiskManager._last_balance_at = datetime.now(UTC).replace(tzinfo=None)
    assert rm.can_open_new_positions() is True

    RiskManager._last_balance_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=601)
    assert rm.can_open_new_positions() is False

    RiskManager._cached_capital_usd = None
    RiskManager._last_balance_at = None
