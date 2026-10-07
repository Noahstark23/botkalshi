"""Rollback atribuible con intent durable y cierre/split atómico de cantidades exactas."""

import uuid
from math import floor

from sqlalchemy import text
from sqlmodel import select

from src.math.fees import kalshi_fee_cents
from src.storage.cost_allocation import allocate_entry, usd_text
from src.storage.models import Trade, _naive_utc_now, get_session
from src.storage.quantities import (
    confirmed_fill,
    filled_fields,
    open_quantity,
    quantity_fields,
    requested_quantity,
    resize_filled,
)

REDUCTION_PREFIX = "reduction_for="


def persist_reduction(original_coid: str, coid: str, count, price: int) -> None:
    with get_session() as s:
        s.exec(text("BEGIN IMMEDIATE"))
        original = s.exec(select(Trade).where(Trade.client_order_id == original_coid)).one()
        if (
            original.status != "filled"
            or original.closed_by_clv
            or not 0 < count <= open_quantity(original)
        ):
            raise ValueError("rollback fuera de la cantidad abierta atribuible")
        note = f"{REDUCTION_PREFIX}{original_coid}"
        if s.exec(select(Trade).where(Trade.status == "pending", Trade.notes == note)).first():
            raise ValueError("rollback anterior sin resolver; prohibido revender")
        s.add(
            Trade(
                client_order_id=coid,
                ticker=original.ticker,
                side=original.side,
                action="sell",
                strategy=original.strategy,
                price_cents=price,
                status="pending",
                notes=note,
                **quantity_fields(count),
            )
        )
        s.commit()


def finalize_reduction(coid: str, sold, order_id: str | None = None) -> None:
    with get_session() as s:
        s.exec(text("BEGIN IMMEDIATE"))
        sell = s.exec(select(Trade).where(Trade.client_order_id == coid)).one()
        if sell.status in {"settled", "cancelled"}:
            if sold != confirmed_fill(sell) or (
                order_id is not None
                and sell.kalshi_order_id is not None
                and order_id != sell.kalshi_order_id
            ):
                raise ValueError("replay de rollback contradice fill/identidad persistidos")
            return
        if not (sell.notes or "").startswith(
            REDUCTION_PREFIX
        ) or not 0 <= sold <= requested_quantity(sell):
            raise ValueError("fill de rollback inválido")
        observed = confirmed_fill(sell)
        if observed is not None and sold < observed:
            raise ValueError("fill final regresivo respecto del piso observado")
        if sold == 0:
            sell.status = "cancelled"
            sell.filled_count = 0
            sell.filled_count_fp = "0.00"
            sell.kalshi_order_id = order_id
            s.add(sell)
            s.commit()
            return
        original_coid = sell.notes[len(REDUCTION_PREFIX) :]
        buy = s.exec(select(Trade).where(Trade.client_order_id == original_coid)).one()
        available = open_quantity(buy)
        if buy.status != "filled" or buy.closed_by_clv or sold > available:
            raise ValueError("venta excede la cantidad abierta de su BUY")
        price = buy.fill_price_cents or buy.price_cents
        allocation = allocate_entry(buy, sold)
        exit_fee = kalshi_fee_cents(sold, sell.price_cents)
        pnl = sold * sell.price_cents - allocation.gross_cents - allocation.fee_cents - exit_fee
        now = _naive_utc_now()
        if sold == available:
            closed = buy
        else:
            resize_filled(buy, available - sold)
            for field, value in allocation.remaining_fields.items():
                setattr(buy, field, value)
            s.add(buy)
            closed = Trade(
                client_order_id=f"{original_coid}-rbs{uuid.uuid4().hex[:8]}",
                commitment_origin_id=buy.commitment_origin_id,
                ticker=buy.ticker,
                side=buy.side,
                action="buy",
                strategy=buy.strategy,
                price_cents=buy.price_cents,
                fill_price_cents=price,
                **filled_fields(sold),
            )
        closed.status = "settled"
        closed.closed_by_clv = True
        closed.settled_at = now
        closed.pnl_usd_fp = usd_text(pnl / 100)
        closed.pnl_cents = floor(pnl)
        for field, value in allocation.closed_fields.items():
            setattr(closed, field, value)
        closed.notes = f"{closed.notes or ''} rollback_sell~{sell.price_cents}c".strip()[:500]
        resize_filled(sell, sold)
        sell.status = "settled"
        sell.closed_by_clv = True
        sell.fill_price_cents = sell.price_cents
        sell.fees_cents = exit_fee
        sell.kalshi_order_id = order_id
        sell.filled_at = sell.settled_at = now
        s.add(closed)
        s.add(sell)
        s.commit()
