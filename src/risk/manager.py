"""
Motor de Gestión de Riesgo (Fase 2 Motor 1).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from loguru import logger
from sqlmodel import col, select

from src.math.arbitrage import ArbOpportunity
from src.monitoring.health import BotState
from src.monitoring.telegram_alerts import alert_risk_event, send_alert
from src.storage.models import Trade, engage_kill_switch, get_session
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
    - Residual del lock: la ventana check→persist-intents sigue abierta (el lock
      cubre el CHECK, los intents los escribe el executor después). Peor caso con
      N motores en el timing exacto: overshoot de N×MAX_TRADE_SIZE_PCT sobre el cap
      de exposición (hoy N=1 motor con single-flight → residual teórico). Cierre
      total = reserva transaccional en el check; anotado como mejora si N crece.
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
        """
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
            # C-02: cash real × factor de seguridad.
            base = cached * (self.settings.CAPITAL_SAFETY_FACTOR_PCT / 100.0)

        # C-02: clamp ÚNICO al hard cap en producción — cubre AMBOS paths (cash real Y
        # fallback). Hoy el fallback ya respeta el techo vía el validator de config, pero
        # clampear acá lo hace robusto si alguien sube ese límite (no depende de otro archivo).
        if self.settings.KALSHI_ENV == "production":
            base = min(base, RiskManager.PROD_CAPITAL_HARD_CAP_USD)
        return base

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

        factory = client_factory or KalshiRestClient
        try:
            async with factory() as client:  # type: ignore[operator]
                data = await client.get_balance()
            cents = data.get("balance") if isinstance(data, dict) else None
            if cents is None:
                raise ValueError(f"get_balance sin campo 'balance': {data!r}")
            usd = int(round(float(cents))) / 100.0  # {'balance': cents:int} → USD
            cls._cached_capital_usd = usd
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
                    msg = (
                        f"*Desfase de capital*: cash real ${real_usd:.2f} vs "
                        f"ACTIVE_CAPITAL_USD=${configured:.2f} ({direction} cash, "
                        f"{drift_pct:.0f}% de desfase ≥ {threshold:.0f}%). "
                        "Actualizá el param en Coolify o revisá el movimiento de cash. "
                        "El sizing YA usa el cash real (C-01/C-02); esto es solo aviso."
                    )
                    logger.warning(
                        f"risk.capital.drift: real=${real_usd:.2f} config=${configured:.2f} "
                        f"drift={drift_pct:.1f}% ≥ {threshold:.0f}% → alerta"
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

        max_count_by_capital = int(usable_usd * 100) // total_cost_per_unit_cents
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

        total_cents = sum(t.price_cents * t.count for t in active_trades)

        # Descuento de arbs hedged: agrupar las FILLED con arb_id identificable.
        groups: dict[str, list[Trade]] = {}
        for t in active_trades:
            if t.status == "filled" and "arb_id=" in (t.notes or ""):
                groups.setdefault(arb_group_key(t), []).append(t)

        for legs in groups.values():
            yes_legs = [t for t in legs if t.side == "yes"]
            no_legs = [t for t in legs if t.side == "no"]
            if not yes_legs or not no_legs:
                continue  # pata suelta → riesgo direccional real, cuenta entera
            tickers = {t.ticker for t in legs}
            if len(tickers) != 1:
                continue  # patas en mercados distintos: NO es el arb binario hedged
            # Emparejar contratos: el hedge cubre min(count_yes, count_no) por lado.
            cnt_yes = sum(t.count for t in yes_legs)
            cnt_no = sum(t.count for t in no_legs)
            paired = min(cnt_yes, cnt_no)
            if paired <= 0:
                continue
            # Costo promedio ponderado por lado × contratos emparejados.
            cost_yes = sum(t.price_cents * t.count for t in yes_legs) / cnt_yes
            cost_no = sum(t.price_cents * t.count for t in no_legs) / cnt_no
            total_cents -= int(paired * (cost_yes + cost_no))

        return max(total_cents, 0) / 100.0

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

        with get_session() as s:
            # Traer todos los trades del mes actual (rango más amplio)
            stmt = select(Trade).where(
                Trade.status == "settled",
                col(Trade.settled_at) >= month_start,
            )
            monthly_trades = list(s.exec(stmt))

        # Filtrar trades para cada timeframe en memoria
        weekly_trades = [t for t in monthly_trades if t.settled_at and t.settled_at >= week_start]
        daily_trades = [t for t in weekly_trades if t.settled_at and t.settled_at >= today_start]

        monthly_pnl_usd = sum((t.pnl_cents or 0) for t in monthly_trades) / 100.0
        weekly_pnl_usd = sum((t.pnl_cents or 0) for t in weekly_trades) / 100.0
        daily_pnl_usd = sum((t.pnl_cents or 0) for t in daily_trades) / 100.0

        limits = [
            ("Diario", daily_pnl_usd, self.settings.MAX_DAILY_LOSS_PCT),
            ("Semanal", weekly_pnl_usd, self.settings.MAX_WEEKLY_LOSS_PCT),
            ("Mensual", monthly_pnl_usd, self.settings.MAX_MONTHLY_LOSS_PCT),
        ]

        # C-01: stop-losses sobre el capital base REAL (balance de Kalshi), no el estático.
        capital_usd = self._get_effective_capital_usd()
        for period_name, pnl_usd, max_pct in limits:
            max_loss_usd = capital_usd * (max_pct / 100.0)
            if pnl_usd < 0 and abs(pnl_usd) >= max_loss_usd:
                await self._trigger_kill_switch(
                    f"Stop-Loss {period_name} superado: PnL=${pnl_usd:.2f}, "
                    f"límite=${-max_loss_usd:.2f} ({max_pct}%)"
                )
                return period_name

        return None

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
        msg = f"KILL SWITCH: {reason}. Bot en pausa. Requiere intervención manual."
        logger.critical(msg)
        await alert_risk_event("kill_switch", msg)
