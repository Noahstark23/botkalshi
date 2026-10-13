"""Reservas durables: un presupuesto original compartido, antes de cualquier orden."""

import hashlib
import json
from datetime import UTC, datetime, time
from fractions import Fraction
from math import ceil
from zoneinfo import ZoneInfo

from sqlalchemy import func, text
from sqlmodel import col, or_, select

from src.control.permissions import DurablePermissionAdapter
from src.math.fees import kalshi_fee_cents
from src.math.quantities import fixed_count
from src.risk.exposure import MM_STRATEGY, exposure_count, open_risk_cents
from src.storage.models import (
    OperationalState,
    PortfolioPosition,
    RiskCommitment,
    Trade,
    get_session,
)
from src.storage.quantities import filled_quantity, requested_quantity
from src.strategies.motor_1_arbitrage.event_exposure import event_ticker_of
from src.strategies.motor_5_mm.fee_policy import SUPPORTED_FEE_TYPE, EffectiveFeeObservation


def naive_utc(value: datetime) -> datetime:
    return value if value.tzinfo is None else value.astimezone(UTC).replace(tzinfo=None)


def daily_committed_cents(session, *, start: datetime, now: datetime) -> int:
    """No reconstruir originales desde cantidades mutables ni aceptar legacy sin evidencia."""
    return _committed_cents(session, start=start, now=now, daily=True)


def lifetime_committed_cents(session, *, now: datetime) -> int:
    """Historial completo: cambiar el inicio o el día no renueva el presupuesto.

    Incluye compromisos anteriores de esta DB. No reconstruye presupuesto desde
    cantidades mutables. Una entrada legacy sin compromiso verificable bloquea.
    """
    return _committed_cents(session, start=datetime.min, now=now, daily=False)


def _committed_cents(session, *, start: datetime, now: datetime, daily: bool) -> int:
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
    since = max(start, day_start) if daily else start
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


def reserve_m1_intents(
    trades: list[Trade],
    *,
    limits: dict | None = None,
    now=None,
    fee_observations: dict | None = None,
    permissions: DurablePermissionAdapter | None = None,
) -> bool:
    if any(t.strategy != "motor_1_arbitrage" or t.action != "buy" for t in trades):
        raise ValueError("sólo intents BUY M1 pendientes")
    return reserve_trade_intents(
        trades,
        limits=limits,
        now=now,
        fee_observations=fee_observations,
        **({"permissions": permissions} if permissions is not None else {}),
    )


def validate_multi_fee(ticker: str, observation: object, *, now: datetime) -> None:
    """El taker multiplier1 sólo es cota si la tarifa efectiva está demostrada <=1."""
    if not isinstance(observation, EffectiveFeeObservation):
        raise ValueError("fee efectiva multi ausente")
    if (
        ticker.split("-", 1)[0] != "KXMLBGAME"
        or observation.series_ticker != "KXMLBGAME"
        or observation.event_ticker != event_ticker_of(ticker)
        or observation.fee_type != SUPPORTED_FEE_TYPE
        or not 0 < observation.multiplier <= 1
    ):
        raise ValueError("fee/universo multi no admitido")
    observed = observation.observed_at
    if (
        observed.tzinfo is None
        or not 0 <= (naive_utc(now) - naive_utc(observed)).total_seconds() <= 30
    ):
        raise ValueError("fee efectiva multi vencida")


def reserve_trade_intents(
    trades: list[Trade],
    *,
    limits: dict | None = None,
    now=None,
    fee_observations: dict | None = None,
    permissions: DurablePermissionAdapter | None = None,
) -> bool:
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
            trade.status != "pending"
            or trade.strategy not in {"motor_1_arbitrage", "motor_2_consensus", MM_STRATEGY}
            or trade.action not in ({"buy", "sell"} if trade.strategy == MM_STRATEGY else {"buy"})
        ):
            raise ValueError("intent de entrada no admitido")
        if trade.strategy != "motor_1_arbitrage" and not (limits or {}).get("multi_profile"):
            raise ValueError("reservas M2/M5 requieren perfil multi")
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
            if limits.get("multi_profile"):
                _validate_multi_stops(session, now=now)
                for name, ceiling in (
                    ("max_open_cents", 300),
                    ("event_cap_cents", 100),
                    ("max_daily_cents", 300),
                ):
                    if type(limits.get(name)) is not int or not 0 < limits[name] <= ceiling:
                        raise ValueError("tope global multi inválido")
                if not isinstance(limits.get("experiment_start"), datetime):
                    raise ValueError("inicio multi ausente")
                if len({t.strategy for t in trades}) != 1:
                    raise ValueError("grupo mezcla propietarios")
                for trade in trades:
                    validate_multi_fee(
                        trade.ticker, (fee_observations or {}).get(trade.ticker), now=now
                    )
                cap = limits.get("max_operation_cents")
                if type(cap) is not int or not 0 < cap <= 100 or proposed > cap:
                    raise ValueError("reserva multi excede operación")
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
            portfolio_rows = list(session.exec(select(PortfolioPosition)))
            if limits.get("multi_profile"):
                _validate_multi_ownership(trades, active, portfolio_rows, now=now)
                if trades[0].strategy == "motor_2_consensus":
                    proposed_events = {event_ticker_of(row.ticker) for row in trades}
                    if any(
                        row.strategy == "motor_2_consensus"
                        and event_ticker_of(row.ticker) in proposed_events
                        for row in active
                    ):
                        raise ValueError("M2 ya tiene entrada en el evento")
                tickers = {row.ticker for row in active} | {
                    row.ticker for row in portfolio_rows if requested_quantity(row) > 0
                }
                if len(tickers | {row.ticker for row in trades}) > 8:
                    raise ValueError("reserva multi excede ocho tickers abiertos")
                if "max_strategy_cents" in limits:
                    cap = limits["max_strategy_cents"]
                    own = [row for row in active if row.strategy == trades[0].strategy]
                    cost = sum(
                        open_risk_cents(row)
                        + kalshi_fee_cents(exposure_count(row), row.price_cents)
                        for row in own
                    )
                    if type(cap) is not int or cap <= 0 or cost + proposed > cap:
                        raise ValueError("reserva multi excede subtope del motor")
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
            for row in portfolio_rows:
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
            if "max_lifetime_cents" in limits:
                cap = limits["max_lifetime_cents"]
                if type(cap) is not int or not 0 < cap <= 5000:
                    raise ValueError("límite histórico de reserva inválido")
                total = lifetime_committed_cents(session, now=now)
                if total + proposed > cap:
                    raise ValueError("reserva M1 excede presupuesto histórico")
        if permissions is not None:
            strategies = {trade.strategy for trade in trades}
            if len(strategies) != 1:
                raise ValueError("grupo de admisión mezcla estrategias")
            scope = {
                "motor_1_arbitrage": "m1.entries",
                "motor_2_consensus": "m2.entries",
                MM_STRATEGY: "m5.quotes",
            }[trades[0].strategy]
            permissions.admit(session, scope, trades)
        for trade, commitment in zip(trades, commitments, strict=True):
            trade.commitment_origin_id = commitment.client_order_id
            trade.placed_at = now.replace(tzinfo=UTC)
            session.add(trade)
            session.add(commitment)
        session.commit()
    return True


def _validate_multi_ownership(trades, active, portfolio_rows, *, now):
    """No netear ni vender inventario de otro motor por compartir cuenta/ticker.

    Una posición de portfolio sin cantidades ejecutadas atribuibles no demuestra
    propiedad. El rechazo conserva la fila y su consumo, nunca reasigna un fill.
    """
    strategy = trades[0].strategy
    for ticker in {t.ticker for t in trades}:
        owned = [row for row in active if row.ticker == ticker]
        if any(row.strategy != strategy for row in owned):
            raise ValueError("ticker ocupado por otro motor")
        for position in portfolio_rows:
            if position.ticker != ticker or requested_quantity(position) <= 0:
                continue
            if not 0 <= (now - naive_utc(position.synced_at)).total_seconds() <= 60:
                raise ValueError("portfolio de ticker sin conciliación fresca")
            net = Fraction(0)
            for row in owned:
                quantity = filled_quantity(row)
                if quantity is None:
                    raise ValueError("propiedad con fill desconocido")
                sign = (1 if row.side == "yes" else -1) * (1 if row.action == "buy" else -1)
                net += sign * quantity
            actual = requested_quantity(position) * (1 if position.side == "yes" else -1)
            if position.side not in {"yes", "no"} or net != actual:
                raise ValueError("portfolio sin propiedad reconciliada")


def _validate_multi_stops(session, *, now):
    """Revalidación durable bajo el mismo lock SQLite que concede el presupuesto."""
    from datetime import timedelta

    from src.monitoring.health import BotState
    from src.strategies.motor_5_mm.incidents import incident_status

    paused = session.get(OperationalState, "kill_switch")
    if BotState.is_paused or (paused is not None and paused.value != "clear"):
        raise ValueError("pausa durable/global antes de admisión")
    if incident_status()[0]:
        raise ValueError("incidente M5 antes de admisión")
    local = now.replace(tzinfo=UTC).astimezone(ZoneInfo("America/Los_Angeles"))
    week_start = naive_utc(
        datetime.combine(
            local.date() - timedelta(days=local.weekday()), time.min, tzinfo=local.tzinfo
        )
    )
    cumulative = weekly = 0
    for row in session.exec(select(Trade).where(Trade.status == "settled")):
        if (
            row.settled_at is None
            or type(row.pnl_cents) is not int
            or naive_utc(row.settled_at) > now
        ):
            raise ValueError("settlement multi inválido")
        cumulative += row.pnl_cents
        if naive_utc(row.settled_at) >= week_start:
            weekly += row.pnl_cents
    if cumulative <= -500 or weekly <= -300:
        raise ValueError("stop de pérdida global multi")
