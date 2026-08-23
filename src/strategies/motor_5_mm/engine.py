"""
Motor5Engine — loop supervisado del market maker. DOS modos por construcción:
  - F1 SHADOW (trading_enabled=False, default): cotiza HIPOTÉTICAMENTE y registra.
  - F2 LIVE-demo (trading_enabled=True): además sincroniza quotes REALES vía
    Motor5Executor + MMReconciler (Capa A: solo se construyen en run() con el flag).

Flujo por tick (LOOP_INTERVAL_SEC):
  1. Consume FairValueBook.fresh(TTL) — sin fair fresco no hay universo (skip implícito;
     el gap fair_fresh=0 sostenido se ve en mm_funnel_snapshots).
  2. Por ticker (cap MOTOR_MM_MAX_TICKERS, orden determinístico): top-of-book vía REST
     get_orderbook (fallback del plan §7 — migrar a V2 cuando esté estable es un cambio
     local en _book_top). Sin book → skip_no_book (el MM nunca cotiza a ciegas).
  3. Fills hipotéticos: el book ACTUAL contra la quote resting del tick ANTERIOR
     (regla conservadora de cruce estricto, shadow_fill.py). Se aplican al inventario
     simulado y se persisten (mm_shadow_fills).
  4. Nueva quote (quoter.py) → persiste (mm_quotes) y queda resting para el próximo tick.
  5. MMFunnelSnapshot por tick + log una línea `motor5.funnel`.

En F2 el tick antepone los GATES: kill-switch (cancel-all una vez, luego gestión mínima),
quotes_paused (gestiona sin cotizar), reconcile (la VERDAD runtime; propaga/limpia
corrupción del executor) y la aplicación idempotente de fills reales al inventario.
Los fills SHADOW (inferencia por cruce) quedan apagados con executor: la única fuente
de fills en live es la verdad del reconciler/cancel-response. En producción el validador
de config bloquea EXECUTION=true hasta F3.

Regla de oro (Lección 9): el estado (inventario + quotes vivas) lo muta SOLO este loop,
secuencialmente. Una excepción aplicando estado de un ticker descarta su quote viva
(re-sync natural el próximo tick) — nunca "sigue operando" con estado dudoso.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from loguru import logger
from sqlmodel import select

from src.clients.kalshi_rest import KalshiRestClient
from src.monitoring.health import BotState
from src.risk.manager import RiskManager
from src.storage.models import (
    MMExperimentRun,
    MMFunnelSnapshot,
    MMQuote,
    MMShadowFill,
    Trade,
    get_session,
    kill_switch_engaged,
    mm_quotes_paused,
)
from src.strategies.data_capture import parse_price_to_cents, rest_orderbook_sides
from src.strategies.fair_value_book import FAIR_METHOD_VERSION, FairValue, FairValueBook
from src.strategies.motor_5_mm.executor import STRATEGY, Motor5Executor
from src.strategies.motor_5_mm.fee_policy import (
    SeriesFeeObservation,
    SeriesFeePolicy,
    UnsupportedSeriesFeeError,
)
from src.strategies.motor_5_mm.fill_feed import MMFillFeed
from src.strategies.motor_5_mm.inventory import InventoryBook
from src.strategies.motor_5_mm.quoter import QuoteSet, compute_quote
from src.strategies.motor_5_mm.reconciler import MMReconciler
from src.strategies.motor_5_mm.shadow_fill import ShadowFill, fills_for_quote


class Motor5DataIntegrityError(RuntimeError):
    """La cohorte ya no puede auditarse; continuar alteraría la trayectoria shadow."""


def _fixed_point_depth(value: object) -> Decimal | None:
    """Parsea FixedPointCount sin pasar por float ni redondear contratos fraccionales."""
    if value is None or isinstance(value, bool):
        return None
    try:
        depth = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return depth if depth.is_finite() and depth > 0 else None


def _top_bid_with_depth(levels: list[object]) -> tuple[int | None, Decimal | None]:
    """Mejor bid y su size exacto; local a M5 para no cambiar otros motores."""
    best_price: int | None = None
    best_depth: Decimal | None = None
    for level in levels:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        price = parse_price_to_cents(level[0])
        depth = _fixed_point_depth(level[1])
        if price is None or depth is None:
            continue
        if best_price is None or price > best_price:
            best_price = price
            best_depth = depth
    return best_price, best_depth


class Motor5Engine:
    """Market maker F1: quotes shadow alrededor del fair de Motor 2, contra el book real."""

    LOOP_INTERVAL_SEC = 60.0
    # Re-arme del diagnóstico de book: un skip CRÓNICO no debe auto-silenciarse — un
    # one-shot de por vida se consume una vez (p.ej. en el boot, fuera de la ventana de
    # log que alguien esté mirando) y el skip perpetuo vuelve a ser arqueología.
    BOOK_DIAG_REARM_SEC = 1800.0
    # FRESCURA del mark para medir markout (bug encontrado por la sonda 2026-08-14):
    # históricamente _last_marks no se invalidaba — con max_tickers=10 y fair_fresh=20-48
    # los tickers rotaban, el mark se congelaba y ambos horizontes medían contra el mismo
    # valor viejo. Ahora un book ausente/unilateral invalida el mark y este TTL protege
    # además cualquier ruta de error: contra un mark congelado no se mide.
    MARK_FRESH_MAX_SEC = 90.0  # 1.5 ticks
    SHUTDOWN_OBSERVE_TIMEOUT_SEC = 10.0
    F1_METRIC_VERSION = "f1-v2-bbo-depth"
    F1_OBSERVABLE_FILL_CAP = 1

    def __init__(
        self,
        *,
        max_tickers: int = 10,
        series_csv: str = "*",
        experiment_label: str = "test",
        half_spread_cents: int = 3,
        edge_skew_cents: int = 0,
        quote_size_contracts: int = 1,
        max_inventory_contracts: int = 50,
        fair_ttl_sec: float = 600.0,
        require_pregame: bool = False,
        kickoff_buffer_sec: float = 120.0,
        client_factory: type[KalshiRestClient] = KalshiRestClient,
        trading_enabled: bool = False,
        risk_manager: RiskManager | None = None,
        fill_feed: MMFillFeed | None = None,
        mm_exposure_cap_usd: float | None = None,
        fees_as_maker: bool = False,
        jump_retreat_cents: float = 5.0,
        exchange_environment: str = "demo",
        fair_min_books: int = 1,
        fair_max_book_age_min: float | None = None,
        fair_odds_regions: str = "",
        fair_sport_keys_config: str = "",
        fair_cache_ttl_sec: float = 0.0,
    ) -> None:
        self._max_tickers = max_tickers
        parsed_series = {value.strip().upper() for value in series_csv.split(",") if value.strip()}
        if not parsed_series:
            raise ValueError("series_csv de Motor 5 no puede quedar vacío")
        # "*" existe solo para tests/uso directo retrocompatible. Runner siempre pasa la
        # lista explícita de Settings (producción F1-v2: KXMLBGAME).
        self._allowed_series: set[str] | None = None if "*" in parsed_series else parsed_series
        if self._allowed_series is not None and len(self._allowed_series) != 1:
            raise ValueError("Motor 5 F1-v2 exige exactamente una serie por cohorte")
        self._half_spread = half_spread_cents
        self._edge_skew = edge_skew_cents
        self._size = quote_size_contracts
        self._max_inventory = max_inventory_contracts
        self._fair_ttl = fair_ttl_sec
        self._require_pregame = require_pregame
        self._kickoff_buffer_sec = kickoff_buffer_sec
        self._client_factory = client_factory
        self._client: KalshiRestClient | None = None
        # APUESTA 1 (2026-08-12): modelo de fee del shadow — maker ($0) o taker (legacy).
        self._fees_as_maker = fees_as_maker
        # Retiro por salto (blindaje del maker): 0 = off.
        self._jump_retreat = jump_retreat_cents
        self._exchange_environment = exchange_environment
        self._fair_min_books = fair_min_books
        self._fair_max_book_age_min = fair_max_book_age_min
        self._fair_odds_regions = fair_odds_regions
        self._fair_sport_keys_config = fair_sport_keys_config
        self._fair_cache_ttl_sec = fair_cache_ttl_sec
        if self._fees_as_maker and not trading_enabled:
            if self._exchange_environment != "production":
                raise ValueError("M5 F1 shadow auditable exige exchange_environment=production")
            if self._size != 1:
                raise ValueError("M5 F1 shadow auditable exige quote_size_contracts=1")
        experiment_config = {
            "metric": self.F1_METRIC_VERSION,
            "label": experiment_label,
            "series": sorted(self._allowed_series or {"*"}),
            "max_tickers": self._max_tickers,
            "half_spread": self._half_spread,
            "edge_skew": self._edge_skew,
            "quote_size": self._size,
            "observable_fill_cap": self.F1_OBSERVABLE_FILL_CAP,
            "max_inventory": self._max_inventory,
            "fair_ttl": self._fair_ttl,
            "require_pregame": self._require_pregame,
            "kickoff_buffer": self._kickoff_buffer_sec,
            "fees_as_maker": self._fees_as_maker,
            "jump_retreat": self._jump_retreat,
            "exchange_environment": self._exchange_environment,
            "fair_min_books": self._fair_min_books,
            "fair_max_book_age_min": self._fair_max_book_age_min,
            "fair_odds_regions": self._fair_odds_regions,
            "fair_sport_keys_config": self._fair_sport_keys_config,
            "fair_cache_ttl_sec": self._fair_cache_ttl_sec,
            "fair_method_version": FAIR_METHOD_VERSION,
        }
        config_hash = hashlib.sha256(
            json.dumps(experiment_config, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:12]
        safe_label = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in experiment_label)
        self._experiment_id = f"{safe_label[:32]}-{config_hash}"
        self._inventory = InventoryBook(fees_as_maker=fees_as_maker)
        # MARKOUT (la métrica que decide si el maker chico sobrevive): fills shadow
        # pendientes de medir el mid a T+30s y T+5min contra el precio del fill —
        # markout negativo sistemático = selección adversa (nos cruzan cuando el fair
        # ya se movió). Acotado (nada sin tope) y best-effort.
        self._markouts_pendientes: list[dict] = []
        self._live_quotes: dict[str, QuoteSet] = {}
        self._live_quote_fees: dict[str, SeriesFeeObservation] = {}
        self._live_quote_commence: dict[str, datetime | None] = {}
        self._live_quote_created_at: dict[str, datetime] = {}
        self._live_quote_fairs: dict[str, FairValue] = {}
        self._fee_policy: SeriesFeePolicy | None = None
        self._last_marks: dict[str, float] = {}  # último mark conocido por ticker (MTM)
        self._last_marks_at: dict[str, float] = {}  # monotonic del último refresh del mark
        # MTM puede usar fair como prior si el book es unilateral. Markout NO: medir
        # selección adversa contra el mismo fair que originó la quote sería circular.
        self._mtm_marks: dict[str, float] = {}
        # Salto del mark en el tick en que se CREÓ cada quote viva. El fill lo hereda
        # (quote_jump_cents) → permite evaluar CUALQUIER umbral de blindaje después,
        # no solo el configurado. ticker → salto en ¢ (0.0 si el tick fue calmo).
        self._quote_jump: dict[str, float] = {}
        # ── F2 (demo): ejecución real. CAPA A: el executor SOLO se construye (en run())
        # con trading_enabled=True — que el runner deriva de TRADING_ENABLED AND
        # MOTOR_MM_EXECUTION_ENABLED (y el validador de config bloquea EXECUTION=true en
        # producción hasta F3). Con executor=None, este engine ES el F1 shadow intacto.
        self._trading_enabled = trading_enabled
        self._risk = risk_manager
        self._fill_feed = fill_feed
        self._mm_exposure_cap_usd = mm_exposure_cap_usd
        self._executor: Motor5Executor | None = None
        self._reconciler: MMReconciler | None = None
        self._settled_coids: set[str] = set()  # fills ya aplicados al inventario (1 sola vez)
        self._kill_cancelled = False  # cancel-all del kill-switch ya disparado
        # Diagnóstico de book POR-TICKER con backoff (no un bool global de por vida): un
        # ticker sano en el boot no consume el diagnóstico del que tiene el problema, y
        # un skip crónico re-loguea cada BOOK_DIAG_REARM_SEC en vez de callarse para
        # siempre tras la primera vez. Valor = time.monotonic() del último log.
        self._book_diag_last: dict[str, float] = {}
        self._experiment_run_id: int | None = None

    def _begin_experiment_run(self) -> None:
        """Abre una época durable antes de tocar estado shadow.

        Una época anterior ``running`` prueba un cierre no observado. No se borra ni se
        auto-corrige: el gate bloqueará esa etiqueta de cohorte y /ready lo hará visible.
        """
        try:
            with get_session() as s:
                prior = list(
                    s.exec(
                        select(MMExperimentRun).where(
                            MMExperimentRun.experiment_id == self._experiment_id,
                            MMExperimentRun.status != "clean",
                        )
                    )
                )
                row = MMExperimentRun(experiment_id=self._experiment_id, status="running")
                s.add(row)
                s.commit()
                s.refresh(row)
        except Exception as exc:
            raise Motor5DataIntegrityError(
                "no se pudo abrir la cadena de custodia del experimento"
            ) from exc
        if row.id is None:  # pragma: no cover - SQLite asigna PK tras refresh
            raise Motor5DataIntegrityError("run M5 sin id durable")
        self._experiment_run_id = row.id
        if prior:
            BotState.motor5_experiment_invalid = True
            BotState.motor5_experiment_invalid_reason = (
                f"cohorte arrastra {len(prior)} run(s) no-clean: "
                f"{prior[0].status} {(prior[0].reason or 'sin cierre')[:300]}"
            )
        else:
            BotState.motor5_experiment_invalid = False
            BotState.motor5_experiment_invalid_reason = None

    def _invalidate_experiment(self, reason: str) -> None:
        """Marca la época inválida; nunca vuelve de invalid a clean."""
        short_reason = reason[:500]
        BotState.motor5_experiment_invalid = True
        BotState.motor5_experiment_invalid_reason = short_reason
        run_id = self._experiment_run_id
        if run_id is None:
            return
        try:
            with get_session() as s:
                row = s.get(MMExperimentRun, run_id)
                if row is None:
                    raise Motor5DataIntegrityError(f"run_id={run_id} desapareció")
                row.status = "invalid"
                row.reason = short_reason
                s.add(row)
                s.commit()
        except Motor5DataIntegrityError:
            raise
        except Exception as exc:
            # Si este write también falla, la fila previa queda `running`, que el gate
            # igualmente rechaza. Nunca hay transición silenciosa a clean.
            raise Motor5DataIntegrityError(
                f"no se pudo marcar inválida la cohorte: {short_reason}"
            ) from exc

    def _close_experiment_run_clean(self) -> None:
        run_id = self._experiment_run_id
        if run_id is None:
            return
        try:
            with get_session() as s:
                row = s.get(MMExperimentRun, run_id)
                if row is None:
                    raise Motor5DataIntegrityError(f"run_id={run_id} desapareció")
                if row.status == "running":
                    row.status = "clean"
                row.ended_at = datetime.now(UTC)
                s.add(row)
                s.commit()
        except Motor5DataIntegrityError:
            raise
        except Exception as exc:
            raise Motor5DataIntegrityError(
                "no se pudo cerrar la cadena de custodia del experimento"
            ) from exc

    def _rehidratar_markouts(self) -> None:
        """Reconstruye la cola de markouts pendientes desde la TABLA tras un restart.

        Fuga encontrada por la sonda 2026-08-14: la cola vivía solo en RAM, así que
        cada redeploy borraba los markouts en vuelo (los fills 254/255 perdieron su
        T+5min porque el proceso se reinició 6 minutos después). Con la cadencia de
        deploys de estos días eso es una fuga MATERIAL de la métrica que decide el gate.

        No hace falta una cola persistida con due_at: la tabla YA sabe qué falta medir
        (markout NULL + fill reciente). Se rehidrata una vez al arrancar el loop; los
        fills demasiado viejos (>600s, el mismo tope del fail-safe) se ignoran porque su
        ventana ya venció. Best-effort: un fallo de DB no impide arrancar el motor."""
        limite = datetime.now(UTC) - timedelta(seconds=600)
        try:
            with get_session() as s:
                filas = list(
                    s.exec(
                        select(MMShadowFill).where(
                            MMShadowFill.created_at >= limite,
                            MMShadowFill.markout2_cents.is_(None),  # type: ignore[union-attr]
                        )
                    )
                )
        except Exception as exc:
            logger.exception("motor5.rehidratar_markouts_error")
            raise Motor5DataIntegrityError("no se pudo rehidratar la cola de markouts") from exc
        for f in filas:
            if f.id is None:
                continue
            creado = f.created_at
            if creado.tzinfo is None:  # SQLite no preserva tz
                creado = creado.replace(tzinfo=UTC)
            self._markouts_pendientes.append(
                {
                    "id": f.id,
                    "ticker": f.ticker,
                    "side": f.side,
                    "price": f.price_cents,
                    "creado": creado,
                    "m1_hecho": f.markout1_cents is not None,
                }
            )
        if filas:
            logger.info(f"motor5.markouts_rehidratados={len(filas)} (sobrevivieron al redeploy)")

    def _rehidratar_inventory(self) -> None:
        """Reconstruye la trayectoria F1-v2 desde fills durables de ESTA cohorte.

        Sin esto cada redeploy volvía el inventario a cero y cambiaba el skew/lado cotizado,
        mezclando varias políticas bajo el mismo experiment_id.
        """
        try:
            with get_session() as s:
                rows = list(
                    s.exec(
                        select(MMShadowFill)
                        .where(
                            MMShadowFill.metric_version == self.F1_METRIC_VERSION,
                            MMShadowFill.experiment_id == self._experiment_id,
                        )
                        .order_by(MMShadowFill.id)
                    )
                )
        except Exception as exc:
            raise Motor5DataIntegrityError("no se pudo rehidratar el inventario shadow") from exc
        for row in rows:
            self._inventory.apply_fill(
                ShadowFill(
                    ticker=row.ticker,
                    side=row.side,
                    price_cents=row.price_cents,
                    count=row.count,
                    rule=row.rule,
                ),
                fee_multiplier=row.fee_multiplier or 1,
            )
        if rows:
            logger.info(
                f"motor5.inventory_rehidratado fills={len(rows)} "
                f"abs={self._inventory.total_abs_contracts()}"
            )

    async def run(self, stop_event: asyncio.Event) -> None:
        mode = "F2 LIVE (demo)" if self._trading_enabled else "F1 SHADOW — CERO órdenes"
        logger.info(
            f"[MOTOR 5] arrancado {mode}. "
            f"max_tickers={self._max_tickers} half_spread={self._half_spread}c "
            f"size={self._size} max_inv={self._max_inventory} fair_ttl={self._fair_ttl}s "
            f"experiment_id={self._experiment_id}"
        )
        self._begin_experiment_run()
        try:
            self._rehidratar_markouts()
            self._rehidratar_inventory()
            async with self._client_factory() as client:
                self._client = client
                self._fee_policy = SeriesFeePolicy(client)
                if self._trading_enabled:
                    self._executor = Motor5Executor(
                        client, risk=self._risk, max_exposure_usd=self._mm_exposure_cap_usd
                    )
                    self._reconciler = MMReconciler(client)
                while not stop_event.is_set():
                    try:
                        await self._tick()
                    except Motor5DataIntegrityError:
                        raise  # fatal: supervisor/probe nunca declara verde una cohorte corrupta
                    except Exception as exc:
                        logger.exception("motor5.engine.tick_failed")
                        BotState.record_error(f"motor5.engine: {type(exc).__name__}: {exc}")
                        self._invalidate_experiment(f"tick incompleto {type(exc).__name__}: {exc}")
                        raise Motor5DataIntegrityError(
                            "tick M5 incompleto; trayectoria shadow no reproducible"
                        ) from exc
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop_event.wait(), timeout=self.LOOP_INTERVAL_SEC)
                if self._executor is None:
                    await self._finalize_shadow_exposure()
                # Shutdown limpio (auditoría 2026-07-07, P0 de F2): sin esto las quotes GTC
                # quedaban RESTING en el book sin bot que las gestione.
                if self._executor is not None:
                    try:
                        n = await self._executor.cancel_all("shutdown")
                        logger.info(f"[MOTOR 5] shutdown → cancel_all n={n}")
                    except Exception:
                        logger.exception("motor5.engine.shutdown_cancel_error")
            self._close_experiment_run_clean()
        except BaseException as exc:
            if not BotState.motor5_experiment_invalid:
                try:
                    self._invalidate_experiment(f"{type(exc).__name__}: {exc}")
                except Motor5DataIntegrityError:
                    # La fila original permanece `running`, que también bloquea el gate.
                    logger.exception("motor5.experiment_invalidation_write_failed")
            raise
        logger.info("[MOTOR 5 SHADOW] detenido (stop_event)")

    async def _finalize_shadow_exposure(self) -> None:
        """Observa el último intervalo de cada quote antes de declarar el run limpio.

        SIGTERM/redeploy puede llegar entre ticks. Borrar esas quotes sin mirar el BBO
        censura precisamente el último intervalo (y sus fills adversos). Las lecturas se
        lanzan juntas y tienen un presupuesto corto; una que no termina deja la cohorte
        ``invalid``, nunca ``clean`` por optimismo.
        """
        if not self._live_quotes:
            return
        deadline = asyncio.get_running_loop().time() + self.SHUTDOWN_OBSERVE_TIMEOUT_SEC
        tasks = {
            ticker: asyncio.create_task(self._book_top(ticker)) for ticker in self._live_quotes
        }
        done, pending = await asyncio.wait(
            set(tasks.values()), timeout=self.SHUTDOWN_OBSERVE_TIMEOUT_SEC
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        for ticker, task in tasks.items():
            quote = self._live_quotes.get(ticker)
            if quote is None:
                continue
            try:
                top = task.result() if task in done and not task.cancelled() else None
            except Exception:
                logger.exception(f"motor5.shutdown_book_error ticker={ticker}")
                top = None
            observed_at = datetime.now(UTC)
            if top is None:
                self._invalidate_mark(ticker)
                self._invalidate_experiment(
                    f"shutdown con intervalo expuesto sin BBO para {ticker}"
                )
                self._forget_live_quote(ticker)
                continue
            yes_bid, yes_ask, yes_bid_depth, yes_ask_depth = top
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                self._invalidate_experiment(
                    f"shutdown agotó presupuesto con quote expuesta: {ticker}"
                )
                self._forget_live_quote(ticker)
                continue
            try:
                fee_observation = await asyncio.wait_for(
                    self._revalidate_exposed_fee(ticker, self._live_quote_fees.get(ticker)),
                    timeout=remaining,
                )
            except TimeoutError:
                self._invalidate_experiment(
                    f"shutdown sin tiempo para revalidar fee expuesta: {ticker}"
                )
                self._forget_live_quote(ticker)
                continue
            if self._fees_as_maker and fee_observation is None:
                self._forget_live_quote(ticker)
                continue
            prior_mark = self._last_marks.get(ticker)
            mark = self._record_mark(ticker, yes_bid, yes_ask, quote.fair_prob)
            mark_jump = (
                abs(mark - prior_mark) if mark is not None and prior_mark is not None else None
            )
            self._settle_fills(
                quote,
                yes_bid,
                yes_ask,
                yes_bid_depth=yes_bid_depth,
                yes_ask_depth=yes_ask_depth,
                fill_fair_prob=quote.fair_prob,
                mark_jump=mark_jump,
                quote_jump=self._quote_jump.get(ticker),
                fee_observation=fee_observation,
                commence_time=self._live_quote_commence.get(ticker),
                quote_created_at=self._live_quote_created_at.get(ticker),
                fill_at=observed_at,
            )
            self._forget_live_quote(ticker)

    async def _tick(self) -> None:
        BotState.motor5_tick_started(experiment_id=self._experiment_id)
        now = datetime.now(UTC)
        counters = {
            "quoted": 0,
            "skip_no_book": 0,
            "skip_unprofitable": 0,
            "skip_degenerate": 0,
            "skip_fair_range": 0,
            "fills": 0,
            "exec_corrupted": 0,
            "exec_risk_blocked": 0,
            "skip_fee_policy": 0,
            "skip_pregame": 0,
        }
        # ── Gates F2 (solo con executor) — se evalúan ANTES de cualquier quote ──────
        quotes_allowed = True
        if self._executor is not None:
            engaged, ks_reason = kill_switch_engaged()
            if engaged:
                # Pánico: cancel-all UNA vez (<5s vía batch) y el motor queda en gestión
                # mínima (reconcile) sin cotizar hasta que el humano limpie el switch.
                if not self._kill_cancelled:
                    n = await self._executor.cancel_all(f"kill_switch: {ks_reason}")
                    logger.warning(f"[MOTOR 5] kill-switch → cancel_all n={n}")
                    self._kill_cancelled = True
                quotes_allowed = False
            else:
                self._kill_cancelled = False
                paused, pause_reason = mm_quotes_paused()
                if paused:
                    # quotes_paused: NO se emiten quotes nuevas; se sigue gestionando
                    # (reconcile + fills). Las resting existentes quedan gestionadas.
                    logger.info(f"[MOTOR 5] quotes_paused ({pause_reason}) → tick sin cotizar")
                    quotes_allowed = False
            # Fast-path del canal fill: cualquier fill encolado dispara reconcile YA.
            ws_fills = self._fill_feed.drain() if self._fill_feed is not None else []
            if self._reconciler is not None:
                # live_coids: lo que ESTE proceso gestiona. Tras un restart es vacío →
                # el reconcile cancela las resting del proceso anterior (huérfanas, P0
                # auditoría 2026-07-07) antes de que el tick cotice encima.
                report = await self._reconciler.reconcile(live_coids=self._executor.live_coids())
                # Verdad del reconcile: los tickers con discrepancia quedan corruptos;
                # los que salieron limpios se des-corrompen (ya se puede cotizar).
                self._executor.corrupted = set(report.corrupted_tickers)
                if ws_fills:
                    logger.info(f"[MOTOR 5] fill_feed fast-path: {len(ws_fills)} fills")
            counters["fills"] += self._apply_settled_fills()
        raw_fairs = {
            ticker: fair
            for ticker, fair in FairValueBook.fresh(self._fair_ttl, now=now).items()
            if self._allowed_series is None
            or ticker.split("-", 1)[0].upper() in self._allowed_series
        }
        fairs = raw_fairs
        if self._require_pregame:
            before_pregame = len(fairs)
            fairs = {
                ticker: fair
                for ticker, fair in fairs.items()
                if fair.commence_time is not None
                and now < fair.commence_time - timedelta(seconds=self._kickoff_buffer_sec)
            }
            counters["skip_pregame"] = before_pregame - len(fairs)
        # Universo del tick: determinístico (orden alfabético) y capado. Antes de retirar
        # una quote shadow que salió por TTL/pregame/rotación, se observa su ÚLTIMO intervalo
        # de exposición. Borrarla sin leer el book censuraba justo el cruce de T−buffer.
        tickers = sorted(fairs)[: self._max_tickers] if quotes_allowed else []
        for stale_ticker in [t for t in self._live_quotes if t not in tickers]:
            previous = self._live_quotes.get(stale_ticker)
            if previous is not None and self._executor is None:
                top = await self._book_top(stale_ticker)
                observed_at = datetime.now(UTC)
                if top is not None:
                    yes_bid, yes_ask, yes_bid_depth, yes_ask_depth = top
                    prior_mark = self._last_marks.get(stale_ticker)
                    current_fair = raw_fairs.get(stale_ticker)
                    fair_prob = (
                        current_fair.fair_prob if current_fair is not None else previous.fair_prob
                    )
                    current_mark = self._record_mark(stale_ticker, yes_bid, yes_ask, fair_prob)
                    mark_jump = (
                        abs(current_mark - prior_mark)
                        if current_mark is not None and prior_mark is not None
                        else None
                    )
                    fee_observation = await self._revalidate_exposed_fee(
                        stale_ticker, self._live_quote_fees.get(stale_ticker)
                    )
                    if not self._fees_as_maker or fee_observation is not None:
                        counters["fills"] += self._settle_fills(
                            previous,
                            yes_bid,
                            yes_ask,
                            yes_bid_depth=yes_bid_depth,
                            yes_ask_depth=yes_ask_depth,
                            fill_fair_prob=fair_prob,
                            mark_jump=mark_jump,
                            quote_jump=self._quote_jump.get(stale_ticker),
                            fee_observation=fee_observation,
                            commence_time=self._live_quote_commence.get(stale_ticker),
                            quote_created_at=self._live_quote_created_at.get(stale_ticker),
                            fill_at=observed_at,
                        )
                else:
                    self._invalidate_mark(stale_ticker)
                    self._invalidate_experiment(
                        f"intervalo expuesto sin BBO al retirar {stale_ticker}"
                    )
            self._forget_live_quote(stale_ticker)
            if self._executor is not None:
                await self._executor.retire_ticker(stale_ticker)
        for ticker in tickers:
            fv = fairs[ticker]
            top = await self._book_top(ticker)
            if top is None:
                counters["skip_no_book"] += 1
                self._invalidate_mark(ticker)
                if ticker in self._live_quotes:
                    self._invalidate_experiment(f"intervalo expuesto sin BBO para {ticker}")
                # Sin book no se evalúan fills (no hay evidencia de cruce) NI se re-cotiza:
                # la quote vieja se retira (no cotizamos a ciegas — plan §7, books stale).
                self._forget_live_quote(ticker)
                if self._executor is not None:
                    await self._executor.retire_ticker(ticker)
                continue
            yes_bid, yes_ask, yes_bid_depth, yes_ask_depth = top
            observed_at = datetime.now(UTC)
            # SALTO del mark desde el tick anterior (Apuesta 1, blindaje): el insumo del
            # retiro por salto Y la etiqueta de los fills (mark_jump_cents). Se captura
            # ANTES de refrescar el mark.
            mark_previo = self._last_marks.get(ticker)
            mark_actual = self._record_mark(ticker, yes_bid, yes_ask, fv.fair_prob)
            salto = (
                abs(mark_actual - mark_previo)
                if mark_actual is not None and mark_previo is not None
                else None
            )
            # Fills SHADOW solo sin executor (F1): con ejecución real, los fills vienen
            # de la verdad del reconciler (_apply_settled_fills) — nunca de la inferencia.
            prev = self._live_quotes.get(ticker)
            prev_fee_observation = self._live_quote_fees.get(ticker)
            provenance_error = self._fair_provenance_error(fv, observed_at)
            if self._fees_as_maker and provenance_error is not None:
                counters["skip_fee_policy"] += 1
                if prev is not None and self._executor is None:
                    self._invalidate_experiment(
                        f"fair provenance inválida con quote expuesta {ticker}: {provenance_error}"
                    )
                self._forget_live_quote(ticker)
                if self._executor is not None:
                    await self._executor.retire_ticker(ticker)
                logger.warning(f"motor5.fair_provenance_block ticker={ticker}: {provenance_error}")
                continue
            fee_observation: SeriesFeeObservation | None = None
            fee_multiplier = 1
            if self._fees_as_maker:
                try:
                    if self._fee_policy is None:  # pragma: no cover - run() siempre la crea
                        raise UnsupportedSeriesFeeError("fee policy no inicializada")
                    if fv.event_ticker is None:
                        raise UnsupportedSeriesFeeError(
                            "fair sin event_ticker oficial; no se puede resolver fee efectiva"
                        )
                    fee_observation = await self._fee_policy.observe(
                        ticker, event_ticker=fv.event_ticker
                    )
                    fee_multiplier = fee_observation.multiplier
                    if (
                        prev is not None
                        and self._executor is None
                        and (
                            prev_fee_observation is None
                            or self._fee_signature(prev_fee_observation)
                            != self._fee_signature(fee_observation)
                        )
                    ):
                        self._invalidate_experiment(
                            f"fee cambió o faltó durante quote expuesta: {ticker}"
                        )
                        self._forget_live_quote(ticker)
                        continue
                except Exception as exc:
                    counters["skip_fee_policy"] += 1
                    if prev is not None and self._executor is None:
                        self._invalidate_experiment(
                            f"fee no observable con quote expuesta {ticker}: "
                            f"{type(exc).__name__}: {exc}"
                        )
                    self._forget_live_quote(ticker)
                    if self._executor is not None:
                        await self._executor.retire_ticker(ticker)
                    logger.warning(
                        f"motor5.fee_policy_block ticker={ticker}: {type(exc).__name__}: {exc}"
                    )
                    continue
            if prev is not None and self._executor is None:
                try:
                    # El fill que un salto ya causó SE CUENTA (un maker real tampoco
                    # cancela en 0ms) — pero queda etiquetado para que el gate segmente
                    # markout de salto vs calmo. Medir, no asumir.
                    counters["fills"] += self._settle_fills(
                        prev,
                        yes_bid,
                        yes_ask,
                        yes_bid_depth=yes_bid_depth,
                        yes_ask_depth=yes_ask_depth,
                        fill_fair_prob=fv.fair_prob,
                        mark_jump=salto,
                        quote_jump=self._quote_jump.get(ticker),
                        fee_observation=fee_observation,
                        commence_time=self._live_quote_commence.get(ticker, fv.commence_time),
                        quote_created_at=self._live_quote_created_at.get(ticker),
                        fill_at=observed_at,
                    )
                except Exception:
                    # Estado del ticker en duda → quote fuera y re-sync el próximo tick.
                    self._forget_live_quote(ticker)
                    raise
            # El reloj del tick NO se congela: los awaits REST pueden durar minutos. Una
            # quote previa se observa arriba con hora real; una nueva solo nace si el fair
            # y el kickoff siguen válidos en este instante.
            if not self._fair_is_eligible(fv, observed_at):
                counters["skip_pregame"] += 1
                self._forget_live_quote(ticker)
                if self._executor is not None:
                    await self._executor.retire_ticker(ticker)
                continue
            # RETIRO POR SALTO (primer dato del gate 13-ago: 4 fills in-play con markout
            # −18/−20¢, ambos lados perdiendo a la vez — el evento del juego atravesó
            # las quotes): mark saltó ≥ umbral → la quote se retira y este tick no se
            # re-cotiza. Vuelve sola al primer tick calmo.
            if salto is not None and self._jump_retreat > 0 and salto >= self._jump_retreat:
                counters["skip_jump"] = counters.get("skip_jump", 0) + 1
                # Paridad de política: F1 debe simular exactamente las quotes que F2/F3
                # crearían. El fill de la quote PREVIA ya se midió arriba (un maker real no
                # cancela en 0ms); después del salto ambos modos retiran y no re-cotizan.
                # Observar quotes post-salto solo en shadow contaminaba inventario/skew y
                # permitía aprobar ganancias de órdenes que live jamás habría colocado.
                self._forget_live_quote(ticker)
                if self._executor is not None:
                    await self._executor.retire_ticker(ticker)
                logger.info(
                    f"motor5.jump_retreat ticker={ticker} salto={salto:.1f}c "
                    f"≥ {self._jump_retreat:.0f}c → quote RETIRADA "
                    f"({'live' if self._executor is not None else 'shadow'})"
                )
                continue
            action_at = datetime.now(UTC)
            if not self._fair_is_eligible(fv, action_at):
                counters["skip_pregame"] += 1
                self._forget_live_quote(ticker)
                if self._executor is not None:
                    await self._executor.retire_ticker(ticker)
                continue
            quote, skip = compute_quote(
                ticker,
                fv.fair_prob,
                half_spread_cents=self._half_spread,
                edge_skew_cents=self._edge_skew,
                size_contracts=self._size,
                inventory_contracts=self._inventory.net(ticker),
                max_inventory_contracts=self._max_inventory,
                best_yes_bid=yes_bid,
                best_yes_ask=yes_ask,
                fees_as_maker=self._fees_as_maker,
                fee_multiplier=fee_multiplier,
            )
            if quote is None:
                key = {"fair_out_of_range": "skip_fair_range"}.get(skip or "", f"skip_{skip}")
                counters[key] = counters.get(key, 0) + 1
                self._forget_live_quote(ticker)
                if self._executor is not None:
                    await self._executor.retire_ticker(ticker)
                continue
            counters["quoted"] += 1
            self._live_quotes[ticker] = quote
            self._live_quote_commence[ticker] = fv.commence_time
            self._live_quote_created_at[ticker] = action_at
            self._live_quote_fairs[ticker] = fv
            if fee_observation is not None:
                self._live_quote_fees[ticker] = fee_observation
            else:
                self._live_quote_fees.pop(ticker, None)
            self._quote_jump[ticker] = salto if salto is not None else 0.0
            self._persist_quote(
                quote,
                fv_age_sec=(action_at - fv.computed_at).total_seconds(),
                yes_bid=yes_bid,
                yes_ask=yes_ask,
            )
            # CAPA B: guard en el call site — sin executor (shadow) acá no pasa nada.
            if self._executor is not None:
                outcome = await self._executor.sync_quotes(quote)
                if outcome == "corrupted":
                    counters["exec_corrupted"] += 1
                elif outcome == "risk_blocked":
                    counters["exec_risk_blocked"] += 1
        # Los tickers con markout pendiente que rotaron FUERA del universo necesitan su
        # mark refrescado aparte: si no, su horizonte largo no se mide nunca (bug 15-ago).
        await self._refrescar_marks_pendientes(tickers)
        # MARKOUT: con los marks del tick recién refrescados, medir los fills pendientes
        # (T+30s / T+5min). Va acá y no en _settle_fills porque el mark "posterior" de un
        # fill es el de un tick FUTURO, no el del tick que lo detectó.
        self._medir_markouts()
        mtm = self._inventory.total_mtm_cents(self._mtm_marks)
        self._persist_snapshot(len(fairs), counters, mtm)
        BotState.motor5_heartbeat(
            experiment_id=self._experiment_id,
            fair_fresh=len(fairs),
            book_attempted=len(tickers),
            quoted=counters["quoted"],
            skip_no_book=counters["skip_no_book"],
            skip_fee_policy=counters["skip_fee_policy"],
        )
        logger.info(
            f"motor5.funnel fair_fresh={len(fairs)} quoted={counters['quoted']} "
            f"skip_book={counters['skip_no_book']} skip_unprof={counters['skip_unprofitable']} "
            f"skip_degen={counters['skip_degenerate']} skip_fair={counters['skip_fair_range']} "
            f"skip_jump={counters.get('skip_jump', 0)} "
            f"skip_fee={counters['skip_fee_policy']} "
            f"skip_pregame={counters['skip_pregame']} "
            f"fills={counters['fills']} inv_abs={self._inventory.total_abs_contracts()} "
            f"mtm={mtm}c"
            + (
                f" exec[corrupted={counters['exec_corrupted']} "
                f"risk_blocked={counters['exec_risk_blocked']}]"
                if self._executor is not None
                else ""
            )
        )

    def _apply_settled_fills(self) -> int:
        """Aplica al inventario los fills REALES ya confirmados (filas Trade filled del
        motor), UNA sola vez por client_order_id. Única fuente de mutación del inventario
        en modo live: la verdad del reconciler/cancel-response — jamás la inferencia del
        shadow ni el WS directo (evita doble conteo y carreras)."""
        try:
            with get_session() as s:
                rows = list(
                    s.exec(
                        select(Trade).where(Trade.strategy == STRATEGY, Trade.status == "filled")
                    )
                )
        except Exception:
            logger.exception("motor5.engine.settled_fills_query_error")
            return 0
        applied = 0
        for row in rows:
            if row.client_order_id in self._settled_coids:
                continue
            self._settled_coids.add(row.client_order_id)
            count = row.filled_count if row.filled_count is not None else row.count
            if count <= 0:
                continue
            fill = ShadowFill(
                ticker=row.ticker,
                side=row.action,  # buy (bid) | sell (ask), eje YES
                price_cents=row.fill_price_cents or row.price_cents,
                count=count,
                rule="real_fill",
            )
            inv = self._inventory.apply_fill(fill)
            applied += 1
            logger.info(
                f"[MOTOR 5 LIVE] fill {fill.side} {count}x{row.ticker} "
                f"@{fill.price_cents}c net={inv.net_contracts} fees={inv.fees_cents}c"
            )
        return applied

    def _settle_fills(
        self,
        quote: QuoteSet,
        yes_bid: int | None,
        yes_ask: int | None,
        *,
        yes_bid_depth: Decimal | None = None,
        yes_ask_depth: Decimal | None = None,
        fill_fair_prob: float,
        mark_jump: float | None = None,
        quote_jump: float | None = None,
        fee_observation: SeriesFeeObservation | None = None,
        commence_time: datetime | None = None,
        quote_created_at: datetime | None = None,
        fill_at: datetime | None = None,
    ) -> int:
        """Aplica los fills hipotéticos de la quote resting contra el book actual.

        Fair-at-fill (2026-08-07): junto con el fill se registra el fair VIGENTE (el del
        ciclo que detectó el cruce) además del fair de la quote (tick t−1). La resta de
        ambos separa la pregunta del A/B: edge≥0 contra el fair vigente = spread capturado;
        edge<0 = el fair se movió y el mercado nos cruzó (selección adversa). El dato se
        persiste crudo (probs + book) y el juicio lo hace el análisis, no el hot path."""
        fills = fills_for_quote(
            quote,
            best_yes_bid=yes_bid,
            best_yes_ask=yes_ask,
            best_yes_bid_depth=yes_bid_depth,
            best_yes_ask_depth=yes_ask_depth,
            observable_count_cap=self.F1_OBSERVABLE_FILL_CAP,
        )
        for fill in fills:
            multiplier = fee_observation.multiplier if fee_observation is not None else 1
            projected_inventory = self._inventory.net(fill.ticker) + (
                fill.count if fill.side == "buy" else -fill.count
            )
            fill_fair_cents = fill_fair_prob * 100.0
            edge_c = (
                fill_fair_cents - fill.price_cents
                if fill.side == "buy"
                else fill.price_cents - fill_fair_cents
            )
            drift_c = (fill_fair_prob - quote.fair_prob) * 100.0
            fill_id = self._persist_fill(
                fill,
                projected_inventory,
                quote_fair_prob=quote.fair_prob,
                fill_fair_prob=fill_fair_prob,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                mark_jump=mark_jump,
                quote_jump=quote_jump,
                fee_observation=fee_observation,
                commence_time=commence_time,
                quote_created_at=quote_created_at,
                fill_at=fill_at,
                fair_value=self._live_quote_fairs.get(fill.ticker),
            )
            if fill_id is None:
                self._invalidate_experiment(
                    f"fill shadow observado pero no persistido ticker={fill.ticker}"
                )
                raise Motor5DataIntegrityError(
                    f"fill shadow observado pero no persistido ticker={fill.ticker}"
                )
            # Solo después del commit: la trayectoria en RAM nunca puede adelantarse al ledger.
            inv = self._inventory.apply_fill(fill, fee_multiplier=multiplier)
            logger.info(
                f"[MOTOR 5 SHADOW] fill {fill.side} {fill.count}x{fill.ticker} "
                f"@{fill.price_cents}c ({fill.rule}) net={inv.net_contracts} "
                f"cash={inv.cash_cents}c fees={inv.fees_cents}c "
                f"edge={edge_c:+.1f}c drift={drift_c:+.1f}c"
            )
            # Encolar la medición de markout (T+30s/T+5min contra el mark futuro).
            self._markouts_pendientes.append(
                {
                    "id": fill_id,
                    "ticker": fill.ticker,
                    "side": fill.side,
                    "price": fill.price_cents,
                    # Reloj de PARED, no monotonic: es lo único que sobrevive a un
                    # redeploy (ver _rehidratar_markouts). Los horizontes son de
                    # 30s/300s — el drift de NTP es irrelevante a esa escala.
                    "creado": fill_at or datetime.now(UTC),
                    "m1_hecho": False,
                }
            )
            if len(self._markouts_pendientes) > 500:
                raise Motor5DataIntegrityError("cola de markouts excedió 500; cohorte incompleta")
        return len(fills)

    async def _refrescar_marks_pendientes(self, universo: list[str]) -> None:
        """Refresca el mark de los tickers con markout PENDIENTE que quedaron FUERA del
        universo cotizado.

        Bug encontrado por la sonda 2026-08-15 (6 fills con markout1 medido y markout2
        NULL a 2h del fill): el universo es `sorted(fairs)[:max_tickers]` — los 10
        primeros ALFABÉTICAMENTE de 20-48 fairs frescos — así que los tickers ROTAN
        constantemente. Cuando uno rota fuera, _record_mark deja de correr para él, su
        mark se congela, el guard de frescura (#235, correcto) lo descarta, y su
        markout2 no se mide JAMÁS: a los 600s se suelta en NULL. El juez PRINCIPAL del
        gate no acumulaba para ningún ticker rotado — o sea, para la mayoría.

        La medición no puede depender de si en este momento estamos cotizando ese
        ticker. Costo acotado: un get_orderbook por ticker DISTINTO con markout
        pendiente fuera del universo (unidades, no cientos). Best-effort: un fallo deja
        el mark viejo y el guard de frescura sigue protegiendo — nunca se inventa."""
        if not self._markouts_pendientes:
            return
        en_universo = set(universo)
        faltantes = {
            p["ticker"] for p in self._markouts_pendientes if p["ticker"] not in en_universo
        }
        for ticker in sorted(faltantes):
            try:
                top = await self._book_top(ticker)
            except Exception:
                self._invalidate_mark(ticker)
                logger.exception("motor5.refresco_mark_pendiente_error")
                continue
            if top is None:
                self._invalidate_mark(ticker)
                continue
            yes_bid, yes_ask, _yes_bid_depth, _yes_ask_depth = top
            if yes_bid is None or yes_ask is None:
                self._invalidate_mark(ticker)
                continue  # sin las dos puntas no hay mid: no se inventa un mark
            self._last_marks[ticker] = (yes_bid + yes_ask) / 2.0
            self._last_marks_at[ticker] = time.monotonic()

    def _medir_markouts(self) -> None:
        """Markout de los fills shadow: (mark_posterior − precio) para buys, invertido
        para sells — positivo = el mercado nos dio la razón; negativo sistemático =
        SELECCIÓN ADVERSA (nos cruzan cuando el fair ya se movió), la causa de muerte
        documentada del maker chico. Resolución = cadencia del tick (~60s): el T+30s
        real es "el primer tick ≥30s" y se persiste la edad exacta. Best-effort."""
        if not self._markouts_pendientes:
            return
        ahora = datetime.now(UTC)
        mono = time.monotonic()
        restantes: list[dict] = []
        for p in self._markouts_pendientes:
            edad = (ahora - p["creado"]).total_seconds()
            mark = self._mark_fresco(p["ticker"], mono)
            try:
                if not p["m1_hecho"] and edad >= 30.0 and mark is not None:
                    self._persist_markout(p["id"], 1, self._markout_cents(p, mark), edad)
                    p["m1_hecho"] = True
                    # markout2 JAMÁS en la misma pasada: dos horizontes exigen DOS
                    # observaciones distintas del mercado, no la misma leída dos veces.
                elif edad >= 300.0:
                    if mark is not None:
                        self._persist_markout(p["id"], 2, self._markout_cents(p, mark), edad)
                        continue  # completo → sale de la cola
                if edad >= 600.0:
                    continue  # sin mark FRESCO en 10min (ticker fuera del universo) → soltar
            except Motor5DataIntegrityError:
                raise
            except Exception:
                logger.exception("motor5.markout_error")
                continue  # un fill problemático no bloquea la cola
            restantes.append(p)
        self._markouts_pendientes = restantes

    def _mark_fresco(self, ticker: str, ahora: float) -> float | None:
        """El mark SOLO si fue refrescado en este tick o el anterior. Un ticker que rotó
        fuera del universo conserva su último mark en _last_marks para siempre; medir
        contra él es inventar una observación que nadie hizo."""
        marcado_en = self._last_marks_at.get(ticker)
        if marcado_en is None or (ahora - marcado_en) > self.MARK_FRESH_MAX_SEC:
            return None
        return self._last_marks.get(ticker)

    def _fair_is_eligible(self, fair: FairValue, at: datetime) -> bool:
        """Revalida fair y kickoff con el reloj REAL posterior a cada await."""
        if (at - fair.computed_at).total_seconds() > self._fair_ttl:
            return False
        if not self._require_pregame:
            return True
        return fair.commence_time is not None and at < fair.commence_time - timedelta(
            seconds=self._kickoff_buffer_sec
        )

    def _fair_provenance_error(self, fair: FairValue, at: datetime) -> str | None:
        provenance = fair.provenance
        if provenance is None:
            return "sin provenance del consenso"
        if not fair.event_ticker or provenance.event_ticker != fair.event_ticker:
            return "event_ticker inconsistente"
        if provenance.method_version != FAIR_METHOD_VERSION:
            return f"método inesperado {provenance.method_version!r}"
        if provenance.min_books != self._fair_min_books:
            return "min_books no coincide con la cohorte"
        if provenance.max_book_age_min != self._fair_max_book_age_min:
            return "max_book_age no coincide con la cohorte"
        if len(provenance.bookmaker_keys) < self._fair_min_books:
            return "menos books efectivos que el mínimo"
        configured_sports = {
            value.strip() for value in self._fair_sport_keys_config.split(",") if value.strip()
        }
        if not configured_sports or provenance.sport_key not in configured_sports:
            return f"sport_key fuera de config: {provenance.sport_key!r}"
        if not self._fair_odds_regions.strip() or self._fair_cache_ttl_sec <= 0:
            return "config de Odds API incompleta"
        if self._fair_max_book_age_min is not None:
            oldest = provenance.oldest_book_update
            newest = provenance.newest_book_update
            if oldest is None or newest is None:
                return "books sin timestamps completos"
            if oldest > newest or newest > at:
                return "timestamps de books incoherentes"
            age_min = (at - oldest).total_seconds() / 60.0
            if age_min > self._fair_max_book_age_min:
                return "fair envejeció más que max_book_age antes de cotizar"
        return None

    @staticmethod
    def _fee_signature(observation: SeriesFeeObservation) -> tuple[object, ...]:
        return (
            observation.event_ticker,
            observation.source,
            observation.fee_type,
            observation.multiplier,
            observation.base_fee_type,
            observation.base_multiplier,
            observation.override_fee_type,
            observation.override_multiplier,
        )

    async def _revalidate_exposed_fee(
        self, ticker: str, previous: SeriesFeeObservation | None
    ) -> SeriesFeeObservation | None:
        """Demuestra que la fee no cambió durante una quote shadow expuesta.

        Si no puede observarse, o cambió base/override, el intervalo no tiene una fee
        atribuible con honestidad: se invalida toda la cohorte en lugar de inventar P&L.
        """
        if not self._fees_as_maker:
            return previous
        if self._fee_policy is None or previous is None or not previous.event_ticker:
            self._invalidate_experiment(f"quote expuesta sin provenance de fee: {ticker}")
            return None
        try:
            current = await self._fee_policy.observe(ticker, event_ticker=previous.event_ticker)
        except Exception as exc:
            self._invalidate_experiment(
                f"fee no observable al cerrar intervalo {ticker}: {type(exc).__name__}: {exc}"
            )
            return None
        if self._fee_signature(current) != self._fee_signature(previous):
            self._invalidate_experiment(f"fee cambió durante quote expuesta: {ticker}")
            return None
        return current

    def _forget_live_quote(self, ticker: str) -> None:
        """Retira todo el metadata de una quote como una sola operación lógica."""
        self._live_quotes.pop(ticker, None)
        self._live_quote_fees.pop(ticker, None)
        self._live_quote_commence.pop(ticker, None)
        self._live_quote_created_at.pop(ticker, None)
        self._live_quote_fairs.pop(ticker, None)
        self._quote_jump.pop(ticker, None)

    def _invalidate_mark(self, ticker: str) -> None:
        """Un book ausente/unilateral invalida el BBO previo para cualquier markout."""
        self._last_marks.pop(ticker, None)
        self._last_marks_at.pop(ticker, None)

    @staticmethod
    def _markout_cents(p: dict, mark_cents: float) -> float:
        signo = 1.0 if p["side"] == "buy" else -1.0
        return signo * (mark_cents - p["price"])

    def _persist_markout(self, fill_id: int, which: int, valor: float, edad_sec: float) -> None:
        try:
            with get_session() as s:
                row = s.get(MMShadowFill, fill_id)
                if row is None:
                    raise Motor5DataIntegrityError(f"fill_id={fill_id} desapareció del ledger")
                if which == 1:
                    row.markout1_cents = round(valor, 2)
                    row.markout1_age_sec = round(edad_sec, 1)
                else:
                    row.markout2_cents = round(valor, 2)
                    row.markout2_age_sec = round(edad_sec, 1)
                s.add(row)
                s.commit()
            logger.info(
                f"[MOTOR 5 SHADOW] markout fill_id={fill_id} t+{edad_sec:.0f}s "
                f"mo{which}={valor:+.1f}c"
            )
        except Motor5DataIntegrityError:
            raise
        except Exception as exc:
            logger.exception("motor5.persist_markout_error")
            raise Motor5DataIntegrityError(
                f"no se pudo persistir markout fill_id={fill_id}"
            ) from exc

    def _record_mark(
        self, ticker: str, yes_bid: int | None, yes_ask: int | None, fair_prob: float
    ) -> float | None:
        """Registra MTM y devuelve únicamente un mid Kalshi bilateral para markout."""
        if yes_bid is not None and yes_ask is not None:
            mid = (yes_bid + yes_ask) / 2.0
            self._last_marks[ticker] = mid
            self._mtm_marks[ticker] = mid
            self._last_marks_at[ticker] = time.monotonic()
            return mid
        else:
            self._mtm_marks[ticker] = fair_prob * 100.0
            self._invalidate_mark(ticker)
            return None

    async def _book_top(self, ticker: str) -> tuple[int, int, Decimal, Decimal] | None:
        """BBO YES vía REST: precios y depth fixed-point; None = sin book usable.

        El book de Kalshi lista BIDs resting de cada lado: yes_ask = 100 − no_bid (todo se
        cotiza desde el eje YES). El depth del no_bid es por tanto el depth observable del
        yes_ask. Fail-safe (Lección 7): error → None, el tick sigue."""
        if self._client is None:
            return None
        try:
            ob = await self._client.get_orderbook(ticker)
        except Exception as exc:
            logger.warning(f"motor5.book_error ticker={ticker}: {type(exc).__name__}: {exc}")
            return None
        # Shape 2026-07-15: la API migró a 'orderbook_fp' + 'yes_dollars'/'no_dollars'
        # (dólares-string) y este parser leía vacío en silencio (160 líneas book_shape
        # sobre books con ~$500k resting). El normalizador tolera TODAS las generaciones.
        sides = rest_orderbook_sides(ob)
        yes_levels = sides["yes"]
        no_levels = sides["no"]
        yes_bid, yes_bid_depth = _top_bid_with_depth(yes_levels)
        no_bid, no_bid_depth = _top_bid_with_depth(no_levels)
        yes_ask = (100 - no_bid) if no_bid is not None else None
        if yes_bid is None and yes_ask is None:
            # Diagnóstico POR-TICKER que separa las tres causas que el funnel colapsa en
            # 'skip_no_book' (el quoter tolera un solo lado; acá solo cae AMBOS sin top):
            #   a) listas presentes pero VACÍAS  → book sin resting (selección de market)
            #   b) niveles presentes pero size=0 → sin volumen usable
            #   c) claves inesperadas / no-lista → shape (book_keys lo delata)
            # OJO forense: si skip_no_book incrementa SIN ninguna línea book_shape, el None
            # salió del path de excepción de arriba → grep motor5.book_error, no shape.
            mono = time.monotonic()
            last = self._book_diag_last.get(ticker)
            if last is None or mono - last >= self.BOOK_DIAG_REARM_SEC:
                self._book_diag_last[ticker] = mono
                logger.warning(
                    f"motor5.book_shape ticker={ticker} sin top usable — "
                    f"yes_levels={len(yes_levels)} no_levels={len(no_levels)} "
                    f"resp_keys={sorted(ob.keys()) if isinstance(ob, dict) else type(ob).__name__} "
                    f"raw={str(ob)[:400]}"
                )
            return None
        # F1 necesita un BBO bilateral económicamente posible. `parse_price_to_cents`
        # acepta strings fixed-point; un shape legacy ambiguo como "27" se interpreta
        # 2700¢. Sin este límite, yes_ask podía ser negativo y el shadow inventaba AMBOS
        # fills. Un book cruzado también es una observación transitoria/stale, no evidencia
        # ejecutable para un maker externo.
        valid_bbo = (
            yes_bid is not None
            and yes_ask is not None
            and 0 <= yes_bid <= 99
            and 1 <= yes_ask <= 100
            and yes_bid < yes_ask
            and yes_bid_depth is not None
            and no_bid_depth is not None
        )
        if not valid_bbo:
            mono = time.monotonic()
            last = self._book_diag_last.get(ticker)
            if last is None or mono - last >= self.BOOK_DIAG_REARM_SEC:
                self._book_diag_last[ticker] = mono
                logger.warning(
                    f"motor5.book_invalid ticker={ticker} yes_bid={yes_bid} "
                    f"yes_ask={yes_ask} — F1 exige BBO bilateral 0<=bid<ask<=100"
                )
            return None
        return yes_bid, yes_ask, yes_bid_depth, no_bid_depth

    # ---- persistencia best-effort (un fallo de DB loguea, no rompe el tick) ----

    def _persist_quote(
        self, quote: QuoteSet, *, fv_age_sec: float, yes_bid: int | None, yes_ask: int | None
    ) -> None:
        try:
            with get_session() as s:
                s.add(
                    MMQuote(
                        ticker=quote.ticker[:100],
                        fair_prob=round(quote.fair_prob, 4),
                        fair_age_sec=round(fv_age_sec, 1),
                        bid_cents=quote.bid_cents,
                        ask_cents=quote.ask_cents,
                        size=quote.size,
                        yes_bid=yes_bid,
                        yes_ask=yes_ask,
                        inventory=self._inventory.net(quote.ticker),
                    )
                )
                s.commit()
        except Exception:
            logger.exception("motor5.persist_quote_error")

    def _persist_fill(
        self,
        fill,
        inventory_after: int,
        *,
        quote_fair_prob: float | None = None,
        fill_fair_prob: float | None = None,
        yes_bid: int | None = None,
        yes_ask: int | None = None,
        mark_jump: float | None = None,
        quote_jump: float | None = None,
        fee_observation: SeriesFeeObservation | None = None,
        commence_time: datetime | None = None,
        quote_created_at: datetime | None = None,
        fill_at: datetime | None = None,
        fair_value: FairValue | None = None,
    ) -> int | None:
        """Devuelve el id de la fila (para el markout) o None si la persistencia falló.
        fee_cents SIEMPRE registra la fee de TAKER (kalshi_fee_cents) aunque el modelo
        del shadow sea maker: es el dato que permite derivar las DOS contabilidades de
        la misma tabla — el análisis resta o no según el modelo bajo estudio."""
        from src.math.fees import kalshi_fee_cents, kalshi_maker_fee_cents

        try:
            multiplier = fee_observation.multiplier if fee_observation is not None else 1
            provenance = fair_value.provenance if fair_value is not None else None
            with get_session() as s:
                row = MMShadowFill(
                    ticker=fill.ticker[:100],
                    side=fill.side,
                    price_cents=fill.price_cents,
                    count=fill.count,
                    crossed_depth=fill.observed_depth,
                    fee_cents=kalshi_fee_cents(
                        fill.count, fill.price_cents, fee_multiplier=multiplier
                    ),
                    fee_model="maker" if self._fees_as_maker else "taker",
                    fee_effective_cents=(
                        kalshi_maker_fee_cents(
                            fill.count, fill.price_cents, fee_multiplier=multiplier
                        )
                        if self._fees_as_maker
                        else kalshi_fee_cents(
                            fill.count, fill.price_cents, fee_multiplier=multiplier
                        )
                    ),
                    metric_version=(
                        self.F1_METRIC_VERSION if fee_observation is not None else None
                    ),
                    experiment_id=(self._experiment_id if fee_observation is not None else None),
                    fee_multiplier=float(multiplier),
                    fee_type=fee_observation.fee_type if fee_observation is not None else None,
                    event_ticker=(
                        fee_observation.event_ticker if fee_observation is not None else None
                    ),
                    fee_source=fee_observation.source if fee_observation is not None else None,
                    fee_base_multiplier=(
                        float(fee_observation.base_multiplier)
                        if fee_observation is not None
                        else None
                    ),
                    fee_base_type=(
                        fee_observation.base_fee_type if fee_observation is not None else None
                    ),
                    fee_override_multiplier=(
                        float(fee_observation.override_multiplier)
                        if fee_observation is not None
                        and fee_observation.override_multiplier is not None
                        else None
                    ),
                    fee_override_type=(
                        fee_observation.override_fee_type if fee_observation is not None else None
                    ),
                    fee_schedule_observed_at=(
                        fee_observation.observed_at if fee_observation is not None else None
                    ),
                    exchange_environment=self._exchange_environment,
                    fair_method_version=(
                        provenance.method_version if provenance is not None else None
                    ),
                    fair_odds_event_id=(
                        provenance.odds_event_id if provenance is not None else None
                    ),
                    fair_sport_key=provenance.sport_key if provenance is not None else None,
                    fair_bookmaker_keys=(
                        ",".join(provenance.bookmaker_keys) if provenance is not None else None
                    ),
                    fair_book_count=(
                        len(provenance.bookmaker_keys) if provenance is not None else None
                    ),
                    fair_oldest_book_update=(
                        provenance.oldest_book_update if provenance is not None else None
                    ),
                    fair_newest_book_update=(
                        provenance.newest_book_update if provenance is not None else None
                    ),
                    fair_min_books=provenance.min_books if provenance is not None else None,
                    fair_max_book_age_min=(
                        provenance.max_book_age_min if provenance is not None else None
                    ),
                    fair_computed_at=fair_value.computed_at if fair_value is not None else None,
                    fair_odds_regions=self._fair_odds_regions,
                    fair_sport_keys_config=self._fair_sport_keys_config,
                    fair_cache_ttl_sec=self._fair_cache_ttl_sec,
                    fair_ttl_sec=self._fair_ttl,
                    commence_time=commence_time,
                    seconds_to_kickoff=(
                        round((commence_time - (fill_at or datetime.now(UTC))).total_seconds(), 1)
                        if commence_time is not None
                        else None
                    ),
                    policy_require_pregame=self._require_pregame,
                    policy_kickoff_buffer_sec=self._kickoff_buffer_sec,
                    quote_seconds_to_kickoff=(
                        round((commence_time - quote_created_at).total_seconds(), 1)
                        if commence_time is not None and quote_created_at is not None
                        else None
                    ),
                    rule=fill.rule[:50],
                    inventory_after=inventory_after,
                    quote_fair_prob=round(quote_fair_prob, 4)
                    if quote_fair_prob is not None
                    else None,
                    fill_fair_prob=round(fill_fair_prob, 4) if fill_fair_prob is not None else None,
                    yes_bid=yes_bid,
                    yes_ask=yes_ask,
                    mark_jump_cents=round(mark_jump, 2) if mark_jump is not None else None,
                    quote_jump_cents=round(quote_jump, 2) if quote_jump is not None else None,
                )
                s.add(row)
                s.commit()
                s.refresh(row)
                return row.id
        except Exception:
            logger.exception("motor5.persist_fill_error")
        return None

    def _persist_snapshot(self, fair_fresh: int, counters: dict[str, int], mtm: int) -> None:
        try:
            with get_session() as s:
                s.add(
                    MMFunnelSnapshot(
                        fair_fresh=fair_fresh,
                        quoted=counters["quoted"],
                        skip_no_book=counters["skip_no_book"],
                        skip_unprofitable=counters["skip_unprofitable"],
                        skip_degenerate=counters["skip_degenerate"],
                        skip_fair_range=counters["skip_fair_range"],
                        skip_fee_policy=counters["skip_fee_policy"],
                        skip_pregame=counters["skip_pregame"],
                        fills=counters["fills"],
                        inventory_abs=self._inventory.total_abs_contracts(),
                        mtm_pnl_cents=mtm,
                    )
                )
                s.commit()
        except Exception:
            logger.exception("motor5.persist_snapshot_error")
