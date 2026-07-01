"""
Motor2Executor — ejecutor SINGLE-LEG direccional del Motor 2 (consenso sportsbooks).

A diferencia del RestExecutor (arb binario de 2 patas con rollback/reconciliación), una
apuesta de Motor 2 es UNA sola orden direccional: comprar UN lado (YES o NO) de UN
mercado, al precio donde el consenso de casas marca edge. No hay pata hermana → NO hay
rollback ni "exposición huérfana": si la orden no llena, no pasó nada.

Reusa, sin reimplementar:
  - El patrón A.1 (fila Trade 'intent' PRE-red → los ojos del RiskManager).
  - El RiskManager (vía un ArbOpportunity de UNA pata → todos los caps y stop-losses).
  - El muro Capa C de place_order (buy bloqueado con TRADING_ENABLED=false).
  - El settlement poller (se extiende para incluir strategy="motor_2_consensus").

Orden IOC limit (immediate_or_cancel): toma liquidez al precio AHORA o cancela — NUNCA
deja una orden resting viva (evita el bug del Issue #14 de exposición silenciosa).

⚠️ DIRECCIONAL, NO ARBITRAJE: una apuesta de Motor 2 PUEDE PERDER (es +EV, no profit
bloqueado). El sizing lo acota el RiskManager (tope por trade + cap de exposición); la
varianza a tamaño chico es alta — el edge paga en muchas apuestas, no en pocas.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from loguru import logger
from sqlmodel import col, select

from src.clients.kalshi_rest import KalshiClientError, KalshiRestClient, TradingDisabledError
from src.math.arbitrage import ArbLeg, ArbOpportunity
from src.math.fees import kalshi_fee_cents
from src.risk.manager import RiskManager
from src.storage.models import Trade, engage_kill_switch, get_session
from src.strategies.motor_2_consensus.detector import ConsensusSignal

STRATEGY = "motor_2_consensus"


def _naive_utc_now() -> datetime:
    """Convención del proyecto para writes a Trade (manager.py): naive UTC."""
    return datetime.now(UTC).replace(tzinfo=None)


def _as_int(value: object) -> int | None:
    """Castea a int un campo que puede venir int o fixed-point string. None si inválido."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (str, float)):
        try:
            return int(round(float(value)))
        except (ValueError, TypeError):
            return None
    return None


@dataclass
class Motor2ExecutionOutcome:
    """Desenlace de intentar ejecutar una señal de Motor 2."""

    placed: bool  # llegó a tocar la red (place_order invocado)
    filled: bool  # llenó (posición real abierta)
    filled_count: int = 0
    reason: str = ""
    client_order_id: str | None = None
    uncertain: bool = False  # ERROR_RED: fill desconocido (fila queda pending)


class Motor2Executor:
    """
    Ejecutor direccional del Motor 2. Una instancia por motor.

    `execute(signal)` aplica: sizing → RiskManager → intent PRE-red → orden IOC →
    update de la fila. SHADOW-safe por construcción: el muro Capa C de place_order
    bloquea cualquier buy con TRADING_ENABLED=false (lo trata como no-fill limpio).
    """

    def __init__(
        self,
        client: KalshiRestClient,
        risk_manager: RiskManager,
        *,
        min_entry_cents: int = 1,
        underdog_filter_enabled: bool = False,
        one_bet_per_event: bool = True,
    ) -> None:
        self.client = client
        self.risk = risk_manager
        # Scope del dedup cross-ciclo (fix auditoría 2026-07-01): True → una posición
        # abierta por EVENTO (la promesa del flag MOTOR_2_ONE_BET_PER_EVENT); False →
        # por market_ticker. Sin esto, un edge de consenso persistente (dura horas) se
        # re-apostaba cada ciclo de 5 min hasta chocar con el cap global de exposición.
        self._one_bet_per_event = one_bet_per_event
        # Filtro underdog (FASE 3): las entradas <40c sangraron −$110,77 en el histórico
        # (17 de 21 perdedoras). min_entry_cents=1 + filter off = no-op (default seguro).
        self._min_entry_cents = min_entry_cents
        self._underdog_filter_enabled = underdog_filter_enabled

    async def execute(self, signal: ConsensusSignal) -> Motor2ExecutionOutcome:
        side = signal.kalshi_side.lower()  # ConsensusSignal usa "YES"/"NO"
        price = signal.kalshi_price_cents
        if side not in ("yes", "no") or not (1 <= price <= 99):
            return Motor2ExecutionOutcome(False, False, reason="señal fuera de rango")

        # Filtro underdog: SHADOW intra-live → con el flag off LOGUEA lo que bloquearía pero
        # igual entra; con el flag on, bloquea. (El executor solo existe con TRADING_ENABLED,
        # así que el "shadow" del filtro es este, no la Capa A global.)
        if price < self._min_entry_cents:
            logger.info(
                f"motor2.exec.underdog_skip ticker={signal.market_ticker} side={side} "
                f"price={price}c < {self._min_entry_cents}c enabled={self._underdog_filter_enabled}"
            )
            if self._underdog_filter_enabled:
                return Motor2ExecutionOutcome(False, False, reason="underdog_filter")

        # DEDUP CROSS-CICLO: si ya hay una posición viva (pending/filled) de Motor 2 en
        # este evento (o ticker), NO se re-apuesta el mismo edge en el próximo ciclo.
        # Una fila 'settled' (resuelta o cerrada por exit) libera el evento de nuevo.
        if self._open_position_exists(signal):
            logger.info(
                f"motor2.exec.dedup_skip ticker={signal.market_ticker} "
                f"event={signal.event_key or '(sin event_key → scope ticker)'} "
                "(posición abierta previa en el evento — no se re-apuesta)"
            )
            return Motor2ExecutionOutcome(False, False, reason="already_open")

        # Sizing deseado desde el detector; el RiskManager es la autoridad final (cap por
        # trade + exposición) y puede recortarlo. FLOOR, no round (deuda auditoría
        # 2026-07-01: round subía hasta medio contrato sobre el stake y max(1,...)
        # forzaba 1 contrato aunque el stake fuera menor que el precio → hasta 1.5×
        # el cap de sizing diseñado).
        if signal.recommended_size_usd <= 0:
            return Motor2ExecutionOutcome(False, False, reason="size recomendado 0")
        desired = int(signal.recommended_size_usd * 100 // price)
        if desired <= 0:
            logger.info(
                f"motor2.exec.stake_below_one_contract ticker={signal.market_ticker} "
                f"stake=${signal.recommended_size_usd:.2f} < price={price}c (no se fuerza 1 contrato)"
            )
            return Motor2ExecutionOutcome(False, False, reason="stake_below_one_contract")

        opp = self._as_single_leg_opp(signal, side, price, desired)
        decision = await self.risk.check_pre_trade(opp)
        if not decision.approved:
            logger.info(
                f"motor2.exec.rejected ticker={signal.market_ticker} reason={decision.reason}"
            )
            return Motor2ExecutionOutcome(False, False, reason=decision.reason)
        count = decision.max_allowed_count

        # A.1 — intent PRE-red. bet_id (uuid) como prefijo del coid; NO se marca como
        # 'arb_id=' en notes (es direccional: el RiskManager debe contarlo ENTERO, sin
        # descuento de hedge). Si el persist falla → ABORT sin colocar (fail-safe).
        bet_id = str(uuid.uuid4())
        coid = f"{bet_id}-{side}"
        if not self._persist_intent(signal, side, price, count, coid):
            return Motor2ExecutionOutcome(False, False, reason="persist_intent_failed")

        try:
            resp = await self.client.place_order(
                ticker=signal.market_ticker,
                side=side,
                action="buy",
                count=count,
                order_type="limit",
                yes_price=price if side == "yes" else None,
                no_price=price if side == "no" else None,
                client_order_id=coid,
                time_in_force="immediate_or_cancel",
            )
        except TradingDisabledError:
            # Muro Capa C (shadow): el buy se bloquea con TRADING_ENABLED=false. No es
            # error — la fila se cierra como cancelada y el motor sigue midiendo.
            self._update_trade(
                coid, critical=False, status="cancelled", notes_append="trading_disabled"
            )
            return Motor2ExecutionOutcome(
                False, False, reason="trading_disabled", client_order_id=coid
            )
        except KalshiClientError as exc:
            # 4xx determinístico (saldo, mercado cerrado, precio inválido): sin posición.
            self._update_trade(
                coid,
                critical=False,
                status="cancelled",
                notes_append=f"client_error_{exc.status_code}",
            )
            logger.warning(
                f"motor2.exec.client_error ticker={signal.market_ticker} status={exc.status_code}: {exc}"
            )
            return Motor2ExecutionOutcome(
                True, False, reason=f"client_error_{exc.status_code}", client_order_id=coid
            )
        except Exception as exc:
            # ERROR_RED: la orden pudo llegar y llenar → DESCONOCIDO. Se deja la fila
            # 'pending': el RiskManager la cuenta como exposición (conservador) y el
            # reconcile/settlement la resuelve. NO kill-switch: una apuesta direccional
            # incierta no es exposición naked como una pata huérfana de arb.
            self._update_trade(coid, critical=False, notes_append="error_red_unknown")
            logger.warning(f"motor2.exec.error_red ticker={signal.market_ticker}: {exc}")
            return Motor2ExecutionOutcome(
                True, False, uncertain=True, reason="error_red", client_order_id=coid
            )

        order = resp.get("order", resp) if isinstance(resp, dict) else {}
        order_id = str(order.get("order_id", "")) or None
        # Coalesce por is-not-None (deuda auditoría 2026-07-01): fill_count PRESENTE con
        # null enmascaraba el fallback fill_count_fp.
        fill_raw = order.get("fill_count")
        if fill_raw is None:
            fill_raw = order.get("fill_count_fp")
        fill_count = _as_int(fill_raw)
        if fill_count is None:
            # HTTP 200 con fill ILEGIBLE en ambos shapes: NO asumir no-fill (la orden pudo
            # llenar — tratarla como 'cancelled' dejaba una posible posición viva INVISIBLE
            # al RiskManager). La fila queda 'pending' (exposición conservadora, mismo
            # trato que ERROR_RED); el settlement/reconcile la resuelve.
            self._update_trade(coid, critical=False, notes_append="fill_unreadable")
            logger.warning(
                f"motor2.exec.fill_unreadable ticker={signal.market_ticker} coid={coid} "
                f"keys={sorted(order.keys())} (200 OK con fill ilegible → fila pending)"
            )
            try:
                from src.monitoring.health import BotState

                BotState.record_error(f"motor2.exec fill_unreadable coid={coid}")
            except Exception:  # pragma: no cover - visibilidad best-effort
                pass
            return Motor2ExecutionOutcome(
                True, False, uncertain=True, reason="fill_unreadable", client_order_id=coid
            )

        if fill_count > 0:
            # Posición real abierta. Precio: se usa el LÍMITE (la IOC marketable llena
            # al límite o mejor; registrar el límite sobrestima el costo levemente →
            # conservador para el stop-loss). [verificar en smoke] leer avg fill real.
            self._update_trade(
                coid,
                critical=True,
                status="filled",
                filled_at=_naive_utc_now(),
                count=fill_count,
                fill_price_cents=price,
                fees_cents=kalshi_fee_cents(fill_count, price),
                kalshi_order_id=order_id,
            )
            logger.info(
                f"motor2.exec.filled ticker={signal.market_ticker} side={side} "
                f"count={fill_count} price={price}c edge={signal.edge_pct:.3f}"
            )
            return Motor2ExecutionOutcome(True, True, filled_count=fill_count, client_order_id=coid)

        # IOC sin fill (no había liquidez al límite) → cancelado limpio, sin posición.
        self._update_trade(
            coid,
            critical=False,
            status="cancelled",
            notes_append="ioc_no_fill",
            kalshi_order_id=order_id,
        )
        logger.info(
            f"motor2.exec.no_fill ticker={signal.market_ticker} side={side} (IOC sin liquidez al límite)"
        )
        return Motor2ExecutionOutcome(True, False, reason="ioc_no_fill", client_order_id=coid)

    # =====================================================
    # Helpers
    # =====================================================

    @staticmethod
    def _as_single_leg_opp(
        signal: ConsensusSignal, side: str, price: int, count: int
    ) -> ArbOpportunity:
        """
        Envuelve la señal en un ArbOpportunity de UNA pata para reusar check_pre_trade.

        El RiskManager solo usa `legs` (para el costo por unidad) y `count`; los campos
        de profit son informativos. edge_pct se propaga para la fila Trade/decisión.
        """
        leg = ArbLeg(
            market_ticker=signal.market_ticker,
            side=side,  # type: ignore[arg-type]
            price_cents=price,
            count=count,
            available_size=count,
        )
        return ArbOpportunity(
            legs=(leg,),
            count=count,
            gross_profit_cents=0,
            fees_cents=kalshi_fee_cents(count, price),
            net_profit_cents=0,
            edge_pct=signal.edge_pct,
        )

    def _open_position_exists(self, signal: ConsensusSignal) -> bool:
        """True si hay un Trade vivo (pending/filled) de Motor 2 en el evento/ticker.

        Scope EVENTO (default, `one_bet_per_event=True`): cualquier market del mismo
        evento bloquea (los outcomes de un partido viven en tickers DISTINTOS pero son
        el mismo riesgo direccional — ver _collapse_event_signals). Scope TICKER si el
        flag está off o la señal no trae event_key. Fail-safe: un error de DB bloquea
        la apuesta (mejor perder una señal que apilar exposición sin saberlo).
        """
        try:
            with get_session() as s:
                stmt = select(Trade).where(
                    Trade.strategy == STRATEGY,
                    col(Trade.status).in_(("pending", "filled")),
                )
                if self._one_bet_per_event and signal.event_key:
                    stmt = stmt.where(col(Trade.ticker).like(f"{signal.event_key}-%"))
                else:
                    stmt = stmt.where(Trade.ticker == signal.market_ticker)
                return s.exec(stmt.limit(1)).first() is not None
        except Exception:
            logger.exception(
                f"motor2.exec.dedup_check_failed ticker={signal.market_ticker} → skip conservador"
            )
            return True

    def _persist_intent(
        self, signal: ConsensusSignal, side: str, price: int, count: int, coid: str
    ) -> bool:
        """Escribe la fila Trade 'pending' ANTES de tocar la red (patrón A.1). False si falla."""
        try:
            with get_session() as s:
                s.add(
                    Trade(
                        client_order_id=coid,
                        ticker=signal.market_ticker,
                        side=side,
                        action="buy",
                        count=count,
                        price_cents=price,
                        strategy=STRATEGY,
                        estimated_edge_pct=signal.edge_pct,
                        status="pending",
                        notes=f"motor2 fair={signal.odds_api_fair_prob}",
                    )
                )
                s.commit()
            return True
        except Exception:
            logger.critical(
                f"motor2.exec.persist_intent_failed coid={coid} → ABORT (RiskManager quedaría ciego)"
            )
            return False

    def _update_trade(
        self, coid: str, *, critical: bool, notes_append: str | None = None, **fields: object
    ) -> None:
        """
        Actualiza la fila Trade. critical=True (fill real): si el update falla, el
        RiskManager queda ciego a una posición viva → PAUSA PREVENTIVA persistente
        (misma decisión que A.1). critical=False: best-effort.
        """
        try:
            with get_session() as s:
                t = s.exec(select(Trade).where(Trade.client_order_id == coid)).first()
                if t is None:
                    logger.warning(f"motor2.exec.update_trade.missing coid={coid}")
                    return
                for k, v in fields.items():
                    setattr(t, k, v)
                if notes_append:
                    t.notes = f"{t.notes or ''} {notes_append}".strip()[:500]
                s.add(t)
                s.commit()
        except Exception:
            logger.exception(f"motor2.exec.update_trade_failed coid={coid} critical={critical}")
            if critical:
                self._engage_preventive_pause(coid)

    def _engage_preventive_pause(self, coid: str) -> None:
        """Fill real que no se pudo registrar → kill-switch persistente (RiskManager ciego)."""
        msg = f"motor2: persist post-fill falló (coid={coid}) — RiskManager ciego a posición viva"
        logger.critical(f"motor2.exec.preventive_pause {msg}")
        try:
            from src.monitoring.health import BotState

            BotState.is_paused = True
            BotState.pause_reason = msg
            engage_kill_switch(msg)
        except Exception:
            logger.exception("motor2.exec.preventive_pause.persist_failed")
