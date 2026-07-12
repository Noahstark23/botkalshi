"""
Motor 6 — detector de LINE MOVES (puro, sin red/DB).

TESIS (nacida de los datos del funnel, 2026-07-12): los edges reales de M2 son
TRANSITORIOS — aparecen cuando las casas MUEVEN la línea (lineups, noticias) y Kalshi
tarda ciclos en seguirla, y mueren en minutos. M2 compara contra el consenso ESTÁTICO
(foto); este motor explota la DINÁMICA (el delta entre fotos): si el fair se movió
≥ move_min_pp entre dos ciclos y el ask de Kalshi NO acompañó, comprar la dirección
del movimiento ANTES de que Kalshi ajuste.

Distinto de M2 por diseño: M2 exige que la foto muestre 3pp de descuento (raro en
mercado eficiente); M6 exige que la película muestre un salto que Kalshi aún no
digirió (exactamente los 5.63pp/3.20pp de un-solo-ciclo que el funnel registró).

PURO: recibe quotes + fair actual + fair previo, devuelve señales. La memoria del
fair previo vive en shadow.Motor6LineMoveShadow; acá no hay estado ni I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.math.fees import kalshi_fee_cents
from src.strategies.motor_2_consensus.detector import KalshiQuote


@dataclass(frozen=True, slots=True)
class LineMoveSignal:
    """Un movimiento de línea con edge neto post-fee aún sin digerir por Kalshi."""

    market_ticker: str
    side: str  # "YES" (el fair subió) | "NO" (el fair bajó)
    move_pp: float  # movimiento del fair entre ciclos, con signo (pp)
    fair_prob: float  # prob justa NUEVA del lado a comprar
    ask_cents: int  # ask actual de Kalshi de ese lado
    net_edge_pp: float  # edge neto post-fee vs el ask actual (pp)
    fee_cents: int  # fee estimada (1 contrato) usada en el neto


def find_linemove_signals(
    quotes: dict[str, KalshiQuote],
    fair_now: dict[str, float],
    fair_prev: dict[str, float],
    *,
    move_min_pp: float,
    edge_min_pp: float,
    max_edge_pp: float,
) -> list[LineMoveSignal]:
    """Señales de line-move: fair movido ≥ move_min_pp Y Kalshi rezagado (neto en banda).

    Banda (edge_min_pp, max_edge_pp]: el piso filtra moves que Kalshi ya digirió (el ask
    acompañó → neto chico); el techo es el anti-fantasma heredado de M2/REST (un "neto"
    enorme = quote stale o mercado a medio resolver, NO un regalo — lección 2026-06-16).

    Solo tickers presentes en AMBAS fotos del fair (un outcome nuevo no tiene delta) y
    con quote actual de Kalshi. El pre-match está garantizado aguas arriba: fair_now
    sale de find_signals de M2, que solo procesa eventos que aún no arrancaron.
    """
    out: list[LineMoveSignal] = []
    for ticker, prob_now in fair_now.items():
        prob_prev = fair_prev.get(ticker)
        q = quotes.get(ticker)
        if prob_prev is None or q is None:
            continue
        move_pp = (prob_now - prob_prev) * 100.0
        if move_pp >= move_min_pp:
            side, prob, ask = "YES", prob_now, q.yes_ask_cents
        elif move_pp <= -move_min_pp:
            side, prob, ask = "NO", 1.0 - prob_now, q.no_ask_cents
        else:
            continue  # movimiento por debajo del umbral → ruido, no señal
        if not (1 <= ask <= 99):
            continue  # lado vacío/degenerado
        fee = kalshi_fee_cents(1, ask)
        net_pp = prob * 100.0 - ask - fee
        if net_pp < edge_min_pp:
            continue  # Kalshi ya digirió el move (o el edge no cubre fees)
        if net_pp > max_edge_pp:
            continue  # anti-fantasma: demasiado bueno = stale/in-play a medio resolver
        out.append(
            LineMoveSignal(
                market_ticker=ticker,
                side=side,
                move_pp=move_pp,
                fair_prob=prob,
                ask_cents=ask,
                net_edge_pp=net_pp,
                fee_cents=fee,
            )
        )
    return out
