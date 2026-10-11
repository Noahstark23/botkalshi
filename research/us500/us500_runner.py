"""Deterministic US500 paper runner. No network, credentials or order transport.

A frozen momentum hypothesis generates hypothetical targets at observation t;
only observation t+1 can supply liquidity. This is an evaluation harness, not
an assertion of positive expectancy. All funding and infrastructure costs are
explicit scenario inputs. A full deterministic replay makes crash recovery safe.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from pathlib import Path

from us500_control import ControlError, PaperControl
from us500_engine import (
    DataError,
    body_digest,
    decode,
    instrument_metadata,
    normalize,
    number,
    save_new,
    timestamp,
)
from us500_ledger import LedgerError, PaperLedger

MAX_FRAMES = 4096
SCHEMA = "us500-paper-session-v1"
MODEL_REVISION = 2


def textnum(value):
    return format(value, "f")


def compact_state(state):
    # Full order history appears once, in final_state, avoiding quadratic reports.
    return {k: v for k, v in state.items() if k != "orders"}


def validate_session(data):
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise DataError("INVALID_SESSION_SCHEMA")
    if not isinstance(data.get("run_id"), str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,64}", data["run_id"]
    ):
        raise DataError("INVALID_RUN_ID")
    if data.get("mode") != "paper" or not isinstance(data.get("ledger"), dict):
        raise DataError("PAPER_MODE_AND_EXPLICIT_LEDGER_REQUIRED")
    s = data.get("strategy")
    keys = {
        "name",
        "lookback",
        "threshold_bps",
        "quantity",
        "max_spread_bps",
        "slippage_bps",
        "assumptions_note",
    }
    if not isinstance(s, dict) or set(s) != keys or s["name"] != "momentum-v1":
        raise DataError("INVALID_FROZEN_STRATEGY")
    if type(s["lookback"]) is not int or not 2 <= s["lookback"] <= 256:
        raise DataError("INVALID_LOOKBACK")
    for key in ("threshold_bps", "quantity", "max_spread_bps"):
        number(s[key], positive=True)
    if not 0 <= number(s["slippage_bps"]) < 10000:
        raise DataError("INVALID_SLIPPAGE")
    if (
        not isinstance(s["assumptions_note"], str)
        or not 1 <= len(s["assumptions_note"]) <= 1000
    ):
        raise DataError("ASSUMPTIONS_NOTE_REQUIRED")
    frames = data.get("frames")
    if not isinstance(frames, list) or not 1 <= len(frames) <= MAX_FRAMES:
        raise DataError("INVALID_FRAME_COUNT")
    previous = 0
    assembly = data.get("assembly")
    if "assembly" in data:
        if (
            not isinstance(assembly, dict)
            or set(assembly)
            != {
                "schema",
                "instrument_metadata",
                "instrument_metadata_sha256",
                "synthetic_input",
                "observation_claim",
                "funding_and_costs",
            }
            or not isinstance(assembly.get("instrument_metadata"), dict)
            or assembly.get("schema") != "us500-session-assembly-v1"
            or type(assembly.get("synthetic_input")) is not bool
            or assembly.get("funding_and_costs")
            != "explicit_caller_inputs_not_account_statements"
            or assembly.get("instrument_metadata_sha256")
            != body_digest(assembly.get("instrument_metadata"))
        ):
            raise DataError("INVALID_SESSION_ASSEMBLY")
        claim = (
            "synthetic" if assembly["synthetic_input"] else "observed_unverified_origin"
        )
        if assembly.get("observation_claim") != claim:
            raise DataError("INVALID_SESSION_ASSEMBLY")
    for frame in frames:
        required = {
            "at_ms",
            "snapshot",
            "funding_cashflow_usd",
            "infrastructure_cost_usd",
        }
        optional = {"variation_margin_settlement_price", "margin_rates"}
        if (
            not isinstance(frame, dict)
            or not required <= set(frame)
            or set(frame) - required - optional
        ):
            raise DataError("EXPLICIT_INTERVAL_COSTS_REQUIRED")
        at = timestamp(frame["at_ms"])
        if at <= previous:
            raise DataError("NONINCREASING_FRAME_TIME")
        snapshot = frame.get("snapshot")
        if not isinstance(snapshot, dict):
            raise DataError("MALFORMED_SESSION_SNAPSHOT")
        evidence = snapshot.get("session_interval_cost_evidence")
        if assembly is not None or "session_interval_cost_evidence" in snapshot:
            _validate_interval_evidence(frame, evidence, previous or at)
        if assembly is not None:
            if (
                instrument_metadata(snapshot) != assembly["instrument_metadata"]
                or snapshot.get("synthetic") is not assembly["synthetic_input"]
            ):
                raise DataError("SESSION_ASSEMBLY_METADATA_MISMATCH")
        previous = at
        number(frame["funding_cashflow_usd"])
        if number(frame["infrastructure_cost_usd"]) < 0:
            raise DataError("NEGATIVE_INFRASTRUCTURE_COST")
        if "variation_margin_settlement_price" in frame:
            number(frame["variation_margin_settlement_price"], positive=True)
        if "margin_rates" in frame:
            rates = frame["margin_rates"]
            if not isinstance(rates, dict) or set(rates) != {
                "initial_margin_rate",
                "maintenance_margin_rate",
            }:
                raise DataError("INVALID_SCENARIO_MARGIN_RATES")
            if not (
                0
                < number(rates["maintenance_margin_rate"])
                <= number(rates["initial_margin_rate"])
                <= 1
            ):
                raise DataError("INVALID_SCENARIO_MARGIN_RATES")
    # Validate ledger configuration before opening a persistent file.
    probe = PaperLedger(":memory:", data["ledger"])
    probe.close()
    with localcontext() as ctx:
        ctx.prec = 80
        if number(s["quantity"]) % number(
            data["ledger"]["quantity_step"], positive=True
        ):
            raise DataError("STRATEGY_QUANTITY_OFF_STEP")
    return data


def _validate_interval_evidence(frame, evidence, previous_at):
    required = {
        "from_ms",
        "at_ms",
        "funding_cashflow_usd",
        "infrastructure_cost_usd",
        "provenance_note",
    }
    if not isinstance(evidence, dict) or set(evidence) != required:
        raise DataError("INTERVAL_COST_EVIDENCE_REQUIRED")
    if (
        timestamp(evidence["from_ms"]) != previous_at
        or timestamp(evidence["at_ms"]) != frame["at_ms"]
        or number(evidence["funding_cashflow_usd"])
        != number(frame["funding_cashflow_usd"])
        or number(evidence["infrastructure_cost_usd"])
        != number(frame["infrastructure_cost_usd"])
    ):
        raise DataError("INTERVAL_COST_EVIDENCE_MISMATCH")
    if (
        not isinstance(evidence["provenance_note"], str)
        or not 1 <= len(evidence["provenance_note"]) <= 1000
    ):
        raise DataError("INTERVAL_COST_PROVENANCE_REQUIRED")


def frame_observation(frame, config, identity):
    obs = normalize(frame["snapshot"], at_ms=frame["at_ms"])
    if not obs["payload_integrity_checked"]:
        raise DataError("SNAPSHOT_DIGEST_REQUIRED")
    current = tuple(
        obs[k]
        for k in (
            "ticker",
            "environment",
            "synthetic",
            "tick_size",
            "contract_size",
            "underlying_multiplier",
            "fractional_trading_enabled",
        )
    )
    current += (instrument_metadata(frame["snapshot"], allow_legacy_synthetic=True),)
    if identity is not None and identity != current:
        raise DataError("INSTRUMENT_CHANGED_DURING_SESSION")
    if number(config["tick_size"]) != obs["tick_size"]:
        raise DataError("CONFIG_TICK_MISMATCH")
    step = number(config["quantity_step"])
    if not obs["fractional_trading_enabled"] and step != step.to_integral_value():
        raise DataError("CONFIG_FRACTIONAL_STEP_MISMATCH")
    try:
        mark = frame["snapshot"]["observations"]["market"]["body"]["market"][
            "liquidation_mark_price"
        ]
        value = number(mark["price"], positive=True)
        age = frame["at_ms"] - timestamp(mark["ts_ms"])
        if not 0 <= age <= 5000:
            raise DataError("STALE_LIQUIDATION_MARK")
        if value >= Decimal(1000000000):
            raise DataError("INVALID_LIQUIDATION_MARK")
    except (KeyError, TypeError) as exc:
        raise DataError("LIQUIDATION_MARK_REQUIRED") from exc
    return obs, current, value


def build_plan(data):
    """Replay in isolated memory first; invalid schema cannot partly write a DB."""
    validate_session(data)
    config, strategy = data["ledger"], data["strategy"]
    events, reports, prices, identity = [], [], [], None
    pending_target = Decimal(0)
    previous_decision_at = None
    book = PaperLedger(":memory:", config)
    peak = number(config["initial_cash_usd"])
    max_drawdown = Decimal(0)
    economic_complete = True
    with localcontext() as ctx:
        ctx.prec = 80
        try:
            for i, frame in enumerate(data["frames"]):
                at = frame["at_ms"]
                prefix = f"{data['run_id']}:{i}"
                seq = 0

                def emit(kind, **fields):
                    nonlocal seq
                    event = {
                        "event_id": f"{prefix}:{seq}",
                        "at_ms": at,
                        "type": kind,
                        **fields,
                    }
                    state = book.apply(event)
                    events.append(event)
                    seq += 1
                    return state

                try:
                    obs, identity, mark = frame_observation(frame, config, identity)
                    earliest_request = min(
                        frame["snapshot"]["observations"][name]["requested_at_ms"]
                        for name in ("market", "orderbook")
                    )
                    if (
                        previous_decision_at is not None
                        and earliest_request <= previous_decision_at
                    ):
                        raise DataError("REUSED_OR_OVERLAPPING_CAPTURE")
                    previous_decision_at = at
                except DataError as exc:
                    emit("pause", reason=str(exc))
                    reports.append(
                        {
                            "frame": i,
                            "at_ms": at,
                            "status": "DATA_BLOCKED",
                            "reason": str(exc),
                            "state": compact_state(book.state()),
                        }
                    )
                    economic_complete = False
                    break
                if (
                    Decimal(book.state()["position_quantity"]) == 0
                    and number(frame["funding_cashflow_usd"]) != 0
                ):
                    raise DataError("FUNDING_WITHOUT_POSITION")
                state = emit("mark", price=textnum(mark))
                if "variation_margin_settlement_price" in frame:
                    state = emit(
                        "variation_margin",
                        settlement_price=frame["variation_margin_settlement_price"],
                    )
                if "margin_rates" in frame:
                    state = emit("margin_rates", **frame["margin_rates"])
                # Explicit signed scenario cash flow, not an estimated venue rate.
                state = emit("funding", cashflow_usd=frame["funding_cashflow_usd"])
                state = emit("cost", amount_usd=frame["infrastructure_cost_usd"])
                reason, filled = "WARMUP_OR_HOLD", Decimal(0)
                position = Decimal(state["position_quantity"])
                spread_ok = obs["spread_bps"] <= number(strategy["max_spread_bps"])
                if state["paused"]:
                    reason = "PAUSED_RESIDUAL_EXPOSURE_RETAINED"
                    economic_complete = False
                elif not spread_ok:
                    reason = "SPREAD_LIMIT"
                elif i > 0 and pending_target != position:
                    # Reversals first close existing exposure. A later observation
                    # must independently admit opening the opposite position.
                    closing = bool(
                        position
                        and (
                            pending_target == 0
                            or (pending_target > 0) != (position > 0)
                        )
                    )
                    delta = -position if closing else pending_target - position
                    side = "buy" if delta > 0 else "sell"
                    quantity = abs(delta)
                    levels = obs["asks"] if side == "buy" else obs["bids"]
                    slip = number(strategy["slippage_bps"]) / 10000
                    tick = number(config["tick_size"])
                    step = number(config["quantity_step"])
                    fills, left = [], quantity
                    for price, available in levels:
                        used = (min(left, available) / step).to_integral_value(
                            rounding=ROUND_FLOOR
                        ) * step
                        if used == 0:
                            continue
                        modeled = price * (1 + slip if side == "buy" else 1 - slip)
                        modeled = (modeled / tick).to_integral_value(
                            rounding=ROUND_CEILING if side == "buy" else ROUND_FLOOR
                        ) * tick
                        if modeled <= 0:
                            raise DataError("INVALID_MODELED_FILL_PRICE")
                        fills.append((used, modeled))
                        left -= used
                        if left == 0:
                            break
                    if fills:
                        oid = f"{prefix}:paper-order"
                        limit = (
                            max(p for _, p in fills)
                            if side == "buy"
                            else min(p for _, p in fills)
                        )
                        # Reserve the whole requested amount; partial fills only
                        # consume actually modeled book depth, remainder cancels.
                        reserved = emit(
                            "reserve",
                            order_id=oid,
                            side=side,
                            quantity=textnum(quantity),
                            limit_price=textnum(limit),
                            reduce_only=closing or abs(pending_target) < abs(position),
                        )
                        if reserved["last_event"]["status"] == "REJECTED":
                            reason = "RISK_REJECTED:" + reserved["last_event"]["reason"]
                        else:
                            for qty, price in fills:
                                emit(
                                    "fill",
                                    order_id=oid,
                                    quantity=textnum(qty),
                                    price=textnum(price),
                                )
                                filled += qty
                            if left > 0:
                                emit("cancel", order_id=oid)
                            reason = (
                                "HYPOTHETICAL_PARTIAL_IOC"
                                if left
                                else "HYPOTHETICAL_FILL"
                            )
                    else:
                        reason = "NO_USABLE_DEPTH"
                state = book.state()
                if state["paused"]:
                    economic_complete = False
                prices.append(obs["mid_usd_per_api_unit"])
                window = strategy["lookback"]
                momentum = None
                if len(prices) >= window:
                    momentum = (prices[-1] / prices[-window] - 1) * 10000
                    threshold = number(strategy["threshold_bps"])
                    pending_target = number(strategy["quantity"]) * (
                        1
                        if momentum > threshold
                        else -1
                        if momentum < -threshold
                        else 0
                    )
                if state["paused"] or not spread_ok:
                    pending_target = Decimal(0)
                equity = Decimal(state["equity_usd"])
                peak = max(peak, equity)
                max_drawdown = max(max_drawdown, peak - equity)
                reports.append(
                    {
                        "frame": i,
                        "at_ms": at,
                        "status": reason,
                        "hypothetical_filled_quantity": textnum(filled),
                        "momentum_bps": textnum(momentum)
                        if momentum is not None
                        else None,
                        "next_observation_target": textnum(pending_target),
                        "state": compact_state(state),
                    }
                )
            final = book.state()
        finally:
            book.close()
    return events, {
        "schema": "us500-paper-report-v1",
        "run_id": data["run_id"],
        "input_sha256": body_digest(data),
        "strategy_sha256": body_digest(strategy),
        "ledger_config_sha256": body_digest(config),
        "execution_available": False,
        "actual_orders": 0,
        "actual_fills": 0,
        "strategy_validated": False,
        "independent_settled_samples": 0,
        "input_provenance": {
            "synthetic_frames_declared": sum(
                f["snapshot"].get("synthetic") is True for f in data["frames"]
            ),
            "observed_frames_declared": sum(
                f["snapshot"].get("synthetic") is False for f in data["frames"]
            ),
            "source_authenticity_verified": False,
            "complete_market_session_verified": False,
            "independence_verified": False,
            "requested_frames": len(data["frames"]),
            "processed_frames": len(reports),
            "valid_observation_frames": sum(
                r["status"] != "DATA_BLOCKED" for r in reports
            ),
            "max_input_gap_ms": max(
                (
                    b["at_ms"] - a["at_ms"]
                    for a, b in zip(data["frames"], data["frames"][1:])
                ),
                default=0,
            ),
            "first_input_at_ms": data["frames"][0]["at_ms"],
            "last_input_at_ms": data["frames"][-1]["at_ms"],
            "assembly_cost_evidence_checked": "assembly" in data,
        },
        "hypothesis": strategy,
        "economic_path_complete_under_explicit_assumptions": economic_complete,
        "venue_liquidation_replicated": False,
        "final_state": final,
        "frames": reports,
        "ledger_events": len(events),
        "max_sampled_drawdown_usd": textnum(max_drawdown),
        "residual_position_quantity": final["position_quantity"],
        "limitations": [
            "Funding is caller-specified per interval, including any explicit zero.",
            "Displayed liquidity is hypothetical; queue, latency and intrainterval paths are unobserved.",
            "Margin rates are scenario assumptions; a breach pauses without inventing a liquidation fill.",
            "Momentum is frozen for evaluation and has no established positive expectancy.",
            "Residual positions remain marked, never silently closed at the final price.",
        ],
    }


@contextmanager
def _exclusive_database(path):
    path = Path(path)
    if path.is_symlink() or str(path) == ":memory:":
        raise DataError("INVALID_PERSISTENT_DATABASE")
    lockpath = Path(str(path) + ".lock")
    fd = os.open(lockpath, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DataError("PAPER_SESSION_BUSY") from exc
        yield path
    finally:
        os.close(fd)


def _pin_session(path, data):
    """Pin an append-only input prefix before applying any ledger event."""
    metadata = {k: v for k, v in data.items() if k != "frames"}
    metadata["_runner_model_revision"] = MODEL_REVISION
    frame_hashes = [body_digest(frame) for frame in data["frames"]]
    if path.exists():
        with closing(
            sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        ) as check:
            names = {
                row[0]
                for row in check.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "us500_runner_manifest" not in names:
                raise DataError("DATABASE_NOT_OWNED_BY_THIS_RUNNER")
    else:
        # Publish a fully initialized manifest atomically: interruption during
        # initialization cannot leave an unowned empty target DB on restart.
        fd, tmpname = tempfile.mkstemp(prefix=".us500-init-", dir=path.parent)
        os.close(fd)
        temporary = Path(tmpname)
        try:
            with closing(sqlite3.connect(temporary)) as init:
                init.execute(
                    "CREATE TABLE us500_runner_manifest (id INTEGER PRIMARY KEY CHECK(id=1), metadata TEXT NOT NULL, frames TEXT NOT NULL)"
                )
                init.execute(
                    "INSERT INTO us500_runner_manifest VALUES(1,?,?)",
                    (json.dumps(metadata, sort_keys=True), json.dumps(frame_hashes)),
                )
                init.commit()
            with temporary.open("rb") as saved:
                os.fsync(saved.fileno())
            os.link(temporary, path)
            dirfd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        finally:
            temporary.unlink(missing_ok=True)
    connection = sqlite3.connect(path)
    try:
        with connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS us500_runner_manifest (id INTEGER PRIMARY KEY CHECK(id=1), metadata TEXT NOT NULL, frames TEXT NOT NULL)"
            )
            old = connection.execute(
                "SELECT metadata, frames FROM us500_runner_manifest WHERE id=1"
            ).fetchone()
            if old:
                prior = json.loads(old[1])
                if json.loads(old[0]).get("_runner_model_revision") != MODEL_REVISION:
                    raise DataError("RUNNER_MODEL_VERSION_MISMATCH_USE_NEW_DATABASE")
                if (
                    json.loads(old[0]) != metadata
                    or len(frame_hashes) < len(prior)
                    or frame_hashes[: len(prior)] != prior
                ):
                    raise DataError("SESSION_INPUT_CHANGED_OR_TRUNCATED")
            connection.execute(
                "INSERT OR REPLACE INTO us500_runner_manifest VALUES(1,?,?)",
                (json.dumps(metadata, sort_keys=True), json.dumps(frame_hashes)),
            )
    finally:
        connection.close()


def replay(data, database, *, control=None):
    """Replay offline; an optional operator gate serializes every durable event.

    The same control DB lock protects a pause and an event commit. Once a pause
    is acknowledged, no later event passes its permit. A pause between reserve
    and fill retains that reserve; resuming replays those same event IDs.
    """
    events, report = build_plan(data)
    if control is not None and Path(database).resolve() == control.path.resolve():
        raise DataError("CONTROL_AND_LEDGER_DATABASE_MUST_DIFFER")
    with _exclusive_database(database) as path:

        def initialize(snapshot=None):
            if path.exists():
                with closing(
                    sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
                ) as prior:
                    if prior.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='us500_control_binding'"
                    ).fetchone():
                        if snapshot is None:
                            raise DataError("BOUND_PAPER_CONTROL_REQUIRED")
                        old = prior.execute(
                            "SELECT control_id FROM us500_control_binding WHERE id=1"
                        ).fetchone()
                        if not old or old[0] != snapshot["control_id"]:
                            raise DataError("PAPER_CONTROL_BINDING_CONFLICT")
            _pin_session(path, data)
            if snapshot is not None:
                with closing(sqlite3.connect(path)) as db:
                    with db:
                        db.execute(
                            "CREATE TABLE IF NOT EXISTS us500_control_binding (id INTEGER PRIMARY KEY CHECK(id=1), control_id TEXT NOT NULL)"
                        )
                        old = db.execute(
                            "SELECT control_id FROM us500_control_binding WHERE id=1"
                        ).fetchone()
                        if old and old[0] != snapshot["control_id"]:
                            raise DataError("PAPER_CONTROL_BINDING_CONFLICT")
                        db.execute(
                            "INSERT OR IGNORE INTO us500_control_binding VALUES(1, ?)",
                            (snapshot["control_id"],),
                        )
            return PaperLedger(path, data["ledger"])

        if control is None:
            book = initialize()
        else:
            permit = control.with_permit(initialize)
            if not permit["permitted"]:
                return {
                    "schema": "us500-paper-control-report-v1",
                    "run_id": data["run_id"],
                    "status": "PAPER_DISABLED",
                    "events_applied_this_call": 0,
                    "ledger_not_read": True,
                    "execution_available": False,
                    "actual_orders": 0,
                    "control": control.snapshot(),
                }
            book = permit["result"]
        applied = 0
        try:
            for event in events:
                if control is None:
                    book.apply(event)
                else:
                    permit = control.with_permit(lambda _snapshot: book.apply(event))
                    if not permit["permitted"]:
                        return {
                            "schema": "us500-paper-control-report-v1",
                            "run_id": data["run_id"],
                            "status": "OPERATOR_PAUSED",
                            "events_applied_this_call": applied,
                            "events_include_idempotent_replays": True,
                            "final_state": book.state(),
                            "execution_available": False,
                            "actual_orders": 0,
                            "control": control.snapshot(),
                        }
                applied += 1
            if book.state() != report["final_state"]:
                raise DataError("PERSISTED_REPLAY_DIVERGENCE")
        finally:
            book.close()
    report["durable_replay_verified"] = True
    if control is not None:
        report["control"] = control.snapshot()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--control-database", type=Path)
    parser.add_argument("--owner-user-id", type=int)
    parser.add_argument("--owner-chat-id", type=int)
    args = parser.parse_args()
    try:
        if args.output.exists() or args.output.is_symlink():
            raise DataError("OUTPUT_ALREADY_EXISTS")
        control_args = (args.control_database, args.owner_user_id, args.owner_chat_id)
        if any(v is not None for v in control_args) and any(
            v is None for v in control_args
        ):
            raise DataError("ALL_CONTROL_ARGUMENTS_REQUIRED")
        control = (
            PaperControl(
                args.control_database,
                owner_user_id=args.owner_user_id,
                owner_chat_id=args.owner_chat_id,
            )
            if args.control_database is not None
            else None
        )
        result = replay(decode(args.input.read_bytes()), args.database, control=control)
        save_new(args.output, result)
    except (DataError, LedgerError, ControlError, OSError, sqlite3.Error) as exc:
        print(
            json.dumps(
                {
                    "error": str(exc)
                    if isinstance(exc, (DataError, LedgerError, ControlError))
                    else "LOCAL_IO_ERROR",
                    "execution_available": False,
                }
            )
        )
        return 2
    print(
        json.dumps(
            {
                "saved": str(args.output),
                "durable_replay_verified": result.get("durable_replay_verified", False),
                "execution_available": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
