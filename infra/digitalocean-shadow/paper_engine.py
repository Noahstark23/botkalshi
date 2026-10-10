"""Paper trading: señales de motores → fills SIMULADOS contra el book → ledger ficticio.

Completa el simulador de P3: `simulation_bank` ya reservaba capital ficticio, pero nada
convertía una señal en un fill ni un fill en PnL. Este módulo cierra el ciclo
señal → fill → reserva → settlement → PnL realizado, sin emitir una sola orden.

Garantías estructurales (con test-guard, mismo patrón que los F1 shadow):
  - NO importa `src.clients`, executors, RiskManager ni nada de red. No hay forma de
    que llegue a Kalshi: el único efecto es escribir en la SQLite del simulador.
  - Toda fila lleva mode=SIMULATION_ONLY y execution_authorized=False.

Modelo de fill (deliberadamente pesimista — un sim optimista es peor que ninguno):
  - Solo órdenes de COMPRA inmediatas (taker), que es lo que emiten M1/M2/REST. Comprar
    YES cruza contra los bids de NO (ask_yes = 100 − bid_no) y viceversa.
  - Se recorre la profundidad nivel a nivel hasta el límite de precio de la señal;
    lo que no alcanza queda SIN llenar (fill parcial explícito, nunca inventado).
  - Book más viejo que `max_book_age_sec` respecto de la señal → REJECTED_STALE_BOOK.
  - El fee se cobra con el modelo CONSERVADOR de producción (ceil al centavo, el que
    usan los motores para decidir) y se registra también el exacto del schedule
    (centicent), para medir cuánto sesga.
  - Queue position, latencia y slippage por movimiento del book NO se modelan: un
    fill simulado es una cota superior de lo que la estrategia podría capturar.

Persistencia: tablas `paper_fills` y `paper_settlements` en el MISMO archivo que
`simulation_bank` (una transacción por operación: la reserva del costo máximo y el
fill se escriben juntos o no se escribe nada).
"""

from __future__ import annotations

import contextlib
import json
import math
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import simulation_bank as bank
from paper_fees import (
    FEE_MODEL_CONSERVATIVE,
    FEE_MODEL_EXACT,
    fee_centicents_exact,
    fee_cents_conservative,
    usd4,
)

SCHEMA_VERSION_SIGNAL = "botkalshi-paper-signal-v1"
SCHEMA_VERSION_OUTPUT = "botkalshi-paper-fill-v1"
DEFAULT_MAX_BOOK_AGE_SEC = 10

_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.-]{2,127}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
# M5 resting fills require strict crossing and queue/depth evidence; an immediate
# taker purchase cannot stand in for that model. Use m5_bank_bridge for its review.
_ORIGINS = {"M1", "M2", "M3", "M6", "M8", "M9", "REST"}
_SIGNAL_FIELDS = {
    "schema_version",
    "signal_id",
    "origin",
    "cycle_id",
    "ticker",
    "side",
    "limit_price_cents",
    "count",
    "signal_at",
}


class PaperEngineError(Exception):
    """Error del simulador. Nada parcial queda escrito cuando se levanta."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _usd(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}{whole}.{frac:02d}"


def _parse_time(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise PaperEngineError(f"{field} debe ser ISO-8601 string")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PaperEngineError(f"{field} no es ISO-8601") from exc
    if dt.tzinfo is None:
        raise PaperEngineError(f"{field} debe tener zona horaria (aware UTC)")
    return dt.astimezone(UTC)


def validate_signal(raw: object) -> dict[str, Any]:
    """Valida el contrato de señal. Campos de más o de menos → error (fail-closed)."""
    if not isinstance(raw, dict):
        raise PaperEngineError("la señal debe ser un objeto")
    if set(raw) != _SIGNAL_FIELDS:
        extra = sorted(set(raw) - _SIGNAL_FIELDS)
        missing = sorted(_SIGNAL_FIELDS - set(raw))
        raise PaperEngineError(f"campos inválidos: extra={extra} faltan={missing}")
    if raw["schema_version"] != SCHEMA_VERSION_SIGNAL:
        raise PaperEngineError("schema_version de señal desconocido")
    if raw.get("origin") == "M5":
        raise PaperEngineError("M5 requiere evidencia de quote resting; no admite fill taker")
    for f in ("signal_id", "cycle_id"):
        if not isinstance(raw[f], str) or not _ID_RE.fullmatch(raw[f]):
            raise PaperEngineError(f"{f} inválido")
    if raw["origin"] not in _ORIGINS:
        raise PaperEngineError(f"origin desconocido: {raw['origin']!r}")
    if not isinstance(raw["ticker"], str) or not _TICKER_RE.fullmatch(raw["ticker"]):
        raise PaperEngineError("ticker inválido")
    if raw["side"] not in ("yes", "no"):
        raise PaperEngineError("side debe ser yes/no")
    lp, cnt = raw["limit_price_cents"], raw["count"]
    if type(lp) is not int or not 1 <= lp <= 99:
        raise PaperEngineError("limit_price_cents debe ser int 1..99")
    if type(cnt) is not int or not 1 <= cnt <= 10_000:
        raise PaperEngineError("count debe ser int 1..10000")
    return {**raw, "_signal_at": _parse_time(raw["signal_at"], "signal_at")}


def _levels(raw: object, side: str) -> list[tuple[int, int]]:
    """[[price_cents, qty], ...] → lista ordenada de bids (mejor primero)."""
    if not isinstance(raw, list):
        raise PaperEngineError(f"{side}_bids debe ser lista")
    out = []
    seen_prices: set[int] = set()
    for lvl in raw:
        if (
            not isinstance(lvl, (list, tuple))
            or len(lvl) != 2
            or type(lvl[0]) is not int
            or type(lvl[1]) is not int
            or not 1 <= lvl[0] <= 99
            or lvl[1] < 0
        ):
            raise PaperEngineError(f"nivel inválido en {side}_bids: {lvl!r}")
        if lvl[0] in seen_prices:
            raise PaperEngineError(f"precio duplicado en {side}_bids: {lvl[0]}")
        seen_prices.add(lvl[0])
        if lvl[1] > 0:
            out.append((lvl[0], lvl[1]))
    out.sort(key=lambda x: -x[0])
    return out


def simulate_fill(signal: dict[str, Any], book: dict[str, Any]) -> dict[str, Any]:
    """Puro: decide el fill simulado de una señal YA validada contra un book."""
    observed = _parse_time(book.get("observed_at"), "book.observed_at")
    max_age = book.get("max_book_age_sec", DEFAULT_MAX_BOOK_AGE_SEC)
    if (
        type(max_age) not in (int, float)
        or not math.isfinite(max_age)
        or not 0 <= max_age <= DEFAULT_MAX_BOOK_AGE_SEC
    ):
        raise PaperEngineError("max_book_age_sec debe ser finito entre 0 y 10")
    if book.get("ticker") != signal["ticker"]:
        raise PaperEngineError("el book no es del ticker de la señal")
    age = (signal["_signal_at"] - observed).total_seconds()
    base = {"book_observed_at": observed.isoformat(), "book_age_sec": round(age, 3)}
    if age < 0 or age > max_age:
        return {**base, "status": "REJECTED_STALE_BOOK", "filled_count": 0, "legs": []}
    # Comprar YES consume bids de NO (y viceversa): ask = 100 − bid contrario.
    opposite = "no" if signal["side"] == "yes" else "yes"
    bids = _levels(book.get(f"{opposite}_bids"), opposite)
    remaining = signal["count"]
    legs: list[dict[str, int]] = []
    for bid_price, qty in bids:
        ask = 100 - bid_price
        if ask > signal["limit_price_cents"] or remaining == 0:
            break
        take = min(qty, remaining)
        legs.append({"price_cents": ask, "count": take})
        remaining -= take
    filled = signal["count"] - remaining
    if filled == 0:
        status = "NO_FILL"
    elif remaining:
        status = "PARTIAL_FILL"
    else:
        status = "FILLED"
    return {**base, "status": status, "filled_count": filled, "legs": legs}


def _fees_for(legs: list[dict[str, int]]) -> tuple[int, int]:
    """(cents conservadores, centicents calculados), agrupados por nivel del book.

    Es una convención del simulador: el book no identifica los fills individuales
    ni el agrupamiento real de fees de una orden. No demuestra el cobro del exchange.
    """
    cons = sum(fee_cents_conservative(lg["count"], lg["price_cents"]) for lg in legs)
    exact = sum(fee_centicents_exact(lg["count"], lg["price_cents"]) for lg in legs)
    return cons, exact


def _ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS paper_fills (
          signal_id TEXT PRIMARY KEY,
          origin TEXT NOT NULL,
          cycle_id TEXT NOT NULL,
          ticker TEXT NOT NULL,
          side TEXT NOT NULL CHECK (side IN ('yes','no')),
          requested_count INTEGER NOT NULL,
          filled_count INTEGER NOT NULL,
          cost_cents INTEGER NOT NULL,
          fee_cents INTEGER NOT NULL,
          fee_exact_centicents INTEGER NOT NULL,
          status TEXT NOT NULL,
          result_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          signal_json TEXT,
          settled INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS paper_settlements (
          signal_id TEXT PRIMARY KEY REFERENCES paper_fills(signal_id),
          market_result TEXT NOT NULL CHECK (market_result IN ('yes','no')),
          payout_cents INTEGER NOT NULL,
          pnl_cents INTEGER NOT NULL,
          settled_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS paper_market_results (
          ticker TEXT PRIMARY KEY,
          market_result TEXT NOT NULL CHECK (market_result IN ('yes','no'))
        );
        CREATE TABLE IF NOT EXISTS paper_partial_settlements (
          signal_id TEXT PRIMARY KEY REFERENCES paper_fills(signal_id),
          yes_payout_cents INTEGER NOT NULL CHECK (yes_payout_cents BETWEEN 1 AND 99),
          payout_cents INTEGER NOT NULL,
          pnl_cents INTEGER NOT NULL,
          settled_at TEXT NOT NULL
        );
        """
    )
    if "signal_json" not in {r[1] for r in con.execute("PRAGMA table_info(paper_fills)")}:
        try:
            con.execute("ALTER TABLE paper_fills ADD COLUMN signal_json TEXT")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc):
                raise


@contextlib.contextmanager
def _connect(db_path: Path):
    if db_path.is_symlink() or db_path.parent.is_symlink():
        raise PaperEngineError("la ruta de la DB no puede ser symlink")
    with bank._connect(db_path) as con:
        _ensure_schema(con)
        if con.execute(
            "SELECT 1 FROM (SELECT signal_id FROM paper_settlements UNION ALL "
            "SELECT signal_id FROM paper_partial_settlements) s LEFT JOIN simulation_realizations r "
            "ON r.idempotency_key = 'paper:' || s.signal_id WHERE r.idempotency_key IS NULL LIMIT 1"
        ).fetchone():
            raise PaperEngineError(
                "liquidaciones legacy sin capital conciliado; requieren revisión"
            )
        if con.execute(
            "SELECT 1 FROM paper_partial_settlements s "
            "LEFT JOIN paper_fills f USING(signal_id) "
            "LEFT JOIN simulation_realizations r ON r.idempotency_key='paper:' || s.signal_id "
            "LEFT JOIN simulation_reservations v ON v.idempotency_key=r.idempotency_key "
            "WHERE f.signal_id IS NULL OR f.settled != 1 OR v.status != 'RELEASED' "
            "OR v.status IS NULL OR s.pnl_cents != r.pnl_cents "
            "OR s.pnl_cents != s.payout_cents-f.cost_cents-f.fee_cents "
            "OR s.payout_cents != f.filled_count * "
            "(CASE f.side WHEN 'yes' THEN s.yes_payout_cents ELSE 100-s.yes_payout_cents END) "
            "OR EXISTS (SELECT 1 FROM paper_settlements old WHERE old.signal_id=s.signal_id) LIMIT 1"
        ).fetchone():
            raise PaperEngineError("liquidación parcial sin contabilidad consistente")
        yield con


def _base_result(signal: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION_OUTPUT,
        "signal_id": signal["signal_id"],
        "origin": signal["origin"],
        "cycle_id": signal["cycle_id"],
        "ticker": signal["ticker"],
        "side": signal["side"],
        "requested_count": signal["count"],
        "limit_price_cents": signal["limit_price_cents"],
        "mode": "SIMULATION_ONLY",
        "capital_source": "FICTIONAL_TEST_CAPITAL",
        "execution_authorized": False,
        "order_capability_present": False,
        "real_entry_eligible": False,
    }


def process_signal(db_path: str | Path, raw_signal: object, book: dict[str, Any]) -> dict[str, Any]:
    """Valida, simula, reserva el costo+fee en el banco ficticio y persiste el fill.

    Idempotente por signal_id: reprocesar la misma señal devuelve el resultado
    guardado sin cobrar dos veces. Sin fill (stale/no_fill) se persiste igual —
    el "no hubiera llenado" es dato del experimento, no ruido a descartar.
    Fondos ficticios insuficientes → status REJECTED_INSUFFICIENT_FUNDS, sin fill.
    """
    path = Path(db_path)
    signal = validate_signal(raw_signal)
    signal_json = json.dumps(raw_signal, sort_keys=True, separators=(",", ":"), allow_nan=False)
    key = bank._parse_id(f"paper:{signal['signal_id']}", field="signal_id")
    # Una sola transacción: también serializa reintentos simultáneos. Nunca queda
    # una reserva huérfana por fallar el INSERT posterior del fill ficticio.
    with _connect(path) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            prior = con.execute(
                "SELECT result_json, signal_json FROM paper_fills WHERE signal_id = ?",
                (signal["signal_id"],),
            ).fetchone()
            if prior is not None:
                if prior[1] != signal_json:
                    raise PaperEngineError(
                        "signal_id reutilizado con otro payload o payload legacy no verificable"
                    )
                con.execute("COMMIT")
                return {**json.loads(prior[0]), "replayed": True}
            if (
                bank._market_payout(con, signal["ticker"]) is not None
                or con.execute(
                    "SELECT 1 FROM paper_market_results WHERE ticker = ? UNION ALL "
                    "SELECT 1 FROM paper_settlements s JOIN paper_fills f USING(signal_id) WHERE f.ticker = ?",
                    (signal["ticker"], signal["ticker"]),
                ).fetchone()
            ):
                raise PaperEngineError("mercado ya liquidado en el simulador")
            sim = simulate_fill(signal, book)
            cost = sum(lg["count"] * lg["price_cents"] for lg in sim["legs"])
            fee_c, fee_cc = _fees_for(sim["legs"])
            result = {
                **_base_result(signal),
                **sim,
                "cost_usd": _usd(cost),
                "fee_model": FEE_MODEL_CONSERVATIVE,
                "fee_usd": _usd(fee_c),
                "fee_exact_model": FEE_MODEL_EXACT,
                "fee_exact_usd": usd4(fee_cc),
                "max_loss_usd": _usd(cost + fee_c),
                "reservation_error": None,
            }
            if cost + fee_c > 0:
                try:
                    bank._reserve_locked(
                        con,
                        key=key,
                        origin_value=f"PAPER_{signal['origin']}",
                        cycle_value=signal["cycle_id"],
                        amount_cents=cost + fee_c,
                        evidence_json=bank._canonical_evidence_json(
                            {
                                "ticker": signal["ticker"],
                                "side": signal["side"],
                                "legs": sim["legs"],
                                "fee_model": FEE_MODEL_CONSERVATIVE,
                            }
                        ),
                    )
                    status = con.execute(
                        "SELECT status FROM simulation_reservations WHERE idempotency_key = ?",
                        (key,),
                    ).fetchone()[0]
                    if status != "ACTIVE":
                        raise PaperEngineError("reserva previa liberada sin fill verificable")
                except bank.SimulationBankInsufficientFundsError as exc:
                    result.update(
                        status="REJECTED_INSUFFICIENT_FUNDS",
                        filled_count=0,
                        legs=[],
                        cost_usd="0.00",
                        fee_usd="0.00",
                        fee_exact_usd="0.0000",
                        max_loss_usd="0.00",
                        reservation_error=f"{type(exc).__name__}: {exc}",
                    )
                    cost, fee_c, fee_cc = 0, 0, 0
            con.execute(
                "INSERT INTO paper_fills (signal_id, origin, cycle_id, ticker, side, "
                "requested_count, filled_count, cost_cents, fee_cents, fee_exact_centicents, "
                "status, result_json, created_at, signal_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    signal["signal_id"],
                    signal["origin"],
                    signal["cycle_id"],
                    signal["ticker"],
                    signal["side"],
                    signal["count"],
                    result["filled_count"],
                    cost,
                    fee_c,
                    fee_cc,
                    result["status"],
                    json.dumps(result, sort_keys=True),
                    _now(),
                    signal_json,
                ),
            )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    return result


def settle_market(
    db_path: str | Path,
    *,
    ticker: str,
    market_result: str | None = None,
    yes_payout_cents: int | None = None,
) -> list[dict[str, Any]]:
    """Liquida todos los fills simulados abiertos de un ticker y libera sus reservas.

    Exactamente un resultado: yes/no legacy O valor YES entero de 0..100¢.
    NO recibe el complemento. No infiere precio justo ni devuelve el coste de
    entrada en un empate. pnl = pago − coste − comisión ya cobrada.
    La tabla parcial es aditiva: no reescribe liquidaciones binarias históricas.
    """
    if (market_result is None) == (yes_payout_cents is None):
        raise PaperEngineError("indicar exactamente un resultado de liquidación")
    if market_result is not None:
        if market_result not in ("yes", "no"):
            raise PaperEngineError("market_result debe ser yes/no")
        yes_payout_cents = 100 if market_result == "yes" else 0
    if type(yes_payout_cents) is not int or not 0 <= yes_payout_cents <= 100:
        raise PaperEngineError("yes_payout_cents debe ser entero entre 0 y 100")
    binary = {0: "no", 100: "yes"}.get(yes_payout_cents)
    if not isinstance(ticker, str) or not _TICKER_RE.fullmatch(ticker):
        raise PaperEngineError("ticker inválido")
    path = Path(db_path)
    settled: list[dict[str, Any]] = []
    with _connect(path) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            previous = con.execute(
                "SELECT market_result FROM paper_market_results WHERE ticker = ? UNION ALL "
                "SELECT s.market_result FROM paper_settlements s JOIN paper_fills f USING(signal_id) WHERE f.ticker = ?",
                (ticker, ticker),
            ).fetchall()
            if any(row[0] != binary for row in previous):
                raise PaperEngineError("resultado de liquidación contradictorio")
            partial = con.execute(
                "SELECT s.yes_payout_cents FROM paper_partial_settlements s "
                "JOIN paper_fills f USING(signal_id) WHERE f.ticker = ?",
                (ticker,),
            ).fetchall()
            if any(row[0] != yes_payout_cents for row in partial):
                raise PaperEngineError("resultado de liquidación contradictorio")
            bank._record_market_payout_locked(con, ticker, yes_payout_cents)
            if binary is not None:
                con.execute(
                    "INSERT OR IGNORE INTO paper_market_results VALUES (?, ?)", (ticker, binary)
                )
            rows = con.execute(
                "SELECT signal_id, side, filled_count, cost_cents, fee_cents FROM paper_fills "
                "WHERE ticker = ? AND settled = 0 AND filled_count > 0",
                (ticker,),
            ).fetchall()
            now = _now()
            for signal_id, side, filled, cost, fee in rows:
                payout = filled * (yes_payout_cents if side == "yes" else 100 - yes_payout_cents)
                pnl = payout - cost - fee
                bank._realize_locked(con, key=f"paper:{signal_id}", payout_cents=payout)
                if binary is not None:
                    con.execute(
                        "INSERT INTO paper_settlements VALUES (?,?,?,?,?)",
                        (signal_id, binary, payout, pnl, now),
                    )
                else:
                    con.execute(
                        "INSERT INTO paper_partial_settlements VALUES (?,?,?,?,?)",
                        (signal_id, yes_payout_cents, payout, pnl, now),
                    )
                con.execute("UPDATE paper_fills SET settled = 1 WHERE signal_id = ?", (signal_id,))
                settled.append(
                    {"signal_id": signal_id, "payout_usd": _usd(payout), "pnl_usd": _usd(pnl)}
                )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    return settled


def summary(db_path: str | Path) -> dict[str, Any]:
    """PnL realizado por motor + exposición abierta + sesgo del fee conservador."""
    path = Path(db_path)
    with _connect(path) as con:
        by_origin = {}
        for origin, n, filled, n_settled, pnl, fee_c, fee_cc in con.execute(
            "SELECT f.origin, COUNT(*), SUM(f.filled_count > 0), COUNT(s.signal_id), "
            "COALESCE(SUM(s.pnl_cents),0), COALESCE(SUM(f.fee_cents),0), "
            "COALESCE(SUM(f.fee_exact_centicents),0) "
            "FROM paper_fills f LEFT JOIN (SELECT signal_id, pnl_cents FROM paper_settlements "
            "UNION ALL SELECT signal_id, pnl_cents FROM paper_partial_settlements) s USING(signal_id) "
            "GROUP BY f.origin ORDER BY f.origin"
        ):
            by_origin[origin] = {
                "signals": n,
                "filled": filled or 0,
                "settled": n_settled,
                "realized_pnl_usd": _usd(pnl),
                "fees_conservative_usd": _usd(fee_c),
                "fees_exact_usd": usd4(fee_cc),
                "fee_overestimate_usd": usd4(fee_c * 100 - fee_cc),
            }
        open_cost = con.execute(
            "SELECT COALESCE(SUM(cost_cents + fee_cents),0) FROM paper_fills "
            "WHERE settled = 0 AND filled_count > 0"
        ).fetchone()[0]
    return {
        "schema": "botkalshi-paper-summary-v1",
        "mode": "SIMULATION_ONLY",
        "by_origin": by_origin,
        "open_exposure_usd": _usd(open_cost),
        "execution_authorized": False,
        "order_capability_present": False,
        "real_entry_eligible": False,
    }
