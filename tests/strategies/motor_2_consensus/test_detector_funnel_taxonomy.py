"""
Taxonomía del embudo — higiene post-deploy (medición en vivo 2026-07-02).

La hipótesis "faltan aliases" NO se cumplió: los nombres ya llegan canónicos y aun así
rej_names subió 14→21. name_debug reveló dos causas de RUIDO en el diagnóstico:

Causa A — el clasificador de "candidato más cercano" comparaba contra odds events de
OTRA fecha ET: en una rotación MLB, Kalshi [chicago cubs, st louis cardinals] (mañana)
vs odds [atlanta braves, st louis cardinals] (hoy) comparten un equipo → overlap 1 →
etiquetado 'names' ("falta un alias") cuando la verdad es 'absent' ("el partido no está
en el feed"). El diagnóstico solo debe considerar candidatos del MISMO día ET del key.

Causa B — eventos Kalshi multi-outcome (ganador de grupo/torneo, >3 outcomes) nunca
pueden matchear un feed h2h (2-way / 1X2): inflaban rej_cardinality/rej_names cada
ciclo. Se filtran PRE-funnel con contador propio (skip_multi_outcome).

Ninguna de las dos causaba apuestas malas (match_outcomes exige igualdad exacta de
conjuntos); el costo era observabilidad: el embudo señalaba "poblar aliases" donde no
había nada que poblar.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.clients.odds_api import Bookmaker, Market, OddsEvent, Outcome
from src.strategies.motor_2_consensus.detector import (
    KalshiEventQuotes,
    KalshiQuote,
    find_signals,
)
from src.strategies.motor_2_consensus.matcher import ET, start_time_et

_KEY_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")

NOW = datetime(2026, 6, 27, 12, 0, tzinfo=ET).astimezone(UTC)


def _stamp(dt: datetime) -> str:
    et = start_time_et(dt)
    return f"{et.year % 100:02d}{_KEY_MONTHS[et.month - 1]}{et.day:02d}"


def _odds(home: str, away: str, commence: datetime, *, oid: str) -> OddsEvent:
    return OddsEvent(
        id=oid,
        sport_key="baseball_mlb",
        commence_time=commence,
        home_team=home,
        away_team=away,
        bookmakers=(
            Bookmaker(
                key="pinnacle",
                title="P",
                markets=(
                    Market(
                        key="h2h",
                        outcomes=(
                            Outcome(name=home, price=1.60),
                            Outcome(name=away, price=2.60),
                        ),
                    ),
                ),
            ),
        ),
    )


def _kalshi(event_key: str, names: tuple[str, ...]) -> KalshiEventQuotes:
    return KalshiEventQuotes(
        event_key=event_key,
        outcomes=tuple(KalshiQuote(f"{event_key}-{i}", n, 50, 55) for i, n in enumerate(names)),
    )


def _find(kalshi, odds, diag):
    return find_signals(kalshi, odds, min_edge=0.03, now=NOW, diag=diag)


# =====================================================
# Causa A — coherencia de FECHA en el diagnóstico
# =====================================================


def test_shared_team_other_date_is_absent_not_names():
    """El caso de campo: Kalshi Cubs@Cardinals (mañana) no está en el feed; el feed trae
    Braves@Cardinals (HOY, comparte a los Cardinals) + otro partido mañana. El candidato
    de otra fecha NO debe etiquetar el rechazo como 'names' → es 'absent'."""
    tomorrow = NOW + timedelta(hours=30)
    braves_cards_today = _odds(
        "St Louis Cardinals", "Atlanta Braves", NOW + timedelta(hours=6), oid="today"
    )
    other_tomorrow = _odds("Seattle Mariners", "Houston Astros", tomorrow, oid="tmrw")
    ke = _kalshi(f"KXMLBGAME-{_stamp(tomorrow)}CHCSTL", ("Chicago Cubs", "St Louis Cardinals"))
    diag: dict[str, float] = {}
    assert _find([ke], [braves_cards_today, other_tomorrow], diag) == []
    assert diag["reject_absent"] == 1.0
    assert diag["reject_names"] == 0.0 and diag["reject_cardinality"] == 0.0
    assert diag["skip_out_of_horizon"] == 0.0  # la fecha SÍ está cubierta por el feed


def test_same_date_missing_alias_still_reject_names():
    """Control positivo: mismo día ET, un nombre que NO canoniza → sigue siendo 'names'
    (el caso genuinamente fixable con alias no se pierde con el gate de fecha)."""
    game = NOW + timedelta(hours=6)
    odds = _odds("Philadelphia Phillies", "New York Metropolitans", game, oid="g")
    ke = _kalshi(f"KXMLBGAME-{_stamp(game)}PHINYM", ("Philadelphia Phillies", "New York Mets"))
    diag: dict[str, float] = {}
    assert _find([ke], [odds], diag) == []
    assert diag["reject_names"] == 1.0 and diag["reject_absent"] == 0.0


def test_unparseable_key_keeps_legacy_diag():
    """Key sin datestamp → no hay fecha con la que exigir coherencia: el diagnóstico
    considera todos los candidatos pre-match (comportamiento previo)."""
    braves_cards = _odds(
        "St Louis Cardinals", "Atlanta Braves", NOW + timedelta(hours=6), oid="today"
    )
    ke = _kalshi("KXMLBGAME-CHCSTL", ("Chicago Cubs", "St Louis Cardinals"))
    diag: dict[str, float] = {}
    assert _find([ke], [braves_cards], diag) == []
    assert diag["reject_names"] == 1.0


# =====================================================
# Causa B — multi-outcome fuera del funnel (pre-filtro)
# =====================================================


def test_multi_outcome_event_is_skipped_pre_funnel():
    """Ganador de grupo (6 outcomes) vs feed h2h: jamás puede matchear (igualdad exacta
    de conjuntos) → skip_multi_outcome, SIN inflar rej_cardinality/rej_names."""
    game = NOW + timedelta(hours=6)
    soccer = OddsEvent(
        id="s",
        sport_key="soccer_fifa_world_cup",
        commence_time=game,
        home_team="Argentina",
        away_team="Belgium",
        bookmakers=(
            Bookmaker(
                key="pinnacle",
                title="P",
                markets=(
                    Market(
                        key="h2h",
                        outcomes=(
                            Outcome(name="Argentina", price=1.5),
                            Outcome(name="Belgium", price=4.0),
                            Outcome(name="Draw", price=4.5),
                        ),
                    ),
                ),
            ),
        ),
    )
    ke = _kalshi(
        "KXWCGROUPWIN-26-K",
        ("Argentina", "Belgium", "Colombia", "Ecuador", "Uruguay", "Draw"),
    )
    diag: dict[str, float] = {}
    assert _find([ke], [soccer], diag) == []
    assert diag["skip_multi_outcome"] == 1.0
    assert diag["reject_cardinality"] == 0.0 and diag["reject_names"] == 0.0


def test_three_outcome_1x2_still_flows():
    """El 1X2 (3 outcomes: A/B/Draw) es el mercado soccer normal → NO se filtra."""
    game = NOW + timedelta(hours=6)
    soccer = OddsEvent(
        id="s",
        sport_key="soccer_fifa_world_cup",
        commence_time=game,
        home_team="Argentina",
        away_team="Belgium",
        bookmakers=(
            Bookmaker(
                key="pinnacle",
                title="P",
                markets=(
                    Market(
                        key="h2h",
                        outcomes=(
                            Outcome(name="Argentina", price=1.5),
                            Outcome(name="Belgium", price=4.0),
                            Outcome(name="Draw", price=4.5),
                        ),
                    ),
                ),
            ),
        ),
    )
    ke = _kalshi(f"KXWCGAME-{_stamp(game)}ARGBEL", ("Argentina", "Belgium", "Draw"))
    diag: dict[str, float] = {}
    sigs = _find([ke], [soccer], diag)
    assert diag["skip_multi_outcome"] == 0.0
    assert diag["events_matched"] == 1.0
    assert len(sigs) >= 1
