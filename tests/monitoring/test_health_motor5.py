"""Motor 5 no puede quedar verde si su loop terminó o dejó de completar ticks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from src.monitoring.health import (
    MOTOR5_FAIR_FLOW_GRACE_SEC,
    MOTOR5_HEARTBEAT_MAX_AGE_SEC,
    MOTOR5_QUOTE_FLOW_GRACE_SEC,
    BotState,
    app,
)


@pytest.fixture(autouse=True)
def reset_motor5_state():
    original_started_at = BotState.started_at
    original_db_initialized = BotState.db_initialized
    BotState.started_at = datetime.now(UTC) - timedelta(minutes=10)
    BotState.last_error = None
    BotState.last_error_at = None
    BotState.is_paused = False
    BotState.capture_running = True
    BotState.last_ws_message = datetime.now(UTC)
    BotState.v2_manager = None
    BotState.db_initialized = True
    BotState.motor5_enabled = False
    BotState.motor5_task_running = False
    BotState.last_motor5_tick_started = None
    BotState.last_motor5_tick = None
    BotState.motor5_experiment_id = None
    BotState.motor5_last_book_attempted = 0
    BotState.motor5_last_skip_no_book = 0
    BotState.motor5_fee_policy_blocked = False
    BotState.motor5_no_fair_since = None
    BotState.motor5_no_quote_since = None
    BotState.motor5_book_flow_blocked = False
    BotState.motor5_experiment_invalid = False
    BotState.motor5_experiment_invalid_reason = None
    yield
    BotState.motor5_enabled = False
    BotState.motor5_task_running = False
    BotState.last_motor5_tick_started = None
    BotState.last_motor5_tick = None
    BotState.motor5_experiment_id = None
    BotState.motor5_last_book_attempted = 0
    BotState.motor5_last_skip_no_book = 0
    BotState.motor5_fee_policy_blocked = False
    BotState.motor5_no_fair_since = None
    BotState.motor5_no_quote_since = None
    BotState.motor5_book_flow_blocked = False
    BotState.motor5_experiment_invalid = False
    BotState.motor5_experiment_invalid_reason = None
    BotState.last_error = None
    BotState.last_error_at = None
    BotState.capture_running = False
    BotState.last_ws_message = None
    BotState.v2_manager = None
    BotState.started_at = original_started_at
    BotState.db_initialized = original_db_initialized


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_enabled_without_first_tick_is_not_ready(client):
    BotState.motor5_enabled = True
    BotState.motor5_task_running = True

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["detail"]["checks"]["motor5_task_alive"] is False


def test_fresh_tick_is_healthy_and_ready(client):
    BotState.motor5_enabled = True
    BotState.motor5_task_running = True
    BotState.motor5_heartbeat(
        experiment_id="m5-f1-test",
        fair_fresh=3,
        book_attempted=3,
        quoted=2,
        skip_no_book=0,
        skip_fee_policy=0,
    )

    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200


def test_stale_tick_breaks_health_and_readiness(client):
    BotState.motor5_enabled = True
    BotState.motor5_task_running = True
    BotState.last_motor5_tick = datetime.now(UTC) - timedelta(
        seconds=MOTOR5_HEARTBEAT_MAX_AGE_SEC + 1
    )

    health = client.get("/health")
    ready = client.get("/ready")

    assert health.status_code == 200  # llamada/tick lento no provoca restart-loop
    assert health.json()["checks"]["motor5_task_alive"] is True
    assert ready.status_code == 503
    assert ready.json()["detail"]["checks"]["motor5_task_alive"] is False


def test_task_terminal_si_rompe_liveness(client):
    BotState.motor5_enabled = True
    BotState.motor5_task_running = False

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["detail"]["checks"]["motor5_task_alive"] is False


def test_fee_policy_block_is_visible_in_readiness(client):
    BotState.motor5_enabled = True
    BotState.motor5_task_running = True
    BotState.motor5_heartbeat(
        experiment_id="m5-f1-test",
        fair_fresh=3,
        book_attempted=3,
        quoted=0,
        skip_no_book=0,
        skip_fee_policy=3,
    )

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["detail"]["checks"]["motor5_fee_policy"] is False


def test_no_fair_has_startup_grace_but_eventually_breaks_readiness(client):
    BotState.motor5_enabled = True
    BotState.motor5_task_running = True
    BotState.motor5_heartbeat(
        experiment_id="m5-f1-test",
        fair_fresh=0,
        book_attempted=0,
        quoted=0,
        skip_no_book=0,
        skip_fee_policy=0,
    )

    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200

    BotState.motor5_no_fair_since = datetime.now(UTC) - timedelta(
        seconds=MOTOR5_FAIR_FLOW_GRACE_SEC + 1
    )
    assert client.get("/health").status_code == 200
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["detail"]["checks"]["motor5_fair_flow"] is False


def test_fair_flow_recovers_on_next_nonempty_tick(client):
    BotState.motor5_enabled = True
    BotState.motor5_task_running = True
    BotState.motor5_no_fair_since = datetime.now(UTC) - timedelta(hours=1)

    BotState.motor5_heartbeat(
        experiment_id="m5-f1-test",
        fair_fresh=2,
        book_attempted=2,
        quoted=1,
        skip_no_book=0,
        skip_fee_policy=0,
    )

    assert BotState.motor5_no_fair_since is None
    assert client.get("/ready").status_code == 200


def test_invalid_experiment_breaks_readiness_but_not_liveness(client):
    BotState.motor5_enabled = True
    BotState.motor5_task_running = True
    BotState.motor5_heartbeat(
        experiment_id="m5-f1-test",
        fair_fresh=2,
        book_attempted=2,
        quoted=1,
        skip_no_book=0,
        skip_fee_policy=0,
    )
    BotState.motor5_experiment_invalid = True
    BotState.motor5_experiment_invalid_reason = "intervalo expuesto sin BBO"

    assert client.get("/health").status_code == 200
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["detail"]["checks"]["motor5_experiment_integrity"] is False


def test_all_books_missing_has_grace_then_breaks_readiness_only(client):
    BotState.motor5_enabled = True
    BotState.motor5_task_running = True
    BotState.motor5_heartbeat(
        experiment_id="m5-f1-test",
        fair_fresh=20,
        book_attempted=5,
        quoted=0,
        skip_no_book=5,
        skip_fee_policy=0,
    )

    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200

    BotState.motor5_no_quote_since = datetime.now(UTC) - timedelta(
        seconds=MOTOR5_QUOTE_FLOW_GRACE_SEC + 1
    )
    BotState.motor5_heartbeat(
        experiment_id="m5-f1-test",
        fair_fresh=20,
        book_attempted=5,
        quoted=0,
        skip_no_book=5,
        skip_fee_policy=0,
    )

    assert client.get("/health").status_code == 200
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["detail"]["checks"]["motor5_book_flow"] is False
    assert BotState.current_error() is not None


def test_book_flow_recovers_when_a_quote_is_produced(client):
    BotState.motor5_enabled = True
    BotState.motor5_task_running = True
    BotState.motor5_no_quote_since = datetime.now(UTC) - timedelta(hours=1)
    BotState.motor5_book_flow_blocked = True

    BotState.motor5_heartbeat(
        experiment_id="m5-f1-test",
        fair_fresh=3,
        book_attempted=3,
        quoted=1,
        skip_no_book=2,
        skip_fee_policy=0,
    )

    assert BotState.motor5_no_quote_since is None
    assert BotState.motor5_book_flow_blocked is False
    assert client.get("/ready").status_code == 200
