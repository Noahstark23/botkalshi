"""El filtro no puede leer el futuro ni convertir incertidumbre en permiso."""

from __future__ import annotations

import copy

import pytest

from scripts.jev_client import JevError, JevResult
from scripts.jev_ecosystem_judgments import (
    DEFAULT_CASES,
    decision,
    evaluate,
    news_state,
    prepare,
)


@pytest.mark.parametrize(
    ("probabilities", "expected"),
    [
        ([0.01, 0.02], "ALLOW"),
        ([0.94, 0.95], "BLOCK"),
        ([0.05, 0.10], "REVIEW"),
        ([0.47, 0.51], "REVIEW"),
        ([0.90, 0.92], "BLOCK"),
        ([0.0], "REVIEW"),
        ([], "REVIEW"),
        ([float("nan"), 0.0], "REVIEW"),
        ([-1, -1], "REVIEW"),
    ],
)
def test_uncertainty_abstains(probabilities, expected):
    assert decision(probabilities) == expected


def test_future_prices_and_labels_never_reach_model():
    fixture, _ = prepare(DEFAULT_CASES)
    state = news_state(fixture, "schedule")
    mutated = copy.deepcopy(fixture)
    mutated["scenarios"] = [{"future_profit": 999999}]
    mutated["news"]["schedule"]["expected_suspension"] = True
    assert news_state(mutated, "schedule") == state
    assert set(state) == {"target_event", "bulletin"}


def test_failure_stops_without_retry_or_free_cost_claim():
    fixture, report = prepare(DEFAULT_CASES)

    class FailingClient:
        def evaluate(self, *_):
            raise JevError("Jev: fallo simulado")

    snapshots = []
    evaluate(fixture, report, FailingClient(), lambda r: snapshots.append(copy.deepcopy(r)))
    assert report["api_attempts"] == 1
    assert report["api_successes"] == 0
    assert report["attempts_with_unknown_usage"] == 1
    assert report["usage_complete"] is False
    assert all(r["decision"] == "REVIEW" for r in report["news_results"].values())
    assert snapshots[0]["api_attempts"] == 1


def test_six_unique_news_not_twenty_two_independent_ai_observations():
    fixture, report = prepare(DEFAULT_CASES)
    states = []

    class FakeClient:
        def evaluate(self, state, instructions):
            states.append(state)
            return JevResult(0.01, "jev-1.13.0", 100, 1, "test-hash", 1.0)

    evaluate(fixture, report, FakeClient(), lambda _: None)
    assert report["api_attempts"] == report["api_successes"] == 12
    assert report["unique_news_completed"] == 6
    assert len(fixture["scenarios"]) == 22
    assert states[0] == states[1]
    assert report["news_results"]["missing"]["decision"] == "REVIEW"
    assert report["news_results"]["unavailable"]["decision"] == "REVIEW"
