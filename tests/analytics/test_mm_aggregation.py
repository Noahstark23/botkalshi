"""Agregador MM del Analyst Loop (Motor 5 F1 — tracker del gate F1→F2)."""

from __future__ import annotations

from datetime import timedelta

from sqlmodel import Session

from src.analytics.analyst_loop import _naive_utc_now, aggregate_mm
from src.storage.models import MMFunnelSnapshot


def test_aggregate_mm_empty_window(_tmp_db_engine):
    with Session(_tmp_db_engine) as s:
        agg = aggregate_mm(s, _naive_utc_now() - timedelta(hours=24))
    assert agg.cycles == 0 and agg.quoted == 0 and agg.fills == 0


def test_aggregate_mm_sums_flows_and_takes_last_stock(_tmp_db_engine):
    """quoted/fills se SUMAN (flujos); mtm/inventario se toma el ÚLTIMO (stocks)."""
    now = _naive_utc_now()
    with Session(_tmp_db_engine) as s:
        s.add(
            MMFunnelSnapshot(
                quoted=5,
                fills=1,
                mtm_pnl_cents=-10,
                inventory_abs=10,
                created_at=now - timedelta(hours=2),
            )
        )
        s.add(
            MMFunnelSnapshot(
                quoted=7,
                fills=2,
                mtm_pnl_cents=35,
                inventory_abs=20,
                created_at=now - timedelta(hours=1),
            )
        )
        # fuera de ventana:
        s.add(
            MMFunnelSnapshot(
                quoted=99,
                fills=99,
                mtm_pnl_cents=999,
                inventory_abs=99,
                created_at=now - timedelta(hours=48),
            )
        )
        s.commit()
        agg = aggregate_mm(s, now - timedelta(hours=24))
    assert agg.cycles == 2
    assert agg.quoted == 12 and agg.fills == 3
    assert agg.mtm_last_cents == 35 and agg.inventory_abs_last == 20
