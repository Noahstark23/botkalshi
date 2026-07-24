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


# =====================================================
# Pares M1 (corrección de lectura 2026-07-07: por PAR, no por pata)
# =====================================================


def _leg(coid: str, side: str, price: int, *, arb: str | None = None, **kw) -> Trade:
    notes = f"arb_id={arb}" if arb else None
    return _t(
        coid,
        strategy="motor_1_arbitrage",
        side=side,
        price_cents=price,
        fill_price_cents=price,
        notes=notes,
        **kw,
    )


ARB_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def test_pares_motor1_empareja_por_arb_id():
    """yes@18 + no@75 x10 con arb_id compartido → 1 par; net = 70 − fee(10,18) − fee(10,75)."""
    from src.analytics.rentabilidad import pares_motor1

    rows = [
        _leg("y", "yes", 18, arb=ARB_A, count=10, pnl_cents=-180),
        _leg("n", "no", 75, arb=ARB_A, count=10, pnl_cents=250),
    ]
    out = pares_motor1(rows)
    assert len(out.pares) == 1 and out.sin_par == 0
    par = out.pares[0]
    assert par.via == "arb_id" and par.paired == 10
    esperado = (100 - 18 - 75) * 10 - kalshi_fee_cents(10, 18) - kalshi_fee_cents(10, 75)
    assert par.net_cents == esperado
    assert out.perdedores == 0  # net positivo → no es perdedor determinístico


def test_pares_motor1_legacy_por_ventana_temporal():
    """Sin arb_id (filas pre 2026-07-02): mismo ticker + placed_at a <10s → par."""
    from src.analytics.rentabilidad import pares_motor1

    t0 = datetime(2026, 6, 30, 12, 0, 0)
    rows = [
        _leg("y", "yes", 40, count=5, placed_at=t0),
        _leg("n", "no", 45, count=5, placed_at=t0.replace(second=3)),
    ]
    out = pares_motor1(rows)
    assert len(out.pares) == 1 and out.pares[0].via == "ventana"
    assert out.pares[0].net_cents == 15 * 5 - kalshi_fee_cents(5, 40) - kalshi_fee_cents(5, 45)


def test_pares_motor1_huerfana_queda_aparte_con_su_pnl():
    """Pata con arb_id SIN gemela = huérfana: no forma par y su pnl se reporta como
    costo operacional (la separación estrategia vs operación que motivó la vista)."""
    from src.analytics.rentabilidad import pares_motor1

    rows = [_leg("y", "yes", 18, arb=ARB_A, count=21, pnl_cents=-378)]
    out = pares_motor1(rows)
    assert out.pares == []
    assert out.sin_par == 1 and out.sin_par_pnl_cents == -378


def test_pares_motor1_ignora_otras_estrategias_y_ventana_lejana():
    """CONTROL: filas de M2 no entran; dos patas del mismo ticker a >10s NO se emparejan."""
    from src.analytics.rentabilidad import pares_motor1

    t0 = datetime(2026, 6, 30, 12, 0, 0)
    rows = [
        _t("m2", strategy="motor_2_consensus", side="yes"),
        _leg("y", "yes", 40, count=5, placed_at=t0),
        _leg("n", "no", 45, count=5, placed_at=t0.replace(minute=5)),  # 5 min después
    ]
    out = pares_motor1(rows)
    assert out.pares == []
    assert out.sin_par == 2  # las dos patas M1 sin emparejar


def test_pares_motor1_perdedor_deterministico():
    """Par cuyo gross no cubre las fees reales → perdedor determinístico (la firma
    del fee bug pre 2026-07-01 que cuantifica loss_audit)."""
    from src.analytics.rentabilidad import pares_motor1

    # gross = (100−50−49)·2 = 2c; fees = fee(2,50)+fee(2,49) = 1+1 = 2c → net 0 ≤ 0
    rows = [
        _leg("y", "yes", 50, arb=ARB_A, count=2),
        _leg("n", "no", 49, arb=ARB_A, count=2),
    ]
    out = pares_motor1(rows)
    assert len(out.pares) == 1
    assert out.pares[0].net_cents <= 0
    assert out.perdedores == 1
