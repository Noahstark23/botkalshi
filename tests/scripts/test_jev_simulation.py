"""Prueba de controles de investigación; nunca evidencia de alpha o inferencia Jev."""

from dataclasses import replace
from decimal import Decimal
from unittest.mock import Mock

import pytest

from scripts.jev_client import JevError
from scripts.jev_simulation import SCENARIOS, Scenario, evaluate_cases, main, paper_result


def test_outcome_and_labels_never_reach_model():
    case = Scenario("initial")
    changed = replace(case, exit_bid=Decimal("1"), label=0, fill=Decimal("0.25"))
    assert case.model_state() == changed.model_state()
    assert set(case.model_state()) == {"instrument", "hypothesis", "report"}


def test_partial_fill_keeps_fraction_and_actual_fees():
    result = paper_result(Scenario("partial", fill=Decimal("0.25")), True)
    assert result["filled_units"] == "0.25"
    assert Decimal(result["gross_usd"]) == Decimal("0.245")
    assert Decimal(result["fees_usd"]) == Decimal("0.060306")
    assert Decimal(result["net_usd"]) == Decimal("0.184694")


@pytest.mark.parametrize(
    "name",
    [
        "no_liquidity",
        "wide_spread",
        "stale_book",
        "kill_switch",
        "unreconciled_restart",
        "insufficient_cash",
        "injected_halt",
    ],
)
def test_model_yes_never_overrides_mechanical_gate(name):
    case = next(case for case in SCENARIOS if case.name == name)
    assert paper_result(case, True)["action"] == "HOLD"


def test_no_future_profit_guarantee_and_costs_on_flat_market():
    for name in ("reversal_after_signal", "flat_after_signal", "adverse_latency"):
        case = next(case for case in SCENARIOS if case.name == name)
        assert Decimal(paper_result(case, True)["net_usd"]) < 0


def test_missing_future_exit_keeps_entry_but_invalidates_result():
    result = paper_result(Scenario("future_missing", exit_bid=Decimal("NaN")), True)
    assert result == {"action": "PAPER_LONG", "status": "INCOMPLETE", "net_usd": None}


def test_positive_funding_is_a_cost():
    result = paper_result(Scenario("funding", funding_bps=Decimal("10")), True)
    assert Decimal(result["funding_usd"]) == Decimal("0.10002")


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity"])
def test_invalid_price_never_enters(number):
    assert paper_result(Scenario("invalid", ask=Decimal(number)), True)["action"] == "HOLD"


def test_offline_discloses_no_jev_calls():
    report = evaluate_cases("offline")
    assert report["source"] == "FIXED_TEST_RESPONSES_NOT_JEV"
    assert report["completed_scenarios"] == 15
    assert report["unique_semantic_inputs"] == 4
    assert report["assumptions"]["maximum_api_calls"] == 30
    assert report["api_successes"] == 0
    assert report["production_ready"] is False


def test_model_failure_abstains_and_stops_batch():
    client = Mock()
    client.evaluate.side_effect = JevError("timeout")
    report = evaluate_cases("api", client)
    assert report["api_errors"] == 1
    assert client.evaluate.call_count == 1
    assert report["results"][0]["rules_and_evaluator"]["action"] == "HOLD"
    assert report["completed_scenarios"] == 0


@pytest.mark.parametrize("values", [(0.99, 0.5), (0.5, 0.5), (0.95, 0.90)])
def test_uncertain_or_inconsistent_model_abstains(values):
    client = Mock()
    client.evaluate.side_effect = [
        Mock(
            probability_yes=p,
            model="jev-1.13.0",
            input_tokens=10,
            output_tokens=2,
            request_sha256="synthetic_transport_test",
            latency_ms=1,
        )
        for p in values
    ] + [JevError("stop_fixture")]
    report = evaluate_cases("api", client)
    assert report["results"][0]["rules_and_evaluator"]["action"] == "HOLD"


def test_missing_key_is_explicitly_blocked(monkeypatch, capsys):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert main(["--mode", "api"]) == 2
    assert '"api_calls": 0' in capsys.readouterr().out
