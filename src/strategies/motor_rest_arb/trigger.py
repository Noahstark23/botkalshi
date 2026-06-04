"""
Trigger del Motor REST — detección de arbitraje sobre el canal WS `ticker`.

Lógica pura de detección (no graba, no ejecuta, no toca red):
    parsear ticker (BBO YES, shape Gate 0) → derivar pata NO → detect_binary_arb()
    → si net_profit_cents >= umbral Y profundidad suficiente → emitir señal.

Reusa:
    - parse_price_to_cents (data_capture): fixed-point dollar strings → cents.
    - detect_binary_arb (math.arbitrage): comisión oficial + net_profit (NO se
      reimplementa el fee a mano).

Profundidad: si el size de la pata limitante no se puede leer del payload, se
trata como INSUFICIENTE (fallo seguro → no dispara), nunca como suficiente.

⚠️ [verificar contra captura real] Los nombres de los campos de size del ticker
NO fueron registrados con exactitud en Gate 0 (solo se confirmó "sizes asociados
al BBO"). El parser prueba varios nombres candidatos; al correr el PRIMER shadow
contra mercado real, lo PRIMERO a confirmar es que _parse_size_fp lee un size de
verdad (loguear un par de sizes parseados) — si devuelve None en silencio, el
filtro de profundidad es decorativo y la data del shadow no sirve.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.math.arbitrage import ArbOpportunity, detect_binary_arb
from src.strategies.data_capture import parse_price_to_cents, parse_size

# Nombres candidatos para el size de cada nivel del BBO en el payload `ticker`.
# Gate 0 no fijó el nombre exacto → se prueban variantes en orden. Si ninguno
# aparece, _parse_size_fp devuelve None → profundidad insuficiente (fallo seguro).
_YES_BID_SIZE_KEYS = ("yes_bid_size_fp", "yes_bid_size", "yes_bid_qty")
_YES_ASK_SIZE_KEYS = ("yes_ask_size_fp", "yes_ask_size", "yes_ask_qty")


@dataclass(frozen=True, slots=True)
class TriggerSignal:
    """Señal pura emitida por el trigger (no graba ni ejecuta)."""

    market_ticker: str
    opportunity: ArbOpportunity
    net_edge_cents: int      # net_profit_cents de la opp
    gross_spread_cents: int  # gross_profit_cents (pre-comisión)
    limiting_depth: int      # contratos en la pata limitante


def _parse_size_fp(data: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    """
    Lee el size de un nivel probando varios nombres candidatos.

    Devuelve None si ninguno aparece (→ el caller trata la profundidad como
    insuficiente: fallo seguro hacia 'no disparo').
    """
    for k in keys:
        if k in data:
            return parse_size(data[k])
    return None


def evaluate_ticker(
    raw_msg: dict[str, Any],
    *,
    min_edge_cents: int,
    min_depth: int,
) -> TriggerSignal | None:
    """
    Evalúa un mensaje `ticker` y devuelve una TriggerSignal si hay arbitraje
    rentable post-comisión con profundidad suficiente; None en caso contrario.

    Shape esperado (Gate 0): payload con yes_bid_dollars/yes_ask_dollars
    (fixed-point strings) + sizes. La pata NO se deriva (Kalshi binario).
    """
    data = raw_msg.get("msg", raw_msg)
    ticker = data.get("market_ticker")
    if not ticker:
        return None

    yes_bid = parse_price_to_cents(data.get("yes_bid_dollars"))
    yes_ask = parse_price_to_cents(data.get("yes_ask_dollars"))
    if yes_bid is None or yes_ask is None:
        return None
    if not (1 <= yes_bid <= 99 and 1 <= yes_ask <= 99):
        return None

    # Derivar la pata NO (complementaria). Para comprar el arb se toman los asks:
    #   yes_ask (comprar YES) y no_ask = 100 - yes_bid (comprar NO).
    no_ask = 100 - yes_bid
    if not (1 <= no_ask <= 99):
        return None

    # Profundidad: sizes de la pata limitante. Para el arb se ejecuta contra
    # el yes_ask (size del ask YES) y el no_ask = vender-YES-bid (size del bid YES).
    yes_ask_size = _parse_size_fp(data, _YES_ASK_SIZE_KEYS)
    yes_bid_size = _parse_size_fp(data, _YES_BID_SIZE_KEYS)
    # Fallo seguro: si no se puede leer algún size → profundidad insuficiente.
    if yes_ask_size is None or yes_bid_size is None:
        return None
    # La pata NO sintética se ejecuta contra el bid YES; su size disponible es el del bid.
    no_ask_size = yes_bid_size
    limiting_depth = min(yes_ask_size, no_ask_size)
    if limiting_depth < min_depth:
        return None

    # Matemática oficial (reuso): detect_binary_arb descuenta la comisión de ambas
    # patas y devuelve net_profit_cents. Resuelve "cruce bruto que no sobrevive
    # comisión" gratis (devuelve None si los fees consumen el spread).
    opp = detect_binary_arb(
        ticker,
        yes_ask_cents=yes_ask,
        yes_available_size=yes_ask_size,
        no_ask_cents=no_ask,
        no_available_size=no_ask_size,
    )
    if opp is None:
        return None
    if opp.net_profit_cents < min_edge_cents:
        return None

    return TriggerSignal(
        market_ticker=ticker,
        opportunity=opp,
        net_edge_cents=opp.net_profit_cents,
        gross_spread_cents=opp.gross_profit_cents,
        limiting_depth=limiting_depth,
    )
