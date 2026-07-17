"""
Mutua exclusión por EVENTO en el detector de Motor 2 (fix de la doble exposición correlacionada).

Motor 2 es DIRECCIONAL: una sola apuesta por partido. El bug de prod (−$218, PHI vs NYM) fue
apostar `yes@...-PHI` + `no@...-NYM`: dos market_tickers DISTINTOS, ambos = "PHI gana". Un dedup
por market_ticker no lo agarra; hay que colapsar a nivel EVENTO. Estos tests fijan:
  (a) yes@equipoA + no@equipoB del mismo partido → UNA sola señal (la de mayor edge);
  (b) la exposición por evento queda en un solo trade (size <= cap 5%);
  (c) partidos distintos siguen generando señales independientes;
  (d) one_per_event=False → restaura el comportamiento previo (una señal por outcome/lado).
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

from loguru import logger

from src.clients.odds_api import Bookmaker, Market, OddsEvent, Outcome
from src.strategies.motor_2_consensus.detector import (
    KalshiEventQuotes,
    KalshiQuote,
    _collapse_event_signals,
    find_signals,
)
from src.strategies.motor_2_consensus.matcher import start_time_et

# El matching exige coherencia fecha(event_key) ↔ commence_time (ET); se derivan juntos.
_KEY_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_COMMENCE = datetime.now(UTC) + timedelta(hours=2)


def _key_datestamp(dt) -> str:
    et = start_time_et(dt)
    return f"{et.year % 100:02d}{_KEY_MONTHS[et.month - 1]}{et.day:02d}"


_PHINYM_EVENT_KEY = f"KXMLBGAME-{_key_datestamp(_COMMENCE)}PHINYM"

CAPITAL = 300.0
CAP_USD = CAPITAL * 0.05  # 5% = $15.00


@contextlib.contextmanager
def _capture_logs():
    captured: list[str] = []
    sink = logger.add(lambda m: captured.append(str(m)), level="INFO")
    try:
        yield captured
    finally:
        logger.remove(sink)


def _phi_nym_event(*, phi_price: float = 1.72, nym_price: float = 2.38) -> OddsEvent:
    """OddsEvent PHI vs NYM (h2h). Default → fair PHI ≈ 0.58."""
    market = Market(
        key="h2h",
        outcomes=(
            Outcome(name="Philadelphia Phillies", price=phi_price),
            Outcome(name="New York Mets", price=nym_price),
        ),
    )
    return OddsEvent(
        id="phinym",
        sport_key="baseball_mlb",
        commence_time=_COMMENCE,
        home_team="Philadelphia Phillies",
        away_team="New York Mets",
        bookmakers=(Bookmaker(key="pinnacle", title="Pinnacle", markets=(market,)),),
    )


def _phi_nym_kalshi(*, phi_yes: int, phi_no: int, nym_yes: int, nym_no: int) -> KalshiEventQuotes:
    """Las DOS patas del partido, cada una en su market_ticker (...-PHI y ...-NYM)."""
    return KalshiEventQuotes(
        event_key=_PHINYM_EVENT_KEY,
        outcomes=(
            KalshiQuote(
                _PHINYM_EVENT_KEY + "-PHI",
                "Philadelphia Phillies",
                yes_ask_cents=phi_yes,
                no_ask_cents=phi_no,
            ),
            KalshiQuote(
                _PHINYM_EVENT_KEY + "-NYM",
                "New York Mets",
                yes_ask_cents=nym_yes,
                no_ask_cents=nym_no,
            ),
        ),
    )


# =====================================================
# (a) yes@PHI + no@NYM (misma dirección, tickers distintos) → UNA señal
# =====================================================


def test_same_event_correlated_legs_emit_single_signal():
    """fair PHI≈0.58. yes@-PHI (ask48) y no@-NYM (ask47) ambos 'PHI gana' con edge → UNA señal."""
    odds = _phi_nym_event()
    # yes@-PHI: edge sobre fair 0.58. no@-NYM: edge sobre 1-0.42=0.58. Los lados contrarios
    # (no@-PHI, yes@-NYM) con ask alto → sin edge.
    ke = _phi_nym_kalshi(phi_yes=48, phi_no=90, nym_yes=90, nym_no=47)

    with _capture_logs() as cap:
        signals = find_signals([ke], [odds], capital_usd=CAPITAL)

    assert len(signals) == 1  # NO dos patas correlacionadas
    assert any("motor2.signal.event_collapsed" in m for m in cap)


# =====================================================
# (b) exposición por evento acotada a un solo trade (cap 5%)
# =====================================================


def test_event_exposure_capped_to_single_trade():
    """La única señal emitida por evento respeta el cap de tamaño por trade (5%)."""
    odds = _phi_nym_event()
    ke = _phi_nym_kalshi(phi_yes=48, phi_no=90, nym_yes=90, nym_no=47)
    signals = find_signals([ke], [odds], capital_usd=CAPITAL)
    assert len(signals) == 1
    assert signals[0].recommended_size_usd <= CAP_USD


# =====================================================
# (c) partidos distintos → señales independientes
# =====================================================


def test_distinct_events_independent_signals():
    """Dos partidos distintos con edge → una señal por cada uno (no se colapsan entre sí)."""
    odds_phi = _phi_nym_event()
    odds_lal = OddsEvent(
        id="lalbos",
        sport_key="basketball_nba",
        commence_time=_COMMENCE,
        home_team="Los Angeles Lakers",
        away_team="Boston Celtics",
        bookmakers=(
            Bookmaker(
                key="pinnacle",
                title="Pinnacle",
                markets=(
                    Market(
                        key="h2h",
                        outcomes=(
                            Outcome(name="Los Angeles Lakers", price=1.72),
                            Outcome(name="Boston Celtics", price=2.38),
                        ),
                    ),
                ),
            ),
        ),
    )
    ke_phi = _phi_nym_kalshi(phi_yes=48, phi_no=90, nym_yes=90, nym_no=47)
    ke_lal = KalshiEventQuotes(
        event_key="KXNBA-LAL-BOS",
        outcomes=(
            KalshiQuote("KXNBA-LAL", "Los Angeles Lakers", yes_ask_cents=48, no_ask_cents=90),
            KalshiQuote("KXNBA-BOS", "Boston Celtics", yes_ask_cents=90, no_ask_cents=90),
        ),
    )
    signals = find_signals([ke_phi, ke_lal], [odds_phi, odds_lal], capital_usd=CAPITAL)
    tickers = {s.market_ticker for s in signals}
    assert len(signals) == 2  # un trade por partido
    assert any(t.startswith("KXMLBGAME") for t in tickers)
    assert any(t.startswith("KXNBA") for t in tickers)


# =====================================================
# (d) escape hatch: one_per_event=False restaura el comportamiento previo
# =====================================================


def test_one_per_event_disabled_keeps_all_legs():
    """Con one_per_event=False, el evento vuelve a emitir todas las patas con edge."""
    odds = _phi_nym_event()
    ke = _phi_nym_kalshi(phi_yes=48, phi_no=90, nym_yes=90, nym_no=47)
    signals = find_signals([ke], [odds], capital_usd=CAPITAL, one_per_event=False)
    assert len(signals) == 2  # yes@-PHI y no@-NYM, ambas


# =====================================================
# Unidad: _collapse_event_signals
# =====================================================


def test_collapse_keeps_highest_edge():
    """_collapse_event_signals deja solo la señal de mayor edge del evento + loguea."""
    from src.strategies.motor_2_consensus.detector import ConsensusSignal

    a = ConsensusSignal(
        market_ticker="EVT-A",
        kalshi_side="YES",
        odds_api_fair_prob=0.58,
        kalshi_price_cents=48,
        edge_pct=0.09,
        recommended_size_usd=10.0,
    )
    b = ConsensusSignal(
        market_ticker="EVT-B",
        kalshi_side="NO",
        odds_api_fair_prob=0.58,
        kalshi_price_cents=47,
        edge_pct=0.11,
        recommended_size_usd=10.0,
    )
    with _capture_logs() as cap:
        out = _collapse_event_signals([a, b], one_per_event=True, event_key="EVT")
    assert len(out) == 1
    assert out[0] is b  # mayor edge
    assert any("motor2.signal.event_collapsed" in m and "EVT" in m for m in cap)
