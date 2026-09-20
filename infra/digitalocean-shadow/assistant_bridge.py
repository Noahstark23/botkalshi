"""Bounded communication bridge between the research bot and an AI assessor.

The bridge can read verified public research artifacts, produce a structured
assessment, and pause further assessments.  It has no Kalshi authentication,
order, cancellation, transfer, shell, or arbitrary-command capability.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise BridgeError("output path cannot be a symlink")
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


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


def current_control(data_dir: Path) -> dict[str, Any]:
    state = _read_optional(data_dir / "assistant" / "control-state.json")
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


def set_pause(data_dir: Path, *, paused: bool, reason: str, actor: str) -> dict[str, Any]:
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
    _atomic_write(data_dir / "assistant" / "control-state.json", state)
    return state


def build_status(data_dir: Path, *, now: datetime | None = None) -> dict[str, Any]:
    health, packet, coverage, risk = load_artifacts(data_dir)
    verification = verify_cycle(
        health,
        packet,
        coverage,
        risk,
        now=now or datetime.now(UTC),
    )
    latest = _read_optional(data_dir / "assistant" / "assessment-latest.json")
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
        "control": current_control(data_dir),
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


def _extract_output_text(body: dict[str, Any]) -> str:
    for item in body.get("output", []) if isinstance(body.get("output"), list) else []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []) if isinstance(item.get("content"), list) else []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str):
                    return text
    raise BridgeError("OpenAI response did not contain output_text")


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
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise BridgeError("OPENAI_API_KEY is required for provider=openai")
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
        with urlopen(request, timeout=45) as response:
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
    except (json.JSONDecodeError, UnicodeError, TypeError):
        raise BridgeError("OpenAI response invalid") from None
    return _validate_assessment(result)


def assess(
    data_dir: Path,
    *,
    provider: str,
    model: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    health, packet, coverage, risk = load_artifacts(data_dir)
    verification = verify_cycle(health, packet, coverage, risk, now=now)
    control = current_control(data_dir)
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
    _atomic_write(data_dir / "assistant" / "assessment-latest.json", assessment)
    if assessment["decision"] == "PAUSE_RESEARCH":
        set_pause(
            data_dir,
            paused=True,
            reason="AI defensive pause: " + assessment["summary"][:200],
            actor="ai-defensive-pause",
        )
        assessment["commands_executed"] = ["PAUSE_RESEARCH"]
        _atomic_write(data_dir / "assistant" / "assessment-latest.json", assessment)
    return assessment


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
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
            result = build_status(args.data)
        elif args.command == "pause":
            result = set_pause(args.data, paused=True, reason=args.reason, actor="operator")
        elif args.command == "resume-simulation":
            status = build_status(args.data)
            if status["cycle"]["technical_status"] != "VERIFIED":
                raise BridgeError("cannot resume assessments with a blocked cycle")
            result = set_pause(
                args.data,
                paused=False,
                reason="operator confirmed simulation-only assessment",
                actor="operator",
            )
        else:
            result = assess(args.data, provider=args.provider, model=args.model)
        _print(result)
        return 0
    except (BridgeError, CycleVerificationError, EvidenceError, OSError, ValueError) as exc:
        print(f"assistant bridge blocked: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
