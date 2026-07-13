"""
Dashboard de Telegram on-demand (/dashboard) — TODA la foto del bot, READ-ONLY.

Reutiliza lo que ya existe, sin duplicar lógica:
  - analytics.daily_pnl.aggregate_daily_pnl + MotorPnL (PnL realizado por motor, win rate,
    avg win/loss, fees) y los formateadores _money / _motor_label.
  - risk.manager.RiskManager.capital_status() (mode/raw/effective/paused) — igual que /status.
  - monitoring.health.BotState (is_paused, pause_reason).
  - monitoring.telegram_alerts.send_alert (cliente Telegram ya configurado).

⛔ NO tradea, NO cambia config, NO toca el contador de PnL. Todas las lecturas son read-only.
El command handler es long-polling (getUpdates) y SOLO responde al TELEGRAM_CHAT_ID autorizado.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC, datetime, timedelta

import httpx
from loguru import logger
from sqlmodel import col, select

from src.analytics.daily_pnl import _money, _motor_label, aggregate_daily_pnl
from src.monitoring.health import BotState
from src.monitoring.telegram_alerts import send_alert
from src.storage.models import PortfolioPosition, Trade, get_session
from src.utils.config import get_settings

_EPOCH_NAIVE = datetime(2000, 1, 1)  # piso para el agregado "total acumulado" (settled_at naive)


def build_dashboard_text(now: datetime | None = None) -> str:
    """Arma el texto Markdown del dashboard. READ-ONLY: abre su propia sesión y solo LEE.

    Reglas de tiempo (heredadas del schema existente):
      - PnL realizado: por `settled_at` (NAIVE UTC) → mismas ventanas que daily_pnl.
      - Trades de hoy: por `placed_at` (AWARE UTC) → igual que /status.
    """
    settings = get_settings()
    now = now or datetime.now(UTC)

    today_naive = datetime(now.year, now.month, now.day)
    tomorrow_naive = today_naive + timedelta(days=1)
    week_naive = today_naive - timedelta(days=6)  # últimos 7 días (incluye hoy)
    month_naive = today_naive - timedelta(days=29)  # últimos 30 días
    today_aware = datetime(now.year, now.month, now.day, tzinfo=UTC)

    # Import diferido: RiskManager importa BotState de health → evita el ciclo (igual que /status).
    from src.risk.manager import RiskManager

    cap = RiskManager.capital_status()
    eff_usd = cap.get("effective_usd") or settings.ACTIVE_CAPITAL_USD

    with get_session() as s:
        today = aggregate_daily_pnl(s, today_naive, tomorrow_naive)
        week = aggregate_daily_pnl(s, week_naive, tomorrow_naive)
        month = aggregate_daily_pnl(s, month_naive, tomorrow_naive)
        total = aggregate_daily_pnl(s, _EPOCH_NAIVE, tomorrow_naive)
        trades_today = list(s.exec(select(Trade).where(col(Trade.placed_at) >= today_aware)))
        open_positions = list(s.exec(select(PortfolioPosition)))
        clv_closes = len(
            list(
                s.exec(
                    select(Trade).where(col(Trade.closed_by_clv).is_(True), Trade.action == "buy")
                )
            )
        )

    paused = BotState.is_paused
    estado = "⏸️ EN PAUSA" if paused else "▶️ Corriendo"

    # 🔴=vende de verdad (EXECUTION on) / ✅=corriendo en shadow / ⬜=apagado — sin esto
    # era imposible distinguir shadow de live desde el monitoreo (deuda auditoría).
    def _motor_icon(enabled: bool, execution: bool) -> str:
        return "🔴 LIVE" if (enabled and execution) else ("✅" if enabled else "⬜")

    motors = (
        f"M1 {_motor_icon(settings.MOTOR_1_ARBITRAGE_ENABLED, False)} · "
        f"M2 {_motor_icon(settings.MOTOR_2_SPORTSBOOK_ENABLED, settings.MOTOR_2_EXECUTION_ENABLED)} · "
        f"M3 {_motor_icon(settings.MOTOR_3_CLV_ENABLED, settings.MOTOR_3_EXECUTION_ENABLED)} · "
        f"M5 {_motor_icon(settings.MOTOR_MM_ENABLED, settings.MOTOR_MM_EXECUTION_ENABLED)}"
    )

    lines: list[str] = [
        f"🤖 *Dashboard KalshiBot* · {now:%H:%M} UTC",
        f"*Estado:* {estado}",
    ]
    if paused and BotState.pause_reason:
        lines.append(f"_motivo:_ {BotState.pause_reason}")
    lines.append(f"*Motores:* {motors}")

    # 💰 Capital
    raw = cap.get("raw_balance_usd")
    raw_s = f"${raw:.2f}" if raw is not None else "—"
    lines += [
        "",
        f"💰 *Capital* ({cap.get('mode', '?')})",
        f"Balance real: `{raw_s}` · Efectivo: `${eff_usd:.2f}`"
        + ("  ⚠️ entradas pausadas" if cap.get("is_paused") else ""),
    ]

    # 📊 PnL realizado (settled)
    lines += [
        "",
        "📊 *PnL realizado* (liquidado, UTC)",
        f"Hoy: `{_money(today.total_pnl_cents)}` · Semana: `{_money(week.total_pnl_cents)}`",
        f"Mes: `{_money(month.total_pnl_cents)}` · Total: `{_money(total.total_pnl_cents)}`",
    ]

    # 🏆 Por motor (acumulado — donde avg win/loss tiene muestra)
    lines += ["", "🏆 *Por motor* (acumulado)"]
    if not total.by_motor:
        lines.append("Sin trades liquidados todavía.")
    for m in sorted(total.by_motor, key=lambda x: x.pnl_cents, reverse=True):
        lines.append(
            f"{_motor_label(m.strategy)}: `{_money(m.pnl_cents)}` · {m.n_trades}t · "
            f"{m.win_rate_pct:.0f}% win · avgW `{_money(round(m.avg_win_cents))}` / "
            f"avgL `{_money(round(m.avg_loss_cents))}` · fees `{_money(-m.fees_cents)}`"
        )

    # 🎯 Trading hoy
    win_today = sum(1 for t in trades_today if t.pnl_cents and t.pnl_cents > 0)
    loss_today = sum(1 for t in trades_today if t.pnl_cents and t.pnl_cents < 0)
    pend_today = sum(1 for t in trades_today if t.status == "pending")
    lines += [
        "",
        "🎯 *Trading hoy*",
        f"Trades: `{len(trades_today)}` (✅{win_today} ❌{loss_today} ⏳{pend_today}) · "
        f"Posiciones abiertas: `{len(open_positions)}`",
    ]

    # 🛑 Stop-loss — % consumido del límite (pérdida realizada del período / límite)
    lines += ["", "🛑 *Stop-loss* (consumido del límite)"]
    for label, pct, agg in (
        ("Diario", settings.MAX_DAILY_LOSS_PCT, today),
        ("Semanal", settings.MAX_WEEKLY_LOSS_PCT, week),
        ("Mensual", settings.MAX_MONTHLY_LOSS_PCT, month),
    ):
        limit_usd = pct / 100.0 * eff_usd
        loss_usd = max(0.0, -agg.total_pnl_cents / 100.0)
        consumed = (loss_usd / limit_usd * 100.0) if limit_usd else 0.0
        lines.append(f"{label}: `{consumed:.1f}%` de `${limit_usd:.2f}` ({pct:.0f}%)")

    # 🔒 Trailing / CLV
    lines += [
        "",
        "🔒 *Cierre anticipado (Motor 3 / CLV)*",
        f"Trailing: `{'ON' if settings.MOTOR_3_TRAILING_ENABLED else 'OFF'}` "
        f"(drop {settings.MOTOR_3_TRAILING_DROP_CENTS}¢) · "
        f"Take-profit: `{'ON' if settings.MOTOR_3_TAKE_PROFIT_ENABLED else 'OFF'}` "
        f"({settings.MOTOR_3_TAKE_PROFIT_CENTS}¢)",
        f"Cierres por CLV (closed_by_clv): `{clv_closes}`",
    ]

    return "\n".join(lines)


class TelegramCommandLoop:
    """Long-polling de Telegram (getUpdates) para comandos on-demand, SOLO desde el
    TELEGRAM_CHAT_ID autorizado. Best-effort: un fallo se loguea y el loop sigue.

    Centro de comando C1 (2026-07-12): además del /dashboard, despacha los comandos
    READ-ONLY de monitoring.command_center (incidentes/salud/funnel/pnl/posiciones/disco).
    Nivel 0 por diseño: acá NO se muta nada — pausar/reanudar es C2/C3 (tiers + token)."""

    POLL_TIMEOUT_SEC = 25  # long-poll: Telegram retiene la conexión hasta este tiempo
    COMMANDS = ("/dashboard", "/dash", "/status")

    def __init__(self) -> None:
        self.settings = get_settings()
        self._offset: int | None = None  # update_id del próximo update a traer
        self._last_auto_send: float | None = None

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self.settings.telegram_configured:
            logger.info("telegram.dashboard: Telegram no configurado → no arranca")
            return
        logger.info(
            f"telegram.dashboard loop started (commands={self.COMMANDS}, "
            f"auto_interval={self.settings.TELEGRAM_DASHBOARD_INTERVAL_SEC:.0f}s)"
        )
        while not stop_event.is_set():
            try:
                await self._poll_once()
                await self._maybe_auto_send()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"telegram.dashboard.tick: {type(exc).__name__}: {exc}")
                BotState.record_error(f"telegram.dashboard: {type(exc).__name__}: {exc}")
                with contextlib.suppress(TimeoutError):  # backoff ante error (rate limit / red)
                    await asyncio.wait_for(stop_event.wait(), timeout=5.0)

    async def _poll_once(self) -> None:
        from src.monitoring.command_center import COMMAND_BUILDERS

        for update in await self._get_updates():
            self._offset = update.get("update_id", 0) + 1  # ack: no re-traer este update
            msg = update.get("message") or update.get("channel_post") or {}
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            text = (msg.get("text") or "").strip()
            cmd = text.split(maxsplit=1)[0].split("@")[0] if text else ""
            if cmd not in self.COMMANDS and cmd not in COMMAND_BUILDERS:
                continue
            if chat_id != str(self.settings.TELEGRAM_CHAT_ID):
                logger.warning(
                    f"telegram.dashboard: {cmd} de chat NO autorizado {chat_id} → ignorado"
                )
                continue
            logger.info(f"telegram.dashboard: {cmd} solicitado por chat autorizado")
            if cmd in self.COMMANDS:
                await self._send_dashboard()
            else:
                await self._send_command(cmd, COMMAND_BUILDERS[cmd])

    async def _send_command(self, cmd: str, builder) -> None:
        """Ejecuta un builder del centro de comando. Best-effort: un builder roto responde
        el error en el chat (el operador VE que falló) y el loop sigue."""
        try:
            text = builder()
        except Exception as exc:
            logger.exception(f"telegram.command_center.{cmd}_failed")
            text = f"⚠️ Error en {cmd}: {type(exc).__name__}"
        await send_alert(text)

    async def _maybe_auto_send(self) -> None:
        interval = self.settings.TELEGRAM_DASHBOARD_INTERVAL_SEC
        if interval <= 0:
            return
        now = time.monotonic()
        if self._last_auto_send is None:
            self._last_auto_send = now  # ancla: no envía apenas arranca
            return
        if now - self._last_auto_send >= interval:
            self._last_auto_send = now
            logger.info("telegram.dashboard: auto-envío periódico")
            await self._send_dashboard()

    async def _send_dashboard(self) -> None:
        try:
            text = build_dashboard_text()
        except Exception as exc:
            logger.exception("telegram.dashboard.build_failed")
            text = f"⚠️ Error armando el dashboard: {type(exc).__name__}"
        await send_alert(text)

    async def _get_updates(self) -> list[dict]:
        """getUpdates con long-poll. Devuelve [] ante cualquier error (no crashea el loop).
        Nota: si el bot tiene un webhook seteado, getUpdates da 409 → degradación limpia."""
        url = f"https://api.telegram.org/bot{self.settings.TELEGRAM_BOT_TOKEN}/getUpdates"
        params: dict[str, object] = {"timeout": self.POLL_TIMEOUT_SEC}
        if self._offset is not None:
            params["offset"] = self._offset
        async with httpx.AsyncClient(timeout=self.POLL_TIMEOUT_SEC + 10) as client:
            resp = await client.get(url, params=params)
        if resp.status_code != 200:
            logger.warning(f"telegram.getUpdates {resp.status_code}: {resp.text[:200]}")
            return []
        return resp.json().get("result", []) or []
