"""Identidades de dos pares curados y metadatos de replay; jamás señales."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import football_replay_contract as football  # noqa: E402


def fixture(league):
    if league == "nfl":
        prefix, stamp, codes = "KXNFLGAME", "26OCT11", ["PHI", "JAC"]
        names, odds_names = (
            ["Philadelphia", "Jacksonville"],
            ["Philadelphia Eagles", "Jacksonville Jaguars"],
        )
        kickoff, sport = "2026-10-11T16:30:00Z", "americanfootball_nfl"
    else:
        prefix, stamp, codes = "KXNCAAFGAME", "26OCT09", ["FSU", "LOU"]
        names, odds_names = (
            ["Florida St.", "Louisville"],
            ["Florida State Seminoles", "Louisville Cardinals"],
        )
        kickoff, sport = "2026-10-10T02:00:00Z", "americanfootball_ncaaf"
    ticker = prefix + "-" + stamp + "".join(codes)
    event = {"event_ticker": ticker, "series_ticker": prefix}
    markets = [
        {
            "ticker": ticker + "-" + code,
            "event_ticker": ticker,
            "yes_sub_title": name,
            "status": "active",
            "occurrence_datetime": kickoff,
            "custom_strike": {"football_team": "id-" + code},
            "rules_primary": "Fixture only",
            "rules_secondary": "Explicit fixture rules",
        }
        for name, code in zip(names, codes, strict=True)
    ]
    series = {"ticker": prefix, "fee_type": "quadratic", "fee_multiplier": 1}
    odds = {
        "id": "fixture",
        "sport_key": sport,
        "home_team": odds_names[0],
        "away_team": odds_names[1],
        "commence_time": kickoff,
    }
    milestones = {
        "cursor": "",
        "milestones": [
            {
                "id": "fixture-milestone",
                "category": "Sports",
                "type": "football_game",
                "start_date": kickoff,
                "last_updated_ts": "2026-10-09T12:00:00Z",
                "primary_event_tickers": [ticker],
                "related_event_tickers": [ticker],
                "details": {
                    "main_game_event_ticker": ticker,
                    "league": "NFL" if league == "nfl" else "NCAAFB",
                    "status": "scheduled",
                    "home_team_id": "id-" + codes[0],
                    "away_team_id": "id-" + codes[1],
                },
            }
        ],
    }
    return {"event": event, "markets": markets, "milestones": milestones}, series, odds


def _inspect(payload, series):
    return football.inspect_event(payload, series, milestones=payload["milestones"])


@pytest.mark.parametrize("league", ["nfl", "ncaaf"])
def test_identity_kickoff_and_fee_recognized_without_claiming_rule_equivalence(league):
    payload, series, odds = fixture(league)
    result = _inspect(payload, series)
    assert result["fee_type"] == "quadratic"
    assert not result["event_equivalence_verified"]
    check = football.compare_odds(
        result, odds, observed_at="2026-10-09T16:00:00Z", now="2026-10-09T16:01:00Z"
    )
    assert check["participants_and_kickoff_match"]
    assert check["blockers"] == ["SPORTSBOOK_SETTLEMENT_RULES_NOT_PROVIDED"]
    assert not check["fair_or_signal_authorized"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("yes_sub_title", "Louisville"),
        ("ticker", "KXNFLGAME-26OCT11PHIJAC-JAC"),
        ("custom_strike", {}),
        ("rules_secondary", ""),
        ("status", "closed"),
    ],
)
def test_conflicts_and_missing_rules_reject(field, value):
    payload, series, _ = fixture("nfl")
    payload["markets"][0][field] = value
    with pytest.raises(football.ContractError):
        _inspect(payload, series)


def test_duplicate_participant_and_cross_league_reject():
    payload, series, _ = fixture("nfl")
    payload["markets"][1] = copy.deepcopy(payload["markets"][0])
    with pytest.raises(football.ContractError):
        _inspect(payload, series)
    payload, _, _ = fixture("ncaaf")
    with pytest.raises(football.ContractError):
        _inspect(payload, series)


@pytest.mark.parametrize("value", [True, "NaN", "Infinity", -1, 0, "-0.5", 101])
def test_invalid_fee_multiplier_rejects(value):
    payload, series, _ = fixture("nfl")
    series["fee_multiplier"] = value
    with pytest.raises(football.ContractError):
        _inspect(payload, series)


def test_override_must_be_complete_and_known():
    payload, series, _ = fixture("nfl")
    payload["event"]["fee_type_override"] = "quadratic_with_maker_fees"
    with pytest.raises(football.ContractError):
        _inspect(payload, series)
    payload["event"]["fee_multiplier_override"] = "0.5"
    assert _inspect(payload, series)["fee_source"] == "event_override"
    payload["event"]["fee_type_override"] = "flat"
    with pytest.raises(football.ContractError):
        _inspect(payload, series)


@pytest.mark.parametrize(
    "field,value",
    [
        ("sport_key", "baseball_mlb"),
        ("home_team", "Miami"),
        ("commence_time", "2026-10-11T16:31:00Z"),
    ],
)
def test_odds_wrong_league_unknown_team_or_changed_kickoff_rejects(field, value):
    payload, series, odds = fixture("nfl")
    odds[field] = value
    with pytest.raises(football.ContractError):
        football.compare_odds(
            _inspect(payload, series),
            odds,
            observed_at="2026-10-09T16:00:00Z",
            now="2026-10-09T16:01:00Z",
        )


def test_old_snapshot_remains_old_even_if_names_match():
    payload, series, odds = fixture("nfl")
    result = football.compare_odds(
        _inspect(payload, series),
        odds,
        observed_at="2026-10-09T16:00:00Z",
        now="2026-10-09T17:00:00Z",
    )
    assert result["odds_age_seconds"] == 3600
    assert "ODDS_SNAPSHOT_STALE_OR_FUTURE" in result["blockers"]


def test_occurrence_is_not_kickoff_and_missing_milestone_never_falls_back():
    payload, series, odds = fixture("nfl")
    payload["milestones"]["milestones"][0]["start_date"] = "2026-10-11T13:30:00Z"
    odds["commence_time"] = "2026-10-11T13:30:00Z"
    contract = _inspect(payload, series)
    assert contract["kickoff"] == "2026-10-11T13:30:00+00:00"
    assert set(contract["market_occurrence_datetimes"].values()) == {"2026-10-11T16:30:00Z"}
    assert contract["kickoff_source"] == "milestone.start_date"
    assert football.compare_odds(
        contract, odds, observed_at="2026-10-10T17:00:00Z", now="2026-10-10T17:01:00Z"
    )["participants_and_kickoff_match"]
    with pytest.raises(football.ContractError, match="MILESTONES"):
        football.inspect_event(payload, series)


@pytest.mark.parametrize(
    "mutation",
    [
        "cursor",
        "duplicate",
        "league",
        "status",
        "related",
        "main_event",
        "primary",
        "team",
        "date",
        "naive",
        "missing_id",
    ],
)
def test_milestone_conflicts_cannot_select_a_start_time(mutation):
    payload, series, _ = fixture("nfl")
    page = payload["milestones"]
    row = page["milestones"][0]
    if mutation == "cursor":
        page["cursor"] = "unread-page"
    elif mutation == "duplicate":
        page["milestones"].append(copy.deepcopy(row))
    elif mutation in {"league", "status"}:
        row["details"][mutation] = "other"
    elif mutation in {"related", "primary"}:
        row[mutation + "_event_tickers"] = []
    elif mutation == "main_event":
        row["details"]["main_game_event_ticker"] = "OTHER"
    elif mutation == "team":
        row["details"]["away_team_id"] = "id-OTHER"
    elif mutation == "date":
        row["start_date"] = "2026-10-12T13:30:00Z"
    elif mutation == "naive":
        row["start_date"] = "2026-10-11T13:30:00"
    else:
        row["id"] = ""
    with pytest.raises(football.ContractError):
        _inspect(payload, series)


def test_game_start_cutoff_uses_milestone_even_if_occurrence_is_later():
    payload, series, odds = fixture("nfl")
    payload["milestones"]["milestones"][0]["start_date"] = "2026-10-11T13:30:00Z"
    odds["commence_time"] = "2026-10-11T13:30:00Z"
    result = football.compare_odds(
        _inspect(payload, series),
        odds,
        observed_at="2026-10-11T13:31:00Z",
        now="2026-10-11T13:31:00Z",
    )
    assert "EVENT_ALREADY_STARTED" in result["blockers"]


@pytest.mark.parametrize(
    "change,valid",
    [
        (None, True),
        ("stale", False),
        ("future", False),
        ("price_nan", False),
        ("price_bool", False),
        ("duplicate_outcome", False),
        ("wrong_market", False),
        ("no_timestamp", False),
    ],
)
def test_book_quote_data_is_checked_separately_from_capture_age(change, valid):
    payload, series, odds = fixture("nfl")
    market = {
        "key": "h2h",
        "last_update": "2026-10-10T17:00:00Z",
        "outcomes": [
            {"name": name, "price": 1.9} for name in ("Philadelphia Eagles", "Jacksonville Jaguars")
        ],
    }
    odds["bookmakers"] = [{"key": "example", "markets": [market]}]
    if change == "stale":
        market["last_update"] = "2026-10-10T16:00:00Z"
    elif change == "future":
        market["last_update"] = "2026-10-10T18:00:00Z"
    elif change == "no_timestamp":
        del market["last_update"]
    elif change == "price_nan":
        market["outcomes"][0]["price"] = "NaN"
    elif change == "price_bool":
        market["outcomes"][0]["price"] = True
    elif change == "duplicate_outcome":
        market["outcomes"][0] = market["outcomes"][1]
    elif change == "wrong_market":
        market["key"] = "h2h_3_way"
    result = football.inspect_odds_books(
        _inspect(payload, series),
        odds,
        observed_at="2026-10-10T17:01:00Z",
        now="2026-10-10T17:01:00Z",
    )
    assert result["usable_quote_data_count"] == int(valid)
    assert not result["books"][0]["fair_or_signal_authorized"]


def test_duplicate_book_cannot_be_counted_as_independent_source():
    payload, series, odds = fixture("nfl")
    odds["bookmakers"] = [{"key": "same", "markets": []}] * 2
    with pytest.raises(football.ContractError, match="DUPLICATE_BOOKMAKER"):
        football.inspect_odds_books(
            _inspect(payload, series),
            odds,
            observed_at="2026-10-10T17:01:00Z",
            now="2026-10-10T17:01:00Z",
        )
