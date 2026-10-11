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

from us500_engine import (
    DataError,
    body_digest,
    decode,
    normalize,
    number,
    save_new,
    timestamp,
)
from us500_ledger import LedgerError, PaperLedger

MAX_FRAMES = 4096
SCHEMA = "us500-paper-session-v1"


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
    for frame in frames:
        if not isinstance(frame, dict) or set(frame) != {
            "at_ms",
            "snapshot",
            "funding_cashflow_usd",
            "infrastructure_cost_usd",
        }:
            raise DataError("EXPLICIT_INTERVAL_COSTS_REQUIRED")
        at = timestamp(frame["at_ms"])
        if at <= previous:
            raise DataError("NONINCREASING_FRAME_TIME")
        previous = at
        number(frame["funding_cashflow_usd"])
        if number(frame["infrastructure_cost_usd"]) < 0:
            raise DataError("NEGATIVE_INFRASTRUCTURE_COST")
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


def replay(data, database):
    events, report = build_plan(data)
    with _exclusive_database(database) as path:
        _pin_session(path, data)
        book = PaperLedger(path, data["ledger"])
        try:
            for event in events:
                book.apply(event)
            if book.state() != report["final_state"]:
                raise DataError("PERSISTED_REPLAY_DIVERGENCE")
        finally:
            book.close()
    report["durable_replay_verified"] = True
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.output.exists() or args.output.is_symlink():
            raise DataError("OUTPUT_ALREADY_EXISTS")
        result = replay(decode(args.input.read_bytes()), args.database)
        save_new(args.output, result)
    except (DataError, LedgerError, OSError, sqlite3.Error) as exc:
        print(
            json.dumps(
                {
                    "error": str(exc)
                    if isinstance(exc, (DataError, LedgerError))
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
                "durable_replay_verified": True,
                "execution_available": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
