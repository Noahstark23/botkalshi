"""
Daily P&L digest — resumen diario de P&L por Telegram (ADVISORY ONLY, read-only).

Bucle agendado que, una vez por día UTC, agrega el P&L REALIZADO (trades 'settled') del
último día completo por motor, lo PERSISTE en `DailyPnL` (memoria + idempotencia por
`date` único; alimenta /health y check_portfolio) y manda un digest a Telegram. La métrica
clave de la Fase 2 es el `retorno del día %` (pnl / capital).

⛔ NO tradea, NO cambia config, NO toca capital. Es ASESOR. Best-effort: un tick que falla
se registra (BotState.record_error) y el loop sigue (patrón AnalystLoop / SettlementPoller).

Honestidad: el P&L es por fecha de SETTLEMENT (cuándo se realizó), no de apuesta — una
apuesta de hoy suele liquidar mañana, cuando termina el partido. Es el número realizado,
igual que lo computan el RiskManager y check_portfolio.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from loguru import logger
from sqlmodel import Session, col, select

from src.monitoring.health import BotState
from src.monitoring.telegram_alerts import send_alert
from src.storage.models import DailyPnL, Trade, get_session
from src.utils.config import get_settings

# strategy → etiqueta legible para el digest. Lo no mapeado se muestra tal cual.
_MOTOR_LABELS = {
    "motor_2_consensus": "Motor 2 (consenso)",
    "motor_rest_arb": "Motor REST",
    "motor_1_arbitrage": "Motor 1 (arb)",
    "motor_3_clv": "Motor 3 (CLV)",
}


def _naive_utc_today() -> date:
    return datetime.now(UTC).date()


def _money(cents: int) -> str:
    """Centavos → string de dólares con signo: 1316 → '+$13.16', -261 → '−$2.61'."""
    return f"{'+' if cents >= 0 else '−'}${abs(cents) / 100:.2f}"


def _motor_label(strategy: str) -> str:
    return _MOTOR_LABELS.get(strategy, strategy)


# =====================================================
# Skill pura (agregación) — testeable sin red ni loop
# =====================================================


@dataclass(frozen=True, slots=True)
class MotorPnL:
    strategy: str
    pnl_cents: int
    n_trades: int
    wins: int  # pnl_cents > 0
    losses: int  # pnl_cents < 0
    contracts: int
    fees_cents: int

    @property
    def win_rate_pct(self) -> float:
        return (self.wins / self.n_trades * 100) if self.n_trades else 0.0


@dataclass(frozen=True, slots=True)
class DailyPnLAgg:
    by_motor: list[MotorPnL] = field(default_factory=list)
    total_pnl_cents: int = 0
    total_trades: int = 0
    total_wins: int = 0
    total_losses: int = 0
    total_fees_cents: int = 0

    @property
    def total_win_rate_pct(self) -> float:
        return (self.total_wins / self.total_trades * 100) if self.total_trades else 0.0


def aggregate_daily_pnl(session: Session, day_start: datetime, day_end: datetime) -> DailyPnLAgg:
    """
    Agrega el P&L REALIZADO de los trades 'settled' con settled_at ∈ [day_start, day_end).

    Agrupa por `strategy`. day_start/day_end son NAIVE UTC (igual que settled_at). Solo
    cuenta filas con pnl_cents no None (las realizadas). Pura/testeable.
    """
    rows = list(
        session.exec(
            select(Trade).where(
                Trade.status == "settled",
                col(Trade.settled_at) >= day_start,
                col(Trade.settled_at) < day_end,
                col(Trade.pnl_cents).is_not(None),
            )
        )
    )

    by_strategy: dict[str, list[Trade]] = {}
    for t in rows:
        by_strategy.setdefault(t.strategy, []).append(t)

    motors: list[MotorPnL] = []
    for strategy, trades in by_strategy.items():
        pnls = [t.pnl_cents or 0 for t in trades]
        motors.append(
            MotorPnL(
                strategy=strategy,
                pnl_cents=sum(pnls),
                n_trades=len(trades),
                wins=sum(1 for p in pnls if p > 0),
                losses=sum(1 for p in pnls if p < 0),
                contracts=sum(t.count for t in trades),
                fees_cents=sum(t.fees_cents or 0 for t in trades),
            )
        )

    return DailyPnLAgg(
        by_motor=motors,
        total_pnl_cents=sum(m.pnl_cents for m in motors),
        total_trades=sum(m.n_trades for m in motors),
        total_wins=sum(m.wins for m in motors),
        total_losses=sum(m.losses for m in motors),
        total_fees_cents=sum(m.fees_cents for m in motors),
    )


def format_digest(day: date, agg: DailyPnLAgg, capital_usd: float) -> str:
    """Arma el texto del digest de Telegram (Markdown)."""
    ret_pct = (agg.total_pnl_cents / 100 / capital_usd * 100) if capital_usd else 0.0
    lines = [f"📈 *P&L diario — {day.isoformat()}* (UTC, liquidado)"]
    if not agg.by_motor:
        lines.append("Sin trades liquidados este día.")
    for m in sorted(agg.by_motor, key=lambda x: x.pnl_cents, reverse=True):
        lines.append(
            f"{_motor_label(m.strategy)}: {_money(m.pnl_cents)} · {m.n_trades} trades · "
            f"{m.win_rate_pct:.0f}% win · {m.contracts} contratos"
        )
    lines.append("──────────")
    lines.append(
        f"TOTAL: {_money(agg.total_pnl_cents)} · fees {_money(-agg.total_fees_cents)} · "
        f"retorno *{ret_pct:+.2f}%* (cap ${capital_usd:.0f})"
    )
    return "\n".join(lines)


# =====================================================
# El loop
# =====================================================


class DailyPnLLoop:
    """Loop advisory-only: una vez por día UTC computa, persiste y reporta el P&L del día."""

    MAX_BACKFILL_DAYS = 7  # tope de días a recuperar en el primer arranque / tras outage

    def __init__(
        self, *, interval_sec: float | None = None, capital_usd: float | None = None
    ) -> None:
        s = get_settings() if (interval_sec is None or capital_usd is None) else None
        self._interval = (
            interval_sec if interval_sec is not None else s.DAILY_PNL_REPORT_INTERVAL_SEC
        )
        self._capital = capital_usd if capital_usd is not None else s.ACTIVE_CAPITAL_USD

    async def run(self, stop_event: asyncio.Event) -> None:
        logger.info(f"daily_pnl loop started interval={self._interval:.0f}s")
        while not stop_event.is_set():
            try:
                await self.report_pending_days()
            except Exception as exc:
                logger.exception("daily_pnl.tick_failed")
                BotState.record_error(f"daily_pnl: {type(exc).__name__}: {exc}")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval)

    async def report_pending_days(self) -> int:
        """Reporta cada día UTC COMPLETO sin fila DailyPnL, desde el último hasta ayer."""
        yesterday = _naive_utc_today() - timedelta(days=1)
        with get_session() as s:
            last = s.exec(select(DailyPnL).order_by(col(DailyPnL.date).desc())).first()
        first = (date.fromisoformat(last.date) + timedelta(days=1)) if last else yesterday
        # Tope de backfill (evita un flood si la DB venía sin reportes).
        earliest = yesterday - timedelta(days=self.MAX_BACKFILL_DAYS)
        if first < earliest:
            first = earliest

        reported = 0
        day = first
        while day <= yesterday:
            if await self._report_day(day):
                reported += 1
            day += timedelta(days=1)
        return reported

    async def _report_day(self, day: date) -> bool:
        """Computa, persiste DailyPnL(date=day) e informa. False si ya existía (idempotente)."""
        day_start = datetime(day.year, day.month, day.day)  # naive UTC medianoche
        day_end = day_start + timedelta(days=1)
        with get_session() as s:
            if s.exec(select(DailyPnL).where(DailyPnL.date == day.isoformat())).first():
                return False  # ya reportado → idempotente (también lo blinda el unique de date)
            agg = aggregate_daily_pnl(s, day_start, day_end)
            pnl_usd = agg.total_pnl_cents / 100
            s.add(
                DailyPnL(
                    date=day.isoformat(),
                    starting_capital=self._capital,
                    ending_capital=self._capital + pnl_usd,
                    pnl=pnl_usd,
                    pnl_pct=(pnl_usd / self._capital * 100) if self._capital else 0.0,
                    trades_count=agg.total_trades,
                    winning_trades=agg.total_wins,
                    losing_trades=agg.total_losses,
                    strategy_breakdown=json.dumps(
                        {
                            m.strategy: {
                                "pnl_cents": m.pnl_cents,
                                "n": m.n_trades,
                                "wins": m.wins,
                                "contracts": m.contracts,
                                "fees_cents": m.fees_cents,
                            }
                            for m in agg.by_motor
                        }
                    ),
                )
            )
            s.commit()

        digest = format_digest(day, agg, self._capital)
        logger.info(f"daily_pnl.reported date={day.isoformat()} pnl={_money(agg.total_pnl_cents)}")
        with contextlib.suppress(Exception):
            await send_alert(digest)  # no-op si Telegram no está configurado
        return True
