"""Evidence defects must not manufacture fictitious fills or active reserves."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import m5_bank_bridge as bridge  # noqa: E402
import paper_engine as paper  # noqa: E402
import simulation_bank as bank  # noqa: E402

NOW = "2026-10-09T12:00:00+00:00"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "fictional.sqlite3"
    bank.init_bank(path, initial_capital_usd="100.00")
    return path


def signal(**changes):
    return {
        "schema_version": paper.SCHEMA_VERSION_SIGNAL,
        "signal_id": "boundary",
        "origin": "M2",
        "cycle_id": "offline",
        "ticker": "SIM-BOUNDARY",
        "side": "yes",
        "limit_price_cents": 50,
        "count": 10,
        "signal_at": NOW,
        **changes,
    }


def book(**changes):
    return {
        "ticker": "SIM-BOUNDARY",
        "observed_at": NOW,
        "yes_bids": [[50, 10]],
        "no_bids": [[50, 10]],
        **changes,
    }


@pytest.mark.parametrize("side", ["yes", "no"])
@pytest.mark.parametrize("levels", [[[50, 5], [50, 5]], [[50, 0], [50, 5]]])
def test_duplicate_price_levels_are_ambiguous_not_extra_liquidity(db, side, levels):
    before = bank.get_snapshot(db)
    opposite = "no" if side == "yes" else "yes"
    with pytest.raises(paper.PaperEngineError, match="duplicado"):
        paper.process_signal(db, signal(side=side), book(**{f"{opposite}_bids": levels}))
    assert bank.get_snapshot(db) == before


def test_m5_cannot_be_reported_as_a_taker_fill(db):
    before = bank.get_snapshot(db)
    with pytest.raises(paper.PaperEngineError, match="M5"):
        paper.process_signal(db, signal(origin="M5"), book())
    assert bank.get_snapshot(db) == before


def test_m5_released_reservation_is_not_reported_as_reserved_on_replay(db):
    payload = {
        "schema_version": "botkalshi-m5-fill-input-v1",
        "fill": {
            "id": 1,
            "side": "buy",
            "price_cents": 40,
            "count": 2,
            "fee_effective_cents": 1,
            "fee_model": "maker",
            "ticker": "SIM-BOUNDARY",
            "experiment_id": "boundary",
            "metric_version": "f1-v2-bbo-depth",
            "fee_source": "series",
            "fee_type": "quadratic_with_maker_fees",
            "fee_multiplier": 1,
        },
    }
    first = bridge.apply_m5_fill(db, payload)
    assert first["reservation"]["reserved_usd"] == "0.81"
    bank.release(db, idempotency_key=first["review"]["idempotency_key"])
    before = bank.get_snapshot(db)
    replay = bridge.apply_m5_fill(db, payload)
    assert replay["reservation"] is None
    assert "SimulationBankConflictError" in replay["reservation_error"]
    assert bank.get_snapshot(db) == before
