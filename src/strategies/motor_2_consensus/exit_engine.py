"""
Motor2ExitEngine — cierre de posiciones de Motor 2 (take-profit / trailing).

PROBLEMA QUE RESUELVE: el Motor2Executor SOLO abre posiciones (compra UN lado direccional);
nunca cierra. El histórico (1.613 trades, closed_by_clv=0 → 100% ride-to-settlement) sangró
por asimetría destructiva: avg_loss ≫ avg_win. Este loop agrega la pata que faltaba — cerrar
anticipadamente cuando el bid del lado abierto ya "casi ganó", asegurando antes de la remontada.

DISEÑO (espejo de Motor3Engine, REUSANDO sus helpers puros — sin duplicar lógica):
  - take_profit_due / trailing_stop_due / next_peak_bid (de motor_3_clv) deciden la salida.
  - Motor3ExitExecutor (de motor_3_clv) ejecuta el SELL IOC y, vía _settle_originals, cierra
    las patas BUY de motor_2_consensus marcándolas closed_by_clv=1 + pnl_cents realizado.

DIFERENCIA CLAVE con Motor 3: Motor 2 NO usa PortfolioPosition (esa la sincroniza el poller de
Motor 3 desde Kalshi). Las posiciones de Motor 2 son filas Trade (strategy=motor_2_consensus,
action=buy, status=filled, closed_by_clv=False). Acá se agregan por (ticker, side) en un
Motor2Position liviano — los helpers solo usan .count, así que encajan sin tocar PortfolioPosition.

Gating (dos capas, idéntico a Motor 3):
  - MOTOR_2_SPORTSBOOK_ENABLED (runner): si el engine CORRE.
  - TRADING_ENABLED && MOTOR_2_EXECUTION_ENABLED: si construye el executor (CAPA A). En shadow →
    executor None → detecta y loguea [MOTOR 2 TP/TRAIL SHADOW] con net de fees, pero NUNCA vende.

NOTA YES+NO mismo market: Motor 2 a veces abre YES y NO del MISMO partido (entradas direccionales
independientes, no un hedge — el RiskManager las cuenta enteras). Acá se tratan como DOS posiciones
separadas, cada una con su propio bid y su propio take-profit. Es lo correcto para el exit; que la
ENTRADA compre ambos lados a costo >100¢ (pérdida estructural) es un problema del detector/executor
de entrada, fuera del alcance de este loop de salida.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

from loguru import logger
from sqlmodel import col, select

from src.clients.kalshi_rest import KalshiRestClient
from src.math.fees import kalshi_fee_cents
from src.monitoring.health import BotState
from src.storage.models import Trade, get_session
from src.strategies.data_capture import _top_bid
from src.strategies.motor_3_clv.executor import Motor3ExitExecutor
from src.strategies.motor_3_clv.take_profit import DEFAULT_TAKE_PROFIT_CENTS, take_profit_due
from src.strategies.motor_3_clv.trailing_stop import (
    DEFAULT_TRAILING_DROP_CENTS,
    next_peak_bid,
    trailing_stop_due,
)

STRATEGY = "motor_2_consensus"


@dataclass
class Motor2Position:
    """Posición abierta de Motor 2 agregada por (ticker, side) desde las filas BUY filled.

    `count` y los helpers son lo único que mira take_profit_due/trailing_stop_due (encaja sin
    PortfolioPosition). `entry_cents` es el promedio ponderado (display + baseline del peak).
    `legs` guarda (count, entry) por pata para el PnL neto EXACTO (FIFO por-pata, igual que
    _settle_originals → el net del shadow coincide con el realizado al cerrar)."""

    ticker: str
    side: str
    count: int
    entry_cents: int
    legs: list[tuple[int, int]]


class Motor2ExitEngine:
    """Bucle de cierre de Motor 2: agrega posiciones abiertas → detecta take-profit/trailing →
    (liquida si execution on). Supervisor explícito por tick (Lección 7: nunca tragar y morir)."""

    LOOP_INTERVAL_SEC = 60.0

    def __init__(
        self,
        *,
        trading_enabled: bool = False,
        take_profit_enabled: bool = False,
        tp_threshold: int = DEFAULT_TAKE_PROFIT_CENTS,
        trailing_enabled: bool = False,
        trailing_drop: int = DEFAULT_TRAILING_DROP_CENTS,
    ) -> None:
        self._trading_enabled = trading_enabled
        self._take_profit_enabled = take_profit_enabled
        self._tp_threshold = tp_threshold
        self._trailing_enabled = trailing_enabled
        self._trailing_drop = trailing_drop
        self._executor: Motor3ExitExecutor | None = None
        self._client: KalshiRestClient | None = None
        # Peak del bid por (ticker, side) para el trailing. EN MEMORIA (Motor 2 no tiene fila
        # PortfolioPosition donde persistirlo) → se resetea en cada restart. Aceptable: el
        # trailing está OFF por default y es shadow-first; el take-profit (absoluto) no usa peak.
        self._peaks: dict[tuple[str, str], int] = {}

    async def run(self, stop_event: asyncio.Event) -> None:
        logger.info(
            f"motor2.exit_engine started (trading_enabled={self._trading_enabled} "
            f"take_profit_enabled={self._take_profit_enabled} tp_threshold={self._tp_threshold}c "
            f"trailing_enabled={self._trailing_enabled} trailing_drop={self._trailing_drop}c)"
        )
        # El cliente REST se abre SIEMPRE (el take-profit lee el orderbook aun en shadow). La
        # venta la gatea la EXISTENCIA del executor (CAPA A): solo se construye con trading on.
        async with KalshiRestClient() as client:
            self._client = client
            if self._trading_enabled:
                self._executor = Motor3ExitExecutor(client, strategy=STRATEGY)
            await self._loop(stop_event)

    async def _loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self._tick()
            except Exception as exc:
                logger.exception("motor2.exit_engine.tick_failed")
                BotState.record_error(f"motor2.exit_engine: {type(exc).__name__}: {exc}")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=self.LOOP_INTERVAL_SEC)

    async def _tick(self) -> None:
        """Un ciclo: agrega posiciones abiertas de Motor 2, detecta salidas y (liquida)."""
        positions = self._load_open_positions()
        logger.info(
            f"[MOTOR 2 EXIT DIAG] open_positions={len(positions)} "
            f"tp_enabled={self._take_profit_enabled} trail_enabled={self._trailing_enabled} "
            f"execution={self._executor is not None}"
        )
        if not positions:
            return

        bid_cache: dict[tuple[str, str], int | None] = {}
        exits: list[Motor2Position] = []
        for pos in positions:
            bid = await self._current_bid(pos.ticker, pos.side, bid_cache)
            tp_due = self._take_profit_enabled and take_profit_due(pos, bid, self._tp_threshold)

            trail_due = False
            peak: int | None = None
            if self._trailing_enabled and bid is not None:
                key = (pos.ticker, pos.side)
                peak = next_peak_bid(self._peaks.get(key), bid, pos.entry_cents)
                self._peaks[key] = peak
                trail_due = trailing_stop_due(pos, peak, bid, pos.entry_cents, self._trailing_drop)

            if not (tp_due or trail_due):
                continue

            # bid es int acá (los helpers devuelven False con bid None → no llegaríamos).
            assert bid is not None
            net = self._net_pnl_cents(pos, bid)
            if tp_due:
                logger.info(
                    f"[MOTOR 2 TP SHADOW] take_profit {pos.ticker} {pos.count}c side={pos.side} "
                    f"bid={bid}c >= {self._tp_threshold}c entry={pos.entry_cents}c "
                    f"net=${net / 100:+.2f}"
                )
            else:
                logger.info(
                    f"[MOTOR 2 TRAIL SHADOW] trailing_stop {pos.ticker} {pos.count}c "
                    f"side={pos.side} peak={peak}c bid={bid}c entry={pos.entry_cents}c "
                    f"drop={self._trailing_drop}c net=${net / 100:+.2f}"
                )
            exits.append(pos)

        if self._executor is not None:
            for pos in exits:
                # Motor2Position es duck-compatible con lo que usa exit_position (ticker/side/count).
                await self._executor.exit_position(pos)  # type: ignore[arg-type]

    def _load_open_positions(self) -> list[Motor2Position]:
        """Posiciones abiertas de Motor 2 = filas BUY filled no cerradas por CLV, agregadas por
        (ticker, side). Best-effort: un fallo de DB loguea y devuelve [] (no rompe el tick)."""
        try:
            with get_session() as s:
                buys = list(
                    s.exec(
                        select(Trade)
                        .where(
                            Trade.strategy == STRATEGY,
                            Trade.action == "buy",
                            Trade.status == "filled",
                            col(Trade.closed_by_clv).is_(False),
                        )
                        .order_by(col(Trade.placed_at))
                    )
                )
        except Exception as exc:
            logger.warning(f"motor2.exit.load_positions_error: {exc}")
            return []

        groups: dict[tuple[str, str], list[tuple[int, int]]] = {}
        for b in buys:
            if b.count <= 0 or b.side not in ("yes", "no"):
                continue
            entry = b.fill_price_cents or b.price_cents
            groups.setdefault((b.ticker, b.side), []).append((b.count, entry))

        positions: list[Motor2Position] = []
        for (ticker, side), legs in groups.items():
            total = sum(c for c, _ in legs)
            if total <= 0:
                continue
            wavg = round(sum(c * e for c, e in legs) / total)
            positions.append(
                Motor2Position(ticker=ticker, side=side, count=total, entry_cents=wavg, legs=legs)
            )
        return positions

    def _net_pnl_cents(self, pos: Motor2Position, bid: int) -> int:
        """PnL realizado NETO de fees si la posición se cerrara al bid ahora. Por-pata (FIFO),
        idéntico criterio que _settle_originals → el net del shadow = el realizado al cerrar."""
        total = 0
        for leg_count, leg_entry in pos.legs:
            total += (
                leg_count * (bid - leg_entry)
                - kalshi_fee_cents(leg_count, bid)
                - kalshi_fee_cents(leg_count, leg_entry)
            )
        return total

    async def _current_bid(
        self, ticker: str, side: str, cache: dict[tuple[str, str], int | None]
    ) -> int | None:
        """Bid actual del lado abierto (a quién le venderíamos). Cacheado por (ticker, side)
        dentro del tick — Motor 2 puede tener YES y NO del MISMO ticker, cada uno con su bid.
        Fail-safe: un error de orderbook loguea y devuelve None (los helpers lo tratan como
        'no decidible' → no dispara la salida)."""
        key = (ticker, side)
        if key in cache:
            return cache[key]
        bid: int | None = None
        if self._client is not None:
            try:
                ob = await self._client.get_orderbook(ticker)
                book = ob.get("orderbook", ob) if isinstance(ob, dict) else {}
                bid, _ = _top_bid(book.get(side) or [])
            except Exception as exc:
                logger.warning(f"motor2.exit.orderbook_error ticker={ticker} side={side}: {exc}")
                bid = None
        cache[key] = bid
        return bid
