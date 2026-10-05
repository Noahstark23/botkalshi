"""Regresiones de incertidumbre con red falsa y SQLite temporal, nunca una cuenta."""

import asyncio
from unittest.mock import patch

import pytest
from sqlmodel import select

from src.clients.kalshi_rest import KalshiOrderConfirmationError
from src.storage.models import Trade, get_session
from src.strategies.motor_5_mm.executor import LiveOrder, Motor5Executor
from src.strategies.motor_5_mm.order_state import COUNT_QUARANTINE_NOTE
from src.strategies.motor_5_mm.reconciler import MMReconciler


def _pending(coid="m5mm-safe", oid="K1"):
    with get_session() as session:
        session.add(
            Trade(
                client_order_id=coid,
                kalshi_order_id=oid,
                ticker="T",
                side="yes",
                action="buy",
                count=10,
                price_cents=47,
                strategy="motor_5_mm",
                status="pending",
            )
        )
        session.commit()


def _row():
    with get_session() as session:
        return session.exec(select(Trade)).one()


def _order(**changes):
    result = {
        "client_order_id": "m5mm-safe",
        "order_id": "K1",
        "ticker": "T",
        "status": "canceled",
        "fill_count_fp": "0.00",
    }
    result.update(changes)
    return result


class _Client:
    def __init__(self, orders=None, error=None, cancel_response=None):
        self.orders = orders or []
        self.error = error
        self.cancel_response = cancel_response
        self.cancelled = []

    async def get_orders(self, **kwargs):
        if self.error:
            raise self.error
        return {"orders": self.orders, "cursor": ""}

    async def get_all_orders(self, **kwargs):
        if self.error:
            raise self.error
        return self.orders

    async def cancel_order(self, oid):
        self.cancelled.append(oid)
        if isinstance(self.cancel_response, BaseException):
            raise self.cancel_response
        return self.cancel_response


@pytest.mark.asyncio
async def test_failed_read_is_explicitly_incomplete_and_preserves_pending():
    _pending()
    report = await MMReconciler(_Client(error=TimeoutError())).reconcile()
    assert report.complete is False
    assert report.discrepancies == 1
    assert _row().status == "pending"


@pytest.mark.asyncio
async def test_reconciliation_uses_complete_order_collection():
    _pending()
    client = _Client([_order()])

    async def first_page_only(**kwargs):
        return {"orders": [], "cursor": "next-page"}

    client.get_orders = first_page_only
    report = await MMReconciler(client).reconcile()
    assert report.resolved_cancelled == 1
    assert report.complete


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"fill_count_fp": None},
        {"fill_count_fp": "0.005"},
        {"fill_count_fp": "NaN"},
        {"fill_count_fp": "11.00"},
        {"fill_count_fp": "3.00", "fill_count": "0"},
        {"order_id": "OTHER"},
        {"ticker": "OTHER"},
        {"status": "executed", "fill_count_fp": "0.00"},
    ],
)
async def test_unproven_terminal_order_never_releases_pending(changes):
    _pending()
    report = await MMReconciler(_Client([_order(**changes)])).reconcile()
    assert _row().status == "pending"
    assert report.discrepancies == 1
    assert "T" in report.corrupted_tickers


@pytest.mark.asyncio
async def test_fp_partial_cancel_records_exact_fill():
    _pending()
    report = await MMReconciler(_Client([_order(fill_count_fp="3.00")])).reconcile()
    assert report.resolved_filled == 1
    assert _row().filled_count == 3


@pytest.mark.asyncio
async def test_duplicate_identity_fails_before_any_mutation():
    _pending()
    client = _Client([_order(), _order(status="resting")])
    report = await MMReconciler(client).reconcile(live_coids=set())
    assert _row().status == "pending"
    assert not report.complete
    assert client.cancelled == []


@pytest.mark.asyncio
async def test_database_failure_is_not_a_resolved_order():
    _pending()
    reconciler = MMReconciler(_Client([_order()]))
    with (
        patch.object(reconciler, "_update", return_value=False),
        pytest.raises(RuntimeError, match="evidencia terminal"),
    ):
        await reconciler.reconcile()
    assert _row().status == "pending"


@pytest.mark.asyncio
async def test_orphan_ack_without_terminal_evidence_preserves_pending():
    _pending()
    client = _Client([_order(status="resting")], cancel_response={"order": {"order_id": "K1"}})
    report = await MMReconciler(client).reconcile(live_coids=set())
    assert report.orphan_cancelled == 0
    assert _row().status == "pending"
    assert not report.complete


@pytest.mark.asyncio
async def test_phantom_failed_cancel_is_not_reported_as_cancelled():
    client = _Client([_order(status="resting")], cancel_response=TimeoutError())
    report = await MMReconciler(client).reconcile(live_coids=set())
    assert report.phantom_cancelled == 0
    assert not report.complete
    assert "T" in report.corrupted_tickers


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_fill", ["0.00", "3.00"])
async def test_orphan_cancel_cannot_erase_fill_already_seen_in_snapshot(cancel_fill):
    _pending()
    client = _Client(
        [_order(status="resting", fill_count_fp="4.00")],
        cancel_response={"order": _order(fill_count_fp=cancel_fill)},
    )
    report = await MMReconciler(client).reconcile(live_coids=set())
    assert not report.complete
    assert report.orphan_cancelled == report.resolved_cancelled == 0
    assert _row().status == "pending"
    assert _row().filled_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_fill", ["4.00", "5.00"])
async def test_orphan_cancel_accepts_monotone_snapshot_fill(cancel_fill):
    _pending()
    client = _Client(
        [_order(status="resting", fill_count_fp="4.00")],
        cancel_response={"order": _order(fill_count_fp=cancel_fill)},
    )
    report = await MMReconciler(client).reconcile(live_coids=set())
    assert report.complete
    assert report.orphan_cancelled == report.resolved_filled == 1
    assert _row().filled_count == int(float(cancel_fill))
    assert COUNT_QUARANTINE_NOTE not in (_row().notes or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("fill", [None, "0.005", "11.00"])
async def test_orphan_invalid_snapshot_never_resolves(fill):
    _pending()
    client = _Client(
        [_order(status="resting", fill_count_fp=fill)], cancel_response={"order": _order()}
    )
    report = await MMReconciler(client).reconcile(live_coids=set())
    assert not report.complete
    assert _row().status == "pending"
    assert client.cancelled == []


def _executor_with_pending(client):
    executor = Motor5Executor(client)
    executor._live = {"T": {"buy": LiveOrder("m5mm-safe", "K1", "buy", 47, 10)}}
    return executor


@pytest.mark.asyncio
async def test_managed_fill_is_persisted_before_executor_retire():
    _pending()
    client = _Client(
        [_order(status="resting", fill_count_fp="4.00")], cancel_response={"order": _order()}
    )
    executor = _executor_with_pending(client)
    report = await MMReconciler(client).reconcile(live_coids=executor.managed_coids())
    executor.corrupted.update(report.corrupted_tickers)
    await executor.retire_ticker("T")
    assert report.complete
    assert _row().status == "pending" and _row().filled_count == 4
    assert executor.live_coids() == {"m5mm-safe"}
    assert executor._mm_open_cost_usd() == 4.70  # reserva completa, no sólo 4 contratos


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [None, "0.005", "11.00"])
async def test_unsupported_snapshot_stays_quarantined_after_cancel_and_restart(invalid):
    _pending()
    with get_session() as session:
        row = session.exec(select(Trade)).one()
        row.notes = "x" * 500
        session.add(row)
        session.commit()
    client = _Client(
        [_order(status="resting", fill_count_fp=invalid)], cancel_response={"order": _order()}
    )
    executor = _executor_with_pending(client)
    report = await MMReconciler(client).reconcile(live_coids=executor.managed_coids())
    await executor.retire_ticker("T")
    assert not report.complete
    assert _row().notes.startswith(COUNT_QUARANTINE_NOTE)
    assert len(_row().notes) == 500
    assert _row().status == "pending"
    assert executor.live_coids() == {"m5mm-safe"}
    # Nuevas instancias: el bloqueo no dependía de las colecciones en memoria.
    client.orders = [_order()]
    restarted_report = await MMReconciler(client).reconcile(live_coids=set())
    restarted_executor = _executor_with_pending(client)
    assert not restarted_report.complete
    assert await restarted_executor.cancel_all("test restart") == 0
    assert _row().status == "pending"
    # La cuarentena puede contener más fills que count: reservar sólo $4.70
    # permitiría a otro motor utilizar un headroom que no está demostrado.
    assert restarted_executor._mm_open_cost_usd() == float("inf")


@pytest.mark.asyncio
async def test_failed_quarantine_write_aborts_before_further_accounting():
    _pending()
    client = _Client(
        [_order(status="resting", fill_count_fp="0.005")], cancel_response={"order": _order()}
    )
    reconciler = MMReconciler(client)
    with (
        patch.object(reconciler, "_update", return_value=False),
        pytest.raises(RuntimeError, match="cuarentena"),
    ):
        await reconciler.reconcile(live_coids={"m5mm-safe"})
    assert client.cancelled == []
    assert _row().status == "pending"


@pytest.mark.asyncio
async def test_failed_fill_floor_write_aborts_before_cancel_can_erase_it():
    _pending()
    client = _Client(
        [_order(status="resting", fill_count_fp="4.00")], cancel_response={"order": _order()}
    )
    reconciler = MMReconciler(client)
    with (
        patch.object(reconciler, "_update", return_value=False),
        pytest.raises(RuntimeError, match="evidencia resting"),
    ):
        await reconciler.reconcile(live_coids={"m5mm-safe"})
    assert client.cancelled == []
    assert _row().status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [None, "0.005", "11.00"])
@pytest.mark.parametrize("via_orphan", [False, True])
async def test_terminal_invalid_count_survives_later_zero_and_restart(invalid, via_orphan):
    _pending()
    client = _Client(
        [_order(status="resting") if via_orphan else _order(fill_count_fp=invalid)],
        cancel_response={"order": _order(fill_count_fp=invalid)},
    )
    first = await MMReconciler(client).reconcile(live_coids=set())
    assert not first.complete
    assert _row().status == "pending"
    assert COUNT_QUARANTINE_NOTE in (_row().notes or "")

    # Nueva instancia y API aparentemente limpia: no reemplaza evidencia previa.
    client.orders = [_order()]
    second = await MMReconciler(client).reconcile(live_coids=set())
    assert not second.complete
    assert second.terminal_coids == set()
    assert _row().status == "pending"
    assert COUNT_QUARANTINE_NOTE in (_row().notes or "")
    assert _executor_with_pending(client)._mm_open_cost_usd() == float("inf")


@pytest.mark.asyncio
async def test_terminal_quarantine_write_failure_aborts_before_next_order():
    _pending()
    _pending(coid="m5mm-next", oid="K2")
    client = _Client(
        [
            _order(fill_count_fp="0.005"),
            _order(client_order_id="m5mm-next", order_id="K2", status="resting"),
        ],
        cancel_response={"order": _order(client_order_id="m5mm-next", order_id="K2")},
    )
    reconciler = MMReconciler(client)
    with (
        patch.object(reconciler, "_update", return_value=False),
        pytest.raises(RuntimeError, match="cuarentena"),
    ):
        await reconciler.reconcile(live_coids=set())
    assert client.cancelled == []
    with get_session() as session:
        assert all(row.status == "pending" for row in session.exec(select(Trade)))


@pytest.mark.asyncio
async def test_terminal_contradictory_legacy_count_stays_quarantined_after_zero():
    _pending()
    client = _Client([_order(fill_count_fp="3.00", fill_count=0)])
    first = await MMReconciler(client).reconcile(live_coids=set())
    assert not first.complete
    assert COUNT_QUARANTINE_NOTE in (_row().notes or "")
    client.orders = [_order()]
    second = await MMReconciler(client).reconcile(live_coids=set())
    assert not second.complete and _row().status == "pending"


@pytest.mark.asyncio
async def test_terminal_omitted_count_is_incomplete_but_can_be_retried():
    _pending()
    missing = _order()
    del missing["fill_count_fp"]
    client = _Client([missing])
    first = await MMReconciler(client).reconcile(live_coids=set())
    assert not first.complete
    assert _row().status == "pending"
    assert COUNT_QUARANTINE_NOTE not in (_row().notes or "")
    client.orders = [_order()]
    second = await MMReconciler(client).reconcile(live_coids=set())
    assert second.complete and _row().status == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["0.005", "11.00"])
async def test_orphan_confirmation_exception_retains_unrepresentable_fill(invalid):
    _pending()
    error = KalshiOrderConfirmationError("unconfirmed cancel")
    error.observed_order = _order(fill_count_fp=invalid)
    client = _Client([_order(status="resting")], cancel_response=error)
    first = await MMReconciler(client).reconcile(live_coids=set())
    assert not first.complete
    assert first.terminal_coids == set() and first.orphan_cancelled == 0
    assert COUNT_QUARANTINE_NOTE in (_row().notes or "")
    client.orders = [_order()]
    second = await MMReconciler(client).reconcile(live_coids=set())
    assert not second.complete
    assert second.terminal_coids == set() and _row().status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [{"order_id": "WRONG"}, {"client_order_id": "m5mm-other"}, {"fill_count_fp": "0.00"}],
)
async def test_orphan_exception_never_confirms_or_quarantines_unrelated_evidence(changes):
    _pending()
    error = KalshiOrderConfirmationError("unconfirmed cancel")
    error.observed_order = _order(fill_count_fp="11.00") | changes
    client = _Client([_order(status="resting")], cancel_response=error)
    report = await MMReconciler(client).reconcile(live_coids=set())
    assert not report.complete
    assert report.terminal_coids == set() and report.orphan_cancelled == 0
    assert _row().status == "pending"
    assert COUNT_QUARANTINE_NOTE not in (_row().notes or "")


@pytest.mark.asyncio
async def test_orphan_error_retains_highest_observed_fill_across_restart():
    _pending()
    before = _order(status="resting", fill_count_fp="4.00")
    after = _order()
    error = KalshiOrderConfirmationError("cancel returned contradictory fill")
    error.observed_order = after
    error.observed_orders = [before, after]
    client = _Client([_order(status="resting")], cancel_response=error)
    first = await MMReconciler(client).reconcile(live_coids=set())
    assert not first.complete and first.terminal_coids == set()
    assert _row().status == "pending" and _row().filled_count == 4
    assert COUNT_QUARANTINE_NOTE not in (_row().notes or "")
    client.orders = [_order()]
    second = await MMReconciler(client).reconcile(live_coids=set())
    assert not second.complete and _row().status == "pending"
    assert _row().filled_count == 4
    client.orders = [_order(fill_count_fp="4.00")]
    third = await MMReconciler(client).reconcile(live_coids=set())
    assert third.complete and third.resolved_filled == 1
    assert _row().status == "filled" and _row().filled_count == 4


@pytest.mark.asyncio
async def test_orphan_legacy_exception_retains_observed_fill_floor():
    _pending()
    error = KalshiOrderConfirmationError("unconfirmed")
    error.observed_order = _order(fill_count_fp="4.00")
    client = _Client([_order(status="resting")], cancel_response=error)
    report = await MMReconciler(client).reconcile(live_coids=set())
    assert not report.complete and report.terminal_coids == set()
    assert _row().status == "pending" and _row().filled_count == 4


@pytest.mark.asyncio
async def test_orphan_observation_floor_write_failure_aborts():
    _pending()
    error = KalshiOrderConfirmationError("unconfirmed")
    error.observed_orders = [_order(fill_count_fp="4.00"), _order()]
    client = _Client([_order(status="resting")], cancel_response=error)
    reconciler = MMReconciler(client)
    real_update = reconciler._update

    def fail_floor(coid, **kwargs):
        if kwargs.get("filled_count") == 4:
            return False
        return real_update(coid, **kwargs)

    with (
        patch.object(reconciler, "_update", side_effect=fail_floor),
        pytest.raises(RuntimeError, match="evidencia"),
    ):
        await reconciler.reconcile(live_coids=set())
    assert _row().status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("observations", [["4.00", "0.005", "0.00"], ["11.00", "4.00", "0.00"]])
async def test_orphan_multiple_observations_keep_both_quarantine_and_fill_floor(observations):
    _pending()
    error = KalshiOrderConfirmationError("unconfirmed cancel")
    error.observed_orders = [_order(fill_count_fp=fill) for fill in observations]
    client = _Client([_order(status="resting")], cancel_response=error)
    first = await MMReconciler(client).reconcile(live_coids=set())
    assert not first.complete and first.terminal_coids == set()
    assert _row().status == "pending" and _row().filled_count == 4
    assert COUNT_QUARANTINE_NOTE in (_row().notes or "")
    client.orders = [_order()]
    second = await MMReconciler(client).reconcile(live_coids=set())
    assert not second.complete and _row().status == "pending"
    assert _row().filled_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [OSError, TimeoutError, asyncio.CancelledError])
async def test_original_cancel_errors_preserve_observed_floor_and_cancellation(error_type):
    _pending()
    error = error_type("original")
    error.observed_orders = [_order(status="resting", fill_count_fp="4.00")]
    client = _Client([_order(status="resting")], cancel_response=error)
    reconciler = MMReconciler(client)
    if isinstance(error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError) as caught:
            await reconciler.reconcile(live_coids=set())
        assert caught.value is error
    else:
        report = await reconciler.reconcile(live_coids=set())
        assert not report.complete and report.terminal_coids == set()
    assert _row().status == "pending" and _row().filled_count == 4


@pytest.mark.asyncio
async def test_cancelled_orphan_preserves_original_when_fill_persistence_fails():
    _pending()
    original = asyncio.CancelledError("original")
    original.observed_orders = [_order(fill_count_fp="4.00")]
    client = _Client([_order(status="resting")], cancel_response=original)
    reconciler = MMReconciler(client)
    real_update = reconciler._update

    def fail_floor(coid, **kwargs):
        if kwargs.get("filled_count") == 4:
            return False
        return real_update(coid, **kwargs)

    with (
        patch.object(reconciler, "_update", side_effect=fail_floor),
        pytest.raises(asyncio.CancelledError) as caught,
    ):
        await reconciler.reconcile(live_coids=set())
    assert caught.value is original
    assert _row().status == "pending"
