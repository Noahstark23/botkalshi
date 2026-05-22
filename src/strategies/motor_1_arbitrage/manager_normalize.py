"""
Normaliza payloads crudos del WS de Kalshi al formato que consume Día 1
(OrderbookState.apply_snapshot / apply_delta).

Payloads reales verificados contra producción 2026-05-22:
  orderbook_snapshot: yes_dollars_fp / no_dollars_fp con listas de [price_str, size_str]
  orderbook_delta:    price_dollars (str), delta_fp (str), side (str), seq (int global)

Reglas de conversión:
  - Precios: Decimal exacto, no float. float("0.97") * 100 = 96.999... → centavo perdido.
  - Sizes/deltas: round(float(s)) — Kalshi raramente manda fracciones; para arbitraje
    binario 3.16 contratos no es operable. Se redondea al entero más cercano.
  - previous_seq NO viene de Kalshi. Esta capa solo extrae lo que sí existe.
    El manager pasa state.local_seq como synthetic_previous_seq para trazabilidad.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation


def _price_to_cents(price_str: str) -> int:
    """
    '0.9700' → 97. Usa Decimal para evitar float drift.

    float('0.97') * 100 = 96.999..., int(...) = 96. Decimal lo hace exacto.
    """
    try:
        cents = Decimal(str(price_str)) * 100
        return int(cents)
    except (InvalidOperation, TypeError) as e:
        raise ValueError(f"cannot convert price {price_str!r} to cents: {e}") from e


def _size_to_int(size_str: str | int | float) -> int:
    """
    '502000.00' → 502000. '3.16' → 3. '-62.00' → -62. Tolerante a int/float/str.

    round() en lugar de int() para tamaños fraccionarios: 3.5 → 4, 3.16 → 3.
    """
    try:
        return round(float(size_str))
    except (ValueError, TypeError) as e:
        raise ValueError(f"cannot convert size {size_str!r} to int: {e}") from e


def normalize_snapshot_payload(raw_msg: dict) -> dict:
    """
    Extrae el shape que apply_snapshot espera:
        {"seq": int, "yes": [[price_cents, size], ...], "no": [...]}

    seq viene del stream global (top-level del WS msg).

    Raises:
        ValueError: si faltan campos requeridos o tipos no convertibles.
    """
    try:
        seq = int(raw_msg["seq"])
        body = raw_msg["msg"]
        yes_raw = body.get("yes_dollars_fp") or []
        no_raw = body.get("no_dollars_fp") or []

        yes = [[_price_to_cents(p), _size_to_int(s)] for p, s in yes_raw]
        no = [[_price_to_cents(p), _size_to_int(s)] for p, s in no_raw]

        return {"seq": seq, "yes": yes, "no": no}
    except KeyError as e:
        raise ValueError(f"snapshot normalize failed: missing key {e}") from e
    except (TypeError, ArithmeticError) as e:
        raise ValueError(f"snapshot normalize failed: {type(e).__name__}: {e}") from e


def normalize_delta_payload(raw_msg: dict, synthetic_previous_seq: int) -> dict:
    """
    Extrae el shape que apply_delta espera:
        {"seq": int, "previous_seq": int, "side": str, "price": int, "delta": int}

    previous_seq es ARGUMENTO porque Kalshi no lo provee. El manager pasa su
    contador local (local_seq) como synthetic_previous_seq para trazabilidad.
    Día 1's apply_delta valida new_seq > state.sequence (no previous_seq directamente),
    pero el manager pasa el local counter como seq para mantener la invariante.

    Raises:
        ValueError: si faltan campos requeridos o tipos no convertibles.
    """
    try:
        seq = int(raw_msg["seq"])
        body = raw_msg["msg"]
        side = str(body["side"]).lower()
        if side not in ("yes", "no"):
            raise ValueError(f"invalid side: {side!r}")

        price = _price_to_cents(body["price_dollars"])
        delta = _size_to_int(body["delta_fp"])

        return {
            "seq": seq,
            "previous_seq": synthetic_previous_seq,
            "side": side,
            "price": price,
            "delta": delta,
        }
    except KeyError as e:
        raise ValueError(f"delta normalize failed: missing key {e}") from e
    except (TypeError, ArithmeticError) as e:
        raise ValueError(f"delta normalize failed: {type(e).__name__}: {e}") from e


def extract_ticker(raw_msg: dict) -> str | None:
    """Extrae market_ticker del msg body. None si falta o msg malformado."""
    try:
        return raw_msg["msg"]["market_ticker"]
    except (KeyError, TypeError):
        return None


def extract_sid(raw_msg: dict) -> int | None:
    """Extrae sid del top-level. None si falta o no es int."""
    sid = raw_msg.get("sid")
    return int(sid) if isinstance(sid, int) else None
