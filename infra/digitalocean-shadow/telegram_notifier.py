"""Bounded Telegram notifications for the read-only research service.

The notifier reads only locally verified technical status and a small,
allow-listed assessment summary.  It cannot place or cancel orders, move
money, run shell commands, or grant execution authority.

Delivery is at-least-once across ambiguous network failures: if Telegram
accepts a message but the HTTPS response is lost, no success state is written
and a later run may retry the same message.  Confirmed responses are
deduplicated persistently and concurrent invocations are locked out.

With ``--state-data``, assistant control/assessment input is read from its
``assistant`` child while Telegram owns only the sibling ``telegram`` child.
Without it, the legacy ``DATA/assistant`` layout remains available.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import stat
import sys
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from assistant_bridge import (
    ALLOWED_DECISIONS,
    BridgeError,
    EXECUTION_NAMES,
    KALSHI_SECRET_NAMES,
    build_status,
    current_control,
    latest_assessment,
)
from cycle_verifier import CycleVerificationError
from reporting import EvidenceError, read_object, timestamp


TELEGRAM_API_ROOT = "https://api.telegram.org"
STATE_SCHEMA = "botkalshi-telegram-state-v2"
MAX_MESSAGE_CHARS = 4096
MAX_RESPONSE_BYTES = 131_072
MAX_BLOCKERS = 64
MAX_ASSESSMENT_AGE_SECONDS = 35 * 60
TELEGRAM_TIMEOUT_SECONDS = 20.0
LOCK_WAIT_SECONDS = 25.0
LOCK_RETRY_SECONDS = 0.05
AI_HEALTH_STATES = {"MISSING", "STALE", "OFFLINE", "ACTIVE"}
EVENTS = {
    "recovery",
    "blocked",
    "assessment",
    "ai-alert",
    "ai-failure",
    "ai-recovery",
    "status",
}
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
SAFE_CODE = re.compile(r"[A-Z0-9_-]{1,80}\Z")
BOT_TOKEN = re.compile(r"[0-9]{5,16}:[A-Za-z0-9_-]{20,128}\Z")
CHAT_ID = re.compile(r"-?[0-9]{1,20}\Z")


class TelegramNotificationError(RuntimeError):
    """A notification was blocked without exposing credentials or content."""


class _NoRedirectHandler(HTTPRedirectHandler):
    """Keep the bot token on the one fixed Telegram HTTPS origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


# Do not inherit HTTP(S) proxies from the service environment and never follow a
# redirect carrying the token-bearing URL to another origin.
TLS_CONTEXT = ssl.create_default_context()
TLS_CONTEXT.minimum_version = ssl.TLSVersion.TLSv1_2
TELEGRAM_OPENER = build_opener(
    ProxyHandler({}),
    HTTPSHandler(context=TLS_CONTEXT),
    _NoRedirectHandler(),
)


def _clean_id(value: object, *, required: bool = True) -> str | None:
    if isinstance(value, str) and SAFE_ID.fullmatch(value):
        return value
    if not required and value is None:
        return None
    raise TelegramNotificationError("technical identifier is invalid")


def _valid_chat_id(value: str) -> bool:
    if not CHAT_ID.fullmatch(value):
        return False
    parsed = int(value)
    return parsed != 0 and -(2**63) <= parsed < 2**63 and str(parsed) == value


def _read_credential(env_name: str, credential_name: str) -> str:
    """Read a systemd credential, falling back to env only outside that mode."""

    credentials_directory = os.environ.get("CREDENTIALS_DIRECTORY", "").strip()
    if not credentials_directory:
        return os.environ.get(env_name, "").strip()
    path = Path(credentials_directory) / credential_name
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise TelegramNotificationError(f"{credential_name} credential is missing") from None
    except OSError:
        raise TelegramNotificationError(f"{credential_name} credential is unreadable") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4096:
            raise TelegramNotificationError(f"{credential_name} credential is invalid")
        raw = os.read(descriptor, 4097)
    finally:
        os.close(descriptor)
    if len(raw) > 4096:
        raise TelegramNotificationError(f"{credential_name} credential is too large")
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeError:
        raise TelegramNotificationError(f"{credential_name} credential is invalid") from None
    if not value or any(character.isspace() for character in value):
        raise TelegramNotificationError(f"{credential_name} credential is invalid")
    return value


def _safe_environment() -> tuple[str, str]:
    for name in KALSHI_SECRET_NAMES:
        if os.environ.get(name, "").strip():
            raise TelegramNotificationError("Telegram notifier must not receive Kalshi credentials")
    for name in EXECUTION_NAMES:
        value = os.environ.get(name, "").strip().lower()
        if value not in ("", "0", "false", "off", "no"):
            raise TelegramNotificationError("Telegram notifier requires execution flags to remain false")

    token = _read_credential("TELEGRAM_BOT_TOKEN", "telegram_bot_token")
    chat_id = _read_credential("TELEGRAM_CHAT_ID", "telegram_chat_id")
    if not BOT_TOKEN.fullmatch(token):
        raise TelegramNotificationError("TELEGRAM_BOT_TOKEN is missing or invalid")
    if not _valid_chat_id(chat_id):
        raise TelegramNotificationError("TELEGRAM_CHAT_ID is missing or invalid")
    return token, chat_id


def _bounded_status(
    data_dir: Path,
    *,
    state_data_dir: Path | None,
    now: datetime,
) -> dict[str, Any]:
    raw = build_status(data_dir, now=now, state_data_dir=state_data_dir)
    cycle = raw.get("cycle")
    control = raw.get("control")
    capabilities = raw.get("capabilities")
    if (
        not isinstance(cycle, dict)
        or not isinstance(control, dict)
        or not isinstance(capabilities, dict)
    ):
        raise TelegramNotificationError("technical status shape is invalid")
    control_paused = control.get("paused")
    if type(control_paused) is not bool:
        raise TelegramNotificationError("assistant control state is invalid")
    technical_status = cycle.get("technical_status")
    if technical_status not in {"VERIFIED", "BLOCKED"}:
        raise TelegramNotificationError("technical status value is invalid")
    if any(
        capabilities.get(name) is not False
        for name in ("place_order", "cancel_order", "move_money", "shell")
    ):
        raise TelegramNotificationError("forbidden capability is present")
    if any(
        cycle.get(name) is not False
        for name in ("execution_authorized", "order_capability_present", "real_entry_eligible")
    ):
        raise TelegramNotificationError("execution authority is not explicitly false")

    raw_blockers = cycle.get("blockers")
    if not isinstance(raw_blockers, list) or len(raw_blockers) > MAX_BLOCKERS:
        raise TelegramNotificationError("technical blockers are invalid")
    blockers: list[str] = []
    for value in raw_blockers:
        if not isinstance(value, str) or not SAFE_CODE.fullmatch(value):
            raise TelegramNotificationError("technical blocker code is invalid")
        blockers.append(value)

    coverage = cycle.get("coverage_status")
    if coverage not in {"BOUNDED_COMPLETE", "PARTIAL", "UNVERIFIED"}:
        raise TelegramNotificationError("coverage status is invalid")
    count = cycle.get("observed_market_count")
    if count is not None and (type(count) is not int or not 0 <= count <= 100):
        raise TelegramNotificationError("observed market count is invalid")
    cycle_id = _clean_id(cycle.get("cycle_id"), required=False)
    if technical_status == "VERIFIED" and cycle_id is None:
        raise TelegramNotificationError("verified cycle identifier is missing")
    return {
        "technical_status": technical_status,
        "cycle_id": cycle_id,
        "coverage_status": coverage,
        "observed_market_count": count,
        "blockers": sorted(set(blockers)),
        "control_paused": control_paused,
        "execution_authorized": False,
        "order_capability_present": False,
    }


def _ai_failure_status(
    data_dir: Path,
    *,
    state_data_dir: Path | None,
) -> dict[str, Any]:
    """Build a fail-closed status without reading collector-owned artifacts."""

    control = current_control(data_dir, state_data_dir=state_data_dir)
    control_paused = control.get("paused")
    if type(control_paused) is not bool:
        raise TelegramNotificationError("assistant control state is invalid")
    return {
        "technical_status": "BLOCKED",
        "cycle_id": None,
        "coverage_status": "UNVERIFIED",
        "observed_market_count": None,
        "blockers": ["ASSISTANT_SERVICE_FAILED"],
        "control_paused": control_paused,
        "execution_authorized": False,
        "order_capability_present": False,
    }


def _assessment_observation(
    data_dir: Path,
    *,
    state_data_dir: Path | None = None,
    current_cycle_id: str | None,
    control_paused: bool,
    now: datetime,
) -> dict[str, Any]:
    try:
        raw = latest_assessment(data_dir, state_data_dir=state_data_dir)
    except FileNotFoundError:
        return {
            "health": "OFFLINE" if control_paused else "MISSING",
            "assessment": None,
            "assessment_id": None,
            "created_at": None,
        }
    except (BridgeError, EvidenceError, OSError):
        raise TelegramNotificationError("latest assessment is unreadable") from None

    decision = raw.get("decision")
    if decision not in ALLOWED_DECISIONS:
        raise TelegramNotificationError("latest assessment decision is invalid")
    provider = raw.get("provider")
    if provider not in {"openai", "offline", "local-fail-closed", "local-paused"}:
        raise TelegramNotificationError("latest assessment provider is invalid")
    assessment_id = _clean_id(raw.get("assessment_id"))
    source_cycle_id = _clean_id(raw.get("source_cycle_id"), required=False)
    try:
        created_at = timestamp(raw.get("created_at"))
    except EvidenceError:
        return {
            "health": "OFFLINE" if control_paused else "STALE",
            "assessment": None,
            "assessment_id": assessment_id,
            "created_at": None,
        }
    age = (now - created_at).total_seconds()
    fresh = 0 <= age <= MAX_ASSESSMENT_AGE_SECONDS
    if control_paused:
        health = "OFFLINE"
    elif not fresh:
        health = "STALE"
    elif provider != "openai":
        health = "OFFLINE"
    else:
        health = "ACTIVE"
    assessment = None
    if fresh and source_cycle_id is not None:
        assessment = {
            "assessment_id": assessment_id,
            "decision": decision,
            "provider": provider,
            "source_cycle_id": source_cycle_id,
            "matches_current_cycle": source_cycle_id == current_cycle_id,
        }
    return {
        "health": health,
        "assessment": assessment,
        "assessment_id": assessment_id,
        "created_at": created_at,
    }


def _read_state(path: Path, *, now: datetime) -> dict[str, Any] | None:
    try:
        value = read_object(path)
    except FileNotFoundError:
        return None
    except (EvidenceError, OSError):
        raise TelegramNotificationError("Telegram delivery state is unreadable") from None
    if value.get("schema_version") != STATE_SCHEMA:
        raise TelegramNotificationError("Telegram delivery state schema is invalid")
    try:
        updated_at = timestamp(value.get("updated_at"))
    except EvidenceError:
        raise TelegramNotificationError("Telegram delivery state time is invalid") from None
    if updated_at > now:
        raise TelegramNotificationError("Telegram delivery state time is invalid")
    status = value.get("last_technical_status")
    if status not in {"VERIFIED", "BLOCKED"}:
        raise TelegramNotificationError("Telegram delivery state is invalid")
    if value.get("last_ai_health") not in AI_HEALTH_STATES:
        raise TelegramNotificationError("Telegram delivery state is invalid")
    if value.get("last_event") not in EVENTS:
        raise TelegramNotificationError("Telegram delivery state is invalid")
    if type(value.get("last_control_paused")) is not bool:
        raise TelegramNotificationError("Telegram delivery state is invalid")
    delivery_key = value.get("last_delivery_key")
    if not isinstance(delivery_key, str) or not re.fullmatch(r"[0-9a-f]{64}", delivery_key):
        raise TelegramNotificationError("Telegram delivery state is invalid")
    assessment_id = value.get("last_assessment_id")
    if assessment_id is not None:
        _clean_id(assessment_id)
    blockers_digest = value.get("last_blockers_digest")
    if not isinstance(blockers_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", blockers_digest):
        raise TelegramNotificationError("Telegram delivery state is invalid")
    if value.get("execution_authorized") is not False:
        raise TelegramNotificationError("Telegram delivery state is invalid")
    if value.get("order_capability_present") is not False:
        raise TelegramNotificationError("Telegram delivery state is invalid")
    last_cycle_id = value.get("last_cycle_id")
    if last_cycle_id is not None:
        _clean_id(last_cycle_id)
    value["_updated_at"] = updated_at
    return value


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise TelegramNotificationError("Telegram delivery state cannot be a symlink")
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except (OSError, TypeError, ValueError):
        raise TelegramNotificationError("Telegram delivery state could not be written") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise TelegramNotificationError("Telegram notification lock cannot be a symlink")
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError:
        raise TelegramNotificationError("Telegram notification lock could not be opened") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise TelegramNotificationError("Telegram notification lock is invalid")
        os.fchmod(descriptor, 0o600)
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TelegramNotificationError(
                        "Telegram notification lock wait timed out"
                    ) from None
                time.sleep(min(LOCK_RETRY_SECONDS, remaining))
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _choose_event(
    requested: str,
    status: dict[str, Any],
    assessment: dict[str, Any] | None,
    state: dict[str, Any] | None,
) -> str | None:
    technical = status["technical_status"]
    if requested == "recovery":
        if technical != "VERIFIED":
            raise TelegramNotificationError("recovery notification requires a verified cycle")
        return "recovery"
    if requested == "status":
        return "status"
    if requested == "ai-failure":
        return "ai-failure"
    if requested != "auto":
        raise TelegramNotificationError("notification event is invalid")

    if state is None:
        return "recovery" if technical == "VERIFIED" else "blocked"
    if state["last_event"] == "ai-failure" and state["last_ai_health"] != "ACTIVE":
        if status["ai_health"] == "ACTIVE":
            return "ai-recovery"
        blockers_digest = _digest(status["blockers"])
        if technical == "BLOCKED" and (
            state["last_technical_status"] != "BLOCKED"
            or state["last_blockers_digest"] != blockers_digest
        ):
            return "blocked"
        return None
    if state["last_technical_status"] != technical:
        return "recovery" if technical == "VERIFIED" else "blocked"
    blockers_digest = _digest(status["blockers"])
    if technical == "BLOCKED" and state["last_blockers_digest"] != blockers_digest:
        return "blocked"
    if (
        state["last_ai_health"] != status["ai_health"]
        or state["last_control_paused"] != status["control_paused"]
    ):
        return "ai-recovery" if status["ai_health"] == "ACTIVE" else "ai-alert"
    if (
        assessment is not None
        and assessment["decision"] != "NO_ACTION"
        and state.get("last_assessment_id") != assessment["assessment_id"]
    ):
        return "assessment"
    return None


def _message(
    event: str,
    status: dict[str, Any],
    assessment: dict[str, Any] | None,
    *,
    now: datetime,
) -> str:
    heading = {
        "recovery": "Botkalshi volvió",
        "blocked": "Alerta técnica de Botkalshi",
        "assessment": "Evaluación de Botkalshi requiere atención",
        "ai-alert": "Alerta técnica de IA de Botkalshi",
        "ai-failure": "Falló el ciclo de IA de Botkalshi",
        "ai-recovery": "La IA de Botkalshi volvió",
        "status": "Estado técnico de Botkalshi",
    }[event]
    lines = [
        heading,
        "Modo: SHADOW_READONLY (simulación)",
        f"Estado técnico: {status['technical_status']}",
        f"Ciclo: {status['cycle_id'] or 'no verificado'}",
        f"Cobertura: {status['coverage_status']}",
        "Mercados observados: "
        + (str(status["observed_market_count"]) if status["observed_market_count"] is not None else "no verificado"),
        f"Estado IA: {status['ai_health']}",
        "Evaluaciones IA pausadas: " + ("sí" if status["control_paused"] else "no"),
    ]
    if event == "ai-failure":
        lines.append("Evento IA: ASSISTANT_SERVICE_FAILED")
    if status["blockers"]:
        lines.append("Bloqueos: " + ", ".join(status["blockers"]))
    if assessment is not None:
        lines.extend(
            (
                f"Evaluación: {assessment['decision']}",
                f"Proveedor: {assessment['provider']}",
                f"Ciclo evaluado: {assessment['source_cycle_id']}",
            )
        )
    lines.extend(
        (
            "Trading real: BLOQUEADO",
            "Capacidad de órdenes: NO DISPONIBLE",
            f"Hora UTC: {now.astimezone(UTC).isoformat()}",
        )
    )
    text = "\n".join(lines)
    if not 1 <= len(text) <= MAX_MESSAGE_CHARS:
        raise TelegramNotificationError("Telegram message length is invalid")
    return text


def _send_message(token: str, chat_id: str, text: str) -> int:
    if not BOT_TOKEN.fullmatch(token) or not _valid_chat_id(chat_id):
        raise TelegramNotificationError("Telegram credentials are invalid")
    if not isinstance(text, str) or not 1 <= len(text) <= MAX_MESSAGE_CHARS:
        raise TelegramNotificationError("Telegram message length is invalid")
    payload = json.dumps(
        {"chat_id": chat_id, "text": text},
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    try:
        request = Request(
            f"{TELEGRAM_API_ROOT}/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with TELEGRAM_OPENER.open(request, timeout=TELEGRAM_TIMEOUT_SECONDS) as response:
            response_status = getattr(response, "status", None)
            if response_status is None:
                response_status = response.getcode()
            if response_status != 200:
                raise TelegramNotificationError("Telegram API response status was not successful")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except TelegramNotificationError:
        raise
    except HTTPError as exc:
        raise TelegramNotificationError(f"Telegram API returned HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise TelegramNotificationError("Telegram API request failed") from None
    except Exception:
        # Third-party/network exceptions may embed the full URL (and bot token).
        raise TelegramNotificationError("Telegram API request failed") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise TelegramNotificationError("Telegram API response was too large")
    try:
        body = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise TelegramNotificationError("Telegram API response was invalid") from None
    if not isinstance(body, dict):
        raise TelegramNotificationError("Telegram API response was invalid")
    result = body.get("result")
    message_id = result.get("message_id") if isinstance(result, dict) else None
    chat = result.get("chat") if isinstance(result, dict) else None
    if body.get("ok") is not True or type(message_id) is not int or message_id < 1:
        raise TelegramNotificationError("Telegram API did not confirm delivery")
    if not isinstance(chat, dict):
        raise TelegramNotificationError("Telegram API returned an unexpected chat")
    returned_chat_id = chat.get("id")
    if type(returned_chat_id) is not int or str(returned_chat_id) != chat_id:
        raise TelegramNotificationError("Telegram API returned an unexpected chat")
    return message_id


def notify(
    data_dir: Path,
    *,
    state_data_dir: Path | None = None,
    event: str = "auto",
    force_confirm: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    forced_confirmation = force_confirm is not None
    if forced_confirmation and (event != "recovery" or force_confirm != "INSTALLATION_TEST"):
        raise TelegramNotificationError(
            "forced delivery requires recovery and literal INSTALLATION_TEST"
        )
    checked_at = now or datetime.now(UTC)
    if checked_at.tzinfo is None:
        raise TelegramNotificationError("notification check time must include a timezone")
    checked_at = checked_at.astimezone(UTC)
    token, chat_id = _safe_environment()
    if state_data_dir is None:
        telegram_dir = data_dir / "assistant" / "telegram"
    else:
        telegram_dir = state_data_dir / "telegram"
    state_path = telegram_dir / "telegram-state.json"
    with _exclusive_lock(telegram_dir / "notify.lock"):
        state = _read_state(state_path, now=checked_at)
        if event == "ai-failure":
            status = _ai_failure_status(
                data_dir,
                state_data_dir=state_data_dir,
            )
            observation = {
                "health": "OFFLINE",
                "assessment": None,
                "assessment_id": None,
                "created_at": None,
            }
        else:
            status = _bounded_status(
                data_dir,
                state_data_dir=state_data_dir,
                now=checked_at,
            )
            observation = _assessment_observation(
                data_dir,
                state_data_dir=state_data_dir,
                current_cycle_id=(
                    status["cycle_id"] if status["technical_status"] == "VERIFIED" else None
                ),
                control_paused=status["control_paused"],
                now=checked_at,
            )
        status["ai_health"] = observation["health"]
        assessment = observation["assessment"]
        if (
            state is not None
            and state["last_ai_health"] != "ACTIVE"
            and status["ai_health"] == "ACTIVE"
        ):
            created_at = observation["created_at"]
            assessment_id = observation["assessment_id"]
            recovered_by_new_assessment = (
                isinstance(created_at, datetime)
                and created_at > state["_updated_at"]
                and isinstance(assessment_id, str)
                and assessment_id != state.get("last_assessment_id")
            )
            if not recovered_by_new_assessment:
                status["ai_health"] = state["last_ai_health"]
                assessment = None
        if forced_confirmation and status["ai_health"] != "ACTIVE":
            raise TelegramNotificationError(
                "installation confirmation requires an active AI assessment"
            )
        selected = _choose_event(event, status, assessment, state)
        if selected is None:
            return {
                "schema_version": "botkalshi-telegram-result-v2",
                "delivered": False,
                "reason": "UNCHANGED",
                "technical_status": status["technical_status"],
                "cycle_id": status["cycle_id"],
                "ai_health": status["ai_health"],
                "control_paused": status["control_paused"],
                "execution_authorized": False,
                "order_capability_present": False,
            }

        assessment_events = {"assessment", "ai-alert", "ai-recovery", "status"}
        technical_events = {"blocked", "recovery", "status"}
        delivery_basis = {
            "event": selected,
            "technical_status": status["technical_status"],
            "cycle_id": status["cycle_id"] if selected in technical_events else None,
            "coverage_status": (
                status["coverage_status"] if selected in technical_events else None
            ),
            "blockers": status["blockers"] if selected in {"blocked", "status"} else [],
            "ai_health": status["ai_health"],
            "control_paused": status["control_paused"],
            "assessment_id": (
                assessment["assessment_id"]
                if assessment is not None and selected in assessment_events
                else None
            ),
            "assessment_decision": (
                assessment["decision"]
                if assessment is not None and selected in assessment_events
                else None
            ),
        }
        delivery_key = _digest(delivery_basis)
        if (
            not forced_confirmation
            and state is not None
            and state["last_delivery_key"] == delivery_key
        ):
            return {
                "schema_version": "botkalshi-telegram-result-v2",
                "delivered": False,
                "reason": "DUPLICATE",
                "event": selected,
                "technical_status": status["technical_status"],
                "cycle_id": status["cycle_id"],
                "ai_health": status["ai_health"],
                "control_paused": status["control_paused"],
                "execution_authorized": False,
                "order_capability_present": False,
            }

        message = _message(selected, status, assessment, now=checked_at)
        message_id = _send_message(token, chat_id, message)
        preserve_technical_state = selected == "ai-failure" and state is not None
        next_state = {
            "schema_version": STATE_SCHEMA,
            "updated_at": checked_at.isoformat(),
            "last_delivery_key": delivery_key,
            "last_event": selected,
            "last_technical_status": (
                state["last_technical_status"]
                if preserve_technical_state
                else status["technical_status"]
            ),
            "last_blockers_digest": (
                state["last_blockers_digest"]
                if preserve_technical_state
                else _digest(status["blockers"])
            ),
            "last_ai_health": status["ai_health"],
            "last_control_paused": status["control_paused"],
            "last_assessment_id": (
                state.get("last_assessment_id")
                if preserve_technical_state
                else observation["assessment_id"]
            ),
            "last_cycle_id": (
                state.get("last_cycle_id")
                if preserve_technical_state
                else status["cycle_id"]
            ),
            "last_message_id": message_id,
            "execution_authorized": False,
            "order_capability_present": False,
        }
        _atomic_write(state_path, next_state)
        return {
            "schema_version": "botkalshi-telegram-result-v2",
            "delivered": True,
            "event": selected,
            "message_id": message_id,
            "technical_status": status["technical_status"],
            "cycle_id": status["cycle_id"],
            "ai_health": status["ai_health"],
            "control_paused": status["control_paused"],
            "forced_confirmation": forced_confirmation,
            "execution_authorized": False,
            "order_capability_present": False,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--state-data", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    command = subparsers.add_parser("notify")
    command.add_argument(
        "--event",
        choices=("auto", "recovery", "status", "ai-failure"),
        default="auto",
    )
    command.add_argument("--force-confirm")
    args = parser.parse_args()
    try:
        result = notify(
            args.data,
            state_data_dir=args.state_data,
            event=args.event,
            force_confirm=args.force_confirm,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
        return 0
    except (
        TelegramNotificationError,
        CycleVerificationError,
        EvidenceError,
        OSError,
        ValueError,
    ) as exc:
        print(f"Telegram notification blocked: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
