"""SQLite real temporal; efectos financieros ficticios y cero red."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from threading import Event

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import Session, SQLModel, create_engine, select

from src.control.channel import Authenticator, CommandChannel, ControlError, Intent
from src.control.permissions import (
    Admission,
    DurablePermissionAdapter,
    GateFacts,
    PermissionBinding,
)
from src.storage.models import OperationalState, Trade

NOW = 1791900000000


class Clock:
    value = NOW

    def __call__(self):
        return self.value


@pytest.fixture
def setup(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    SQLModel.metadata.create_all(engine)
    clock = Clock()
    binding = PermissionBinding(
        11,
        22,
        "demo",
        "a" * 40,
        "b" * 32,
        "c" * 64,
        frozenset({"m1.entries", "m2.exits", "m3.exits", "m5.quotes"}),
        frozenset({"m1.entries", "m2.exits", "m3.exits", "m5.quotes"}),
    )
    state = [GateFacts(NOW, True, True, 0, 0, 0)]
    adapter = DurablePermissionAdapter(engine, binding, lambda *_: state[0], clock=clock)
    adapter.provision()
    yield adapter, state, clock
    engine.dispose()


def command(scope="m1.entries", enabled=True, revision=0, command_id="d" * 32):
    return Intent(
        command_id,
        11,
        22,
        "demo",
        "a" * 40,
        "b" * 32,
        "c" * 64,
        scope,
        enabled,
        revision,
        NOW,
        NOW + 60000,
    )


def trade(coid="test-order", **changes):
    return Trade(
        client_order_id=coid,
        ticker="KXTEST",
        side="yes",
        action=changes.get("action", "buy"),
        count=1,
        price_cents=30,
        strategy=changes.get("strategy", "motor_1_arbitrage"),
        status="pending",
    )


def counts(adapter):
    with Session(adapter.engine) as session:
        return len(session.exec(select(Trade)).all()), len(session.exec(select(Admission)).all())


def admit(adapter, value=None, scope="m1.entries"):
    with Session(adapter.engine) as session:
        adapter.admit(session, scope, [value or trade()])
        session.commit()


def test_provision_disabled_and_reopen_never_resets(setup):
    adapter, _, clock = setup
    assert adapter.observe("m1.entries").enabled is False
    adapter.apply_if_current(command())
    reopened = DurablePermissionAdapter(adapter.engine, adapter.binding, adapter.facts, clock=clock)
    reopened.provision()
    assert reopened.observe("m1.entries").revision == 1
    assert reopened.observe("m1.entries").applied_command_id == command().command_id
    with pytest.raises(ControlError, match="COMMAND_ALREADY_USED"):
        reopened.apply_if_current(command())


def test_missing_provision_does_not_create_or_enable(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'new.db'}")
    b = PermissionBinding(
        11, 22, "demo", "a" * 40, "b" * 32, "c" * 64, frozenset({"m1.entries"}), frozenset()
    )
    (tmp_path / "new.db").touch(mode=0o600)
    adapter = DurablePermissionAdapter(engine, b, lambda *_: None)
    with pytest.raises(OperationalError):
        adapter.observe("m1.entries")
    with engine.connect() as c:
        assert c.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).all() == []


def test_trade_and_attribution_commit_together(setup):
    adapter, _, _ = setup
    adapter.apply_if_current(command())
    admit(adapter)
    assert counts(adapter) == (1, 1)
    with Session(adapter.engine) as session:
        row = session.get(Admission, "test-order")
        assert (row.permission_revision, row.command_id, row.scope) == (1, "d" * 32, "m1.entries")
        assert row.source_sha == "a" * 40 and len(row.intent_digest) == 64


def test_rollback_or_insert_failure_leaves_neither_record(setup):
    adapter, _, _ = setup
    adapter.apply_if_current(command())
    with Session(adapter.engine) as session:
        adapter.admit(session, "m1.entries", [trade()])
        session.rollback()
    assert counts(adapter) == (0, 0)
    with adapter.engine.begin() as c:
        c.execute(
            text(
                "CREATE TRIGGER fail_trade BEFORE INSERT ON trades BEGIN SELECT RAISE(ABORT,'test'); END"
            )
        )
    with pytest.raises(IntegrityError):
        admit(adapter)
    assert counts(adapter) == (0, 0)
    with adapter.engine.connect() as c:
        assert (
            c.execute(text("SELECT admission_count FROM motor_permissions_meta")).scalar_one() == 0
        )


@pytest.mark.parametrize(
    "change",
    [
        {"actor_id": 12},
        {"chat_id": 23},
        {"environment": "production"},
        {"source_sha": "e" * 40},
        {"instance_id": "e" * 32},
        {"limits_sha256": "e" * 64},
        {"expected_revision": 1},
        {"expires_at_ms": NOW},
        {"issued_at_ms": NOW + 1},
    ],
)
def test_adapter_revalidates_intent_without_channel(setup, change):
    adapter, _, _ = setup
    with pytest.raises(ControlError):
        adapter.apply_if_current(replace(command(), **change))
    assert adapter.observe("m1.entries").revision == 0


@pytest.mark.parametrize(
    "change",
    [
        {"gates_clear": False},
        {"available": False},
        {"uncertain_orders": 1},
        {"gates_clear": 1},
        {"open_positions": -1},
        {"observed_at_ms": NOW - 30001},
        {"observed_at_ms": NOW + 1},
    ],
)
def test_changed_or_unknown_facts_block_apply_and_admission(setup, change):
    adapter, state, _ = setup
    adapter.apply_if_current(command())
    state[0] = replace(state[0], **change)
    with pytest.raises(ControlError):
        adapter.apply_if_current(command(revision=1, command_id="e" * 32))
    with pytest.raises(ControlError):
        admit(adapter)
    assert counts(adapter) == (0, 0)


def test_disabled_unknown_scope_and_boot_ceiling_block(setup):
    adapter, _, clock = setup
    with pytest.raises(ControlError, match="PERMISSION_DISABLED"):
        admit(adapter)
    with pytest.raises(ControlError, match="SCOPE_UNAVAILABLE"):
        adapter.observe("m8.observe")
    other = DurablePermissionAdapter(
        adapter.engine,
        replace(adapter.binding, boot_allowed=frozenset()),
        adapter.facts,
        clock=clock,
    )
    with pytest.raises(ControlError, match="BINDING_CHANGED"):
        other.provision()


@pytest.mark.parametrize("value", ["engaged", "garbage"])
def test_durable_kill_never_cleared(setup, value):
    adapter, _, _ = setup
    with Session(adapter.engine) as session:
        session.add(OperationalState(key="kill_switch", value=value))
        session.commit()
    with pytest.raises((ControlError, ValueError)):
        adapter.apply_if_current(command())
    with Session(adapter.engine) as session:
        assert session.get(OperationalState, "kill_switch").value == value


@pytest.mark.parametrize("scope", ["m2.exits", "m3.exits"])
@pytest.mark.parametrize("field", ["open_positions", "resting_orders", "uncertain_orders"])
def test_exit_protection_cannot_be_disabled_with_exposure(setup, scope, field):
    adapter, state, _ = setup
    adapter.apply_if_current(command(scope=scope))
    state[0] = replace(state[0], **{field: 1})
    with pytest.raises(ControlError, match="EXIT_PROTECTION_REQUIRED"):
        adapter.apply_if_current(
            command(scope=scope, enabled=False, revision=1, command_id="e" * 32)
        )
    assert adapter.observe(scope).enabled


def test_entry_pause_preserves_unknown_and_reserved_trade(setup):
    adapter, state, _ = setup
    adapter.apply_if_current(command())
    admit(adapter)
    state[0] = replace(state[0], gates_clear=False, uncertain_orders=1)
    adapter.apply_if_current(command(enabled=False, revision=1, command_id="e" * 32))
    assert not adapter.observe("m1.entries").enabled
    assert counts(adapter) == (1, 1)
    with Session(adapter.engine) as session:
        assert session.exec(select(Trade)).one().status == "pending"


def test_concurrent_commands_one_revision_one_winner(setup):
    adapter, _, _ = setup

    def apply(i):
        try:
            adapter.apply_if_current(command(command_id=i * 32))
            return True
        except ControlError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(apply, ["d", "e"])) == [False, True]
    assert adapter.observe("m1.entries").revision == 1


def test_pause_waits_for_reservation_transaction_and_preserves_attribution(setup):
    adapter, _, _ = setup
    adapter.apply_if_current(command())
    started = Event()

    def pause():
        started.set()
        adapter.apply_if_current(command(enabled=False, revision=1, command_id="e" * 32))

    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(adapter.engine) as session:
            adapter.admit(session, "m1.entries", [trade()])
            future = pool.submit(pause)
            assert started.wait(2)
            session.commit()
        future.result(timeout=5)
    with pytest.raises(ControlError, match="PERMISSION_DISABLED"):
        admit(adapter, trade("second"))
    with Session(adapter.engine) as session:
        assert session.get(Admission, "test-order").permission_revision == 1
    assert adapter.observe("m1.entries").revision == 2


def test_same_id_changed_intent_never_reused(setup):
    adapter, _, _ = setup
    adapter.apply_if_current(command())
    with pytest.raises(ControlError, match="COMMAND_ALREADY_USED"):
        adapter.apply_if_current(command(enabled=False, revision=1))


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE motor_permissions SET state='unknown' WHERE scope='m1.entries'",
        "UPDATE motor_permissions SET revision=-1 WHERE scope='m1.entries'",
        "DELETE FROM motor_permissions WHERE scope='m3.exits'",
        "DELETE FROM motor_permission_commands",
        "UPDATE motor_permissions_meta SET admission_count=-1",
    ],
)
def test_corruption_blocks_without_repair(setup, sql):
    adapter, _, _ = setup
    adapter.apply_if_current(command())
    with adapter.engine.begin() as c:
        c.execute(text(sql))
    with pytest.raises(ControlError):
        admit(adapter)
    assert counts(adapter) == (0, 0)


def test_clock_regression_and_expiry_during_observation(setup):
    adapter, state, clock = setup
    clock.value -= 1
    with pytest.raises(ControlError, match="CLOCK_REGRESSION"):
        adapter.apply_if_current(command())
    clock.value = NOW

    def slow(*_):
        clock.value = NOW + 60000
        return replace(state[0], observed_at_ms=clock.value)

    adapter.facts = slow
    with pytest.raises(ControlError, match="EXPIRED_OR_FUTURE"):
        adapter.apply_if_current(command())


def test_full_capacities_fail_closed_without_pruning(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'bounded.db'}")
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
    with pytest.raises(ControlError, match="ADMISSION_CAPACITY"):
        admit(adapter, trade("second"))
    with pytest.raises(ControlError, match="PERMISSION_COMMAND_CAPACITY"):
        adapter.apply_if_current(command(enabled=False, revision=1, command_id="e" * 32))
    assert adapter.observe("m1.entries").enabled
    assert counts(adapter) == (1, 1)
    engine.dispose()


def test_command_channel_lost_ack_reconciles_without_reapply(setup, tmp_path):
    adapter, _, clock = setup
    auth = Authenticator(b"x" * 32)
    channel = CommandChannel(
        tmp_path / "journal.db", auth=auth, actor_id=11, chat_id=22, environment="demo", clock=clock
    )

    def send(op, token=None):
        return auth.open(
            "response",
            channel.handle(
                auth.seal(
                    "request", {"operation": op, "intent": asdict(command()), "confirmation": token}
                ),
                adapter=adapter,
            ),
        )

    p = send("propose")
    send("confirm", p["confirmation"])
    original = adapter.apply_if_current
    calls = []

    def lost_ack(i):
        original(i)
        calls.append(1)
        raise RuntimeError("simulated ack loss")

    adapter.apply_if_current = lost_ack
    assert channel.consume(command().command_id, adapter=adapter)["state"] == "UNKNOWN"
    assert channel.consume(command().command_id, adapter=adapter)["state"] == "UNKNOWN"
    assert channel.reconcile(command().command_id, adapter=adapter)["state"] == "APPLIED"
    assert calls == [1]


def test_bound_ledger_replacement_blocks_without_recreating(setup):
    adapter, _, _ = setup
    adapter.path.rename(adapter.path.with_suffix(".old"))
    adapter.path.touch()
    with pytest.raises(ControlError, match="LEDGER_REPLACED"):
        adapter.observe("m1.entries")
    assert adapter.path.stat().st_size == 0


def test_valid_looking_state_change_without_command_is_rejected(setup):
    adapter, _, _ = setup
    adapter.apply_if_current(command())
    with adapter.engine.begin() as c:
        c.execute(text("UPDATE motor_permissions SET state='disabled' WHERE scope='m1.entries'"))
    with pytest.raises(ControlError, match="ATTRIBUTION_INVALID"):
        adapter.observe("m1.entries")


def test_boot_ceiling_blocks_enable_even_with_clear_facts(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ceiling.db'}")
    SQLModel.metadata.create_all(engine)
    binding = PermissionBinding(
        11, 22, "demo", "a" * 40, "b" * 32, "c" * 64, frozenset({"m1.entries"}), frozenset()
    )
    adapter = DurablePermissionAdapter(
        engine, binding, lambda *_: GateFacts(NOW, True, True, 0, 0, 0), clock=lambda: NOW
    )
    adapter.provision()
    with pytest.raises(ControlError, match="ENABLE_BLOCKED"):
        adapter.apply_if_current(command())
    assert adapter.observe("m1.entries").revision == 0
    engine.dispose()


@pytest.mark.parametrize("operation", ["observe", "apply", "admit"])
@pytest.mark.parametrize(
    "end,reason", [(NOW + 1, "CLOCK_REGRESSION"), (NOW + 30001, "STALE_GATE_FACTS")]
)
def test_time_change_after_facts_blocks_transaction(setup, operation, end, reason):
    adapter, _, _ = setup
    adapter.apply_if_current(command())
    times = iter([NOW, NOW + 2, end])
    adapter.clock = lambda: next(times)
    with pytest.raises(ControlError, match=reason):
        if operation == "observe":
            adapter.observe("m1.entries")
        elif operation == "apply":
            adapter.apply_if_current(command(enabled=False, revision=1, command_id="e" * 32))
        else:
            admit(adapter)
    assert counts(adapter) == (0, 0)
    adapter.clock = lambda: NOW
    assert adapter.observe("m1.entries").revision == 1


def test_command_insert_failure_rolls_back_permission_and_counter(setup):
    adapter, _, _ = setup
    with adapter.engine.begin() as c:
        c.execute(
            text(
                "CREATE TRIGGER fail_command BEFORE INSERT ON motor_permission_commands BEGIN SELECT RAISE(ABORT,'test'); END"
            )
        )
    with pytest.raises(IntegrityError):
        adapter.apply_if_current(command())
    assert adapter.observe("m1.entries").revision == 0
    with adapter.engine.connect() as c:
        assert c.execute(text("SELECT command_count FROM motor_permissions_meta")).scalar_one() == 0


def test_rebuild_preserves_permissions_commands_and_admissions(setup, monkeypatch):
    import sqlite3

    import scripts.rebuild_db as rebuild

    adapter, _, clock = setup
    adapter.apply_if_current(command())
    admit(adapter)
    monkeypatch.setenv("DATABASE_URL", str(adapter.engine.url))
    monkeypatch.setattr(rebuild, "_MIN_FREE_BYTES", 0)
    assert rebuild.main() == 0
    tables = (
        "motor_permissions_meta",
        "motor_permissions",
        "motor_permission_commands",
        "motor_permission_admissions",
    )
    with (
        sqlite3.connect(f"file:{adapter.path}?mode=ro", uri=True) as old,
        sqlite3.connect(f"file:{adapter.path}.rebuilt?mode=ro", uri=True) as new,
    ):
        for table in tables:
            assert (
                old.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                == new.execute(  # noqa: S608
                    f"SELECT * FROM {table} ORDER BY 1"  # noqa: S608
                ).fetchall()
            )
    copied_engine = create_engine(f"sqlite:///{adapter.path}.rebuilt")
    copied = DurablePermissionAdapter(copied_engine, adapter.binding, adapter.facts, clock=clock)
    copied.provision()
    assert copied.observe("m1.entries").revision == 1
    assert counts(copied) == (1, 1)
    with pytest.raises(ControlError, match="COMMAND_ALREADY_USED"):
        copied.apply_if_current(command())
    copied_engine.dispose()


@pytest.mark.parametrize("damage", ["delete_admission", "drop_admission", "delete_old_command"])
def test_missing_history_never_repaired_or_accepted(setup, damage):
    adapter, _, _ = setup
    adapter.apply_if_current(command())
    admit(adapter)
    adapter.apply_if_current(command(revision=1, command_id="e" * 32))
    with adapter.engine.begin() as c:
        sql = {
            "delete_admission": "DELETE FROM motor_permission_admissions",
            "drop_admission": "DROP TABLE motor_permission_admissions",
            "delete_old_command": "DELETE FROM motor_permission_commands WHERE command_id='"
            + "d" * 32
            + "'",
        }[damage]
        c.execute(text(sql))
    with pytest.raises(ControlError):
        adapter.provision()
    with pytest.raises((ControlError, OperationalError)):
        admit(adapter, trade("second"))
    with Session(adapter.engine) as s:
        assert s.exec(select(Trade)).one().client_order_id == "test-order"
    if damage == "drop_admission":
        with adapter.engine.connect() as c:
            assert (
                c.execute(
                    text("SELECT name FROM sqlite_master WHERE name='motor_permission_admissions'")
                ).first()
                is None
            )


def test_lost_meta_is_not_reprovisioned(setup):
    adapter, _, _ = setup
    with adapter.engine.begin() as c:
        c.execute(text("DELETE FROM motor_permissions_meta"))
    with pytest.raises(ControlError, match="BINDING_CHANGED"):
        adapter.provision()
    with adapter.engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM motor_permissions_meta")).scalar_one() == 0


def test_initial_provision_schema_rolls_back_on_failure(tmp_path, monkeypatch):
    from src.control.permissions import _Model

    engine = create_engine(f"sqlite:///{tmp_path / 'atomic-schema.db'}")
    SQLModel.metadata.create_all(engine)
    binding = PermissionBinding(
        11, 22, "demo", "a" * 40, "b" * 32, "c" * 64, frozenset({"m1.entries"}), frozenset()
    )
    adapter = DurablePermissionAdapter(engine, binding, lambda *_: None, clock=lambda: NOW)
    original = _Model.metadata.create_all

    def fail_after_ddl(connection):
        original(connection)
        raise RuntimeError("simulated interruption after DDL")

    with monkeypatch.context() as m:
        m.setattr(_Model.metadata, "create_all", fail_after_ddl)
        with pytest.raises(RuntimeError, match="interruption"):
            adapter.provision()
    with engine.connect() as c:
        assert (
            c.execute(
                text("SELECT name FROM sqlite_master WHERE name LIKE 'motor_permission%'")
            ).all()
            == []
        )
    adapter.provision()
    engine.dispose()


def test_autocommit_reservation_requires_actual_sqlite_transaction(setup):
    adapter, _, clock = setup
    adapter.apply_if_current(command())
    autocommit = create_engine(str(adapter.engine.url), isolation_level="AUTOCOMMIT")
    other = DurablePermissionAdapter(autocommit, adapter.binding, adapter.facts, clock=clock)
    with pytest.raises(ControlError, match="RESERVATION_TRANSACTION_REQUIRED"):
        admit(other)
    assert counts(adapter) == (0, 0)
    with Session(autocommit) as session:
        session.exec(text("BEGIN IMMEDIATE"))
        other.admit(session, "m1.entries", [trade()])
        session.rollback()
    assert counts(adapter) == (0, 0)
    autocommit.dispose()


@pytest.mark.parametrize("operation", ["observe", "apply", "admit"])
def test_forward_clock_jump_is_not_automatically_repaired(setup, operation):
    adapter, facts, clock = setup
    adapter.apply_if_current(command())
    clock.value = NOW + 3600000
    facts[0] = replace(facts[0], observed_at_ms=clock.value)
    adapter.observe("m1.entries")
    clock.value = NOW
    facts[0] = replace(facts[0], observed_at_ms=NOW)
    with pytest.raises(ControlError, match="CLOCK_REGRESSION"):
        if operation == "observe":
            adapter.observe("m1.entries")
        elif operation == "apply":
            adapter.apply_if_current(command(enabled=False, revision=1, command_id="e" * 32))
        else:
            admit(adapter)
    with adapter.engine.connect() as c:
        assert (
            c.execute(text("SELECT last_seen_ms FROM motor_permissions_meta")).scalar_one()
            == NOW + 3600000
        )
    assert counts(adapter) == (0, 0)
