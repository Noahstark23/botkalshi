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

    def __init__(self, client: KalshiRestClient) -> None:
        self.client = client
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
        filled = fill_count > 0
        self._record_exit(coid, ticker, side, count, bid, fill_count, order_id, filled)

        if filled:
            logger.info(f"motor3.exit.filled ticker={ticker} side={side} sold={fill_count}@{bid}c")
            return Motor3ExitOutcome(
                True, True, filled_count=fill_count, sell_price_cents=bid, client_order_id=coid
            )
        logger.info(f"motor3.exit.no_fill ticker={ticker} (IOC sin cruce al bid)")
        return Motor3ExitOutcome(True, False, reason="ioc_no_fill", client_order_id=coid)

    def _record_exit(
        self,
        coid: str,
        ticker: str,
        side: str,
        count: int,
        price: int,
        fill_count: int,
        order_id: str | None,
        filled: bool,
    ) -> None:
        """Registra el SELL del exit como fila Trade (auditoría). Best-effort."""
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
                        strategy=STRATEGY,
                        status="filled" if filled else "cancelled",
                        kalshi_order_id=order_id,
                        fill_price_cents=price if filled else None,
                        fees_cents=kalshi_fee_cents(fill_count, price) if filled else None,
                        filled_at=_naive_utc_now() if filled else None,
                        notes="clv_exit",
                    )
                )
                s.commit()
        except Exception:
            logger.exception(f"motor3.exit.record_failed coid={coid}")
