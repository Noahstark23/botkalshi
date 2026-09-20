"""Bounded communication bridge between the research bot and an AI assessor.

The bridge can read verified public research artifacts, produce a structured
assessment, and pause further assessments.  It has no Kalshi authentication,
order, cancellation, transfer, shell, or arbitrary-command capability.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
import ssl
import stat
import sys
import tempfile
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from cycle_verifier import CycleVerificationError, verify_cycle
from reporting import EvidenceError, read_object


OPENAI_ENDPOINT = "https://api.openai.com/v1/responses"
MAX_MODEL_INPUT_BYTES = 120_000
MAX_MARKETS_FOR_MODEL = 20
ALLOWED_DECISIONS = {
    "NO_ACTION",
    "PAUSE_RESEARCH",
    "REQUEST_HUMAN_REVIEW",
    "REFRESH_PUBLIC_SNAPSHOT",
}
ALLOWED_COMMANDS = {
    "NOOP",
    "PAUSE_RESEARCH",
    "REQUEST_HUMAN_REVIEW",
    "REFRESH_PUBLIC_SNAPSHOT",
}
KALSHI_SECRET_NAMES = (
    "KALSHI_API_KEY_ID",
    "KALSHI_PRIVATE_KEY",
    "KALSHI_PRIVATE_KEY_PATH",
)
EXECUTION_NAMES = (
    "TRADING_ENABLED",
    "MOTOR_MM_EXECUTION_ENABLED",
    "MOTOR_1_EXECUTION_ENABLED",
    "MOTOR_2_EXECUTION_ENABLED",
    "MOTOR_2_ENTRY_EXECUTION_ENABLED",
    "MOTOR_3_EXECUTION_ENABLED",
    "MOTOR_REST_EXECUTION_ENABLED",
)


class BridgeError(RuntimeError):
    """A bridge operation failed without granting additional authority."""


class _NoRedirect(HTTPRedirectHandler):
    """Keep credentials pinned to the configured HTTPS API origin."""

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def _open_https(request: Request, *, timeout: float):
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    opener = build_opener(
        ProxyHandler({}),
        HTTPSHandler(context=context),
        _NoRedirect(),
    )
    return opener.open(request, timeout=timeout)


def _read_credential(env_name: str, credential_name: str) -> str:
    """Read a systemd credential, with an environment fallback for local tests."""

    credentials_dir = os.environ.get("CREDENTIALS_DIRECTORY", "").strip()
    if credentials_dir:
        path = Path(credentials_dir) / credential_name
        flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            descriptor = None
        except OSError:
            raise BridgeError(f"{credential_name} credential is unreadable") from None
        if descriptor is None:
            raise BridgeError(f"{credential_name} credential is missing")
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4096:
                raise BridgeError(f"{credential_name} credential is invalid")
            raw = os.read(descriptor, 4097)
        finally:
            os.close(descriptor)
        if len(raw) > 4096:
            raise BridgeError(f"{credential_name} credential is too large")
        try:
            value = raw.decode("utf-8").strip()
        except UnicodeError:
            raise BridgeError(f"{credential_name} credential is invalid") from None
        if not value or any(character.isspace() for character in value):
            raise BridgeError(f"{credential_name} credential is invalid")
        return value
    return os.environ.get(env_name, "").strip()


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise BridgeError("output path cannot be a symlink")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        # The isolated botkalshi-ai group may include the read-only Telegram
        # notifier.  The containing directory is not accessible to other users.
        os.fchmod(descriptor, 0o640)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_symlink():
            raise BridgeError("output path cannot be a symlink")
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise BridgeError("lock path cannot be a symlink")
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError:
        raise BridgeError("assistant operation lock is unavailable") from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise BridgeError("assistant operation lock is invalid")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BridgeError("another assistant operation is already running") from None
        yield
    finally:
        os.close(descriptor)


def _read_optional(path: Path) -> dict[str, Any] | None:
    try:
        return read_object(path)
    except (EvidenceError, OSError):
        return None


def load_artifacts(data_dir: Path) -> tuple[dict[str, Any] | None, ...]:
    return tuple(
        _read_optional(data_dir / relative)
        for relative in (
            "health.json",
            "packets/latest.json",
            "coverage.json",
            "risk-status.json",
        )
    )


def _assistant_state_dir(data_dir: Path, state_data_dir: Path | None = None) -> Path:
    """Locate integration state separately from collector-owned research data."""

    return (state_data_dir or data_dir) / "assistant"


def current_control(
    data_dir: Path,
    *,
    state_data_dir: Path | None = None,
) -> dict[str, Any]:
    state = _read_optional(_assistant_state_dir(data_dir, state_data_dir) / "control-state.json")
    if not isinstance(state, dict) or type(state.get("paused")) is not bool:
        return {
            "schema_version": "botkalshi-assistant-control-v1",
            "paused": True,
            "reason": "control state missing or invalid",
            "updated_at": None,
            "updated_by": "fail-closed-default",
        }
    return {
        "schema_version": "botkalshi-assistant-control-v1",
        "paused": state["paused"],
        "reason": state.get("reason") if isinstance(state.get("reason"), str) else None,
        "updated_at": state.get("updated_at") if isinstance(state.get("updated_at"), str) else None,
        "updated_by": state.get("updated_by") if isinstance(state.get("updated_by"), str) else None,
    }


def set_pause(
    data_dir: Path,
    *,
    paused: bool,
    reason: str,
    actor: str,
    state_data_dir: Path | None = None,
) -> dict[str, Any]:
    assistant_dir = _assistant_state_dir(data_dir, state_data_dir)
    with _exclusive_lock(assistant_dir / "control-state.lock"):
        clean_reason = " ".join(reason.strip().split())
        if not clean_reason or len(clean_reason) > 240:
            raise BridgeError("pause reason must contain 1-240 characters")
        if actor not in {"operator", "ai-defensive-pause"}:
            raise BridgeError("invalid control actor")
        state = {
            "schema_version": "botkalshi-assistant-control-v1",
            "paused": paused,
            "reason": clean_reason,
            "updated_at": datetime.now(UTC).isoformat(),
            "updated_by": actor,
            "scope": "AI_ASSESSMENT_ONLY",
            "trading_state_changed": False,
        }
        _atomic_write(assistant_dir / "control-state.json", state)
        return state


def build_status(
    data_dir: Path,
    *,
    now: datetime | None = None,
    state_data_dir: Path | None = None,
) -> dict[str, Any]:
    health, packet, coverage, risk = load_artifacts(data_dir)
    verification = verify_cycle(
        health,
        packet,
        coverage,
        risk,
        now=now or datetime.now(UTC),
    )
    latest = _read_optional(
        _assistant_state_dir(data_dir, state_data_dir) / "assessment-latest.json"
    )
    latest_summary = None
    if isinstance(latest, dict):
        latest_summary = {
            "assessment_id": latest.get("assessment_id"),
            "created_at": latest.get("created_at"),
            "provider": latest.get("provider"),
            "decision": latest.get("decision"),
            "source_cycle_id": latest.get("source_cycle_id"),
        }
    return {
        "schema_version": "botkalshi-assistant-status-v1",
        "checked_at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
        "cycle": verification,
        "control": current_control(data_dir, state_data_dir=state_data_dir),
        "latest_assessment": latest_summary,
        "capabilities": {
            "read_verified_snapshot": True,
            "write_structured_assessment": True,
            "defensive_pause": True,
            "self_resume": False,
            "place_order": False,
            "cancel_order": False,
            "move_money": False,
            "shell": False,
        },
    }


def build_snapshot(data_dir: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Return a bounded, verified, credential-free research snapshot."""

    checked_at = (now or datetime.now(UTC)).astimezone(UTC)
    health, packet, coverage, risk = load_artifacts(data_dir)
    verification = verify_cycle(health, packet, coverage, risk, now=checked_at)
    if verification["technical_status"] != "VERIFIED":
        raise BridgeError("research snapshot is unavailable because the cycle is not verified")
    assert isinstance(packet, dict) and isinstance(coverage, dict) and isinstance(risk, dict)
    return {
        "schema_version": "botkalshi-assistant-snapshot-v1",
        "checked_at": checked_at.isoformat(),
        "cycle": verification,
        "snapshot": _model_input(verification, packet, coverage, risk),
        "execution_authorized": False,
        "order_capability_present": False,
    }


def latest_assessment(
    data_dir: Path,
    *,
    state_data_dir: Path | None = None,
) -> dict[str, Any]:
    """Return the latest bounded assessment, rejecting any authority escalation."""

    value = read_object(
        _assistant_state_dir(data_dir, state_data_dir) / "assessment-latest.json"
    )
    if value.get("schema_version") != "botkalshi-assistant-assessment-v1":
        raise BridgeError("latest assessment schema invalid")
    if value.get("execution_authorized") is not False:
        raise BridgeError("latest assessment attempted to authorize execution")
    if value.get("order_capability_present") is not False:
        raise BridgeError("latest assessment attempted to claim order capability")
    return value


def _bounded_levels(value: object) -> dict[str, list[list[str]]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, list[list[str]]] = {}
    for side in ("yes", "no", "yes_dollars", "no_dollars"):
        rows = value.get(side)
        if not isinstance(rows, list):
            continue
        clean: list[list[str]] = []
        for row in rows[:5]:
            if isinstance(row, list) and len(row) == 2:
                clean.append([str(row[0])[:32], str(row[1])[:32]])
        result[side] = clean
    return result


def _model_input(
    verification: dict[str, Any],
    packet: dict[str, Any],
    coverage: dict[str, Any],
    risk: dict[str, Any],
) -> dict[str, Any]:
    kalshi = packet.get("kalshi") if isinstance(packet.get("kalshi"), dict) else {}
    raw_markets = kalshi.get("markets") if isinstance(kalshi.get("markets"), list) else []
    markets = []
    for raw in raw_markets[:MAX_MARKETS_FOR_MODEL]:
        if not isinstance(raw, dict):
            continue
        markets.append(
            {
                "ticker": raw.get("ticker"),
                "event_ticker": raw.get("event_ticker"),
                "status": raw.get("status"),
                "close_time": raw.get("close_time"),
                "levels": _bounded_levels(raw.get("levels")),
            }
        )
    sportsbook = packet.get("sportsbook") if isinstance(packet.get("sportsbook"), dict) else {}
    return {
        "cycle_verification": verification,
        "market_sample": markets,
        "market_sample_truncated": len(raw_markets) > len(markets),
        "sportsbook": {
            "provider": sportsbook.get("provider"),
            "status": sportsbook.get("status"),
            "event_count": sportsbook.get("event_count"),
        },
        "coverage": {
            key: coverage.get(key)
            for key in (
                "series",
                "horizon_hours",
                "pages_fetched",
                "cursor_exhausted",
                "open_markets_seen",
                "eligible_in_horizon",
                "orderbooks_fetched",
                "truncated_by_page_limit",
                "truncated_by_orderbook_limit",
            )
        },
        "risk": {
            key: risk.get(key)
            for key in (
                "bank_state",
                "mode",
                "reference_bank_usd",
                "reference_unit_cap_usd",
                "new_risk_headroom_usd",
                "illustrative_headroom_usd",
                "real_entry_eligible",
                "execution_authorized",
                "order_capability_present",
                "reasons",
            )
        },
    }


ASSESSMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": sorted(ALLOWED_DECISIONS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "summary": {"type": "string", "maxLength": 600},
        "evidence_ids": {
            "type": "array",
            "items": {"type": "string", "maxLength": 128},
            "maxItems": 20,
        },
        "blockers": {
            "type": "array",
            "items": {"type": "string", "maxLength": 160},
            "maxItems": 20,
        },
        "proposed_commands": {
            "type": "array",
            "items": {"type": "string", "enum": sorted(ALLOWED_COMMANDS)},
            "maxItems": 4,
        },
    },
    "required": [
        "decision",
        "confidence",
        "summary",
        "evidence_ids",
        "blockers",
        "proposed_commands",
    ],
    "additionalProperties": False,
}


def _assert_ai_environment() -> None:
    for name in KALSHI_SECRET_NAMES:
        if os.environ.get(name, "").strip():
            raise BridgeError("AI bridge must not receive Kalshi credentials")
    for name in EXECUTION_NAMES:
        if os.environ.get(name, "").strip().lower() not in ("", "0", "false", "off", "no"):
            raise BridgeError("AI bridge requires every execution flag to remain false")


def _extract_output_text(body: object) -> str:
    if not isinstance(body, dict):
        raise BridgeError("OpenAI response body was not an object")
    if body.get("status") != "completed":
        raise BridgeError("OpenAI response was not completed")
    output_texts: list[str] = []
    for item in body.get("output", []) if isinstance(body.get("output"), list) else []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []) if isinstance(item.get("content"), list) else []:
            if not isinstance(content, dict):
                continue
            if content.get("type") == "refusal":
                raise BridgeError("OpenAI refused the assessment")
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                output_texts.append(content["text"])
    if len(output_texts) != 1:
        raise BridgeError("OpenAI response did not contain exactly one output_text")
    return output_texts[0]


def _validate_assessment(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(ASSESSMENT_SCHEMA["required"]):
        raise BridgeError("assessment shape invalid")
    if value.get("decision") not in ALLOWED_DECISIONS:
        raise BridgeError("assessment decision invalid")
    confidence = value.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise BridgeError("assessment confidence invalid")
    summary = value.get("summary")
    if not isinstance(summary, str) or not 1 <= len(summary) <= 600:
        raise BridgeError("assessment summary invalid")
    for field, max_items, max_length in (
        ("evidence_ids", 20, 128),
        ("blockers", 20, 160),
    ):
        rows = value.get(field)
        if (
            not isinstance(rows, list)
            or len(rows) > max_items
            or any(not isinstance(row, str) or len(row) > max_length for row in rows)
        ):
            raise BridgeError(f"assessment {field} invalid")
    commands = value.get("proposed_commands")
    if (
        not isinstance(commands, list)
        or len(commands) > 4
        or any(command not in ALLOWED_COMMANDS for command in commands)
    ):
        raise BridgeError("assessment commands invalid")
    return value


def _openai_assessment(model_input: dict[str, Any], *, model: str) -> dict[str, Any]:
    _assert_ai_environment()
    api_key = _read_credential("OPENAI_API_KEY", "openai_api_key")
    if not api_key:
        raise BridgeError("OpenAI API credential is required for provider=openai")
    if not model or len(model) > 80:
        raise BridgeError("an explicit OpenAI model is required")
    user_text = json.dumps(model_input, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(user_text.encode("utf-8")) > MAX_MODEL_INPUT_BYTES:
        raise BridgeError("model input exceeds bridge limit")
    payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": (
                    "You assess a read-only prediction-market research collector. "
                    "Use only the supplied evidence. Never propose placing, canceling, "
                    "sizing, or funding a trade. Prefer NO_ACTION when evidence is partial. "
                    "PAUSE_RESEARCH is allowed only as a defensive reduction of activity."
                ),
            },
            {"role": "user", "content": user_text},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "botkalshi_research_assessment",
                "strict": True,
                "schema": ASSESSMENT_SCHEMA,
            }
        },
        "max_output_tokens": 800,
        "store": False,
    }
    request = Request(
        OPENAI_ENDPOINT,
        data=json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _open_https(request, timeout=45) as response:
            response_status = getattr(response, "status", None)
            if response_status is None:
                response_status = response.getcode()
            if response_status != 200:
                raise BridgeError("OpenAI response status was not successful")
            raw = response.read(1_000_001)
    except HTTPError as exc:
        raise BridgeError(f"OpenAI HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError):
        raise BridgeError("OpenAI request failed") from None
    if len(raw) > 1_000_000:
        raise BridgeError("OpenAI response too large")
    try:
        body = json.loads(raw)
        result = json.loads(_extract_output_text(body))
    except (json.JSONDecodeError, UnicodeError, TypeError, RecursionError):
        raise BridgeError("OpenAI response invalid") from None
    return _validate_assessment(result)


def _assess_locked(
    data_dir: Path,
    *,
    provider: str,
    model: str | None = None,
    now: datetime | None = None,
    state_data_dir: Path | None = None,
) -> dict[str, Any]:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    health, packet, coverage, risk = load_artifacts(data_dir)
    verification = verify_cycle(health, packet, coverage, risk, now=now)
    control = current_control(data_dir, state_data_dir=state_data_dir)
    effective_model: str | None = None
    if verification["technical_status"] != "VERIFIED":
        result = {
            "decision": "NO_ACTION",
            "confidence": 1.0,
            "summary": "Assessment blocked because the four-artifact cycle is not coherent.",
            "evidence_ids": [],
            "blockers": verification["blockers"],
            "proposed_commands": ["REQUEST_HUMAN_REVIEW"],
        }
        provider_used = "local-fail-closed"
    elif control["paused"]:
        result = {
            "decision": "NO_ACTION",
            "confidence": 1.0,
            "summary": "AI assessment is paused; no external model request was made.",
            "evidence_ids": [verification["cycle_id"]],
            "blockers": ["AI_ASSESSMENT_PAUSED"],
            "proposed_commands": ["REQUEST_HUMAN_REVIEW"],
        }
        provider_used = "local-paused"
    elif provider == "offline":
        result = {
            "decision": "NO_ACTION",
            "confidence": 1.0,
            "summary": "Cycle is technically coherent; offline mode cannot infer a trading edge.",
            "evidence_ids": [verification["cycle_id"]],
            "blockers": ["NO_EXTERNAL_ASSESSOR"],
            "proposed_commands": ["NOOP"],
        }
        provider_used = "offline"
    elif provider == "openai":
        assert isinstance(packet, dict) and isinstance(coverage, dict) and isinstance(risk, dict)
        effective_model = model or os.environ.get("BOTKALSHI_OPENAI_MODEL", "").strip()
        result = _openai_assessment(
            _model_input(verification, packet, coverage, risk),
            model=effective_model,
        )
        provider_used = "openai"
    else:
        raise BridgeError("provider must be offline or openai")

    assessment = {
        "schema_version": "botkalshi-assistant-assessment-v1",
        "assessment_id": f"assessment-{now.strftime('%Y%m%dT%H%M%S%fZ')}",
        "created_at": now.isoformat(),
        "provider": provider_used,
        "model": effective_model if provider_used == "openai" else None,
        "source_cycle_id": verification.get("cycle_id"),
        "source_artifact_sha256": verification["artifact_sha256"],
        **result,
        "execution_authorized": False,
        "order_capability_present": False,
        "commands_executed": [],
    }
    assessment_path = (
        _assistant_state_dir(data_dir, state_data_dir) / "assessment-latest.json"
    )
    _atomic_write(assessment_path, assessment)
    if assessment["decision"] == "PAUSE_RESEARCH":
        set_pause(
            data_dir,
            paused=True,
            reason="AI defensive pause: " + assessment["summary"][:200],
            actor="ai-defensive-pause",
            state_data_dir=state_data_dir,
        )
        assessment["commands_executed"] = ["PAUSE_RESEARCH"]
        _atomic_write(assessment_path, assessment)
    return assessment


def assess(
    data_dir: Path,
    *,
    provider: str,
    model: str | None = None,
    now: datetime | None = None,
    state_data_dir: Path | None = None,
) -> dict[str, Any]:
    assistant_dir = _assistant_state_dir(data_dir, state_data_dir)
    with _exclusive_lock(assistant_dir / "assessment.lock"):
        return _assess_locked(
            data_dir,
            provider=provider,
            model=model,
            now=now,
            state_data_dir=state_data_dir,
        )


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument(
        "--state-data",
        type=Path,
        help=(
            "root for integration-owned state; defaults to --data for backward "
            "compatibility"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("snapshot")
    sub.add_parser("latest-assessment")
    pause = sub.add_parser("pause")
    pause.add_argument("--reason", required=True)
    resume = sub.add_parser("resume-simulation")
    resume.add_argument("--confirm", choices=("SIMULATION_ONLY",), required=True)
    run = sub.add_parser("assess")
    run.add_argument("--provider", choices=("offline", "openai"), default="offline")
    run.add_argument("--model")
    args = parser.parse_args()
    try:
        if args.command == "status":
            result = build_status(args.data, state_data_dir=args.state_data)
        elif args.command == "snapshot":
            result = build_snapshot(args.data)
        elif args.command == "latest-assessment":
            result = latest_assessment(args.data, state_data_dir=args.state_data)
        elif args.command == "pause":
            result = set_pause(
                args.data,
                paused=True,
                reason=args.reason,
                actor="operator",
                state_data_dir=args.state_data,
            )
        elif args.command == "resume-simulation":
            status = build_status(args.data, state_data_dir=args.state_data)
            if status["cycle"]["technical_status"] != "VERIFIED":
                raise BridgeError("cannot resume assessments with a blocked cycle")
            result = set_pause(
                args.data,
                paused=False,
                reason="operator confirmed simulation-only assessment",
                actor="operator",
                state_data_dir=args.state_data,
            )
        else:
            result = assess(
                args.data,
                provider=args.provider,
                model=args.model,
                state_data_dir=args.state_data,
            )
        _print(result)
        return 0
    except (BridgeError, CycleVerificationError, EvidenceError, OSError, ValueError) as exc:
        print(f"assistant bridge blocked: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
