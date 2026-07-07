"""Scoreboard de rentabilidad — helpers puros pineados con números a mano."""

from __future__ import annotations

from datetime import datetime

from src.analytics.rentabilidad import (
    buckets_por_precio,
    granularidad_fee,
    pnl_mensual,
    resumen_por_motor,
    veredicto,
)
from src.math.fees import kalshi_fee_cents
from src.storage.models import Trade


def _t(coid: str, **kw) -> Trade:
    base = {
        "client_order_id": coid,
        "ticker": "KXMLBGAME-26JUL07TEST",
        "side": "yes",
        "action": "buy",
        "count": 10,
        "price_cents": 50,
        "fill_price_cents": 50,
        "strategy": "motor_2_consensus",
        "status": "settled",
        "pnl_cents": 100,
        "placed_at": datetime(2026, 7, 5, 12, 0),
        "settled_at": datetime(2026, 7, 6, 3, 0),
    }
    base.update(kw)
    return Trade(**base)


def test_resumen_por_motor_expectancy_y_profit_factor():
    rows = [
        _t("w1", pnl_cents=300),
        _t("w2", pnl_cents=100),
        _t("l1", pnl_cents=-200),
        _t("m1", strategy="motor_1_arbitrage", pnl_cents=-50),
        _t("skip", status="filled", pnl_cents=None),  # no settled → fuera
    ]
    out = resumen_por_motor(rows)
    m2 = out["motor_2_consensus"]
    assert m2.n == 3 and m2.wins == 2
    assert m2.pnl_cents == 200
    assert m2.gross_win_cents == 400 and m2.gross_loss_cents == 200
    assert m2.profit_factor == 2.0
    assert m2.expectancy_cents == 200 / 3
    assert m2.win_pct == 100 * 2 / 3
    assert out["motor_1_arbitrage"].n == 1


def test_pnl_mensual_agrupa_por_settled_at():
    rows = [
        _t("a", settled_at=datetime(2026, 6, 30, 23, 0), pnl_cents=-100),
        _t("b", settled_at=datetime(2026, 7, 1, 1, 0), pnl_cents=50),
        _t("c", settled_at=datetime(2026, 7, 2, 1, 0), pnl_cents=25),
    ]
    out = pnl_mensual(rows)
    assert out["2026-06"]["motor_2_consensus"] == -100
    assert out["2026-07"]["motor_2_consensus"] == 75


def test_buckets_por_precio_underdog_separado():
    rows = [
        _t("u1", fill_price_cents=25, pnl_cents=-100),  # underdog <40c
        _t("f1", fill_price_cents=70, pnl_cents=60),  # 60-79c
        _t("f2", fill_price_cents=85, pnl_cents=10),  # >=80c
    ]
    out = {b.label: b for b in buckets_por_precio(rows)}
    assert out["<40c"].n == 1 and out["<40c"].pnl_cents == -100
    assert out["60-79c"].n == 1 and out["60-79c"].pnl_cents == 60
    assert out[">=80c"].n == 1


def test_granularidad_sobrecosto_del_ceil():
    """El caso que motiva la métrica: 1 contrato a 50c → fee teórico 1.75c pero Kalshi
    cobra ceil = 2c (+14%). A precios extremos es peor: 1@95c teórico 0.3325c → 1c
    (+200%). El sobrecosto del bucket chico tiene que reflejarlo."""
    rows = [_t("g1", count=1, fill_price_cents=50, pnl_cents=10)]
    out = {g.label: g for g in granularidad_fee(rows)}
    chico = out["<=5"]
    assert chico.n == 1
    assert chico.fee_real_cents == kalshi_fee_cents(1, 50) == 2
    assert chico.fee_teorico_x100 == 175  # 1.75c en x100
    assert chico.sobrecosto_redondeo_cents == 2 - 1.75


def test_granularidad_trade_grande_sin_sobrecosto_relevante():
    """CONTROL: 100 contratos a 50c → teórico 17.5c, real ceil 18c — sobrecosto ~0.5c,
    despreciable frente al bucket chico."""
    rows = [_t("g2", count=100, fill_price_cents=50, pnl_cents=10)]
    out = {g.label: g for g in granularidad_fee(rows)}
    grande = out[">100"] if out.get(">100") and out[">100"].n else out["21-100"]
    assert grande.n == 1
    assert 0 <= grande.sobrecosto_redondeo_cents < 1.0


def test_veredicto_ordena_por_pnl_y_menciona_granularidad():
    rows = [
        _t("v1", strategy="motor_1_arbitrage", pnl_cents=-500, count=2),
        _t("v2", strategy="motor_2_consensus", pnl_cents=300),
    ]
    lineas = veredicto(rows).lineas
    assert "motor_1_arbitrage" in lineas[0]  # el que más pierde primero
    assert any("granularidad" in ln or "redondeo" in ln for ln in lineas)


def test_veredicto_sin_datos():
    assert "Sin filas settled" in veredicto([]).lineas[0]
