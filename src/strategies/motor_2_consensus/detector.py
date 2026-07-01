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
from datetime import UTC, datetime

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
# Edge máximo PLAUSIBLE (fracción): una ventaja de consenso real sobre un binario líquido
# rara vez supera ~10-15pp. Un "edge" mayor es casi seguro un ARTEFACTO de datos (mercado
# resuelto/in-play, quotes stale, set de odds degenerado) → se descarta y loguea, NO se
# graba ni se apuesta. Backstop contra spreads fantasma (ej. GER vs CUW, ~50pp in-play).
MAX_PLAUSIBLE_EDGE = 0.15


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
    # Evento Kalshi al que pertenece el market (ej. "KXMLBGAME-26JUN27NYMPHI").
    # Lo estampa find_signals; el executor lo usa para el dedup cross-ciclo por evento
    # ("" en señales construidas a mano → el dedup cae al scope por ticker).
    event_key: str = ""
    kalshi_side: str  # "YES" | "NO"
    odds_api_fair_prob: float  # probabilidad JUSTA (post-no-vig) del lado señalado
    kalshi_price_cents: int  # ask de Kalshi del lado señalado
    edge_pct: float  # edge NETO post-comisión (fracción; 0.03 = 3pp)
    recommended_size_usd: float  # ¼ Kelly con cap 5%
    # Desglose AUDITABLE del edge (¢ por contrato): neto = bruto − fee. Permite ver en la
    # EdgeWindow grabada de dónde sale el edge y cuánto se comió la comisión.
    gross_edge_cents: int = 0  # fair_prob*100 − ask (PRE-comisión)
    fee_cents: int = 0  # comisión Kalshi de 1 contrato a ese precio


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
    book_keys: set[str] = set()
    for bk in odds_event.bookmakers:
        for mk in bk.markets:
            if mk.key != "h2h":
                continue
            book_keys.add(bk.key)
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
    # n_books = cuántas casas formaron el consenso. POCAS casas → fair ruidoso/sesgado: es el
    # input que infla el sizing en mercados de edge sobreestimado (correlacionar después con el
    # PnL). MLB en The Odds API suele traer menos casas que fútbol → más riesgo de sobre-edge.
    logger.info(
        f"motor2.consensus event={getattr(odds_event, 'id', '?')} n_books={len(book_keys)} "
        f"outcomes={len(names)} fair={ {n: round(p, 4) for n, p in zip(names, fair, strict=True)} }"
    )
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


def _size_usd(
    true_prob: float, ask_cents: int, capital_usd: float, *, max_stake_pct: float = 0.0
) -> float:
    """Stake recomendado en USD para UNA señal.

    max_stake_pct > 0 → FLAT: fracción FIJA del bankroll por trade (capital_usd * pct/100),
    DESACOPLADA del edge. Kelly escala el stake con (true_prob − ask); si el `true_prob` está
    sobreestimado (consenso ruidoso, MLB con pocas casas), Kelly sobre-apuesta justo donde el edge
    es falso → la asimetría que sangró −19% ROI (sim sobre 141 settled: flat constante +22.9%).
    El flat corta ese acoplamiento. El count entero lo deriva el executor (size·100/price) y el
    RiskManager lo re-capea aguas abajo (capital efectivo + MAX_TRADE_SIZE_USD).

    max_stake_pct == 0 → fallback ¼ Kelly con cap 5% (comportamiento previo)."""
    if max_stake_pct > 0:
        return round(capital_usd * max_stake_pct / 100.0, 2)
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
    now: datetime | None = None,
    diag: dict[str, float] | None = None,
    one_per_event: bool = True,
    max_stake_pct: float = 0.0,
) -> list[ConsensusSignal]:
    """
    Emite ConsensusSignal por cada outcome con edge neto > min_edge. Puro y testeable.

    GUARDARRAÍL PRE-MATCH: solo evalúa partidos que AÚN NO arrancaron (commence_time > now).
    Una vez que rueda la pelota, los precios de Kalshi reaccionan al juego en vivo mientras
    la odds API puede seguir reportando líneas pre-partido/in-play degeneradas → spread
    FANTASMA (ej. GER vs CUW resuelto: edges de ~50pp imposibles). El edge de consenso solo
    es válido antes del kickoff.

    DIAGNÓSTICO (opcional `diag`): puebla el embudo del ciclo para distinguir "mercado
    eficiente" de "el matcher rechaza en silencio" cuando señales=0. Por cada evento de
    Kalshi que NO matchea, clasifica POR QUÉ según su evento de odds pre-match más cercano
    (mayor solapamiento de nombres canónicos):
      - reject_absent: el partido no está en el feed de odds (overlap 0) — cobertura/timing.
      - reject_cardinality: mismo partido pero distinto nº de outcomes (2-way vs 3-way).
      - reject_names: mismo partido, set de nombres difiere → FIXABLE con alias (loguea ambos).
      - reject_no_fair: matcheó pero no hubo consenso usable.
    """
    now = now or datetime.now(UTC)
    if diag is not None:
        diag.update(
            odds_total=float(len(odds_events)),
            odds_started_skip=float(sum(1 for oe in odds_events if oe.commence_time <= now)),
            kalshi_total=float(len(kalshi_events)),
            events_matched=0.0,
            reject_absent=0.0,
            reject_cardinality=0.0,
            reject_names=0.0,
            reject_no_fair=0.0,
            best_net_edge=-1.0,  # fracción; -1 = no se evaluó ningún outcome
        )
    signals: list[ConsensusSignal] = []
    name_debug_budget = 3  # cap de logs name_debug por ciclo (evita spam)
    for ke in kalshi_events:
        k_names = [q.outcome_name for q in ke.outcomes]
        k_canon = {canonical_name(n) for n in k_names}
        matched = False
        best_overlap = -1
        best_reason = "absent"
        best_pair: tuple[set[str], set[str]] | None = None
        for oe in odds_events:
            # PRE-MATCH ONLY: partido ya iniciado → comparación inválida, se saltea.
            if oe.commence_time <= now:
                continue
            odds_names = _h2h_outcome_names(oe)
            if not odds_names:
                continue
            o_canon = {canonical_name(n) for n in odds_names}
            # Rastrear el oe pre-match MÁS CERCANO (mayor overlap) para diagnosticar el rechazo.
            overlap = len(k_canon & o_canon)
            if overlap > best_overlap:
                best_overlap = overlap
                best_pair = (k_canon, o_canon)
                if overlap == 0:
                    best_reason = "absent"
                elif len(k_names) != len(odds_names):
                    best_reason = "cardinality"
                elif k_canon != o_canon:
                    best_reason = "names"
                else:
                    best_reason = "no_fair"  # set igual → si no matchea/no fair, es esto
            # Regla del matcher: cardinalidad + conjuntos exactos. None → no es el partido.
            if match_outcomes(k_names, odds_names) is None:
                continue
            fair = _consensus_fair_probs(oe)
            if not fair:
                best_reason = "no_fair"
                continue
            matched = True
            if diag is not None:
                diag["events_matched"] += 1.0
            # Candidatos de TODOS los outcomes del partido (cada uno en su market_ticker), luego
            # colapsados a UNA apuesta direccional por evento (_collapse_event_signals).
            event_signals: list[ConsensusSignal] = []
            for q in ke.outcomes:
                cn = canonical_name(q.outcome_name)
                fp = fair.get(cn)
                if fp is None:
                    continue
                event_signals.extend(
                    _signals_for_outcome(
                        q, fp, capital_usd, min_edge, diag, max_stake_pct=max_stake_pct
                    )
                )
            for es in event_signals:
                es.event_key = ke.event_key  # para el dedup cross-ciclo del executor
            signals.extend(_collapse_event_signals(event_signals, one_per_event, ke.event_key))
            break  # ya emparejado este evento Kalshi
        if not matched and diag is not None:
            diag["reject_" + best_reason] = diag.get("reject_" + best_reason, 0.0) + 1.0
            # name_debug SOLO para el caso fixable (mismo partido, nombres distintos): muestra
            # los dos sets canónicos → se ve EXACTO qué alias falta agregar a TEAM_ALIASES.
            if best_reason in ("names", "cardinality") and best_pair and name_debug_budget > 0:
                name_debug_budget -= 1
                k_set, o_set = best_pair
                # El DIFF de sets muestra EXACTO qué nombre sobra de cada lado → el alias a
                # agregar es kalshi_solo[i] → odds_solo[i] (ej. 'turkiye' → 'turkey').
                logger.info(
                    f"motor2.name_debug reason={best_reason} "
                    f"kalshi_solo={sorted(k_set - o_set)} odds_solo={sorted(o_set - k_set)} "
                    f"(kalshi={sorted(k_set)} odds={sorted(o_set)})"
                )
    return signals


def _emit_if_plausible(
    out: list[ConsensusSignal],
    q: KalshiQuote,
    side: str,
    fair_prob: float,
    ask_cents: int,
    edge: float,
    capital_usd: float,
    min_edge: float,
    max_stake_pct: float = 0.0,
) -> None:
    """Emite la señal si min_edge < edge ≤ MAX_PLAUSIBLE_EDGE; un edge monstruoso se descarta."""
    if edge <= min_edge:
        return
    if edge > MAX_PLAUSIBLE_EDGE:
        # Backstop: edge implausible = artefacto (mercado resuelto/stale) → NO graba ni apuesta.
        logger.warning(
            f"motor2.signal.discarded_suspicious ticker={q.market_ticker} side={side} "
            f"edge={edge:.3f} (> {MAX_PLAUSIBLE_EDGE} → artefacto probable: mercado stale/in-play)"
        )
        return
    out.append(
        _build(q, side, fair_prob, ask_cents, edge, capital_usd, max_stake_pct=max_stake_pct)
    )


def _signals_for_outcome(
    q: KalshiQuote,
    fair_prob: float,
    capital_usd: float,
    min_edge: float,
    diag: dict[str, float] | None = None,
    *,
    max_stake_pct: float = 0.0,
) -> list[ConsensusSignal]:
    """Candidatos YES (prob justa) y NO (complemento) de UN outcome con edge neto > umbral.

    Genera SOLO candidatos; la mutua exclusión la aplica find_signals a nivel EVENTO
    (_collapse_event_signals) sobre el conjunto del partido — no acá, porque las patas
    correlacionadas viven en market_tickers DISTINTOS (ej. yes@-PHI y no@-NYM son la misma
    dirección 'PHI gana') y este helper solo ve un market a la vez."""
    out: list[ConsensusSignal] = []
    # YES: comprar a yes_ask si el mercado lo subvalúa frente al fair.
    yes_edge = _net_edge_pct(fair_prob, q.yes_ask_cents)
    # NO: comprar a no_ask con la prob complementaria.
    no_edge = _net_edge_pct(1.0 - fair_prob, q.no_ask_cents)
    if diag is not None:
        # Mejor edge NETO visto (aunque no supere el umbral) → distingue "mercado eficiente"
        # (best_edge ~1-2pp) de "filtro angosto" (sin outcomes evaluados / best_edge alto pero 0 señales).
        diag["best_net_edge"] = max(diag.get("best_net_edge", -1.0), yes_edge, no_edge)
    _emit_if_plausible(
        out, q, "YES", fair_prob, q.yes_ask_cents, yes_edge, capital_usd, min_edge, max_stake_pct
    )
    _emit_if_plausible(
        out, q, "NO", 1.0 - fair_prob, q.no_ask_cents, no_edge, capital_usd, min_edge, max_stake_pct
    )
    return out


def _collapse_event_signals(
    event_signals: list[ConsensusSignal], one_per_event: bool, event_key: str
) -> list[ConsensusSignal]:
    """Mutua exclusión POR EVENTO (no por market). Un partido tiene outcomes mutuamente
    excluyentes en market_tickers DISTINTOS (ej. ...-PHI y ...-NYM). Apostar yes en el market de
    un equipo y no en el del otro = MISMA dirección → doble exposición correlacionada al mismo
    resultado (el caso que sangró −$218: PHI con yes@-PHI + no@-NYM, ambos 'PHI gana'). Un dedup
    por market_ticker NO lo agarra; por eso se colapsa a nivel EVENTO.

    Motor 2 es DIRECCIONAL → UNA sola apuesta por evento: se queda con la de MAYOR edge neto y
    descarta el resto. Esto además acota la EXPOSICIÓN por partido a un solo trade (ya capeado al
    5% por _size_usd), en vez de sumar stake sobre varias patas del mismo evento."""
    if not one_per_event or len(event_signals) <= 1:
        return event_signals
    best = max(event_signals, key=lambda s: s.edge_pct)
    dropped = [
        f"{s.market_ticker}/{s.kalshi_side}@{s.edge_pct:.3f}"
        for s in event_signals
        if s is not best
    ]
    logger.warning(
        f"motor2.signal.event_collapsed event={event_key} "
        f"kept={best.market_ticker}/{best.kalshi_side}@{best.edge_pct:.3f} dropped={dropped} "
        "(1 apuesta direccional por evento → exposición del partido capeada a un trade)"
    )
    return [best]


def _build(
    q: KalshiQuote,
    side: str,
    fair_prob: float,
    ask_cents: int,
    edge: float,
    capital_usd: float,
    *,
    max_stake_pct: float = 0.0,
) -> ConsensusSignal:
    fee_cents = kalshi_fee_cents(1, ask_cents)
    gross_edge_cents = int(round(fair_prob * 100.0 - ask_cents))
    sig = ConsensusSignal(
        market_ticker=q.market_ticker,
        kalshi_side=side,
        odds_api_fair_prob=round(fair_prob, 4),
        kalshi_price_cents=ask_cents,
        edge_pct=edge,
        recommended_size_usd=_size_usd(
            fair_prob, ask_cents, capital_usd, max_stake_pct=max_stake_pct
        ),
        gross_edge_cents=gross_edge_cents,
        fee_cents=fee_cents,
    )
    logger.info(
        f"motor2.signal ticker={sig.market_ticker} side={sig.kalshi_side} "
        f"fair_prob={sig.odds_api_fair_prob} kalshi={sig.kalshi_price_cents}c "
        f"gross={gross_edge_cents}c fee={fee_cents}c edge={sig.edge_pct:.3f} "
        f"sizing={'flat' if max_stake_pct > 0 else 'kelly'} "
        f"recommended_size_usd=${sig.recommended_size_usd}"
    )
    return sig
