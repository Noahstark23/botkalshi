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
from dataclasses import dataclass
from typing import ClassVar

from loguru import logger
from sqlmodel import col, select

from src.clients.kalshi_rest import KalshiClientError, KalshiRestClient, TradingDisabledError
from src.math.fees import kalshi_fee_cents
from src.storage.models import (
    PortfolioPosition,
    Trade,
    _naive_utc_now,
    engage_kill_switch,
    get_session,
)
from src.strategies.data_capture import _top_bid, rest_orderbook_sides
from src.strategies.motor_3_clv.poller import _as_int

STRATEGY = "motor_3_clv"


@dataclass
class Motor3ExitOutcome:
    """Desenlace de intentar liquidar una posición."""

    placed: bool  # llegó a tocar la red (place_order invocado)
    filled: bool  # llenó algo (posición reducida/cerrada)
    filled_count: int = 0
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
            count = min(position.count, open_count)
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
                return sum(b.count for b in self._attributable_buys(s, ticker, side))
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
        order_id = str(order.get("order_id", "")) or None
        fill_count = _as_int(order.get("fill_count", order.get("fill_count_fp"))) or 0

        if fill_count > 0:
            # Fill REAL: intent→settled + cierre de las patas BUY. CRÍTICO: si la
            # persistencia falla, el sistema queda ciego a una venta real (re-venta
            # fantasma + PnL doble) → pausa preventiva persistente, como en los otros
            # executors. La venta ya ocurrió: el outcome la reporta igual.
            try:
                self._finalize_fill(coid, ticker, side, fill_count, bid, order_id)
            except Exception:
                logger.critical(f"motor3.exit.persist_after_fill_failed coid={coid}")
                self._engage_preventive_pause(coid)
            logger.info(f"motor3.exit.filled ticker={ticker} side={side} sold={fill_count}@{bid}c")
            # Alerta Telegram de la venta REAL (deuda auditoría 2026-07-01: el operador se
            # enteraba solo por logs/digest). Best-effort: jamás rompe el exit.
            try:
                from src.monitoring.telegram_alerts import send_alert

                await send_alert(
                    f"💸 Exit {self._strategy}: vendidos {fill_count}c {side.upper()} "
                    f"de {ticker} @ {bid}c"
                )
            except Exception:  # pragma: no cover - visibilidad best-effort
                logger.exception("motor3.exit.telegram_alert_failed")
            return Motor3ExitOutcome(
                True, True, filled_count=fill_count, sell_price_cents=bid, client_order_id=coid
            )
        self._update_sell_intent(
            coid, critical=False, status="cancelled", notes_append="ioc_no_fill"
        )
        logger.info(f"motor3.exit.no_fill ticker={ticker} (IOC sin cruce al bid)")
        return Motor3ExitOutcome(True, False, reason="ioc_no_fill", client_order_id=coid)

    def _persist_sell_intent(
        self, coid: str, ticker: str, side: str, count: int, price: int
    ) -> bool:
        """Escribe la fila SELL 'pending' ANTES de tocar la red (patrón A.1). False si falla
        (→ el caller ABORTA sin vender: sin rastro no hay reconciliación posible)."""
        try:
            with get_session() as s:
                s.add(
                    Trade(
                        client_order_id=coid,
                        ticker=ticker,
                        side=side,
                        action="sell",
                        count=count,
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
        self, coid: str, ticker: str, side: str, fill_count: int, price: int, order_id: str | None
    ) -> None:
        """Registra un fill REAL de venta: intent→'settled' + cierre de las patas BUY.

        status='settled' en el SELL a propósito: una salida YA realizada no cuenta como
        exposición (la query del RiskManager es pending/filled) ni dobla el PnL
        (pnl_cents queda None; el PnL realizado vive en las BUY que settlea
        `_settle_originals`). CRÍTICO: cualquier excepción acá escala al caller, que
        engancha la pausa preventiva — un fill real sin registrar deja al sistema ciego.
        """
        now = _naive_utc_now()
        self._update_sell_intent(
            coid,
            critical=True,
            status="settled",
            count=fill_count,
            fill_price_cents=price,
            fees_cents=kalshi_fee_cents(fill_count, price),
            kalshi_order_id=order_id,
            filled_at=now,
            settled_at=now,
            notes_append="clv_exit_filled",
        )
        self._settle_originals(ticker, side, price, fill_count)

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
                            Trade.strategy == self._strategy,
                        )
                    )
                )
        except Exception:
            logger.exception(f"motor3.exit.reconcile_load_failed ticker={ticker}")
            return False
        if not pending:
            return True
        try:
            resp = await self.client.get_orders(ticker=ticker)
        except Exception as exc:
            logger.warning(f"motor3.exit.reconcile_get_orders_failed ticker={ticker}: {exc}")
            return False
        orders = {
            str(o.get("client_order_id", "")): o
            for o in (resp.get("orders", []) if isinstance(resp, dict) else [])
            if isinstance(o, dict)
        }
        clear = True
        for t in pending:
            o = orders.get(t.client_order_id)
            if o is None:
                logger.warning(
                    f"motor3.exit.reconcile_unresolved coid={t.client_order_id} "
                    "(no aparece en get_orders — sin veredicto, no se vende este tick)"
                )
                clear = False
                continue
            status = str(o.get("status", ""))
            fill = _as_int(o.get("fill_count", o.get("fill_count_fp")))
            if fill is None and status == "executed":
                fill = t.count  # executed sin fill_count legible → IOC lleno completo
            fill = fill or 0
            if fill > 0:
                try:
                    self._finalize_fill(
                        t.client_order_id,
                        ticker,
                        side,
                        fill,
                        t.price_cents,
                        str(o.get("order_id", "")) or None,
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
                    notes_append=f"reconciled_{status or 'unknown'}",
                )
        return clear

    def _engage_preventive_pause(self, coid: str) -> None:
        """Fill real de venta que no se pudo registrar → kill-switch persistente (el sistema
        quedaría ciego: re-venta fantasma + PnL doble). Mismo patrón que los otros executors."""
        msg = f"motor3.exit: persist post-fill falló (coid={coid}) — venta real sin registrar"
        logger.critical(f"motor3.exit.preventive_pause {msg}")
        try:
            from src.monitoring.health import BotState

            BotState.is_paused = True
            BotState.pause_reason = msg
            engage_kill_switch(msg)
        except Exception:  # pragma: no cover - último recurso
            logger.exception("motor3.exit.preventive_pause_failed")

    def _settle_originals(self, ticker: str, side: str, exit_price: int, filled_count: int) -> None:
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
            with get_session() as s:
                # Mismo criterio que _open_attributable_count (entry_origin + huérfanas de
                # Motor 1 si el flag está on) → el settle cierra EXACTAMENTE las patas que
                # habilitaron la venta, FIFO. Un hedge de Motor 1 jamás aparece acá.
                buys = self._attributable_buys(s, ticker, side)
                remaining = filled_count
                # Fee del EXIT asignada por tramo ACUMULATIVO (deuda auditoría 2026-07-01):
                # sumar ceil(fee(closed)) por pata sobre-cobraba hasta (n_patas−1)¢ y no
                # cuadraba con la fila SELL. cumfee(k)−cumfee(k_prev) suma EXACTO el fee
                # real del fill total.
                closed_so_far = 0
                for b in buys:
                    if remaining <= 0:
                        break
                    if b.closed_by_clv:
                        continue
                    buy_price = b.fill_price_cents or b.price_cents
                    closed = min(b.count, remaining)
                    exit_fee_tramo = kalshi_fee_cents(
                        closed_so_far + closed, exit_price
                    ) - kalshi_fee_cents(closed_so_far, exit_price)
                    entry_fee_tramo = kalshi_fee_cents(closed, buy_price)
                    # PnL realizado del tramo cerrado: (salida − entrada) − fees de ambos lados.
                    pnl = closed * (exit_price - buy_price) - exit_fee_tramo - entry_fee_tramo
                    if closed == b.count:
                        b.status = "settled"
                        b.closed_by_clv = True
                        b.settled_at = now
                        b.pnl_cents = pnl
                        b.notes = f"{b.notes or ''} closed_by_clv".strip()[:500]
                        s.add(b)
                    else:
                        # Partial: reduce el original (remanente sigue abierto) + hija settled.
                        b.count = b.count - closed
                        if b.fees_cents is not None:
                            # Prorratear las fees de ENTRADA al remanente (deuda auditoría:
                            # dejar el fee del count original hacía que _leg_pnl_cents las
                            # restara DOS veces cuando el remanente settlea por resolución).
                            b.fees_cents = max(0, b.fees_cents - entry_fee_tramo)
                        s.add(b)
                        s.add(
                            Trade(
                                client_order_id=f"{b.client_order_id}-clv{uuid.uuid4().hex[:8]}",
                                ticker=ticker,
                                side=side,
                                action="buy",
                                count=closed,
                                price_cents=b.price_cents,
                                fill_price_cents=buy_price,
                                strategy=b.strategy,
                                status="settled",
                                closed_by_clv=True,
                                settled_at=now,
                                pnl_cents=pnl,
                                fees_cents=entry_fee_tramo,
                                notes="closed_by_clv split",
                            )
                        )
                    remaining -= closed
                    closed_so_far += closed
                s.commit()
        except Exception:
            logger.exception(f"motor3.exit.settle_originals_failed ticker={ticker}")
            raise
