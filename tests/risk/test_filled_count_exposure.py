"""
Motor 5 F2 — RiskManager: reservado (resting) vs expuesto (filled parcial) +
modo quotes_paused persistente.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import src.storage.models as models
from src.storage.models import Trade, get_session, mm_quotes_paused, set_mm_quotes_paused


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    db = tmp_path / "risk_f2.db"
    settings = MagicMock()
    settings.DATABASE_URL = f"sqlite:///{db}"
    models._engine = None
    with patch("src.storage.models.get_settings", return_value=settings):
        engine = models.get_engine()
    models.SQLModel.metadata.create_all(engine)
    yield engine
    models._engine = None


def _rm():
    from src.risk.manager import RiskManager

    with patch.object(RiskManager, "__init__", lambda self: None):
        rm = RiskManager()
    return rm


def _trade(coid: str, status: str, count: int, filled_count: int | None = None) -> Trade:
    return Trade(
        client_order_id=coid,
        ticker="T",
        side="yes",
        action="buy",
        count=count,
        price_cents=50,
        strategy="motor_5_mm",
        status=status,
        filled_count=filled_count,
    )


def test_partial_fill_exposes_only_filled_count():
    """Resting de 1000 con 500 llenados (resto cancelado) → expone $250, no $500."""
    with get_session() as s:
        s.add(_trade("a", "filled", 1000, filled_count=500))
        s.commit()
    assert _rm()._get_current_exposure_usd() == pytest.approx(250.0)


def test_pending_resting_reserves_full_count():
    """Una orden RESTING (pending) reserva el count COMPLETO — puede llenarse entera
    en cualquier momento (conservador: frena antes, nunca después)."""
    with get_session() as s:
        s.add(_trade("b", "pending", 100))
        s.commit()
    assert _rm()._get_current_exposure_usd() == pytest.approx(50.0)


def test_legacy_none_semantics_unchanged():
    """filled_count=None (órdenes FOK/IOC legacy) → count entero, como siempre."""
    with get_session() as s:
        s.add(_trade("c", "filled", 100))
        s.commit()
    assert _rm()._get_current_exposure_usd() == pytest.approx(50.0)


def test_quotes_paused_roundtrip_persistent():
    assert mm_quotes_paused() == (False, None)
    set_mm_quotes_paused(True, "spread anomalo en demo")
    paused, reason = mm_quotes_paused()
    assert paused is True and reason == "spread anomalo en demo"
    set_mm_quotes_paused(False)
    assert mm_quotes_paused() == (False, None)


def test_quotes_paused_is_independent_of_kill_switch():
    """quotes_paused NO toca el kill-switch: son niveles distintos de pausa."""
    set_mm_quotes_paused(True, "x")
    assert models.kill_switch_engaged() == (False, None)
