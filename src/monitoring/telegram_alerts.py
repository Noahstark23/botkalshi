"""
Telegram alerts.

Notifica eventos importantes al chat configurado.
Falla silenciosa si Telegram no está configurado (opcional).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from loguru import logger

from src.utils.config import get_settings

if TYPE_CHECKING:
    from src.math.arbitrage import ArbOpportunity
    from src.strategies.motor_rest_arb.executor import ExecutionOutcome


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


async def alert_orderbook_anomaly(
    kind: str,  # "sid_gap_warning" | "sid_gap_critical" | "manager_zombie"
    details: str,
) -> None:
    """Notifica anomalía del orderbook manager V2."""
    urgent = kind == "sid_gap_critical"
    await send_alert(f"*Orderbook {kind}*\n{details}", urgent=urgent)


async def alert_trade(outcome: ExecutionOutcome, opp: ArbOpportunity) -> None:
    """
    Notifica el resultado de una ejecución del Motor REST.

    ANTI-DUPLICADO — una campana por evento (orden de guardas):
      - kill_switch_fired → NO alertar: el executor YA mandó alert_error desde
        _fire_kill_switch (executor.py). Re-alertar acá sonaría dos veces.
      - filled → mensaje normal: el arb se capturó completo.
      - rollback_triggered → urgent: hubo exposición y se intentó cerrar.
      - rejected_paused → urgent: el circuit breaker rechazó la orden (el executor
        solo lo loguea CRITICAL, no manda Telegram → esta es su única campana).
      - ambas patas KILL (nada de lo anterior) → SIN mensaje: ventana cerrada sin
        exposición, no es un evento. No spamea el chat.
    """
    if outcome.kill_switch_fired:
        return  # ya alertó el executor (alert_error) — no duplicar

    ticker = opp.legs[0].market_ticker if opp.legs else "?"
    leg_states = "/".join(s.value for s in outcome.leg_states)

    if outcome.filled:
        await send_alert(
            f"✅ *Motor REST: Arb ejecutado*\n"
            f"Market: `{ticker}`\n"
            f"Contratos: `{opp.count}` | Net: `{opp.net_profit_cents}c` "
            f"(edge `{opp.edge_pct:.2f}%`)"
        )
    elif outcome.rollback_triggered:
        await send_alert(
            f"*Motor REST: rollback ejecutado*\n"
            f"Market: `{ticker}` | Legs: `{leg_states}`\n"
            f"Posición recuperada: `{outcome.rollback_filled}`",
            urgent=True,
        )
    elif outcome.rejected_paused:
        await send_alert(
            f"*Motor REST: orden rechazada — circuit breaker pausado*\n"
            f"Market: `{ticker}`",
            urgent=True,
        )
    # else: ambas KILL → sin mensaje (no spamear)
