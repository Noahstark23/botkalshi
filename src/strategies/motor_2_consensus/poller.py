"""
Poller shadow del Motor 2 — orquesta el cable Kalshi ↔ consenso, en loop, SIN ejecutar.

Por ciclo:
  1. Extrae los quotes 1X2 de Kalshi (KalshiQuoteSource).
  2. Trae el consenso de odds (OddsSource: fake hoy, The Odds API mañana).
  3. find_signals() → ConsensusSignal por outcome con edge neto > umbral.
  4. Graba EdgeWindow(kind="consensus") SOLO si la fuente de odds es real (is_live).
     Con la fuente fake las edges no son data → se loguea pero NO se persiste basura.

SHADOW estricto: emite/loguea/graba señales, JAMÁS coloca órdenes. Independiente de
TRADING_ENABLED. Best-effort: un ciclo que falla se loguea y el loop sigue.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from loguru import logger

from src.storage.models import EdgeWindow, FairKickoffSnapshot, Motor2FunnelSnapshot, get_session
from src.strategies.fair_value_book import FairProvenance, FairValueBook
from src.strategies.motor_2_consensus.detector import MIN_EDGE_PCT, ConsensusSignal, find_signals
from src.strategies.motor_2_consensus.executor import Motor2Executor
from src.strategies.motor_2_consensus.sources import KalshiQuoteSource, OddsSource
from src.utils.config import get_settings

if TYPE_CHECKING:
    from src.risk.manager import RiskManager


class Motor2ShadowPoller:
    """
    Loop del Motor 2. Por defecto SHADOW (solo detecta/graba). Si se le inyecta un
    `executor` Y la fuente de odds es REAL (is_live), apuesta — nunca sobre el fixture
    fake (apostar contra odds inventadas sería quemar plata).
    """

    DEFAULT_INTERVAL_SEC = 300.0  # 5 min — calibrable; el consenso no se mueve por segundo.
    MAX_BETS_PER_CYCLE = 5  # tope de órdenes por ciclo (el RiskManager además frena por exposición)

    def __init__(
        self,
        kalshi_source: KalshiQuoteSource,
        odds_source: OddsSource,
        *,
        interval_sec: float | None = None,
        capital_usd: float | None = None,
        min_edge: float | None = None,
        one_per_event: bool = True,
        max_stake_pct: float = 0.0,
        max_edge: float | None = None,
        risk_manager: RiskManager | None = None,
        executor: Motor2Executor | None = None,
        min_books: int = 1,
        max_book_age_min: float | None = None,
        fee_at_stake_count: bool = False,
        burst_interval_sec: float = 0.0,
        burst_window_min: float = 45.0,
        linemove=None,
    ):
        self._kalshi = kalshi_source
        self._odds = odds_source
        self._interval = interval_sec if interval_sec is not None else self.DEFAULT_INTERVAL_SEC
        # Capital base del sizing. Con un RiskManager, se toma su capital EFECTIVO por ciclo
        # (dinámico: cash real de Kalshi × factor, con piso/techo — refleja depósitos/retiros sin
        # tocar variables). `capital_usd` queda como fallback ESTÁTICO (tests / sin RM).
        self._risk = risk_manager
        self._capital_usd_static = (
            capital_usd if capital_usd is not None else get_settings().ACTIVE_CAPITAL_USD
        )
        # Umbral de edge NETO como FRACCIÓN (0.03 = 3pp). Default = el del detector; el
        # runner lo pasa desde config (MOTOR_2_MIN_EDGE_PCT / 100).
        self._min_edge = min_edge if min_edge is not None else MIN_EDGE_PCT
        # Techo de EJECUCIÓN (fracción; None = sin techo extra). El runner lo pasa desde
        # MOTOR_2_MAX_EDGE_PCT/100 (anti-fantasma: los buckets 12-13% sangraron −$621).
        self._max_edge = max_edge
        # Mutua exclusión por EVENTO (una sola apuesta direccional por partido). El runner lo
        # pasa desde config (MOTOR_2_ONE_BET_PER_EVENT); default True = seguro.
        self._one_per_event = one_per_event
        # Sizing flat (% del capital por trade, desacoplado de Kelly). El runner lo pasa desde
        # config (MOTOR_2_MAX_STAKE_PCT); 0 = ¼ Kelly previo.
        self._max_stake_pct = max_stake_pct
        # Presente SOLO con TRADING_ENABLED=true (lo construye el runner, Capa A). None = shadow.
        self._executor = executor
        # Endurecimiento de la MEDICIÓN del fair (auditoría rentabilidad 2026-07-07): mínimo
        # de casas para que exista consenso + frescura máxima de la línea de cada casa.
        # El runner los pasa desde MOTOR_2_MIN_BOOKS / MOTOR_2_MAX_BOOK_AGE_MIN.
        self._min_books = min_books
        self._max_book_age_min = max_book_age_min
        # Fee del edge al count real del stake (MOTOR_2_FEE_AT_STAKE_COUNT; default off =
        # comportamiento histórico). Ver _net_edge_pct en detector.py.
        self._fee_at_stake_count = fee_at_stake_count
        # Burst polling pre-kickoff (auditoría 2026-07-12): los edges reales del funnel son
        # TRANSITORIOS (5.63pp el 07-05, 3.20pp el 07-07 — un ciclo y desaparecen) y se
        # concentran cerca del inicio del partido (line moves por lineups/noticias que
        # Kalshi tarda en seguir). Con el intervalo base (300s) se ven de casualidad.
        # burst_interval_sec > 0 → cuando hay un kickoff dentro de burst_window_min, el
        # loop acelera a este intervalo. 0 = off (comportamiento histórico).
        self._burst_interval_sec = burst_interval_sec
        self._burst_window_min = burst_window_min
        self._next_kickoff: datetime | None = None  # próximo commence_time futuro visto
        self._in_burst = False  # para loguear solo las transiciones (anti-spam)
        # CLV-al-kickoff: tickers ya snapshoteados este proceso (dedup barato en memoria;
        # el dedup duro es la fila en DB — un reboot re-snapshotea a lo sumo una vez más).
        self._kickoff_snapshotted: set[str] = set()
        # Motor 6 (F1 shadow, opcional): pasajero del ciclo — observa (quotes, fair) y
        # detecta line-moves. None = apagado. JAMÁS ejecuta (no existe executor en F1).
        self._linemove = linemove

    def _capital_for_cycle(self) -> float:
        """Capital base del sizing ESTE ciclo: el efectivo del RiskManager (dinámico) si hay RM;
        si no, el estático. Por-ciclo a propósito: el bankroll cambia con depósitos/retiros y con
        el refresh del cash real, así el sizing flat sigue el capital vivo sin reconstruir nada."""
        if self._risk is not None:
            return self._risk.effective_capital_usd()
        return self._capital_usd_static

    async def poll_once(self) -> list[ConsensusSignal]:
        """Un ciclo: extrae, cruza, detecta, (persiste + apuesta si live). Devuelve las señales."""
        kalshi_events = await self._kalshi.fetch()
        if not kalshi_events:
            return []
        odds_events = await self._odds.fetch()
        if not odds_events:
            logger.info("motor2.shadow sin eventos de odds este ciclo")
            return []
        self._note_next_kickoff(odds_events)

        diag: dict[str, float] = {}
        fair_out: dict[str, float] = {}
        fair_commence_out: dict[str, datetime] = {}
        fair_event_out: dict[str, str] = {}
        fair_provenance_out: dict[str, FairProvenance] = {}
        signals = find_signals(
            kalshi_events,
            odds_events,
            capital_usd=self._capital_for_cycle(),
            min_edge=self._min_edge,
            diag=diag,
            one_per_event=self._one_per_event,
            max_stake_pct=self._max_stake_pct,
            fair_out=fair_out,
            fair_commence_out=fair_commence_out,
            fair_event_out=fair_event_out,
            fair_provenance_out=fair_provenance_out,
            max_edge=self._max_edge,
            min_books=self._min_books,
            max_book_age_min=self._max_book_age_min,
            fee_at_stake_count=self._fee_at_stake_count,
        )
        # Canal Motor 5 (F1 shadow): publica el fair de todo outcome matcheado SOLO con
        # odds reales — un fair del fixture fake no es precio de referencia para cotizar.
        if self._odds.is_live and fair_out:
            FairValueBook.publish(
                fair_out,
                commence_times=fair_commence_out,
                event_tickers=fair_event_out,
                provenances=fair_provenance_out,
            )
            logger.info(f"motor2.fair_book publicados={len(fair_out)}")
            # CLV-al-kickoff (Propuesta 2026-08-13): en el ÚLTIMO ciclo antes del
            # kickoff, el fair de cada ticker se persiste como su "cierre" — el
            # benchmark del CLV de los fills shadow de M5. Pasajero best-effort:
            # jamás rompe el ciclo del host.
            try:
                self._snapshot_fair_kickoff(kalshi_events, fair_out)
            except Exception:
                logger.exception("motor2.fair_kickoff_snapshot falló (el ciclo sigue)")
        # Motor 6 (pasajero best-effort): line-moves sobre el MISMO (quotes, fair) del ciclo
        # — cero requests extra. Su observe es internamente best-effort; el guard de acá es
        # el cinturón redundante: M6 nunca puede romper el ciclo del host.
        if self._linemove is not None:
            try:
                self._linemove.observe(kalshi_events, fair_out, is_live=self._odds.is_live)
            except Exception:
                logger.exception("motor6.shadow hook falló (el ciclo de M2 sigue)")
        logger.info(
            f"motor2.shadow ciclo: kalshi={len(kalshi_events)} odds={len(odds_events)} "
            f"señales={len(signals)} live={self._odds.is_live} executor={self._executor is not None}"
        )
        # Embudo diagnóstico: cuando señales=0, distingue "mercado eficiente" (best_edge bajo,
        # matched>0) de "el matcher rechaza" (reject_names/cardinality altos) o "pocas ventanas
        # pre-match" (started_skip alto / reject_absent alto). best_edge<0 = nada evaluado.
        best_pp = diag.get("best_net_edge", -1.0) * 100
        logger.info(
            f"motor2.funnel odds={int(diag.get('odds_total', 0))} "
            f"started_skip={int(diag.get('odds_started_skip', 0))} "
            f"kalshi={int(diag.get('kalshi_total', 0))} matched={int(diag.get('events_matched', 0))} "
            f"rej_absent={int(diag.get('reject_absent', 0))} "
            f"rej_card={int(diag.get('reject_cardinality', 0))} "
            f"rej_names={int(diag.get('reject_names', 0))} "
            f"rej_nofair={int(diag.get('reject_no_fair', 0))} "
            f"rej_date={int(diag.get('reject_date', 0))} "
            f"rej_ambig={int(diag.get('reject_ambiguous', 0))} "
            f"rej_started={int(diag.get('reject_started', 0))} "
            f"rej_high={int(diag.get('reject_high_edge', 0))} "
            f"skip_horizon={int(diag.get('skip_out_of_horizon', 0))} "
            f"skip_multi={int(diag.get('skip_multi_outcome', 0))} "
            f"best_edge={best_pp:.2f}pp umbral={self._min_edge * 100:.1f}pp"
        )
        # GATE DE DINERO REAL: persistir/apostar SOLO con odds reales (nunca sobre el fixture).
        if self._odds.is_live:
            # Memoria del embudo: una foto por ciclo (también con 0 señales — ESE es el dato
            # que el Analyst Loop necesita para trendear eficiente-vs-bug). Best-effort.
            self._persist_funnel(diag, len(signals))
            if signals:
                self._persist(signals)
                if self._executor is not None:
                    await self._execute(signals)
        return signals

    def _persist_funnel(self, diag: dict[str, float], n_signals: int) -> None:
        """Graba la foto del embudo del ciclo (Loop Engineering — memoria del diagnóstico)."""
        try:
            with get_session() as s:
                s.add(
                    Motor2FunnelSnapshot(
                        odds_total=int(diag.get("odds_total", 0)),
                        started_skip=int(diag.get("odds_started_skip", 0)),
                        kalshi_total=int(diag.get("kalshi_total", 0)),
                        events_matched=int(diag.get("events_matched", 0)),
                        reject_absent=int(diag.get("reject_absent", 0)),
                        reject_cardinality=int(diag.get("reject_cardinality", 0)),
                        reject_names=int(diag.get("reject_names", 0)),
                        reject_no_fair=int(diag.get("reject_no_fair", 0)),
                        best_net_edge_pp=diag.get("best_net_edge", -1.0) * 100,
                        signals=n_signals,
                    )
                )
                s.commit()
        except Exception:
            logger.exception("motor2.funnel.persist_error")

    async def _execute(self, signals: list[ConsensusSignal]) -> dict[str, int]:
        """
        Apuesta las mejores señales del ciclo (mayor edge primero), hasta MAX_BETS_PER_CYCLE.
        El RiskManager (dentro del executor) corta por exposición/stop-loss. Best-effort:
        un error de una señal se loguea y NO frena las demás ni el loop.

        Devuelve el conteo de desenlaces por motivo y lo loguea (fix observabilidad
        2026-07-02: "el bot no apostó hoy" era indiagnosticable sin grepear línea por
        línea — dedup_skip, stake_below, rechazos del RiskManager, etc. ahora salen
        agregados en UNA línea `motor2.exec.cycle`).
        """
        outcomes: dict[str, int] = {}
        top = sorted(signals, key=lambda s: s.edge_pct, reverse=True)[: self.MAX_BETS_PER_CYCLE]
        for sig in top:
            try:
                outcome = await self._executor.execute(sig)  # type: ignore[union-attr]
                key = "filled" if outcome.filled else (outcome.reason or "sin_motivo")
                outcomes[key] = outcomes.get(key, 0) + 1
                if outcome.filled:
                    logger.info(
                        f"motor2.bet FILLED ticker={sig.market_ticker} side={sig.kalshi_side} "
                        f"count={outcome.filled_count}"
                    )
                    # Aviso a Telegram: el bot APOSTÓ. Best-effort y aislado — si Telegram
                    # falla NO debe romper el loop ni la próxima apuesta. edge_pct es
                    # FRACCIÓN (0.03=3%) → ×100 para mostrarlo en porcentaje.
                    try:
                        from src.monitoring.telegram_alerts import alert_bet_placed

                        await alert_bet_placed(
                            motor="Motor 2 (consenso deportes)",
                            ticker=sig.market_ticker,
                            side=sig.kalshi_side,
                            count=outcome.filled_count,
                            price_cents=sig.kalshi_price_cents,
                            edge_pct=sig.edge_pct * 100,
                        )
                    except Exception:
                        logger.exception("motor2.bet.alert_error")
            except Exception as e:
                outcomes["error"] = outcomes.get("error", 0) + 1
                logger.exception(
                    f"motor2.bet error ticker={sig.market_ticker}: {type(e).__name__}: {e}"
                )
        if outcomes:
            desglose = " ".join(f"{k}={v}" for k, v in sorted(outcomes.items()))
            logger.info(
                f"motor2.exec.cycle señales={len(signals)} intentadas={len(top)} {desglose}"
            )
        return outcomes

    def _persist(self, signals: list[ConsensusSignal]) -> None:
        """
        Graba cada señal como EdgeWindow(kind="consensus") — el mismo sustrato de medición
        del shadow de arbitraje. Solo se llega acá con odds REALES. Fail-safe: un error de
        DB se loguea, nunca tira el loop.
        """
        try:
            with get_session() as s:
                for sig in signals:
                    # magnitude = edge NETO en ¢/contrato; gross/fees pueblan el desglose
                    # AUDITABLE (antes quedaban None → no se podía ver de dónde salía el edge).
                    s.add(
                        EdgeWindow(
                            market_ticker=sig.market_ticker[:100],
                            magnitude_cents=int(round(sig.edge_pct * 100)),
                            gross_spread_cents=sig.gross_edge_cents,
                            fees_cents=sig.fee_cents,
                            edge_pct=sig.edge_pct,
                            kind="consensus",
                        )
                    )
                s.commit()
        except Exception:
            logger.exception("motor2.shadow persist_error")

    def _snapshot_fair_kickoff(self, kalshi_events, fair_out: dict[str, float]) -> None:
        """Persiste el fair del ÚLTIMO ciclo pre-kickoff por ticker (el "cierre" del CLV).

        El kickoff sale de parse_event_key_start(event_key) — cero requests extra. Se
        dispara cuando el kickoff cae dentro del próximo intervalo del poller (este es
        el último fair que vamos a computar antes del arranque). Una vez por ticker."""
        from src.strategies.motor_2_consensus.matcher import ET, parse_event_key_start

        now = datetime.now(UTC)
        for ke in kalshi_events:
            parsed = parse_event_key_start(ke.event_key)
            if parsed is None or parsed[1] is None:
                continue  # sin hora en el key no hay kickoff preciso → sin cierre
            d, (hh, mm) = parsed
            kickoff = datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET).astimezone(UTC)
            restante = (kickoff - now).total_seconds()
            if not (0 <= restante <= self._interval * 1.5):
                continue  # todavía no es el último ciclo (o ya arrancó)
            filas = []
            for q in ke.outcomes:
                t = q.market_ticker
                if t in self._kickoff_snapshotted or t not in fair_out:
                    continue
                filas.append(
                    FairKickoffSnapshot(
                        ticker=t[:100], fair_prob=round(fair_out[t], 4), kickoff_at=kickoff
                    )
                )
                self._kickoff_snapshotted.add(t)
            if filas:
                with get_session() as s:
                    for fila in filas:
                        s.add(fila)
                    s.commit()
                logger.info(
                    f"motor2.fair_kickoff event={ke.event_key} snapshoteados={len(filas)} "
                    f"(cierre del CLV, kickoff en {restante:.0f}s)"
                )

    def _note_next_kickoff(self, odds_events: list) -> None:
        """Registra el próximo commence_time FUTURO del feed (para el burst pre-kickoff).
        Best-effort: cualquier rareza del feed deja None → ritmo base (nunca rompe el loop)."""
        try:
            now = datetime.now(UTC)
            upcoming = [
                oe.commence_time
                for oe in odds_events
                if getattr(oe, "commence_time", None) and oe.commence_time > now
            ]
            self._next_kickoff = min(upcoming) if upcoming else None
        except Exception:
            logger.exception("motor2.burst next_kickoff falló (se sigue a ritmo base)")
            self._next_kickoff = None

    def _cycle_timeout(self) -> float:
        """Intervalo del PRÓXIMO ciclo: burst si hay kickoff dentro de la ventana, base si no.
        El burst caza los edges transitorios pre-kickoff que el ritmo de 300s ve de casualidad."""
        if self._burst_interval_sec <= 0 or self._next_kickoff is None:
            burst = False
        else:
            mins_to_kick = (self._next_kickoff - datetime.now(UTC)).total_seconds() / 60.0
            burst = 0.0 <= mins_to_kick <= self._burst_window_min
        if burst != self._in_burst:  # loguear SOLO transiciones (anti-spam)
            self._in_burst = burst
            if burst:
                logger.info(
                    f"motor2.burst ON: kickoff {self._next_kickoff} en <"
                    f"{self._burst_window_min:.0f}min → interval={self._burst_interval_sec:.0f}s"
                )
            else:
                logger.info(f"motor2.burst OFF → interval base {self._interval:.0f}s")
        return self._burst_interval_sec if burst else self._interval

    async def run(self, stop_event: asyncio.Event) -> None:
        """Loop supervisado hasta stop_event. Cada ciclo es best-effort."""
        cap_src = "effective(RM)" if self._risk is not None else "static"
        burst_desc = (
            f", burst={self._burst_interval_sec:.0f}s@T-{self._burst_window_min:.0f}min"
            if self._burst_interval_sec > 0
            else ""
        )
        logger.info(
            f"motor2.shadow arrancado (interval={self._interval}s{burst_desc}, "
            f"odds={'LIVE' if self._odds.is_live else 'FAKE'}, "
            f"capital=${self._capital_for_cycle():.2f} [{cap_src}])"
        )
        while not stop_event.is_set():
            try:
                await self.poll_once()
            except Exception as e:
                logger.exception(f"motor2.shadow ciclo falló: {type(e).__name__}: {e}")
            # Espera interrumpible: el timeout es el ritmo del loop (base o burst); un set()
            # del stop_event lo corta de inmediato (shutdown sin esperar el intervalo entero).
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=self._cycle_timeout())
        logger.info("motor2.shadow detenido (stop_event)")
