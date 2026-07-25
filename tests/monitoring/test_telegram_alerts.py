"""Tests de telegram_alerts — formato del aviso de apuesta colocada (alert_bet_placed)."""

from __future__ import annotations

from unittest.mock import AsyncMock

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


# =====================================================
# Incidente 2026-07-25: alertas MUDAS con Telegram sin configurar
# =====================================================


@pytest.fixture(autouse=True)
def _reset_unconfigured_warned():
    telegram_alerts._unconfigured_warned = False
    yield
    telegram_alerts._unconfigured_warned = False


def _capture_warnings():
    from loguru import logger

    records: list[str] = []
    sink = logger.add(lambda m: records.append(str(m)), level="WARNING")
    return records, sink


@pytest.mark.asyncio
async def test_send_alert_unconfigured_warns_once(monkeypatch):
    """Sin Telegram configurado, send_alert loguea UNA VEZ que las alertas están
    desactivadas (el disco llegó a 0.03GB sin un solo aviso y sin rastro del porqué)
    y no spamea en llamadas siguientes."""
    from loguru import logger

    monkeypatch.setattr(
        telegram_alerts, "get_settings", lambda: type("S", (), {"telegram_configured": False})()
    )
    records, sink = _capture_warnings()
    try:
        assert await telegram_alerts.send_alert("disco critical") is False
        assert await telegram_alerts.send_alert("otra alerta") is False
        assert await telegram_alerts.send_alert("y otra") is False
    finally:
        logger.remove(sink)

    warns = [r for r in records if "alertas_DESACTIVADAS" in r]
    assert len(warns) == 1  # one-shot: una línea, no una por alerta
    assert "disco critical" in warns[0]  # incluye la primera alerta descartada


@pytest.mark.asyncio
async def test_send_alert_configured_does_not_warn(monkeypatch):
    """CONTROL: con Telegram configurado no aparece el aviso de desactivadas."""
    from unittest.mock import MagicMock, patch

    from loguru import logger

    s = type(
        "S",
        (),
        {"telegram_configured": True, "TELEGRAM_BOT_TOKEN": "t0k3n", "TELEGRAM_CHAT_ID": "42"},
    )()
    monkeypatch.setattr(telegram_alerts, "get_settings", lambda: s)
    records, sink = _capture_warnings()
    resp = MagicMock(status_code=200)
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=resp)
    try:
        with patch.object(telegram_alerts.httpx, "AsyncClient", return_value=client):
            assert await telegram_alerts.send_alert("hola") is True
    finally:
        logger.remove(sink)

    assert not any("alertas_DESACTIVADAS" in r for r in records)
