"""
Dashboard de Telegram on-demand: el builder read-only + el command handler con authz.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.analytics.daily_pnl import aggregate_daily_pnl
from src.monitoring import dashboard as dash
from src.risk.manager import RiskManager
from src.storage.models import PortfolioPosition, Trade, get_session

DAY = datetime(2026, 6, 28, 12, 0)  # naive UTC (settled_at) — el "ahora" del dashboard


def _seed_settled(strategy: str, pnl_cents: int, *, fees_cents: int = 10, coid: str) -> None:
    with get_session() as s:
        s.add(
            Trade(
                client_order_id=coid,
                ticker="KXTEST",
                side="yes",
                action="buy",
                count=10,
                price_cents=50,
                strategy=strategy,
                status="settled",
                pnl_cents=pnl_cents,
                fees_cents=fees_cents,
                settled_at=DAY,  # naive UTC, dentro del día/semana/mes/total
            )
        )
        s.commit()


def _mock_settings() -> MagicMock:
    s = MagicMock()
    s.ACTIVE_CAPITAL_USD = 300.0
    s.DYNAMIC_CAPITAL_ENABLED = False  # capital efectivo = ACTIVE (sin cash real en tests)
    s.KALSHI_ENV = "production"
    s.MAX_DAILY_LOSS_PCT = 3.0
    s.MAX_WEEKLY_LOSS_PCT = 8.0
    s.MAX_MONTHLY_LOSS_PCT = 15.0
    s.MOTOR_1_ARBITRAGE_ENABLED = True
    s.MOTOR_2_SPORTSBOOK_ENABLED = True
    s.MOTOR_3_CLV_ENABLED = True
    s.MOTOR_3_TRAILING_ENABLED = False
    s.MOTOR_3_TRAILING_DROP_CENTS = 5
    s.MOTOR_3_TAKE_PROFIT_ENABLED = True
    s.MOTOR_3_TAKE_PROFIT_CENTS = 62
    return s


# =====================================================
# aggregate_daily_pnl: avg win / avg loss (campos nuevos)
# =====================================================


def test_aggregate_computes_avg_win_loss():
    _seed_settled("motor_2_consensus", 1000, coid="w1")  # win +$10
    _seed_settled("motor_2_consensus", 2000, coid="w2")  # win +$20
    _seed_settled("motor_2_consensus", -3000, coid="l1")  # loss -$30
    with get_session() as s:
        agg = aggregate_daily_pnl(s, datetime(2026, 6, 28), datetime(2026, 6, 29))
    m = agg.by_motor[0]
    assert m.wins == 2 and m.losses == 1
    assert m.avg_win_cents == 1500.0  # (1000+2000)/2
    assert m.avg_loss_cents == -3000.0


# =====================================================
# build_dashboard_text
# =====================================================


def test_build_dashboard_text_sections(monkeypatch):
    settings = _mock_settings()
    monkeypatch.setattr(dash, "get_settings", lambda: settings)
    monkeypatch.setattr("src.risk.manager.get_settings", lambda: settings)
    RiskManager._cached_capital_usd = None
    RiskManager._last_raw_balance_usd = None

    _seed_settled("motor_2_consensus", 1500, coid="win")
    _seed_settled("motor_2_consensus", -3000, coid="loss")
    with get_session() as s:
        s.add(PortfolioPosition(ticker="KXOPEN", side="yes", count=10))
        s.commit()

    text = dash.build_dashboard_text(now=DAY.replace(tzinfo=UTC))

    assert "Dashboard KalshiBot" in text
    assert "PnL realizado" in text
    assert "Por motor" in text and "Motor 2 (consenso)" in text
    assert "Stop-loss" in text
    assert "Trailing: `OFF`" in text  # MOTOR_3_TRAILING_ENABLED=false
    assert "Take-profit: `ON`" in text
    assert "Posiciones abiertas: `1`" in text
    # PnL total del día = 1500 - 3000 = -1500c = -$15.00
    assert "−$15.00" in text


def test_build_dashboard_text_no_trades(monkeypatch):
    """Sin trades liquidados → no rompe, sección por-motor lo dice."""
    settings = _mock_settings()
    monkeypatch.setattr(dash, "get_settings", lambda: settings)
    monkeypatch.setattr("src.risk.manager.get_settings", lambda: settings)
    RiskManager._cached_capital_usd = None
    RiskManager._last_raw_balance_usd = None

    text = dash.build_dashboard_text(now=DAY.replace(tzinfo=UTC))
    assert "Sin trades liquidados todavía." in text


# =====================================================
# TelegramCommandLoop — authz
# =====================================================


def _loop_with_update(monkeypatch, update: dict, chat_id: str = "123"):
    sent: list[str] = []

    async def _fake_send(text, **kw):
        sent.append(text)
        return True

    settings = MagicMock(TELEGRAM_CHAT_ID=chat_id, TELEGRAM_DASHBOARD_INTERVAL_SEC=0.0)
    monkeypatch.setattr(dash, "get_settings", lambda: settings)  # ANTES de construir el loop
    monkeypatch.setattr(dash, "send_alert", _fake_send)
    monkeypatch.setattr(dash, "build_dashboard_text", lambda: "DASH-OK")
    loop = dash.TelegramCommandLoop()
    loop._get_updates = AsyncMock(return_value=[update])
    return loop, sent


@pytest.mark.asyncio
async def test_authorized_dashboard_command_responds(monkeypatch):
    loop, sent = _loop_with_update(
        monkeypatch, {"update_id": 5, "message": {"chat": {"id": 123}, "text": "/dashboard"}}
    )
    await loop._poll_once()
    assert sent == ["DASH-OK"]
    assert loop._offset == 6  # ack: próximo update


@pytest.mark.asyncio
async def test_unauthorized_chat_ignored(monkeypatch):
    loop, sent = _loop_with_update(
        monkeypatch,
        {"update_id": 7, "message": {"chat": {"id": 999}, "text": "/dashboard"}},  # otro chat
        chat_id="123",
    )
    await loop._poll_once()
    assert sent == []  # NO responde a chats no autorizados
    assert loop._offset == 8  # pero igual ack-ea el update (no re-procesa)


@pytest.mark.asyncio
async def test_non_command_text_ignored(monkeypatch):
    loop, sent = _loop_with_update(
        monkeypatch, {"update_id": 9, "message": {"chat": {"id": 123}, "text": "hola"}}
    )
    await loop._poll_once()
    assert sent == []
