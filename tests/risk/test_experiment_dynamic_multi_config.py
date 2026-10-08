"""El perfil conjunto valida objetos Settings sin construir clientes ni leer secretos."""

import pytest
from pydantic import ValidationError

from src.utils.config import Settings
from tests.risk.test_experiment_dynamic_m1_config import profile  # noqa: F401


@pytest.fixture
def multi_profile(profile, tmp_path):  # noqa: F811 — fixture importada deliberadamente
    return {
        **profile,
        "EXPERIMENT_DYNAMIC_M1_ENABLED": False,
        "EXPERIMENT_DYNAMIC_MULTI_ENABLED": True,
        "ODDS_SHARED_SNAPSHOT_DIR": str(tmp_path / "snapshot"),
        "MOTOR2_SERIES": "KXMLBGAME",
        "MOTOR_MM_SERIES": "KXMLBGAME",
        "MOTOR_MM_MAX_EXPOSURE_USD": 3,
        "UNREALIZED_STOP_ENABLED": True,
    }


def test_profile_is_opt_in_and_does_not_arm_any_engine(multi_profile):
    settings = Settings(**multi_profile)
    assert settings.EXPERIMENT_DYNAMIC_MULTI_ENABLED
    assert not settings.TRADING_ENABLED
    assert not settings.MOTOR_1_EXECUTION_ENABLED
    assert not settings.MOTOR_2_ENTRY_EXECUTION_ENABLED
    assert not settings.MOTOR_MM_EXECUTION_ENABLED
    assert not Settings.model_fields["EXPERIMENT_DYNAMIC_MULTI_ENABLED"].default


@pytest.mark.parametrize(
    "key,value",
    [
        ("EXPERIMENT_DYNAMIC_M1_ENABLED", True),
        ("EXPERIMENT_BANK50_ENABLED", True),
        ("EXPERIMENT_BANK200_ENABLED", True),
        ("ODDS_SHARED_SNAPSHOT_DIR", ""),
        ("ODDS_SHARED_SNAPSHOT_DIR", "relative"),
        ("ODDS_API_KEY", "fixture"),
        ("ODDS_SHARED_SNAPSHOT_MAX_AGE_SEC", 0),
        ("ODDS_SHARED_SNAPSHOT_MAX_AGE_SEC", 661),
        ("ODDS_SHARED_SNAPSHOT_MAX_AGE_SEC", float("nan")),
        ("MOTOR2_SERIES", "KXNFLGAME"),
        ("MOTOR_MM_SERIES", "KXNFLGAME"),
        ("MOTOR_MM_FAIR_TTL_SEC", 600),
        ("MOTOR_MM_MAX_EXPOSURE_USD", 3.01),
        ("UNREALIZED_STOP_ENABLED", False),
        ("MOTOR_2_ONE_BET_PER_EVENT", False),
        ("MOTOR_2_EXECUTION_ENABLED", True),
        ("TRADING_ENABLED", True),
        ("MOTOR_1_EXECUTION_ENABLED", True),
        ("MOTOR_2_ENTRY_EXECUTION_ENABLED", True),
        ("MOTOR_MM_EXECUTION_ENABLED", True),
        ("MOTOR_REST_EXECUTION_ENABLED", True),
        ("MOTOR_3_EXECUTION_ENABLED", True),
        ("MOTOR_REST_ENABLED", True),
        ("MOTOR_3_CLV_ENABLED", True),
        ("MOTOR_6_LINEMOVE_ENABLED", True),
        ("MOTOR_8_OFI_ENABLED", True),
        ("MOTOR_9_SPILLOVER_ENABLED", True),
        ("MAX_TRADE_SIZE_USD", 1.01),
        ("MAX_EVENT_DIRECTIONAL_EXPOSURE_USD", 1.01),
        ("BALANCE_MAX_AGE_SECONDS", 61),
        ("CAPITAL_SAFETY_FACTOR_PCT", 100),
    ],
)
def test_profile_rejects_looser_or_incompatible_controls(multi_profile, key, value):
    with pytest.raises(ValidationError):
        Settings(**{**multi_profile, key: value})


def test_shared_snapshot_can_feed_m5_without_odds_key(multi_profile):
    # Sólo valida la fuente de datos shadow: no se construye ningún cliente.
    shadow = {
        **multi_profile,
        "KALSHI_ENV": "production",
        "MOTOR_2_SPORTSBOOK_ENABLED": True,
        "MOTOR_MM_ENABLED": True,
        "MOTOR_MM_FEES_AS_MAKER": True,
        "MOTOR_MM_QUOTE_SIZE_CONTRACTS": 1,
        "MOTOR_2_MIN_BOOKS": 3,
        "MOTOR_2_MAX_BOOK_AGE_MIN": 15,
    }
    settings = Settings(**shadow)
    assert not settings.ODDS_API_KEY and settings.ODDS_SHARED_SNAPSHOT_DIR
    assert settings.MOTOR_MM_FAIR_TTL_SEC == 360
    assert not settings.TRADING_ENABLED and not settings.MOTOR_MM_EXECUTION_ENABLED
    # Ni un acknowledgement legado puede convertir el perfil nuevo en ejecución.
    with pytest.raises(ValidationError, match="fuera del perfil"):
        Settings(**{**shadow, "MOTOR_MM_EXECUTION_ENABLED": True, "MOTOR_MM_F3_ACK": "NOEL-OK-F3"})


@pytest.mark.parametrize("reference,ack", [(49.49, 100.43), (100.43, 49.49)])
def test_dynamic_balance_is_not_a_manually_acknowledged_bank(multi_profile, reference, ack):
    assert Settings(
        **{**multi_profile, "ACTIVE_CAPITAL_USD": reference, "EXPERIMENT_BANK_CONFIRMED_USD": ack}
    )
