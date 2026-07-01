"""Tests del resumen diario de P&L (src/analytics/daily_pnl.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select

import src.storage.models as models
from src.analytics.daily_pnl import (
    DailyPnLLoop,
    aggregate_daily_pnl,
    format_digest,
)
from src.storage.models import DailyPnL, Trade

# La DB temporal autouse la monta el conftest del paquete (tests/analytics/conftest.py).

_COID = 0


def _add_trade(strategy, pnl_cents, *, settled_at, count=1, fees_cents=1, status="settled"):
    global _COID
    _COID += 1
    with models.get_session() as s:
        s.add(
            Trade(
                client_order_id=f"coid-{_COID}",
                ticker="KX-T",
                side="yes",
                action="buy",
                count=count,
                price_cents=50,
                strategy=strategy,
                status=status,
                fees_cents=fees_cents,
                pnl_cents=pnl_cents,
                settled_at=settled_at,
            )
        )
        s.commit()


def _day_window(day: datetime):
    start = datetime(day.year, day.month, day.day)
    return start, start + timedelta(days=1)


# =====================================================
# aggregate_daily_pnl (pura)
# =====================================================


def test_aggregate_groups_by_strategy():
    noon = datetime(2026, 6, 21, 12, 0)
    _add_trade("motor_2_consensus", 500, settled_at=noon, count=5)
    _add_trade("motor_2_consensus", -200, settled_at=noon, count=3)
    _add_trade("motor_rest_arb", -100, settled_at=noon, count=2)
    start, end = _day_window(noon)

    with models.get_session() as s:
        agg = aggregate_daily_pnl(s, start, end)

    m2 = next(m for m in agg.by_motor if m.strategy == "motor_2_consensus")
    assert m2.pnl_cents == 300 and m2.n_trades == 2 and m2.wins == 1 and m2.losses == 1
    assert m2.contracts == 8 and m2.win_rate_pct == 50.0
    assert agg.total_pnl_cents == 200 and agg.total_trades == 3
    assert agg.total_fees_cents == 3  # 1+1+1


def test_aggregate_excludes_non_settled_and_out_of_window():
    noon = datetime(2026, 6, 21, 12, 0)
    _add_trade("motor_2_consensus", 999, settled_at=noon, status="filled")  # no settled
    _add_trade("motor_2_consensus", 999, settled_at=noon - timedelta(days=1))  # día anterior
    _add_trade("motor_2_consensus", 999, settled_at=noon + timedelta(days=1))  # día siguiente
    _add_trade("motor_2_consensus", 100, settled_at=noon)  # el único válido
    start, end = _day_window(noon)

    with models.get_session() as s:
        agg = aggregate_daily_pnl(s, start, end)

    assert agg.total_trades == 1 and agg.total_pnl_cents == 100


def test_aggregate_empty_day():
    start, end = _day_window(datetime(2026, 6, 21))
    with models.get_session() as s:
        agg = aggregate_daily_pnl(s, start, end)
    assert agg.by_motor == [] and agg.total_pnl_cents == 0


# =====================================================
# format_digest
# =====================================================


def test_format_digest_signs_and_return():
    from datetime import date

    noon = datetime(2026, 6, 21, 12, 0)
    _add_trade("motor_2_consensus", 1316, settled_at=noon, count=10, fees_cents=20)
    _add_trade("motor_rest_arb", -261, settled_at=noon, count=4, fees_cents=10)
    start, end = _day_window(noon)
    with models.get_session() as s:
        agg = aggregate_daily_pnl(s, start, end)

    text = format_digest(date(2026, 6, 21), agg, capital_usd=300.0)
    assert "Motor 2 (consenso): +$13.16" in text
    assert "Motor REST: −$2.61" in text
    assert "TOTAL: +$10.55" in text
    assert "retorno *+3.52%*" in text  # 10.55 / 300 * 100


def test_format_digest_empty():
    from datetime import date

    from src.analytics.daily_pnl import DailyPnLAgg

    text = format_digest(date(2026, 6, 21), DailyPnLAgg(), 300.0)
    assert "Sin trades liquidados" in text


# =====================================================
# DailyPnLLoop — persistencia + idempotencia + backfill
# =====================================================


@pytest.mark.asyncio
async def test_report_day_persists_and_is_idempotent():
    from datetime import date

    day = date(2026, 6, 21)
    noon = datetime(2026, 6, 21, 12, 0)
    _add_trade("motor_2_consensus", 500, settled_at=noon, count=5, fees_cents=3)
    loop = DailyPnLLoop(interval_sec=1.0, capital_usd=300.0)

    with patch("src.analytics.daily_pnl.send_alert", new=AsyncMock()) as alert:
        first = await loop._report_day(day)
        second = await loop._report_day(day)  # idempotente

    assert first is True and second is False
    assert alert.await_count == 1  # solo una vez
    with models.get_session() as s:
        rows = list(s.exec(select(DailyPnL)))
    assert len(rows) == 1
    r = rows[0]
    assert r.date == "2026-06-21" and r.pnl == 5.0 and r.trades_count == 1
    assert r.starting_capital == 300.0 and r.ending_capital == 305.0
    assert abs(r.pnl_pct - (5.0 / 300 * 100)) < 1e-9


@pytest.mark.asyncio
async def test_report_pending_days_reports_yesterday_once():
    yesterday = datetime.now(UTC).date() - timedelta(days=1)
    y_noon = datetime(yesterday.year, yesterday.month, yesterday.day, 12, 0)
    _add_trade("motor_2_consensus", 700, settled_at=y_noon)
    loop = DailyPnLLoop(interval_sec=1.0, capital_usd=300.0)

    with patch("src.analytics.daily_pnl.send_alert", new=AsyncMock()):
        n1 = await loop.report_pending_days()
        n2 = await loop.report_pending_days()  # ya reportado

    assert n1 == 1 and n2 == 0
    with models.get_session() as s:
        assert s.exec(select(DailyPnL).where(DailyPnL.date == yesterday.isoformat())).first()


def test_exit_sell_fees_counted_in_fees_line():
    """Deuda auditoría 2026-07-01: la fila SELL de audit de un exit (settled, pnl None,
    fees seteadas) suma al renglón de fees SIN tocar el PnL ni el conteo de trades."""
    mid = datetime(2026, 6, 21, 12, 0)
    day_start, day_end = _day_window(mid)
    with models.get_session() as s:
        s.add(
            models.Trade(
                client_order_id="buy-closed",
                ticker="KXF",
                side="yes",
                action="buy",
                count=10,
                price_cents=45,
                strategy="motor_2_consensus",
                status="settled",
                closed_by_clv=True,
                pnl_cents=120,
                fees_cents=18,
                settled_at=mid,
            )
        )
        s.add(
            models.Trade(
                client_order_id="sell-audit",
                ticker="KXF",
                side="yes",
                action="sell",
                count=10,
                price_cents=60,
                strategy="motor_2_consensus",
                status="settled",
                pnl_cents=None,  # audit: el PnL vive en la BUY
                fees_cents=17,
                settled_at=mid,
            )
        )
        s.commit()

    with models.get_session() as s:
        agg = aggregate_daily_pnl(s, day_start, day_end)
    m = next(mm for mm in agg.by_motor if mm.strategy == "motor_2_consensus")
    assert m.pnl_cents == 120  # el PnL no cambia
    assert m.n_trades == 1  # la fila SELL no cuenta como trade
    assert m.fees_cents == 18 + 17  # entrada + salida
    assert agg.total_fees_cents == 35
