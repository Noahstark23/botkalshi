"""
Telegram alerts.

Notifica eventos importantes al chat configurado.
Best-effort: sin Telegram configurado NO envía, pero lo dice UNA vez en el log
(incidente 2026-07-25: "falla silenciosa" total dejó el disco llegar a 0.03GB
sin un solo aviso — el silencio absoluto de las alertas es en sí una alerta).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from loguru import logger

from src.utils.config import get_settings

if TYPE_CHECKING:
    from src.math.arbitrage import ArbOpportunity
    from src.strategies.motor_rest_arb.executor import ExecutionOutcome

# One-shot: avisar una sola vez por proceso que las alertas están desactivadas.
_unconfigured_warned = False


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
        # Incidente 2026-07-25: el disco llegó a 0.03GB libres y NINGUNA alerta salió —
        # este return silencioso era el agujero: sin Telegram configurado, TODAS las
        # alertas del bot (disco, riesgo, breakers) morían mudas sin dejar rastro.
        # One-shot por proceso (no spamear el log por cada alerta descartada).
        global _unconfigured_warned
        if not _unconfigured_warned:
            _unconfigured_warned = True
            logger.warning(
                "telegram.alertas_DESACTIVADAS: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID sin "
                "configurar — NINGÚN aviso (disco, riesgo, breakers) va a llegar. "
                f"Alerta descartada: {message[:120]!r}"
            )
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


async def alert_startup(capital_usd: float | None = None) -> None:
    """Notifica que el bot arrancó.

    `capital_usd`: capital EFECTIVO real (cash de Kalshi ya factorizado) si el runner lo
    pudo traer antes de la alerta. None → fallback al param estático, ETIQUETADO como
    config (fix 2026-07-01: 'Capital: $1200' con cash real $561 era engañoso — el sizing
    no usa ese número)."""
    settings = get_settings()
    if capital_usd is not None:
        capital_line = f"Capital: `${capital_usd:.2f}` (cash real)"
    else:
        capital_line = f"Capital: `${settings.ACTIVE_CAPITAL_USD}` (config, fallback)"
    msg = (
        f"*Kalshi Bot iniciado*\n"
        f"Env: `{settings.KALSHI_ENV}`\n"
        f"{capital_line}\n"
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
            f"*Motor REST: orden rechazada — circuit breaker pausado*\nMarket: `{ticker}`",
            urgent=True,
        )
    # else: ambas KILL → sin mensaje (no spamear)


async def alert_bet_placed(
    *,
    motor: str,
    ticker: str,
    side: str,
    count: int,
    price_cents: int,
    edge_pct: float | None = None,
    extra: str | None = None,
) -> None:
    """
    Notifica que el bot COLOCÓ (y llenó) una apuesta/orden real. Genérico y reusable por
    cualquier motor direccional (Motor 2 hoy; Motor 1/3 cuando ejecuten).

    `edge_pct` debe venir ya en PORCENTAJE (el caller convierte si su fuente es fracción).
    Best-effort: send_alert ya falla en silencio si Telegram no está configurado o la red
    cae — el caller igual lo envuelve en try/except para aislar el flujo de ejecución.
    """
    cost_usd = count * price_cents / 100
    lines = [
        f"🎰 *{motor}: apuesta colocada*",
        f"Market: `{ticker}`",
        f"Lado: `{side.upper()}` · `{count}` contrato(s) @ `{price_cents}¢`",
        f"Costo: `${cost_usd:.2f}`",
    ]
    if edge_pct is not None:
        lines.append(f"Edge estimado: `{edge_pct:.2f}%`")
    if extra:
        lines.append(extra)
    await send_alert("\n".join(lines))
