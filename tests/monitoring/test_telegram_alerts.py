"""Tests de telegram_alerts — formato del aviso de apuesta colocada (alert_bet_placed)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.monitoring import telegram_alerts


@pytest.mark.asyncio
async def test_alert_bet_placed_formats_message(monkeypatch):
    """El mensaje incluye motor, market, lado (upper), contratos, precio, costo y edge."""
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(telegram_alerts, "send_alert", sent)

    await telegram_alerts.alert_bet_placed(
        motor="Motor 2 (consenso deportes)",
        ticker="KXMLBGAME-26-ABC",
        side="yes",
        count=4,
        price_cents=61,
        edge_pct=3.2,
    )

    sent.assert_awaited_once()
    msg = sent.await_args.args[0]
    assert "Motor 2 (consenso deportes)" in msg
    assert "KXMLBGAME-26-ABC" in msg
    assert "YES" in msg  # side se muestra en mayúscula
    assert "4" in msg and "61" in msg
    assert "$2.44" in msg  # costo = 4 * 61 / 100
    assert "3.20%" in msg  # edge ya viene en porcentaje


@pytest.mark.asyncio
async def test_alert_bet_placed_omits_edge_when_none(monkeypatch):
    """Sin edge_pct, el mensaje no incluye la línea de edge (pero sí el resto)."""
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(telegram_alerts, "send_alert", sent)

    await telegram_alerts.alert_bet_placed(
        motor="Motor 1", ticker="T-1", side="no", count=1, price_cents=40
    )

    msg = sent.await_args.args[0]
    assert "Edge" not in msg
    assert "NO" in msg and "$0.40" in msg


@pytest.mark.asyncio
async def test_alert_bet_placed_silent_when_telegram_not_configured(monkeypatch):
    """send_alert real devuelve False sin config → alert_bet_placed no lanza."""
    monkeypatch.setattr(
        telegram_alerts, "get_settings", lambda: type("S", (), {"telegram_configured": False})()
    )
    # No debe lanzar aunque Telegram no esté configurado.
    await telegram_alerts.alert_bet_placed(
        motor="Motor 2", ticker="T", side="yes", count=1, price_cents=50, edge_pct=2.0
    )


@pytest.mark.asyncio
class TestSendAlertParseFallback:
    """
    Incidente 2026-07-28: HTTP 400 'can't parse entities' perdía la alerta. El digest
    diario de PnL sale por este canal y es el instrumento del mes de prueba: no puede
    perderse por el formato del mensaje.
    """

    @staticmethod
    def _settings(monkeypatch):
        s = MagicMock()
        s.telegram_configured = True
        s.TELEGRAM_BOT_TOKEN = "t"
        s.TELEGRAM_CHAT_ID = "c"
        monkeypatch.setattr(telegram_alerts, "get_settings", lambda: s)

    async def test_reintenta_en_texto_plano_si_markdown_falla(self, monkeypatch):
        self._settings(monkeypatch)
        calls: list[dict] = []

        class FakeResp:
            def __init__(self, code, text=""):
                self.status_code = code
                self.text = text

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json):
                calls.append(json)
                if "parse_mode" in json:
                    return FakeResp(400, "Bad Request: can't parse entities")
                return FakeResp(200)

        monkeypatch.setattr(telegram_alerts.httpx, "AsyncClient", lambda **kw: FakeClient())
        assert await telegram_alerts.send_alert("precio *roto_ [x") is True
        assert len(calls) == 2
        assert "parse_mode" in calls[0]
        assert "parse_mode" not in calls[1]  # el fallback va sin formato

    async def test_error_no_de_parseo_no_reintenta(self, monkeypatch):
        """401/403 (credenciales) no se reintenta: no es problema de formato."""
        self._settings(monkeypatch)
        calls: list[dict] = []

        class FakeResp:
            status_code = 401
            text = "Unauthorized"

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json):
                calls.append(json)
                return FakeResp()

        monkeypatch.setattr(telegram_alerts.httpx, "AsyncClient", lambda **kw: FakeClient())
        assert await telegram_alerts.send_alert("hola") is False
        assert len(calls) == 1

    async def test_markdown_ok_no_usa_fallback(self, monkeypatch):
        self._settings(monkeypatch)
        calls: list[dict] = []

        class FakeResp:
            status_code = 200
            text = ""

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json):
                calls.append(json)
                return FakeResp()

        monkeypatch.setattr(telegram_alerts.httpx, "AsyncClient", lambda **kw: FakeClient())
        assert await telegram_alerts.send_alert("*ok*") is True
        assert len(calls) == 1
