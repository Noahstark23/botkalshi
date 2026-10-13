"""Parada independiente: SQLite temporal, carreras reales, sin red ni trading."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from threading import Event

import pytest
from sqlalchemy import event, text
from sqlmodel import Session, select

from src.control.channel import Authenticator, ControlError
from src.control.entry_stop import EntryStopChannel, StopRequest, verify_receipt
from src.control.permissions import DurablePermissionAdapter
from src.storage.models import Trade
from tests.control.test_permissions import (
    NOW,
    admit,
    command,
    counts,
    trade,
)
from tests.control.test_permissions import setup as permission_setup

setup = permission_setup  # Fixture SQLite compartida, sin recursos operativos.


def request(**changes):
    return replace(
        StopRequest("f" * 32, 11, 22, "demo", "a" * 40, "b" * 32, "c" * 64, NOW, NOW + 60000),
        **changes,
    )


def test_stop_survives_missing_facts_and_full_history(setup):
    adapter, _, _ = setup
    adapter.apply_if_current(command())
    admit(adapter)
    # Simula presupuestos ocupados/inconsistentes: detener no depende de ellos,
    # y no los borra/repara ni permite ninguna nueva admisión.
    with adapter.engine.begin() as c:
        c.execute(
            text("UPDATE motor_permissions_meta SET command_count=1000, admission_count=100000")
        )
    adapter.facts = lambda *_: pytest.fail("la parada no debe pedir hechos")
    status = adapter.entry_stop(request())
    assert status.latched and status.first_request_id == "f" * 32
    assert counts(adapter) == (1, 1)
    with adapter.engine.connect() as c:
        assert c.execute(
            text("SELECT command_count, admission_count FROM motor_permissions_meta")
        ).one() == (1000, 100000)
    assert adapter.entry_stop(request()) == status
    later = request(request_id="e" * 32)
    assert adapter.entry_stop(later) == status  # conserva la atribución original
    with pytest.raises(ControlError, match="PERMISSION_HISTORY_MISMATCH"):
        admit(adapter, trade("later"))


def test_stop_blocks_entries_before_facts_but_preserves_exits(setup):
    adapter, _, clock = setup
    for i, scope in enumerate(("m1.entries", "m5.quotes", "m2.exits", "m3.exits")):
        adapter.apply_if_current(command(scope, command_id=f"{i:032x}"))
    admit(adapter)
    adapter.entry_stop(request())
    facts = adapter.facts
    adapter.facts = lambda *_: pytest.fail("no consultar hechos de una entrada detenida")
    for scope in ("m1.entries", "m5.quotes"):
        with pytest.raises(ControlError, match="NEW_ENTRIES_BLOCKED"):
            admit(adapter, scope=scope)
    adapter.facts = facts
    assert not adapter.observe("m1.entries").gates_clear
    with pytest.raises(ControlError, match="ENABLE_BLOCKED"):
        adapter.apply_if_current(command(revision=1, command_id="e" * 32))
    for scope, strategy in (("m2.exits", "motor_2_consensus"), ("m3.exits", "motor_3_clv")):
        admit(adapter, trade(scope, strategy=strategy, action="sell"), scope)
    assert counts(adapter) == (3, 3)
    with Session(adapter.engine) as s:
        pending = s.exec(select(Trade).where(Trade.client_order_id == "test-order")).one()
        assert pending.status == "pending"  # no cancela, libera o reinterpreta UNKNOWN
    reopened = DurablePermissionAdapter(adapter.engine, adapter.binding, facts, clock=clock)
    reopened.provision()
    assert reopened.entry_stop(request(operation="status", request_id="1" * 32)).latched


def test_regressive_clock_can_stop_without_reopening_normal_path(setup):
    adapter, _, _ = setup
    with adapter.engine.begin() as c:
        c.execute(
            text("UPDATE motor_permissions_meta SET last_seen_ms=:later"), {"later": NOW + 100000}
        )
    assert adapter.entry_stop(request()).latched
    with adapter.engine.connect() as c:
        assert (
            c.execute(text("SELECT last_seen_ms FROM motor_permissions_meta")).scalar_one()
            == NOW + 100000
        )
    with pytest.raises(ControlError, match="CLOCK_REGRESSION"):
        adapter.observe("m2.exits")


@pytest.mark.parametrize(
    "changes",
    [
        {"actor_id": 12},
        {"chat_id": 23},
        {"environment": "production"},
        {"source_sha": "e" * 40},
        {"instance_id": "e" * 32},
        {"limits_sha256": "e" * 64},
        {"issued_at_ms": NOW + 1},
        {"expires_at_ms": NOW},
        {"expires_at_ms": NOW + 60001},
        {"operation": "resume"},
        {"actor_id": True},
        {"request_id": "x" * 32},
    ],
)
def test_stop_revalidates_binding_shape_and_expiry(setup, changes):
    adapter, _, _ = setup
    with pytest.raises(ControlError):
        adapter.entry_stop(request(**changes))
    assert not adapter.entry_stop(request(operation="status")).latched


def test_same_request_id_different_intent_rejected(setup):
    adapter, _, _ = setup
    adapter.entry_stop(request())
    with pytest.raises(ControlError, match="STOP_ID_CONFLICT"):
        adapter.entry_stop(request(expires_at_ms=NOW + 1000))


def test_failed_commit_never_emits_confirmed_receipt(setup):
    adapter, _, _ = setup
    auth = Authenticator(b"x" * 32)
    channel = EntryStopChannel(adapter, auth)
    with adapter.engine.begin() as c:
        c.execute(
            text(
                "CREATE TRIGGER fail_stop BEFORE UPDATE ON motor_permissions_meta BEGIN SELECT RAISE(ABORT,'test'); END"
            )
        )
    with pytest.raises(ControlError, match="^ENTRY_STOP_UNCONFIRMED$"):
        channel.handle(auth.seal("entry-stop-request-v1", asdict(request())))
    with adapter.engine.connect() as c:
        assert (
            c.execute(text("SELECT entry_stop_state FROM motor_permissions_meta")).scalar_one()
            == "clear"
        )


def test_signed_receipt_bound_to_request_domain_and_time(setup):
    adapter, _, _ = setup
    auth = Authenticator(b"x" * 32)
    channel = EntryStopChannel(adapter, auth)
    wire = auth.seal("entry-stop-request-v1", asdict(request()))
    receipt = channel.handle(wire)
    assert verify_receipt(auth, request(), receipt, now_ms=NOW).latched
    for other in (
        request(operation="status"),
        request(request_id="e" * 32),
        request(source_sha="e" * 40),
    ):
        with pytest.raises(ControlError, match="WRONG_ENTRY_STOP_RECEIPT"):
            verify_receipt(auth, other, receipt, now_ms=NOW)
    with pytest.raises(ControlError, match="EXPIRED_OR_FUTURE"):
        verify_receipt(auth, request(), receipt, now_ms=NOW + 60000)
    with pytest.raises(ControlError, match="UNAUTHENTICATED"):
        channel.handle(auth.seal("request", asdict(request())))


def test_stop_serializes_with_inflight_admission(setup):
    adapter, _, _ = setup
    adapter.apply_if_current(command())
    entered, release, stopping = Event(), Event(), Event()

    def reserve():
        with Session(adapter.engine) as s:
            adapter.admit(s, "m1.entries", [trade()])
            entered.set()
            assert release.wait(5)
            s.commit()

    def before_begin(conn, cursor, statement, parameters, context, executemany):
        if statement == "BEGIN IMMEDIATE":
            stopping.set()

    event.listen(adapter.engine, "before_cursor_execute", before_begin)

    def stop():
        return adapter.entry_stop(request())

    with ThreadPoolExecutor(2) as pool:
        reservation = pool.submit(reserve)
        assert entered.wait(5)
        halted = pool.submit(stop)
        assert stopping.wait(5)
        assert not halted.done()
        with adapter.engine.connect() as observer:
            assert (
                observer.execute(
                    text("SELECT entry_stop_state FROM motor_permissions_meta")
                ).scalar_one()
                == "clear"
            )
        release.set()
        reservation.result(5)
        assert halted.result(5).latched
    event.remove(adapter.engine, "before_cursor_execute", before_begin)
    with pytest.raises(ControlError, match="NEW_ENTRIES_BLOCKED"):
        admit(adapter, trade("new"))
    assert counts(adapter) == (1, 1)


def test_status_does_not_write_or_latch_future_clock(setup):
    adapter, _, clock = setup
    with adapter.engine.begin() as c:
        c.execute(
            text(
                "CREATE TRIGGER no_writes BEFORE UPDATE ON motor_permissions_meta BEGIN SELECT RAISE(ABORT,'forbidden'); END"
            )
        )
        clock.value = NOW + 600000
        probe = request(
            operation="status", issued_at_ms=clock.value, expires_at_ms=clock.value + 60000
        )
        assert not adapter.entry_stop(probe).latched
        assert (
            c.execute(text("SELECT last_seen_ms FROM motor_permissions_meta")).scalar_one() == NOW
        )


def test_corrupt_stop_is_not_repaired_or_cleared(setup):
    adapter, _, _ = setup
    with adapter.engine.begin() as c:
        c.execute(text("UPDATE motor_permissions_meta SET entry_stop_state='broken'"))
    with pytest.raises(ControlError, match="INVALID_ENTRY_STOP"):
        adapter.entry_stop(request())
    with pytest.raises(ControlError, match="INVALID_ENTRY_STOP"):
        adapter.observe("m2.exits")


def test_old_candidate_schema_is_rejected_without_migration(setup):
    adapter, _, _ = setup
    with adapter.engine.begin() as c:
        c.execute(text("ALTER TABLE motor_permissions_meta DROP COLUMN entry_stop_state"))
    from sqlalchemy.exc import OperationalError

    with pytest.raises(OperationalError):
        adapter.provision()
    with adapter.engine.connect() as c:
        assert "entry_stop_state" not in {
            r[1] for r in c.execute(text("PRAGMA table_info(motor_permissions_meta)"))
        }


def test_valid_exhausted_budgets_do_not_block_stop(tmp_path):
    from sqlmodel import SQLModel, create_engine

    from src.control.permissions import GateFacts, PermissionBinding

    engine = create_engine(f"sqlite:///{tmp_path / 'full.db'}")
    SQLModel.metadata.create_all(engine)
    binding = PermissionBinding(
        11,
        22,
        "demo",
        "a" * 40,
        "b" * 32,
        "c" * 64,
        frozenset({"m1.entries"}),
        frozenset({"m1.entries"}),
        1,
        1,
    )
    adapter = DurablePermissionAdapter(
        engine, binding, lambda *_: GateFacts(NOW, True, True, 0, 0, 0), clock=lambda: NOW
    )
    adapter.provision()
    adapter.apply_if_current(command())
    admit(adapter)
    with pytest.raises(ControlError, match="PERMISSION_COMMAND_CAPACITY"):
        adapter.apply_if_current(command(enabled=False, revision=1, command_id="e" * 32))
    with pytest.raises(ControlError, match="ADMISSION_CAPACITY"):
        admit(adapter, trade("excess"))
    assert adapter.entry_stop(request()).latched
    assert counts(adapter) == (1, 1)
    engine.dispose()
