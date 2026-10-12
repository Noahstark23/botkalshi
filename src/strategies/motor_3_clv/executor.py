"""
Motor3ExitExecutor (Motor 3, FASE 3) — liquida UNA posición direccional con un SELL.

Una salida CLV es UNA orden de venta del lado que tenemos abierto (yes/no), al bid actual,
para cerrar la posición antes del cierre del mercado. Single-leg → sin rollback ni pata
huérfana (modelado sobre el executor de Motor 2, no sobre el RestExecutor de arb).

⚠️ SEGURIDAD: `place_order` NO bloquea `sell` con TRADING_ENABLED=false (Capa C es solo
para entradas). Por eso la protección shadow de Motor 3 es CAPA A: este executor SOLO se
construye con TRADING_ENABLED=true (lo decide el runner/engine). En shadow no existe.

IOC, NO FOK (desviación deliberada de la spec): en una LIQUIDACIÓN, un fill parcial es
mejor que no salir. FOK abandonaría la posición entera si no llena todo el size; IOC toma
la liquidez que haya al bid y cancela el resto (se reintenta en el próximo ciclo dentro de
la ventana [28,30] min). El sensor de fill (200+fill_count / 4xx / ERROR_RED) es el mismo
patrón validado.

[follow-up pre-live] PnL realizado del exit + cerrar la fila Trade original (evitar que el
settlement la liquide de nuevo por resolución de mercado). Documentado; no bloquea el build
dormido (MOTOR_3_CLV_ENABLED=false, 0 posiciones en shadow).
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from fractions import Fraction
from math import ceil, floor
from typing import ClassVar

from loguru import logger
from sqlalchemy import text
from sqlmodel import col, select

from src.clients.kalshi_rest import KalshiClientError, KalshiRestClient, TradingDisabledError
from src.math.fees import kalshi_fee_cents
from src.math.quantities import terminal_fill
from src.storage.cost_allocation import allocate_entry, usd_text
from src.storage.exit_capture import apply_exit_execution, exit_execution_note
from src.storage.exit_fills import ExitFillSummary
from src.storage.models import (
    PortfolioPosition,
    Trade,
    _naive_utc_now,
    engage_kill_switch,
    get_session,
)
from src.storage.order_reconciliation import (
    capture_terminal_sell_execution,
    validate_pending_sell_response,
    validate_sell_order_identity,
)
from src.storage.quantities import (
    confirmed_fill,
    filled_fields,
    hold_uncertain_order,
    open_quantity,
    quantity_fields,
    requested_quantity,
    resize_filled,
)
from src.storage.reductions import estimated_exit_note, validated_exit_fee
from src.strategies.data_capture import _top_bid, rest_orderbook_sides

STRATEGY = "motor_3_clv"


@dataclass
class Motor3ExitOutcome:
    """Desenlace de intentar liquidar una posición."""

    placed: bool  # llegó a tocar la red (place_order invocado)
    filled: bool  # llenó algo (posición reducida/cerrada)
    filled_count: int | Fraction = 0
    sell_price_cents: int | None = None
    reason: str = ""
    client_order_id: str | None = None


class Motor3ExitExecutor:
    """Liquida posiciones con SELL IOC al bid. Lock por-ticker anti doble-venta concurrente."""

    # Locks POR TICKER compartidos a nivel PROCESO (ClassVar, fix auditoría 2026-07-01):
    # Motor 2 exits y Motor 3 construyen instancias SEPARADAS de este executor; con locks
    # por instancia, ambos motores podían vender la misma posición en la misma ventana —
    # y en la API V2 la segunda venta no se rechaza: ABRE una posición contraria (yes+sell
    # es el mismo book_side que no+buy).
    _locks: ClassVar[dict[str, asyncio.Lock]] = {}

    def __init__(
        self,
        client: KalshiRestClient,
        *,
        strategy: str = STRATEGY,
        entry_origin: tuple[str, ...] = ("motor_2_consensus",),
        include_motor1_orphans: bool = False,
        min_sell_bid_cents: int = 0,
    ) -> None:
        self.client = client
        # Estrategia con la que se audita la fila SELL del exit. Default motor_3_clv; Motor 2
        # reusa este executor pasando "motor_2_consensus" para que el audit quede atribuido al
        # motor que disparó el cierre (las patas BUY que cierra _settle_originals ya se marcan
        # closed_by_clv por su propia strategy, sin importar este tag).
        self._strategy = strategy
        # Estrategias cuyas patas BUY este exit puede cerrar (y contra las que se dimensiona
        # la venta). motor_rest_arb NO va acá: una leg de arb hedged nunca se liquida suelta
        # (vender la leg ganadora rompe el hedge → profit lockeado se vuelve pérdida).
        self._entry_origin = entry_origin
        # Bug 4 (incidente 2026-07-07): con True, las patas HUÉRFANAS de Motor 1 (arb sin la
        # pata hermana fillada) también cuentan como entradas atribuibles — el cap de
        # _open_attributable_count y el settle las incluyen. Un par hedged completo JAMÁS
        # entra (motor1_orphan_buys lo excluye) → imposible vender la pata de un hedge.
        self._include_motor1_orphans = include_motor1_orphans
        # Auditoría de rentabilidad 2026-07-07: piso de PRECIO para vender. Vender polvo
        # (bid 1-4c) recupera centavos menos el fee (ceil ≥1c) y dona el spread — casi
        # todo lo recuperable se pierde en el peaje. Debajo del piso NO se vende: la
        # posición va a settle (que no paga fee). 0 = sin piso (comportamiento previo).
        self._min_sell_bid_cents = min_sell_bid_cents

    def _lock_for(self, ticker: str) -> asyncio.Lock:
        return type(self)._locks.setdefault(ticker, asyncio.Lock())

    async def exit_position(self, position: PortfolioPosition) -> Motor3ExitOutcome:
        """
        Vende la posición del lado abierto. Si OTRA ejecución ya está liquidando este
        ticker (lock tomado), se DESCARTA — no encolar (estaría stale) ni doble-vender.

        DENTRO del lock se re-verifica contra las filas Trade (fuente de verdad sincrónica):
        si otro exit engine acaba de vender y settlear, las filas ya están closed_by_clv
        aunque la PortfolioPosition (cache de 60s) o el agregado de Motor 2 estén stale —
        se skipea en vez de re-vender. La venta se CAPEA al total abierto atribuible.
        """
        # DECISIÓN DE POLÍTICA (fix auditoría 2026-07-01): pausa/kill-switch bloquea
        # TAMBIÉN las ventas. El runbook del kill-switch (clear_kill_switch.py) exige
        # reconciliación MANUAL con el bot quieto — un exit automático en esa ventana
        # puede duplicar la venta manual del operador (V2: el exceso abre posición
        # contraria) y mantiene el "posiciones != 0" que aborta la limpieza en loop.
        # La detección/shadow logging del engine sigue corriendo; solo la orden se frena.
        try:
            from src.monitoring.health import BotState

            if BotState.is_paused:
                logger.warning(
                    f"motor3.exit.paused ticker={position.ticker} "
                    f"(is_paused=True — sin ventas hasta despausar: {BotState.pause_reason})"
                )
                return Motor3ExitOutcome(False, False, reason="paused")
        except Exception:  # pragma: no cover - el gate nunca debe romper el tick
            logger.exception("motor3.exit.pause_check_failed")
            return Motor3ExitOutcome(False, False, reason="pause_check_failed")
        lock = self._lock_for(position.ticker)
        if lock.locked():
            logger.info(f"motor3.exit.skip_busy ticker={position.ticker}")
            return Motor3ExitOutcome(False, False, reason="busy")
        async with lock:
            # RECONCILE (fix auditoría 2026-07-01): si un sell previo quedó en ERROR_RED
            # (intent 'pending'), resolverlo contra get_orders ANTES de vender de nuevo.
            # Si llenó, re-vender abriría posición contraria (V2 no rechaza el exceso);
            # si no se puede resolver, este tick NO vende (ante la duda, ninguna orden).
            if not await self._reconcile_pending_sells(position.ticker, position.side):
                return Motor3ExitOutcome(False, False, reason="pending_sell_unresolved")
            open_count = self._open_attributable_count(position.ticker, position.side)
            if open_count <= 0:
                logger.info(
                    f"motor3.exit.already_closed ticker={position.ticker} side={position.side} "
                    "(sin patas BUY abiertas atribuibles — otro exit ya cerró o posición ajena)"
                )
                return Motor3ExitOutcome(False, False, reason="already_closed")
            count = min(requested_quantity(position), open_count)
            return await self._exit_locked(position.ticker, position.side, count)

    def _attributable_buys(self, s, ticker: str, side: str) -> list[Trade]:
        """BUYs 'filled' abiertos atribuibles para (ticker, side): los de entry_origin y — con
        include_motor1_orphans — las HUÉRFANAS de Motor 1. La orfandad se decide mirando AMBOS
        sides del arb (por eso la query de motor_1 va por ticker completo): un arb con las dos
        patas vivas es un hedge y queda fuera SIEMPRE. Ordenado FIFO por placed_at (mismo
        criterio de settle de siempre)."""
        from src.strategies.motor_3_clv.orphans import motor1_orphan_buys

        buys = [
            b
            for b in s.exec(
                select(Trade).where(
                    Trade.ticker == ticker,
                    Trade.side == side,
                    Trade.action == "buy",
                    Trade.status == "filled",
                    col(Trade.strategy).in_(self._entry_origin),
                )
            )
            if not b.closed_by_clv and b.count > 0
        ]
        if self._include_motor1_orphans:
            m1_all_sides = [
                b
                for b in s.exec(
                    select(Trade).where(
                        Trade.ticker == ticker,
                        Trade.action == "buy",
                        Trade.status == "filled",
                        Trade.strategy == "motor_1_arbitrage",
                    )
                )
                if not b.closed_by_clv and b.count > 0
            ]
            buys.extend(o for o in motor1_orphan_buys(m1_all_sides) if o.side == side)
        buys.sort(key=lambda b: b.placed_at)
        return buys

    def _open_attributable_count(self, ticker: str, side: str) -> int:
        """Contratos BUY 'filled' aún abiertos (no closed_by_clv) atribuibles para
        (ticker, side). Fail-safe: error de DB → 0 (ante la duda, NO vender)."""
        try:
            with get_session() as s:
                return sum(open_quantity(b) for b in self._attributable_buys(s, ticker, side))
        except Exception:
            logger.exception(f"motor3.exit.attributable_check_failed ticker={ticker}")
            return 0

    async def _exit_locked(self, ticker: str, side: str, count: int) -> Motor3ExitOutcome:
        if side not in ("yes", "no") or count <= 0:
            return Motor3ExitOutcome(False, False, reason="posición inválida")

        # Bid del lado que tenemos (a quién le vendemos). Sin bid → no hay con quién cerrar.
        try:
            ob = await self.client.get_orderbook(ticker)
        except Exception as exc:
            logger.warning(f"motor3.exit.orderbook_error ticker={ticker}: {exc}")
            return Motor3ExitOutcome(False, False, reason="orderbook_error")
        # rest_orderbook_sides: shape 2026-07-15 — sin él, book vacío → 'no_bid' perpetuo
        # y las posiciones quedaban invendibles (fail-closed, pero ciego).
        bid, _ = _top_bid(rest_orderbook_sides(ob)[side])
        if bid is None or not (1 <= bid <= 99):
            logger.info(
                f"motor3.exit.no_bid ticker={ticker} side={side} (sin liquidez para liquidar)"
            )
            return Motor3ExitOutcome(False, False, reason="no_bid")
        if bid < self._min_sell_bid_cents:
            # Piso de venta (auditoría 2026-07-07): a bid de polvo, fee+spread se comen
            # casi todo lo recuperable — mejor dejar que settlee (settle no paga fee).
            logger.info(
                f"motor3.exit.bid_below_floor ticker={ticker} side={side} bid={bid}c < "
                f"{self._min_sell_bid_cents}c (no se vende polvo; va a settle)"
            )
            return Motor3ExitOutcome(False, False, reason="bid_below_floor")

        # Vender al bid (cruza con el mejor bid). IOC: toma lo que haya, cancela el resto.
        coid = f"{uuid.uuid4()}-clvexit"
        # INTENT PRE-RED (fix auditoría 2026-07-01, patrón A.1 de los otros executors):
        # la fila SELL 'pending' se escribe ANTES de tocar la red. Si la respuesta se
        # pierde (ERROR_RED), queda el rastro para que _reconcile_pending_sells lo
        # resuelva por coid — sin esto, un fill no registrado terminaba en re-venta
        # fantasma (posición contraria) + PnL doble vía settlement. Nota: mientras esté
        # 'pending' cuenta en la exposición del RiskManager (su query no filtra por
        # action) — sobrestima temporal y conservador.
        if not self._persist_sell_intent(coid, ticker, side, count, bid):
            return Motor3ExitOutcome(False, False, reason="persist_intent_failed")
        try:
            resp = await self.client.place_order(
                ticker=ticker,
                side=side,
                action="sell",
                reduce_only=True,
                count=count,
                order_type="limit",
                yes_price=bid if side == "yes" else None,
                no_price=bid if side == "no" else None,
                client_order_id=coid,
                time_in_force="immediate_or_cancel",
            )
        except TradingDisabledError:
            # No debería pasar (sell no se bloquea); defensa por si la invariante cambiara.
            logger.warning(f"motor3.exit.trading_disabled ticker={ticker}")
            self._update_sell_intent(
                coid, critical=False, status="cancelled", notes_append="trading_disabled"
            )
            return Motor3ExitOutcome(False, False, reason="trading_disabled", client_order_id=coid)
        except KalshiClientError as exc:
            if exc.status_code in (408, 409):
                # La venta pudo entrar: mantener el intent impide otra venta del
                # mismo inventario antes de conciliarla, incluso tras reiniciar.
                reason = f"ambiguous_http_{exc.status_code}"
                self._update_sell_intent(coid, critical=False, notes_append=reason)
                logger.warning(f"motor3.exit.{reason} ticker={ticker} coid={coid}")
                return Motor3ExitOutcome(True, False, reason=reason, client_order_id=coid)
            # 4xx determinístico: la orden NO entró — sin posición, intent liberado.
            logger.warning(
                f"motor3.exit.client_error ticker={ticker} status={exc.status_code}: {exc}"
            )
            self._update_sell_intent(
                coid,
                critical=False,
                status="cancelled",
                notes_append=f"client_error_{exc.status_code}",
            )
            return Motor3ExitOutcome(
                True, False, reason=f"client_error_{exc.status_code}", client_order_id=coid
            )
        except Exception as exc:
            # ERROR_RED: el sell pudo llegar y LLENAR. El intent queda 'pending' — el
            # próximo exit_position lo reconcilia por coid contra get_orders antes de
            # intentar cualquier venta nueva (nunca re-vender a ciegas).
            logger.warning(f"motor3.exit.error_red ticker={ticker}: {exc}")
            try:
                from src.monitoring.health import BotState

                BotState.record_error(f"motor3.exit error_red ticker={ticker} coid={coid}")
            except Exception:  # pragma: no cover - visibilidad best-effort
                pass
            return Motor3ExitOutcome(True, False, reason="error_red", client_order_id=coid)

        order = resp.get("order", resp) if isinstance(resp, dict) else {}
        try:
            validate_pending_sell_response(coid, order)
        except Exception:
            # No atribuir ni siquiera cero a un ACK de identidad incompatible.
            # El guard ya conserva pending y pausa durable, sin aprender su OID.
            return Motor3ExitOutcome(
                True, False, reason="order_identity_unconfirmed", client_order_id=coid
            )
        fill_count = terminal_fill(order, count, placement=True)
        if fill_count is None:
            hold_uncertain_order(coid, order)
            return Motor3ExitOutcome(True, False, reason="fill_unreadable", client_order_id=coid)

        if fill_count > 0:
            # Fill REAL: intent→settled + cierre de las patas BUY. CRÍTICO: si la
            # persistencia falla, el sistema queda ciego a una venta real (re-venta
            # fantasma + PnL doble) → pausa preventiva persistente, como en los otros
            # executors. La venta ya ocurrió: el outcome la reporta igual.
            try:
                execution = await capture_terminal_sell_execution(
                    self.client, coid, order, placement=True
                )
                self._finalize_fill(
                    coid,
                    ticker,
                    side,
                    fill_count,
                    bid,
                    execution.order_id,
                    execution=execution,
                )
            except Exception:
                logger.critical(f"motor3.exit.persist_after_fill_failed coid={coid}")
                self._engage_preventive_pause(coid)
                return Motor3ExitOutcome(
                    True,
                    True,
                    filled_count=fill_count,
                    reason="exit_accounting_pending",
                    client_order_id=coid,
                )
            average_cents = execution.gross_usd * 100 / fill_count
            observed_price = int(average_cents) if average_cents.denominator == 1 else None
            logger.info(
                f"motor3.exit.filled ticker={ticker} side={side} sold={fill_count} "
                f"gross_usd={usd_text(execution.gross_usd)} fees_usd={usd_text(execution.fee_usd)}"
            )
            # Alerta Telegram de la venta REAL (deuda auditoría 2026-07-01: el operador se
            # enteraba solo por logs/digest). Best-effort: jamás rompe el exit.
            try:
                from src.monitoring.telegram_alerts import send_alert

                await send_alert(
                    f"💸 Exit {self._strategy}: vendidos {fill_count}c {side.upper()} "
                    f"de {ticker}; ingreso observado ${usd_text(execution.gross_usd)} "
                    f"y fee ${usd_text(execution.fee_usd)}"
                )
            except Exception:  # pragma: no cover - visibilidad best-effort
                logger.exception("motor3.exit.telegram_alert_failed")
            return Motor3ExitOutcome(
                True,
                True,
                filled_count=fill_count,
                sell_price_cents=observed_price,
                client_order_id=coid,
            )
        self._update_sell_intent(
            coid, critical=False, status="cancelled", notes_append="ioc_no_fill"
        )
        logger.info(f"motor3.exit.no_fill ticker={ticker} (IOC sin cruce al bid)")
        return Motor3ExitOutcome(True, False, reason="ioc_no_fill", client_order_id=coid)

    def _persist_sell_intent(
        self, coid: str, ticker: str, side: str, count: int, price: int
    ) -> bool:
        """Reserva inventario bajo lock SQLite ANTES de red, compartido entre motores.

        Revalida tras el await del libro: otra salida pudo reservar o cerrar las BUY.
        False obliga al caller a abortar sin vender; no se reutiliza inventario incierto.
        """
        try:
            with get_session() as s:
                s.exec(text("BEGIN IMMEDIATE"))
                pending = s.exec(
                    select(Trade.id)
                    .where(
                        Trade.ticker == ticker,
                        Trade.side == side,
                        Trade.action == "sell",
                        Trade.status == "pending",
                    )
                    .limit(1)
                ).first()
                if pending is not None:
                    return False
                available = sum(open_quantity(b) for b in self._attributable_buys(s, ticker, side))
                if count <= 0 or count > available:
                    return False
                s.add(
                    Trade(
                        client_order_id=coid,
                        ticker=ticker,
                        side=side,
                        action="sell",
                        **quantity_fields(count),
                        price_cents=price,
                        strategy=self._strategy,
                        status="pending",
                        notes="clv_exit_intent",
                    )
                )
                s.commit()
            return True
        except Exception:
            logger.critical(
                f"motor3.exit.persist_intent_failed coid={coid} → ABORT (sin rastro no se vende)"
            )
            return False

    def _update_sell_intent(
        self, coid: str, *, critical: bool, notes_append: str | None = None, **fields: object
    ) -> None:
        """Actualiza la fila SELL del intent. critical=True (fill real): la excepción ESCALA
        al caller (que engancha la pausa preventiva); critical=False: best-effort."""
        try:
            with get_session() as s:
                t = s.exec(select(Trade).where(Trade.client_order_id == coid)).first()
                if t is None:
                    logger.warning(f"motor3.exit.update_intent.missing coid={coid}")
                    return
                for k, v in fields.items():
                    setattr(t, k, v)
                if notes_append:
                    t.notes = f"{t.notes or ''} {notes_append}".strip()[:500]
                s.add(t)
                s.commit()
        except Exception:
            logger.exception(f"motor3.exit.update_intent_failed coid={coid} critical={critical}")
            if critical:
                raise

    def _finalize_fill(
        self,
        coid: str,
        ticker: str,
        side: str,
        fill_count: int,
        price: int,
        order_id: str | None,
        *,
        execution: ExitFillSummary | None = None,
    ) -> None:
        """Registra un fill REAL de venta: intent→'settled' + cierre de las patas BUY.

        status='settled' en el SELL a propósito: una salida YA realizada no cuenta como
        exposición (la query del RiskManager es pending/filled) ni dobla el PnL
        (pnl_cents queda None; el PnL realizado vive en las BUY que settlea
        `_settle_originals`). CRÍTICO: cualquier excepción acá escala al caller, que
        engancha la pausa preventiva — un fill real sin registrar deja al sistema ciego.
        """
        now = _naive_utc_now()
        with get_session() as s:
            s.exec(text("BEGIN IMMEDIATE"))
            sell = s.exec(select(Trade).where(Trade.client_order_id == coid)).first()
            if sell is None:
                raise ValueError("intent de salida ausente")
            if (
                sell.ticker != ticker
                or sell.side != side
                or sell.action != "sell"
                or sell.price_cents != price
            ):
                raise ValueError("atribución o precio de salida contradice intent persistido")
            if sell.status == "settled":
                if fill_count != open_quantity(sell) or (
                    order_id is not None
                    and sell.kalshi_order_id is not None
                    and order_id != sell.kalshi_order_id
                ):
                    raise ValueError("replay de salida contradice el ledger")
                if execution is not None:
                    apply_exit_execution(sell, execution, fill_count, order_id)
                validated_exit_fee(sell, fill_count)
                return  # Reconciliación repetida después de reiniciar: no volver a cerrar BUYs.
            if fill_count > requested_quantity(sell):
                raise ValueError("fill de salida excede intent")
            observed = confirmed_fill(sell)
            if observed is not None and fill_count < observed:
                raise ValueError("fill de salida regresivo")
            if execution is not None:
                apply_exit_execution(sell, execution, fill_count, order_id)
            exit_fee, observed_fee = validated_exit_fee(sell, fill_count)
            self._settle_originals(
                ticker,
                side,
                price,
                fill_count,
                session=s,
                observed_exit_fee_cents=exit_fee if observed_fee else None,
                observed_exit_gross_cents=execution.gross_usd * 100
                if execution is not None
                else None,
            )
            resize_filled(sell, fill_count)
            sell.status = "settled"
            average = (
                execution.gross_usd * 100 / fill_count if execution is not None else Fraction(price)
            )
            sell.fill_price_cents = int(average) if average.denominator == 1 else None
            sell.fees_cents = ceil(exit_fee)
            sell.kalshi_order_id = order_id
            sell.filled_at = now
            sell.settled_at = now
            if execution is not None:
                sell.notes = exit_execution_note(
                    sell.notes, "clv_exit_filled", entry_observed=False
                )
            else:
                sell.notes = estimated_exit_note(
                    sell.notes, "clv_exit_filled", observed_fee=observed_fee
                )
            s.add(sell)
            s.commit()  # SELL y splits BUY juntos: ningún crash deja el ledger a medias.

    async def _reconcile_pending_sells(self, ticker: str, side: str) -> bool:
        """Resuelve intents SELL 'pending' (ERROR_RED previo) contra get_orders, por coid.

        Devuelve True si el camino quedó limpio para una venta nueva. False si algún
        intent sigue irresoluble (la orden no aparece / la API falla / la persistencia del
        fill reconciliado falla) → el caller NO vende este tick.

        - executed / fill_count>0 → _finalize_fill con el fill real (sin re-vender).
        - canceled / fill 0 → intent 'cancelled' (IOC nunca descansa: no hay resting).
        """
        try:
            with get_session() as s:
                pending = list(
                    s.exec(
                        select(Trade).where(
                            Trade.ticker == ticker,
                            Trade.side == side,
                            Trade.action == "sell",
                            Trade.status == "pending",
                        )
                    )
                )
        except Exception:
            logger.exception(f"motor3.exit.reconcile_load_failed ticker={ticker}")
            return False
        if not pending:
            return True
        if any(t.strategy != self._strategy for t in pending):
            # Los motores comparten inventario: una venta incierta ajena también
            # bloquea. Su dueño conserva la conciliación y la atribución contable.
            logger.warning(f"motor3.exit.shared_pending_sell ticker={ticker} side={side}")
            return False
        try:
            resp = await self.client.get_orders(ticker=ticker)
        except Exception as exc:
            logger.warning(f"motor3.exit.reconcile_get_orders_failed ticker={ticker}: {exc}")
            return False
        orders = [
            o
            for o in (resp.get("orders", []) if isinstance(resp, dict) else [])
            if isinstance(o, dict)
        ]
        clear = True
        for t in pending:
            matches = [o for o in orders if o.get("client_order_id") == t.client_order_id]
            if len(matches) != 1:
                logger.warning(
                    f"motor3.exit.reconcile_unresolved coid={t.client_order_id} "
                    "(ausente o duplicado en get_orders — sin veredicto, no se vende este tick)"
                )
                if matches:
                    self._engage_preventive_pause(t.client_order_id)
                clear = False
                continue
            o = matches[0]
            try:
                validate_sell_order_identity(t, o)
            except ValueError:
                self._engage_preventive_pause(t.client_order_id)
                clear = False
                continue
            status = str(o.get("status", ""))
            fill = terminal_fill(o, requested_quantity(t))
            observed = confirmed_fill(t)
            if fill is not None and observed is not None and fill < observed:
                clear = False
                self._engage_preventive_pause(t.client_order_id)
                continue
            if fill is None:
                hold_uncertain_order(t.client_order_id, o)
                clear = False
                continue
            if fill > 0:
                try:
                    execution = await capture_terminal_sell_execution(
                        self.client, t.client_order_id, o
                    )
                    self._finalize_fill(
                        t.client_order_id,
                        ticker,
                        side,
                        fill,
                        t.price_cents,
                        execution.order_id,
                        execution=execution,
                    )
                    logger.info(
                        f"motor3.exit.reconciled_filled coid={t.client_order_id} fill={fill} "
                        "(el sell del error_red SÍ llenó — no se re-vende)"
                    )
                except Exception:
                    logger.critical(
                        f"motor3.exit.reconcile_persist_failed coid={t.client_order_id}"
                    )
                    self._engage_preventive_pause(t.client_order_id)
                    clear = False
            else:
                self._update_sell_intent(
                    t.client_order_id,
                    critical=False,
                    status="cancelled",
                    filled_count=0,
                    filled_count_fp="0.00",
                    notes_append=f"reconciled_{status or 'unknown'}",
                )
        return clear

    def _engage_preventive_pause(self, coid: str) -> None:
        """Salida sin conciliar: impedir re-venta y PnL doble con pausa persistente.

        También cubre identidad incompatible y cero-fill; no presume una venta.
        """
        msg = f"motor3.exit: salida sin conciliar (coid={coid}) — requiere revisión"
        logger.critical(f"motor3.exit.preventive_pause {msg}")
        try:
            from src.monitoring.health import BotState

            BotState.is_paused = True
            BotState.pause_reason = msg
            engage_kill_switch(msg)
        except Exception:  # pragma: no cover - último recurso
            logger.exception("motor3.exit.preventive_pause_failed")

    def _settle_originals(
        self,
        ticker: str,
        side: str,
        exit_price: int,
        filled_count: int | Fraction,
        *,
        session=None,
        observed_exit_fee_cents: Fraction | None = None,
        observed_exit_gross_cents: Fraction | None = None,
    ) -> None:
        """
        Cierra las patas BUY originales (Motor 2/REST) que este exit liquidó (FIFO por
        antigüedad), marcándolas closed_by_clv + settled con el PnL REALIZADO al precio de
        salida → el SettlementPoller las saltea (no doble-cuenta por resolución de mercado).

        Parcial: si el fill cubre solo parte de una pata, esa pata se PARTE — el remanente
        sigue 'filled' (abierto, el poller lo re-sincroniza y se reintenta) y se crea una
        hija 'settled' por la porción cerrada.

        CRÍTICO (fix auditoría 2026-07-01): un fallo acá ESCALA al caller (antes era
        best-effort → BUYs 'filled' para siempre = re-venta fantasma + settlement doble).
        """
        now = _naive_utc_now()
        try:
            with get_session() if session is None else nullcontext(session) as s:
                if session is None:
                    s.exec(text("BEGIN IMMEDIATE"))
                # Mismo criterio que _open_attributable_count (entry_origin + huérfanas de
                # Motor 1 si el flag está on) → el settle cierra EXACTAMENTE las patas que
                # habilitaron la venta, FIFO. Un hedge de Motor 1 jamás aparece acá.
                buys = self._attributable_buys(s, ticker, side)
                if sum(open_quantity(b) for b in buys) < filled_count:
                    raise ValueError("fill de salida excede cantidad atribuible")
                remaining = filled_count
                # Fee del EXIT asignada por tramo ACUMULATIVO (deuda auditoría 2026-07-01):
                # sumar ceil(fee(closed)) por pata sobre-cobraba hasta (n_patas−1)¢ y no
                # cuadraba con la fila SELL. cumfee(k)−cumfee(k_prev) suma EXACTO el fee
                # calculada total (estimada cuando no existe fee FP observada).
                closed_so_far = 0
                for b in buys:
                    if remaining <= 0:
                        break
                    if b.closed_by_clv:
                        continue
                    buy_price = b.fill_price_cents or b.price_cents
                    quantity = open_quantity(b)
                    closed = min(quantity, remaining)
                    if observed_exit_fee_cents is None:
                        exit_fee_tramo = kalshi_fee_cents(
                            closed_so_far + closed, exit_price
                        ) - kalshi_fee_cents(closed_so_far, exit_price)
                    else:
                        # Prorrateo de la fee TOTAL observada; microUSD acumulados
                        # conservan el residuo para el último tramo del FIFO.
                        units = observed_exit_fee_cents * 10_000
                        exit_fee_tramo = Fraction(
                            floor(units * (closed_so_far + closed) / filled_count)
                            - floor(units * closed_so_far / filled_count),
                            10_000,
                        )
                    allocation = allocate_entry(b, closed)
                    gross = closed * exit_price
                    if observed_exit_gross_cents is not None:
                        units = observed_exit_gross_cents * 10_000
                        gross = Fraction(
                            floor(units * (closed_so_far + closed) / filled_count)
                            - floor(units * closed_so_far / filled_count),
                            10_000,
                        )
                    pnl = gross - allocation.gross_cents - exit_fee_tramo - allocation.fee_cents
                    if closed == quantity:
                        b.status = "settled"
                        b.closed_by_clv = True
                        b.settled_at = now
                        b.pnl_usd_fp = usd_text(pnl / 100)
                        b.pnl_cents = floor(pnl)
                        for field, value in allocation.closed_fields.items():
                            setattr(b, field, value)
                        if observed_exit_gross_cents is not None:
                            b.notes = exit_execution_note(
                                b.notes,
                                "closed_by_clv",
                                entry_observed=b.fill_cost_usd_fp is not None
                                and b.fees_usd_fp is not None,
                            )
                        else:
                            b.notes = estimated_exit_note(
                                b.notes,
                                "closed_by_clv",
                                observed_fee=observed_exit_fee_cents is not None,
                            )
                        s.add(b)
                    else:
                        # Partial: reduce el original (remanente sigue abierto) + hija settled.
                        resize_filled(b, quantity - closed)
                        for field, value in allocation.remaining_fields.items():
                            setattr(b, field, value)
                        s.add(b)
                        s.add(
                            Trade(
                                client_order_id=f"{b.client_order_id}-clv{uuid.uuid4().hex[:8]}",
                                commitment_origin_id=b.commitment_origin_id,
                                ticker=ticker,
                                side=side,
                                action="buy",
                                **filled_fields(closed),
                                price_cents=b.price_cents,
                                fill_price_cents=buy_price,
                                strategy=b.strategy,
                                status="settled",
                                closed_by_clv=True,
                                settled_at=now,
                                pnl_cents=floor(pnl),
                                pnl_usd_fp=usd_text(pnl / 100),
                                **allocation.closed_fields,
                                notes=exit_execution_note(
                                    None,
                                    "closed_by_clv split",
                                    entry_observed=b.fill_cost_usd_fp is not None
                                    and b.fees_usd_fp is not None,
                                )
                                if observed_exit_gross_cents is not None
                                else estimated_exit_note(
                                    None,
                                    "closed_by_clv split",
                                    observed_fee=observed_exit_fee_cents is not None,
                                ),
                            )
                        )
                    remaining -= closed
                    closed_so_far += closed
                if session is None:
                    s.commit()
        except Exception:
            logger.exception(f"motor3.exit.settle_originals_failed ticker={ticker}")
            raise
