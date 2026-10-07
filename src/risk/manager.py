"""
Motor de Gestión de Riesgo (Fase 2 Motor 1).
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from zoneinfo import ZoneInfo

from loguru import logger
from sqlmodel import col, or_, select

from src.math.arbitrage import ArbLeg, ArbOpportunity
from src.math.balance import available_cash_usd
from src.math.fees import kalshi_fee_cents
from src.monitoring.health import BotState
from src.monitoring.telegram_alerts import alert_risk_event, send_alert
from src.risk.commitments import daily_committed_cents
from src.risk.experiment_policy import limits_for_capital
from src.risk.exposure import (
    MM_STRATEGY,
    contract_risk_cents,
    exposure_count,
    open_risk_cents,
)
from src.storage.models import RiskEvent, Trade, engage_kill_switch, get_session
from src.storage.quantities import filled_quantity
from src.strategies.motor_5_mm.incidents import incident_status
from src.strategies.motor_5_mm.order_state import COUNT_QUARANTINE_NOTE
from src.strategies.motor_rest_arb.settlement import arb_group_key
from src.utils.config import get_settings


@dataclass(frozen=True, slots=True)
class TradeDecision:
    approved: bool
    reason: str
    max_allowed_count: int


@dataclass(frozen=True, slots=True)
class _RiskMark:
    bid_cents: int | None
    ask_cents: int | None
    observed_at: datetime
    fee_multiplier: Fraction | None


def _exact_usd_cents(value: object) -> Fraction:
    """Coste observado: USD decimal exacto, hasta seis decimales, sin redondearlo."""
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError("coste USD debe ser texto decimal acotado")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("coste USD inválido") from exc
    if (
        not number.is_finite()
        or number < 0
        or number > Decimal("1000000000000000000")
        or number.as_tuple().exponent < -6
    ):
        raise ValueError("coste USD no representable")
    return Fraction(number) * 100


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

    def _experiment_enabled(self) -> bool:
        """Solo un bool True explícito activa el guard; mocks/ausencia nunca lo arman."""
        return getattr(self.settings, "EXPERIMENT_BANK200_ENABLED", False) is True

    def _requires_confirmed_cash(self) -> bool:
        return self.settings.KALSHI_ENV == "production" or self._experiment_enabled()

    def _fresh_cash_usd(self) -> float | None:
        """La última lectura cruda limita el suavizado; un retiro no aumenta el margen."""
        cached = RiskManager._cached_capital_usd
        raw = RiskManager._last_raw_balance_usd
        last = RiskManager._last_balance_at
        if cached is None or raw is None or last is None:
            return None
        if not all(math.isfinite(v) and v > 0 for v in (cached, raw)):
            return None
        age = (datetime.now(UTC).replace(tzinfo=None) - self._as_utc_naive(last)).total_seconds()
        ttl = self.settings.BALANCE_MAX_AGE_SECONDS
        if self._experiment_enabled():
            ttl = min(ttl, self.settings.EXPERIMENT_BALANCE_MAX_AGE_SEC)
        if age < 0 or age > ttl:
            return None
        return min(cached, raw)

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
        diagnóstico. Loguea WARNING una vez; este fallback no habilita entradas live.

        Capital dinámico: DYNAMIC_CAPITAL_ENABLED=False usa capital estático en demo;
        en producción lo limita además al cash confirmado. Sobre el cash se aplica techo (CAPITAL_CAP_USD) y
        piso (CAPITAL_FLOOR_USD) — el efectivo nunca baja del piso para no romper la matemática
        de riesgo; la PAUSA de nuevas entradas la decide can_open_new_positions().
        """
        # Sólo demo sin experimento puede usar capital estático sin observar cash.
        if not self.settings.DYNAMIC_CAPITAL_ENABLED and not self._requires_confirmed_cash():
            return self.settings.ACTIVE_CAPITAL_USD

        cached = RiskManager._cached_capital_usd
        if cached is None:
            if not RiskManager._capital_fallback_warned:
                RiskManager._capital_fallback_warned = True
                logger.warning(
                    "risk.capital: sin balance real de Kalshi todavía → fallback a "
                    f"ACTIVE_CAPITAL_USD=${self.settings.ACTIVE_CAPITAL_USD:.2f} (diagnóstico; no habilita entradas live)"
                )
            base = self.settings.ACTIVE_CAPITAL_USD
        else:
            if self._requires_confirmed_cash():
                raw = RiskManager._last_raw_balance_usd
                if raw is not None and math.isfinite(raw) and raw >= 0:
                    cached = min(cached, raw)
            # C-02: cash real × factor de seguridad, con techo y piso configurables.
            base = cached * (self.settings.CAPITAL_SAFETY_FACTOR_PCT / 100.0)
            base = min(base, self.settings.CAPITAL_CAP_USD)
            base = max(base, self.settings.CAPITAL_FLOOR_USD)

        if not self.settings.DYNAMIC_CAPITAL_ENABLED:
            base = min(base, self.settings.ACTIVE_CAPITAL_USD)

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
        """Producción exige cash fresco incluso en modo estático. No bloquea salidas.

        El fallback sigue disponible para diagnóstico, nunca autoriza entradas live.
        Demo fuera del perfil Bank200 conserva su simulación con capital estático.
        """
        if self._requires_confirmed_cash():
            cash = self._fresh_cash_usd()
            if cash is None:
                return False
            usable = min(
                cash * self.settings.CAPITAL_SAFETY_FACTOR_PCT / 100.0,
                self.settings.CAPITAL_CAP_USD,
            )
            if not self.settings.DYNAMIC_CAPITAL_ENABLED:
                usable = min(usable, self.settings.ACTIVE_CAPITAL_USD)
            return usable >= self.settings.CAPITAL_FLOOR_USD

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
                "is_paused": True,
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

        # Demo estático evita red; producción siempre necesita observar fondos.
        if not get_settings().DYNAMIC_CAPITAL_ENABLED and not cls()._requires_confirmed_cash():
            return cls._cached_capital_usd

        factory = client_factory or KalshiRestClient
        try:
            async with factory() as client:  # type: ignore[operator]
                data = await client.get_balance()
            usd = float(available_cash_usd(data))
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
            # Observabilidad EN VIVO de los frenos (auditoría 2026-07-17): pasajero del
            # refresh periódico — el % usado de cada ventana queda en el log ANTES de que
            # dispare, no solo post-mortem. Best-effort adentro; jamás rompe el refresh.
            cls.log_stop_loss_status()
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
                    "balance previo → producción mantiene nuevas entradas bloqueadas"
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

    @staticmethod
    def _as_utc_naive(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value
        return value.astimezone(UTC).replace(tzinfo=None)

    def _experiment_start_utc_naive(self) -> datetime | None:
        raw = self.settings.EXPERIMENT_START_AT.strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC).replace(tzinfo=None)

    def _experiment_accounting_snapshot(self) -> dict:
        """Ledger derivado para el bank-200; no hace red y no muta estado."""
        if not self._experiment_enabled():
            return {}

        start = self._experiment_start_utc_naive()
        if start is None:
            return {"valid": False, "reason": "EXPERIMENT_START_AT inválido o ausente"}

        now_utc = datetime.now(UTC)
        la = ZoneInfo("America/Los_Angeles")
        now_la = now_utc.astimezone(la)
        week_date = now_la.date() - timedelta(days=now_la.weekday())
        week_start_la = datetime.combine(week_date, time.min, tzinfo=la)
        week_start = week_start_la.astimezone(UTC).replace(tzinfo=None)

        with get_session() as s:
            try:
                committed_cents = daily_committed_cents(s, start=start, now=now_utc)
            except ValueError as exc:
                return {"valid": False, "reason": str(exc)}
            settled = list(
                s.exec(select(Trade).where(Trade.status == "settled", Trade.settled_at >= start))
            )

        weekly_cents = 0
        cumulative_cents = 0
        for trade in settled:
            if trade.settled_at is None:
                continue
            settled_at = self._as_utc_naive(trade.settled_at)
            if settled_at < start:
                continue
            pnl = trade.pnl_cents or 0
            cumulative_cents += pnl
            if settled_at >= week_start:
                weekly_cents += pnl

        limits = limits_for_capital(self._get_effective_capital_usd())
        return {
            "valid": True,
            "start_utc": start.isoformat(),
            "daily_new_risk_usd": committed_cents / 100.0,
            "weekly_pnl_usd": weekly_cents / 100.0,
            "cumulative_pnl_usd": cumulative_cents / 100.0,
            "limits": limits,
        }

    async def _check_experiment_stops(self) -> str | None:
        if not self._experiment_enabled():
            return None
        snap = self._experiment_accounting_snapshot()
        if not snap.get("valid"):
            return str(snap.get("reason") or "experiment ledger inválido")

        weekly = float(snap["weekly_pnl_usd"])
        cumulative = float(snap["cumulative_pnl_usd"])
        if cumulative <= -20.0:
            reason = f"Experimento bank-200 alcanzó pérdida acumulada ${cumulative:.2f} <= -$20.00"
            await self._trigger_kill_switch(reason)
            return reason
        if weekly <= -12.0:
            reason = f"Experimento bank-200 alcanzó pérdida semanal ${weekly:.2f} <= -$12.00"
            await self._trigger_kill_switch(reason)
            return reason

        limits = snap["limits"]
        daily = float(snap["daily_new_risk_usd"])
        daily_cap = float(limits.max_daily_new_risk_usd)
        if daily >= daily_cap:
            return f"Presupuesto diario bank-200 consumido: ${daily:.2f} / ${daily_cap:.2f}"
        return None

    async def check_pre_trade(self, opp: ArbOpportunity) -> TradeDecision:
        """Gatekeeper crítico. Debe llamarse con await desde el executor.

        Serializado con _check_lock (ver docstring de clase): dos motores no pueden
        evaluar exposición simultáneamente y aprobarse mutuamente por encima del cap.
        """
        async with RiskManager._check_lock:
            return await self._check_pre_trade_locked(opp)

    @staticmethod
    def _shared_incident_block() -> TradeDecision | None:
        """Una phantom M5 sin fila también compromete el capital de otros motores.

        El latch bloquea sólo gates de entrada: no inventa exposición numérica ni
        modifica las rutas de cancelación/gestión. Una lectura fallida bloquea.
        """
        blocked, reason = incident_status()
        if blocked:
            return TradeDecision(False, f"Exposición M5 incierta: {reason}", 0)
        return None

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
            incident_block = self._shared_incident_block()
            if incident_block is not None:
                return incident_block
            if not persist_intent(decision):
                return TradeDecision(False, "persist_intent_failed", 0)
            return decision

    def m1_reservation_limits(self) -> dict:
        """Foto síncrona para revalidar admisión M1 bajo BEGIN IMMEDIATE, sin red."""
        if not self.can_open_new_positions():
            raise ValueError("M1 sin capital confirmado sobre el piso")
        capital = self._get_effective_capital_usd()
        limits = {
            "max_open_cents": math.floor(capital * self.settings.MAX_SIMULTANEOUS_EXPOSURE_PCT),
            "event_cap_cents": math.floor(self.settings.MAX_EVENT_DIRECTIONAL_EXPOSURE_USD * 100),
        }
        if self._experiment_enabled():
            start = self._experiment_start_utc_naive()
            if start is None:
                raise ValueError("inicio experimental inválido")
            policy = limits_for_capital(capital)
            limits.update(
                max_open_cents=min(limits["max_open_cents"], int(policy.max_open_usd * 100)),
                max_daily_cents=int(policy.max_daily_new_risk_usd * 100),
                experiment_start=start,
            )
        return limits

    def _confirmed_quote_capital_usd(self) -> float | None:
        """M5 sólo abre contra cash observado fresco, nunca contra piso/fallback.

        El valor crudo también limita el suavizado: un retiro pequeño no puede
        conservar capacidad ficticia. Esto no cambia los clamps de otros motores.
        """
        cash = self._fresh_cash_usd()
        if cash is None:
            return None
        confirmed = cash * self.settings.CAPITAL_SAFETY_FACTOR_PCT / 100.0
        confirmed = min(confirmed, self.settings.CAPITAL_CAP_USD)
        if confirmed < self.settings.CAPITAL_FLOOR_USD:
            return None
        return min(confirmed, self._get_effective_capital_usd())

    def can_quote_new_positions(self) -> bool:
        """Permiso de cash M5 compartido con su gate de reservas, incluso en demo.

        Permite retirar quotes resting al perder una lectura confirmada; no sustituye
        stops, incidentes ni el check atómico previo a cada nueva orden.
        """
        return self._confirmed_quote_capital_usd() is not None

    async def check_quote_and_reserve(
        self,
        trade: Trade,
        persist_intent: Callable[[], bool],
        *,
        max_exposure_usd: float | None = None,
    ) -> TradeDecision:
        """M5: gate + canary + INSERT bajo el lock compartido, antes de la red.

        La quote se acepta completa o se rechaza. El lock cubre un proceso; los
        motores con check_pre_trade e INSERT separado aún requieren migración.
        """
        async with RiskManager._check_lock:
            try:
                if trade.strategy != MM_STRATEGY or trade.status != "pending":
                    return TradeDecision(False, "Intent M5 inválido", 0)
                risk_cents = open_risk_cents(trade)
                capital = self._confirmed_quote_capital_usd()
                if capital is None:
                    return TradeDecision(False, "M5 sin capital confirmado fresco sobre el piso", 0)
                unit_risk = contract_risk_cents(
                    action=trade.action, side=trade.side, price_cents=trade.price_cents
                )
                risk_side = (
                    trade.side
                    if trade.action == "buy"
                    else ("no" if trade.side == "yes" else "yes")
                )
                # El gate existente recibe el BUY equivalente; incluye sus mismos
                # stops, exposición, sizing y fees, sin permitir count parcial.
                opp = ArbOpportunity(
                    legs=(ArbLeg(trade.ticker, risk_side, unit_risk, trade.count, trade.count),),
                    count=trade.count,
                    gross_profit_cents=0,
                    fees_cents=0,
                    net_profit_cents=0,
                    edge_pct=0.0,
                )
                decision = await self._check_pre_trade_locked(opp, capital_usd=capital)
                if not decision.approved:
                    return decision
                # Los stops pueden esperar I/O de alertas. Una pausa/retiro en esa
                # espera invalida el permiso antes de persistir la nueva reserva.
                current_capital = self._confirmed_quote_capital_usd()
                if BotState.is_paused or current_capital is None or current_capital < capital:
                    return TradeDecision(False, "M5 capital/pausa cambió durante el check", 0)
                if decision.max_allowed_count < trade.count:
                    return TradeDecision(False, "M5 quote completa excede sizing disponible", 0)
                if max_exposure_usd is not None:
                    if not math.isfinite(max_exposure_usd) or max_exposure_usd < 0:
                        return TradeDecision(False, "M5 canary cap inválido", 0)
                    with get_session() as session:
                        rows = list(
                            session.exec(
                                select(Trade).where(
                                    Trade.strategy == MM_STRATEGY,
                                    col(Trade.status).notin_(["settled", "cancelled"]),
                                )
                            )
                        )
                    if sum(open_risk_cents(row) for row in rows) + risk_cents > int(
                        max_exposure_usd * 100
                    ):
                        return TradeDecision(False, "M5 canary cap alcanzado", 0)
            except Exception:
                logger.exception("risk.quote_check_failed → no se autoriza riesgo nuevo")
                return TradeDecision(False, "M5 riesgo/capital desconocido", 0)
            # El callback debe persistir o devolver False. Sus errores de latch no
            # se silencian acá: el executor/supervisor deben ver el fallo durable.
            incident_block = self._shared_incident_block()
            if incident_block is not None:
                return incident_block
            if not persist_intent():
                return TradeDecision(False, "persist_intent_failed", 0)
            return decision

    async def _check_pre_trade_locked(
        self, opp: ArbOpportunity, *, capital_usd: float | None = None
    ) -> TradeDecision:
        """Cuerpo del check (bajo lock). Lógica idéntica a la versión histórica."""
        if BotState.is_paused:
            reason = BotState.pause_reason or "Razón desconocida"
            return TradeDecision(False, f"BotState.is_paused activo: {reason}", 0)

        incident_block = self._shared_incident_block()
        if incident_block is not None:
            return incident_block

        experiment_block = await self._check_experiment_stops()
        if experiment_block:
            return TradeDecision(False, experiment_block, 0)

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
            if self._requires_confirmed_cash():
                reason = (
                    "Sin balance Kalshi fresco o capital bajo el piso: nuevas entradas en pausa"
                )
            else:
                reason = (
                    f"Capital bajo el piso (${self.settings.CAPITAL_FLOOR_USD:.2f}): "
                    "nuevas entradas en pausa"
                )
            return TradeDecision(False, reason, 0)

        # Gate SOFT de pérdida latente (MTM) — solo con UNREALIZED_STOP_ENABLED (default
        # off: cambia la semántica realized-only, decisión del owner). Rechaza entradas
        # nuevas; salidas/gestión siguen (no pasan por este check).
        unrealized_reason = self._check_unrealized_stop()
        if unrealized_reason:
            return TradeDecision(False, unrealized_reason, 0)

        # C-01: capital base = balance REAL de Kalshi (no el ACTIVE_CAPITAL_USD estático).
        if capital_usd is None:
            capital_usd = self._get_effective_capital_usd()
        elif self._requires_confirmed_cash():
            if not math.isfinite(capital_usd) or capital_usd <= 0:
                return TradeDecision(False, "Capital propuesto inválido", 0)
            capital_usd = min(capital_usd, self._get_effective_capital_usd())
        current_exposure_usd = self._get_current_exposure_usd()
        max_total_exposure_usd = capital_usd * (self.settings.MAX_SIMULTANEOUS_EXPOSURE_PCT / 100.0)
        daily_remaining_usd = float("inf")
        experiment_limits = None
        if self._experiment_enabled():
            experiment_limits = limits_for_capital(capital_usd)
            max_total_exposure_usd = min(
                max_total_exposure_usd,
                float(experiment_limits.max_open_usd),
            )
            snap = self._experiment_accounting_snapshot()
            if not snap.get("valid"):
                return TradeDecision(
                    False, str(snap.get("reason") or "experiment ledger inválido"), 0
                )
            daily_remaining_usd = max(
                0.0,
                float(experiment_limits.max_daily_new_risk_usd) - float(snap["daily_new_risk_usd"]),
            )

        remaining_exposure_usd = max_total_exposure_usd - current_exposure_usd
        if remaining_exposure_usd <= 0:
            return TradeDecision(
                False,
                f"Límite Exposición Simultánea ({self.settings.MAX_SIMULTANEOUS_EXPOSURE_PCT}%) alcanzado "
                f"(actual: ${current_exposure_usd:.2f})",
                0,
            )

        max_trade_usd = capital_usd * (self.settings.MAX_TRADE_SIZE_PCT / 100.0)
        if experiment_limits is not None:
            max_trade_usd = min(max_trade_usd, float(experiment_limits.max_per_operation_usd))

        # Cap ABSOLUTO anti-slippage + exposición + presupuesto de riesgo nuevo del día.
        # En bank-200, daily_remaining NO se repone por cierres/ganancias del mismo día.
        usable_usd = min(
            max_trade_usd,
            remaining_exposure_usd,
            self.settings.MAX_TRADE_SIZE_USD,
            daily_remaining_usd,
        )

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

        # Los stops de arriba pueden esperar alertas. El incidente compartido se
        # vuelve a consultar después de todos los awaits, justo antes de aprobar.
        incident_block = self._shared_incident_block()
        if incident_block is not None:
            return incident_block
        return TradeDecision(True, "Aprobado", allowed_count)

    def exposure_headroom_usd(self) -> float:
        """Headroom de exposición restante (USD): capital efectivo × MAX_SIMULTANEOUS
        _EXPOSURE_PCT − exposición actual (reservado + expuesto, todos los motores).
        Lo usa el Motor 5 F2 como gate pre-orden: cada quote nueva debe caber en el
        headroom (su fila pending reserva el capital para los demás motores)."""
        capital_usd = self._get_effective_capital_usd()
        max_total = capital_usd * (self.settings.MAX_SIMULTANEOUS_EXPOSURE_PCT / 100.0)
        if self._experiment_enabled():
            max_total = min(max_total, float(limits_for_capital(capital_usd).max_open_usd))
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
        try:
            with get_session() as s:
                stmt = select(Trade).where(
                    or_(
                        col(Trade.status).in_(["pending", "filled"]),
                        (Trade.strategy == MM_STRATEGY)
                        & col(Trade.status).notin_(["settled", "cancelled"]),
                    )
                )
                active_trades = list(s.exec(stmt))
            total_cents = sum(open_risk_cents(trade) for trade in active_trades)
        except Exception:
            logger.exception("risk.exposure_unknown → bloquea riesgo adicional")
            return float("inf")

        if not active_trades:
            return 0.0

        # Descuento de arbs hedged: agrupar las FILLED con arb_id identificable.
        groups: dict[str, list[Trade]] = {}
        for t in active_trades:
            if (
                t.status == "filled"
                and t.action == "buy"
                and t.strategy in ("motor_1_arbitrage", "motor_rest_arb")
                and "arb_id=" in (t.notes or "")
            ):
                groups.setdefault(arb_group_key(t), []).append(t)

        for arb_id, legs in groups.items():
            yes_legs = [t for t in legs if t.side == "yes"]
            no_legs = [t for t in legs if t.side == "no"]
            tickers = {t.ticker for t in legs}
            if yes_legs and no_legs and len(tickers) == 1:
                # Arb BINARIO hedged (yes+no del mismo ticker): descuenta el par.
                cnt_yes = sum(exposure_count(t) for t in yes_legs)
                cnt_no = sum(exposure_count(t) for t in no_legs)
                paired = min(cnt_yes, cnt_no)
                if paired <= 0:
                    continue
                # Costo promedio ponderado por lado × contratos emparejados.
                cost_yes = sum(open_risk_cents(t) for t in yes_legs) / cnt_yes
                cost_no = sum(open_risk_cents(t) for t in no_legs) / cnt_no
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
        paired = min(sum(exposure_count(t) for t in ts) for ts in by_ticker.values())
        if paired <= 0:
            return 0
        set_cost = sum(
            sum(open_risk_cents(t) for t in ts) / sum(exposure_count(t) for t in ts)
            for ts in by_ticker.values()
        )
        if set_cost >= 100:
            return 0  # el set cuesta >= payout: no hay hedge que descontar
        return int(paired * set_cost)

    def _stop_loss_snapshot(self) -> dict:
        """
        Foto ÚNICA de todas las ventanas de stop-loss: PnL settled + límite efectivo +
        % usado, por ventana (rolling / mensual / semanal / diario, ordenadas MÁS severa
        primero) + PnL del mes por motor.

        Es LA fuente compartida entre el check que dispara (arriba) y la observabilidad
        (stop_loss_status → dashboard, /pnl, log sl_status): la auditoría 2026-07-17
        encontró el dashboard mostrando "796% consumido" mientras el freno decía OK —
        comparaba una ventana ROLLING de 30 días contra el límite MENSUAL y sin pisos.
        Dos matemáticas separadas SIEMPRE terminan divergiendo; acá queda una sola.

        El rolling se COMPUTA siempre (observabilidad); si ROLLING_DRAWDOWN_STOP_ENABLED
        es False, la ventana sale con gate_disabled=True y el check la salta (se ve el
        drawdown venir sin que frene — el freno lo decide el operador con el flag).
        """
        now = datetime.now(UTC).replace(tzinfo=None)
        today_start = datetime.combine(now.date(), time.min)
        week_start = datetime.combine(now.date() - timedelta(days=now.weekday()), time.min)
        month_start = datetime.combine(now.date().replace(day=1), time.min)
        rolling_days = self.settings.MAX_ROLLING_DRAWDOWN_DAYS
        rolling_start = now - timedelta(days=rolling_days)

        # BUG FIX (borde de mes, 2026-07-01): el rango base cubre TODA la ventana más
        # antigua — el lunes puede caer en el mes anterior y el rolling siempre es el
        # más largo. Filtrar cada ventana en memoria desde el set completo.
        range_start = min(month_start, week_start, rolling_start)
        with get_session() as s:
            stmt = select(Trade).where(
                Trade.status == "settled",
                col(Trade.settled_at) >= range_start,
            )
            all_trades = list(s.exec(stmt))

        def _pnl_since(start: datetime) -> float:
            return (
                sum(
                    (t.pnl_cents or 0) for t in all_trades if t.settled_at and t.settled_at >= start
                )
                / 100.0
            )

        # C-01: límites sobre el capital base REAL. Límite efectivo = max(capital × %,
        # piso USD) — con capital chico los % puros daban límites de ruido (2026-07-12).
        capital_usd = self._get_effective_capital_usd()

        def _window(name: str, pnl: float, pct: float, floor: float, disabled: bool = False):
            limit = max(capital_usd * (pct / 100.0), floor)
            used = (abs(pnl) / limit * 100.0) if (pnl < 0 and limit > 0) else 0.0
            return {
                "name": name,
                "pnl_usd": pnl,
                "pct": pct,
                "floor_usd": floor,
                "limit_usd": limit,
                "used_pct": used,
                "gate_disabled": disabled,
            }

        s_ = self.settings
        windows = [
            _window(
                f"Rolling{rolling_days}d",
                _pnl_since(rolling_start),
                s_.MAX_ROLLING_DRAWDOWN_PCT,
                s_.MAX_ROLLING_DRAWDOWN_FLOOR_USD,
                disabled=not s_.ROLLING_DRAWDOWN_STOP_ENABLED,
            ),
            _window(
                "Mensual",
                _pnl_since(month_start),
                s_.MAX_MONTHLY_LOSS_PCT,
                s_.MAX_MONTHLY_LOSS_FLOOR_USD,
            ),
            _window(
                "Semanal",
                _pnl_since(week_start),
                s_.MAX_WEEKLY_LOSS_PCT,
                s_.MAX_WEEKLY_LOSS_FLOOR_USD,
            ),
            _window(
                "Diario",
                _pnl_since(today_start),
                s_.MAX_DAILY_LOSS_PCT,
                s_.MAX_DAILY_LOSS_FLOOR_USD,
            ),
        ]

        # Por-motor (mes calendario): el stop-loss es GLOBAL y el neto ENMASCARA qué motor
        # sangra (M2 −$432 mientras M1 +$34). Observabilidad, no gate.
        per_motor: dict[str, float] = {}
        for t in all_trades:
            if t.settled_at and t.settled_at >= month_start:
                per_motor[t.strategy] = per_motor.get(t.strategy, 0.0) + (t.pnl_cents or 0) / 100.0

        return {"capital_usd": capital_usd, "windows": windows, "per_motor_month": per_motor}

    def stop_loss_status(self) -> dict:
        """Snapshot read-only para dashboard//pnl/log — la MISMA matemática del freno, sin
        efectos. Best-effort: nunca debe romper a su consumidor (dashboard, /status)."""
        try:
            return self._stop_loss_snapshot()
        except Exception as exc:
            logger.warning(f"risk.stop_loss_status falló: {type(exc).__name__}: {exc}")
            return {"capital_usd": None, "windows": [], "per_motor_month": {}}

    @classmethod
    def log_stop_loss_status(cls) -> None:
        """Una línea INFO `risk.sl_status` con el % usado de CADA freno + PnL del mes por
        motor. Pasajero del refresh de balance (ya periódico) — el estado de los frenos
        se ve EN VIVO en los logs, no solo post-mortem cuando ya disparó. Best-effort."""
        try:
            snap = cls().stop_loss_status()
            if not snap["windows"]:
                return
            parts = [
                f"{w['name']}={w['pnl_usd']:+.2f}/{-w['limit_usd']:.2f} ({w['used_pct']:.0f}%"
                + (" gate_off)" if w["gate_disabled"] else ")")
                for w in snap["windows"]
            ]
            motors = " ".join(f"{k}={v:+.2f}" for k, v in sorted(snap["per_motor_month"].items()))
            logger.info(
                f"risk.sl_status capital=${snap['capital_usd']:.2f} "
                + " ".join(parts)
                + (f" | mes_por_motor: {motors}" if motors else "")
            )
        except Exception as exc:
            logger.warning(f"risk.sl_status falló: {type(exc).__name__}: {exc}")

    # Gate SOFT: sólo habilitado explícitamente. Datos incompletos bloquean entradas,
    # nunca salidas ni reconciliación. Un mark es BBO observable, no fair ni midpoint.
    _marks: dict[tuple[str, str], _RiskMark] = {}
    _unrealized_alert_date: date | None = None

    @classmethod
    def record_mark(
        cls,
        ticker: str,
        side: str,
        bid_cents: int | None,
        *,
        ask_cents: int | None = None,
        observed_at: datetime | None = None,
        fee_multiplier: int | float | str | Fraction | None = None,
    ) -> None:
        """Publica BBO real y fee de cierre verificada, sin I/O adicional.

        BUY se liquida al bid, SELL se cubre al ask. Un libro parcial no inventa
        el lado ausente. None/None o evidencia inválida invalida el mark anterior;
        conservarlo rejuvenecido escondería la pérdida de cobertura. Los callers
        legacy con sólo bid siguen siendo compatibles, pero su fee desconocida
        bloquea MTM cuando el gate está habilitado.
        """
        if not isinstance(ticker, str) or not ticker or side not in ("yes", "no"):
            logger.warning("risk.record_mark_invalid_identity")
            return
        key = (ticker, side)
        try:
            for price in (bid_cents, ask_cents):
                if price is not None and (type(price) is not int or not 1 <= price <= 99):
                    raise ValueError("precio de mark inválido")
            if bid_cents is None and ask_cents is None:
                cls._marks.pop(key, None)
                return
            if bid_cents is not None and ask_cents is not None and bid_cents > ask_cents:
                raise ValueError("BBO cruzado")
            moment = cls._as_utc_naive(observed_at or datetime.now(UTC))
            if moment > datetime.now(UTC).replace(tzinfo=None):
                raise ValueError("mark futuro")
            multiplier = None
            if fee_multiplier is not None:
                if isinstance(fee_multiplier, bool):
                    raise ValueError("fee inválida")
                multiplier = Fraction(str(fee_multiplier))
                if multiplier <= 0:
                    raise ValueError("fee no positiva")
            existing = cls._marks.get(key)
            if isinstance(existing, _RiskMark) and moment < existing.observed_at:
                return  # una observación tardía no desplaza otra más reciente
            cls._marks[key] = _RiskMark(bid_cents, ask_cents, moment, multiplier)
            # Cache de diagnóstico acotada; retirar un mark sólo puede frenar entradas.
            if len(cls._marks) > 8192:
                oldest = min(cls._marks, key=lambda k: cls._marks[k].observed_at)
                cls._marks.pop(oldest, None)
        except Exception:  # el publicador conserva su ciclo; el gate detecta el hueco
            cls._marks.pop(key, None)
            logger.warning("risk.record_mark_invalid ticker={} side={}", ticker, side)

    @staticmethod
    def _entry_costs(trade: Trade, quantity: Fraction) -> tuple[Fraction, Fraction, bool]:
        """Bruto y fees de entrada en cents exactos; None nunca equivale a fee cero.

        Sin coste bruto observado usamos el límite como cota conservadora: coste
        máximo BUY / ingreso mínimo SELL. Esa cota no se persiste como fill real.
        La tarifa actual jamás reconstruye una comisión histórica desconocida.
        """
        fixed_fee = getattr(trade, "fees_usd_fp", None)
        legacy_fee = trade.fees_cents
        if legacy_fee is not None and (type(legacy_fee) is not int or legacy_fee < 0):
            raise ValueError("INVALID_ENTRY_FEES")
        if fixed_fee is not None:
            fees = _exact_usd_cents(fixed_fee)
            # Espejo legacy: versiones anteriores truncaban; el escritor nuevo
            # reserva hacia arriba. La verdad monetaria siempre es el FP exacto.
            if legacy_fee is not None and legacy_fee not in (math.floor(fees), math.ceil(fees)):
                raise ValueError("CONTRADICTORY_ENTRY_FEES")
        elif legacy_fee is not None:
            fees = Fraction(legacy_fee)
        else:
            raise ValueError("MISSING_ENTRY_FEES")
        fixed_cost = getattr(trade, "fill_cost_usd_fp", None)
        if fixed_cost is not None:
            gross = _exact_usd_cents(fixed_cost)
            if gross > 100 * quantity:
                raise ValueError("INVALID_ENTRY_COST")
            return gross, fees, False
        observed = trade.fill_price_cents
        price = trade.price_cents if observed is None else observed
        if type(price) is not int or not 1 <= price <= 99:
            raise ValueError("INVALID_ENTRY_PRICE")
        return price * quantity, fees, observed is None

    def _closing_mark(self, ticker: str, quantity: Fraction, now: datetime) -> tuple[int, Fraction]:
        """Una sola liquidación del neto YES; acepta el BBO NO complementario real."""
        ttl = self.settings.UNREALIZED_MARK_TTL_SEC
        if not isinstance(ttl, (int, float)) or not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("INVALID_MARK_TTL")
        problems = []
        for side in ("yes", "no"):
            mark = RiskManager._marks.get((ticker, side))
            if not isinstance(mark, _RiskMark):
                problems.append("MISSING_MARK")
                continue
            age = (now - mark.observed_at).total_seconds()
            if age < 0 or age > ttl:
                problems.append("STALE_MARK")
                continue
            # Un long YES se vende al bid YES o se cubre comprando NO al ask NO.
            price = (
                (mark.bid_cents if quantity > 0 else mark.ask_cents)
                if side == "yes"
                else (mark.ask_cents if quantity > 0 else mark.bid_cents)
            )
            if price is None:
                problems.append("MISSING_CLOSE_SIDE")
                continue
            if mark.fee_multiplier is None:
                problems.append("MISSING_CLOSE_FEE")
                continue
            return (price if side == "yes" else 100 - price), mark.fee_multiplier
        raise ValueError("/".join(dict.fromkeys(problems)))

    def _unrealized_snapshot(self) -> dict:
        """PnL de cierre conservador desde cantidades realmente ejecutadas.

        Normaliza BUY YES/SELL NO a +YES y SELL YES/BUY NO a -YES. Los pares
        compensados del mismo contrato quedan con su resultado bloqueado, sin
        cobrarles spread ni fee de cierre otra vez. El sobrante sí requiere mark.
        Un arb_id, por sí solo, no prueba que una pata esté cubierta.
        """
        now = datetime.now(UTC).replace(tzinfo=None)
        with get_session() as session:
            trades = list(
                session.exec(
                    select(Trade).where(
                        Trade.status != "settled",
                        col(Trade.closed_by_clv).is_(False),
                        or_(
                            col(Trade.status).in_(["filled", "pending"]),
                            Trade.filled_count > 0,
                            col(Trade.filled_count_fp).is_not(None),
                        ),
                    )
                )
            )
        cash: dict[str, Fraction] = {}
        net: dict[str, Fraction] = {}
        unknown = []
        limit_rows = 0
        executed_rows = 0
        for trade in trades:
            try:
                if COUNT_QUARANTINE_NOTE in (trade.notes or ""):
                    raise ValueError("QUANTITY_QUARANTINED")
                quantity = filled_quantity(trade)
                if quantity is None:
                    raise ValueError("QUANTITY_UNKNOWN")
                if not quantity:
                    continue
                if trade.side not in ("yes", "no") or trade.action not in ("buy", "sell"):
                    raise ValueError("INVALID_DIRECTION")
                gross, fees, limited = self._entry_costs(trade, quantity)
                sign = 1 if trade.action == "buy" else -1
                amount = -sign * gross - fees
                if trade.side == "no":
                    amount += sign * 100 * quantity
                    sign *= -1
                cash[trade.ticker] = cash.get(trade.ticker, Fraction(0)) + amount
                net[trade.ticker] = net.get(trade.ticker, Fraction(0)) + sign * quantity
                limit_rows += limited
                executed_rows += 1
            except (ValueError, TypeError, ArithmeticError) as exc:
                unknown.append(f"{trade.ticker}:{exc}")
        pnl = sum(cash.values(), Fraction(0))
        covered = 0
        for ticker, quantity in net.items():
            if not quantity:
                continue
            try:
                price, multiplier = self._closing_mark(ticker, quantity, now)
                pnl += quantity * price - kalshi_fee_cents(
                    abs(quantity), price, fee_multiplier=multiplier
                )
                covered += 1
            except (ValueError, TypeError, ArithmeticError) as exc:
                unknown.append(f"{ticker}:{exc}")
        return {
            "pnl_cents": None if unknown else pnl,
            "unknown": unknown,
            "covered": covered,
            "executed_rows": executed_rows,
            "limit_price_rows": limit_rows,
            "net_contracts": net,
        }

    def unrealized_entry_block_reason(self) -> str | None:
        """Mismo gate MTM para retirar quotes resting que podrían seguir llenándose.

        El engine debe publicar los marks primero y mantener la reconciliación y
        su refresco aunque este gate bloquee; no toca las rutas de salida.
        """
        return self._check_unrealized_stop()

    def _check_unrealized_stop(self) -> str | None:
        """Gate SOFT de entradas: cobertura completa o bloqueo, jamás marks inventados."""
        if not self.settings.UNREALIZED_STOP_ENABLED:
            return None
        try:
            snap = self._unrealized_snapshot()
        except Exception:
            logger.exception("risk.unrealized_unavailable")
            return "MTM sin valoración completa: lectura del ledger falló; entradas nuevas en pausa"
        if snap["unknown"]:
            detail = "; ".join(snap["unknown"][:3])
            return f"MTM sin valoración completa: {detail}; entradas nuevas en pausa"
        pnl_cents = snap["pnl_cents"]
        limit_usd = max(
            Fraction(str(self._get_effective_capital_usd()))
            * Fraction(str(self.settings.MAX_UNREALIZED_LOSS_PCT))
            / 100,
            Fraction(str(self.settings.MAX_UNREALIZED_LOSS_FLOOR_USD)),
        )
        if pnl_cents < 0 and -pnl_cents >= limit_usd * 100:
            # Comparación exacta; al mostrar una pérdida redondeamos hacia arriba,
            # nunca dejamos desaparecer fracciones adversas por el formato en USD.
            loss_cents = math.ceil(-pnl_cents)
            reason = (
                f"Pérdida latente ${-loss_cents / 100:.2f} ≥ límite ${-float(limit_usd):.2f} "
                f"({snap['covered']} posiciones netas con mark): entradas nuevas en pausa (MTM soft)"
            )
            today = datetime.now(UTC).date()
            if RiskManager._unrealized_alert_date != today:
                RiskManager._unrealized_alert_date = today
                logger.warning(f"risk.unrealized_stop: {reason}")
            return reason
        return None

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
        snapshot = self._stop_loss_snapshot()

        # Orden MÁS SEVERO PRIMERO (rolling → mensual → semanal → diario): con el breach
        # diario en modo soft (no latchea), si un mismo día rompe también una ventana
        # nuclear, el kill-switch persistente DEBE latchear — si el diario se evaluara
        # primero, lo taparía. El ROLLING va primero: es la ventana más larga (la sangría
        # gradual que las de calendario dejan pasar — auditoría 2026-07-17, M2 −$430
        # repartidos jun→jul sin un solo breach mensual).
        for window in snapshot["windows"]:
            period_name = window["name"]
            pnl_usd = window["pnl_usd"]
            max_loss_usd = window["limit_usd"]
            max_pct = window["pct"]
            floor_usd = window["floor_usd"]
            if window.get("gate_disabled"):
                continue
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
