"""El replay debe calcular el consenso y respetar los rechazos reales de M2."""

from datetime import timedelta

import pytest

from scripts.jev_ecosystem_inputs import BASE_TIME, TARGET_TICKER, compute_fairs
from src.strategies.fair_value_book import FairValueBook


def test_real_consensus_provenance_and_no_implicit_publication():
    FairValueBook.clear()
    result = compute_fairs({"id": "local", "odds_mode": "fresh", "fair_prob": 0.35}, BASE_TIME)
    assert result["fairs"][TARGET_TICKER] == pytest.approx(0.35)
    assert sum(result["fairs"].values()) == pytest.approx(1.0)
    assert len(result["provenances"][TARGET_TICKER].bookmaker_keys) == 3
    assert result["commence_times"][TARGET_TICKER] == BASE_TIME + timedelta(hours=3)
    assert FairValueBook.size() == 0


@pytest.mark.parametrize("mode", ["stale", "missing_timestamp", "insufficient_books", "mismatch"])
def test_bad_odds_do_not_generate_reference_prices(mode):
    assert compute_fairs({"id": "local", "odds_mode": mode}, BASE_TIME)["fairs"] == {}


def test_kickoff_does_not_move_forward_with_replay_clock():
    case = {"id": "local", "odds_mode": "kickoff"}
    before = compute_fairs(case, BASE_TIME)
    assert before["commence_times"][TARGET_TICKER] == BASE_TIME + timedelta(seconds=60)
    assert compute_fairs(case, BASE_TIME + timedelta(seconds=60))["fairs"] == {}
