"""Fail-closed risk/accounting policy for the USD 200 research trial.

This module never places orders and never authenticates to Kalshi.  It evaluates
only explicitly supplied reconciled state and writes a local decision status.
Missing or invalid reconciliation means no real-money entry is eligible.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import json
import os
from pathlib import Path
from typing import Any

from cycle_contract import RISK_SCHEMA

CENT = Decimal("0.01")
REFERENCE_BANK = Decimal("200.00")
REFERENCE_UNIT = Decimal("2.00")
WEEKLY_STOP = Decimal("12.00")
EXPERIMENT_STOP = Decimal("20.00")
MAX_INPUT_BYTES = 64_000
MAX_RECONCILED_AGE = timedelta(hours=24)
ALLOWED_DECLARATIVE_SOURCES = frozenset({"confirmed-fill-ledger"})


class RiskInputError(ValueError):
    pass


def _money(value: Any, *, field: str, allow_negative: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise RiskInputError(f"{field} must be numeric")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise RiskInputError(f"{field} must be numeric") from None
    if not amount.is_finite():
        raise RiskInputError(f"{field} must be finite")
    if not allow_negative and amount < 0:
        raise RiskInputError(f"{field} must be non-negative")
    return amount.quantize(CENT)


def _fmt(value: Decimal | None) -> str | None:
    return None if value is None else f"{value.quantize(CENT):.2f}"


def _reconciled_at(value: object, *, now: datetime) -> datetime:
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        raise RiskInputError("capital_reconciled_at is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RiskInputError("capital_reconciled_at invalid") from None
    if parsed.tzinfo is None:
        raise RiskInputError("capital_reconciled_at must include a timezone")
    try:
        moment = parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        raise RiskInputError("capital_reconciled_at outside supported UTC range") from None
    if moment > now:
        raise RiskInputError("capital_reconciled_at is in the future")
    if now - moment > MAX_RECONCILED_AGE:
        raise RiskInputError("capital_reconciled_at is stale")
    return moment


def build_status(
    raw: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    cycle_id: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Return policy status from an explicitly reconciled input snapshot.

    Required for a REAL_SEPARATED state:
      - capital_reconciled_usd
      - capital_reconciled_at
      - source
      - open_risk_usd
      - today_new_risk_usd
      - week_net_pnl_usd
      - cumulative_net_pnl_usd

    The output is advisory/fail-closed. execution_authorized is always False.
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    base = {
        "schema_version": RISK_SCHEMA,
        "cycle_id": cycle_id,
        "generated_at": generated_at or now.isoformat(),
        "reference_bank_usd": _fmt(REFERENCE_BANK),
        "reference_unit_cap_usd": _fmt(REFERENCE_UNIT),
        "weekly_pause_loss_usd": _fmt(WEEKLY_STOP),
        "experiment_pause_loss_usd": _fmt(EXPERIMENT_STOP),
        "execution_authorized": False,
        "order_capability_present": False,
    }
    if raw is None:
        return {
            **base,
            "bank_state": "PENDING_RECONCILIATION",
            "mode": "UNCONFIRMED",
            "capital_reconciled_usd": None,
            "capital_reconciled_at": None,
            "source": None,
            "unit_usd": _fmt(REFERENCE_UNIT),
            "max_open_risk_usd": _fmt(REFERENCE_UNIT * 3),
            "max_new_daily_risk_usd": _fmt(REFERENCE_UNIT * 3),
            "open_risk_usd": None,
            "today_new_risk_usd": None,
            "week_net_pnl_usd": None,
            "cumulative_net_pnl_usd": None,
            "new_risk_headroom_usd": "0.00",
            "real_entry_eligible": False,
            "reasons": ["bank real pendiente de conciliación", "modo real/simulado no confirmado"],
        }
    if not isinstance(raw, dict):
        raise RiskInputError("risk input must be an object")

    mode = raw.get("mode")
    if mode not in {"SIMULATION", "REAL_SEPARATED"}:
        raise RiskInputError("mode must be SIMULATION or REAL_SEPARATED")

    capital = _money(raw.get("capital_reconciled_usd"), field="capital_reconciled_usd")
    open_risk = _money(raw.get("open_risk_usd"), field="open_risk_usd")
    today_new = _money(raw.get("today_new_risk_usd"), field="today_new_risk_usd")
    week_pnl = _money(raw.get("week_net_pnl_usd"), field="week_net_pnl_usd", allow_negative=True)
    cumulative_pnl = _money(raw.get("cumulative_net_pnl_usd"), field="cumulative_net_pnl_usd", allow_negative=True)
    reconciled_at = _reconciled_at(raw.get("capital_reconciled_at"), now=now)
    source = raw.get("source")
    if source not in ALLOWED_DECLARATIVE_SOURCES:
        raise RiskInputError("source is not an allowed reconciliation source")
    for field in (
        "evidence_verified",
        "execution_authorized",
        "order_capability_present",
        "real_entry_eligible",
    ):
        if field in raw and raw[field] is not False:
            raise RiskInputError(f"{field} cannot grant authority")

    unit = min(REFERENCE_UNIT, (capital * Decimal("0.01")).quantize(CENT, rounding=ROUND_DOWN))
    max_open = (unit * 3).quantize(CENT)
    max_daily = (unit * 3).quantize(CENT)
    open_headroom = max(Decimal("0.00"), max_open - open_risk)
    daily_headroom = max(Decimal("0.00"), max_daily - today_new)
    loss_room_before_experiment_pause = max(Decimal("0.00"), EXPERIMENT_STOP + cumulative_pnl)
    experiment_headroom = max(Decimal("0.00"), loss_room_before_experiment_pause - open_risk)

    reasons: list[str] = []
    paused_weekly = week_pnl <= -WEEKLY_STOP
    paused_experiment = cumulative_pnl <= -EXPERIMENT_STOP
    if paused_weekly:
        reasons.append("pausa semanal activada por pérdida neta confirmada")
    if paused_experiment:
        reasons.append("pausa experimental activada por pérdida acumulada confirmada")
    if experiment_headroom <= 0 and not paused_experiment:
        reasons.append("pérdida máxima de posiciones abiertas agotaría el límite experimental")
    if open_headroom <= 0:
        reasons.append("límite de riesgo abierto agotado")
    if daily_headroom <= 0:
        reasons.append("presupuesto de nuevo riesgo diario agotado")
    if mode != "REAL_SEPARATED":
        reasons.append("estado en simulación; no habilita entradas reales")
    if capital <= 0:
        reasons.append("capital reconciliado no disponible")

    headroom = min(unit, open_headroom, daily_headroom, experiment_headroom)
    if paused_weekly or paused_experiment or mode != "REAL_SEPARATED" or capital <= 0:
        headroom = Decimal("0.00")

    illustrative_headroom = headroom
    headroom = Decimal("0.00")
    eligible = False
    reasons.append("account evidence is declarative; real entry remains disabled")

    return {
        **base,
        "bank_state": "DECLARED" if mode == "REAL_SEPARATED" else "SIMULATION",
        "mode": mode,
        "capital_reconciled_usd": _fmt(capital),
        "capital_reconciled_at": reconciled_at.isoformat(),
        "source": source,
        "unit_usd": _fmt(unit),
        "max_open_risk_usd": _fmt(max_open),
        "max_new_daily_risk_usd": _fmt(max_daily),
        "open_risk_usd": _fmt(open_risk),
        "today_new_risk_usd": _fmt(today_new),
        "week_net_pnl_usd": _fmt(week_pnl),
        "cumulative_net_pnl_usd": _fmt(cumulative_pnl),
        "new_risk_headroom_usd": _fmt(headroom),
        "illustrative_headroom_usd": _fmt(illustrative_headroom),
        "real_entry_eligible": eligible,
        "reasons": reasons,
    }


def _load_input(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise RiskInputError("risk input must be a regular file")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise RiskInputError("risk input too large")
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RiskInputError("risk input unreadable") from None
    if not isinstance(obj, dict):
        raise RiskInputError("risk input must be an object")
    return obj


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def write_status(
    data_dir: Path,
    *,
    now: datetime | None = None,
    cycle_id: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    input_path = data_dir / "risk-input.json"
    output_path = data_dir / "risk-status.json"
    try:
        status = build_status(
            _load_input(input_path),
            now=now,
            cycle_id=cycle_id,
            generated_at=generated_at,
        )
    except RiskInputError as exc:
        status = {
            "schema_version": RISK_SCHEMA,
            "cycle_id": cycle_id,
            "generated_at": generated_at or (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
            "bank_state": "INVALID_INPUT_FAIL_CLOSED",
            "reference_bank_usd": _fmt(REFERENCE_BANK),
            "reference_unit_cap_usd": _fmt(REFERENCE_UNIT),
            "execution_authorized": False,
            "order_capability_present": False,
            "real_entry_eligible": False,
            "new_risk_headroom_usd": "0.00",
            "reasons": [str(exc)],
        }
    _atomic_write(output_path, status)
    return status
