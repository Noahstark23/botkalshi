"""Cartera M5 FICTICIA: reserva antes de quote, fills y liquidación atómicos.

Sin cliente, red, configuración ni ejecutor. Sólo acepta propuestas explícitas;
no calcula señales ni probabilidades. SQLite comparte capital con paper_engine M2.
Reservamos ambos lados por separado y fees por contrato (cota conservadora ante
fills fragmentados), sin neteo optimista. Un fill hipotético exige cruce estricto
y profundidad >=1; como máximo un contrato por lado y observación.

Limitaciones: no modela cola/latencia. Un hueco mayor a 60s o una observación tras
expirar vuelve UNKNOWN la quote, conserva reservas y pausa durablemente admisión.
Cancelar exige observar el book en ese mismo instante; no borra fills anteriores.
La pausa no se levanta automáticamente. Toda liquidación es declarada, ficticia.
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import simulation_bank as bank
from paper_fees import fee_cents_conservative


class LedgerError(ValueError):
    """Evidencia incompleta o contradictoria: no se cambia el ledger."""


def _time(raw):
    if not isinstance(raw, str):
        raise LedgerError("timestamp must be a string")
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LedgerError("invalid timestamp") from exc
    if value.tzinfo is None:
        raise LedgerError("timestamp must be aware")
    return value.astimezone(UTC)


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise LedgerError("integer outside supported range")
    return value


def _json(value):
    return bank._canonical_evidence_json(value)


def _key(qid, side):
    return bank._parse_id(f"m5paper:{qid}:{side}", field="reservation")


def _fee(quote, price):
    if quote["fee_type"] == "quadratic":
        return 0  # Modelo maker general sin fee; no extrapolar a otra serie/override.
    return fee_cents_conservative(
        1, price, fee_type="maker", fee_multiplier=quote["fee_multiplier"]
    )


@contextlib.contextmanager
def _transaction(path):
    with bank._connect(Path(path)) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS m5_paper_quotes (
          quote_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, payload TEXT NOT NULL,
          state TEXT NOT NULL, last_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS m5_paper_legs (
          quote_id TEXT NOT NULL, side TEXT NOT NULL, price INTEGER NOT NULL,
          capacity INTEGER NOT NULL, filled INTEGER NOT NULL DEFAULT 0,
          fees INTEGER NOT NULL DEFAULT 0, reserve_key TEXT NOT NULL UNIQUE,
          PRIMARY KEY(quote_id,side)
        );
        CREATE TABLE IF NOT EXISTS m5_paper_observations (
          quote_id TEXT NOT NULL, observation_id TEXT NOT NULL, payload TEXT NOT NULL,
          result TEXT NOT NULL, PRIMARY KEY(quote_id,observation_id)
        );
        CREATE TABLE IF NOT EXISTS m5_paper_settlements (
          quote_id TEXT PRIMARY KEY, yes_payout INTEGER NOT NULL, pnl_cents INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS m5_paper_pause (id INTEGER PRIMARY KEY CHECK(id=1), reason TEXT);
        """)
        con.execute("BEGIN IMMEDIATE")
        try:
            bank._snapshot(con)  # Requiere banco ficticio inicializado explícitamente.
            yield con
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise


def _read(con, qid):
    row = con.execute(
        "SELECT payload,state,last_at FROM m5_paper_quotes WHERE quote_id=?", (qid,)
    ).fetchone()
    if row is None:
        raise LedgerError("unknown quote")
    return json.loads(row[0]), row[1], _time(row[2])


def _result(con, qid):
    quote, state, last = _read(con, qid)
    legs = con.execute(
        "SELECT side,price,capacity,filled,fees FROM m5_paper_legs WHERE quote_id=? ORDER BY side",
        (qid,),
    ).fetchall()
    expected_sides = {
        side
        for side, field in (("buy", "bid_cents"), ("sell", "ask_cents"))
        if quote[field] is not None
    }
    if {row[0] for row in legs} != expected_sides:
        raise LedgerError("quote legs incomplete")
    for side, price, capacity, filled, fees in legs:
        expected_amount = capacity * (
            (price if side == "buy" else 100 - price) + _fee(quote, price)
        )
        expected_status = (
            "RELEASED" if state == "SETTLED" or (state == "CANCELED" and not filled) else "ACTIVE"
        )
        reservation = con.execute(
            "SELECT amount_cents,status,origin,cycle_id FROM simulation_reservations WHERE idempotency_key=?",
            (_key(qid, side),),
        ).fetchone()
        if (
            reservation != (expected_amount, expected_status, "PAPER_M5", qid)
            or price != quote["bid_cents" if side == "buy" else "ask_cents"]
            or capacity != quote["count"]
            or not 0 <= filled <= capacity
            or fees != filled * _fee(quote, price)
        ):
            raise LedgerError("ledger and reservation disagree")
    return {
        "mode": "SIMULATION_ONLY",
        "execution_authorized": False,
        "order_capability_present": False,
        "evidence_state": "HYPOTHETICAL_LOCAL_LEDGER",
        "quote_id": qid,
        "ticker": quote["ticker"],
        "state": state,
        "last_at": last.isoformat(),
        "legs": [
            dict(zip(("side", "price_cents", "capacity", "filled", "fee_cents"), leg, strict=True))
            for leg in legs
        ],
        "bank": bank._snapshot(con),
        "paused": con.execute("SELECT reason FROM m5_paper_pause WHERE id=1").fetchone()
        is not None,
    }


def _book(raw, ticker, at):
    required = {"ticker", "observed_at", "yes_bid", "yes_ask", "bid_depth", "ask_depth"}
    if not isinstance(raw, dict) or set(raw) != required or raw["ticker"] != ticker:
        raise LedgerError("book identity or fields invalid")
    if not 0 <= (at - _time(raw["observed_at"])).total_seconds() <= 10:
        raise LedgerError("book stale or future")
    bid, ask = (_integer(raw[name], 1, 99) for name in ("yes_bid", "yes_ask"))
    if bid >= ask:
        raise LedgerError("crossed or locked book is ambiguous")
    depths = []
    for key in ("bid_depth", "ask_depth"):
        if type(raw[key]) not in (str, int) or len(str(raw[key])) > 32:
            raise LedgerError("depth must be bounded fixed point")
        try:
            value = Decimal(str(raw[key]))
        except InvalidOperation as exc:
            raise LedgerError("invalid depth") from exc
        if not value.is_finite() or not 0 <= value <= 1_000_000_000:
            raise LedgerError("invalid depth")
        depths.append(value)
    return bid, ask, *depths


def admit_quote(path, raw, book):
    """Acepta propuesta ficticia sólo si TODO su riesgo puede reservarse antes."""
    fields = {
        "quote_id",
        "ticker",
        "created_at",
        "expires_at",
        "bid_cents",
        "ask_cents",
        "count",
        "fee_type",
        "fee_multiplier",
    }
    if not isinstance(raw, dict) or set(raw) != fields:
        raise LedgerError("quote fields invalid")
    qid = bank._parse_id(raw["quote_id"], field="quote_id")
    bank._parse_id(raw["ticker"], field="ticker")
    created, expires = _time(raw["created_at"]), _time(raw["expires_at"])
    if not 0 < (expires - created).total_seconds() <= 600:
        raise LedgerError("quote lifetime must be 0..600 seconds")
    count = _integer(raw["count"], 1, 100)
    if raw["fee_type"] not in ("quadratic", "quadratic_with_maker_fees"):
        raise LedgerError("unsupported fee type")
    multiplier = raw["fee_multiplier"]
    if type(multiplier) not in (int, str) or len(str(multiplier)) > 16:
        raise LedgerError("fee multiplier must be exact")
    try:
        m = Decimal(str(multiplier))
    except InvalidOperation as exc:
        raise LedgerError("invalid multiplier") from exc
    if not m.is_finite() or not 0 < m <= 100:
        raise LedgerError("invalid multiplier")
    bid, ask, _, _ = _book(book, raw["ticker"], created)
    prices = {
        side: raw[name]
        for side, name in (("buy", "bid_cents"), ("sell", "ask_cents"))
        if raw[name] is not None
    }
    if not prices:
        raise LedgerError("quote has no sides")
    for side, price in prices.items():
        _integer(price, 1, 99)
        if (side == "buy" and price >= ask) or (side == "sell" and price <= bid):
            raise LedgerError("quote would be taker")
    if len(prices) == 2 and prices["buy"] >= prices["sell"]:
        raise LedgerError("quote crossed")
    payload = _json(raw)
    with _transaction(path) as con:
        previous = con.execute(
            "SELECT payload FROM m5_paper_quotes WHERE quote_id=?", (qid,)
        ).fetchone()
        if previous:
            if previous[0] != payload:
                raise LedgerError("quote identity reused with different payload")
            return _result(con, qid)  # Nunca reabre una quote cancelada/liquidada.
        if con.execute("SELECT 1 FROM m5_paper_pause").fetchone():
            raise LedgerError("simulation paused")
        if bank._market_payout(con, raw["ticker"]) is not None:
            raise LedgerError("market already settled")
        if con.execute(
            "SELECT 1 FROM m5_paper_quotes WHERE ticker=? AND state IN ('OPEN','UNKNOWN')",
            (raw["ticker"],),
        ).fetchone():
            raise LedgerError("ticker already has an exposed quote")
        for side, price in prices.items():
            collateral = price if side == "buy" else 100 - price
            key = _key(qid, side)
            if con.execute(
                "SELECT 1 FROM simulation_reservations WHERE idempotency_key=?", (key,)
            ).fetchone():
                raise LedgerError("orphan reservation conflicts with quote")
            bank._reserve_locked(
                con,
                key=key,
                origin_value="PAPER_M5",
                cycle_value=qid,
                amount_cents=count * (collateral + _fee(raw, price)),
                evidence_json=payload,
            )
            con.execute(
                "INSERT INTO m5_paper_legs VALUES (?,?,?,?,0,0,?)", (qid, side, price, count, key)
            )
        con.execute(
            "INSERT INTO m5_paper_quotes VALUES (?,?,?,'OPEN',?)",
            (qid, raw["ticker"], payload, created.isoformat()),
        )
        return _result(con, qid)


def observe(path, qid, *, observation_id, at, book):
    """Avanza una observación durable; reintentar el mismo ID no fabrica fills."""
    bank._parse_id(observation_id, field="observation_id")
    when = _time(at)
    payload = _json({"at": at, "book": book})
    with _transaction(path) as con:
        previous = con.execute(
            "SELECT payload,result FROM m5_paper_observations WHERE quote_id=? AND observation_id=?",
            (qid, observation_id),
        ).fetchone()
        if previous:
            if previous[0] != payload:
                raise LedgerError("observation identity conflict")
            return json.loads(previous[1])
        quote, state, last = _read(con, qid)
        if state != "OPEN" or when <= last:
            raise LedgerError("quote not open or observation out of order")
        bid, ask, bid_depth, ask_depth = _book(book, quote["ticker"], when)
        # Un snapshot anterior a la última decisión no demuestra un cruce nuevo.
        if _time(book["observed_at"]) <= last:
            raise LedgerError("book does not advance the observation clock")
        if when >= _time(quote["expires_at"]) or (when - last).total_seconds() > 60:
            con.execute("UPDATE m5_paper_quotes SET state='UNKNOWN' WHERE quote_id=?", (qid,))
            con.execute(
                "INSERT OR IGNORE INTO m5_paper_pause VALUES (1,?)",
                ("unobserved interval or expired quote",),
            )
        else:
            for side, price, capacity, filled, key in con.execute(
                "SELECT side,price,capacity,filled,reserve_key FROM m5_paper_legs WHERE quote_id=?",
                (qid,),
            ).fetchall():
                reserve = con.execute(
                    "SELECT status FROM simulation_reservations WHERE idempotency_key=?", (key,)
                ).fetchone()
                if reserve != ("ACTIVE",):
                    raise LedgerError("quote lost its capital reservation")
                crosses = (
                    ask < price and ask_depth >= 1
                    if side == "buy"
                    else bid > price and bid_depth >= 1
                )
                if crosses and filled < capacity:
                    con.execute(
                        "UPDATE m5_paper_legs SET filled=filled+1,fees=fees+? WHERE quote_id=? AND side=?",
                        (_fee(quote, price), qid, side),
                    )
        con.execute(
            "UPDATE m5_paper_quotes SET last_at=? WHERE quote_id=?", (when.isoformat(), qid)
        )
        result = _result(con, qid)
        con.execute(
            "INSERT INTO m5_paper_observations VALUES (?,?,?,?)",
            (qid, observation_id, payload, json.dumps(result, sort_keys=True)),
        )
        return result


def _release(con, key):
    if con.execute(
        "SELECT status FROM simulation_reservations WHERE idempotency_key=?", (key,)
    ).fetchone() != ("ACTIVE",):
        raise LedgerError("reservation is not active")
    now = bank._now()
    con.execute(
        "UPDATE simulation_reservations SET status='RELEASED',released_at=? WHERE idempotency_key=?",
        (now, key),
    )
    con.execute("UPDATE simulation_bank SET revision=revision+1,updated_at=? WHERE id=1", (now,))


def cancel_quote(path, qid, *, at):
    when = _time(at)
    with _transaction(path) as con:
        _, state, last = _read(con, qid)
        if state in ("CANCELED", "SETTLED"):
            if when != last:
                raise LedgerError("conflicting cancellation timestamp")
            return _result(con, qid)
        if state != "OPEN" or when != last:
            raise LedgerError("cancel requires a complete observation at the same instant")
        for filled, key in con.execute(
            "SELECT filled,reserve_key FROM m5_paper_legs WHERE quote_id=?", (qid,)
        ).fetchall():
            if filled == 0:
                _release(con, key)
        con.execute("UPDATE m5_paper_quotes SET state='CANCELED' WHERE quote_id=?", (qid,))
        return _result(con, qid)


def settle_quote(path, qid, *, yes_payout_cents):
    """YES=100, NO=0, empate=50; siempre explícito, nunca inferido de un nombre."""
    _integer(yes_payout_cents, 0, 100)
    with _transaction(path) as con:
        quote, state, _ = _read(con, qid)
        previous = con.execute(
            "SELECT yes_payout,pnl_cents FROM m5_paper_settlements WHERE quote_id=?", (qid,)
        ).fetchone()
        if previous:
            if previous[0] != yes_payout_cents:
                raise LedgerError("conflicting settlement")
            return {**_result(con, qid), "pnl_cents": previous[1]}
        if state != "CANCELED":
            raise LedgerError("settlement requires a canceled, fully observed quote")
        bank._record_market_payout_locked(con, quote["ticker"], yes_payout_cents)
        pnl = 0
        for side, price, filled, fees, key in con.execute(
            "SELECT side,price,filled,fees,reserve_key FROM m5_paper_legs WHERE quote_id=?", (qid,)
        ).fetchall():
            if not filled:
                continue
            collateral = (price if side == "buy" else 100 - price) * filled
            payout = (yes_payout_cents if side == "buy" else 100 - yes_payout_cents) * filled
            reserved = con.execute(
                "SELECT amount_cents FROM simulation_reservations WHERE idempotency_key=?", (key,)
            ).fetchone()[0]
            # Devuelve la garantía de contratos no llenados junto con el payout.
            bank._realize_locked(con, key=key, payout_cents=reserved - collateral - fees + payout)
            pnl += payout - collateral - fees
        con.execute("INSERT INTO m5_paper_settlements VALUES (?,?,?)", (qid, yes_payout_cents, pnl))
        con.execute("UPDATE m5_paper_quotes SET state='SETTLED' WHERE quote_id=?", (qid,))
        return {**_result(con, qid), "pnl_cents": pnl}


def snapshot(path, qid):
    with _transaction(path) as con:
        return _result(con, qid)


def summary(path):
    """Cartera M5 consistente; las posiciones pendientes son fills, no capacidad."""
    with _transaction(path) as con:
        quotes = [row[0] for row in con.execute("SELECT quote_id FROM m5_paper_quotes")]
        states = {}
        positions = {}
        total_filled = total_fees = 0
        for qid in quotes:
            result = _result(con, qid)  # No ocultar reservas corruptas en el informe.
            state = result["state"]
            states[state] = states.get(state, 0) + 1
            for leg in result["legs"]:
                total_filled += leg["filled"]
                total_fees += leg["fee_cents"]
                if state != "SETTLED" and leg["filled"]:
                    ticker = result["ticker"]
                    row = positions.setdefault(ticker, {"buy": 0, "sell": 0, "net_yes": 0})
                    row[leg["side"]] += leg["filled"]
                    row["net_yes"] += leg["filled"] * (1 if leg["side"] == "buy" else -1)
        pnl = con.execute("SELECT coalesce(sum(pnl_cents),0) FROM m5_paper_settlements").fetchone()[
            0
        ]
        reserved = con.execute(
            "SELECT coalesce(sum(amount_cents),0) FROM simulation_reservations "
            "WHERE origin='PAPER_M5' AND status='ACTIVE'"
        ).fetchone()[0]
        pause = con.execute("SELECT reason FROM m5_paper_pause WHERE id=1").fetchone()
        return {
            "mode": "SIMULATION_ONLY",
            "execution_authorized": False,
            "order_capability_present": False,
            "quotes_by_state": dict(sorted(states.items())),
            "filled_contracts": total_filled,
            "hypothetical_fees_cents": total_fees,
            "realized_pnl_cents": pnl,
            "reserved_cents": reserved,
            "unsettled_positions": dict(sorted(positions.items())),
            "paused": bool(pause),
            "pause_reason": pause[0] if pause else None,
        }
