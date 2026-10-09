"""Contabilidad ficticia pura: no API, Settings, DB, credenciales ni órdenes."""

import ast
from dataclasses import FrozenInstanceError
from fractions import Fraction
from pathlib import Path

import pytest

from src.storage.exit_fills import ExitFillError, summarize_exit_fills


def fill(*, side="yes", **changes):
    row = {
        "fill_id": "fill-a",
        "trade_id": "fill-a",
        "order_id": "order-sell",
        "ticker": "TEST-EXIT",
        "market_ticker": "TEST-EXIT",
        "side": side,
        "action": "sell",
        "outcome_side": "no" if side == "yes" else "yes",
        "book_side": "ask" if side == "yes" else "bid",
        "count_fp": "0.25",
        "yes_price_dollars": "0.600100",
        "no_price_dollars": "0.399900",
        "fee_cost": "0.000123",
        "subaccount_number": 0,
        "exchange_index": 0,
        "is_taker": True,
    }
    row.update(changes)
    return row


def summarize(rows, **changes):
    kwargs = {
        "order_id": "order-sell",
        "ticker": "TEST-EXIT",
        "side": "yes",
        "quantity": Fraction(1, 4),
        "complete": True,
    }
    kwargs.update(changes)
    return summarize_exit_fills(rows, **kwargs)


@pytest.mark.parametrize(
    ("side", "expected"), [("yes", Fraction(6001, 40000)), ("no", Fraction(3999, 40000))]
)
def test_yes_and_no_sell_use_explicit_contract_price_not_outcome(side, expected):
    result = summarize([fill(side=side)], side=side)
    assert result.gross_usd == expected
    assert result.fee_usd == Fraction(123, 1_000_000)  # Fee total, no multiplicada por .25.
    assert result.count == Fraction(1, 4)
    assert result.fill_ids == ("fill-a",)
    assert result.order_id == "order-sell" and result.ticker == "TEST-EXIT"
    assert result.side == side and result.subaccount == 0
    assert len(result.evidence_sha256) == 64
    with pytest.raises(FrozenInstanceError):
        result.count = Fraction(0)


def test_multiple_prices_and_fills_sum_exactly_and_evidence_order_independent():
    a = fill()
    b = fill(
        fill_id="fill-b",
        trade_id="fill-b",
        count_fp="0.75",
        yes_price_dollars="0.700000",
        no_price_dollars="0.300000",
        fee_cost="0.001234",
    )
    result = summarize([a, b], quantity=Fraction(1))
    assert result.gross_usd == Fraction(675025, 1_000_000)
    assert result.fee_usd == Fraction(1357, 1_000_000)
    assert result == summarize([b, a], quantity=Fraction(1))
    assert result == summarize([a, b, dict(reversed(list(a.items())))], quantity=Fraction(1))


def test_fractional_product_below_documented_microdollar_is_rejected_not_rounded():
    row = fill(
        count_fp="0.01", yes_price_dollars="0.000001", no_price_dollars="0.999999", fee_cost="0"
    )
    with pytest.raises(ExitFillError, match="UNSUPPORTED_FILL_REVENUE_PRECISION"):
        summarize([row], quantity=Fraction(1, 100))


def test_exact_microdollar_price_product_and_zero_observed_fee():
    row = fill(
        count_fp="0.01", yes_price_dollars="0.000100", no_price_dollars="0.999900", fee_cost="0"
    )
    result = summarize([row], quantity=Fraction(1, 100))
    assert result.gross_usd == Fraction(1, 1_000_000)
    assert result.fee_usd == 0


def test_zero_terminal_fill_requires_complete_empty_collection():
    assert summarize([], quantity=Fraction(0)).count == 0
    with pytest.raises(ExitFillError, match="TERMINAL_FILL_QUANTITY_MISMATCH"):
        summarize([], quantity=Fraction(1, 4))
    with pytest.raises(ExitFillError, match="TERMINAL_FILL_QUANTITY_MISMATCH"):
        summarize([fill()], quantity=Fraction(0))


@pytest.mark.parametrize("complete", [False, None, 1, "true", [], {}])
def test_collection_must_be_explicitly_complete(complete):
    with pytest.raises(ExitFillError, match="INCOMPLETE_FILL_COLLECTION"):
        summarize([fill()], complete=complete)


def test_default_is_incomplete_not_silent_attestation():
    with pytest.raises(ExitFillError, match="INCOMPLETE_FILL_COLLECTION"):
        summarize_exit_fills(
            [fill()], order_id="order-sell", ticker="TEST-EXIT", side="yes", quantity=Fraction(1, 4)
        )


@pytest.mark.parametrize(
    "field",
    [
        "fill_id",
        "order_id",
        "ticker",
        "count_fp",
        "fee_cost",
        "subaccount_number",
        "yes_price_dollars",
    ],
)
def test_required_evidence_missing_fails_closed(field):
    row = fill()
    del row[field]
    with pytest.raises(ExitFillError):
        summarize([row])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fill_id", ""),
        ("trade_id", "another-fill"),
        ("order_id", "another-order"),
        ("ticker", "OTHER"),
        ("market_ticker", "OTHER"),
        ("action", "buy"),
        ("side", "no"),
        ("outcome_side", "yes"),
        ("book_side", "bid"),
        ("subaccount_number", 1),
        ("subaccount_number", None),
        ("subaccount_number", False),
        ("subaccount_number", "0"),
        ("exchange_index", -1),
        ("exchange_index", False),
        ("count", 1),
        ("count", True),
        ("yes_price", 60),
        ("no_price", 40),
    ],
)
def test_identity_alias_or_direction_contradiction_rejects(field, value):
    with pytest.raises(ExitFillError):
        summarize([fill(**{field: value})])


@pytest.mark.parametrize("field", ["count_fp", "yes_price_dollars", "fee_cost"])
@pytest.mark.parametrize(
    "value", [None, True, 0.25, 1, "NaN", "Infinity", "-0.01", "1e-2", " 0.25", "٠.٢٥", "0.0000001"]
)
def test_non_decimal_or_excess_precision_is_unknown_not_zero(field, value):
    with pytest.raises(ExitFillError):
        summarize([fill(**{field: value})])


@pytest.mark.parametrize("count", ["0", "0.00", "0.251", "1000000001"])
def test_fill_count_bounds(count):
    with pytest.raises(ExitFillError):
        summarize([fill(count_fp=count)])


def test_prices_are_bounded_and_complementary_and_fee_is_bounded_by_payout():
    for changes in (
        {"yes_price_dollars": "1.01"},
        {"no_price_dollars": "0.600100"},
        {"fee_cost": "0.250001"},
    ):
        with pytest.raises(ExitFillError):
            summarize([fill(**changes)])


@pytest.mark.parametrize("direction", ["canonical", "book_only", "outcome_only", "legacy"])
def test_supported_direction_schemas_are_checked_against_sell_intent(direction):
    row = fill()
    removed = {
        "canonical": ("side", "action"),
        "book_only": ("side", "action", "outcome_side"),
        "outcome_only": ("side", "action", "book_side"),
        "legacy": ("outcome_side", "book_side"),
    }[direction]
    for key in removed:
        del row[key]
    assert summarize([row]).count == Fraction(1, 4)


def test_no_direction_and_incomplete_legacy_are_rejected():
    for keep_side in (False, True):
        row = fill()
        for key in ("action", "outcome_side", "book_side"):
            del row[key]
        if not keep_side:
            del row["side"]
        with pytest.raises(ExitFillError, match="MISSING_FILL_DIRECTION"):
            summarize([row])


def test_explicit_side_price_required_even_when_complement_and_order_cost_exist():
    row = fill(fill_cost_usd_fp="0.150025", maker_fill_cost_dollars="0.150025")
    del row["yes_price_dollars"]
    with pytest.raises(ExitFillError, match="INVALID_FILL_PRICE"):
        summarize([row])


@pytest.mark.parametrize(
    "field,value",
    [("fee_cost", "0.000124"), ("created_time", "2026-10-08T00:00:00Z"), ("is_taker", False)],
)
def test_repeated_id_with_any_record_difference_is_not_deduplicated(field, value):
    with pytest.raises(ExitFillError, match="CONFLICTING_DUPLICATE_FILL"):
        summarize([fill(), fill(**{field: value})])


def test_fill_ids_not_trade_ids_are_required_and_aliases_cannot_rename_duplicates():
    row = fill()
    del row["fill_id"]
    with pytest.raises(ExitFillError, match="INVALID_IDENTITY"):
        summarize([row])
    with pytest.raises(ExitFillError, match="FILL_ID_ALIAS_MISMATCH"):
        summarize([fill(), fill(fill_id="fill-b")], quantity=Fraction(1, 2))


def test_collection_bounds_type_and_non_json_records_fail_closed():
    for rows in (None, {}, "rows", iter([fill()]), [None], [fill()] * 10_001):
        with pytest.raises(ExitFillError):
            summarize(rows)
    for extra in (float("nan"), object(), "x" * 16_384):
        with pytest.raises(ExitFillError):
            summarize([fill(extra=extra)])


@pytest.mark.parametrize("quantity", [True, -1, 0.25, "0.25", Fraction(1, 1000), 1_000_000_001])
def test_expected_quantity_must_be_exact_supported_and_nonnegative(quantity):
    with pytest.raises(ExitFillError, match="INVALID_TERMINAL_QUANTITY"):
        summarize([fill()], quantity=quantity)


def test_explicit_subaccount_and_single_exchange_shard():
    assert summarize([fill(subaccount_number=3)], subaccount=3).subaccount == 3
    for account in (-1, 64, False, "0"):
        with pytest.raises(ExitFillError, match="INVALID_SUBACCOUNT"):
            summarize([fill()], subaccount=account)
    with pytest.raises(ExitFillError, match="FILL_EXCHANGE_MISMATCH"):
        summarize(
            [fill(), fill(fill_id="fill-b", trade_id="fill-b", exchange_index=1)],
            quantity=Fraction(1, 2),
        )


def test_large_complete_collection_is_supported_and_inputs_unchanged():
    rows = [fill(fill_id=f"f{i}", trade_id=f"f{i}") for i in range(200)]
    before = [dict(row) for row in rows]
    result = summarize(rows, quantity=Fraction(50))
    assert result.count == 50 and len(result.fill_ids) == 200
    assert rows == before


def test_errors_do_not_echo_payload_and_evidence_hash_tracks_economics():
    secret = "DO-NOT-ECHO-FICTITIOUS-TOKEN"
    with pytest.raises(ExitFillError) as error:
        summarize([fill(fee_cost=secret)])
    assert secret not in str(error.value)
    assert summarize([fill()]).evidence_sha256 != summarize([fill(fee_cost="0")]).evidence_sha256


def test_module_has_only_stdlib_imports():
    module = Path(__file__).resolve().parents[2] / "src/storage/exit_fills.py"
    tree = ast.parse(module.read_text())
    permitted = {"collections", "dataclasses", "fractions", "hashlib", "json", "re", "typing"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] in permitted for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.module.split(".")[0] in permitted
