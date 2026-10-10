"""Conciliación READ-ONLY: posiciones/fills del exchange ↔ ledger `trades` del bot.

Responde una sola pregunta: ¿lo que Kalshi dice que tenemos coincide con lo que el
bot cree que tiene? Es el gap que el README de este paquete marca como requisito
antes de cualquier discusión de ejecución ("Integrar un ledger conciliado …").

Entradas (ambas DATOS, ninguna llamada de red acá):
  - `account`: el snapshot saneado de `account_reader.collect_account_snapshot`
    (positions + fills), p. ej. el JSON de `scripts/read_research_account.py`.
  - `ledger_rows`: filas de la tabla `trades` como dicts (ver `load_ledger_rows`,
    que abre la SQLite en modo `ro`).

Convenciones (las mismas que el RiskManager para la exposición abierta):
  - Posición abierta del ledger = filas status='filled', action='buy',
    closed_by_clv=False; contratos = filled_count si existe, si no count.
  - Signo Kalshi: position > 0 = YES, < 0 = NO. buy yes → +n, buy no → −n. Un arb
    YES+NO en el mismo ticker netea igual que en el exchange.
  - Filas 'pending' = estado DESCONOCIDO: la conciliación no puede certificarse.

Clasificación por ticker:
  EXCHANGE_ONLY   el exchange tiene posición que el ledger no conoce — el caso
                  peligroso (huérfana: 2026-07-07, residual direccional ~$135).
  LEDGER_ONLY     el ledger cree tener algo que el exchange no muestra (settlement
                  aún no procesado por el SettlementPoller, o fill que nunca existió).
  QTY_MISMATCH    ambos tienen posición pero distinta.

Fees: por cada fill del exchange con count entero y precio en centavos enteros, se
compara `fee_cost` real contra los dos modelos de `paper_fees` (schedule centicent y
ceil-al-centavo de producción). El conteo dice cuál reproduce al exchange: esa es la
validación empírica de fees que ningún test unitario puede dar.

Nunca otorga autoridad: `execution_authorized=False` siempre.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

from paper_fees import (
    fee_centicents_exact,
    fee_cents_conservative,
    parse_usd_to_centicents,
    usd4,
)

from src.math.fees import maker_fee_multiplier_for_ticker

SCHEMA_VERSION = "botkalshi-reconciliation-v1"

_LEDGER_COLUMNS = (
    "client_order_id",
    "kalshi_order_id",
    "ticker",
    "side",
    "action",
    "count",
    "filled_count",
    "status",
    "strategy",
    "closed_by_clv",
    "fill_price_cents",
    "fees_cents",
    "filled_at",
)


class ReconciliationInputError(ValueError):
    """Entrada malformada: no se concilia sobre datos que no se entienden."""


def _signed_qty(side: str, count: int) -> int:
    if side == "yes":
        return count
    if side == "no":
        return -count
    raise ReconciliationInputError(f"side desconocido en ledger: {side!r}")


def _exchange_position(row: dict[str, Any]) -> Fraction:
    raw = row.get("position_fp")
    if raw is None:
        raw = row.get("position")
    if raw is None:
        raise ReconciliationInputError(f"posición sin cantidad para {row.get('ticker')!r}")
    try:
        return Fraction(str(raw))
    except (ValueError, ZeroDivisionError) as exc:
        raise ReconciliationInputError(f"posición inválida {raw!r}") from exc


def _fmt_qty(q: Fraction) -> str:
    if q.denominator == 1:
        return str(q.numerator)
    return f"{float(q):.2f}"


def ledger_open_positions(ledger_rows: list[dict[str, Any]]) -> tuple[dict[str, int], int]:
    """(posición neta abierta por ticker, cantidad de filas pending)."""
    net: dict[str, int] = defaultdict(int)
    pending = 0
    for r in ledger_rows:
        status = r.get("status")
        if status == "pending":
            pending += 1
            continue
        if status != "filled" or r.get("action") != "buy" or bool(r.get("closed_by_clv")):
            continue
        count = _ledger_count(r)
        net[r["ticker"]] += _signed_qty(r.get("side"), count)
    return {t: q for t, q in net.items() if q != 0}, pending


def _ledger_count(row: dict[str, Any]) -> int:
    """La columna fija manda; el dominio entero de este informe nunca trunca."""
    for field in ("filled_count_fp", "filled_count", "count_fp", "count"):
        raw = row.get(field)
        if raw is None:
            continue
        if isinstance(raw, bool):
            raise ReconciliationInputError("count booleano inválido")
        try:
            quantity = Fraction(str(raw))
        except (ValueError, ZeroDivisionError) as exc:
            raise ReconciliationInputError("count inválido") from exc
        if quantity < 0 or quantity.denominator != 1:
            raise ReconciliationInputError(
                "cantidad negativa o fraccionaria fuera del dominio del informe"
            )
        return int(quantity)
    raise ReconciliationInputError("ledger sin cantidad")


def _reconcile_positions(
    account: dict[str, Any], ledger_net: dict[str, int]
) -> list[dict[str, Any]]:
    exchange: dict[str, Fraction] = {}
    for row in account.get("positions") or []:
        q = _exchange_position(row)
        if q != 0:
            exchange[row["ticker"]] = q
    out = []
    for ticker in sorted(set(exchange) | set(ledger_net)):
        ex = exchange.get(ticker, Fraction(0))
        led = Fraction(ledger_net.get(ticker, 0))
        if ex == led:
            continue
        if led == 0:
            kind = "EXCHANGE_ONLY"
        elif ex == 0:
            kind = "LEDGER_ONLY"
        else:
            kind = "QTY_MISMATCH"
        out.append(
            {
                "ticker": ticker,
                "kind": kind,
                "exchange_position": _fmt_qty(ex),
                "ledger_position": _fmt_qty(led),
                "diff": _fmt_qty(ex - led),
            }
        )
    return out


def _fill_cents(price_dollars: object) -> int | None:
    cc = parse_usd_to_centicents(price_dollars)
    if cc is None or cc % 100 != 0:
        return None  # sub-centavo: fuera del dominio de las funciones de fee en centavos
    cents = cc // 100
    return cents if 0 <= cents <= 100 else None


def _fill_count(fill: dict[str, Any]) -> int | None:
    raw = fill.get("count_fp") if fill.get("count_fp") is not None else fill.get("count")
    if raw is None:
        return None
    try:
        q = Fraction(str(raw))
    except (ValueError, ZeroDivisionError):
        return None
    return int(q) if q.denominator == 1 and q >= 0 else None


def validate_fill_fee(fill: dict[str, Any]) -> dict[str, Any]:
    """Compara el fee_cost real de UN fill contra ambos modelos."""
    base = {"trade_id": fill.get("trade_id"), "ticker": fill.get("ticker")}
    count = _fill_count(fill)
    price = _fill_cents(fill.get("price_dollars"))
    actual_cc = parse_usd_to_centicents(fill.get("fee_cost_dollars"))
    is_taker = fill.get("is_taker")
    if count is None or price is None or actual_cc is None or type(is_taker) is not bool:
        return {**base, "verdict": "NOT_EVALUATED"}
    fee_type = "taker" if is_taker else "maker"
    ticker = fill.get("ticker") or ""
    when = _parse_time(fill.get("created_time"))
    multiplier = 1 if is_taker else maker_fee_multiplier_for_ticker(ticker, as_of=when)
    exact_cc = fee_centicents_exact(count, price, fee_type=fee_type, fee_multiplier=multiplier)
    cons_cc = 100 * fee_cents_conservative(
        count, price, fee_type=fee_type, fee_multiplier=multiplier
    )
    if actual_cc == exact_cc and actual_cc == cons_cc:
        verdict = "MATCH_BOTH"
    elif actual_cc == exact_cc:
        verdict = "MATCH_EXACT"
    elif actual_cc == cons_cc:
        verdict = "MATCH_CONSERVATIVE"
    else:
        verdict = "MATCH_NEITHER"
    return {
        **base,
        "verdict": verdict,
        "fee_type": fee_type,
        "fee_multiplier": str(multiplier),
        "count": count,
        "price_cents": price,
        "actual_fee_usd": usd4(actual_cc),
        "exact_fee_usd": usd4(exact_cc),
        "conservative_fee_usd": usd4(cons_cc),
    }


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _reconcile_fills(
    account: dict[str, Any], ledger_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Fills por order_id: exchange vs filas 'filled' con kalshi_order_id.

    Una fila del ledger solo se marca sin confirmar si su filled_at cae DENTRO de la
    ventana cubierta por el snapshot (desde el fill más viejo): el snapshot paginado
    no trae la historia entera y eso no es una discrepancia.
    """
    ex_by_order: dict[str, int] = defaultdict(int)
    oldest: datetime | None = None
    for f in account.get("fills") or []:
        oid = f.get("order_id")
        cnt = _fill_count(f)
        t = _parse_time(f.get("created_time"))
        if t is not None and (oldest is None or t < oldest):
            oldest = t
        if oid and cnt is not None:
            ex_by_order[oid] += cnt
    led_by_order: dict[str, tuple[int, str, Any]] = {}
    for r in ledger_rows:
        oid = r.get("kalshi_order_id")
        if not oid or r.get("status") not in ("filled", "settled"):
            continue
        cnt = _ledger_count(r)
        prev = led_by_order.get(oid, (0, r.get("ticker"), r.get("filled_at")))
        led_by_order[oid] = (prev[0] + int(cnt), prev[1], prev[2])
    out = []
    for oid, cnt in sorted(ex_by_order.items()):
        if oid not in led_by_order:
            out.append({"order_id": oid, "kind": "EXCHANGE_FILL_UNKNOWN", "exchange_count": cnt})
        elif led_by_order[oid][0] != cnt:
            out.append(
                {
                    "order_id": oid,
                    "kind": "FILL_COUNT_MISMATCH",
                    "exchange_count": cnt,
                    "ledger_count": led_by_order[oid][0],
                }
            )
    for oid, (cnt, ticker, filled_at) in sorted(led_by_order.items()):
        if oid in ex_by_order:
            continue
        t = _parse_time(filled_at) if isinstance(filled_at, str) else None
        if isinstance(filled_at, datetime):
            t = filled_at if filled_at.tzinfo else filled_at.replace(tzinfo=UTC)
        if oldest is not None and t is not None and t >= oldest:
            out.append(
                {
                    "order_id": oid,
                    "ticker": ticker,
                    "kind": "LEDGER_FILL_UNCONFIRMED",
                    "ledger_count": cnt,
                }
            )
    return out


def reconcile(
    account: dict[str, Any],
    ledger_rows: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(account, dict) or not isinstance(ledger_rows, list):
        raise ReconciliationInputError("account debe ser objeto y ledger_rows lista")
    if account.get("schema_version") != "botkalshi-account-snapshot-v1":
        raise ReconciliationInputError("account no es un snapshot botkalshi-account-snapshot-v1")
    for field, identity in (("positions", "ticker"), ("fills", "trade_id")):
        rows = account.get(field)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ReconciliationInputError(f"{field} debe ser una lista explícita")
        identifiers = [row.get(identity) for row in rows]
        if any(not isinstance(key, str) or not key for key in identifiers):
            raise ReconciliationInputError(f"{field} sin identificador válido")
        if len(set(identifiers)) != len(identifiers):
            raise ReconciliationInputError(f"{field} contiene identificadores duplicados")
        if f"{field}_count" in account and account[f"{field}_count"] != len(rows):
            raise ReconciliationInputError(f"{field}_count contradice las filas")
    ledger_net, pending = ledger_open_positions(ledger_rows)
    positions = _reconcile_positions(account, ledger_net)
    fills = _reconcile_fills(account, ledger_rows)
    fee_rows = [validate_fill_fee(f) for f in account.get("fills") or []]
    tally: dict[str, int] = defaultdict(int)
    for row in fee_rows:
        tally[row["verdict"]] += 1
    blockers = []
    if positions:
        blockers.append("POSITION_DISCREPANCY")
    if fills:
        blockers.append("FILL_DISCREPANCY")
    if pending:
        blockers.append("PENDING_ORDERS_UNKNOWN_STATE")
    if tally.get("MATCH_NEITHER"):
        blockers.append("FEE_MODEL_UNEXPLAINED")
    if tally.get("NOT_EVALUATED"):
        blockers.append("FILL_NOT_EVALUATED")
    observed_order_ids = {fill.get("order_id") for fill in account["fills"] if fill.get("order_id")}
    unconfirmed = sum(
        row.get("status") in ("filled", "settled")
        and _ledger_count(row) > 0
        and row.get("kalshi_order_id") not in observed_order_ids
        for row in ledger_rows
    )
    if unconfirmed:
        blockers.append("FILL_COVERAGE_UNVERIFIED")
    if any(not fill.get("order_id") for fill in account["fills"]):
        blockers.append("FILL_WITHOUT_ORDER_ID")
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
        "account_observed_at": account.get("observed_at"),
        "status": "RECONCILED" if not blockers else "NOT_RECONCILED",
        "scope": "PROVIDED_SNAPSHOTS_ONLY_NOT_LIVE_ACCOUNT_CERTIFICATION",
        "ledger_fills_without_coverage": unconfirmed,
        "blockers": blockers,
        "exchange_balance_usd": account.get("balance_usd"),
        "ledger_open_tickers": len(ledger_net),
        "ledger_pending_rows": pending,
        "position_discrepancies": positions,
        "fill_discrepancies": fills,
        "fee_validation": {"tally": dict(sorted(tally.items())), "rows": fee_rows},
        "execution_authorized": False,
        "order_capability_present": False,
        "real_entry_eligible": False,
    }


def load_ledger_rows(db_path: str | Path) -> list[dict[str, Any]]:
    """Lee `trades` en modo SOLO LECTURA (uri mode=ro). Nunca escribe.

    La conexión conserva el manejo normal de WAL de SQLite; no usa immutable=1
    ni solicita checkpoint o modificación de la DB histórica.
    """
    path = Path(db_path)
    if not path.is_file():
        raise ReconciliationInputError(f"no existe la DB {path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
    try:
        con.row_factory = sqlite3.Row
        cols = {r[1] for r in con.execute("PRAGMA table_info(trades)").fetchall()}
        missing = [c for c in _LEDGER_COLUMNS if c not in cols]
        if missing:
            raise ReconciliationInputError(f"tabla trades sin columnas {missing}")
        con.execute("PRAGMA query_only=ON")
        selected = [*_LEDGER_COLUMNS, *(c for c in ("filled_count_fp", "count_fp") if c in cols)]
        rows = con.execute(f"SELECT {', '.join(selected)} FROM trades").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()
