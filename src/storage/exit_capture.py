"""Captura contable de fills SELL por GET; jamás crea ni cancela órdenes."""

import asyncio
import re
from fractions import Fraction

from src.math.quantities import fixed_count
from src.storage.cost_allocation import usd_text
from src.storage.exit_fills import ExitFillSummary, summarize_exit_fills
from src.storage.order_costs import COST_QUARANTINE_NOTE, record_order_fill
from src.storage.quantities import confirmed_fill


async def collect_exit_execution(
    client, *, order_id: str, ticker: str, side: str, quantity: Fraction, subaccount: int = 0
) -> ExitFillSummary:
    """Máximo diez páginas de 100 filas/20 segundos; ausencia nunca significa cero.

    El caller acredita el estado terminal y la identidad de la orden. El histórico
    carece de filtro order_id: se valida su ámbito antes de seleccionar las filas.
    Ambos listados deben terminar explícitamente; no se acepta un cursor faltante.
    """
    if not order_id or not ticker or side not in {"yes", "no"} or subaccount != 0:
        raise ValueError("EXIT_CAPTURE_IDENTITY_INVALID")
    rows = []
    async with asyncio.timeout(20):
        for name in ("get_fills", "get_historical_fills"):
            cursor, seen = None, set()
            for _ in range(5):
                params = {"ticker": ticker, "subaccount": subaccount, "limit": 100}
                if cursor:
                    params["cursor"] = cursor
                if name == "get_fills":
                    params["order_id"] = order_id
                page = await getattr(client, name)(**params)
                if not isinstance(page, dict) or page.get("error") is not None:
                    raise ValueError("EXIT_CAPTURE_PAGE_INVALID")
                data = page.get("fills")
                if not isinstance(data, list) or len(data) > 100:
                    raise ValueError("EXIT_CAPTURE_PAGE_INVALID")
                for row in data:
                    if (
                        not isinstance(row, dict)
                        or not isinstance(row.get("order_id"), str)
                        or not row["order_id"]
                        or row.get("ticker", row.get("market_ticker")) != ticker
                        or row.get("market_ticker", ticker) != ticker
                        or type(row.get("subaccount_number")) is not int
                        or row["subaccount_number"] != subaccount
                    ):
                        raise ValueError("EXIT_CAPTURE_SCOPE_INVALID")
                    if row["order_id"] == order_id:
                        rows.append(row)
                cursor = page.get("cursor")
                if cursor == "":
                    break
                if (
                    not isinstance(cursor, str)
                    or not cursor
                    or len(cursor) > 2048
                    or cursor in seen
                ):
                    raise ValueError("EXIT_CAPTURE_CURSOR_INVALID")
                seen.add(cursor)
            else:
                raise ValueError("EXIT_CAPTURE_PAGE_LIMIT")
    return summarize_exit_fills(
        rows,
        order_id=order_id,
        ticker=ticker,
        side=side,
        quantity=quantity,
        subaccount=subaccount,
        complete=True,
    )


def apply_exit_execution(row, execution: ExitFillSummary, sold, order_id: str | None) -> None:
    """Valida contra el ledger releído bajo lock; el caller confirma o revierte todo."""
    if (
        not isinstance(execution, ExitFillSummary)
        or row.action != "sell"
        or row.ticker != execution.ticker
        or row.side != execution.side
        or execution.order_id != order_id
        or row.kalshi_order_id not in (None, order_id)
        or type(execution.subaccount) is not int
        or execution.subaccount != 0
        or execution.count != sold
        or sold <= 0
        or type(execution.gross_usd) is not Fraction
        or type(execution.fee_usd) is not Fraction
        or not 0 <= execution.gross_usd <= sold
        or not 0 <= execution.fee_usd <= sold
        or not isinstance(execution.evidence_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", execution.evidence_sha256)
        or not isinstance(execution.fill_ids, tuple)
        or not execution.fill_ids
        or any(not isinstance(item, str) or not item for item in execution.fill_ids)
        or len(set(execution.fill_ids)) != len(execution.fill_ids)
    ):
        raise ValueError("EXIT_EXECUTION_IDENTITY_INVALID")
    gross, fee = usd_text(execution.gross_usd), usd_text(execution.fee_usd)
    if row.status == "settled":
        if (
            row.sale_proceeds_usd_fp != gross
            or row.fees_usd_fp != fee
            or confirmed_fill(row) != sold
            or row.exit_fill_evidence_sha256 != execution.evidence_sha256
        ):
            raise ValueError("EXIT_EXECUTION_REPLAY_CONFLICT")
        return
    if row.status != "pending":
        raise ValueError("EXIT_EXECUTION_NOT_PENDING")
    record_order_fill(
        row,
        sold,
        {
            "fill_count_fp": fixed_count(sold),
            "maker_fees_dollars": fee,
            "taker_fees_dollars": "0.000000",
        },
    )
    if COST_QUARANTINE_NOTE in (row.notes or ""):
        raise ValueError("EXIT_EXECUTION_MONEY_CONFLICT")
    row.sale_proceeds_usd_fp = gross
    row.exit_fill_evidence_sha256 = execution.evidence_sha256
    row.kalshi_order_id = order_id


def exit_execution_note(notes: str | None, suffix: str, *, entry_observed: bool) -> str:
    basis = "observed" if entry_observed else "estimated_entry"
    return (
        f"exit_execution_observed exit_price=fills exit_fee=fills pnl_basis={basis} | "
        f"{notes or ''} {suffix}"
    ).strip()[:500]
