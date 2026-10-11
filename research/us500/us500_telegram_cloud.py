"""Isolated persistent receiver: Telegram, runtime GET status and paper permission.

No financial client, runner, shell command, deployment API or real flag writer.
Run only after stopping the previous consumer and migrating its two SQLite DBs.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import stat
import threading
from pathlib import Path

from motor_status_readonly import RuntimeStatusReader
from us500_control import PaperControl
from us500_telegram import TelegramTransport
from us500_telegram_service import TelegramPaperService


class PreflightTransient(ValueError):
    """Retryable connection/availability failure, without raw provider text."""


def load_credential(path, *, bot_id, owner_chat_id, connection_factory=None):
    """Credential never enters arguments, environment, logs or state databases."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in {0, os.getuid()}
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > 256
        ):
            raise ValueError("CREDENTIAL_UNAVAILABLE")
        token = source.read(257).decode("ascii").strip()
    transport = TelegramTransport(token, owner_chat_id=owner_chat_id)
    if transport.bot_id != bot_id:
        raise ValueError("BOT_IDENTITY_CHANGED")
    factory = connection_factory or http.client.HTTPSConnection
    for method in ("getMe", "getWebhookInfo"):
        connection = factory("api.telegram.org", timeout=7)
        try:
            connection.request("GET", f"/bot{token}/{method}")
            response = connection.getresponse()
            if response.status == 429 or response.status >= 500:
                raise PreflightTransient("PREFLIGHT_TEMPORARILY_UNAVAILABLE")
            raw = response.read(65537)
            if response.status != 200 or len(raw) > 65536:
                raise ValueError("PREFLIGHT_UNAVAILABLE")
            data = json.loads(raw)
            if type(data) is not dict or data.get("ok") is not True:
                raise ValueError("PREFLIGHT_UNAVAILABLE")
            result = data.get("result")
            if type(result) is not dict:
                raise ValueError("PREFLIGHT_UNAVAILABLE")
            if method == "getMe" and (
                type(result.get("id")) is not int
                or result["id"] != bot_id
                or result.get("is_bot") is not True
            ):
                raise ValueError("BOT_IDENTITY_CHANGED")
            if method == "getWebhookInfo" and result.get("url") != "":
                raise ValueError("WEBHOOK_PRESENT_OR_UNKNOWN")
        except (OSError, http.client.HTTPException):
            raise PreflightTransient("PREFLIGHT_TEMPORARILY_UNAVAILABLE") from None
        finally:
            connection.close()
    return token


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--bot-id", type=int, required=True)
    parser.add_argument("--owner-id", type=int, required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    try:
        # Refuse an accidental fresh DB on migration/reboot. Existing identity
        # and cursor are part of the deployment, not recreated by startup.
        for name in ("control.sqlite", "outbox.sqlite"):
            path = args.state_dir / name
            if not path.is_file() or path.is_symlink():
                raise ValueError("MIGRATED_STATE_REQUIRED")
        control = PaperControl(
            args.state_dir / "control.sqlite",
            owner_user_id=args.owner_id,
            owner_chat_id=args.owner_id,
            status_provider=RuntimeStatusReader(),
        )
        preflight_transient = False

        def credential():
            nonlocal preflight_transient
            try:
                return load_credential(
                    args.credential_file,
                    bot_id=args.bot_id,
                    owner_chat_id=args.owner_id,
                )
            except PreflightTransient:
                preflight_transient = True
                raise

        service = TelegramPaperService(
            args.state_dir,
            control=control,
            credential_provider=credential,
        )
        result = service.run(None, 3, stop, persistent=True)
        print(json.dumps(result, sort_keys=True), flush=True)
        if (
            preflight_transient
            and result.get("paper_pause_confirmed") is True
            and result.get("errors") == ["CREDENTIAL_OR_TRANSPORT_UNAVAILABLE"]
        ):
            return 75
        # Explicit fatal configuration/state faults require intervention. A
        # process crash still receives systemd's delayed restart policy.
        return 0 if result["phase"] == "STOPPED" else 78
    except Exception:
        print(
            '{"error":"CLOUD_RECEIVER_START_FAILED","production_control_available":false}',
            flush=True,
        )
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
