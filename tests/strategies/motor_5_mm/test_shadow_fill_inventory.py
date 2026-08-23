"""Shadow fills (regla de cruce ESTRICTO) + inventario simulado con fees reales."""

from __future__ import annotations

from decimal import Decimal

from src.math.fees import kalshi_fee_cents
from src.strategies.motor_5_mm.inventory import InventoryBook
from src.strategies.motor_5_mm.quoter import QuoteSet
from src.strategies.motor_5_mm.shadow_fill import ShadowFill, fills_for_quote

QUOTE = QuoteSet(ticker="T", fair_prob=0.50, bid_cents=47, ask_cents=53, size=10)


# =====================================================
# Regla de cruce estricto (conservadora)
# =====================================================


def test_no_fill_when_book_inside_our_quotes():
    assert fills_for_quote(QUOTE, best_yes_bid=48, best_yes_ask=52) == []


def test_touch_does_not_fill():
    """El book TOCA nuestros precios (ask==47, bid==53) → NO llena (prioridad de cola
    incognoscible; conservador = perder fills reales, jamás inventarlos)."""
    assert fills_for_quote(QUOTE, best_yes_bid=53, best_yes_ask=47) == []


def test_strict_cross_fills_bid():
    fills = fills_for_quote(QUOTE, best_yes_bid=40, best_yes_ask=46)
    assert len(fills) == 1
    f = fills[0]
    assert f.side == "buy" and f.price_cents == 47 and f.count == 10


def test_f1_v2_no_inventa_un_contrato_desde_depth_fraccional():
    fills = fills_for_quote(
        QUOTE,
        best_yes_bid=40,
        best_yes_ask=46,
        best_yes_ask_depth=Decimal("0.33"),
        observable_count_cap=1,
    )
    assert fills == []


def test_f1_v2_afirma_un_contrato_si_depth_cruzada_lo_cubre():
    fills = fills_for_quote(
        QUOTE,
        best_yes_bid=40,
        best_yes_ask=46,
        best_yes_ask_depth=Decimal("1.00"),
        observable_count_cap=1,
    )
    assert len(fills) == 1
    assert fills[0].count == 1
    assert fills[0].observed_depth == Decimal("1.00")


def test_strict_cross_fills_ask():
    fills = fills_for_quote(QUOTE, best_yes_bid=54, best_yes_ask=60)
    assert len(fills) == 1
    f = fills[0]
    assert f.side == "sell" and f.price_cents == 53 and f.count == 10


def test_one_sided_quote_only_fills_live_side():
    ask_only = QuoteSet(ticker="T", fair_prob=0.5, bid_cents=None, ask_cents=53, size=10)
    # El ask del book cruza "donde estaría el bid" → sin bid vivo, no hay fill de compra.
    assert fills_for_quote(ask_only, best_yes_bid=40, best_yes_ask=41) == []
    fills = fills_for_quote(ask_only, best_yes_bid=54, best_yes_ask=60)
    assert len(fills) == 1 and fills[0].side == "sell"


def test_missing_book_side_never_fills():
    assert fills_for_quote(QUOTE, best_yes_bid=None, best_yes_ask=None) == []


# =====================================================
# Inventario + PnL neto de fees
# =====================================================


def test_round_trip_captures_spread_net_of_real_fees():
    """Compra 10@47 y vende 10@53 → bruto 60¢; fees con count REAL (no lineal)."""
    inv = InventoryBook()
    inv.apply_fill(ShadowFill("T", "buy", 47, 10, rule="r"))
    state = inv.apply_fill(ShadowFill("T", "sell", 53, 10, rule="r"))
    assert state.net_contracts == 0
    expected_fees = kalshi_fee_cents(10, 47) + kalshi_fee_cents(10, 53)
    assert state.fees_cents == expected_fees
    assert inv.total_mtm_cents({"T": 50.0}) == 60 - expected_fees


def test_mtm_of_open_long_uses_mark():
    inv = InventoryBook()
    inv.apply_fill(ShadowFill("T", "buy", 47, 10, rule="r"))
    fee = kalshi_fee_cents(10, 47)
    # comprado a 47, mark 50 → +3¢/contrato − fee
    assert inv.total_mtm_cents({"T": 50.0}) == 10 * (50 - 47) - fee


def test_mtm_of_open_short_uses_mark():
    inv = InventoryBook()
    inv.apply_fill(ShadowFill("T", "sell", 53, 10, rule="r"))
    fee = kalshi_fee_cents(10, 53)
    # vendido a 53, mark 50 → +3¢/contrato − fee
    assert inv.total_mtm_cents({"T": 50.0}) == 10 * (53 - 50) - fee


def test_missing_mark_falls_back_to_neutral_50():
    """Sin mark, un corto NO se marca a 0 (eso sobreestimaría PnL): prior neutral 50¢."""
    inv = InventoryBook()
    inv.apply_fill(ShadowFill("T", "sell", 53, 10, rule="r"))
    assert inv.total_mtm_cents({}) == inv.total_mtm_cents({"T": 50.0})


def test_net_and_total_abs():
    inv = InventoryBook()
    inv.apply_fill(ShadowFill("A", "buy", 40, 10, rule="r"))
    inv.apply_fill(ShadowFill("B", "sell", 60, 5, rule="r"))
    assert inv.net("A") == 10 and inv.net("B") == -5 and inv.net("C") == 0
    assert inv.total_abs_contracts() == 15
