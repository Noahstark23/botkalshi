"""Frontera de autorización/claim; adaptador CAS de prueba, sin red u órdenes."""

from __future__ import annotations

import ast
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event

import pytest

from src.control.channel import (
    Authenticator,
    CommandChannel,
    ControlError,
    Intent,
    Observation,
)

NOW = 1791845700000


class Clock:
    value = NOW

    def __call__(self):
        return self.value


class Adapter:
    """Sólo una tabla ficticia de permisos; CAS antes de cualquier efecto."""

    def __init__(self, clock):
        self.clock = clock
        self.calls = 0
        self.after_apply = None
        self.before_apply = None
        self.observe_error = False
        self.value = Observation(
            environment="demo",
            source_sha="a" * 40,
            instance_id="b" * 32,
            limits_sha256="c" * 64,
            scope="m1.entries",
            revision=0,
            observed_at_ms=clock(),
            boot_allowed=True,
            available=True,
            enabled=False,
            gates_clear=True,
            open_positions=0,
            resting_orders=0,
            uncertain_orders=0,
            dependents_active=False,
        )

    def observe(self, scope):
        if self.observe_error:
            raise RuntimeError("private adapter details must not leak")
        return replace(self.value)

    def apply_if_current(self, intent):
        self.calls += 1
        if self.before_apply:
            self.before_apply()
        if self.value.revision != intent.expected_revision or not self.value.gates_clear:
            raise ControlError("CAS_REJECTED")
        self.value = replace(
            self.value,
            revision=self.value.revision + 1,
            enabled=intent.enabled,
            applied_command_id=intent.command_id,
            observed_at_ms=self.clock(),
        )
        if self.after_apply:
            self.after_apply()


@pytest.fixture
def setup(tmp_path):
    clock = Clock()
    auth = Authenticator(b"x" * 32)
    channel = CommandChannel(
        tmp_path / "control.sqlite",
        auth=auth,
        actor_id=11,
        chat_id=22,
        environment="demo",
        clock=clock,
    )
    return channel, auth, Adapter(clock), clock


def intent(**changes):
    value = Intent(
        "d" * 32,
        11,
        22,
        "demo",
        "a" * 40,
        "b" * 32,
        "c" * 64,
        "m1.entries",
        True,
        0,
        NOW,
        NOW + 60000,
    )
    return replace(value, **changes)


def send(setup, operation, command=None, token=None):
    channel, auth, adapter, _ = setup
    command = command or intent()
    wire = auth.seal(
        "request", {"operation": operation, "intent": asdict(command), "confirmation": token}
    )
    return auth.open("response", channel.handle(wire, adapter=adapter))


def confirmed(setup, command=None):
    proposal = send(setup, "propose", command)
    result = send(setup, "confirm", command, proposal["confirmation"])
    assert result["state"] == "CONFIRMED"
    return result


def test_proposal_and_confirmation_never_apply(setup):
    channel, _, adapter, _ = setup
    p = send(setup, "propose")
    assert p["state"] == "PROPOSED" and adapter.calls == 0
    assert channel.consume(intent().command_id, adapter=adapter)["state"] == "PROPOSED"
    send(setup, "confirm", token=p["confirmation"])
    assert adapter.calls == 0 and not adapter.value.enabled
    assert channel.consume(intent().command_id, adapter=adapter)["state"] == "APPLIED"
    assert adapter.calls == 1 and adapter.value.enabled
    assert send(setup, "status")["state"] == "APPLIED"


@pytest.mark.parametrize(
    "field,value",
    [
        ("actor_id", 12),
        ("chat_id", 23),
        ("environment", "production"),
        ("source_sha", "e" * 40),
        ("instance_id", "e" * 32),
        ("limits_sha256", "e" * 64),
        ("expected_revision", 1),
        ("issued_at_ms", NOW + 1),
        ("expires_at_ms", NOW),
    ],
)
def test_wrong_owner_binding_revision_or_time_cannot_propose(setup, field, value):
    with pytest.raises(ControlError):
        send(setup, "propose", intent(**{field: value}))
    assert setup[2].calls == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"boot_allowed": False},
        {"gates_clear": False},
        {"available": False},
        {"uncertain_orders": 1},
        {"boot_allowed": None},
        {"enabled": 1},
        {"revision": True},
        {"open_positions": -1},
        {"resting_orders": 0.5},
        {"observed_at_ms": NOW - 30001},
        {"observed_at_ms": NOW + 1},
    ],
)
def test_enable_requires_known_fresh_runtime_observation(setup, changes):
    setup[2].value = replace(setup[2].value, **changes)
    with pytest.raises(ControlError):
        send(setup, "propose")
    assert setup[2].calls == 0


@pytest.mark.parametrize("scope", ["m2.exits", "m3.exits"])
@pytest.mark.parametrize("counter", ["open_positions", "resting_orders", "uncertain_orders"])
def test_cannot_disable_exit_protection_with_exposure(setup, scope, counter):
    setup[2].value = replace(setup[2].value, scope=scope, enabled=True, **{counter: 1})
    with pytest.raises(ControlError, match="EXIT_PROTECTION_REQUIRED"):
        send(setup, "propose", intent(scope=scope, enabled=False))


def test_fair_dependency_is_preserved(setup):
    setup[2].value = replace(setup[2].value, scope="m2.fair", enabled=True, dependents_active=True)
    with pytest.raises(ControlError, match="FAIR_DEPENDENCY_ACTIVE"):
        send(setup, "propose", intent(scope="m2.fair", enabled=False))


def test_disabling_entry_does_not_need_to_clear_kill_or_unknown(setup):
    channel, _, adapter, _ = setup
    adapter.value = replace(
        adapter.value, enabled=True, boot_allowed=False, gates_clear=False, uncertain_orders=1
    )
    command = intent(enabled=False)
    confirmed(setup, command)
    # Adaptador de prueba permite pausa, conservando gates/reservas.
    adapter.apply_if_current = lambda i: setattr(
        adapter,
        "value",
        replace(adapter.value, enabled=False, revision=1, applied_command_id=i.command_id),
    )
    assert channel.consume(command.command_id, adapter=adapter)["state"] == "APPLIED"
    assert adapter.value.uncertain_orders == 1 and not adapter.value.gates_clear


def test_confirmation_is_bound_to_exact_intent(setup):
    p = send(setup, "propose")
    other = intent(command_id="e" * 32, scope="m2.entries")
    setup[2].value = replace(setup[2].value, scope=other.scope)
    send(setup, "propose", other)
    with pytest.raises(ControlError, match="INVALID_CONFIRMATION"):
        send(setup, "confirm", other, p["confirmation"])
    assert setup[2].calls == 0


def test_modified_payload_under_same_id_is_rejected(setup):
    send(setup, "propose")
    with pytest.raises(ControlError, match="COMMAND_ID_CONFLICT"):
        send(setup, "propose", intent(enabled=False))


@pytest.mark.parametrize("phase", ["confirm", "consume"])
@pytest.mark.parametrize("change", ["revision", "gates", "instance", "expired"])
def test_revalidates_at_confirmation_and_consumption(setup, phase, change):
    channel, _, adapter, clock = setup
    p = send(setup, "propose")
    if phase == "consume":
        send(setup, "confirm", token=p["confirmation"])
    if change == "revision":
        adapter.value = replace(adapter.value, revision=1)
    elif change == "gates":
        adapter.value = replace(adapter.value, gates_clear=False)
    elif change == "instance":
        adapter.value = replace(adapter.value, instance_id="f" * 32)
    else:
        clock.value += 60000
    if phase == "confirm":
        with pytest.raises(ControlError):
            send(setup, "confirm", token=p["confirmation"])
    else:
        assert channel.consume(intent().command_id, adapter=adapter)["state"] == "BLOCKED"
    assert adapter.calls == 0


def test_ack_loss_is_unknown_and_never_reapplied_then_reconciles(setup):
    channel, _, adapter, _ = setup
    confirmed(setup)

    def lose_ack():
        raise TimeoutError("private detail")

    adapter.after_apply = lose_ack
    result = channel.consume(intent().command_id, adapter=adapter)
    assert result["state"] == "UNKNOWN" and "private" not in str(result)
    for _ in range(3):
        assert channel.consume(intent().command_id, adapter=adapter)["state"] == "UNKNOWN"
    assert adapter.calls == 1
    assert channel.reconcile(intent().command_id, adapter=adapter)["state"] == "APPLIED"
    assert adapter.calls == 1


@pytest.mark.parametrize("wrong", ["id", "revision", "enabled", "instance", "stale"])
def test_apply_return_alone_cannot_prove_effect(setup, wrong):
    channel, _, adapter, _ = setup
    confirmed(setup)
    changes = {
        "id": {"applied_command_id": "f" * 32},
        "revision": {"revision": 2},
        "enabled": {"enabled": False},
        "instance": {"instance_id": "f" * 32},
        "stale": {"observed_at_ms": NOW - 30001},
    }
    adapter.after_apply = lambda: setattr(
        adapter, "value", replace(adapter.value, **changes[wrong])
    )
    assert channel.consume(intent().command_id, adapter=adapter)["state"] == "UNKNOWN"
    assert channel.reconcile(intent().command_id, adapter=adapter)["state"] == "UNKNOWN"
    assert adapter.calls == 1


def test_quote_pause_requires_observed_withdrawal(setup):
    channel, _, adapter, _ = setup
    adapter.value = replace(adapter.value, scope="m5.quotes", enabled=True, resting_orders=1)
    command = intent(scope="m5.quotes", enabled=False)
    confirmed(setup, command)
    assert channel.consume(command.command_id, adapter=adapter)["state"] == "UNKNOWN"
    adapter.value = replace(adapter.value, resting_orders=0)
    assert channel.reconcile(command.command_id, adapter=adapter)["state"] == "APPLIED"
    assert adapter.calls == 1


def test_crash_after_claim_survives_reopen_without_reexecution(setup):
    channel, auth, adapter, clock = setup
    confirmed(setup)

    def crash():
        raise asyncio.CancelledError

    adapter.before_apply = crash
    with pytest.raises(asyncio.CancelledError):
        channel.consume(intent().command_id, adapter=adapter)
    reopened = CommandChannel(
        channel.path, auth=auth, actor_id=11, chat_id=22, environment="demo", clock=clock
    )
    assert reopened.consume(intent().command_id, adapter=adapter)["state"] == "CLAIMED"
    assert reopened.reconcile(intent().command_id, adapter=adapter)["state"] == "UNKNOWN"
    assert adapter.calls == 1 and not adapter.value.enabled


def test_two_consumers_share_one_durable_claim(setup):
    channel, auth, adapter, clock = setup
    confirmed(setup)
    second = CommandChannel(
        channel.path, auth=auth, actor_id=11, chat_id=22, environment="demo", clock=clock
    )
    entered, release = Event(), Event()

    def block():
        entered.set()
        assert release.wait(3)

    adapter.before_apply = block
    with ThreadPoolExecutor(2) as executor:
        one = executor.submit(channel.consume, intent().command_id, adapter=adapter)
        assert entered.wait(3)
        two = executor.submit(second.consume, intent().command_id, adapter=adapter)
        assert two.result(timeout=3)["state"] == "CLAIMED"
        release.set()
        assert one.result(timeout=3)["state"] == "APPLIED"
    assert adapter.calls == 1


def test_two_proposals_same_revision_cannot_both_apply(setup):
    channel, _, adapter, _ = setup
    one, two = intent(), intent(command_id="e" * 32)
    confirmed(setup, one)
    confirmed(setup, two)
    assert channel.consume(one.command_id, adapter=adapter)["state"] == "APPLIED"
    assert channel.consume(two.command_id, adapter=adapter)["state"] == "BLOCKED"
    assert adapter.calls == 1


def test_runtime_cas_rechecks_race_after_channel_preflight(setup):
    channel, _, adapter, _ = setup
    confirmed(setup)
    adapter.before_apply = lambda: setattr(
        adapter, "value", replace(adapter.value, gates_clear=False)
    )
    assert channel.consume(intent().command_id, adapter=adapter)["state"] == "UNKNOWN"
    assert not adapter.value.enabled


def test_clock_progress_during_apply_uses_new_observation_time(setup):
    channel, _, adapter, clock = setup
    confirmed(setup)
    adapter.before_apply = lambda: setattr(clock, "value", clock.value + 1000)
    assert channel.consume(intent().command_id, adapter=adapter)["state"] == "APPLIED"


def test_confirmation_expiring_during_observation_is_rejected(setup):
    _, _, adapter, clock = setup
    p = send(setup, "propose")

    def delayed_observation(scope):
        clock.value += 60000
        return replace(adapter.value, observed_at_ms=clock.value)

    adapter.observe = delayed_observation
    with pytest.raises(ControlError, match="EXPIRED_OR_FUTURE"):
        send(setup, "confirm", token=p["confirmation"])
    assert adapter.calls == 0


def test_failure_to_commit_claim_never_calls_adapter(setup, monkeypatch):
    from sqlmodel import Session

    channel, _, adapter, _ = setup
    confirmed(setup)
    original = Session.commit

    def fail_claim(session):
        if any(getattr(row, "state", None) == "CLAIMED" for row in session.dirty):
            raise OSError("disk full")
        return original(session)

    monkeypatch.setattr(Session, "commit", fail_claim)
    with pytest.raises(OSError):
        channel.consume(intent().command_id, adapter=adapter)
    assert adapter.calls == 0
    assert send(setup, "status")["state"] == "CONFIRMED"


def test_failure_after_effect_preserves_claim_and_allows_only_reconciliation(setup, monkeypatch):
    from sqlmodel import Session

    channel, _, adapter, _ = setup
    confirmed(setup)
    original = Session.commit

    def fail_receipt(session):
        if any(getattr(row, "state", None) == "APPLIED" for row in session.dirty):
            raise OSError("disk full")
        return original(session)

    monkeypatch.setattr(Session, "commit", fail_receipt)
    with pytest.raises(OSError):
        channel.consume(intent().command_id, adapter=adapter)
    assert adapter.calls == 1 and adapter.value.enabled
    assert channel.consume(intent().command_id, adapter=adapter)["state"] == "CLAIMED"
    monkeypatch.setattr(Session, "commit", original)
    assert channel.reconcile(intent().command_id, adapter=adapter)["state"] == "APPLIED"
    assert adapter.calls == 1


def test_expired_history_can_be_read_without_new_effect(setup):
    channel, _, adapter, clock = setup
    confirmed(setup)
    channel.consume(intent().command_id, adapter=adapter)
    clock.value += 100000
    assert send(setup, "status")["state"] == "APPLIED"
    assert send(setup, "propose")["state"] == "APPLIED"
    assert adapter.calls == 1


def test_clock_regression_and_journal_capacity_preserve_history(tmp_path):
    clock, auth = Clock(), Authenticator(b"x" * 32)
    channel = CommandChannel(
        tmp_path / "c.db",
        auth=auth,
        actor_id=11,
        chat_id=22,
        environment="demo",
        max_commands=1,
        clock=clock,
    )
    adapter = Adapter(clock)
    setup = channel, auth, adapter, clock
    confirmed(setup)
    with pytest.raises(ControlError, match="JOURNAL_FULL"):
        send(setup, "propose", intent(command_id="f" * 32))
    assert channel.consume(intent().command_id, adapter=adapter)["state"] == "APPLIED"
    clock.value -= 1
    with pytest.raises(ControlError, match="CLOCK_REGRESSION"):
        send(setup, "status")
    clock.value += 1
    assert send(setup, "status")["state"] == "APPLIED"


def test_authentication_domain_payload_and_owner_checks(setup):
    channel, auth, adapter, _ = setup
    body = {"operation": "propose", "intent": asdict(intent()), "confirmation": None}
    for wire in (
        Authenticator(b"y" * 32).seal("request", body),
        auth.seal("response", body),
        b"{" + b" " * 8192 + b"}",
        b"{}",
        b'{"body":{},"body":{},"mac":"x"}',
    ):
        with pytest.raises(ControlError):
            channel.handle(wire, adapter=adapter)
    wire = json.loads(auth.seal("request", body))
    wire["body"]["intent"]["enabled"] = False
    with pytest.raises(ControlError, match="UNAUTHENTICATED"):
        channel.handle(json.dumps(wire).encode(), adapter=adapter)
    assert adapter.calls == 0
    assert "xxxxxxxx" not in repr(auth)


def test_no_remote_consume_or_freeform_actions(setup):
    with pytest.raises(ControlError, match="INVALID_OPERATION"):
        send(setup, "consume")
    with pytest.raises(ControlError, match="INVALID_ACTION"):
        send(setup, "propose", intent(scope="shell"))
    with pytest.raises(ControlError, match="INVALID_ACTION"):
        send(setup, "propose", intent(enabled="true"))


def test_observation_errors_do_not_leak_private_details(setup):
    setup[2].observe_error = True
    with pytest.raises(ControlError, match="OBSERVATION_UNAVAILABLE") as result:
        send(setup, "propose")
    assert "private" not in str(result.value)


def test_config_and_file_replacement_are_not_silent_reset(setup, tmp_path):
    channel, auth, adapter, clock = setup
    send(setup, "propose")
    with pytest.raises(ControlError, match="CONFIGURATION_MISMATCH"):
        CommandChannel(
            channel.path, auth=auth, actor_id=12, chat_id=22, environment="demo", clock=clock
        )
    channel.path.rename(tmp_path / "preserved.db")
    channel.path.write_bytes(b"")
    channel.path.chmod(0o600)
    with pytest.raises(ControlError, match="DATABASE_REPLACED"):
        channel.consume(intent().command_id, adapter=adapter)


def test_foreign_db_and_symlink_are_rejected(tmp_path):
    auth = Authenticator(b"x" * 32)
    kwargs = {"auth": auth, "actor_id": 11, "chat_id": 22, "environment": "demo"}
    foreign = tmp_path / "foreign.db"
    foreign.touch(mode=0o600)
    with pytest.raises(ControlError, match="FOREIGN_DATABASE"):
        CommandChannel(foreign, **kwargs)
    link = tmp_path / "link.db"
    link.symlink_to(foreign)
    with pytest.raises(ControlError, match="INVALID_DATABASE"):
        CommandChannel(link, **kwargs)


def test_new_journal_is_dedicated_and_has_no_financial_or_network_imports(setup):
    from sqlalchemy import inspect

    from src.control import channel as module

    assert set(inspect(setup[0].engine).get_table_names()) == {
        "motor_channel_meta",
        "motor_channel_commands",
    }
    tree = ast.parse(Path(module.__file__).read_text())
    imported = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert not any(
        name.startswith(("src.clients", "src.strategies", "src.risk", "src.storage"))
        for name in imported
    )
