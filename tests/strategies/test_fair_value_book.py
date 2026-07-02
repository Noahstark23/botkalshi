"""FairValueBook — canal Motor 2 → Motor 5 con TTL (plan Motor 5 §1.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.strategies.fair_value_book import FairValueBook

NOW = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_book():
    FairValueBook.clear()
    yield
    FairValueBook.clear()


def test_publish_and_fresh_within_ttl():
    FairValueBook.publish({"T-A": 0.62, "T-B": 0.38}, now=NOW)
    fresh = FairValueBook.fresh(600, now=NOW + timedelta(seconds=300))
    assert set(fresh) == {"T-A", "T-B"}
    assert fresh["T-A"].fair_prob == 0.62


def test_stale_entries_expire_and_are_purged():
    FairValueBook.publish({"T-A": 0.62}, now=NOW)
    assert FairValueBook.fresh(600, now=NOW + timedelta(seconds=601)) == {}
    assert FairValueBook.size() == 0  # purga: el book no crece sin tope


def test_republish_refreshes_timestamp():
    FairValueBook.publish({"T-A": 0.62}, now=NOW)
    FairValueBook.publish({"T-A": 0.65}, now=NOW + timedelta(seconds=500))
    fresh = FairValueBook.fresh(600, now=NOW + timedelta(seconds=900))
    assert fresh["T-A"].fair_prob == 0.65


def test_absent_ticker_keeps_last_fair_until_ttl():
    """Un ciclo que no matchea el partido (odds API parcial) NO borra su fair: expira por
    TTL, no por ausencia."""
    FairValueBook.publish({"T-A": 0.62, "T-B": 0.38}, now=NOW)
    FairValueBook.publish({"T-A": 0.63}, now=NOW + timedelta(seconds=300))
    fresh = FairValueBook.fresh(600, now=NOW + timedelta(seconds=500))
    assert set(fresh) == {"T-A", "T-B"}
