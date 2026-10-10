"""Identidad de equipo: código del ticker ↔ yes_sub_title, y alias con scope por deporte."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.clients.odds_api import Bookmaker, Market, OddsEvent, Outcome
from src.strategies.motor_2_consensus.detector import find_signals
from src.strategies.motor_2_consensus.matcher import (
    TEAM_ALIASES,
    canonical_name,
    outcomes_match,
)
from src.strategies.motor_2_consensus.sources import KalshiEventQuotes, KalshiQuote
from src.strategies.motor_2_consensus.team_identity import (
    MLB_TICKER_CODES,
    check_ticker_team,
    ticker_team_code,
)

# --- código del ticker --------------------------------------------------------


@pytest.mark.parametrize(
    ("ticker", "code"),
    [
        ("KXMLBGAME-26JUL15NYMPHI-NYM", "NYM"),
        ("KXMLBGAME-26JUL121610AZLAD-AZ", "AZ"),  # doubleheader con hora
        ("KXMLBGAME-26JUN15LADSFG-SFG", "SFG"),
        ("KXMLBGAME-26JUN15LADSFG", None),  # event_key, no market
        ("KXMLBGAME", None),
    ],
)
def test_ticker_team_code(ticker, code):
    assert ticker_team_code(ticker) == code


@pytest.mark.parametrize(
    ("ticker", "name"),
    [
        ("KXMLBGAME-26JUL15NYMPHI-NYM", "New York M"),
        ("KXMLBGAME-26JUL15NYMPHI-PHI", "Philadelphia"),
        ("KXMLBGAME-26JUL121610AZLAD-AZ", "Arizona"),
        ("KXMLBGAME-26JUN15LADSFG-SFG", "San Francisco"),
        ("KXMLBGAME-26JUN30TBKC-KC", "Kansas City"),
        ("KXMLBGAME-26JUL28HOUWSH-WSH", "Washington"),
        ("KXMLBGAME-26JUL07MILSTL-STL", "St. Louis"),
    ],
)
def test_observed_real_tickers_are_consistent(ticker, name):
    assert check_ticker_team(ticker, name) == "CONSISTENT"


@pytest.mark.parametrize(
    ("ticker", "name"),
    [
        ("KXMLBGAME-26JUL15NYMPHI-NYM", "New York Y"),  # misma ciudad, otro club
        ("KXMLBGAME-26JUL15NYMPHI-NYM", "Philadelphia"),  # hermano cruzado
        ("KXMLBGAME-26JUN15LADSFG-LAD", "Los Angeles A"),
    ],
)
def test_contradiction_detected(ticker, name):
    assert check_ticker_team(ticker, name) == "CONTRADICTION"


@pytest.mark.parametrize(
    ("ticker", "name"),
    [
        ("KXMLBGAME-26JUL15NYMPHI-XYZ", "New York M"),  # código desconocido
        ("KXMLBGAME-26JUL15NYMPHI-NYM", "Equipo Sin Alias"),  # nombre desconocido
        ("KXWCGAME-26JUN27JORARG-ARG", "Argentina"),  # serie sin tabla
        ("KXWCGAME-26JUN27JORARG-TIE", "Draw"),
    ],
)
def test_unknown_never_blocks(ticker, name):
    assert check_ticker_team(ticker, name) == "UNKNOWN"


def test_every_code_maps_to_a_canonical_mlb_team():
    mlb_values = {canonical_name(v, "baseball_mlb") for v in MLB_TICKER_CODES.values()}
    assert mlb_values == set(MLB_TICKER_CODES.values())
    assert len(mlb_values) == 30  # 30 franquicias, ni una de más ni de menos


# --- alias con scope por deporte ---------------------------------------------


def test_mlb_aliases_do_not_leak_into_other_sports():
    assert canonical_name("Boston", "basketball_nba") == "boston"
    assert canonical_name("Giants", "americanfootball_nfl") == "giants"
    assert canonical_name("Boston", "baseball_mlb") == "boston red sox"
    assert canonical_name("Boston") == "boston red sox"  # legacy sin sport intacto


def test_generic_aliases_apply_to_every_sport():
    assert canonical_name("Tie", "soccer_fifa_world_cup") == "draw"
    assert canonical_name("Türkiye", "soccer_fifa_world_cup") == "turkey"


def test_scoped_match_mlb_unchanged():
    k, o = ["New York M", "Cincinnati"], ["New York Mets", "Cincinnati Reds"]
    assert outcomes_match(k, o, "baseball_mlb") is True
    assert (
        outcomes_match(
            ["New York M", "Cincinnati"], ["New York Yankees", "Cincinnati Reds"], "baseball_mlb"
        )
        is False
    )


@pytest.mark.parametrize(
    ("variant", "canonical"),
    [
        ("Oakland A's", "athletics"),
        ("Arizona D-backs", "arizona diamondbacks"),
        ("Los Angeles Angels of Anaheim", "los angeles angels"),
    ],
)
def test_new_mlb_variants(variant, canonical):
    assert canonical_name(variant, "baseball_mlb") == canonical


def test_aliases_still_do_not_chain():
    for value in TEAM_ALIASES.values():
        assert TEAM_ALIASES.get(value, value) == value


# --- integración: el detector descarta el evento con ticker contradictorio ----


def _odds_event(now):
    outcomes = (
        Outcome(name="New York Mets", price=2.0),
        Outcome(name="Philadelphia Phillies", price=2.0),
    )
    return OddsEvent(
        id="o1",
        sport_key="baseball_mlb",
        commence_time=now + timedelta(hours=3),
        home_team="Philadelphia Phillies",
        away_team="New York Mets",
        bookmakers=(
            Bookmaker(key="bk", title="bk", markets=(Market(key="h2h", outcomes=outcomes),)),
        ),
    )


def _kalshi(now, swap: bool):
    date = (now + timedelta(hours=3)).astimezone(UTC)
    key = f"KXMLBGAME-{date:%y}{date:%b}{date:%d}NYMPHI".upper()
    names = ("Philadelphia", "New York M") if swap else ("New York M", "Philadelphia")
    return KalshiEventQuotes(
        event_key=key,
        outcomes=(
            KalshiQuote(
                market_ticker=f"{key}-NYM", outcome_name=names[0], yes_ask_cents=40, no_ask_cents=62
            ),
            KalshiQuote(
                market_ticker=f"{key}-PHI", outcome_name=names[1], yes_ask_cents=40, no_ask_cents=62
            ),
        ),
    )


@pytest.mark.parametrize("swap", [False, True])
def test_detector_gate(swap):
    now = datetime(2026, 7, 15, 16, 0, tzinfo=UTC)
    diag: dict[str, float] = {}
    fair_out: dict[str, float] = {}
    find_signals([_kalshi(now, swap)], [_odds_event(now)], now=now, diag=diag, fair_out=fair_out)
    if swap:
        assert fair_out == {}  # jamás el fair de PHI en el ticker de NYM
        assert diag["reject_ticker_team"] == 1.0
        assert diag["events_matched"] == 0.0
    else:
        # control: con ticker y nombre coherentes sí publica, en el ticker correcto
        assert {t.rsplit("-", 1)[1] for t in fair_out} == {"NYM", "PHI"}
        assert diag["events_matched"] == 1.0
        assert diag["reject_ticker_team"] == 0.0
