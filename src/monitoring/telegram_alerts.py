"""
Telegram alerts.

Notifica eventos importantes al chat configurado.
Falla silenciosa si Telegram no está configurado (opcional).
"""
from __future__ import annotations

import httpx
from loguru import logger

from src.utils.config import get_settings


async def send_alert(message: str, *, urgent: bool = False) -> bool:
    """
    Envía mensaje a Telegram.

    Args:
        message: Texto del mensaje (Markdown V2 escape NO automático)
        urgent: Si True, prefija con 🚨

    Returns:
        True si envió, False si Telegram no configurado o falló.
    """
    settings = get_settings()
    if not settings.telegram_configured:
        return False

    prefix = "🚨 " if urgent else ""
    text = f"{prefix}{message}"[:4000]  # Telegram cap

    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": settings.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                logger.warning(f"Telegram falló {resp.status_code}: {resp.text[:200]}")
                return False
            return True
    except Exception as e:
        logger.warning(f"Telegram error: {e}")
        return False


async def alert_startup() -> None:
    """Notifica que el bot arrancó."""
    settings = get_settings()
    msg = (
        f"*Kalshi Bot iniciado*\n"
        f"Env: `{settings.KALSHI_ENV}`\n"
        f"Capital: `${settings.ACTIVE_CAPITAL_USD}`\n"
        f"Trading: `{settings.TRADING_ENABLED}`"
    )
    await send_alert(msg)


async def alert_shutdown(reason: str = "manual") -> None:
    """Notifica shutdown."""
    await send_alert(f"*Kalshi Bot detenido*\nRazón: `{reason}`")


async def alert_risk_event(event_type: str, message: str) -> None:
    """Notifica evento del risk manager."""
    await send_alert(
        f"*Risk Event: {event_type}*\n{message}",
        urgent=True,
    )


async def alert_error(error: str) -> None:
    """Notifica error crítico."""
    await send_alert(f"*Error crítico*\n```\n{error[:500]}\n```", urgent=True)
