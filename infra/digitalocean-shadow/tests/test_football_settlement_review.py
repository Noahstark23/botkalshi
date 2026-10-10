"""Un empate con devolución no se declara equivalente a liquidación 50/50."""

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from football_settlement_review import (  # noqa: E402
    DIMENSIONS,
    FANATICS_MA,
    FANATICS_MA_NCAAF,
    FANDUEL_NJ,
    FANDUEL_NJ_NCAAF,
    RuleProfile,
    compare_rules,
)


def test_tie_and_postponement_differences_remain_blockers_even_with_verified_identity():
    kalshi = RuleProfile(
        "https://kalshi.com", "US", "included", "split_50", "starts_within_48_hours", "fair_price"
    )
    r = compare_rules(kalshi, FANATICS_MA, quote_jurisdiction="MA")
    assert r["differences"] == ["draw", "postponement", "cancellation"]
    assert r["missing_dimensions"] == list(DIMENSIONS[4:])
    assert r["review_status"] == "DIFFERENT"
    assert not r["fair_or_signal_authorized"]


def test_matching_fields_cannot_certify_unmapped_jurisdiction():
    r = compare_rules(FANATICS_MA, FANATICS_MA, quote_jurisdiction=None)
    assert r["review_status"] == "INCOMPLETE"
    assert "QUOTE_RULE_JURISDICTION_UNVERIFIED" in r["blockers"]


def test_two_unknowns_do_not_count_as_equivalent():
    unknown = RuleProfile("https://example.test/rules", "MA", None, None, None, None)
    r = compare_rules(unknown, unknown, quote_jurisdiction="MA")
    assert len(r["missing_dimensions"]) == 9
    assert not r["full_contract_equivalence_verified"]


def test_matching_reviewed_dimensions_are_never_an_execution_or_fair_gate():
    complete = RuleProfile(
        "https://example.test/rules",
        "MA",
        "included",
        "refund_stake",
        "same_scheduling_week",
        "refund_stake",
        suspension="synthetic",
        forfeit_before_start="synthetic",
        venue_reversal="synthetic",
        result_corrections="synthetic",
        replay_from_start="synthetic",
        league="nfl",
        phase="regular_season",
        market_period="full_game_2way",
    )
    r = compare_rules(complete, complete, quote_jurisdiction="MA")
    assert r["review_status"] == "DIMENSIONS_MATCH"
    assert not r["full_contract_equivalence_verified"]
    assert not r["fair_or_signal_authorized"]
    assert not r["execution_authorized"]


def test_fanatics_calendar_is_scoped_by_league():
    assert FANATICS_MA.postponement == "starts_in_nfl_week_thursday_wednesday"
    assert FANATICS_MA_NCAAF.postponement == "starts_in_ncaa_week_tuesday_monday"
    r = compare_rules(FANATICS_MA, FANATICS_MA_NCAAF, quote_jurisdiction="MA")
    assert r["scope_differences"] == ["league"]
    assert "RULE_SCOPE_MISMATCH" in r["blockers"]


def test_fanduel_nfl_clock_is_not_falsely_assigned_to_college():
    assert "current_nfl_schedule" in FANDUEL_NJ.postponement
    assert "same_nfl_week" not in FANDUEL_NJ.postponement
    assert FANDUEL_NJ_NCAAF.postponement is None
    assert "24h" in FANDUEL_NJ.suspension
    r = compare_rules(FANDUEL_NJ_NCAAF, FANDUEL_NJ_NCAAF, quote_jurisdiction="NJ")
    assert "postponement" in r["missing_dimensions"]


def test_playoffs_and_regulation_only_cannot_inherit_regular_full_game_rules():
    for field, value in [("phase", "playoffs"), ("market_period", "regulation_3way")]:
        r = compare_rules(
            FANATICS_MA, replace(FANATICS_MA, **{field: value}), quote_jurisdiction="MA"
        )
        assert r["scope_differences"] == [field]
        assert "RULE_SCOPE_MISMATCH" in r["blockers"]
