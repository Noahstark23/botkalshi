"""
Motor de Gestión de Riesgo (Fase 2 Motor 1).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from loguru import logger
from sqlmodel import col, select

from src.math.arbitrage import ArbOpportunity
from src.math.fees import kalshi_fee_cents
from src.monitoring.health import BotState
from src.monitoring.telegram_alerts import alert_risk_event, send_alert
from src.storage.models import RiskEvent, Trade, engage_kill_switch, get_session
from src.strategies.motor_rest_arb.settlement import arb_group_key
from src.utils.config import get_settings


@dataclass(frozen=True, slots=True)
class TradeDecision:
    approved: bool
    reason: str
    max_allowed_count: int


class RiskManager:
    """
    Motor de Gestión de Riesgo (Fase 2).

    Aplica controles de PnL, exposición máxima y sizing por trade.

    DEUDA RESUELTA (FASE 0.3, 2026-06):
    - Sobrestima de exposición: los arbs COMPLETOS fillados (ambas patas yes+no del
      mismo ticker, identificadas por arb_id de A.1) se descuentan — son posiciones
      HEDGED (payout 100¢ garantizado), no riesgo direccional.
    - Race entre check_pre_trade concurrentes: serializado con lock de CLASE
      (cross-instancia: cada motor crea su RiskManager pero comparten el lock).

    DEUDA VIGENTE (documentada, NO redefinida):
    - PnL realized-only: trades filled-no-settled no cuentan para stop-loss. Es una
      DECISIÓN de semántica financiera del owner, no un bug — el settlement (PR-B)
      la vuelve operativa (settled ahora existe), no la cambia.
    - Residual del lock (REDUCIDO 2026-07-01): check_and_reserve escribe el intent
      bajo el MISMO lock y Motor 2 lo usa — su ventana check→persist quedó cerrada.
      Motor REST sigue con check_pre_trade + _persist_intents fuera del lock
      (migrarlo es un refactor de su engine/executor, anotado como mejora aparte).
    """

    # Lock de CLASE: serializa check_pre_trade entre TODAS las instancias (un
    # RiskManager por motor). En asyncio single-process esto elimina la carrera
    # leer-exposición→aprobar de dos checks simultáneos.
    _check_lock: asyncio.Lock = asyncio.Lock()

    # ── C-01: capital base EFECTIVO = balance REAL de Kalshi (cash disponible) ──────────
    # Cacheado a nivel de CLASE (compartido por todas las instancias, igual que _check_lock):
    # lo refresca una tarea de fondo del runner al arrancar + cada BALANCE_REFRESH_SECONDS,
    # SIEMPRE FUERA del _check_lock (no bloquear el gatekeeper con I/O de red). Es CASH
    # DISPONIBLE, NO equity total (no incluye el valor de las posiciones abiertas).
    _cached_capital_usd: float | None = None
    _last_balance_at: datetime | None = None
    _capital_fallback_warned: bool = False
    # Último cash real crudo (sin factor/clamp) — solo para /status. Se actualiza en CADA
    # refresh exitoso; _cached_capital_usd puede quedar atrás por el suavizado (anti-churn).
    _last_raw_balance_usd: float | None = None
    # C-03: histéresis del alert de desfase config↔cash real (no spamear cada refresh).
    _drift_alerted: bool = False

    # C-02: techo duro de capital en PRODUCCIÓN — espejo del validator _production_safety
    # (config.py), que rechaza ACTIVE_CAPITAL_USD > $5k en prod. El capital derivado del cash
    # real DEBE respetar el mismo techo: aunque el cash sea $10k, no operamos como si lo fuera.
    PROD_CAPITAL_HARD_CAP_USD: float = 5000.0
    # C-03: margen de histéresis para re-armar el alert de desfase (puntos % de drift).
    _DRIFT_REARM_MARGIN_PCT: float = 5.0

    def __init__(self) -> None:
        self.settings = get_settings()

    # =========================================================
    # C-01 — Capital base efectivo (balance real de Kalshi)
    # =========================================================

    def _get_effective_capital_usd(self) -> float:
        """
        Capital base efectivo en USD para TODOS los techos de riesgo (sizing por trade,
        exposición simultánea y stop-losses diario/semanal/mensual). **Fuente única de verdad.**

        C-02: cuando hay balance REAL cacheado (cash disponible, NO equity), el capital base =
        `min(cash_real × CAPITAL_SAFETY_FACTOR_PCT, PROD_CAPITAL_HARD_CAP_USD en prod)`. El
        factor es un colchón anti-desfase (slippage, fills parciales, el cash se mueve entre
        refresh); el clamp respeta el mismo techo de $5k que el validator de producción.

        Decisión explícita: los STOP-LOSSES también se derivan de este capital efectivo (no del
        ACTIVE_CAPITAL_USD estático) — coherencia: todos los techos salen del mismo "dinero que
        me permito arriesgar". Es ligeramente MÁS conservador (umbrales menores), nunca menos.

        Fallback (C-01): si NUNCA se obtuvo balance (refresh no corrió o la API falla desde el
        arranque), cae a `settings.ACTIVE_CAPITAL_USD` SIN factorizar — ese valor ya es un piso
        de seguridad conservador. Loguea WARNING una vez. NUNCA devuelve 0 por un fallo de red.

        Capital dinámico: DYNAMIC_CAPITAL_ENABLED=False fuerza el estático ACTIVE_CAPITAL_USD
        (escudo / dry-run). Sobre el cash real se aplica techo configurable (CAPITAL_CAP_USD) y
        piso (CAPITAL_FLOOR_USD) — el efectivo nunca baja del piso para no romper la matemática
        de riesgo; la PAUSA de nuevas entradas la decide can_open_new_positions().
        """
        # Toggle maestro: dynamic off → capital estático (ignora el cash real).
        if not self.settings.DYNAMIC_CAPITAL_ENABLED:
            return self.settings.ACTIVE_CAPITAL_USD

        cached = RiskManager._cached_capital_usd
        if cached is None:
            if not RiskManager._capital_fallback_warned:
                RiskManager._capital_fallback_warned = True
                logger.warning(
                    "risk.capital: sin balance real de Kalshi todavía → fallback a "
                    f"ACTIVE_CAPITAL_USD=${self.settings.ACTIVE_CAPITAL_USD:.2f} (piso de seguridad)"
                )
            base = self.settings.ACTIVE_CAPITAL_USD
        else:
            # C-02: cash real × factor de seguridad, con techo y piso configurables.
            base = cached * (self.settings.CAPITAL_SAFETY_FACTOR_PCT / 100.0)
            base = min(base, self.settings.CAPITAL_CAP_USD)
            base = max(base, self.settings.CAPITAL_FLOOR_USD)

        # C-02: clamp ÚNICO al hard cap en producción — cubre AMBOS paths (cash real Y
        # fallback). Hoy el fallback ya respeta el techo vía el validator de config, pero
        # clampear acá lo hace robusto si alguien sube ese límite (no depende de otro archivo).
        if self.settings.KALSHI_ENV == "production":
            base = min(base, RiskManager.PROD_CAPITAL_HARD_CAP_USD)
        return base

    def effective_capital_usd(self) -> float:
        """Capital base efectivo en USD (API pública de `_get_effective_capital_usd`): la fuente
        ÚNICA de los techos de riesgo, derivada del cash REAL de Kalshi (capital dinámico) o del
        fallback estático. La consume p.ej. el poller de Motor 2 para dimensionar contra el
        bankroll real por ciclo (refleja depósitos/retiros) en vez de ACTIVE_CAPITAL_USD fijo."""
        return self._get_effective_capital_usd()

    def can_open_new_positions(self) -> bool:
        """True si se permiten NUEVAS entradas. False = capital (cash real × factor, pre-piso)
        bajo CAPITAL_FLOOR_USD → se pausan las entradas, pero la GESTIÓN/CIERRE de posiciones
        abiertas sigue (Motor 3 no pasa por este gate). En modo estático o sin balance real
        todavía, NO bloquea (el sizing/exposición ya protegen)."""
        if not self.settings.DYNAMIC_CAPITAL_ENABLED:
            return True
        cached = RiskManager._cached_capital_usd
        if cached is None:
            return True
        capped = min(
            cached * (self.settings.CAPITAL_SAFETY_FACTOR_PCT / 100.0),
            self.settings.CAPITAL_CAP_USD,
        )
        return capped >= self.settings.CAPITAL_FLOOR_USD

    @classmethod
    def capital_status(cls) -> dict:
        """Resumen del capital para el endpoint /status: mode/raw/effective/paused. Best-effort:
        /status NUNCA debe 500 por el cálculo de capital → ante cualquier fallo, dict degradado."""
        try:
            rm = cls()
            return {
                "mode": "dynamic" if rm.settings.DYNAMIC_CAPITAL_ENABLED else "fixed",
                "raw_balance_usd": cls._last_raw_balance_usd,
                "effective_usd": round(rm._get_effective_capital_usd(), 2),
                "is_paused": not rm.can_open_new_positions(),
            }
        except Exception as exc:
            logger.warning(f"risk.capital_status falló: {type(exc).__name__}: {exc}")
            return {
                "mode": "unknown",
                "raw_balance_usd": cls._last_raw_balance_usd,
                "effective_usd": None,
                "is_paused": False,
            }

    @classmethod
    async def refresh_capital_from_balance(
        cls, *, client_factory: object | None = None
    ) -> float | None:
        """
        Trae el balance REAL de Kalshi (cash disponible) y actualiza la caché de CLASE.

        Corre FUERA del _check_lock (tarea de fondo del runner). **Best-effort:** si
        `get_balance()` falla (timeout/auth/5xx) NO crashea ni pisa la caché — mantiene el
        ÚLTIMO valor conocido y loguea WARNING. Devuelve el capital efectivo en USD, o None si
        falló y aún no había uno previo (los checks usarán ACTIVE_CAPITAL_USD como piso).
        """
        from src.clients.kalshi_rest import KalshiRestClient

        # Capital estático: no consultamos la API (dry-run / escudo). El check usa ACTIVE_CAPITAL_USD.
        if not get_settings().DYNAMIC_CAPITAL_ENABLED:
            return cls._cached_capital_usd

        factory = client_factory or KalshiRestClient
        try:
            async with factory() as client:  # type: ignore[operator]
                data = await client.get_balance()
            cents = data.get("balance") if isinstance(data, dict) else None
            if cents is None:
                raise ValueError(f"get_balance sin campo 'balance': {data!r}")
            usd = int(round(float(cents))) / 100.0  # {'balance': cents:int} → USD
            cls._last_raw_balance_usd = usd  # crudo, siempre (para /status)
            # Suavizado anti-churn: ignorar cambios menores al umbral (no actualizar la caché de
            # riesgo). Primer balance (prev None) o prev<=0 siempre actualiza.
            prev = cls._cached_capital_usd
            smoothing = get_settings().CAPITAL_SMOOTHING_PCT
            if prev is None or prev <= 0 or abs(usd - prev) / prev >= smoothing:
                cls._cached_capital_usd = usd
            else:
                logger.info(
                    f"risk.capital: cambio {abs(usd - prev) / prev * 100:.1f}% < suavizado "
                    f"({smoothing * 100:.0f}%) → se mantiene ${prev:.2f}"
                )
            cls._last_balance_at = datetime.now(UTC).replace(tzinfo=None)
            cls._capital_fallback_warned = False  # ya hay balance real → re-habilita el WARNING
            logger.info(f"risk.capital: balance real de Kalshi = ${usd:.2f} (cash disponible)")
            await cls._check_capital_drift(usd)
            return usd
        except Exception as exc:
            last = cls._cached_capital_usd
            if last is not None:
                logger.warning(
                    f"risk.capital: get_balance falló ({type(exc).__name__}: {exc}) → se "
                    f"mantiene el último balance conocido ${last:.2f}"
                )
            else:
                logger.warning(
                    f"risk.capital: get_balance falló ({type(exc).__name__}: {exc}) y no hay "
                    "balance previo → los checks usarán ACTIVE_CAPITAL_USD (piso de seguridad)"
                )
            return last

    @classmethod
    async def _check_capital_drift(cls, real_usd: float) -> None:
        """
        C-03 — alerta (advisory) si el ACTIVE_CAPITAL_USD configurado en Coolify se desfasa del
        cash REAL de Kalshi. Es la señal de que el param quedó viejo (causa raíz del "no apuesta
        porque el cap no coincide con el cash"). NO cambia el sizing — solo avisa.

        Edge-triggered con histéresis: alerta al CRUZAR el umbral hacia arriba y se re-arma sólo
        cuando el desfase baja por debajo de (umbral − margen). Best-effort: corre en el refresh
        de fondo (fuera del _check_lock); cualquier fallo se loguea y NO rompe el refresh.
        """
        try:
            settings = get_settings()
            configured = settings.ACTIVE_CAPITAL_USD
            threshold = settings.CAPITAL_DRIFT_ALERT_PCT
            if configured <= 0:
                return
            drift_pct = abs(real_usd - configured) / configured * 100.0
            if drift_pct >= threshold:
                if not cls._drift_alerted:
                    cls._drift_alerted = True
                    direction = "MÁS" if real_usd > configured else "MENOS"
                    # ADAPTATIVO (fix 2026-07-01, screenshot del operador): con
                    # DYNAMIC_CAPITAL_ENABLED el param NO maneja el sizing (es solo el
                    # fallback de boot) → el desfase config↔cash es ESPERADO y no exige
                    # mantenimiento manual: log INFO one-shot, sin Telegram. La alerta
                    # accionable queda para dynamic OFF, donde el param SÍ dimensiona.
                    if settings.DYNAMIC_CAPITAL_ENABLED:
                        logger.info(
                            f"risk.capital.drift: cash real ${real_usd:.2f} vs config "
                            f"${configured:.2f} ({direction} cash, {drift_pct:.0f}%). "
                            "Informativo: el sizing usa el cash real; ACTIVE_CAPITAL_USD "
                            "es solo fallback de boot (actualizarlo es opcional)."
                        )
                        return
                    msg = (
                        f"*Desfase de capital*: cash real ${real_usd:.2f} vs "
                        f"ACTIVE_CAPITAL_USD=${configured:.2f} ({direction} cash, "
                        f"{drift_pct:.0f}% de desfase ≥ {threshold:.0f}%). "
                        "DYNAMIC_CAPITAL_ENABLED=False: el sizing usa ESTE param — "
                        "actualizalo en Coolify o revisá el movimiento de cash."
                    )
                    logger.warning(
                        f"risk.capital.drift: real=${real_usd:.2f} config=${configured:.2f} "
                        f"drift={drift_pct:.1f}% ≥ {threshold:.0f}% → alerta (dynamic OFF)"
                    )
                    await send_alert(msg, urgent=False)
                return
            # Histéresis robusta para cualquier umbral: el nivel de re-arme siempre queda en
            # (0, threshold) — con un umbral chico (ej. 3%) un margen fijo de 5 lo volvería
            # negativo y el alert jamás se re-armaría.
            rearm_level = threshold - min(cls._DRIFT_REARM_MARGIN_PCT, threshold / 2.0)
            if cls._drift_alerted and drift_pct < rearm_level:
                cls._drift_alerted = False
                logger.info(
                    f"risk.capital.drift: desfase {drift_pct:.1f}% volvió bajo el umbral → re-armado"
                )
        except Exception as exc:
            logger.warning(f"risk.capital.drift: chequeo falló ({type(exc).__name__}: {exc})")

    async def check_pre_trade(self, opp: ArbOpportunity) -> TradeDecision:
        """Gatekeeper crítico. Debe llamarse con await desde el executor.

        Serializado con _check_lock (ver docstring de clase): dos motores no pueden
        evaluar exposición simultáneamente y aprobarse mutuamente por encima del cap.
        """
        async with RiskManager._check_lock:
            return await self._check_pre_trade_locked(opp)

    async def check_and_reserve(
        self, opp: ArbOpportunity, persist_intent: Callable[[TradeDecision], bool]
    ) -> TradeDecision:
        """Check + persistencia del intent bajo el MISMO lock (deuda auditoría 2026-07-01).

        Cierra la ventana check→persist-intent para quien lo use: la fila intent queda
        escrita ANTES de soltar el lock, así el próximo check (de este u otro motor) ya la
        ve en la exposición. `persist_intent` recibe la decisión aprobada (para dimensionar
        la fila con max_allowed_count) y devuelve False si no pudo escribir → la decisión
        se degrada a rechazo (el caller NO debe operar sin rastro).

        El callback es sincrónico y corto (un INSERT); no meter I/O de red acá — bloquea
        el gatekeeper de todos los motores. Motor REST sigue usando check_pre_trade + su
        propio _persist_intents fuera del lock (residual documentado en el docstring de
        clase; migrarlo es un refactor aparte).
        """
        async with RiskManager._check_lock:
            decision = await self._check_pre_trade_locked(opp)
            if not decision.approved:
                return decision
            if not persist_intent(decision):
                return TradeDecision(False, "persist_intent_failed", 0)
            return decision

    async def _check_pre_trade_locked(self, opp: ArbOpportunity) -> TradeDecision:
        """Cuerpo del check (bajo lock). Lógica idéntica a la versión histórica."""
        if BotState.is_paused:
            reason = BotState.pause_reason or "Razón desconocida"
            return TradeDecision(False, f"BotState.is_paused activo: {reason}", 0)

        breached_period = await self._check_timeframe_stop_losses()
        if breached_period:
            return TradeDecision(
                approved=False,
                reason=f"Stop-Loss {breached_period} superado",
                max_allowed_count=0,
            )

        # Capital dinámico: si el cash real cayó bajo el piso, se PAUSAN las nuevas entradas
        # (la gestión/cierre de lo abierto NO pasa por este gate — Motor 3 no llama check_pre_trade).
        # Gate NUEVO e independiente: no toca el stop-loss ni el kill-switch.
        if not self.can_open_new_positions():
            return TradeDecision(
                False,
                f"Capital bajo el piso (${self.settings.CAPITAL_FLOOR_USD:.2f}): "
                "nuevas entradas en pausa",
                0,
            )

        # C-01: capital base = balance REAL de Kalshi (no el ACTIVE_CAPITAL_USD estático).
        capital_usd = self._get_effective_capital_usd()
        current_exposure_usd = self._get_current_exposure_usd()
        max_total_exposure_usd = capital_usd * (self.settings.MAX_SIMULTANEOUS_EXPOSURE_PCT / 100.0)
        remaining_exposure_usd = max_total_exposure_usd - current_exposure_usd
        if remaining_exposure_usd <= 0:
            return TradeDecision(
                False,
                f"Límite Exposición Simultánea ({self.settings.MAX_SIMULTANEOUS_EXPOSURE_PCT}%) alcanzado "
                f"(actual: ${current_exposure_usd:.2f})",
                0,
            )

        max_trade_usd = capital_usd * (self.settings.MAX_TRADE_SIZE_PCT / 100.0)
        # Cap ABSOLUTO anti-slippage: el USD comprometido por orden nunca supera
        # MAX_TRADE_SIZE_USD ($200), sin importar % ni capital. Combinado con
        # remaining_exposure y opp.count (liquidez real del book) abajo, el size final es
        # min(liquidez_book, kelly/%, $200).
        usable_usd = min(max_trade_usd, remaining_exposure_usd, self.settings.MAX_TRADE_SIZE_USD)

        total_cost_per_unit_cents = sum(leg.price_cents for leg in opp.legs)
        if total_cost_per_unit_cents <= 0:
            return TradeDecision(False, "Costo de oportunidad <= 0 (datos inválidos)", 0)

        # + fees por unidad (deuda auditoría 2026-07-01): el USD comprometido real incluye
        # la comisión — sin esto el count aprobado podía exceder usable_usd por el monto de
        # las fees. fee(1, p) por pata sobrestima por el ceil → dirección conservadora.
        fees_per_unit_cents = sum(kalshi_fee_cents(1, leg.price_cents) for leg in opp.legs)
        max_count_by_capital = int(usable_usd * 100) // (
            total_cost_per_unit_cents + fees_per_unit_cents
        )
        allowed_count = min(opp.count, max_count_by_capital)

        if allowed_count <= 0:
            return TradeDecision(
                False,
                f"Sizing final 0 contratos. usable=${usable_usd:.2f}, "
                f"cost/unit={total_cost_per_unit_cents}c, "
                f"exposición: actual=${current_exposure_usd:.2f}, "
                f"max=${max_total_exposure_usd:.2f}, restante=${remaining_exposure_usd:.2f}",
                0,
            )

        return TradeDecision(True, "Aprobado", allowed_count)

    def exposure_headroom_usd(self) -> float:
        """Headroom de exposición restante (USD): capital efectivo × MAX_SIMULTANEOUS
        _EXPOSURE_PCT − exposición actual (reservado + expuesto, todos los motores).
        Lo usa el Motor 5 F2 como gate pre-orden: cada quote nueva debe caber en el
        headroom (su fila pending reserva el capital para los demás motores)."""
        capital_usd = self._get_effective_capital_usd()
        max_total = capital_usd * (self.settings.MAX_SIMULTANEOUS_EXPOSURE_PCT / 100.0)
        return max_total - self._get_current_exposure_usd()

    def _get_current_exposure_usd(self) -> float:
        """
        Capital EN RIESGO en posiciones abiertas (pending/filled, todos los motores).

        FIX sobrestima (FASE 0.3): un arb COMPLETO fillado (ambas patas yes+no del
        MISMO ticker, mismo grupo arb_id) es una posición HEDGED — pase lo que pase
        paga 100¢/contrato → riesgo direccional CERO. Se descuenta el costo de los
        contratos EMPAREJADOS (min de counts por lado); el excedente sin pareja sigue
        contando entero. Solo se descuenta lo identificable con CERTEZA (grupos con
        arb_id de A.1, status='filled'); pending y patas sueltas cuentan completo —
        ante la duda, sobrestimar (frena antes, nunca después).
        """
        with get_session() as s:
            stmt = select(Trade).where(col(Trade.status).in_(["pending", "filled"]))
            active_trades = list(s.exec(stmt))

        if not active_trades:
            return 0.0

        # Motor 5 F2 — reservado vs expuesto: una fila 'pending' (orden RESTING) reserva
        # su count COMPLETO (conservador: puede llenarse entera en cualquier momento);
        # una 'filled' con filled_count (fill PARCIAL: el resto se canceló) expone SOLO
        # lo llenado. filled_count=None = semántica legacy (count entero).
        def _exposure_count(t: Trade) -> int:
            if t.status == "filled" and t.filled_count is not None:
                return t.filled_count
            return t.count

        total_cents = sum(t.price_cents * _exposure_count(t) for t in active_trades)

        # Descuento de arbs hedged: agrupar las FILLED con arb_id identificable.
        groups: dict[str, list[Trade]] = {}
        for t in active_trades:
            if t.status == "filled" and "arb_id=" in (t.notes or ""):
                groups.setdefault(arb_group_key(t), []).append(t)

        for arb_id, legs in groups.items():
            yes_legs = [t for t in legs if t.side == "yes"]
            no_legs = [t for t in legs if t.side == "no"]
            tickers = {t.ticker for t in legs}
            if yes_legs and no_legs and len(tickers) == 1:
                # Arb BINARIO hedged (yes+no del mismo ticker): descuenta el par.
                cnt_yes = sum(t.count for t in yes_legs)
                cnt_no = sum(t.count for t in no_legs)
                paired = min(cnt_yes, cnt_no)
                if paired <= 0:
                    continue
                # Costo promedio ponderado por lado × contratos emparejados.
                cost_yes = sum(t.price_cents * t.count for t in yes_legs) / cnt_yes
                cost_no = sum(t.price_cents * t.count for t in no_legs) / cnt_no
                total_cents -= int(paired * (cost_yes + cost_no))
                continue
            total_cents -= self._multi_outcome_hedge_discount_cents(legs=legs, arb_id=arb_id)

        return max(total_cents, 0) / 100.0

    def _multi_outcome_hedge_discount_cents(self, *, legs: list[Trade], arb_id: str) -> int:
        """
        Descuento de exposición de un arb MULTI-OUTCOME hedged (auditoría rentabilidad
        2026-07-07: el netting solo reconocía el caso binario intra-ticker — un
        winner-take-all completo de 3 patas YES, payout 100¢/set garantizado, contaba
        su notional BRUTO (~95¢/contrato) durante DÍAS hasta el settle y estrangulaba
        el headroom compartido, el mismo modo de falla del 30-jun arreglado solo para
        el binario).

        SOLO se descuenta lo identificable con CERTEZA (ante la duda, sobrestimar):
          - todas las patas del grupo son BUY YES de motor_rest_arb,
          - >= 3 tickers DISTINTOS, todos hermanos del MISMO evento,
          - NINGUNA fila del arb_id (cualquier status) quedó fuera de 'filled'/'settled'
            — una pata cancelled/pending/error significa set INCOMPLETO (mixto FILL+KILL)
            → riesgo direccional real, cuenta entero,
          - costo del set (Σ wavg por pata) < 100¢ (si no, no hay hedge que descontar).
        Descuento = min(counts por pata) × costo del set — el excedente sin pareja
        sigue contando entero. Best-effort: cualquier fallo devuelve 0 (sin descuento).
        """
        from src.strategies.motor_1_arbitrage.event_exposure import event_ticker_of

        if not legs or any(
            t.side != "yes" or t.action != "buy" or t.strategy != "motor_rest_arb" for t in legs
        ):
            return 0
        by_ticker: dict[str, list[Trade]] = {}
        for t in legs:
            by_ticker.setdefault(t.ticker, []).append(t)
        if len(by_ticker) < 3:
            return 0  # winner-take-all real = >=3 outcomes; con menos no hay certeza de hedge
        events = {event_ticker_of(tk) for tk in by_ticker}
        if len(events) != 1:
            return 0  # patas de eventos distintos: no es un set del mismo partido
        try:
            with get_session() as sess:
                siblings = list(
                    sess.exec(select(Trade).where(col(Trade.notes).contains(f"arb_id={arb_id}")))
                )
        except Exception:
            logger.exception("netting.multi.sibling_check_failed → sin descuento (conservador)")
            return 0
        if any(row.status not in ("filled", "settled") for row in siblings):
            return 0  # una pata no-fillada = set incompleto (KILL/pending) → direccional
        paired = min(sum(t.count for t in ts) for ts in by_ticker.values())
        if paired <= 0:
            return 0
        set_cost = sum(
            sum(t.price_cents * t.count for t in ts) / sum(t.count for t in ts)
            for ts in by_ticker.values()
        )
        if set_cost >= 100:
            return 0  # el set cuesta >= payout: no hay hedge que descontar
        return int(paired * set_cost)

    async def _check_timeframe_stop_losses(self) -> str | None:
        """
        Calcula PnL realizado (UTC) on-the-fly desde tabla Trade.
        Revisa límites diario, semanal (desde el Lunes) y mensual (desde el día 1).

        Returns:
            Nombre del periodo que disparó ('Diario'/'Semanal'/'Mensual') o None si OK.

        Solo cuenta trades con status='settled'. Trades fillados pero
        no settled NO cuentan (PnL no realizado).

        DECISIONES ARQUITECTÓNICAS (mayo 2026):

        1. CALENDAR WINDOWS (no rolling):
           - Daily:   desde 00:00 UTC del día actual
           - Weekly:  desde 00:00 UTC del lunes de esta semana
           - Monthly: desde 00:00 UTC del día 1 del mes actual
           Razón: alinea con cómo Kalshi reporta PnL en su dashboard.
           Trade-off: en transición de periodo el contador resetea (ej: viernes
           23:59 → sábado 00:01). Los 3 timeframes operan juntos como mitigación.

        2. NAIVE UTC DATETIMES:
           SQLite no preserva timezone info. Usamos naive UTC consistente para
           evitar TypeError: can't compare offset-naive and offset-aware datetimes.
           CONVENCIÓN para writes a Trade.settled_at y campos análogos:
               datetime.now(UTC).replace(tzinfo=None)   # correcto
           NUNCA:
               datetime.utcnow()                         # deprecated Python 3.12+
               datetime.now()                            # usa local timezone
               datetime.now(UTC)                         # aware → SQLite lo guarda mal
        """
        # SQLite retorna datetimes sin timezone info, usamos naive UTC para comparar
        now = datetime.now(UTC).replace(tzinfo=None)
        today_start = datetime.combine(now.date(), time.min)

        # Lunes de esta semana a las 00:00 UTC
        days_since_monday = now.weekday()
        week_start = datetime.combine(now.date() - timedelta(days=days_since_monday), time.min)

        # Día 1 de este mes a las 00:00 UTC
        month_start = datetime.combine(now.date().replace(day=1), time.min)

        # BUG FIX (borde de mes): en los primeros días de un mes, el LUNES de esta semana cae en
        # el MES ANTERIOR (ej. hoy 2026-07-01 → week_start 2026-06-29 < month_start 2026-07-01). Si
        # el rango base arrancara en month_start, el stop-loss SEMANAL se computaba sobre un set que
        # ya excluía esos días → SUBCONTABA la pérdida de la semana → protección semanal más débil
        # justo tras el rollover de mes. El rango base debe cubrir TODA la ventana (la más antigua).
        range_start = min(month_start, week_start)
        with get_session() as s:
            stmt = select(Trade).where(
                Trade.status == "settled",
                col(Trade.settled_at) >= range_start,
            )
            all_trades = list(s.exec(stmt))

        # Filtrar cada timeframe en memoria desde el set completo (cada ventana, independiente).
        monthly_trades = [t for t in all_trades if t.settled_at and t.settled_at >= month_start]
        weekly_trades = [t for t in all_trades if t.settled_at and t.settled_at >= week_start]
        daily_trades = [t for t in all_trades if t.settled_at and t.settled_at >= today_start]

        monthly_pnl_usd = sum((t.pnl_cents or 0) for t in monthly_trades) / 100.0
        weekly_pnl_usd = sum((t.pnl_cents or 0) for t in weekly_trades) / 100.0
        daily_pnl_usd = sum((t.pnl_cents or 0) for t in daily_trades) / 100.0

        # Orden MÁS SEVERO PRIMERO (mensual → semanal → diario): con el breach diario en modo
        # soft (no latchea), si un mismo día rompe también la ventana semanal/mensual, el
        # kill-switch persistente DEBE latchear — si el diario se evaluara primero, lo taparía.
        limits = [
            (
                "Mensual",
                monthly_pnl_usd,
                self.settings.MAX_MONTHLY_LOSS_PCT,
                self.settings.MAX_MONTHLY_LOSS_FLOOR_USD,
            ),
            (
                "Semanal",
                weekly_pnl_usd,
                self.settings.MAX_WEEKLY_LOSS_PCT,
                self.settings.MAX_WEEKLY_LOSS_FLOOR_USD,
            ),
            (
                "Diario",
                daily_pnl_usd,
                self.settings.MAX_DAILY_LOSS_PCT,
                self.settings.MAX_DAILY_LOSS_FLOOR_USD,
            ),
        ]

        # C-01: stop-losses sobre el capital base REAL (balance de Kalshi), no el estático.
        # Límite efectivo = max(capital × %, piso USD): con capital chico los % puros daban
        # límites a nivel de ruido ($5.40/día con $180) que apagaban todo el bot (2026-07-12).
        capital_usd = self._get_effective_capital_usd()
        for period_name, pnl_usd, max_pct, floor_usd in limits:
            max_loss_usd = max(capital_usd * (max_pct / 100.0), floor_usd)
            if pnl_usd < 0 and abs(pnl_usd) >= max_loss_usd:
                if period_name == "Diario" and self.settings.DAILY_STOP_ENTRIES_ONLY:
                    # Respuesta escalonada (2026-07-12): un día malo pausa SOLO las entradas
                    # nuevas y se auto-recupera en el rollover UTC (la ventana se recomputa
                    # de DB en cada check). NO latchea el kill-switch persistente ni
                    # BotState.is_paused — semanal/mensual (arriba) siguen siendo nucleares.
                    await self._notify_daily_stop(pnl_usd, max_loss_usd, max_pct)
                    return period_name
                await self._trigger_kill_switch(
                    f"Stop-Loss {period_name} superado: PnL=${pnl_usd:.2f}, "
                    f"límite=${-max_loss_usd:.2f} ({max_pct}% o piso ${floor_usd:.2f})"
                )
                return period_name

        return None

    # Anti-spam del aviso del stop diario soft: fecha UTC del último aviso (estado de CLASE,
    # mismo patrón que la caché de balance). El check corre en cada pre-trade y el breach
    # persiste todo el día → sin esto, una alerta por intento de entrada.
    _daily_stop_alert_date: date | None = None

    async def _notify_daily_stop(self, pnl_usd: float, max_loss_usd: float, max_pct: float) -> None:
        """Aviso del stop diario SOFT — one-shot por día UTC. Best-effort SIEMPRE: un fallo
        de alerta/DB no debe romper el check de riesgo (la entrada ya fue rechazada)."""
        today = datetime.now(UTC).date()
        if RiskManager._daily_stop_alert_date == today:
            return
        RiskManager._daily_stop_alert_date = today
        msg = (
            f"Stop-Loss Diario: PnL=${pnl_usd:.2f}, límite=${-max_loss_usd:.2f} ({max_pct}% "
            f"o piso). Entradas NUEVAS en pausa hasta el próximo día UTC (auto-recupera; "
            f"salidas/gestión siguen operando). Sin kill-switch."
        )
        logger.warning(f"risk.daily_stop: {msg}")
        try:
            with get_session() as s:
                s.add(
                    RiskEvent(
                        event_type="daily_stop",
                        severity="warning",
                        message=msg[:1000],
                        capital_at_event=self._get_effective_capital_usd(),
                    )
                )
                s.commit()
        except Exception:
            logger.exception("risk.daily_stop.risk_event_persist_failed")
        try:
            await alert_risk_event("daily_stop", msg)
        except Exception:
            logger.exception("risk.daily_stop.alert_failed")

    async def _trigger_kill_switch(self, reason: str) -> None:
        """Pausa el bot y notifica. Idempotente (no spamea si ya pausado)."""
        if BotState.is_paused:
            return

        BotState.is_paused = True
        BotState.pause_reason = reason
        # Persistir para sobrevivir restarts (Coolify unless-stopped). Best-effort: un
        # fallo de DB no debe impedir la alerta. NO cambia ninguna query/lógica de riesgo.
        try:
            engage_kill_switch(reason)
        except Exception:
            logger.exception("risk.kill_switch.persist_failed")
        # Auditoría: dejar rastro en risk_events (estaba vacío pese a las pausas). La
        # supervivencia a reinicios la da engage_kill_switch (OperationalState); esto es
        # SOLO el log de eventos. Best-effort independiente: su fallo no frena la alerta.
        try:
            with get_session() as s:
                s.add(
                    RiskEvent(
                        event_type="kill_switch",
                        severity="critical",
                        message=reason[:1000],
                        capital_at_event=self._get_effective_capital_usd(),
                    )
                )
                s.commit()
        except Exception:
            logger.exception("risk.kill_switch.risk_event_persist_failed")
        msg = f"KILL SWITCH: {reason}. Bot en pausa. Requiere intervención manual."
        logger.critical(msg)
        await alert_risk_event("kill_switch", msg)
