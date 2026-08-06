"""
Deudas de matching/fuentes del Motor 2 (auditoría 2026-07-01).

(a) Filtro de status fail-closed: market SIN status ya no pasa como abierto.
(b) Gate serie↔deporte: onboardear NBA "sin tocar código" ya no cruza "Boston" (Celtics)
    contra el set canónico de los Red Sox vía los aliases de ciudad MLB.
(c) Athletics: variantes con ciudad (Oakland/Sacramento/Las Vegas) canonizan al equipo.
(d) FakeOddsSource acepta factory → el fixture regenera commence_time por fetch (no se
    apaga en silencio tras 24h de uptime).
(e) Consenso por bookmaker CONSISTENTE: una casa con set de outcomes distinto/incompleto
    se descarta entera (antes inflaba el fair de los outcomes presentes → edge fantasma).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.clients.odds_api import Bookmaker, Market, OddsEvent, Outcome
from src.strategies.motor_2_consensus.detector import _consensus_fair_probs
from src.strategies.motor_2_consensus.matcher import canonical_name, series_sport_compatible
from src.strategies.motor_2_consensus.sources import FakeOddsSource, _parse_event_quotes

# =====================================================
# (a) status fail-closed
# =====================================================


def _market(ticker: str, name: str, **extra) -> dict:
    return {
        "ticker": ticker,
        "yes_sub_title": name,
        "yes_ask": 40,
        "no_ask": 65,
        **extra,
    }


def test_market_without_status_is_skipped():
    """Sin campo status (shape-drift) → NO se confía: el market se descarta."""
    eq = _parse_event_quotes(
        "KXWCGAME-26JUN27JORARG",
        [
            _market("T-ARG", "Argentina", status="active"),
            _market("T-JOR", "Jordan", status="active"),
            _market("T-TIE", "Draw"),  # sin status → fuera
        ],
    )
    assert eq is not None
    assert {q.outcome_name for q in eq.outcomes} == {"Argentina", "Jordan"}


# =====================================================
# (b) gate serie↔deporte
# =====================================================


def test_series_sport_compatibility():
    assert series_sport_compatible("KXMLBGAME-26JUN27NYMPHI", "baseball_mlb")
    assert not series_sport_compatible("KXMLBGAME-26JUN27NYMPHI", "basketball_nba")
    assert series_sport_compatible("KXWCGAME-26JUN27JORARG", "soccer_fifa_world_cup")
    assert not series_sport_compatible("KXNBAGAME-26JUN27BOSLAL", "baseball_mlb")
    # Serie desconocida → FAIL-CLOSED (cambio 2026-08-06: antes devolvía True "compat" y
    # un onboarding incompleto — serie en MOTOR2_SERIES sin entrada en la tabla — producía
    # matches CRUZADOS entre deportes en silencio: la fábrica de edges fantasma. Ahora no
    # matchea y avisa en WARNING one-shot; el costo de olvidar la tabla es cero señales
    # ruidosas, no plata).
    assert not series_sport_compatible("KXNUEVASERIE-26JUN27AAABBB", "cricket_odi")
    # Y todas las series del default de MOTOR2_SERIES DEBEN estar mapeadas — si esto
    # falla, el default mismo caería en fail-closed (el "typo latente" que el forense
    # buscó y no existía: este test lo vuelve imposible de introducir).
    from src.strategies.motor_2_consensus.matcher import _SERIES_SPORT_PREFIXES
    from src.utils.config import Settings

    default_series = Settings.model_fields["MOTOR2_SERIES"].default.split(",")
    for serie in default_series:
        assert serie in _SERIES_SPORT_PREFIXES, f"{serie} sin deporte en el matcher"


def test_nba_kalshi_event_does_not_match_mlb_odds():
    """El cross-match catastrófico: Kalshi NBA 'Boston' canoniza a 'boston red sox' por
    el alias MLB — sin el gate de deporte, matchearía el odds event MLB Red Sox@Guardians."""
    from src.strategies.motor_2_consensus.detector import (
        KalshiEventQuotes,
        KalshiQuote,
        find_signals,
    )

    now = datetime(2026, 6, 27, 16, 0, tzinfo=UTC)
    mlb_odds = OddsEvent(
        id="mlb",
        sport_key="baseball_mlb",
        commence_time=now + timedelta(hours=2),
        home_team="Boston Red Sox",
        away_team="Cleveland Guardians",
        bookmakers=(
            Bookmaker(
                key="pinnacle",
                title="P",
                markets=(
                    Market(
                        key="h2h",
                        outcomes=(
                            Outcome(name="Boston Red Sox", price=1.60),
                            Outcome(name="Cleveland Guardians", price=2.60),
                        ),
                    ),
                ),
            ),
        ),
    )
    ke_nba = KalshiEventQuotes(
        event_key="KXNBAGAME-26JUN27BOSCLE",
        outcomes=(
            KalshiQuote("KXNBAGAME-26JUN27BOSCLE-BOS", "Boston", 50, 55),
            KalshiQuote("KXNBAGAME-26JUN27BOSCLE-CLE", "Cleveland", 55, 52),
        ),
    )
    assert find_signals([ke_nba], [mlb_odds], min_edge=0.01, now=now) == []


# =====================================================
# (c) Athletics
# =====================================================


@pytest.mark.parametrize(
    "raw", ["A's", "Athletics", "Oakland Athletics", "Sacramento Athletics", "Las Vegas Athletics"]
)
def test_athletics_variants_canonize_to_same_team(raw):
    assert canonical_name(raw) == "athletics"


# =====================================================
# (d) FakeOddsSource con factory
# =====================================================


@pytest.mark.asyncio
async def test_fake_source_factory_regenerates_events_per_fetch():
    calls = {"n": 0}

    def _factory():
        calls["n"] += 1
        return []

    src = FakeOddsSource(_factory)
    await src.fetch()
    await src.fetch()
    assert calls["n"] == 2  # fixture fresco por fetch (commence_time nunca queda stale)


@pytest.mark.asyncio
async def test_fake_source_still_accepts_static_list():
    src = FakeOddsSource([])
    assert await src.fetch() == []


# =====================================================
# (e) consenso por bookmaker consistente
# =====================================================


def _book(key: str, prices: dict[str, float]) -> Bookmaker:
    return Bookmaker(
        key=key,
        title=key,
        markets=(
            Market(
                key="h2h",
                outcomes=tuple(Outcome(name=n, price=p) for n, p in prices.items()),
            ),
        ),
    )


def test_partial_bookmaker_excluded_from_consensus():
    """Casa sin el Draw (set incompleto) → descartada ENTERA: el fair sale solo de las
    casas con el set de referencia completo (antes, el no-vig normalizaba sobre {A,B}
    → A/B inflados y el Draw de Kalshi salteado en silencio)."""
    complete = _book("pinnacle", {"A": 2.0, "B": 3.0, "Draw": 6.0})
    partial = _book("cheapbook", {"A": 1.10, "B": 1.10})  # sin Draw, cuotas absurdas
    ev = OddsEvent(
        id="x",
        sport_key="soccer_test",
        commence_time=datetime.now(UTC) + timedelta(hours=2),
        home_team="A",
        away_team="B",
        bookmakers=(complete, partial),
    )
    only_complete = OddsEvent(
        id="y",
        sport_key="soccer_test",
        commence_time=ev.commence_time,
        home_team="A",
        away_team="B",
        bookmakers=(complete,),
    )
    assert _consensus_fair_probs(ev) == _consensus_fair_probs(only_complete)


def test_all_bookmakers_inconsistent_returns_empty():
    """Si ninguna casa (fuera de la referencia rota) tiene set usable → {} (sin señal)."""
    broken = _book("pinnacle", {"A": 2.0, "B": 0.5, "Draw": 6.0})  # cuota inválida (<1)
    ev = OddsEvent(
        id="z",
        sport_key="soccer_test",
        commence_time=datetime.now(UTC) + timedelta(hours=2),
        home_team="A",
        away_team="B",
        bookmakers=(broken,),
    )
    assert _consensus_fair_probs(ev) == {}


# =====================================================
# Aliases MLB poblados (plan amarillo 2026-07-02)
# =====================================================
# Evidencia: rej_names=16/49 (33%) en el funnel vivo + probes del operador (cws, sf, sd,
# lad, laa, ny yankees, white sox, padres, oakland → MISSING). La estructura del matcher
# ya estaba mergeada; esto es data entry. Colisiones cross-deporte (Giants NFL, Cardinals
# NFL, Rangers NHL) las bloquea series_sport_compatible, no la tabla.

_MLB_VARIANTS = {
    "arizona diamondbacks": ["ARI", "AZ", "Diamondbacks", "D-backs"],
    "atlanta braves": ["ATL", "Braves"],
    "athletics": ["OAK", "ATH", "Oakland", "A's", "Athletics"],
    "baltimore orioles": ["BAL", "Orioles"],
    "boston red sox": ["BOS", "Red Sox"],
    "chicago cubs": ["CHC", "Cubs", "Chi Cubs"],
    "chicago white sox": ["CWS", "CHW", "White Sox", "Chi White Sox"],
    "cincinnati reds": ["CIN", "Reds"],
    "cleveland guardians": ["CLE", "Guardians"],
    "colorado rockies": ["COL", "Rockies"],
    "detroit tigers": ["DET", "Tigers"],
    "houston astros": ["HOU", "Astros"],
    "kansas city royals": ["KC", "KCR", "Royals"],
    "los angeles angels": ["LAA", "LA Angels", "Angels"],
    "los angeles dodgers": ["LAD", "LA Dodgers", "Dodgers"],
    "miami marlins": ["MIA", "Marlins"],
    "milwaukee brewers": ["MIL", "Brewers"],
    "minnesota twins": ["MIN", "Twins"],
    "new york mets": ["NYM", "NY Mets", "Mets"],
    "new york yankees": ["NYY", "NY Yankees", "Yankees"],
    "philadelphia phillies": ["PHI", "Phillies"],
    "pittsburgh pirates": ["PIT", "Pirates"],
    "san diego padres": ["SD", "SDP", "Padres"],
    "san francisco giants": ["SF", "SFG", "Giants"],
    "seattle mariners": ["SEA", "Mariners"],
    "st louis cardinals": ["STL", "Cardinals", "St. Louis Cardinals"],
    "tampa bay rays": ["TB", "TBR", "Rays"],
    "texas rangers": ["TEX", "Rangers"],
    "toronto blue jays": ["TOR", "Blue Jays", "Jays"],
    "washington nationals": ["WSH", "WAS", "Nationals"],
}


@pytest.mark.parametrize(
    ("variant", "canonical"),
    [(v, canon) for canon, variants in _MLB_VARIANTS.items() for v in variants],
)
def test_mlb_variants_canonize(variant, canonical):
    assert canonical_name(variant) == canonical


def test_full_names_still_canonize_to_themselves():
    for canon in _MLB_VARIANTS:
        assert canonical_name(canon.title()) == canon


def test_aliases_do_not_chain():
    """Invariante estructural: todo VALOR de la tabla es canónico (get(v, v) == v) — un
    alias que apunte a otro alias resolvería distinto según el lado que lo canonice."""
    from src.strategies.motor_2_consensus.matcher import TEAM_ALIASES

    for value in TEAM_ALIASES.values():
        assert TEAM_ALIASES.get(value, value) == value, value
