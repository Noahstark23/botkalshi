"""
Tests de la matemática del Trigger del Motor REST.

Mock del canal `ticker` basado en el shape de Gate 0 (yes_bid_dollars/yes_ask_dollars
fixed-point strings + sizes). Casos:
  (a) cruce rentable post-comisión → trigger válido
  (b) cruce bruto que NO sobrevive la comisión → NO trigger
  (c) cruce rentable pero profundidad < umbral → NO trigger
  (d) sin cruce → NO trigger
Extra: size ausente → fallo seguro (NO trigger).
"""
from __future__ import annotations

from src.strategies.motor_rest_arb.trigger import evaluate_ticker


def make_ticker(
    *,
    ticker: str = "KXWC-26-ARG",
    yes_bid: str,
    yes_ask: str,
    yes_bid_size: float = 100,
    yes_ask_size: float = 100,
    include_sizes: bool = True,
) -> dict:
    """Mensaje `ticker` mock con shape de prod (dollars + *_size_fp strings)."""
    msg: dict = {
        "type": "ticker",
        "msg": {
            "market_ticker": ticker,
            "yes_bid_dollars": yes_bid,
            "yes_ask_dollars": yes_ask,
        },
    }
    if include_sizes:
        msg["msg"]["yes_bid_size_fp"] = str(yes_bid_size)
        msg["msg"]["yes_ask_size_fp"] = str(yes_ask_size)
    return msg


def test_a_profitable_cross_after_fees_triggers():
    """yes_ask=40, no_ask=100-yes_bid=42 → 40+42=82 < 100, edge neto sobra la comisión."""
    # yes_bid=0.58 → no_ask = 100-58 = 42; yes_ask=0.40 → 40+42=82, 18c bruto.
    msg = make_ticker(yes_bid="0.5800", yes_ask="0.4000")
    sig = evaluate_ticker(msg, min_edge_cents=1, min_depth=2)
    assert sig is not None
    assert sig.market_ticker == "KXWC-26-ARG"
    assert sig.net_edge_cents >= 1
    assert sig.gross_spread_cents > sig.net_edge_cents  # el fee come algo del bruto
    assert sig.limiting_depth == 100


def test_b_gross_cross_eaten_by_fees_no_trigger():
    """Cruce bruto que la comisión consume exactamente → detect_binary_arb devuelve None.

    yes_bid=0.50 → no_ask = 100-50 = 50; yes_ask=0.49 → 49+50=99, 1c bruto/contrato.
    Con size=2: gross = 1*2 = 2c, fees(2 patas) = 2c → net=0 → no rentable post-comisión.
    Pasa el filtro de profundidad (size 2 >= min_depth 2); la MATEMÁTICA lo rechaza.
    """
    msg = make_ticker(yes_bid="0.5000", yes_ask="0.4900", yes_bid_size=2, yes_ask_size=2)
    sig = evaluate_ticker(msg, min_edge_cents=1, min_depth=2)
    assert sig is None


def test_c_profitable_but_insufficient_depth_no_trigger():
    """Cruce rentable pero size de la pata limitante < umbral → NO trigger."""
    msg = make_ticker(yes_bid="0.5800", yes_ask="0.4000", yes_bid_size=1, yes_ask_size=1)
    sig = evaluate_ticker(msg, min_edge_cents=1, min_depth=2)
    assert sig is None


def test_d_no_cross_no_trigger():
    """yes_ask + no_ask >= 100 → sin arbitraje."""
    # yes_bid=0.45 → no_ask=55; yes_ask=0.50 → 50+55=105 >= 100.
    msg = make_ticker(yes_bid="0.4500", yes_ask="0.5000")
    sig = evaluate_ticker(msg, min_edge_cents=1, min_depth=2)
    assert sig is None


def test_e_missing_size_fails_safe_no_trigger():
    """Size ausente del payload → profundidad insuficiente (fallo seguro), NO trigger."""
    msg = make_ticker(yes_bid="0.5800", yes_ask="0.4000", include_sizes=False)
    sig = evaluate_ticker(msg, min_edge_cents=1, min_depth=2)
    assert sig is None


def test_f_fractional_size_string_parsed_as_float():
    """Size fraccionario ('8.73') se castea a float; floor a int para ejecución."""
    # yes_bid=0.58 → no_ask=42; yes_ask=0.40 → arb rentable. sizes 8.73 ≥ min_depth 2.
    msg = make_ticker(yes_bid="0.5800", yes_ask="0.4000", yes_bid_size=8.73, yes_ask_size=8.73)
    sig = evaluate_ticker(msg, min_edge_cents=1, min_depth=2)
    assert sig is not None
    assert sig.limiting_depth == 8  # floor(8.73)


def test_g_invalid_size_string_fails_safe_no_trigger():
    """Size con casteo inválido → fallo seguro a 0.0 → no alcanza min_depth → NO trigger."""
    msg = make_ticker(yes_bid="0.5800", yes_ask="0.4000")
    msg["msg"]["yes_bid_size_fp"] = "not-a-number"
    msg["msg"]["yes_ask_size_fp"] = "not-a-number"
    sig = evaluate_ticker(msg, min_edge_cents=1, min_depth=2)
    assert sig is None


def test_h_fractional_below_one_fails_min_depth():
    """Size fraccionario < min_depth (0.5 < 2) → NO trigger (no se redondea hacia arriba)."""
    msg = make_ticker(yes_bid="0.5800", yes_ask="0.4000", yes_bid_size=0.5, yes_ask_size=0.5)
    sig = evaluate_ticker(msg, min_edge_cents=1, min_depth=2)
    assert sig is None
