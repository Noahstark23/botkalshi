"""Inspección offline de libros fixed-point archivados; no adapta cantidades a int.

El endpoint REST no prueba continuidad de secuencia ni hora del exchange. La
antigüedad aquí mide RECEPCIÓN, nunca se interpreta como prueba de fills.
"""

from decimal import Decimal, InvalidOperation

from football_replay_contract import _time


def _decimal(raw):
    if not isinstance(raw, str) or not 1 <= len(raw) <= 32:
        raise ValueError("FIXED_POINT_STRING_REQUIRED")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("FIXED_POINT_INVALID") from exc
    if not value.is_finite():
        raise ValueError("FIXED_POINT_NONFINITE")
    return value


def inspect_book(payload, *, received_at, evaluated_at):
    """Bilateralidad y precisión para el simulador en céntimos; no genera quotes."""
    age = (_time(evaluated_at) - _time(received_at)).total_seconds()
    blockers = [] if 0 <= age <= 10 else ["BOOK_RECEIPT_STALE_OR_FUTURE"]
    if not isinstance(payload, dict) or set(payload) != {"orderbook_fp"}:
        raise ValueError("ORDERBOOK_SHAPE_INVALID")
    book = payload["orderbook_fp"]
    if not isinstance(book, dict) or set(book) != {"yes_dollars", "no_dollars"}:
        raise ValueError("ORDERBOOK_SIDES_INVALID")
    levels = {}
    for side in ("yes", "no"):
        rows = book[side + "_dollars"]
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError("ORDERBOOK_DEPTH_INVALID")
        prices, parsed = set(), []
        for row in rows:
            if not isinstance(row, list) or len(row) != 2:
                raise ValueError("ORDERBOOK_LEVEL_INVALID")
            price, count = map(_decimal, row)
            if not 0 < price < 1 or count <= 0 or price in prices:
                raise ValueError("ORDERBOOK_LEVEL_CONFLICT")
            prices.add(price)
            if price * 100 != (price * 100).to_integral_value():
                blockers.append("SUBCENT_PRICE_UNSUPPORTED_BY_SIMULATOR")
            # Mantener el dato sin redondear, aunque el ledger aún use contratos enteros.
            parsed.append((price, count))
        levels[side] = sorted(parsed, reverse=True)
    best = {side: rows[0] if rows else None for side, rows in levels.items()}
    bid = best["yes"][0] if best["yes"] else None
    ask = 1 - best["no"][0] if best["no"] else None
    if bid is None or ask is None:
        blockers.append("ORDERBOOK_NOT_BILATERAL")
    elif bid >= ask:
        blockers.append("ORDERBOOK_LOCKED_OR_CROSSED")
    return {
        "received_at": received_at,
        "evaluated_at": evaluated_at,
        "receipt_age_seconds": age,
        "yes_bid_cents": str(bid * 100) if bid is not None else None,
        "yes_ask_cents": str(ask * 100) if ask is not None else None,
        "bid_depth_contracts": str(best["yes"][1]) if best["yes"] else None,
        "ask_depth_contracts": str(best["no"][1]) if best["no"] else None,
        "levels_captured": {side: len(rows) for side, rows in levels.items()},
        "blockers": sorted(set(blockers)),
        "quote_data_usable": not blockers,
        "exchange_sequence_verified": False,
        "fills_proven": 0,
        "execution_authorized": False,
    }


async def probe_m5_fee_policy(payload, series, *, demo_readonly_validation=False):
    """Ejecuta la política real de M5 contra dos respuestas locales, sin cliente REST.

    Una aceptación sólo cubre metadatos de fees: nunca autoriza fair, quotes o fills.
    """
    from src.strategies.motor_5_mm.fee_policy import (
        SeriesFeePolicy,
        UnsupportedSeriesFeeError,
    )

    event = payload["event"]["event_ticker"]
    calls = []

    class CapturedMetadata:
        async def get_series(self, ticker):
            if ticker != series["ticker"]:
                raise ValueError("CAPTURE_SERIES_MISMATCH")
            calls.append("series")
            return {"series": series}

        async def get_event(self, ticker):
            if ticker != event:
                raise ValueError("CAPTURE_EVENT_MISMATCH")
            calls.append("event")
            return payload

    try:
        result = await SeriesFeePolicy(
            CapturedMetadata(), demo_readonly_validation=demo_readonly_validation
        ).observe(payload["markets"][0]["ticker"], event_ticker=event)
        details = {
            "fee_type": result.fee_type,
            "multiplier": str(result.multiplier),
            "maker_multiplier": str(result.maker_multiplier),
        }
        status = "METADATA_ACCEPTED"
    except UnsupportedSeriesFeeError as error:
        details = {"reason": str(error)}
        status = "REJECTED"
    return {
        "status": status,
        "details": details,
        "cached_metadata_reads": calls,
        "network_calls": 0,
        "engine_started": False,
        "demo_readonly_validation": demo_readonly_validation,
        "execution_authorized": False,
    }
