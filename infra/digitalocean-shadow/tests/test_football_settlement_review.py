"""Un empate con devolución no se declara equivalente a liquidación 50/50."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from football_settlement_review import FANATICS_MA, RuleProfile, compare_rules  # noqa: E402


def test_tie_and_postponement_differences_remain_blockers_even_with_verified_identity():
    kalshi = RuleProfile(
        "https://kalshi.com", "US", "included", "split_50", "starts_within_48_hours", "fair_price"
    )
    r = compare_rules(kalshi, FANATICS_MA, quote_jurisdiction="MA")
    assert r["differences"] == ["draw", "postponement"]
    assert r["missing_dimensions"] == ["cancellation"]
    assert r["review_status"] == "DIFFERENT"
    assert not r["fair_or_signal_authorized"]


def test_matching_fields_cannot_certify_unmapped_jurisdiction():
    r = compare_rules(FANATICS_MA, FANATICS_MA, quote_jurisdiction=None)
    assert r["review_status"] == "INCOMPLETE"
    assert "QUOTE_RULE_JURISDICTION_UNVERIFIED" in r["blockers"]


def test_two_unknowns_do_not_count_as_equivalent():
    unknown = RuleProfile("https://example.test/rules", "MA", None, None, None, None)
    r = compare_rules(unknown, unknown, quote_jurisdiction="MA")
    assert len(r["missing_dimensions"]) == 4
    assert not r["full_contract_equivalence_verified"]


def test_matching_reviewed_dimensions_are_never_an_execution_or_fair_gate():
    complete = RuleProfile(
        "https://example.test/rules",
        "MA",
        "included",
        "refund_stake",
        "same_scheduling_week",
        "refund_stake",
    )
    r = compare_rules(complete, complete, quote_jurisdiction="MA")
    assert r["review_status"] == "DIMENSIONS_MATCH"
    assert not r["full_contract_equivalence_verified"]
    assert not r["fair_or_signal_authorized"]
    assert not r["execution_authorized"]
