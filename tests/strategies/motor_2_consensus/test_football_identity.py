"""Identidad NFL/NCAAF: nombres crudos de Demo, liga exacta y ticker independiente.

Fixtures sintéticas: ni la coincidencia ni el fair numérico certifican reglas de
liquidación. No se publica en FairValueBook ni se construye un ejecutor.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.clients.odds_api import Bookmaker, Market, OddsEvent, Outcome
from src.strategies.motor_2_consensus.detector import find_signals
from src.strategies.motor_2_consensus.matcher import canonical_name, series_sport_compatible
from src.strategies.motor_2_consensus.sources import _parse_event_quotes
from src.strategies.motor_2_consensus.team_identity import check_ticker_team

NOW = datetime(2026, 10, 10, 16, tzinfo=UTC)
CASES = [
    (
        "KXNFLGAME-26OCT11PHIJAC",
        "americanfootball_nfl",
        ("PHI", "JAC"),
        ("Philadelphia", "Jacksonville"),
        ("Philadelphia Eagles", "Jacksonville Jaguars"),
        datetime(2026, 10, 11, 13, 30, tzinfo=UTC),
    ),
    (
        "KXNCAAFGAME-26OCT10UGAALA",
        "americanfootball_ncaaf",
        ("UGA", "ALA"),
        ("Georgia", "Alabama"),
        ("Georgia Bulldogs", "Alabama Crimson Tide"),
        datetime(2026, 10, 10, 23, 30, tzinfo=UTC),
    ),
    (
        "KXNCAAFGAME-26OCT10STANND",
        "americanfootball_ncaaf",
        ("STAN", "ND"),
        ("Stanford", "Notre Dame"),
        ("Stanford Cardinal", "Notre Dame Fighting Irish"),
        datetime(2026, 10, 10, 19, 30, tzinfo=UTC),
    ),
    (
        "KXNCAAFGAME-26OCT09FSULOU",
        "americanfootball_ncaaf",
        ("FSU", "LOU"),
        ("Florida St.", "Louisville"),
        ("Florida State Seminoles", "Louisville Cardinals"),
        datetime(2026, 10, 9, 23, tzinfo=UTC),
    ),
]

# Capturas independientes del catálogo Demo y del snapshot del proveedor. Se
# comprueba el detector completo, incluyendo nombres cruzados y fechas/ligas malas.
NFL_CAPTURE = json.loads(
    (Path(__file__).parents[2] / "fixtures/nfl_demo_identity_20261012.json").read_text()
)["cases"]
CASES.extend(
    (
        row["event_ticker"],
        "americanfootball_nfl",
        tuple(row["codes"]),
        tuple(row["short_names"]),
        tuple(row["long_names"]),
        datetime.fromisoformat(row["commence_time"].replace("Z", "+00:00")),
    )
    for row in NFL_CAPTURE
)


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("failure", [None, "swapped", "wrong_league", "wrong_date"])
def test_raw_source_detector_identity(case, failure):
    key, sport, codes, short_names, long_names, kickoff = case
    names = short_names[::-1] if failure == "swapped" else short_names
    raw_markets = [
        {
            "ticker": f"{key}-{code}",
            "yes_sub_title": name,
            "status": "active",
            "yes_ask_dollars": "0.4400",
            "no_ask_dollars": "0.5800",
        }
        for code, name in zip(codes, names, strict=True)
    ]
    quotes = _parse_event_quotes(key, raw_markets)
    assert quotes is not None
    now = min(NOW, kickoff - timedelta(hours=3))
    odds = OddsEvent(
        id="synthetic-identity-only",
        sport_key=("americanfootball_nfl" if sport.endswith("ncaaf") else "americanfootball_ncaaf")
        if failure == "wrong_league"
        else sport,
        commence_time=kickoff + timedelta(days=1) if failure == "wrong_date" else kickoff,
        home_team=long_names[0],
        away_team=long_names[1],
        bookmakers=(
            Bookmaker(
                key="synthetic",
                title="synthetic",
                last_update=now,
                markets=(
                    Market(
                        key="h2h", outcomes=tuple(Outcome(name=n, price=2.0) for n in long_names)
                    ),
                ),
            ),
        ),
    )
    diagnostic, conditional_fair = {}, {}
    signals = find_signals(
        [quotes],
        [odds],
        now=now,
        min_books=1,
        min_edge=0.03,
        max_edge=0.08,
        diag=diagnostic,
        fair_out=conditional_fair,
        capital_usd=100,
        max_stake_pct=1,
        fee_at_stake_count=False,
    )
    if failure:
        assert signals == []
        assert conditional_fair == {}
        assert diagnostic["events_matched"] == 0
        if failure == "swapped":
            assert diagnostic["reject_ticker_team"] == 1
    else:
        assert diagnostic["events_matched"] == 1
        assert diagnostic["reject_ticker_team"] == 0
        assert conditional_fair == {f"{key}-{code}": 0.5 for code in codes}
        assert signals  # Control positivo sintético: el detector sí evaluó la captura.


@pytest.mark.parametrize(
    "series,expected",
    [
        ("KXNFLGAME", "americanfootball_nfl"),
        ("KXNFL", "americanfootball_nfl"),
        ("KXNCAAFGAME", "americanfootball_ncaaf"),
    ],
)
@pytest.mark.parametrize(
    "sport",
    [
        "americanfootball_nfl",
        "americanfootball_ncaaf",
        "americanfootball",
        "americanfootball_nfl_unknown",
        "americanfootball_ncaaf_unknown",
        "baseball_mlb",
    ],
)
def test_football_league_is_exact(series, expected, sport):
    assert series_sport_compatible(f"{series}-26OCT10TEST", sport) is (sport == expected)


@pytest.mark.parametrize("case", CASES)
def test_ticker_independently_checks_known_short_names(case):
    key, _, codes, short_names, _, _ = case
    for i, code in enumerate(codes):
        assert check_ticker_team(f"{key}-{code}", short_names[i]) == "CONSISTENT"
        assert check_ticker_team(f"{key}-{code}", short_names[1 - i]) == "CONTRADICTION"
        assert check_ticker_team(f"{key}-UNKNOWN", short_names[i]) == "UNKNOWN"
        assert check_ticker_team(f"{key}-{code}", "Unknown school") == "UNKNOWN"


def test_football_aliases_do_not_leak_across_sports_or_legacy():
    assert canonical_name("Philadelphia", "americanfootball_nfl") == "philadelphia eagles"
    assert canonical_name("Philadelphia", "baseball_mlb") == "philadelphia phillies"
    assert canonical_name("Philadelphia") == "philadelphia phillies"
    assert canonical_name("Philadelphia", "americanfootball_ncaaf") == "philadelphia"
    assert canonical_name("Georgia", "americanfootball_ncaaf") == "georgia bulldogs"
    assert canonical_name("Georgia", "soccer_fifa_world_cup") == "georgia"
    assert canonical_name("Georgia", "americanfootball_nfl") == "georgia"
    assert canonical_name("Florida State", "americanfootball_ncaaf") == "florida state seminoles"


@pytest.mark.parametrize("row", NFL_CAPTURE, ids=lambda row: row["event_ticker"])
def test_observed_nfl_names_stay_scoped_to_their_league(row):
    for short, full in zip(row["short_names"], row["long_names"], strict=True):
        expected = full.lower()
        assert canonical_name(short, "americanfootball_nfl") == expected
        for sport in ("americanfootball_ncaaf", "baseball_mlb", "basketball_nba", None):
            assert canonical_name(short, sport) != expected


@pytest.mark.parametrize("ambiguous", ["Los Angeles", "New York"])
def test_ambiguous_nfl_city_is_not_assigned_to_a_team(ambiguous):
    assert canonical_name(ambiguous, "americanfootball_nfl") == ambiguous.lower()
    assert check_ticker_team("KXNFLGAME-26OCT12BUFLAR-LAR", ambiguous) == "UNKNOWN"
