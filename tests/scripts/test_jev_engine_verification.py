"""Prueba el verificador sin red ni inferencias reales; no certifica motores."""

import ast
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import jev_engine_verification as verifier
from scripts.jev_client import JevError, JevResult


def fixture_case(**changes):
    return {
        "id": "local-only-identifier",
        "motor": "M5",
        "criterion": "Does evidence show resting quotes remained after the cash source expired?",
        "evidence": "Cash expired and a quote remained resting after reconciliation.",
        "expected_defect": True,
        "provenance": [{"path": "private-code.py", "line": 123, "note": "local-only-label"}],
        "evidence_kind": "synthetic_contract_fixture",
        **changes,
    }


def response(probability):
    return JevResult(probability, "jev-1.13.0", 100, 5, "fake-http-request-hash", 10.0)


def write_cases(tmp_path, cases):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(cases))
    return path


def test_only_evidence_and_criterion_reach_model():
    client = Mock()
    client.evaluate.side_effect = [response(0.95), response(0.94)]
    case = fixture_case()
    report = verifier.evaluate_cases([case], client, "digest")
    for call in client.evaluate.call_args_list:
        state, instructions = call.args
        assert state == {"criterion": case["criterion"], "evidence": case["evidence"]}
        assert case["id"] not in json.dumps(state)
        assert "private-code.py" not in json.dumps(state)
        assert instructions == verifier.INSTRUCTIONS
    assert report["production_ready"] is False
    assert report["real_orders"] == 0


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([0.9, 0.92], "FLAG"),
        ([0.08, 0.1], "NO_DEFECT_SHOWN"),
        ([0.5, 0.5], "REVIEW"),
        ([0.9, 0.89], "REVIEW"),
        ([0.01, 0.09], "REVIEW"),
        ([0.97], "REVIEW"),
        ([], "REVIEW"),
    ],
)
def test_triage_requires_both_extreme_and_stable_values(values, expected):
    assert verifier.triage(values) == expected


def test_second_request_failure_retains_first_success_and_stops():
    client = Mock()
    client.evaluate.side_effect = [response(0.95), JevError("Jev: HTTP 429; sin reintento")]
    checkpoints = []
    report = verifier.evaluate_cases(
        [fixture_case(), fixture_case(id="second")],
        client,
        "digest",
        lambda r: checkpoints.append(json.loads(json.dumps(r))),
    )
    assert client.evaluate.call_count == 2
    assert report["status"] == "INCOMPLETE_API_ERROR"
    assert report["api_attempts"] == 2
    assert report["api_successes"] == 1
    assert report["api_errors"] == 1
    assert report["completed_cases"] == 0
    assert report["results"][0]["triage"] == "REVIEW"
    assert report["accuracy_first_repeat"] is None
    assert report["usage_complete"] is False
    assert report["attempts_with_unknown_usage"] == 1
    assert any(c["api_successes"] == 1 for c in checkpoints)


def test_metrics_count_cases_not_repeats_and_keep_false_positives():
    cases = [fixture_case(), fixture_case(id="control", expected_defect=False)]
    client = Mock()
    client.evaluate.side_effect = [response(0.95), response(0.94), response(0.6), response(0.59)]
    report = verifier.evaluate_cases(cases, client, "digest")
    assert report["completed_cases"] == 2
    assert report["api_successes"] == 4
    assert report["accuracy_first_repeat"] == 0.5
    assert report["confusion_first_repeat_at_05"]["false_positive"] == 1
    assert report["brier_first_repeat"] == pytest.approx((0.05**2 + 0.6**2) / 2)
    assert report["triage_counts"] == {"FLAG": 1, "REVIEW": 1}
    assert report["estimated_known_usage_cost_usd"] == "0.0000168"
    assert report["usage_complete"] is True


@pytest.mark.parametrize("count", [0, 25])
def test_case_budget_rejected_before_api(tmp_path, count):
    path = write_cases(tmp_path, [fixture_case(id=str(i)) for i in range(count)])
    with pytest.raises(ValueError):
        verifier.load_cases(path)


@pytest.mark.parametrize(
    "change",
    [{"expected_defect": "false"}, {"evidence_kind": "production_log"}, {"provenance": []}],
)
def test_invalid_cases_rejected(tmp_path, change):
    path = write_cases(tmp_path, [fixture_case(**change)])
    with pytest.raises(ValueError):
        verifier.load_cases(path)


def test_duplicate_identifiers_are_rejected(tmp_path):
    path = write_cases(tmp_path, [fixture_case(), fixture_case()])
    with pytest.raises(ValueError):
        verifier.load_cases(path)


def test_case_digest_also_freezes_labels(tmp_path):
    path = write_cases(tmp_path, [fixture_case()])
    _, before = verifier.load_cases(path)
    write_cases(tmp_path, [fixture_case(expected_defect=False)])
    _, after = verifier.load_cases(path)
    assert before != after


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o666])
def test_key_with_group_or_world_permissions_is_rejected(tmp_path, mode):
    path = tmp_path / "key"
    path.write_text("not-a-real-key")
    path.chmod(mode)
    with pytest.raises(ValueError):
        verifier.read_private_key(path)


def test_key_symlink_is_rejected(tmp_path):
    actual = tmp_path / "actual"
    actual.write_text("not-a-real-key")
    actual.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(actual)
    with pytest.raises(OSError):
        verifier.read_private_key(link)


def test_prepare_never_constructs_client_or_reads_key(tmp_path, monkeypatch, capsys):
    path = write_cases(tmp_path, [fixture_case()])
    client = Mock(side_effect=AssertionError("client forbidden"))
    key_reader = Mock(side_effect=AssertionError("key forbidden"))
    monkeypatch.setattr(verifier, "JevClient", client)
    monkeypatch.setattr(verifier, "read_private_key", key_reader)
    assert verifier.main(["--cases", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["api_attempts"] == 0
    client.assert_not_called()
    key_reader.assert_not_called()


def test_existing_result_prevents_repeat_calls(tmp_path, monkeypatch, capsys):
    path = write_cases(tmp_path, [fixture_case()])
    output = tmp_path / "existing.json"
    output.write_text('{"previous":true}')
    client = Mock(side_effect=AssertionError("client forbidden"))
    monkeypatch.setattr(verifier, "JevClient", client)
    assert verifier.main(["--mode", "api", "--cases", str(path), "--output", str(output)]) == 2
    assert "NEW_OUTPUT_PATH_REQUIRED" in capsys.readouterr().out
    assert output.read_text() == '{"previous":true}'
    client.assert_not_called()


def test_checkpoint_failure_preserves_previous_valid_result(tmp_path, monkeypatch):
    output = tmp_path / "progress.json"
    previous = {"api_successes": 12, "status": "RUNNING"}
    verifier.save_report_atomically(output, previous)
    monkeypatch.setattr(verifier.os, "replace", Mock(side_effect=OSError("disk failure")))
    with pytest.raises(OSError):
        verifier.save_report_atomically(output, {"api_successes": 13})
    assert json.loads(output.read_text()) == previous
    assert list(tmp_path.iterdir()) == [output]


def test_module_has_no_import_path_to_bot_or_order_clients():
    tree = ast.parse(Path(verifier.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert not (node.module or "").startswith("src")
        if isinstance(node, ast.Import):
            assert all(not alias.name.startswith("src") for alias in node.names)


def test_complete_fixture_is_balanced_and_covers_all_motors():
    cases, _ = verifier.load_cases(verifier.DEFAULT_CASES)
    assert len(cases) == 24
    assert sum(c["expected_defect"] for c in cases) == 12
    assert {"M1", "M2", "M3", "M5", "REST", "M6", "M8", "M9"} <= {c["motor"] for c in cases}
