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

from loguru import logger
from sqlmodel import col, select

from src.clients.kalshi_rest import KalshiClientError, KalshiRestClient, TradingDisabledError
from src.math.fees import kalshi_fee_cents
from src.storage.models import PortfolioPosition, Trade, _naive_utc_now, get_session
from src.strategies.data_capture import _top_bid
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

    def __init__(self, client: KalshiRestClient, *, strategy: str = STRATEGY) -> None:
        self.client = client
        # Estrategia con la que se audita la fila SELL del exit. Default motor_3_clv; Motor 2
        # reusa este executor pasando "motor_2_consensus" para que el audit quede atribuido al
        # motor que disparó el cierre (las patas BUY que cierra _settle_originals ya se marcan
        # closed_by_clv por su propia strategy, sin importar este tag).
        self._strategy = strategy
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, ticker: str) -> asyncio.Lock:
        return self._locks.setdefault(ticker, asyncio.Lock())

    async def exit_position(self, position: PortfolioPosition) -> Motor3ExitOutcome:
        """
        Vende la posición del lado abierto. Si OTRA ejecución ya está liquidando este
        ticker (lock tomado), se DESCARTA — no encolar (estaría stale) ni doble-vender.
        """
        lock = self._lock_for(position.ticker)
        if lock.locked():
            logger.info(f"motor3.exit.skip_busy ticker={position.ticker}")
            return Motor3ExitOutcome(False, False, reason="busy")
        async with lock:
            return await self._exit_locked(position.ticker, position.side, position.count)

    async def _exit_locked(self, ticker: str, side: str, count: int) -> Motor3ExitOutcome:
        if side not in ("yes", "no") or count <= 0:
            return Motor3ExitOutcome(False, False, reason="posición inválida")

        # Bid del lado que tenemos (a quién le vendemos). Sin bid → no hay con quién cerrar.
        try:
            ob = await self.client.get_orderbook(ticker)
        except Exception as exc:
            logger.warning(f"motor3.exit.orderbook_error ticker={ticker}: {exc}")
            return Motor3ExitOutcome(False, False, reason="orderbook_error")
        book = ob.get("orderbook", ob) if isinstance(ob, dict) else {}
        bid, _ = _top_bid(book.get(side) or [])
        if bid is None or not (1 <= bid <= 99):
            logger.info(
                f"motor3.exit.no_bid ticker={ticker} side={side} (sin liquidez para liquidar)"
            )
            return Motor3ExitOutcome(False, False, reason="no_bid")

        # Vender al bid (cruza con el mejor bid). IOC: toma lo que haya, cancela el resto.
        coid = f"{uuid.uuid4()}-clvexit"
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
            return Motor3ExitOutcome(False, False, reason="trading_disabled", client_order_id=coid)
        except KalshiClientError as exc:
            logger.warning(
                f"motor3.exit.client_error ticker={ticker} status={exc.status_code}: {exc}"
            )
            return Motor3ExitOutcome(
                True, False, reason=f"client_error_{exc.status_code}", client_order_id=coid
            )
        except Exception as exc:
            # ERROR_RED: el sell pudo llegar o no. La fila se registra; el poller re-sincroniza
            # la posición real en el próximo ciclo (si quedó abierta, se reintenta).
            logger.warning(f"motor3.exit.error_red ticker={ticker}: {exc}")
            return Motor3ExitOutcome(True, False, reason="error_red", client_order_id=coid)

        order = resp.get("order", resp) if isinstance(resp, dict) else {}
        order_id = str(order.get("order_id", "")) or None
        fill_count = _as_int(order.get("fill_count", order.get("fill_count_fp"))) or 0

        if fill_count > 0:
            # Audit del SELL (status=settled → realizado, NO cuenta como exposición) +
            # FASE 2: cierre de la pata original para que el SettlementPoller no la liquide
            # de nuevo por resolución (anti doble-conteo de PnL).
            self._record_exit(coid, ticker, side, fill_count, bid, order_id)
            self._settle_originals(ticker, side, bid, fill_count)
            logger.info(f"motor3.exit.filled ticker={ticker} side={side} sold={fill_count}@{bid}c")
            return Motor3ExitOutcome(
                True, True, filled_count=fill_count, sell_price_cents=bid, client_order_id=coid
            )
        logger.info(f"motor3.exit.no_fill ticker={ticker} (IOC sin cruce al bid)")
        return Motor3ExitOutcome(True, False, reason="ioc_no_fill", client_order_id=coid)

    def _record_exit(
        self, coid: str, ticker: str, side: str, fill_count: int, price: int, order_id: str | None
    ) -> None:
        """
        Registra el SELL del exit como fila Trade de AUDITORÍA. Best-effort.

        status='settled' a propósito: un SELL llenado es una salida YA realizada — así NO
        cuenta como exposición (la query del RiskManager es pending/filled) ni dobla el PnL
        (pnl_cents queda en None aquí; el PnL realizado vive en la pata BUY que settlea
        `_settle_originals`).
        """
        now = _naive_utc_now()
        try:
            with get_session() as s:
                s.add(
                    Trade(
                        client_order_id=coid,
                        ticker=ticker,
                        side=side,
                        action="sell",
                        count=fill_count,
                        price_cents=price,
                        strategy=self._strategy,
                        status="settled",
                        kalshi_order_id=order_id,
                        fill_price_cents=price,
                        fees_cents=kalshi_fee_cents(fill_count, price),
                        filled_at=now,
                        settled_at=now,
                        notes="clv_exit",
                    )
                )
                s.commit()
        except Exception:
            logger.exception(f"motor3.exit.record_failed coid={coid}")

    def _settle_originals(self, ticker: str, side: str, exit_price: int, filled_count: int) -> None:
        """
        Cierra las patas BUY originales (Motor 2/REST) que este exit liquidó (FIFO por
        antigüedad), marcándolas closed_by_clv + settled con el PnL REALIZADO al precio de
        salida → el SettlementPoller las saltea (no doble-cuenta por resolución de mercado).

        Parcial: si el fill cubre solo parte de una pata, esa pata se PARTE — el remanente
        sigue 'filled' (abierto, el poller lo re-sincroniza y se reintenta) y se crea una
        hija 'settled' por la porción cerrada. Best-effort: un fallo se loguea, no rompe.
        """
        origin = ("motor_2_consensus", "motor_rest_arb")
        now = _naive_utc_now()
        try:
            with get_session() as s:
                buys = list(
                    s.exec(
                        select(Trade)
                        .where(
                            Trade.ticker == ticker,
                            Trade.side == side,
                            Trade.action == "buy",
                            Trade.status == "filled",
                            col(Trade.strategy).in_(origin),
                        )
                        .order_by(col(Trade.placed_at))
                    )
                )
                remaining = filled_count
                for b in buys:
                    if remaining <= 0:
                        break
                    if b.closed_by_clv:
                        continue
                    buy_price = b.fill_price_cents or b.price_cents
                    closed = min(b.count, remaining)
                    # PnL realizado del tramo cerrado: (salida − entrada) − fees de ambos lados.
                    pnl = (
                        closed * (exit_price - buy_price)
                        - kalshi_fee_cents(closed, exit_price)
                        - kalshi_fee_cents(closed, buy_price)
                    )
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
                                notes="closed_by_clv split",
                            )
                        )
                    remaining -= closed
                s.commit()
        except Exception:
            logger.exception(f"motor3.exit.settle_originals_failed ticker={ticker}")
