"""Recuperación explícita: ledger temporal, cortes reales y ninguna orden en red."""

import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import Session

from src.control.channel import Authenticator, ControlError
from src.control.entry_stop import BINDING_FIELDS, EntryStopChannel
from src.control.permissions import DurablePermissionAdapter
from src.control.stop_mailbox import consume_once
from src.control.telegram_entry_stop import PREFIX, TelegramEntryStopRouter
from tests.control.test_entry_stop import request
from tests.control.test_permissions import NOW, admit, command, counts, trade
from tests.control.test_permissions import setup as setup
from tests.control.test_stop_bridge import bridge as bridge
from tests.control.test_stop_bridge import confirm, update


def successor(adapter, **changes):
    return DurablePermissionAdapter(
        adapter.engine,
        replace(adapter.binding, source_sha="e" * 40, instance_id="f" * 32, **changes),
        adapter.facts,
        clock=adapter.clock,
    )


def rebind(adapter, previous, generation=0):
    return adapter.rebind_instance(
        expected_previous_digest=previous.binding.digest(), expected_generation=generation
    )


def rows(adapter, table):
    with adapter.engine.connect() as connection:
        return connection.execute(text(f"SELECT * FROM {table}")).all()


def test_rebind_multi_scope_keeps_protective_authority_and_all_history(setup):
    old, state, _ = setup
    for i, scope in enumerate(sorted(old.binding.scopes), 1):
        old.apply_if_current(command(scope, command_id=f"{i:032x}"))
    admit(old)
    old.entry_stop(request())
    preserved = {
        t: rows(old, t)
        for t in ("trades", "motor_permission_admissions", "motor_permission_commands")
    }
    state[0] = replace(state[0], available=False, uncertain_orders=3, resting_orders=2)
    new = successor(old)
    assert rebind(new, old) == 1
    for table, history in preserved.items():
        assert rows(new, table) == history
    with new.engine.connect() as c:
        permissions = {
            r[0]: r
            for r in c.execute(
                text(
                    "SELECT scope,state,revision,command_id,transition_generation FROM motor_permissions"
                )
            )
        }
        assert permissions["m1.entries"][1:] == ("disabled", 2, None, 1)
        assert permissions["m5.quotes"][1:] == ("disabled", 2, None, 1)
        for scope in ("m2.exits", "m3.exits"):
            assert permissions[scope][1:3] == ("enabled", 1)
            assert permissions[scope][3] is not None and permissions[scope][4] is None
        assert c.execute(
            text(
                "SELECT command_count,admission_count,entry_stop_request_id FROM motor_permissions_meta"
            )
        ).one() == (4, 1, request().request_id)
    assert new.entry_stop(
        replace(request(operation="status"), source_sha="e" * 40, instance_id="f" * 32)
    ).latched
    for action in (
        lambda: old.observe("m1.entries"),
        lambda: admit(old),
        lambda: old.apply_if_current(command(revision=1)),
        lambda: old.entry_stop(request()),
    ):
        with pytest.raises(ControlError, match="BINDING_CHANGED"):
            action()


@pytest.mark.parametrize(
    "changes",
    [
        {"actor_id": 33},
        {"chat_id": 33},
        {"environment": "production"},
        {"limits_sha256": "d" * 64},
        {"scopes": frozenset({"m1.entries"})},
        {"boot_allowed": frozenset()},
        {"max_commands": 999},
        {"max_admissions": 999},
    ],
)
def test_rebind_stable_identity_exact_not_even_smaller_exits(setup, changes):
    old, _, _ = setup
    if "scopes" in changes:
        changes["boot_allowed"] = changes["scopes"]
    new = successor(old, **changes)
    with pytest.raises(ControlError, match="STABLE_IDENTITY"):
        rebind(new, old)
    assert old.observe("m1.entries").revision == 0


def test_disabled_stays_disabled_counters_not_renewed_and_nonce_never_reused(setup):
    old, _, _ = setup
    new = successor(old)
    rebind(new, old)
    assert new.observe("m1.entries").revision == 0
    third = DurablePermissionAdapter(
        old.engine, replace(old.binding, instance_id="a" * 32), old.facts, clock=old.clock
    )
    assert rebind(third, new, 1) == 2
    with pytest.raises(ControlError, match="INSTANCE_REUSED"):
        rebind(old, third, 2)
    with pytest.raises(ControlError):
        rebind(new, old)
    with old.engine.connect() as c:
        assert c.execute(
            text(
                "SELECT command_count,admission_count,binding_generation FROM motor_permissions_meta"
            )
        ).one() == (0, 0, 2)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE motor_permissions_meta SET binding_json='{}'",
        "UPDATE motor_permissions_meta SET binding_generation=1",
        "UPDATE motor_permissions_meta SET binding_digest='bad'",
        "DELETE FROM motor_permissions_meta",
        "DROP TABLE motor_permission_transitions",
    ],
)
def test_metadata_corruption_cannot_be_recovered_or_provisioned(setup, sql):
    old, _, _ = setup
    with old.engine.begin() as c:
        c.execute(text(sql))
    before = rows(old, "motor_permissions")
    for op in (lambda: rebind(successor(old), old), old.provision):
        with pytest.raises((ControlError, OperationalError)):
            op()
    assert rows(old, "motor_permissions") == before


def test_rebind_rolls_back_every_effect_when_persistence_fails(setup):
    old, _, _ = setup
    old.apply_if_current(command())
    with old.engine.begin() as c:
        c.execute(
            text(
                "CREATE TRIGGER fail_rebind BEFORE INSERT ON motor_permission_transitions BEGIN SELECT RAISE(ABORT,'test'); END"
            )
        )
    with pytest.raises(IntegrityError):
        rebind(successor(old), old)
    assert old.observe("m1.entries").enabled
    assert rows(old, "motor_permission_transitions") == []


def test_rebind_waits_for_old_admission_commit_and_old_authority_then_fails(setup):
    old, _, _ = setup
    old.apply_if_current(command())
    locked, release, attempted = Event(), Event(), Event()

    def reserve():
        with Session(old.engine) as s:
            old.admit(s, "m1.entries", [trade()])
            locked.set()
            assert release.wait(5)
            s.commit()

    new = successor(old)

    def recover():
        attempted.set()
        return rebind(new, old)

    with ThreadPoolExecutor(2) as executor:
        pending = executor.submit(reserve)
        assert locked.wait(5)
        recovery = executor.submit(recover)
        assert attempted.wait(5)
        release.set()
        pending.result()
        assert recovery.result() == 1
    assert counts(new) == (1, 1)
    with pytest.raises(ControlError):
        admit(old, trade("second"))
    assert not new.observe("m1.entries").enabled


def test_lost_nonce_in_separate_dead_process_is_recovered_from_database(setup):
    old, _, _ = setup
    # Simula dueño muerto después de confirmar una transición, sin exportar nonce.
    code = """import os,secrets
from dataclasses import replace
from sqlmodel import create_engine
from src.control.permissions import PermissionBinding,DurablePermissionAdapter
engine=create_engine(os.environ['TEST_LEDGER'])
binding=PermissionBinding.parse(os.environ['TEST_BINDING'])
new=DurablePermissionAdapter(engine,replace(binding,instance_id=secrets.token_hex(16)),lambda *_:None,clock=lambda:int(os.environ['TEST_NOW']))
new.rebind_instance(expected_previous_digest=binding.digest(),expected_generation=0)
os._exit(0)
"""
    env = {
        **os.environ,
        "TEST_LEDGER": str(old.engine.url),
        "TEST_BINDING": old.binding.canonical(),
        "TEST_NOW": str(NOW),
    }
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True)
    assert result.returncode == 0, result.stderr.decode()
    with old.engine.connect() as c:
        digest, generation = c.execute(
            text("SELECT binding_digest,binding_generation FROM motor_permissions_meta")
        ).one()
    new = successor(old)
    assert new.rebind_instance(expected_previous_digest=digest, expected_generation=generation) == 2
    assert new.observe("m1.entries").revision == 0


def test_historical_write_is_exact_never_a_current_permission_or_m5_retirement(setup):
    old, _, clock = setup
    intent = command("m5.quotes", enabled=False)
    old.apply_if_current(intent)
    new = successor(old)
    rebind(new, old)
    clock.value += 60001  # Historia no depende del TTL vigente.
    proof = new.historical_write(intent)
    assert proof.fenced and not proof.enabled
    assert not proof.quotes_retired_proven and not proof.current_permission_proven
    assert new.historical_write(replace(intent, command_id="e" * 32)) is None
    with pytest.raises(ControlError):
        new.historical_write(replace(intent, expected_revision=1))


def test_new_identity_requires_new_approval_and_protective_exit_keeps_its_gates(setup):
    old, state, _ = setup
    old.apply_if_current(command())
    old.apply_if_current(command("m2.exits", command_id="e" * 32))
    new = successor(old)
    rebind(new, old)
    with pytest.raises(ControlError):
        new.apply_if_current(command(revision=2, command_id="a" * 32))
    with pytest.raises(ControlError, match="DISABLED"):
        admit(new)
    new.apply_if_current(
        replace(command(revision=2, command_id="a" * 32), source_sha="e" * 40, instance_id="f" * 32)
    )
    admit(new)
    admit(new, trade("exit", action="sell", strategy="motor_2_consensus"), "m2.exits")
    state[0] = replace(state[0], uncertain_orders=1)
    with pytest.raises(ControlError, match="ENABLE_BLOCKED"):
        admit(new, trade("uncertain-exit", action="sell", strategy="motor_2_consensus"), "m2.exits")


def router_rebind(bridge, new, **changes):
    router, _, requests, receipts, path = bridge
    kwargs = {
        "control": router.control,
        "identity": {k: getattr(new.binding, k) for k in BINDING_FIELDS},
        "auth": router.auth,
        "requests": requests,
        "receipts": receipts,
        "announcement": changes.pop("announcement")
        if "announcement" in changes
        else new.recovery_announcement(router.auth),
        "expected_previous_config": router.config,
        "expected_generation": 0,
        "now_ms": NOW,
    }
    kwargs.update(changes)
    return TelegramEntryStopRouter.rebind_instance(path, **kwargs)


def test_bridge_recovery_same_db_preserves_unknown_latch_history_and_invalidates_old_nonce(bridge):
    router, channel, requests, receipts, path = bridge
    value, _ = confirm(router)
    consume_once(requests, receipts, channel)
    old_result = update(3, PREFIX + "result:stop")
    assert "bloqueadas" in router.handle_update(old_result, now_ms=NOW)[-1]["payload"]["text"]
    pending = router.handle_update(update(4, PREFIX + "stop"), now_ms=NOW)
    callback = pending[-1]["payload"]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    with sqlite3.connect(path) as db:
        history = db.execute("SELECT * FROM entry_stop_panel_history").fetchall()
    old_wire = requests.read("stop")
    old = channel.adapter
    new = successor(old)
    rebind(new, old)
    recovered = router_rebind(bridge, new)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM entry_stop_panel_history").fetchall() == history
        assert db.execute(
            "SELECT stop_count,last_update,generation FROM entry_stop_panel_meta"
        ).fetchone() == (1, 4, 1)
        assert db.execute("SELECT nonce_hash FROM entry_stop_panel_transitions").fetchone()[0]
    assert requests.read("stop") == old_wire
    with pytest.raises(ControlError, match="PANEL_CONFIG_CHANGED"):
        router.snapshot()
    assert "histórica" in recovered.handle_update(old_result, now_ms=NOW)[-1]["payload"]["text"]
    assert (
        "UNKNOWN"
        in recovered.handle_update(update(5, PREFIX + "result:stop"), now_ms=NOW)[-1]["payload"][
            "text"
        ]
    )
    assert (
        "inválida"
        in recovered.handle_update(update(6, callback), now_ms=NOW)[-1]["payload"]["text"]
    )
    recovered.handle_update(update(7, PREFIX + "status"), now_ms=NOW)
    consume_once(requests, receipts, EntryStopChannel(new, router.auth))
    assert (
        "bloqueadas"
        in recovered.handle_update(update(8, PREFIX + "result:status"), now_ms=NOW)[-1]["payload"][
            "text"
        ]
    )


@pytest.mark.parametrize("bad", ["signature", "expired", "target", "cas", "generation", "clock"])
def test_router_rebind_failure_preserves_original_binding_and_history(bridge, bad):
    router, channel, _, _, path = bridge
    confirm(router)
    old = channel.adapter
    new = successor(old)
    rebind(new, old)
    change = {
        "signature": {"announcement": new.recovery_announcement(Authenticator(b"z" * 32))},
        "expired": {"now_ms": NOW + 60000},
        "target": {"identity": dict(router.identity)},
        "cas": {"expected_previous_config": "a" * 64},
        "generation": {"expected_generation": 1},
        "clock": {"now_ms": NOW - 1},
    }[bad]
    with pytest.raises(ControlError):
        router_rebind(bridge, new, **change)
    assert router.snapshot()["entry_stop_publication"] == "PUBLISHED_AWAITING_RECEIPT"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT generation,stop_count FROM entry_stop_panel_meta").fetchone() == (
            0,
            1,
        )


@pytest.mark.parametrize("mode", ["confirmed", "unknown_m5"])
def test_old_journal_commands_never_reapply_or_close_unknown_on_historical_proof(
    setup, tmp_path, mode
):
    from dataclasses import asdict

    from src.control.channel import CommandChannel

    old, _, clock = setup
    intent = command("m5.quotes", enabled=False) if mode == "unknown_m5" else command()
    auth = Authenticator(b"x" * 32)
    channel = CommandChannel(
        tmp_path / "journal.db", auth=auth, actor_id=11, chat_id=22, environment="demo", clock=clock
    )

    def send(op, token=None):
        return auth.open(
            "response",
            channel.handle(
                auth.seal(
                    "request", {"operation": op, "intent": asdict(intent), "confirmation": token}
                ),
                adapter=old,
            ),
        )

    proposed = send("propose")
    send("confirm", proposed["confirmation"])
    calls = []
    if mode == "unknown_m5":
        original = old.apply_if_current

        def lost_ack(value):
            calls.append(1)
            original(value)
            raise RuntimeError("ack lost")

        old.apply_if_current = lost_ack
        assert channel.consume(intent.command_id, adapter=old)["state"] == "UNKNOWN"
    new = successor(old)
    rebind(new, old)
    proof = new.historical_write(intent)
    assert (proof is not None) == (mode == "unknown_m5")
    result = channel.consume(intent.command_id, adapter=new)
    assert result["state"] == ("UNKNOWN" if mode == "unknown_m5" else "BLOCKED")
    if mode == "unknown_m5":
        assert channel.reconcile(intent.command_id, adapter=new)["state"] == "UNKNOWN"
        assert calls == [1]
    assert len(rows(new, "motor_permission_commands")) == (1 if mode == "unknown_m5" else 0)


def test_recovery_at_capacity_does_not_renew_counters_and_local_stop_survives_clock_failure(
    tmp_path,
):
    from sqlmodel import SQLModel, create_engine

    from src.control.permissions import GateFacts, PermissionBinding
    from tests.control.test_permissions import Clock

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
    clock = Clock()
    old = DurablePermissionAdapter(
        engine, binding, lambda *_: GateFacts(clock(), True, True, 0, 0, 0), clock=clock
    )
    old.provision()
    old.apply_if_current(command())
    admit(old)
    new = successor(old)
    rebind(new, old)
    with pytest.raises(ControlError, match="COMMAND_CAPACITY"):
        new.apply_if_current(
            replace(
                command(command_id="e" * 32, revision=2), source_sha="e" * 40, instance_id="f" * 32
            )
        )
    clock.value -= 1
    with pytest.raises(ControlError, match="CLOCK_REGRESSION"):
        rebind(successor(new), new, 1)
    stopped = new.entry_stop(
        replace(
            request(),
            source_sha="e" * 40,
            instance_id="f" * 32,
            issued_at_ms=clock(),
            expires_at_ms=clock() + 60000,
        )
    )
    assert stopped.latched
    with engine.connect() as c:
        assert c.execute(
            text("SELECT command_count,admission_count,last_seen_ms FROM motor_permissions_meta")
        ).one() == (1, 1, NOW)
    engine.dispose()


def test_transition_capacity_is_finite_without_pruning_or_automatic_expansion(setup, monkeypatch):
    import src.control.permissions as permissions

    old, _, _ = setup
    monkeypatch.setattr(permissions, "MAX_REBINDS", 2)
    new = successor(old)
    rebind(new, old)
    third = DurablePermissionAdapter(
        old.engine, replace(old.binding, instance_id="a" * 32), old.facts, clock=old.clock
    )
    rebind(third, new, 1)
    fourth = DurablePermissionAdapter(
        old.engine, replace(old.binding, instance_id="c" * 32), old.facts, clock=old.clock
    )
    with pytest.raises(ControlError, match="CAPACITY"):
        rebind(fourth, third, 2)
    assert len(rows(third, "motor_permission_transitions")) == 2
    assert third.entry_stop(replace(request(), instance_id="a" * 32)).latched


@pytest.mark.parametrize("damage", ["duplicate", "unknown_field", "list_duplicate", "wrong_type"])
def test_noncanonical_binding_metadata_fails_without_repair(setup, damage):
    import json

    old, _, _ = setup
    raw = old.binding.canonical()
    if damage == "duplicate":
        raw = raw.replace('"actor_id":11', '"actor_id":11,"actor_id":11')
    else:
        parsed = json.loads(raw)
        if damage == "unknown_field":
            parsed["extra"] = 1
        if damage == "list_duplicate":
            parsed["scopes"].append(parsed["scopes"][0])
        if damage == "wrong_type":
            parsed["actor_id"] = True
        raw = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    with old.engine.begin() as c:
        c.execute(text("UPDATE motor_permissions_meta SET binding_json=:raw"), {"raw": raw})
    with pytest.raises(ControlError, match="METADATA"):
        rebind(successor(old), old)
    with pytest.raises(ControlError, match="METADATA"):
        old.entry_stop(request())


def test_rebuild_preserves_transition_evidence_and_remains_recoverable(setup, monkeypatch):
    from sqlmodel import create_engine

    import scripts.rebuild_db as rebuild

    old, _, _ = setup
    old.apply_if_current(command())
    admit(old)
    new = successor(old)
    rebind(new, old)
    monkeypatch.setenv("DATABASE_URL", str(old.engine.url))
    monkeypatch.setattr(rebuild, "_MIN_FREE_BYTES", 0)
    assert rebuild.main() == 0
    copied_engine = create_engine(f"sqlite:///{old.path}.rebuilt")
    copied = DurablePermissionAdapter(copied_engine, new.binding, new.facts, clock=new.clock)
    copied.provision()
    for table in (
        "motor_permissions_meta",
        "motor_permissions",
        "motor_permission_commands",
        "motor_permission_admissions",
        "motor_permission_transitions",
        "trades",
    ):
        assert rows(copied, table) == rows(new, table)
    assert copied.historical_write(command()).fenced
    copied_engine.dispose()


def test_second_router_cut_checks_full_digest_chain_before_commit(bridge):
    router, channel, requests, receipts, path = bridge
    old = channel.adapter
    new = successor(old)
    rebind(new, old)
    recovered = router_rebind(bridge, new)
    third = DurablePermissionAdapter(
        old.engine, replace(old.binding, instance_id="a" * 32), old.facts, clock=old.clock
    )
    rebind(third, new, 1)
    wire = third.recovery_announcement(router.auth)
    body = router.auth.open("entry-stop-rebind-v1", wire)
    body["from_permission_digest"] = "d" * 64
    bad = router.auth.seal("entry-stop-rebind-v1", body)
    second = (recovered, EntryStopChannel(new, router.auth), requests, receipts, path)
    with pytest.raises(ControlError, match="TRANSITION_MISMATCH"):
        router_rebind(second, third, expected_generation=1, announcement=bad)
    assert recovered.snapshot()["entry_stop_bridge_configured"]
    rebound = router_rebind(second, third, expected_generation=1, announcement=wire)
    assert rebound.identity["instance_id"] == "a" * 32
    with pytest.raises(ControlError):
        router_rebind(second, third, expected_generation=1, announcement=wire)


def test_router_transition_rollback_preserves_nonce_and_history(bridge):
    router, channel, _, _, path = bridge
    confirm(router)
    new = successor(channel.adapter)
    rebind(new, channel.adapter)
    with sqlite3.connect(path) as db:
        before = db.execute("SELECT * FROM entry_stop_panel_state").fetchall()
        db.execute(
            "CREATE TRIGGER fail_transition BEFORE UPDATE ON entry_stop_panel_meta BEGIN SELECT RAISE(ABORT,'test'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        router_rebind(bridge, new)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM entry_stop_panel_state").fetchall() == before
        assert db.execute("SELECT count(*) FROM entry_stop_panel_transitions").fetchone()[0] == 0
    assert router.snapshot()["entry_stop_bridge_configured"]


def test_legacy_permission_schema_is_rejected_without_backfill(setup):
    old, _, _ = setup
    old.apply_if_current(command())
    with old.engine.begin() as c:
        c.execute(text("ALTER TABLE motor_permission_commands DROP COLUMN binding_generation"))
    with pytest.raises(OperationalError):
        rebind(successor(old), old)
    with old.engine.connect() as c:
        assert "binding_generation" not in {
            r[1] for r in c.execute(text("PRAGMA table_info(motor_permission_commands)"))
        }


def test_router_catches_up_after_two_runtime_cuts_with_lost_expired_announcement(bridge):
    router, channel, requests, receipts, path = bridge
    confirm(router)
    old = channel.adapter
    first = successor(old)
    rebind(first, old)
    expired = first.recovery_announcement(router.auth)
    second = DurablePermissionAdapter(
        old.engine, replace(old.binding, instance_id="a" * 32), old.facts, clock=old.clock
    )
    rebind(second, first, 1)
    old.clock.value += 120000
    with pytest.raises(ControlError):
        router_rebind(bridge, first, announcement=expired, now_ms=old.clock())
    # Sólo el dueño actual puede volver a atestiguar el corte perdido.
    wire1 = second.recovery_announcement(router.auth, generation=1)
    restored1 = router_rebind(bridge, first, announcement=wire1, now_ms=old.clock())
    wire2 = second.recovery_announcement(router.auth, generation=2)
    restored2 = router_rebind(
        (restored1, channel, requests, receipts, path),
        second,
        announcement=wire2,
        expected_generation=1,
        now_ms=old.clock(),
    )
    restored2.handle_update(update(3, PREFIX + "status"), now_ms=old.clock())
    consume_once(requests, receipts, EntryStopChannel(second, router.auth))
    response = restored2.handle_update(update(4, PREFIX + "result:status"), now_ms=old.clock())
    assert "No se observó una parada" in response[-1]["payload"]["text"]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT generation,stop_count FROM entry_stop_panel_meta").fetchone() == (
            2,
            1,
        )
        assert db.execute("SELECT count(*) FROM entry_stop_panel_history").fetchone()[0] == 1
    for generation in (0, 3, True, -1):
        with pytest.raises(ControlError):
            second.recovery_announcement(router.auth, generation=generation)
    with pytest.raises(ControlError):
        first.recovery_announcement(router.auth, generation=1)


def test_router_rebind_serializes_against_inflight_mailbox_publication(bridge, monkeypatch):
    router, channel, requests, _, _ = bridge
    new = successor(channel.adapter)
    rebind(new, channel.adapter)
    entered, release, attempted = Event(), Event(), Event()
    original = requests.publish

    def publish(*args):
        entered.set()
        assert release.wait(5)
        original(*args)

    monkeypatch.setattr(requests, "publish", publish)

    def recover():
        attempted.set()
        return router_rebind(bridge, new)

    with ThreadPoolExecutor(2) as pool:
        pending = pool.submit(router.handle_update, update(1, PREFIX + "status"), now_ms=NOW)
        assert entered.wait(5)
        cut = pool.submit(recover)
        assert attempted.wait(5)
        release.set()
        pending.result()
        recovered = cut.result()
    assert recovered.identity["instance_id"] == new.binding.instance_id
    wire = requests.read("status")
    assert wire
    with pytest.raises(ControlError):
        router.handle_update(update(2, PREFIX + "status"), now_ms=NOW)
    assert requests.read("status") == wire


def test_cut_between_durable_intent_and_publish_keeps_unknown_without_mailbox_write(
    bridge, monkeypatch
):
    from contextlib import contextmanager

    router, channel, requests, _, path = bridge
    response = router.handle_update(update(1, PREFIX + "stop"), now_ms=NOW)
    callback = response[-1]["payload"]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    new = successor(channel.adapter)
    rebind(new, channel.adapter)
    original = router._connect
    calls = 0
    restored = []

    @contextmanager
    def connection():
        nonlocal calls
        calls += 1
        if calls == 2:
            restored.append(router_rebind(bridge, new))
        with original() as db:
            yield db

    monkeypatch.setattr(router, "_connect", connection)
    value = update(2, callback)
    with pytest.raises(ControlError, match="PANEL_CONFIG_CHANGED"):
        router.handle_update(value, now_ms=NOW)
    assert requests.read("stop") is None
    with sqlite3.connect(path) as db:
        assert (
            db.execute("SELECT publication FROM entry_stop_panel_history").fetchone()[0]
            == "PUBLISH_UNCONFIRMED"
        )
    assert "histórica" in restored[0].handle_update(value, now_ms=NOW)[-1]["payload"]["text"]
    assert requests.read("stop") is None
