"""Mecanismo integrado offline en proceso nuevo; ningún cliente externo se construye."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.jev_ecosystem_judgments import DEFAULT_CASES, decision, prepare
from scripts.jev_ecosystem_replay import load_inputs

ROOT = Path(__file__).resolve().parents[2]


def _judgments(cases: Path, path: Path) -> None:
    fixture, report = prepare(cases)
    # Respuestas inventadas para tests, nunca se atribuyen al proveedor.
    for key, news in fixture["news"].items():
        probabilities = (
            []
            if not news["text"]
            else [0.99, 0.99]
            if news["expected_suspension"]
            else [0.01, 0.01]
        )
        report["news_results"][key].update(
            probabilities=probabilities,
            decision=decision(probabilities),
            reason="test_fixture_not_a_model_call",
        )
    path.write_text(json.dumps(report))


def _run(cases: Path, judgments: Path, output: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "scripts.jev_ecosystem_replay",
            "--cases",
            str(cases),
            "--judgments",
            str(judgments),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=45,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "KALSHI_API_KEY_ID": "DO_NOT_READ_TEST_ENV_CREDENTIAL",
            "DATABASE_URL": "sqlite:////nonexistent/do-not-touch.db",
        },
    )


@pytest.fixture(scope="module")
def replay(tmp_path_factory):
    directory = tmp_path_factory.mktemp("ecosystem-replay")
    cases = directory / "cases.json"
    cases.write_bytes(DEFAULT_CASES.read_bytes())
    judgments = directory / "judgments.json"
    _judgments(cases, judgments)
    output = directory / "output.json"
    completed = _run(cases, judgments, output)
    assert completed.returncode == 0, completed.stderr
    assert "DO_NOT_READ_TEST_ENV_CREDENTIAL" not in completed.stdout + completed.stderr
    report = json.loads(output.read_text())
    return {row["id"]: row for row in report["scenarios"]}, report


@pytest.mark.parametrize(
    "case,pnl", [("buy_gain", 8), ("buy_loss", -6), ("sell_gain", 8), ("sell_loss", -6)]
)
def test_real_engine_roundtrips_include_profit_and_loss(replay, case, pnl):
    rows, report = replay
    for arm in ("baseline", "jev"):
        result = rows[case][arm]
        assert result["realized_cents"] == result["mtm_cents"] == pnl
        assert result["inventory_contracts"] == 0
        assert len(result["fills"]) == 2
        assert result["fees_cents"] == 2
        assert sum(fill["fee_effective_cents"] for fill in result["fills"]) == 2
        assert result["cash_cents"] - result["fees_cents"] == pnl
        assert result["epoch_status"] == "clean" and result["valid"]
        assert result["fake_read_calls"]["get_series"] > 0
        assert result["fake_read_calls"]["get_event"] > 0
        assert result["quotes"][0]["created_at"] == "2026-10-06T16:00:00"
        assert result["fills"][0]["created_at"] == "2026-10-06T16:01:00"
        assert result["fills"][1]["created_at"] == "2026-10-06T16:03:00"
    assert report["worker_api_calls"] == report["financial_orders"] == 0


@pytest.mark.parametrize(
    "case", ["postponed_loss", "postponed_gain", "injection", "missing_news", "api_unavailable"]
)
def test_gate_blocks_before_any_quote_without_hiding_baseline(replay, case):
    rows, _ = replay
    blocked = rows[case]["jev"]
    assert blocked["admitted_before_first_quote"] is False
    assert blocked["quotes"] == blocked["fills"] == blocked["ticks"] == []
    assert blocked["epoch_status"] == "not_started"
    assert len(rows[case]["baseline"]["fills"]) == 2


@pytest.mark.parametrize("case", ["touch_only", "fractional_depth"])
def test_touch_and_insufficient_depth_cannot_invent_fills(replay, case):
    rows, _ = replay
    assert rows[case]["baseline"]["quotes"]
    assert rows[case]["baseline"]["fills"] == []
    assert rows[case]["baseline"]["realized_cents"] == 0


@pytest.mark.parametrize(
    "case", ["stale", "missing_timestamp", "insufficient_books", "mismatch", "kickoff"]
)
def test_data_and_pregame_guards_still_block_m5(replay, case):
    rows, _ = replay
    assert rows[case]["baseline"]["quotes"] == []
    assert rows[case]["baseline"]["fills"] == []


def test_open_inventory_is_mtm_and_never_realized_or_liquidated(replay):
    rows, _ = replay
    result = rows["open_inventory"]["baseline"]
    assert result["inventory_contracts"] == 1
    assert result["cash_cents"] == -45 and result["fees_cents"] == 1
    assert result["mark_cents"] == 42 and result["mtm_cents"] == -4
    assert result["realized_cents"] is None
    assert len(result["fills"]) == 1


def test_missing_book_never_reports_valid_zero_profit(replay):
    rows, _ = replay
    for case in ("empty_book", "lost_book"):
        result = rows[case]["baseline"]
        assert result["missing_book"] and not result["valid"]
        assert result["mtm_cents"] is None and result["realized_cents"] is None
    assert rows["lost_book"]["baseline"]["epoch_status"] == "invalid"


def test_prior_quote_is_settled_before_jump_retreat(replay):
    rows, _ = replay
    result = rows["buy_gain"]["baseline"]
    assert result["ticks"][0]["live_quote"] == {"bid_cents": 45, "ask_cents": 55, "size": 1}
    assert result["ticks"][1]["inventory_contracts"] == 1
    assert result["ticks"][1]["live_quote"] is None
    assert result["fills"][0]["price_cents"] == 45


@pytest.mark.parametrize(
    "field,value",
    [
        ("fixture_sha256", "0" * 64),
        ("prompt_sha256", "1" * 64),
        ("model", "unknown"),
        ("schema", "wrong"),
    ],
)
def test_changed_manifest_is_rejected_before_replay(tmp_path, field, value):
    path = tmp_path / "judgments.json"
    _judgments(DEFAULT_CASES, path)
    report = json.loads(path.read_text())
    report[field] = value
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError):
        load_inputs(DEFAULT_CASES, path)


def test_inconsistent_allow_is_rejected(tmp_path):
    path = tmp_path / "judgments.json"
    _judgments(DEFAULT_CASES, path)
    report = json.loads(path.read_text())
    report["news_results"]["postponed"]["decision"] = "ALLOW"
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="decisión"):
        load_inputs(DEFAULT_CASES, path)


def test_existing_output_survives_unchanged(tmp_path):
    output = tmp_path / "result.json"
    output.write_text("preservar")
    completed = _run(DEFAULT_CASES, tmp_path / "missing-judgments.json", output)
    assert completed.returncode != 0
    assert output.read_text() == "preservar"


def test_future_prices_and_local_labels_cannot_change_past_quotes(tmp_path):
    fixture = json.loads(DEFAULT_CASES.read_text())
    fixture["scenarios"] = [fixture["scenarios"][0]]
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps(fixture))
    judgments = tmp_path / "judgments.json"
    _judgments(cases, judgments)
    first = tmp_path / "first.json"
    assert _run(cases, judgments, first).returncode == 0
    # Sólo cambia el futuro. Las etiquetas locales tampoco son inputs de M2/M5.
    fixture["scenarios"][0]["ticks"][-1].update(yes_bid=42, yes_ask=46)
    fixture["scenarios"][0]["expected_realized_cents"] = -999999
    fixture["news"]["schedule"]["expected_suspension"] = True
    cases.write_text(json.dumps(fixture))
    manifest = json.loads(judgments.read_text())
    manifest["fixture_sha256"] = hashlib.sha256(cases.read_bytes()).hexdigest()
    judgments.write_text(json.dumps(manifest))
    second = tmp_path / "second.json"
    assert _run(cases, judgments, second).returncode == 0
    before = json.loads(first.read_text())["scenarios"][0]["baseline"]
    after = json.loads(second.read_text())["scenarios"][0]["baseline"]
    assert before["ticks"][:3] == after["ticks"][:3]
    assert before["realized_cents"] == 8
    assert after["realized_cents"] is None


def test_offline_guard_blocks_network_and_client_construction(tmp_path):
    code = """
from scripts.jev_ecosystem_replay import offline_process
with offline_process():
    import os, socket
    from src.clients.kalshi_rest import KalshiRestClient
    from src.clients.odds_api import OddsAPIClient
    assert 'FORBIDDEN_TEST_KEY' not in os.environ
    for action in (lambda: socket.create_connection(('example.com', 443)),
                   lambda: socket.socket().connect(('127.0.0.1', 9)),
                   lambda: KalshiRestClient(), lambda: OddsAPIClient('unused')):
        try: action()
        except RuntimeError as error: assert 'REPLAY_OFFLINE' in str(error)
        else: raise AssertionError('guard bypass')
"""
    completed = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=ROOT,
        env={"PATH": "/usr/bin:/bin", "FORBIDDEN_TEST_KEY": "unused"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
