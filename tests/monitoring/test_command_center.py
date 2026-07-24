"""
Centro de comando C1 (read-only) — builders + dispatch en el TelegramCommandLoop.

Verifica: cada builder arma su texto desde la DB/estado real (y su caso vacío), el
dispatch responde SOLO al chat autorizado, y un builder roto responde el error en el
chat sin romper el loop (best-effort). Nivel 0: nada de esto muta el bot.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import src.monitoring.dashboard as dash
import src.storage.models as models
from src.monitoring import command_center as cc
from src.monitoring.health import BotState
from src.storage.disk_guard import DiskGuard


@pytest.fixture(autouse=True)
def _reset_state():
    BotState.is_paused, BotState.pause_reason = False, None
    BotState.last_error, BotState.last_error_at = None, None
    BotState.v2_manager = None
    DiskGuard.reset()
    yield
    BotState.is_paused, BotState.pause_reason = False, None
    BotState.last_error, BotState.last_error_at = None, None
    BotState.v2_manager = None
    DiskGuard.reset()


# ── Builders (read-only sobre la DB temporal del conftest) ───────────────────


def test_incidentes_lists_recent_risk_events():
    with models.get_session() as s:
        s.add(
            models.RiskEvent(
                event_type="circuit_breaker", severity="critical", message="3 rollbacks en 60min"
            )
        )
        s.add(models.RiskEvent(event_type="daily_stop", severity="warning", message="PnL -21"))
        s.commit()
    text = cc.build_incidentes_text()
    assert "circuit_breaker" in text and "daily_stop" in text
    assert "3 rollbacks" in text


def test_incidentes_empty_is_green():
    assert "sin eventos" in cc.build_incidentes_text()


def test_salud_reports_pause_error_and_v2_stats():
    BotState.is_paused, BotState.pause_reason = True, "circuit_breaker"
    BotState.record_error("V2 desync ticker=KXMLBGAME-...AZLAD")
    v2 = MagicMock()
    v2.stats.return_value = {
        "tracked_tickers": 240,
        "initialized_tickers": 238,
        "stale_tickers": 2,
        "recovering_sids": [1],
        "gaps_last_60s": 5,
    }
    BotState.v2_manager = v2
    text = cc.build_salud_text()
    assert "EN PAUSA" in text and "circuit_breaker" in text
    assert "desync" in text  # el último error se ve
    assert "stale=`2`" in text and "🔴" in text  # books rotos marcados


def test_funnel_shows_last_snapshot():
    with models.get_session() as s:
        s.add(
            models.Motor2FunnelSnapshot(
                odds_total=15,
                started_skip=11,
                kalshi_total=20,
                events_matched=14,
                best_net_edge_pp=-0.4,
                signals=0,
            )
        )
        s.commit()
    text = cc.build_funnel_text()
    assert "matched=`14`" in text and "-0.40pp" in text and "in-play skip=`11`" in text


def test_pnl_shows_windows_vs_limits(monkeypatch):
    from src.risk.manager import RiskManager

    settings = MagicMock(
        ACTIVE_CAPITAL_USD=180.0,
        MAX_DAILY_LOSS_PCT=3.0,
        MAX_WEEKLY_LOSS_PCT=8.0,
        MAX_MONTHLY_LOSS_PCT=15.0,
        MAX_DAILY_LOSS_FLOOR_USD=20.0,
        MAX_WEEKLY_LOSS_FLOOR_USD=40.0,
        MAX_MONTHLY_LOSS_FLOOR_USD=60.0,
    )
    monkeypatch.setattr(cc, "get_settings", lambda: settings)
    monkeypatch.setattr(
        RiskManager, "capital_status", classmethod(lambda cls: {"effective_usd": 180.0})
    )
    with models.get_session() as s:
        s.add(
            models.Trade(
                client_order_id="x",
                ticker="T",
                side="yes",
                action="buy",
                count=1,
                price_cents=40,
                strategy="motor_2_consensus",
                status="settled",
                pnl_cents=-1000,
                settled_at=cc._naive_now(),
            )
        )
        s.commit()
    text = cc.build_pnl_text()
    # límite diario = max(180×3%, piso 20) = $20; -$10 = 50% usado.
    assert "Diario" in text and "-$20.00" in text and "50% usado" in text


def test_posiciones_lists_open_and_filled():
    with models.get_session() as s:
        s.add(models.PortfolioPosition(ticker="KXMLB-AZ", side="yes", count=5, exposure_cents=200))
        s.add(
            models.Trade(
                client_order_id="r",
                ticker="KXMLB-AZ",
                side="yes",
                action="buy",
                count=10,
                price_cents=29,
                strategy="motor_1_arbitrage",
                status="filled",
            )
        )
        s.commit()
    text = cc.build_posiciones_text()
    assert "KXMLB-AZ" in text and "filled" in text and "$2.90" in text  # residual visible


def test_disco_reports_guard_and_sizes(monkeypatch):
    settings = MagicMock(DATABASE_URL="sqlite:////tmp/nope-inexistente/trades.db")
    monkeypatch.setattr(cc, "get_settings", lambda: settings)
    text = cc.build_disco_text()
    assert "Disco" in text and "estado=`ok`" in text  # DiskGuard default + no crashea sin DB


def test_ayuda_lists_all_commands():
    text = cc.build_ayuda_text()
    for cmd in ("/incidentes", "/salud", "/funnel", "/pnl", "/posiciones", "/disco"):
        assert cmd in text


# ── Dispatch en el loop (autorización + best-effort) ─────────────────────────


def _loop(monkeypatch, update, chat_id="123"):
    sent: list[str] = []

    async def _fake_send(text, **kw):
        sent.append(text)
        return True

    settings = MagicMock(TELEGRAM_CHAT_ID=chat_id, TELEGRAM_DASHBOARD_INTERVAL_SEC=0.0)
    monkeypatch.setattr(dash, "get_settings", lambda: settings)
    monkeypatch.setattr(dash, "send_alert", _fake_send)
    loop = dash.TelegramCommandLoop()
    loop._get_updates = AsyncMock(return_value=[update])
    return loop, sent


@pytest.mark.asyncio
async def test_authorized_command_center_dispatch(monkeypatch):
    monkeypatch.setitem(cc.COMMAND_BUILDERS, "/incidentes", lambda: "INC-OK")
    loop, sent = _loop(
        monkeypatch, {"update_id": 1, "message": {"chat": {"id": 123}, "text": "/incidentes"}}
    )
    await loop._poll_once()
    assert sent == ["INC-OK"]


@pytest.mark.asyncio
async def test_unauthorized_chat_gets_nothing(monkeypatch):
    """CONTROL de seguridad: otro chat NO recibe ni el read-only (silencio total)."""
    loop, sent = _loop(
        monkeypatch,
        {"update_id": 2, "message": {"chat": {"id": 999}, "text": "/incidentes"}},
        chat_id="123",
    )
    await loop._poll_once()
    assert sent == []


@pytest.mark.asyncio
async def test_broken_builder_reports_error_and_survives(monkeypatch):
    """FAIL-SAFE: un builder roto responde el error en el chat (el operador VE que falló)
    y el loop NO muere."""

    def _boom():
        raise RuntimeError("db rota")

    monkeypatch.setitem(cc.COMMAND_BUILDERS, "/disco", _boom)
    loop, sent = _loop(
        monkeypatch, {"update_id": 3, "message": {"chat": {"id": 123}, "text": "/disco"}}
    )
    await loop._poll_once()
    assert len(sent) == 1 and "Error en /disco" in sent[0]
