"""Contrato con el panel original fijado por hashes, sin token ni Telegram real."""

import hashlib
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.control.entry_stop import BINDING_FIELDS, EntryStopChannel
from src.control.stop_mailbox import StopMailbox, consume_once
from src.control.telegram_entry_stop import PREFIX, TelegramEntryStopRouter
from src.control.wire import Authenticator, ControlError
from tests.control.test_permissions import NOW
from tests.control.test_permissions import setup as permission_setup
from tests.control.test_stop_bridge import update

setup = permission_setup


def test_panel_modules_are_stdlib_only():
    code = "import sys; sys.path.insert(0,sys.argv[1]); import src.control.telegram_entry_stop; assert 'sqlalchemy' not in sys.modules; assert not any(m.startswith('src.storage') for m in sys.modules)"
    subprocess.run(
        [sys.executable, "-I", "-c", code, str(Path(__file__).resolve().parents[2])],
        check=True,
    )


def test_exact_panel_single_consumer_stop_roundtrip(tmp_path, setup, request, monkeypatch):
    source = request.config.getoption("--panel-source")
    if source is None:
        pytest.skip("La integración exige fuente exacta del panel; CI la proporciona")
    panel = Path(source).absolute()
    hashes = json.loads(Path(__file__).with_name("panel_22522c3_hashes.json").read_text())
    for name, expected in hashes.items():
        assert hashlib.sha256((panel / name).read_bytes()).hexdigest() == expected
    monkeypatch.syspath_prepend(str(panel))
    paper = importlib.import_module("us500_control")
    telegram = importlib.import_module("us500_telegram")
    adapter, _, _ = setup
    temp = tmp_path / "integration"
    temp.mkdir(mode=0o700)
    for name in ("requests", "receipts"):
        (temp / name).mkdir(mode=0o700)
    requests = StopMailbox(temp / "requests", owner_uid=os.geteuid())
    receipts = StopMailbox(temp / "receipts", owner_uid=os.geteuid())
    auth = Authenticator(b"q" * 32)
    control = paper.PaperControl(temp / "paper.db", owner_user_id=11, owner_chat_id=22)
    identity = {key: getattr(adapter.binding, key) for key in BINDING_FIELDS}
    router = TelegramEntryStopRouter(
        temp / "bridge.db",
        control=control,
        identity=identity,
        auth=auth,
        requests=requests,
        receipts=receipts,
    )
    baseline = control.snapshot()
    with pytest.raises(ControlError, match="PRODUCTION_BRIDGE_NOT_SUPPORTED"):
        TelegramEntryStopRouter(
            temp / "forbidden.db",
            control=control,
            identity={**identity, "environment": "production"},
            auth=auth,
            requests=requests,
            receipts=receipts,
        )

    class FakeTelegram:
        bot_id = 123
        inbox = []
        calls = []
        fail = False

        def call(self, method, payload):
            self.calls.append((method, payload))
            telegram._payload(method, payload, 22)  # validación real de wire/teclados
            if method == "getUpdates":
                batch, self.inbox = self.inbox, []
                return {"ok": True, "result": batch}
            if method == "answerCallbackQuery":
                return {"ok": True, "result": True}
            if self.fail:
                raise telegram.TelegramError("TELEGRAM_RESPONSE_UNKNOWN")
            return {"ok": True, "result": {"message_id": 1, "chat": {"id": 22}}}

    transport = FakeTelegram()
    receiver = telegram.PaperTelegram(temp / "outbox.db", control=router, transport=transport)
    try:

        def poll(value):
            transport.inbox = [value]
            return receiver.poll_once(now_ms=NOW)

        assert poll(update(1))["poll_status"] == "COMPLETE"
        assert poll(update(2, PREFIX + "stop"))["poll_status"] == "COMPLETE"
        message = [p for m, p in transport.calls if m == "sendMessage"][-1]
        confirmation = message["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        assert poll(update(3, confirmation))["poll_status"] == "COMPLETE"
        original = requests.read("stop")
        assert (
            consume_once(requests, receipts, EntryStopChannel(adapter, auth))["stop"]
            == "RECEIPT_WRITTEN"
        )
        assert poll(update(4, PREFIX + "result:stop"))["poll_status"] == "COMPLETE"
        assert (
            "Nuevas entradas bloqueadas"
            in [p for m, p in transport.calls if m == "sendMessage"][-1]["text"]
        )
        assert poll(update(3, confirmation))["poll_status"] == "COMPLETE"
        assert requests.read("stop") == original
        after = control.snapshot()
        assert (after["paper_enabled"], after["revision"], after["control_id"]) == (
            baseline["paper_enabled"],
            baseline["revision"],
            baseline["control_id"],
        )
        assert receiver.snapshot()["next_offset"] == 5
        # Una entrega Telegram incierta permanece UNKNOWN y nunca se repite.
        transport.fail = True
        poll(update(5, PREFIX + "status"))
        assert receiver.snapshot()["outbox_counts"].get("UNKNOWN") == 1
        sent = len([m for m, _ in transport.calls if m == "sendMessage"])
        transport.inbox = []
        receiver.poll_once(now_ms=NOW)
        assert len([m for m, _ in transport.calls if m == "sendMessage"]) == sent
        assert len([m for m, _ in transport.calls if m == "getUpdates"]) == 7
    finally:
        receiver.close()
