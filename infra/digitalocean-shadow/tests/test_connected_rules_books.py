"""Rechazos conectados reproducidos offline; positivos de fees son sintéticos."""

import copy
import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import connected_book_review as books  # noqa: E402
import football_settlement_review as rules  # noqa: E402
import paper_stream  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "connected_rules_fixture", Path(__file__).with_name("test_football_replay_contract.py")
)
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)

T0 = "2026-10-10T12:00:00Z"
T1 = "2026-10-10T12:00:01Z"


def book():
    return {
        "orderbook_fp": {
            "yes_dollars": [["0.7600", "31374.47"]],
            "no_dollars": [["0.2300", "25828.43"]],
        }
    }


def review(raw, at=T1):
    return books.inspect_book(raw, received_at=T0, evaluated_at=at)


def reviewed_fixture(monkeypatch):
    payload, series, odds = fixtures.fixture("nfl")
    reviewed = {
        m["ticker"]: hashlib.sha256(
            (m["rules_primary"] + "\n" + m["rules_secondary"]).encode()
        ).hexdigest()
        for m in payload["markets"]
    }
    monkeypatch.setattr(rules, "REVIEWED_RULE_HASHES", reviewed)
    odds["bookmakers"] = [{"key": key} for key in ["fanatics", "fanduel", "draftkings", "unknown"]]
    return payload, series, odds


def test_reviewed_sources_explain_differences_but_never_certify_quote_jurisdiction(monkeypatch):
    payload, _, odds = reviewed_fixture(monkeypatch)
    result = rules.review_capture_rules(payload, odds)
    assert result["kalshi_exact_text_reviewed"]
    for row in result["bookmakers"][:3]:
        assert row["review_status"] == "DIFFERENT"
        assert "draw" in row["differences"]
        assert "QUOTE_RULE_JURISDICTION_UNVERIFIED" in row["blockers"]
        assert not row["fair_or_signal_authorized"]
    assert result["bookmakers"][3]["review_status"] == "UNREVIEWED"
    assert not result["full_contract_equivalence_verified"]


@pytest.mark.parametrize("change", ["text", "new_event", "duplicate", "wrong_pair"])
def test_review_binding_rejects_text_drift_new_dates_and_wrong_pairs(monkeypatch, change):
    payload, _, odds = reviewed_fixture(monkeypatch)
    if change == "text":
        payload["markets"][0]["rules_secondary"] += " Changed"
    elif change == "new_event":
        payload["event"]["event_ticker"] = "KXNFLGAME-26OCT18PHIJAC"
    elif change == "duplicate":
        payload["markets"][1] = copy.deepcopy(payload["markets"][0])
    else:
        payload["markets"][1]["ticker"] = "KXNCAAFGAME-26OCT10UGAALA-ALA"
    result = rules.review_capture_rules(payload, odds)
    assert not result["kalshi_exact_text_reviewed"]
    assert "KALSHI_RULE_TEXT_NOT_REVIEWED" in result["blockers"]
    assert all(r["review_status"] == "UNREVIEWED" for r in result["bookmakers"])


def test_empty_quotes_and_duplicate_bookmaker_never_create_equivalence(monkeypatch):
    payload, _, odds = reviewed_fixture(monkeypatch)
    odds["bookmakers"] = []
    assert "NO_SPORTSBOOK_QUOTES" in rules.review_capture_rules(payload, odds)["blockers"]
    odds["bookmakers"] = [{"key": "fanatics"}] * 2
    with pytest.raises(ValueError, match="DUPLICATE"):
        rules.review_capture_rules(payload, odds)


@pytest.mark.parametrize("league", ["nfl", "ncaaf"])
def test_full_reference_terms_close_overtime_and_draw_gaps_without_certifying_feed(
    monkeypatch, league
):
    payload, _, odds = fixtures.fixture(league)
    monkeypatch.setattr(
        rules,
        "REVIEWED_RULE_HASHES",
        {
            m["ticker"]: hashlib.sha256(
                (m["rules_primary"] + "\n" + m["rules_secondary"]).encode()
            ).hexdigest()
            for m in payload["markets"]
        },
    )
    odds["bookmakers"] = [{"key": "fanatics"}, {"key": "fanduel"}]
    report = rules.review_capture_rules(payload, odds)
    for row in report["bookmakers"]:
        assert row["dimensions_compared"] == 9
        assert "overtime" not in row["missing_dimensions"] + row["differences"]
        assert "draw" in row["differences"]
        assert "cancellation" in row["differences"]
        assert row["reviewed_league"] == league
        assert len(row["source_sha256"]) == 64
    assert not report["reference_terms_version_at_capture_verified"]
    assert "QUOTE_RULE_VERSION_UNVERIFIED" in report["blockers"]
    assert not report["fair_or_signal_authorized"]


def test_fixed_point_depth_is_not_truncated_and_no_fill_is_inferred():
    result = review(book())
    assert result["quote_data_usable"]
    assert result["yes_bid_cents"] == "76.0000"
    assert result["yes_ask_cents"] == "77.0000"
    assert result["bid_depth_contracts"] == "31374.47"
    assert result["ask_depth_contracts"] == "25828.43"
    assert result["fills_proven"] == 0
    assert not result["exchange_sequence_verified"]


@pytest.mark.parametrize("league,reviewed_count", [("nfl", 9), ("ncaaf", 8)])
def test_expanded_book_reviews_preserve_scope_provenance_and_disabled_fair(
    monkeypatch, league, reviewed_count
):
    payload, _, odds = fixtures.fixture(league)
    monkeypatch.setattr(
        rules,
        "REVIEWED_RULE_HASHES",
        {
            m["ticker"]: hashlib.sha256(
                (m["rules_primary"] + "\n" + m["rules_secondary"]).encode()
            ).hexdigest()
            for m in payload["markets"]
        },
    )
    odds["bookmakers"] = [
        {"key": key}
        for key in (
            "draftkings",
            "williamhill_us",
            "fanduel",
            "betmgm",
            "bovada",
            "betus",
            "mybookieag",
            "betonlineag",
            "lowvig",
            "betrivers",
            "fanatics",
        )
    ]
    report = rules.review_capture_rules(payload, odds)
    rows = {r["bookmaker"]: r for r in report["bookmakers"]}
    assert sum(r["review_status"] != "UNREVIEWED" for r in rows.values()) == reviewed_count
    for row in rows.values():
        assert not row["fair_or_signal_authorized"] and not row["execution_authorized"]
        if row["review_status"] != "UNREVIEWED":
            assert "QUOTE_RULE_JURISDICTION_UNVERIFIED" in row["blockers"]
    for key in ("lowvig", "betrivers"):
        assert rows[key]["source_url"]
        assert rows[key]["source_sha256"] is None
        assert not rows[key]["source_bytes_archived"]
    for key in ("mybookieag", "williamhill_us"):
        assert rows[key]["source_research_gap"]["reference_url"]
        assert rows[key]["review_status"] == "UNREVIEWED"
    assert "QUOTE_RULE_VERSION_UNVERIFIED" in report["blockers"]
    if league == "ncaaf":
        assert rows["betmgm"]["source_research_gap"]["reason"].startswith("NFL_HELP")


@pytest.mark.parametrize("key", ["betonlineag", "lowvig", "betus", "bovada"])
@pytest.mark.parametrize("league", ["nfl", "ncaaf"])
def test_unreviewed_moneyline_draw_never_inherits_teaser_prop_or_australian_rules(key, league):
    profile = rules.SPORTSBOOK_PROFILES[(key, league)]
    result = rules.compare_rules(profile, profile, quote_jurisdiction=profile.jurisdiction)
    assert "draw" in result["missing_dimensions"]
    assert not result["fair_or_signal_authorized"]
    if key in ("betonlineag", "lowvig", "betus"):
        assert profile.postponement == "scheduled_date_and_original_venue_required"
        assert "48" not in profile.postponement


def test_duration_and_ncaa_shortening_exceptions_are_not_erased():
    assert rules.BOVADA_FOOTBALL.suspension == "official_after_50min"
    assert "55min" in rules.BETUS_FOOTBALL.suspension
    assert "less_than_5min" in rules.BETRIVERS_NY.suspension
    assert "shortened_ncaa" not in rules.BETRIVERS_NY.suspension
    assert "shortened_ncaa" in rules.BETRIVERS_NY_NCAAF.suspension
    assert "unspecified" in rules.BOVADA_FOOTBALL.postponement
    assert ("betmgm", "ncaaf") not in rules.SPORTSBOOK_PROFILES


@pytest.mark.parametrize("at", ["2026-10-10T11:59:59Z", "2026-10-10T12:00:11Z"])
def test_old_or_future_receipt_cannot_be_used(at):
    assert "BOOK_RECEIPT_STALE_OR_FUTURE" in review(book(), at)["blockers"]


@pytest.mark.parametrize("side", ["yes_dollars", "no_dollars"])
def test_one_sided_book_remains_one_sided(side):
    raw = book()
    raw["orderbook_fp"][side] = []
    assert "ORDERBOOK_NOT_BILATERAL" in review(raw)["blockers"]


def test_subcent_is_preserved_but_not_rounded_into_a_supported_price():
    raw = book()
    raw["orderbook_fp"]["yes_dollars"][0][0] = "0.7610"
    result = review(raw)
    assert result["yes_bid_cents"] == "76.1000"
    assert "SUBCENT_PRICE_UNSUPPORTED_BY_SIMULATOR" in result["blockers"]


@pytest.mark.parametrize("price", ["0.7700", "0.7800"])
def test_locked_or_crossed_books_reject(price):
    raw = book()
    raw["orderbook_fp"]["yes_dollars"][0][0] = price
    assert "ORDERBOOK_LOCKED_OR_CROSSED" in review(raw)["blockers"]


@pytest.mark.parametrize("bad", [True, 0.76, "NaN", "Infinity", "-1", "0", "1"])
def test_invalid_fixed_point_prices_raise(bad):
    raw = book()
    raw["orderbook_fp"]["yes_dollars"][0][0] = bad
    with pytest.raises(ValueError):
        review(raw)


@pytest.mark.parametrize("bad", [True, 1, "NaN", "Infinity", "-1", "0"])
def test_invalid_depth_raises(bad):
    raw = book()
    raw["orderbook_fp"]["yes_dollars"][0][1] = bad
    with pytest.raises(ValueError):
        review(raw)


def test_duplicate_levels_and_ambiguous_shapes_raise():
    raw = book()
    raw["orderbook_fp"]["yes_dollars"] *= 2
    with pytest.raises(ValueError, match="CONFLICT"):
        review(raw)
    for raw in [{}, {**book(), "orderbook": {}}, {"orderbook_fp": {}}]:
        with pytest.raises(ValueError):
            review(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "league,reason", [("nfl", "fee_type base no soportado"), ("ncaaf", "serie sin aserción")]
)
async def test_actual_m5_fee_policy_rejects_demo_metadata_without_starting_engine(league, reason):
    payload, series, _ = fixtures.fixture(league)
    result = await books.probe_m5_fee_policy(payload, series)
    assert result["status"] == "REJECTED"
    assert reason in result["details"]["reason"]
    assert result["network_calls"] == 0
    assert not result["engine_started"]


@pytest.mark.asyncio
async def test_synthetic_supported_fee_with_event_override_exercises_actual_policy():
    payload, series, _ = fixtures.fixture("nfl")
    series["fee_type"] = "quadratic_with_maker_fees"
    payload["event"].update(
        fee_type_override="quadratic_with_maker_fees", fee_multiplier_override="0.5"
    )
    result = await books.probe_m5_fee_policy(payload, series)
    assert result["status"] == "METADATA_ACCEPTED"
    assert result["details"]["multiplier"] == "1/2"
    assert result["cached_metadata_reads"] == ["series", "event"]
    assert not result["execution_authorized"]


def test_connected_stream_records_rules_and_books_without_reservations_on_restart(tmp_path):
    payload, series, odds = fixtures.fixture("nfl")
    odds["bookmakers"] = []
    event = {
        "type": "football_snapshot",
        "payload": payload,
        "series": series,
        "milestones": payload["milestones"],
        "odds": odds,
        "observed_at": T0,
        "evaluated_at": T1,
        "partial_universe": True,
        "source_hashes": ["a" * 64],
        "orderbooks": {
            m["ticker"]: {"payload": book(), "received_at": T0} for m in payload["markets"]
        },
    }
    db = tmp_path / "capture.db"
    result = paper_stream.run_cycle(db, "100", "capture", "CONNECTED_CAPTURE", [event])
    report = result["captures"][0]
    assert report["status"] == "BLOCKED" and report["signals"] == report["fills"] == 0
    assert "KALSHI_RULE_TEXT_NOT_REVIEWED" in report["blockers"]
    assert len(report["orderbooks"]) == 2
    assert result["bank"]["reserved_usd"] == "0.00"
    again = paper_stream.run_cycle(db, "100", "capture", "CONNECTED_CAPTURE", [event])
    assert again["processed_this_cycle"] == 0
    assert again["bank"] == result["bank"]
    bad = copy.deepcopy(event)
    bad["orderbooks"]["WRONG"] = bad["orderbooks"].pop(next(iter(bad["orderbooks"])))
    with pytest.raises(paper_stream.StreamError, match="TICKER_SCOPE"):
        paper_stream.run_cycle(tmp_path / "wrong.db", "100", "bad", "CONNECTED_CAPTURE", [bad])
