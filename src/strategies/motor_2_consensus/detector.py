"""
Detector del Motor 2 — Kalshi vs consenso de sportsbooks.

Pipeline (analítico, SIN ejecución de capital):
  1. Cruce: empareja cada evento de Kalshi con uno de The Odds API vía `match_outcomes`
     (cardinalidad + exactitud por conjuntos; ambiguo → descarta).
  2. Fair price: promedia las probabilidades implícitas de los bookmakers (consenso) y
     les quita el vig con `remove_vig_multiplicative` → probabilidad JUSTA por outcome.
  3. Edge neto: spread entre el fair price y el ask de Kalshi, DESCONTANDO la comisión
     oficial de Kalshi (kalshi_fee_cents). Señal SOLO si edge_neto > 3pp.
  4. Sizing: ¼ Kelly con cap duro del 5% del capital activo (quarter_kelly_size).
  5. Output: ConsensusSignal (Pydantic) logueado.

DESACOPLAMIENTO DE LA FUENTE (decisión, flag para review):
  El brief mencionaba validar contra "eventos activos en OrderbookManagerV2". El
  detector NO se acopla a V2: (a) V2 está dormant y la regla es mantenerlo intacto;
  (b) un detector que recibe los quotes como input es unit-testeable. La fuente real
  de los quotes (MarketSnapshot / get_market — NO V2) se cablea en un paso aparte.
  Acá `find_signals` recibe los KalshiEventQuotes ya extraídos.

NO cablea ejecución: emite señales, no coloca órdenes. TRADING_ENABLED=false.
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger
from pydantic import BaseModel

from src.clients.odds_api import OddsEvent
from src.math.fees import kalshi_fee_cents
from src.math.kelly import quarter_kelly_size
from src.math.no_vig import implied_prob, remove_vig_multiplicative
from src.strategies.motor_2_consensus.matcher import canonical_name, match_outcomes

MIN_EDGE_PCT = 0.03  # 3pp neto post-comisión
SIZING_MAX_PCT = 5.0  # cap duro de exposición por trade (% del capital activo)
DEFAULT_CAPITAL_USD = 300.0  # capital activo actual (mock/config local)


# =====================================================
# Entrada: quotes de Kalshi (extraídos aguas arriba, no por V2)
# =====================================================


@dataclass(frozen=True, slots=True)
class KalshiQuote:
    """Un outcome de Kalshi con sus asks (lo necesario para medir edge YES/NO)."""

    market_ticker: str
    outcome_name: str  # "Argentina", "Draw", ...
    yes_ask_cents: int  # costo de comprar YES (apostar a que ocurre)
    no_ask_cents: int  # costo de comprar NO (apostar a que NO ocurre)


@dataclass(frozen=True, slots=True)
class KalshiEventQuotes:
    """Un evento de Kalshi = el conjunto de sus outcomes (para el match de cardinalidad)."""

    event_key: str
    outcomes: tuple[KalshiQuote, ...]


# =====================================================
# Salida: la señal
# =====================================================


class ConsensusSignal(BaseModel):
    """Señal de edge Motor 2 (no ejecuta — registro analítico)."""

    market_ticker: str
    kalshi_side: str  # "YES" | "NO"
    odds_api_fair_prob: float  # probabilidad JUSTA (post-no-vig) del lado señalado
    kalshi_price_cents: int  # ask de Kalshi del lado señalado
    edge_pct: float  # edge NETO post-comisión (fracción; 0.03 = 3pp)
    recommended_size_usd: float  # ¼ Kelly con cap 5%


# =====================================================
# Núcleo analítico (puro, testeable)
# =====================================================


def _h2h_outcome_names(odds_event: OddsEvent) -> list[str]:
    """Nombres de outcome del primer bookmaker con mercado h2h (cardinalidad + nombres)."""
    for bk in odds_event.bookmakers:
        for mk in bk.markets:
            if mk.key == "h2h" and mk.outcomes:
                return [o.name for o in mk.outcomes]
    return []


def _consensus_fair_probs(odds_event: OddsEvent) -> dict[str, float]:
    """
    Probabilidad JUSTA por outcome canónico: promedia la prob implícita de cada
    bookmaker (consenso) y le quita el vig (multiplicativo). {} si no hay h2h usable.
    """
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for bk in odds_event.bookmakers:
        for mk in bk.markets:
            if mk.key != "h2h":
                continue
            for o in mk.outcomes:
                cn = canonical_name(o.name)
                try:
                    p = implied_prob(o.price)
                except ValueError:
                    continue  # cuota inválida → ignorar ese outcome
                sums[cn] = sums.get(cn, 0.0) + p
                counts[cn] = counts.get(cn, 0) + 1
    if len(sums) < 2:
        return {}
    names = list(sums)
    avg_implied = [sums[n] / counts[n] for n in names]
    fair = remove_vig_multiplicative(avg_implied)
    return dict(zip(names, fair, strict=True))


def _net_edge_pct(fair_prob: float, ask_cents: int) -> float:
    """
    Edge NETO (fracción) de comprar un lado a `ask_cents` con prob justa `fair_prob`.

    EV por contrato (¢) = fair_prob*100 − ask − comisión; /100 → fracción del payout $1.
    """
    if not (1 <= ask_cents <= 99):
        return -1.0  # ask fuera de rango → sin edge
    fee = kalshi_fee_cents(1, ask_cents)
    net_ev_cents = fair_prob * 100.0 - ask_cents - fee
    return net_ev_cents / 100.0


def _size_usd(true_prob: float, ask_cents: int, capital_usd: float) -> float:
    """¼ Kelly con cap 5% del capital → USD recomendados. El cap lo aplica quarter_kelly_size."""
    bankroll_cents = int(capital_usd * 100)
    count = quarter_kelly_size(
        true_prob, ask_cents, bankroll_cents, kelly_fraction=0.25, max_pct=SIZING_MAX_PCT
    )
    return round(count * ask_cents / 100.0, 2)


def find_signals(
    kalshi_events: list[KalshiEventQuotes],
    odds_events: list[OddsEvent],
    *,
    capital_usd: float = DEFAULT_CAPITAL_USD,
    min_edge: float = MIN_EDGE_PCT,
) -> list[ConsensusSignal]:
    """
    Emite ConsensusSignal por cada outcome con edge neto > min_edge. Puro y testeable.
    """
    signals: list[ConsensusSignal] = []
    for ke in kalshi_events:
        k_names = [q.outcome_name for q in ke.outcomes]
        for oe in odds_events:
            odds_names = _h2h_outcome_names(oe)
            if not odds_names:
                continue
            # Regla del matcher: cardinalidad + conjuntos exactos. None → no es el partido.
            if match_outcomes(k_names, odds_names) is None:
                continue
            fair = _consensus_fair_probs(oe)
            if not fair:
                continue
            for q in ke.outcomes:
                cn = canonical_name(q.outcome_name)
                fp = fair.get(cn)
                if fp is None:
                    continue
                signals.extend(_signals_for_outcome(q, fp, capital_usd, min_edge))
            break  # ya emparejado este evento Kalshi
    return signals


def _signals_for_outcome(
    q: KalshiQuote, fair_prob: float, capital_usd: float, min_edge: float
) -> list[ConsensusSignal]:
    """Evalúa YES (prob justa) y NO (complemento) para un outcome; emite el/los que superen el edge."""
    out: list[ConsensusSignal] = []
    # YES: comprar a yes_ask si el mercado lo subvalúa frente al fair.
    yes_edge = _net_edge_pct(fair_prob, q.yes_ask_cents)
    if yes_edge > min_edge:
        out.append(_build(q, "YES", fair_prob, q.yes_ask_cents, yes_edge, capital_usd))
    # NO: comprar a no_ask con la prob complementaria.
    no_edge = _net_edge_pct(1.0 - fair_prob, q.no_ask_cents)
    if no_edge > min_edge:
        out.append(_build(q, "NO", 1.0 - fair_prob, q.no_ask_cents, no_edge, capital_usd))
    return out


def _build(
    q: KalshiQuote, side: str, fair_prob: float, ask_cents: int, edge: float, capital_usd: float
) -> ConsensusSignal:
    sig = ConsensusSignal(
        market_ticker=q.market_ticker,
        kalshi_side=side,
        odds_api_fair_prob=round(fair_prob, 4),
        kalshi_price_cents=ask_cents,
        edge_pct=edge,
        recommended_size_usd=_size_usd(fair_prob, ask_cents, capital_usd),
    )
    logger.info(
        f"motor2.signal ticker={sig.market_ticker} side={sig.kalshi_side} "
        f"fair_prob={sig.odds_api_fair_prob} kalshi={sig.kalshi_price_cents}c "
        f"edge={sig.edge_pct:.3f} size=${sig.recommended_size_usd}"
    )
    return sig
