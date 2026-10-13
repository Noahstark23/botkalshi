"""Telegram ficticio → spool temporal → ledger real temporal → recibo firmado."""

import json
import os
import sqlite3
from dataclasses import asdict

import pytest

from src.control.entry_stop import BINDING_FIELDS, EntryStopChannel
from src.control.stop_mailbox import StopMailbox, consume_once
from src.control.telegram_entry_stop import PREFIX, TelegramEntryStopRouter
from src.control.wire import Authenticator, ControlError
from tests.control.test_entry_stop import request
from tests.control.test_permissions import NOW
from tests.control.test_permissions import setup as permission_setup

setup = permission_setup


class PaperDelegate:
    owner_user_id, owner_chat_id = 11, 22
    calls = 0

    def snapshot(self):
        return {"mode": "paper", "scope": "US500", "production_control_available": False}

    def handle_update(self, update, **kwargs):
        self.calls += 1
        return [{"method": "sendMessage", "payload": {"chat_id": 22, "text": "Panel US500"}}]

    def pause_for_fault(self, reason, **kwargs):
        return reason

    def set_transport_available(self, available):
        self.available = available


def update(number, action=None, **changes):
    message = {"chat": {"id": 22, "type": "private"}, "date": NOW // 1000}
    actor = {"id": 11, "is_bot": False}
    if action is None:
        value = {"update_id": number, "message": {**message, "from": actor, "text": "/motores"}}
    else:
        value = {
            "update_id": number,
            "callback_query": {
                "id": f"cb-{number}",
                "from": actor,
                "message": message,
                "data": action,
            },
        }
    value.update(changes)
    return value


@pytest.fixture
def bridge(tmp_path, setup):
    adapter, _, _ = setup
    for name in ("requests", "receipts", "panel"):
        (tmp_path / name).mkdir(mode=0o700)
    requests = StopMailbox(tmp_path / "requests", owner_uid=os.geteuid())
    receipts = StopMailbox(tmp_path / "receipts", owner_uid=os.geteuid())
    auth = Authenticator(b"x" * 32)
    delegate = PaperDelegate()
    identity = {key: getattr(adapter.binding, key) for key in BINDING_FIELDS}
    path = tmp_path / "panel" / "router.db"
    router = TelegramEntryStopRouter(
        path, control=delegate, identity=identity, auth=auth, requests=requests, receipts=receipts
    )
    return router, EntryStopChannel(adapter, auth), requests, receipts, path


def confirm(router):
    response = router.handle_update(update(1, PREFIX + "stop"), now_ms=NOW)
    button = response[-1]["payload"]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert len(button.encode()) <= 64
    value = update(2, button)
    return value, router.handle_update(value, now_ms=NOW)


def test_single_consumer_roundtrip_and_duplicate_confirmation(bridge):
    router, channel, requests, receipts, _ = bridge
    value, response = confirm(router)
    assert "pendiente" in response[-1]["payload"]["text"]
    wire = requests.read("stop")
    assert wire and not channel.adapter.entry_stop(request(operation="status")).latched
    assert router.handle_update(value, now_ms=NOW) == response
    assert requests.read("stop") == wire
    assert consume_once(requests, receipts, channel)["stop"] == "RECEIPT_WRITTEN"
    result = router.handle_update(update(3, PREFIX + "result:stop"), now_ms=NOW)
    assert "Nuevas entradas bloqueadas" in result[-1]["payload"]["text"]
    assert "no confirma cancelaciones" in result[-1]["payload"]["text"]
    assert router.control.calls == 0
    # El nonce ya usado en otro update no publica ni reemplaza el pedido.
    value["update_id"] = 4
    assert "ya usada" in router.handle_update(value, now_ms=NOW)[-1]["payload"]["text"]
    assert requests.read("stop") == wire


def test_pending_receipt_expiry_new_query_and_restart(bridge):
    router, channel, requests, receipts, path = bridge
    value, response = confirm(router)
    assert (
        "UNKNOWN"
        in router.handle_update(update(3, PREFIX + "result:stop"), now_ms=NOW)[-1]["payload"][
            "text"
        ]
    )
    consume_once(requests, receipts, channel)
    assert (
        "UNKNOWN"
        in router.handle_update(update(4, PREFIX + "result:stop"), now_ms=NOW + 60000)[-1][
            "payload"
        ]["text"]
    )
    reopened = TelegramEntryStopRouter(
        path,
        control=router.control,
        identity=dict(router.identity),
        auth=router.auth,
        requests=requests,
        receipts=receipts,
    )
    assert reopened.handle_update(value, now_ms=NOW + 60000) == response
    channel.adapter.clock.value = NOW + 60000
    reopened.handle_update(update(5, PREFIX + "status"), now_ms=NOW + 60000)
    consume_once(requests, receipts, channel)
    assert (
        "bloqueadas"
        in reopened.handle_update(update(6, PREFIX + "result:status"), now_ms=NOW + 60000)[-1][
            "payload"
        ]["text"]
    )


def test_failed_publication_does_not_retry_update_or_claim_success(bridge, monkeypatch):
    router, _, requests, _, _ = bridge
    calls = []

    def fail(*args):
        calls.append(args)
        raise OSError("private details")

    monkeypatch.setattr(requests, "publish", fail)
    value, response = confirm(router)
    assert router.handle_update(value, now_ms=NOW) == response and len(calls) == 1
    assert "private" not in str(response)
    assert (
        "UNKNOWN"
        in router.handle_update(update(3, PREFIX + "result:stop"), now_ms=NOW)[-1]["payload"][
            "text"
        ]
    )


@pytest.mark.parametrize("change", ["owner", "chat", "forward", "inline", "bot", "bool_id"])
def test_unauthorized_updates_do_not_touch_spool(bridge, change):
    router, _, requests, _, _ = bridge
    value = update(1, PREFIX + "stop")
    query = value["callback_query"]
    if change == "owner":
        query["from"]["id"] = 33
    if change == "chat":
        query["message"]["chat"]["type"] = "group"
    if change == "forward":
        query["message"]["forward_date"] = 1
    if change == "inline":
        query["inline_message_id"] = "inline"
    if change == "bot":
        query["from"]["is_bot"] = True
    if change == "bool_id":
        query["from"]["id"] = True
    router.handle_update(value, now_ms=NOW)
    assert requests.read("stop") is None
    with sqlite3.connect(router.path) as db:
        assert db.execute("SELECT nonce_hash FROM entry_stop_panel_state").fetchone()[0] is None


def test_menus_preserve_paper_contract_and_never_offer_enable(bridge):
    router, _, _, _, _ = bridge
    responses = router.handle_update(update(1), now_ms=NOW)
    assert responses[0]["payload"]["text"] == "Panel US500"
    assert len(responses) == 1
    assert (
        responses[0]["payload"]["reply_markup"]["inline_keyboard"][-1][0]["callback_data"]
        == PREFIX + "stop"
    )
    assert router.snapshot()["production_control_available"] is False
    assert router.pause_for_fault("TELEGRAM_UNAVAILABLE", now_ms=NOW) == "TELEGRAM_UNAVAILABLE"
    router.set_transport_available(False)
    assert router.control.available is False


def test_conflicting_update_and_binding_fail_closed(bridge):
    router, _, requests, receipts, path = bridge
    router.handle_update(update(1, PREFIX + "stop"), now_ms=NOW)
    with pytest.raises(ControlError, match="UPDATE_ID_CONFLICT"):
        router.handle_update(update(1, PREFIX + "status"), now_ms=NOW)
    with pytest.raises(ControlError, match="PANEL_CONFIG_CHANGED"):
        TelegramEntryStopRouter(
            path,
            control=router.control,
            identity={**router.identity, "instance_id": "e" * 32},
            auth=router.auth,
            requests=requests,
            receipts=receipts,
        )


def test_mailbox_integrity_bounds_and_crash_residue(bridge):
    _, channel, requests, receipts, _ = bridge
    wire = channel.auth.seal("entry-stop-request-v1", asdict(request()))
    requests.publish("stop", wire)
    assert requests.read("stop") == wire
    # Un sobre firmado de parada en el slot de consulta no aplica una parada.
    requests.publish("status", wire)
    assert consume_once(requests, receipts, channel)["status"] == "UNCONFIRMED"
    temporary = requests.path / ".stop.json.pending"
    temporary.write_bytes(b"uncertain")
    with pytest.raises(FileExistsError):
        requests.publish("stop", wire)
    assert temporary.read_bytes() == b"uncertain"
    assert requests.read("stop") == wire
    with pytest.raises(ControlError):
        requests.publish("../../other", wire)
    with pytest.raises(ControlError, match="INVALID_ENVELOPE"):
        requests.publish("stop", b"x" * 8193)


def test_mailbox_rejects_symlinks_insecure_permissions_and_modified_receipts(bridge, tmp_path):
    router, channel, requests, receipts, _ = bridge
    confirm(router)
    consume_once(requests, receipts, channel)
    data = json.loads(receipts.read("stop"))
    data["body"]["status"]["latched"] = False
    receipts.publish("stop", json.dumps(data).encode())
    assert (
        "UNKNOWN"
        in router.handle_update(update(3, PREFIX + "result:stop"), now_ms=NOW)[-1]["payload"][
            "text"
        ]
    )
    os.chmod(requests.path, 0o777)
    with pytest.raises(ControlError, match="INSECURE_MAILBOX_DIRECTORY"):
        requests.read("stop")
    directory = tmp_path / "sym"
    directory.symlink_to(receipts.path, target_is_directory=True)
    with pytest.raises(ControlError):
        StopMailbox(directory, owner_uid=os.geteuid())


def test_new_confirmation_never_overwrites_unknown_stop(bridge):
    router, _, requests, _, path = bridge
    confirm(router)
    first = requests.read("stop")
    next_response = router.handle_update(update(3, PREFIX + "stop"), now_ms=NOW)
    button = next_response[-1]["payload"]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    message = router.handle_update(update(4, button), now_ms=NOW)[-1]["payload"]["text"]
    assert "Ya existe" in message
    assert requests.read("stop") == first
    with sqlite3.connect(path) as db:
        retained = json.loads(
            db.execute("SELECT stop_request FROM entry_stop_panel_state").fetchone()[0]
        )
    assert retained == router.auth.open("entry-stop-request-v1", first)


def test_expired_slot_never_calls_ledger_again(bridge, monkeypatch):
    router, channel, requests, receipts, _ = bridge
    confirm(router)
    consume_once(requests, receipts, channel)
    channel.adapter.clock.value = NOW + 60000
    calls = []
    original = channel.adapter.entry_stop

    def observed(value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(channel.adapter, "entry_stop", observed)
    assert consume_once(requests, receipts, channel)["stop"] == "EXPIRED"
    assert calls == []


def test_failed_publication_is_durably_degraded(bridge, monkeypatch):
    router, _, requests, _, _ = bridge

    def fail(*_):
        raise OSError("not copied")

    monkeypatch.setattr(requests, "publish", fail)
    confirm(router)
    state = router.snapshot()
    assert state["entry_stop_control_available"] is None
    assert state["entry_stop_publication"] == "PUBLISH_UNCONFIRMED"
    result = router.handle_update(update(3, PREFIX + "result:stop"), now_ms=NOW)
    assert "publicación" in result[-1]["payload"]["text"]


def test_expired_unconsumed_stop_can_be_replaced_without_erasing_history(bridge):
    router, channel, requests, receipts, path = bridge
    confirm(router)
    first = router.auth.open("entry-stop-request-v1", requests.read("stop"))
    later = NOW + 60000
    channel.adapter.clock.value = later
    assert consume_once(requests, receipts, channel)["stop"] == "EXPIRED"
    response = router.handle_update(update(3, PREFIX + "stop"), now_ms=later)
    button = response[-1]["payload"]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    router.handle_update(update(4, button), now_ms=later)
    second = router.auth.open("entry-stop-request-v1", requests.read("stop"))
    assert first["request_id"] != second["request_id"]
    assert consume_once(requests, receipts, channel)["stop"] == "RECEIPT_WRITTEN"
    with sqlite3.connect(path) as db:
        retained = [
            json.loads(row[0])
            for row in db.execute("SELECT request FROM entry_stop_panel_history ORDER BY rowid")
        ]
    assert retained == [first, second]


def test_stop_history_loss_blocks_without_repair(bridge):
    router, _, _, _, path = bridge
    confirm(router)
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM entry_stop_panel_history")
    with pytest.raises(ControlError, match="STOP_HISTORY_MISMATCH"):
        router.snapshot()


def test_stop_history_capacity_preserves_uncertainty(bridge, monkeypatch):
    import src.control.telegram_entry_stop as module

    router, _, requests, _, path = bridge
    confirm(router)
    original = requests.read("stop")
    monkeypatch.setattr(module, "MAX_STOP_REQUESTS", 1)
    later = NOW + 60000
    response = router.handle_update(update(3, PREFIX + "stop"), now_ms=later)
    button = response[-1]["payload"]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    result = router.handle_update(update(4, button), now_ms=later)
    assert "Historial de paradas lleno" in result[-1]["payload"]["text"]
    assert requests.read("stop") == original
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM entry_stop_panel_history").fetchone()[0] == 1
