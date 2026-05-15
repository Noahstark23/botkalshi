"""
Motor de Gestión de Riesgo (Fase 2 Motor 1).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from loguru import logger
from sqlmodel import col, select

from src.math.arbitrage import ArbOpportunity
from src.monitoring.health import BotState
from src.monitoring.telegram_alerts import alert_risk_event
from src.storage.models import Trade, get_session
from src.utils.config import get_settings


@dataclass(frozen=True, slots=True)
class TradeDecision:
    approved: bool
    reason: str
    max_allowed_count: int


class RiskManager:
    """
    Motor de Gestión de Riesgo (Fase 2).

    Aplica controles de PnL, exposición máxima y sizing por trade.

    KNOWN TECHNICAL DEBT (Obligatorio antes de TRADING_ENABLED=true):
    - Exposición no descuenta arbitrajes ya fillados completos (sobrestima).
    - PnL realized-only: trades filled pero no settled no cuentan para stop-loss.
      Aceptable para Motor 1 (settlement rápido).
    - Race condition entre check_pre_trade concurrentes: aceptable para
      single-executor Motor 1 v1.
    """

    def __init__(self) -> None:
        self.settings = get_settings()

    async def check_pre_trade(self, opp: ArbOpportunity) -> TradeDecision:
        """Gatekeeper crítico. Debe llamarse con await desde el executor."""
        if BotState.is_paused:
            reason = BotState.pause_reason or "Razón desconocida"
            return TradeDecision(False, f"BotState.is_paused activo: {reason}", 0)

        if await self._check_timeframe_stop_losses():
            return TradeDecision(False, "Stop-Loss (Diario/Semanal/Mensual) superado", 0)

        current_exposure_usd = self._get_current_exposure_usd()
        max_total_exposure_usd = self.settings.ACTIVE_CAPITAL_USD * (
            self.settings.MAX_SIMULTANEOUS_EXPOSURE_PCT / 100.0
        )
        remaining_exposure_usd = max_total_exposure_usd - current_exposure_usd
        if remaining_exposure_usd <= 0:
            return TradeDecision(
                False,
                f"Límite Exposición Simultánea ({self.settings.MAX_SIMULTANEOUS_EXPOSURE_PCT}%) alcanzado "
                f"(actual: ${current_exposure_usd:.2f})",
                0,
            )

        max_trade_usd = self.settings.ACTIVE_CAPITAL_USD * (
            self.settings.MAX_TRADE_SIZE_PCT / 100.0
        )
        usable_usd = min(max_trade_usd, remaining_exposure_usd)

        total_cost_per_unit_cents = sum(leg.price_cents for leg in opp.legs)
        if total_cost_per_unit_cents <= 0:
            return TradeDecision(False, "Costo de oportunidad <= 0 (datos inválidos)", 0)

        max_count_by_capital = int(usable_usd * 100) // total_cost_per_unit_cents
        allowed_count = min(opp.count, max_count_by_capital)

        if allowed_count <= 0:
            return TradeDecision(
                False,
                f"Sizing final 0 contratos. usable=${usable_usd:.2f}, cost/unit={total_cost_per_unit_cents}c",
                0,
            )

        return TradeDecision(True, "Aprobado", allowed_count)

    def _get_current_exposure_usd(self) -> float:
        """Dinero bloqueado actualmente en posiciones abiertas."""
        with get_session() as s:
            stmt = select(Trade).where(col(Trade.status).in_(["pending", "filled"]))
            active_trades = list(s.exec(stmt))

        if not active_trades:
            return 0.0

        total_cents = sum(t.price_cents * t.count for t in active_trades)
        return total_cents / 100.0

    async def _check_timeframe_stop_losses(self) -> bool:
        """
        Calcula PnL realizado (UTC) on-the-fly desde tabla Trade.
        Revisa límites diario, semanal (desde el Lunes) y mensual (desde el día 1).
        
        Solo cuenta trades con status='settled'. Trades fillados pero 
        no settled NO cuentan (PnL no realizado).
        """
        # SQLite retorna datetimes sin timezone info, usamos naive UTC para comparar
        now = datetime.now(UTC).replace(tzinfo=None)
        today_start = datetime.combine(now.date(), time.min)
        
        # Lunes de esta semana a las 00:00 UTC
        days_since_monday = now.weekday()
        week_start = datetime.combine(now.date() - timedelta(days=days_since_monday), time.min)
        
        # Día 1 de este mes a las 00:00 UTC
        month_start = datetime.combine(now.date().replace(day=1), time.min)

        with get_session() as s:
            # Traer todos los trades del mes actual (rango más amplio)
            stmt = select(Trade).where(
                Trade.status == "settled",
                col(Trade.settled_at) >= month_start,
            )
            monthly_trades = list(s.exec(stmt))

        # Filtrar trades para cada timeframe en memoria
        weekly_trades = [t for t in monthly_trades if t.settled_at and t.settled_at >= week_start]
        daily_trades = [t for t in weekly_trades if t.settled_at and t.settled_at >= today_start]

        monthly_pnl_usd = sum((t.pnl_cents or 0) for t in monthly_trades) / 100.0
        weekly_pnl_usd = sum((t.pnl_cents or 0) for t in weekly_trades) / 100.0
        daily_pnl_usd = sum((t.pnl_cents or 0) for t in daily_trades) / 100.0

        limits = [
            ("Diario", daily_pnl_usd, self.settings.MAX_DAILY_LOSS_PCT),
            ("Semanal", weekly_pnl_usd, self.settings.MAX_WEEKLY_LOSS_PCT),
            ("Mensual", monthly_pnl_usd, self.settings.MAX_MONTHLY_LOSS_PCT),
        ]

        for period_name, pnl_usd, max_pct in limits:
            max_loss_usd = self.settings.ACTIVE_CAPITAL_USD * (max_pct / 100.0)
            if pnl_usd < 0 and abs(pnl_usd) >= max_loss_usd:
                await self._trigger_kill_switch(
                    f"Stop-Loss {period_name} superado: PnL=${pnl_usd:.2f}, "
                    f"límite=${-max_loss_usd:.2f} ({max_pct}%)"
                )
                return True

        return False

    async def _trigger_kill_switch(self, reason: str) -> None:
        """Pausa el bot y notifica. Idempotente (no spamea si ya pausado)."""
        if BotState.is_paused:
            return

        BotState.is_paused = True
        BotState.pause_reason = reason
        msg = f"KILL SWITCH: {reason}. Bot en pausa. Requiere intervención manual."
        logger.critical(msg)
        await alert_risk_event("kill_switch", msg)
