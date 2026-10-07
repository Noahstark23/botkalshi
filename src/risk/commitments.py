"""Reserva M1 durable: presupuesto original y admisión por evento en una transacción."""

import hashlib
import json
from datetime import UTC, datetime, time
from math import ceil
from zoneinfo import ZoneInfo

from sqlalchemy import func, text
from sqlmodel import col, or_, select

from src.math.fees import kalshi_fee_cents
from src.math.quantities import fixed_count
from src.risk.exposure import MM_STRATEGY, exposure_count, open_risk_cents
from src.storage.models import PortfolioPosition, RiskCommitment, Trade, get_session
from src.storage.quantities import requested_quantity
from src.strategies.motor_1_arbitrage.event_exposure import event_ticker_of


def naive_utc(value: datetime) -> datetime:
    return value if value.tzinfo is None else value.astimezone(UTC).replace(tzinfo=None)


def daily_committed_cents(session, *, start: datetime, now: datetime) -> int:
    """No reconstruir originales desde cantidades mutables ni aceptar legacy sin evidencia."""
    start, now = naive_utc(start), naive_utc(now)
    if start > now:
        raise ValueError("inicio experimental futuro")
    missing = session.exec(
        select(Trade.id)
        .outerjoin(RiskCommitment, Trade.commitment_origin_id == RiskCommitment.client_order_id)
        .where(
            Trade.placed_at >= start,
            or_(Trade.action == "buy", Trade.strategy == MM_STRATEGY),
            or_(
                col(RiskCommitment.client_order_id).is_(None),
                Trade.ticker != RiskCommitment.ticker,
                Trade.side != RiskCommitment.side,
                Trade.action != RiskCommitment.action,
                Trade.strategy != RiskCommitment.strategy,
            ),
        )
        .limit(1)
    ).first()
    if missing is not None:
        raise ValueError("entrada posterior al inicio sin compromiso original verificable")
    local = now.replace(tzinfo=UTC).astimezone(ZoneInfo("America/Los_Angeles"))
    day_start = naive_utc(datetime.combine(local.date(), time.min, tzinfo=local.tzinfo))
    since = max(start, day_start)
    invalid = session.exec(
        select(RiskCommitment.client_order_id)
        .where(
            RiskCommitment.created_at >= start,
            or_(
                RiskCommitment.created_at > now,
                RiskCommitment.risk_cents <= 0,
                RiskCommitment.fee_reserve_cents < 0,
            ),
        )
        .limit(1)
    ).first()
    if invalid is not None:
        raise ValueError("compromiso original inválido")
    amount = session.exec(
        select(
            func.coalesce(func.sum(RiskCommitment.risk_cents + RiskCommitment.fee_reserve_cents), 0)
        ).where(RiskCommitment.created_at >= since, RiskCommitment.created_at <= now)
    ).one()
    return int(amount)


def reserve_m1_intents(trades: list[Trade], *, limits: dict | None = None, now=None) -> bool:
    """True sólo para reserva nueva; replay idéntico no reenvía órdenes.

    ``limits`` es obligatorio en execute; None sirve al helper de persistencia
    sin admisión (tests/migración explícita). No existe I/O financiero aquí.
    El gross cap es conservador: no libera presupuesto por un hedge supuesto.
    """
    now = naive_utc(now or datetime.now(UTC))
    coids = [t.client_order_id for t in trades]
    if not trades or len(set(coids)) != len(coids):
        raise ValueError("grupo de intents vacío o duplicado")
    group_key = hashlib.sha256(json.dumps(sorted(coids)).encode()).hexdigest()
    commitments = []
    for trade in trades:
        if (
            trade.strategy != "motor_1_arbitrage"
            or trade.status != "pending"
            or trade.action != "buy"
        ):
            raise ValueError("sólo intents BUY M1 pendientes")
        count = requested_quantity(trade)
        if count <= 0:
            raise ValueError("cantidad propuesta inválida")
        risk = open_risk_cents(trade)
        commitments.append(
            RiskCommitment(
                client_order_id=trade.client_order_id,
                group_key=group_key,
                ticker=trade.ticker,
                event_ticker=event_ticker_of(trade.ticker),
                strategy=trade.strategy,
                side=trade.side,
                action=trade.action,
                count_fp=fixed_count(count),
                price_cents=trade.price_cents,
                risk_cents=risk,
                fee_reserve_cents=kalshi_fee_cents(count, trade.price_cents),
                created_at=now,
            )
        )
    with get_session() as session:
        session.exec(text("BEGIN IMMEDIATE"))
        previous = list(
            session.exec(
                select(RiskCommitment).where(col(RiskCommitment.client_order_id).in_(coids))
            )
        )
        existing = list(session.exec(select(Trade).where(col(Trade.client_order_id).in_(coids))))
        if previous or existing:
            if len(previous) != len(trades) or len(existing) != len(trades):
                raise ValueError("reserva previa incompleta")
            prior = {p.client_order_id: p for p in previous}
            for candidate in commitments:
                before = prior[candidate.client_order_id]
                fields = set(type(candidate).model_fields) - {"created_at"}
                if any(getattr(before, f) != getattr(candidate, f) for f in fields):
                    raise ValueError("replay cambia compromiso original")
            return False
        if limits is not None:
            proposed = sum(c.risk_cents + c.fee_reserve_cents for c in commitments)
            for key in ("max_open_cents", "event_cap_cents"):
                if type(limits.get(key)) is not int or limits[key] <= 0:
                    raise ValueError("límite de reserva inválido")
            active = list(
                session.exec(
                    select(Trade).where(
                        or_(
                            col(Trade.status).in_(["pending", "filled"]),
                            (Trade.strategy == MM_STRATEGY)
                            & col(Trade.status).notin_(["settled", "cancelled"]),
                        )
                    )
                )
            )
            pending = {}
            filled = {}
            for row in active:
                amount = open_risk_cents(row)
                fee = kalshi_fee_cents(exposure_count(row), row.price_cents)
                if row.fees_cents is not None:
                    if type(row.fees_cents) is not int or row.fees_cents < 0:
                        raise ValueError("fee de exposición inválida")
                    fee = max(fee, row.fees_cents)
                amount += fee
                direction = (
                    row.side if row.action == "buy" else ("no" if row.side == "yes" else "yes")
                )
                identity = (row.ticker, direction)
                target = pending if row.status == "pending" else filled
                target[identity] = target.get(identity, 0) + amount
            portfolio = {}
            for row in session.exec(select(PortfolioPosition)):
                if requested_quantity(row) <= 0:
                    continue
                amount = row.exposure_cents
                if amount is None:
                    amount = ceil(requested_quantity(row) * 100)
                if type(amount) is not int or amount < 0:
                    raise ValueError("exposición de portfolio inválida")
                if row.side not in {"yes", "no"}:
                    raise ValueError("dirección de portfolio inválida")
                identity = (row.ticker, row.side)
                portfolio[identity] = portfolio.get(identity, 0) + amount
            # Sólo solapar fuentes de la MISMA identidad. Pending nunca se resta:
            # puede llenarse después de la foto de portfolio; sobrecontar es seguro.
            combined = {
                key: pending.get(key, 0) + max(filled.get(key, 0), portfolio.get(key, 0))
                for key in pending.keys() | filled.keys() | portfolio.keys()
            }
            by_event = {}
            for (ticker, _direction), amount in combined.items():
                event = event_ticker_of(ticker)
                by_event[event] = by_event.get(event, 0) + amount
            if sum(combined.values()) + proposed > limits["max_open_cents"]:
                raise ValueError("reserva M1 excede exposición global")
            proposed_events = {}
            for commitment in commitments:
                event = commitment.event_ticker
                proposed_events[event] = (
                    proposed_events.get(event, 0)
                    + commitment.risk_cents
                    + commitment.fee_reserve_cents
                )
            for event, amount in proposed_events.items():
                current = by_event.get(event, 0)
                if current + amount > limits["event_cap_cents"]:
                    raise ValueError("reserva M1 excede cap por evento")
            if limits.get("experiment_start") is not None:
                daily = daily_committed_cents(session, start=limits["experiment_start"], now=now)
                cap = limits.get("max_daily_cents")
                if type(cap) is not int or cap <= 0 or daily + proposed > cap:
                    raise ValueError("reserva M1 excede presupuesto diario")
        for trade, commitment in zip(trades, commitments, strict=True):
            trade.commitment_origin_id = commitment.client_order_id
            trade.placed_at = now.replace(tzinfo=UTC)
            session.add(trade)
            session.add(commitment)
        session.commit()
    return True
