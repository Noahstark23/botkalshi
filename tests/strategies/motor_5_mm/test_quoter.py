"""Quoter Motor 5 (F1 shadow) — reglas del plan §alcance 2, pineadas una a una."""

from __future__ import annotations

from src.math.fees import kalshi_fee_cents
from src.strategies.motor_5_mm.quoter import compute_quote


def _q(fair: float, **kw):
    defaults = {
        "half_spread_cents": 3,
        "size_contracts": 10,
        "inventory_contracts": 0,
        "max_inventory_contracts": 50,
        "best_yes_bid": None,
        "best_yes_ask": None,
    }
    defaults.update(kw)
    return compute_quote("T", fair, **defaults)


def test_symmetric_quote_around_fair_without_inventory():
    quote, reason = _q(0.50)
    assert reason is None and quote is not None
    assert quote.bid_cents == 47 and quote.ask_cents == 53
    assert quote.size == 10


def test_rounding_always_widens_never_tightens():
    """fair=0.505 → centro 50.5: bid floor(47.5)=47, ask ceil(53.5)=54 — el redondeo
    ensancha; jamás cotiza un spread más fino que el configurado."""
    quote, _ = _q(0.505)
    assert quote is not None
    assert quote.ask_cents - quote.bid_cents >= 2 * 3
    assert quote.bid_cents == 47 and quote.ask_cents == 54


def test_long_inventory_skews_quotes_down():
    """Largos 25/50 → skew = −1.5¢: ambas quotes bajan (vender más fácil, comprar menos)."""
    neutral, _ = _q(0.50)
    skewed, _ = _q(0.50, inventory_contracts=25)
    assert skewed.bid_cents < neutral.bid_cents
    assert skewed.ask_cents < neutral.ask_cents


def test_short_inventory_skews_quotes_up():
    neutral, _ = _q(0.50)
    skewed, _ = _q(0.50, inventory_contracts=-25)
    assert skewed.bid_cents > neutral.bid_cents
    assert skewed.ask_cents > neutral.ask_cents


def test_post_only_emulation_never_crosses_book():
    """Book 48/49 (spread fino): nuestro bid se recorta a ask−1=48… pero eso lo haría
    degenerado contra nuestro ask → según fees puede no cotizar. Caso claro: fair muy
    por encima del book → el bid crudo cruzaría el ask del book → se recorta."""
    quote, reason = _q(0.70, best_yes_bid=48, best_yes_ask=49)
    # bid crudo sería 67 > ask del book 49 → post-only lo recorta a 48; ask crudo 73 ≥ bid+1.
    if quote is not None:
        assert quote.bid_cents <= 48
        assert quote.ask_cents >= 49
    else:
        assert reason in ("degenerate", "unprofitable")


def test_inventory_cap_long_quotes_only_ask():
    quote, reason = _q(0.50, inventory_contracts=50)
    assert reason is None and quote is not None
    assert quote.bid_cents is None and quote.ask_cents is not None


def test_inventory_cap_short_quotes_only_bid():
    quote, reason = _q(0.50, inventory_contracts=-50)
    assert reason is None and quote is not None
    assert quote.ask_cents is None and quote.bid_cents is not None


def test_unprofitable_spread_is_skipped():
    """half_spread=1 alrededor de 50¢: captura 2¢, fees 2+2=4¢ → no se cotiza. La regla 5
    usa kalshi_fee_cents exacta (a 50¢ la fee de 1 contrato es 2¢)."""
    assert kalshi_fee_cents(1, 50) == 2
    quote, reason = _q(0.50, half_spread_cents=1)
    assert quote is None and reason == "unprofitable"


def test_profitable_at_extreme_prices_with_small_spread():
    """A precios extremos la fee cae a 1¢ → un spread de 3¢ captura neto. fair=0.08:
    bid 5 / ask 11, fees 1+1=2 < 6."""
    quote, reason = _q(0.08)
    assert reason is None and quote is not None
    assert kalshi_fee_cents(1, quote.bid_cents) == 1


def test_fair_out_of_range_skips():
    assert _q(0.0) == (None, "fair_out_of_range")
    assert _q(1.0) == (None, "fair_out_of_range")


def test_clamp_keeps_prices_in_1_99():
    quote, reason = _q(0.02)
    if quote is not None:
        assert quote.bid_cents >= 1 and quote.ask_cents <= 99
    else:
        assert reason in ("degenerate", "unprofitable")


def test_degenerate_after_clamps_is_skipped():
    """fair pegado a 0 + skew largo al tope → centro negativo → bid y ask colapsan en el
    clamp inferior (1==1) → degenerada, no cotiza."""
    quote, reason = _q(0.01, inventory_contracts=50)
    assert quote is None and reason == "degenerate"
