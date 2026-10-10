"""Liquidación de una captura DECLARADA, exclusivamente en el banco ficticio.

No consulta APIs ni autentica el origen del JSON. Un resultado publicado por el
exchange es distinto de la conciliación de una cuenta: este adaptador sólo usa
el primero para reproducir posiciones hipotéticas. No infiere fills ni resuelve
órdenes UNKNOWN. El stream guarda el JSON íntegro antes del efecto contable.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import m5_paper_ledger as m5
import paper_engine as m2


class SettlementSnapshotError(ValueError):
    """Una captura incompleta/contradictoria no puede liberar capital."""


def _time(value):
    if not isinstance(value, str) or len(value) > 40:
        raise SettlementSnapshotError("SETTLEMENT_TIME_INVALID")
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SettlementSnapshotError("SETTLEMENT_TIME_INVALID") from exc
    if when.tzinfo is None:
        raise SettlementSnapshotError("SETTLEMENT_TIME_INVALID")
    return when.astimezone(UTC)


def _dollars(value):
    # Sin float, exponenciales, NaN, redondeo ni unidades ambiguas.
    if not isinstance(value, str) or not re.fullmatch(r"[01]\.\d{4}", value):
        raise SettlementSnapshotError("SETTLEMENT_DOLLARS_INVALID")
    number = Decimal(value)
    if number > 1:
        raise SettlementSnapshotError("SETTLEMENT_DOLLARS_INVALID")
    return number


def review_snapshot(event):
    """Valida primero TODO el pago y su identidad; no escribe ni abre la DB."""
    fields = {
        "type",
        "target",
        "ticker",
        "rules_sha256",
        "market",
        "observed_at",
        "quote_id",
    }
    if (
        not isinstance(event, dict)
        or set(event) - fields
        or not (fields - {"quote_id"}) <= set(event)
        or event["type"] != "settlement_snapshot"
        or event["target"] not in ("M2", "M5")
    ):
        raise SettlementSnapshotError("SETTLEMENT_EVENT_INVALID")
    ticker, market = event["ticker"], event["market"]
    if (
        not isinstance(ticker, str)
        or not re.fullmatch(r"KX(?:NFL|NCAAF)GAME-[A-Z0-9]+-[A-Z0-9]+", ticker)
        or not isinstance(market, dict)
        or market.get("ticker") != ticker
        or market.get("event_ticker") != ticker.rsplit("-", 1)[0]
        or market.get("market_type") != "binary"
    ):
        raise SettlementSnapshotError("SETTLEMENT_IDENTITY_MISMATCH")
    if event["target"] == "M5":
        qid = event.get("quote_id")
        if not isinstance(qid, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", qid):
            raise SettlementSnapshotError("SETTLEMENT_QUOTE_ID_INVALID")
    elif "quote_id" in event:
        raise SettlementSnapshotError("M2_QUOTE_ID_UNEXPECTED")
    digest = event["rules_sha256"]
    texts = [market.get(k) for k in ("rules_primary", "rules_secondary")]
    if (
        not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or any(not isinstance(t, str) for t in texts)
        or not texts[0]
        or hashlib.sha256("\n".join(texts).encode()).hexdigest() != digest
    ):
        raise SettlementSnapshotError("SETTLEMENT_RULES_CHANGED_OR_MISSING")
    # REST status enum del OpenAPI: 'settled' es un filtro/evento WS, no el
    # valor terminal de market.status. 'determined' ya puede traer precio.
    if market.get("status") != "finalized":
        raise SettlementSnapshotError("MARKET_NOT_FINALIZED")
    settled = _time(market.get("settlement_ts"))
    if settled > _time(event["observed_at"]):
        raise SettlementSnapshotError("SETTLEMENT_IN_FUTURE")
    if _dollars(market.get("notional_value_dollars")) != 1:
        raise SettlementSnapshotError("SETTLEMENT_NOTIONAL_UNSUPPORTED")
    if market.get("settlement_bounds_type") != "default" or (
        market.get("settlement_floor_dollars") is not None
        and _dollars(market["settlement_floor_dollars"]) != 0
    ):
        raise SettlementSnapshotError("SETTLEMENT_ADVANCES_UNSUPPORTED")
    payout = _dollars(market.get("settlement_value_dollars")) * 100
    if payout != payout.to_integral_value():
        raise SettlementSnapshotError("SUBCENT_SETTLEMENT_UNSUPPORTED_BY_PAPER_LEDGER")
    payout = int(payout)
    result = market.get("result")
    if result not in ("yes", "no", "scalar") or (
        result != "scalar" and payout != (100 if result == "yes" else 0)
    ):
        raise SettlementSnapshotError("SETTLEMENT_RESULT_PRICE_CONFLICT")
    if "settlement_value" in market and (
        type(market["settlement_value"]) is not int or market["settlement_value"] != payout
    ):
        raise SettlementSnapshotError("SETTLEMENT_LEGACY_PRICE_CONFLICT")
    return {
        "ticker": ticker,
        "yes_payout_cents": payout,
        "no_payout_cents": 100 - payout,
        "rules_sha256": digest,
        "settlement_ts": settled.isoformat(),
        "evidence": "DECLARED_SNAPSHOT_REPLAY_NOT_ACCOUNT_RECONCILIATION",
        "account_reconciled": False,
        "execution_authorized": False,
        "settlement_fee_model": "WHOLE_CONTRACTS_WHOLE_CENTS_NO_ADVANCES",
    }


def apply_to_paper(db, event):
    """Reutiliza los ledgers idempotentes; jamás cancela automáticamente una quote."""
    reviewed = review_snapshot(event)
    ticker, payout = reviewed["ticker"], reviewed["yes_payout_cents"]
    settled = _time(reviewed["settlement_ts"])
    if event["target"] == "M2":
        with m2._connect(Path(db)) as con:
            rows = con.execute(
                "SELECT origin, signal_json FROM paper_fills WHERE ticker=? AND filled_count>0",
                (ticker,),
            ).fetchall()
            if not rows:
                raise SettlementSnapshotError("NO_M2_PAPER_FILLS")
            for origin, raw in rows:
                if origin != "M2" or not raw:
                    raise SettlementSnapshotError("SETTLEMENT_POSITION_SCOPE_UNKNOWN")
                if _time(json.loads(raw).get("signal_at")) > settled:
                    raise SettlementSnapshotError("PAPER_ACTIVITY_AFTER_SETTLEMENT")
        result = m2.settle_market(db, ticker=ticker, yes_payout_cents=payout)
    else:
        quote = m5.snapshot(db, event["quote_id"])
        if quote["ticker"] != ticker:
            raise SettlementSnapshotError("SETTLEMENT_QUOTE_TICKER_MISMATCH")
        if _time(quote["last_at"]) > settled:
            raise SettlementSnapshotError("PAPER_ACTIVITY_AFTER_SETTLEMENT")
        result = m5.settle_quote(db, event["quote_id"], yes_payout_cents=payout)
    return {**reviewed, "target": event["target"], "paper_result": result}
