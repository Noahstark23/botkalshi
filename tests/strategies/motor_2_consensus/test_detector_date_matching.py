"""
Matching por FECHA + "ambigüedad → descarta" (fix auditoría 2026-07-01).

El bug: el emparejamiento Kalshi↔odds era solo por conjunto de nombres y tomaba el
PRIMER odds event pre-match que coincidía. En una serie MLB (mismo par 3-4 días
seguidos) el evento Kalshi del miércoles matcheaba la línea del martes; en un
doubleheader, el evento Kalshi in-play del juego 1 matcheaba la línea pre-match del
juego 2 (precios en vivo vs línea de OTRO juego — el artefacto "GER vs CUW" por debajo
de MAX_PLAUSIBLE_EDGE).

Escenarios pineados: serie de 2 días → cada evento Kalshi matchea SU juego;
doubleheader con hora en el key → desambigua; sin hora → descarta ambos (ambiguous);
juego 1 in-play → started; fecha sin candidato → date; key sin fecha + 2 candidatos →
ambiguous.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from src.clients.odds_api import Bookmaker, Market, OddsEvent, Outcome
from src.strategies.motor_2_consensus.detector import (
    KalshiEventQuotes,
    KalshiQuote,
    find_signals,
)
from src.strategies.motor_2_consensus.matcher import ET, parse_event_key_start, start_time_et

_KEY_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")

# "Ahora": mediodía ET de un día fijo (evita flakiness en bordes de medianoche).
NOW = datetime(2026, 6, 27, 12, 0, tzinfo=ET).astimezone(UTC)


def _stamp(dt: datetime) -> str:
    et = start_time_et(dt)
    return f"{et.year % 100:02d}{_KEY_MONTHS[et.month - 1]}{et.day:02d}"


def _odds(commence: datetime, *, phi: float = 1.60, nym: float = 2.60, oid: str = "x") -> OddsEvent:
    """PHI vs NYM con fair PHI ≈ 0.62 (edge claro contra ask=50)."""
    return OddsEvent(
        id=oid,
        sport_key="baseball_mlb",
        commence_time=commence,
        home_team="Philadelphia Phillies",
        away_team="New York Mets",
        bookmakers=(
            Bookmaker(
                key="pinnacle",
                title="Pinnacle",
                markets=(
                    Market(
                        key="h2h",
                        outcomes=(
                            Outcome(name="Philadelphia Phillies", price=phi),
                            Outcome(name="New York Mets", price=nym),
                        ),
                    ),
                ),
            ),
        ),
    )


def _kalshi(event_key: str) -> KalshiEventQuotes:
    return KalshiEventQuotes(
        event_key=event_key,
        outcomes=(
            KalshiQuote(f"{event_key}-PHI", "Philadelphia Phillies", 50, 55),
            KalshiQuote(f"{event_key}-NYM", "New York Mets", 55, 52),
        ),
    )


def _find(kalshi, odds, diag=None):
    return find_signals(kalshi, odds, min_edge=0.03, now=NOW, diag=diag)


# =====================================================
# parse_event_key_start (unidad)
# =====================================================


def test_parse_key_date_only():
    assert parse_event_key_start("KXMLBGAME-26JUN27NYMPHI") == (date(2026, 6, 27), None)


def test_parse_key_date_and_time():
    assert parse_event_key_start("KXMLBGAME-26JUN271610PHINYM") == (date(2026, 6, 27), (16, 10))


def test_parse_key_without_datestamp_returns_none():
    assert parse_event_key_start("KXMLBGAME-PHINYM") is None
    assert parse_event_key_start("NBA-LAL-BOS") is None
    assert parse_event_key_start("SINGUION") is None


def test_parse_key_invalid_date_returns_none():
    assert parse_event_key_start("KXMLBGAME-26FEB30AAABBB") is None  # 30 de febrero


# =====================================================
# Serie MLB: mismo par, días distintos
# =====================================================


def test_series_each_kalshi_event_matches_its_own_game():
    """El bug original: el evento del miércoles apostaba contra la línea del martes."""
    game_today = NOW + timedelta(hours=6)  # hoy 6pm ET
    game_tomorrow = NOW + timedelta(hours=30)  # mañana
    odds = [_odds(game_today, oid="today"), _odds(game_tomorrow, phi=3.0, nym=1.36, oid="tmrw")]
    ke_today = _kalshi(f"KXMLBGAME-{_stamp(game_today)}PHINYM")
    ke_tomorrow = _kalshi(f"KXMLBGAME-{_stamp(game_tomorrow)}PHINYM")

    # Hoy: fair PHI≈0.62 vs ask 50 → señal YES PHI (matcheó la línea de HOY, no la de mañana
    # donde PHI es underdog con fair≈0.31 → habría dado señal NO/contraria).
    sig_today = _find([ke_today], odds)
    assert len(sig_today) == 1
    assert sig_today[0].market_ticker.endswith("-PHI") and sig_today[0].kalshi_side == "YES"

    # Mañana: fair PHI≈0.31 → la señal sale del juego de MAÑANA (NYM favorito).
    sig_tomorrow = _find([ke_tomorrow], odds)
    assert len(sig_tomorrow) == 1
    assert sig_tomorrow[0].event_key == ke_tomorrow.event_key
    assert not (
        sig_tomorrow[0].market_ticker.endswith("-PHI") and sig_tomorrow[0].kalshi_side == "YES"
    )


def test_key_date_without_feed_coverage_is_horizon_skip():
    """Solo existe la línea de MAÑANA → la fecha de HOY ni siquiera está en el feed:
    skip_out_of_horizon (pre-funnel, fix observabilidad 2026-07-02), NO reject_date —
    antes este caso inflaba reject_date y tapaba los mismatches reales."""
    game_tomorrow = NOW + timedelta(hours=30)
    ke_today = _kalshi(f"KXMLBGAME-{_stamp(NOW + timedelta(hours=6))}PHINYM")
    diag: dict[str, float] = {}
    assert _find([ke_today], [_odds(game_tomorrow)], diag) == []
    assert diag["skip_out_of_horizon"] == 1.0
    assert diag["reject_date"] == 0.0 and diag["events_matched"] == 0.0


def test_reject_date_when_feed_covers_day_but_matchup_is_another_date():
    """reject_date GENUINO: el feed SÍ cubre el día del key (otro partido), pero este
    matchup solo existe mañana → candidatos por nombres sin fecha correcta."""
    game_today_other = _odds(NOW + timedelta(hours=6), oid="other")
    # otro partido hoy (nombres distintos) para que la fecha esté cubierta:
    from src.clients.odds_api import Bookmaker, Market, Outcome

    game_today_other = OddsEvent(
        id="other",
        sport_key="baseball_mlb",
        commence_time=NOW + timedelta(hours=6),
        home_team="Atlanta Braves",
        away_team="Miami Marlins",
        bookmakers=(
            Bookmaker(
                key="pinnacle",
                title="P",
                markets=(
                    Market(
                        key="h2h",
                        outcomes=(
                            Outcome(name="Atlanta Braves", price=1.6),
                            Outcome(name="Miami Marlins", price=2.6),
                        ),
                    ),
                ),
            ),
        ),
    )
    phinym_tomorrow = _odds(NOW + timedelta(hours=30), oid="tmrw")
    ke_today = _kalshi(f"KXMLBGAME-{_stamp(NOW + timedelta(hours=6))}PHINYM")
    diag: dict[str, float] = {}
    assert _find([ke_today], [game_today_other, phinym_tomorrow], diag) == []
    assert diag["reject_date"] == 1.0 and diag["skip_out_of_horizon"] == 0.0


# =====================================================
# Doubleheader: mismo par, mismo día
# =====================================================


def test_doubleheader_disambiguated_by_key_time():
    """Dos juegos el mismo día; el key trae la hora ET → cada evento matchea el suyo."""
    game1 = datetime(2026, 6, 27, 13, 10, tzinfo=ET).astimezone(UTC)
    game2 = datetime(2026, 6, 27, 19, 5, tzinfo=ET).astimezone(UTC)
    odds = [_odds(game1, oid="g1"), _odds(game2, phi=3.0, nym=1.36, oid="g2")]
    ke_g2 = _kalshi(f"KXMLBGAME-{_stamp(game2)}1905PHINYM")

    sigs = _find([ke_g2], odds)
    # Matchea el juego 2 (fair PHI≈0.31, NYM favorito) — no el juego 1 (PHI favorito).
    assert len(sigs) == 1
    assert not (sigs[0].market_ticker.endswith("-PHI") and sigs[0].kalshi_side == "YES")


def test_doubleheader_without_key_time_rejects_ambiguous():
    """Dos juegos el mismo día y el key SIN hora → ambigüedad → descarta (conservador)."""
    game1 = datetime(2026, 6, 27, 13, 10, tzinfo=ET).astimezone(UTC)
    game2 = datetime(2026, 6, 27, 19, 5, tzinfo=ET).astimezone(UTC)
    ke = _kalshi(f"KXMLBGAME-{_stamp(game1)}PHINYM")
    diag: dict[str, float] = {}
    assert _find([ke], [_odds(game1, oid="g1"), _odds(game2, oid="g2")], diag) == []
    assert diag["reject_ambiguous"] == 1.0


def test_inplay_game1_does_not_match_game2_line():
    """El caso 'GER vs CUW': juego 1 arrancó (Kalshi in-play) — su key apunta al juego 1,
    que ya no es pre-match → reject_started. NO matchea la línea pre-match del juego 2."""
    game1 = NOW - timedelta(minutes=30)  # arrancó hace 30 min
    game2 = NOW + timedelta(hours=6)
    et1 = start_time_et(game1)
    ke_g1 = _kalshi(f"KXMLBGAME-{_stamp(game1)}{et1.hour:02d}{et1.minute:02d}PHINYM")
    diag: dict[str, float] = {}
    assert _find([ke_g1], [_odds(game1, oid="g1"), _odds(game2, oid="g2")], diag) == []
    assert diag["reject_started"] == 1.0 and diag["events_matched"] == 0.0


# =====================================================
# Key sin fecha parseable (fallback conservador)
# =====================================================


def test_unparseable_key_single_candidate_still_matches():
    """Compat: key legacy sin datestamp + UN solo candidato → matchea como antes."""
    game = NOW + timedelta(hours=2)
    sigs = _find([_kalshi("KXMLBGAME-PHINYM")], [_odds(game)])
    assert len(sigs) == 1


def test_unparseable_key_two_candidates_rejects_ambiguous():
    """Key sin datestamp + DOS candidatos del mismo par → imposible desambiguar → descarta."""
    odds = [_odds(NOW + timedelta(hours=2), oid="a"), _odds(NOW + timedelta(hours=26), oid="b")]
    diag: dict[str, float] = {}
    assert _find([_kalshi("KXMLBGAME-PHINYM")], odds, diag) == []
    assert diag["reject_ambiguous"] == 1.0
