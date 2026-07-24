"""Auditoría de pérdidas estructurales — los 3 mecanismos, pineados con números reales."""

from __future__ import annotations

from datetime import timedelta

from src.analytics.loss_audit import (
    audit_motor1_arbs,
    fee_drag_by_strategy,
    motor2_buckets,
)
from src.analytics.shadow_fee_recalc import FEE_FIX_AT_DEFAULT, old_fee_cents
from src.math.fees import kalshi_fee_cents
from src.storage.models import Trade

PRE = FEE_FIX_AT_DEFAULT - timedelta(days=2)
POST = FEE_FIX_AT_DEFAULT + timedelta(hours=6)


def _t(**kw) -> Trade:
    base = {
        "client_order_id": kw.pop("coid"),
        "ticker": "KXMLBGAME-26JUN30TEST",
        "side": "yes",
        "action": "buy",
        "count": 10,
        "price_cents": 50,
        "strategy": "motor_2_consensus",
        "status": "settled",
        "placed_at": PRE,
    }
    base.update(kw)
    return Trade(**base)


# =====================================================
# DB — fee drag oculto
# =====================================================


def test_fee_drag_counts_only_prefix_rows():
    rows = [
        _t(coid="a", fees_cents=old_fee_cents(10, 50), placed_at=PRE),
        _t(coid="b", fees_cents=kalshi_fee_cents(10, 50), placed_at=POST),  # post-fix: real
    ]
    drag = fee_drag_by_strategy(rows)
    agg = drag["motor_2_consensus"]
    assert agg.rows == 1  # la post-fix no entra
    assert agg.drag_cents == kalshi_fee_cents(10, 50) - old_fee_cents(10, 50)
    assert agg.drag_cents > 0  # la DB se veía MEJOR que la realidad


# =====================================================
# M1 — el caso real del 30-jun: perdedor determinístico
# =====================================================


def test_motor1_pair_243_is_deterministic_loser():
    """El par real: 243×243 a 40+57=97¢ → gross $7.29, fees reales ~$8.26 → perdido
    AL COLOCARSE. El fee bug lo mostraba como +$7.25 'garantizados'."""
    rows = [
        _t(
            coid="y",
            strategy="motor_1_arbitrage",
            side="yes",
            count=243,
            price_cents=40,
            status="filled",
        ),
        _t(
            coid="n",
            strategy="motor_1_arbitrage",
            side="no",
            count=243,
            price_cents=57,
            status="filled",
            placed_at=PRE + timedelta(seconds=1),
        ),
    ]
    audit = audit_motor1_arbs(rows)
    assert audit.groups == 1 and audit.paired_contracts == 243
    assert audit.gross_cents == 3 * 243  # 729c
    assert audit.real_fees_cents > audit.gross_cents  # fees > gross
    assert audit.deterministic_losers == 1
    assert audit.net_real_cents < 0


def test_motor1_wide_spread_pair_is_genuinely_profitable():
    """Control: un arb con spread ancho (40+45=85 → gross 15¢/contrato) SÍ es
    net-positivo con fees reales — no todo arb era fantasma."""
    rows = [
        _t(
            coid="y2",
            strategy="motor_1_arbitrage",
            side="yes",
            count=100,
            price_cents=40,
            status="filled",
        ),
        _t(
            coid="n2",
            strategy="motor_1_arbitrage",
            side="no",
            count=100,
            price_cents=45,
            status="filled",
            placed_at=PRE + timedelta(seconds=2),
        ),
    ]
    audit = audit_motor1_arbs(rows)
    assert audit.groups == 1 and audit.deterministic_losers == 0
    assert audit.net_real_cents > 0


def test_motor1_pairs_respect_time_window():
    """Patas del mismo ticker colocadas HORAS aparte no se emparejan (son arbs
    distintos): sin par → no entran al audit de hedges."""
    rows = [
        _t(
            coid="y3",
            strategy="motor_1_arbitrage",
            side="yes",
            count=10,
            price_cents=40,
            status="filled",
        ),
        _t(
            coid="n3",
            strategy="motor_1_arbitrage",
            side="no",
            count=10,
            price_cents=57,
            status="filled",
            placed_at=PRE + timedelta(hours=3),
        ),
    ]
    assert audit_motor1_arbs(rows).groups == 0


# =====================================================
# M2 — buckets con fees reales
# =====================================================


def test_motor2_buckets_classify_and_adjust():
    rows = [
        # bucket 5-8: ganadora pre-fix (drag la reduce un poco)
        _t(coid="w", estimated_edge_pct=0.06, pnl_cents=500, fees_cents=old_fee_cents(10, 50)),
        # bucket >=11: perdedora
        _t(coid="l", estimated_edge_pct=0.12, pnl_cents=-800, fees_cents=old_fee_cents(10, 50)),
        # edge guardado en pp (época vieja) → mismo bucket 5-8
        _t(coid="pp", estimated_edge_pct=6.0, pnl_cents=100, fees_cents=old_fee_cents(10, 50)),
    ]
    buckets = {b.label: b for b in motor2_buckets(rows)}
    assert buckets["5-8%"].n == 2 and buckets[">=11%"].n == 1
    drag = kalshi_fee_cents(10, 50) - old_fee_cents(10, 50)
    assert buckets["5-8%"].pnl_real_cents == 600 - 2 * drag
    assert buckets["5-8%"].win_pct == 100.0


def test_motor2_postfix_rows_have_no_drag():
    rows = [
        _t(
            coid="p",
            estimated_edge_pct=0.06,
            pnl_cents=300,
            fees_cents=kalshi_fee_cents(10, 50),
            placed_at=POST,
        ),
    ]
    b = {x.label: x for x in motor2_buckets(rows)}["5-8%"]
    assert b.pnl_real_cents == b.pnl_recorded_cents == 300
