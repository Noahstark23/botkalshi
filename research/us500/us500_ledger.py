"""Durable local ledger for hypothetical linear futures, never venue accounting.

No sockets, credentials, production imports, external orders, or liquidation fills.
Cash changes only by realized PnL, explicit fees, signed funding, and explicit cost.
Margin rates and fee rounding are assumptions supplied by the caller.
"""

from __future__ import annotations

import json
import re
import sqlite3
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext
from pathlib import Path


MAX_EVENTS = 100_000
MAX_ORDERS = 8_192


class LedgerError(ValueError):
    """Stable validation or conflict error, without raw input values."""


_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,12})?\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_CONFIG = {
    "initial_cash_usd",
    "initial_margin_rate",
    "maintenance_margin_rate",
    "fee_bps",
    "fee_quantum_usd",
    "max_notional_usd",
    "max_loss_usd",
    "max_gap_ms",
    "quantity_step",
    "tick_size",
}
_FIELDS = {
    "reserve": {"order_id", "side", "quantity", "limit_price", "reduce_only"},
    "fill": {"order_id", "quantity", "price"},
    "cancel": {"order_id"},
    "unknown": {"order_id"},
    "mark": {"price"},
    "funding": {"cashflow_usd"},
    "cost": {"amount_usd"},
    "pause": {"reason"},
}


def _decimal(value: object, *, positive=False, nonnegative=False) -> Decimal:
    if not isinstance(value, str) or not _NUMBER.fullmatch(value):
        raise LedgerError("INVALID_DECIMAL_TEXT")
    result = Decimal(value)
    if (positive and result <= 0) or (nonnegative and result < 0):
        raise LedgerError("INVALID_DECIMAL_SIGN")
    return result


def _id(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise LedgerError("INVALID_IDENTIFIER")
    return value


def _text(value: Decimal) -> str:
    return format(value, "f") if value else "0"


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _check_config(config: dict) -> dict:
    if type(config) is not dict or set(config) != _CONFIG:
        raise LedgerError("INVALID_CONFIG_FIELDS")
    checked = {}
    for key in _CONFIG - {"max_gap_ms"}:
        checked[key] = _decimal(config[key], nonnegative=True)
    if any(checked[key] <= 0 for key in _CONFIG - {"max_gap_ms", "fee_bps"}):
        raise LedgerError("NONPOSITIVE_CONFIG")
    if (
        not 0
        < checked["maintenance_margin_rate"]
        <= checked["initial_margin_rate"]
        <= 1
    ):
        raise LedgerError("INVALID_MARGIN_RATES")
    if checked["fee_bps"] > 10000:
        raise LedgerError("INVALID_FEE_RATE")
    if checked["quantity_step"] < Decimal("0.01") or checked["quantity_step"] % Decimal(
        "0.01"
    ):
        raise LedgerError("INVALID_QUANTITY_STEP")
    if (
        type(config["max_gap_ms"]) is not int
        or not 1 <= config["max_gap_ms"] <= 86400000
    ):
        raise LedgerError("INVALID_MAX_GAP")
    checked["max_gap_ms"] = config["max_gap_ms"]
    return checked


class PaperLedger:
    """Transactional replay ledger. Rejected risk requests are durable events.

    Event keys: event_id, type, at_ms and the fields documented in _FIELDS.
    Malformed/conflicting events raise LedgerError with a complete rollback.
    A valid but inadmissible reserve returns last_event.status=REJECTED; no order
    is created. All other events represent supplied facts, including while paused.
    cancel means an authoritative simulated terminal cancellation, not a request.
    No resume event exists; pauses are durable. UNKNOWN never releases a reserve.
    """

    def __init__(self, path: str | Path, config: dict):
        with localcontext() as ctx:
            ctx.prec = 80
            self.config = _check_config(config)
        self._config_json = _canonical(config)
        self._db = sqlite3.connect(str(path), isolation_level=None, timeout=5)
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA synchronous=FULL")
        try:
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS ledger_state (id INTEGER PRIMARY KEY CHECK(id=1), config TEXT NOT NULL, state TEXT NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS ledger_events (event_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            row = self._db.execute(
                "SELECT config FROM ledger_state WHERE id=1"
            ).fetchone()
            if row and row[0] != self._config_json:
                raise LedgerError("CONFIG_CONFLICT")
            if not row:
                initial = {
                    "schema": "us500-paper-ledger-v1",
                    "initial_cash_usd": config["initial_cash_usd"],
                    "cash_usd": config["initial_cash_usd"],
                    "position_quantity": "0",
                    "average_entry_price": "0",
                    "realized_pnl_usd": "0",
                    "fees_usd": "0",
                    "funding_usd": "0",
                    "costs_usd": "0",
                    "mark_price": None,
                    "paused": False,
                    "pause_reasons": [],
                    "orders": {},
                    "last_at_ms": None,
                    "last_event": None,
                    "event_count": 0,
                    "last_mark_at_ms": None,
                    "real_orders": 0,
                    "execution_available": False,
                    "margin_model": "SIMPLIFIED_HYPOTHETICAL_NOT_VENUE_LIQUIDATION",
                    "fee_model": "ASSUMED_CEILING_PER_FILL_RESERVE_MINIMUM_SIZE_FRAGMENTS",
                }
                self._db.execute(
                    "INSERT INTO ledger_state VALUES (1, ?, ?)",
                    (self._config_json, _canonical(initial)),
                )
            self._db.execute("COMMIT")
        except Exception:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            self._db.close()
            raise

    def close(self):
        self._db.close()

    def _load(self) -> dict:
        row = self._db.execute("SELECT state FROM ledger_state WHERE id=1").fetchone()
        if row is None:
            raise LedgerError("MISSING_LEDGER_STATE")
        return json.loads(row[0])

    def _fee(self, notional: Decimal) -> Decimal:
        quantum = self.config["fee_quantum_usd"]
        return (notional * self.config["fee_bps"] / 10000 / quantum).to_integral_value(
            rounding=ROUND_CEILING
        ) * quantum

    def _reservation(self, order: dict) -> Decimal:
        if order["status"] not in {"OPEN", "PARTIAL", "UNKNOWN"}:
            return Decimal(0)
        remaining = Decimal(order["remaining_quantity"])
        notional = remaining * Decimal(order["reserve_price"])
        margin = (
            Decimal(0)
            if order["reduce_only"]
            else notional * self.config["initial_margin_rate"]
        )
        # Every minimum-size partial fill may round its fee upward separately.
        # Reserve that worst case, not just one rounding for the whole order.
        fragments = remaining / self.config["quantity_step"]
        fee_reserve = fragments * self._fee(
            self.config["quantity_step"] * Decimal(order["reserve_price"])
        )
        return margin + fee_reserve

    def _metrics(self, state: dict) -> dict:
        position = Decimal(state["position_quantity"])
        average = Decimal(state["average_entry_price"])
        mark = Decimal(state["mark_price"]) if state["mark_price"] is not None else None
        if position and mark is None:
            raise LedgerError("POSITION_WITHOUT_MARK")
        unrealized = position * (mark - average) if position else Decimal(0)
        notional = abs(position) * mark if position else Decimal(0)
        equity = Decimal(state["cash_usd"]) + unrealized
        reserved = sum(
            (self._reservation(order) for order in state["orders"].values()), Decimal(0)
        )
        open_notional = sum(
            (
                Decimal(o["remaining_quantity"]) * Decimal(o["reserve_price"])
                for o in state["orders"].values()
                if o["status"] in {"OPEN", "PARTIAL", "UNKNOWN"}
                and not o["reduce_only"]
            ),
            Decimal(0),
        )
        initial_margin = notional * self.config["initial_margin_rate"]
        return {
            "unrealized_pnl_usd": _text(unrealized),
            "equity_usd": _text(equity),
            "position_cost_usd": _text(abs(position) * average),
            "notional_usd": _text(notional),
            "open_order_notional_usd": _text(open_notional),
            "reserved_margin_usd": _text(reserved),
            "initial_margin_usd": _text(initial_margin),
            "maintenance_margin_usd": _text(
                notional * self.config["maintenance_margin_rate"]
            ),
            "available_margin_usd": _text(equity - initial_margin - reserved),
            "net_pnl_usd": _text(equity - self.config["initial_cash_usd"]),
        }

    def _output(self, state: dict) -> dict:
        result = json.loads(_canonical(state))
        result.update(self._metrics(state))
        for order in result["orders"].values():
            order["reserved_margin_usd"] = _text(self._reservation(order))
        return result

    def state(self) -> dict:
        with localcontext() as ctx:
            ctx.prec = 80
            return self._output(self._load())

    @staticmethod
    def _pause(state: dict, reason: str):
        state["paused"] = True
        if reason not in state["pause_reasons"]:
            state["pause_reasons"].append(reason)

    def _enforce(self, state: dict):
        metrics = self._metrics(state)
        equity = Decimal(metrics["equity_usd"])
        if Decimal(metrics["net_pnl_usd"]) <= -self.config["max_loss_usd"]:
            self._pause(state, "MAX_LOSS_BREACH")
        if equity < Decimal(metrics["maintenance_margin_usd"]):
            self._pause(state, "MAINTENANCE_MARGIN_BREACH")
        if Decimal(metrics["available_margin_usd"]) < 0:
            self._pause(state, "INITIAL_MARGIN_BREACH")
        if (
            Decimal(metrics["notional_usd"])
            + Decimal(metrics["open_order_notional_usd"])
            > self.config["max_notional_usd"]
        ):
            self._pause(state, "MAX_NOTIONAL_BREACH")

    def _validate_event(self, event: dict) -> dict:
        if (
            type(event) is not dict
            or not isinstance(event.get("type"), str)
            or event["type"] not in _FIELDS
        ):
            raise LedgerError("INVALID_EVENT_TYPE")
        kind = event["type"]
        if set(event) != {"event_id", "type", "at_ms"} | _FIELDS[kind]:
            raise LedgerError("INVALID_EVENT_FIELDS")
        _id(event["event_id"])
        if (
            type(event["at_ms"]) is not int
            or not 946684800000 <= event["at_ms"] <= 4102444800000
        ):
            raise LedgerError("INVALID_TIMESTAMP")
        result = dict(event)
        if "order_id" in event:
            _id(event["order_id"])
        for key in ("quantity", "limit_price", "price"):
            if key in event:
                result[key] = _decimal(event[key], positive=True)
                step = (
                    self.config["quantity_step"]
                    if key == "quantity"
                    else self.config["tick_size"]
                )
                if kind != "mark" and result[key] % step:
                    raise LedgerError("OFF_STEP_VALUE")
        if kind == "reserve" and (
            event["side"] not in ("buy", "sell")
            or type(event["reduce_only"]) is not bool
        ):
            raise LedgerError("INVALID_ORDER_FLAGS")
        if kind == "funding":
            result["cashflow_usd"] = _decimal(event["cashflow_usd"])
        if kind == "cost":
            result["amount_usd"] = _decimal(event["amount_usd"], nonnegative=True)
        if kind == "pause":
            _id(event["reason"])
        return result

    def _reserve(self, state: dict, event: dict) -> str | None:
        order_id = event["order_id"]
        if order_id in state["orders"]:
            raise LedgerError("ORDER_ID_CONFLICT")
        if state["paused"] and not event["reduce_only"]:
            return "LEDGER_PAUSED"
        if state["mark_price"] is None:
            return "MARK_REQUIRED"
        position = Decimal(state["position_quantity"])
        direction = 1 if event["side"] == "buy" else -1
        if event["reduce_only"]:
            committed = sum(
                (
                    Decimal(o["remaining_quantity"])
                    for o in state["orders"].values()
                    if o["status"] in {"OPEN", "PARTIAL", "UNKNOWN"}
                    and o["reduce_only"]
                    and o["side"] == event["side"]
                ),
                Decimal(0),
            )
            if position * direction >= 0 or event["quantity"] + committed > abs(
                position
            ):
                return "REDUCE_ONLY_EXCEEDS_POSITION"
        order = {
            "side": event["side"],
            "quantity": _text(event["quantity"]),
            "remaining_quantity": _text(event["quantity"]),
            "filled_quantity": "0",
            "limit_price": _text(event["limit_price"]),
            "reduce_only": event["reduce_only"],
            "reserve_price": _text(
                max(event["limit_price"], Decimal(state["mark_price"]))
            ),
            "status": "OPEN",
        }
        metrics = self._metrics(state)
        if not event["reduce_only"] and self._reservation(order) > Decimal(
            metrics["available_margin_usd"]
        ):
            return "INSUFFICIENT_MARGIN"
        extra_notional = (
            Decimal(0)
            if order["reduce_only"]
            else event["quantity"] * Decimal(order["reserve_price"])
        )
        if (
            not event["reduce_only"]
            and Decimal(metrics["notional_usd"])
            + Decimal(metrics["open_order_notional_usd"])
            + extra_notional
            > self.config["max_notional_usd"]
        ):
            return "MAX_NOTIONAL_LIMIT"
        state["orders"][order_id] = order
        return None

    def _fill(self, state: dict, event: dict, order: dict):
        qty, price = event["quantity"], event["price"]
        remaining = Decimal(order["remaining_quantity"])
        if qty > remaining:
            raise LedgerError("FILL_EXCEEDS_REMAINING")
        if (order["side"] == "buy" and price > Decimal(order["limit_price"])) or (
            order["side"] == "sell" and price < Decimal(order["limit_price"])
        ):
            raise LedgerError("FILL_VIOLATES_LIMIT")
        old = Decimal(state["position_quantity"])
        average = Decimal(state["average_entry_price"])
        signed = qty if order["side"] == "buy" else -qty
        if order["reduce_only"] and (old * signed >= 0 or qty > abs(old)):
            raise LedgerError("FILL_VIOLATES_REDUCE_ONLY")
        new = old + signed
        realized = Decimal(0)
        if not old or old * signed > 0:
            average = (abs(old) * average + qty * price) / abs(new)
        else:
            closed = min(abs(old), qty)
            realized = closed * (price - average) * (1 if old > 0 else -1)
            if not new:
                average = Decimal(0)
            elif old * new < 0:
                average = price
        fee = self._fee(qty * price)
        state["position_quantity"] = _text(new)
        state["average_entry_price"] = _text(average)
        state["realized_pnl_usd"] = _text(Decimal(state["realized_pnl_usd"]) + realized)
        state["fees_usd"] = _text(Decimal(state["fees_usd"]) + fee)
        state["cash_usd"] = _text(Decimal(state["cash_usd"]) + realized - fee)
        order["remaining_quantity"] = _text(remaining - qty)
        order["filled_quantity"] = _text(Decimal(order["filled_quantity"]) + qty)
        order["status"] = (
            "FILLED"
            if qty == remaining
            else ("UNKNOWN" if order["status"] == "UNKNOWN" else "PARTIAL")
        )

    def _dispatch(self, state: dict, event: dict) -> str | None:
        kind = event["type"]
        if kind == "reserve":
            return self._reserve(state, event)
        if kind in {"fill", "cancel", "unknown"}:
            order = state["orders"].get(event["order_id"])
            if order is None:
                raise LedgerError("UNKNOWN_ORDER_ID")
            if order["status"] not in {"OPEN", "PARTIAL", "UNKNOWN"}:
                raise LedgerError("ORDER_ALREADY_TERMINAL")
            if kind == "fill":
                self._fill(state, event, order)
            elif kind == "cancel":
                order["status"] = "CANCELED"
            else:
                order["status"] = "UNKNOWN"
                self._pause(state, "UNKNOWN_ORDER")
        elif kind == "mark":
            state["mark_price"] = _text(event["price"])
            state["last_mark_at_ms"] = event["at_ms"]
            for order in state["orders"].values():
                if order["status"] in {"OPEN", "PARTIAL", "UNKNOWN"}:
                    order["reserve_price"] = _text(
                        max(Decimal(order["reserve_price"]), event["price"])
                    )
        elif kind == "funding":
            amount = event["cashflow_usd"]
            if amount and not Decimal(state["position_quantity"]):
                raise LedgerError("FUNDING_WITHOUT_POSITION")
            state["funding_usd"] = _text(Decimal(state["funding_usd"]) + amount)
            state["cash_usd"] = _text(Decimal(state["cash_usd"]) + amount)
        elif kind == "cost":
            amount = event["amount_usd"]
            state["costs_usd"] = _text(Decimal(state["costs_usd"]) + amount)
            state["cash_usd"] = _text(Decimal(state["cash_usd"]) - amount)
        elif kind == "pause":
            self._pause(state, "MANUAL:" + event["reason"])
        return None

    def apply(self, event: dict) -> dict:
        with localcontext() as ctx:
            ctx.prec = 80
            try:
                parsed = self._validate_event(event)
                payload = _canonical(event)
                self._db.execute("BEGIN IMMEDIATE")
                prior = self._db.execute(
                    "SELECT payload FROM ledger_events WHERE event_id=?",
                    (event["event_id"],),
                ).fetchone()
                if prior:
                    if prior[0] != payload:
                        raise LedgerError("EVENT_ID_CONFLICT")
                    result = self._output(self._load())
                    self._db.execute("COMMIT")
                    return result
                state = self._load()
                if state["event_count"] >= MAX_EVENTS:
                    raise LedgerError("EVENT_CAPACITY_EXHAUSTED")
                if (
                    parsed["type"] == "reserve"
                    and parsed["order_id"] not in state["orders"]
                    and len(state["orders"]) >= MAX_ORDERS
                ):
                    raise LedgerError("ORDER_CAPACITY_EXHAUSTED")
                last = state["last_at_ms"]
                if last is not None and parsed["at_ms"] < last:
                    raise LedgerError("NONMONOTONIC_TIMESTAMP")
                if (
                    last is not None
                    and parsed["at_ms"] - last > self.config["max_gap_ms"]
                ):
                    self._pause(state, "OBSERVATION_GAP")
                if (
                    state["last_mark_at_ms"] is not None
                    and parsed["at_ms"] - state["last_mark_at_ms"]
                    > self.config["max_gap_ms"]
                ):
                    self._pause(state, "STALE_MARK")
                rejection = self._dispatch(state, parsed)
                self._enforce(state)
                state["last_at_ms"] = parsed["at_ms"]
                state["event_count"] += 1
                state["last_event"] = {
                    "event_id": event["event_id"],
                    "type": parsed["type"],
                    "status": "REJECTED" if rejection else "APPLIED",
                    "reason": rejection,
                }
                self._db.execute(
                    "INSERT INTO ledger_events VALUES (?, ?)",
                    (event["event_id"], payload),
                )
                self._db.execute(
                    "UPDATE ledger_state SET state=? WHERE id=1", (_canonical(state),)
                )
                result = self._output(state)
                self._db.execute("COMMIT")
                return result
            except (InvalidOperation, OverflowError) as exc:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise LedgerError("DECIMAL_ARITHMETIC_ERROR") from exc
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
