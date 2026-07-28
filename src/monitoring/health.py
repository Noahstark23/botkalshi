"""
Health server FastAPI.

Endpoints:
    GET  /health           - Para Coolify monitoring (liveness)
    GET  /ready            - Readiness probe (DB + WS conectado)
    GET  /status           - Dashboard detallado para Noel
    POST /admin/pause      - Pausa trading (no detiene container)
    POST /admin/resume     - Reanuda trading
    GET  /admin/stats      - Estadísticas operacionales
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from loguru import logger
from sqlmodel import select

from src.storage.models import DailyPnL, RiskEvent, Trade, get_session
from src.utils.config import get_settings


class BotState:
    """
    Estado global del bot accesible desde el health server.
    Servicios externos (capture, strategies) actualizan este estado.
    """

    started_at: datetime = datetime.now(UTC)
    last_ws_message: datetime | None = None
    ws_connected: bool = False
    is_paused: bool = False
    pause_reason: str | None = None
    capture_running: bool = False
    last_capture_running_true_at: float = 0.0  # time.monotonic() when capture_running last set True
    db_initialized: bool = False
    tracked_markets_count: int = 0
    last_error: str | None = None
    last_error_at: datetime | None = None
    v2_manager: Any = None  # Set by DataCaptureService when USE_ORDERBOOK_MANAGER_V2=True

    # TTL del last_error: un error mas viejo que esto se considera rancio y se limpia,
    # para que el dashboard no quede mostrando un error ya superado tras la recuperacion.
    LAST_ERROR_TTL_SEC: float = 900.0  # 15 min

    @classmethod
    def heartbeat(cls) -> None:
        cls.last_ws_message = datetime.now(UTC)

    @classmethod
    def record_error(cls, message: str) -> None:
        cls.last_error = message[:500]
        cls.last_error_at = datetime.now(UTC)

    @classmethod
    def current_error(cls) -> str | None:
        """
        last_error con TTL. Devuelve el error solo si es mas reciente que
        LAST_ERROR_TTL_SEC; si expiro, lo limpia y devuelve None.
        """
        if cls.last_error is None or cls.last_error_at is None:
            return None
        age = (datetime.now(UTC) - cls.last_error_at).total_seconds()
        if age > cls.LAST_ERROR_TTL_SEC:
            cls.last_error = None
            cls.last_error_at = None
            return None
        return cls.last_error


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Health server starting")
    yield
    logger.info("Health server stopping")


app = FastAPI(
    title="Kalshi Bot Health",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
)


# =====================================================
# Liveness / Readiness
# =====================================================


@app.get("/health")
async def health() -> dict[str, Any]:
    """
    Liveness: el container está vivo y respondiendo.
    Coolify usa esto para decidir si reiniciar.
    """
    now = datetime.now(UTC)
    uptime = (now - BotState.started_at).total_seconds()

    checks = {
        "alive": True,
        "not_paused": not BotState.is_paused,
        "uptime_ok": uptime > 0,
    }

    # ws_alive: si ya estamos capturando, vigilar silencio del WS.
    # Si aún estamos en discovery/startup (capture_running=False), dar gracia larga:
    # el backoff exponencial puede llegar a 300s/intento antes de que Kalshi libere
    # el rate limit, y no queremos que Coolify mate el container durante ese wait.
    if BotState.capture_running:
        if BotState.last_ws_message:
            ws_silence = (now - BotState.last_ws_message).total_seconds()
            checks["ws_alive"] = ws_silence < 300
        else:
            checks["ws_alive"] = False
    else:
        # Gracia de 30 min para discovery inicial.
        # Kalshi puede mantener rate-limit activo 20-30 min tras un burst de restarts.
        checks["ws_alive"] = uptime < 1800

    all_ok = all(checks.values())

    response = {
        "status": "healthy" if all_ok else "unhealthy",
        "uptime_seconds": int(uptime),
        "checks": checks,
        "timestamp": now.isoformat(),
    }

    if not all_ok:
        raise HTTPException(status_code=503, detail=response)

    return response


@app.get("/ready")
async def ready() -> dict[str, Any]:
    """
    Readiness: el bot está listo para tradear (DB lista, WS conectado).
    Diferente de /health - puedes estar healthy pero no ready.
    """
    checks = {
        "db_initialized": BotState.db_initialized,
        "capture_running": BotState.capture_running,
        "ws_alive": BotState.last_ws_message is not None,
    }

    if not all(checks.values()):
        raise HTTPException(status_code=503, detail={"ready": False, "checks": checks})

    return {"ready": True, "checks": checks}


# =====================================================
# Status dashboard
# =====================================================


@app.get("/status")
async def status() -> dict[str, Any]:
    """
    Dashboard detallado del bot.
    Para uso de Noel via curl o browser.
    """
    settings = get_settings()
    now = datetime.now(UTC)

    today_start = datetime(now.year, now.month, now.day, tzinfo=UTC)
    week_start = today_start - timedelta(days=7)

    with get_session() as s:
        trades_today = list(s.exec(select(Trade).where(Trade.placed_at >= today_start)).all())
        trades_week = list(s.exec(select(Trade).where(Trade.placed_at >= week_start)).all())

        last_pnl = s.exec(select(DailyPnL).order_by(DailyPnL.date.desc()).limit(1)).first()

        recent_risks = list(
            s.exec(
                select(RiskEvent)
                .where(RiskEvent.severity.in_(["warning", "critical"]))
                .order_by(RiskEvent.triggered_at.desc())
                .limit(5)
            ).all()
        )

    # V2 manager metrics. OJO (fix observabilidad 2026-07-17): el estado se reporta si la
    # INSTANCIA existe, NO si el flag USE_ORDERBOOK_MANAGER_V2 está on — porque Motor 1 crea
    # el manager sin el flag (data_capture: OR con MOTOR_1_ARBITRAGE_ENABLED). Antes, con el
    # flag off el status decía solo {"enabled": false} y ESCONDÍA books/recovery/sids
    # deshabilitados aunque el manager estuviera corriendo y sufriendo timeouts de recovery
    # → se volaba ciego sobre V2 (incidente sid=1 stale por request masiva).
    v2_enabled = settings.USE_ORDERBOOK_MANAGER_V2
    v2_mgr = BotState.v2_manager
    if v2_mgr is None:
        # Sin instancia: reportar el flag. Con el flag ON pero sin instancia = bug real.
        v2_info: dict[str, Any] = {"enabled": v2_enabled}
        if v2_enabled:
            BotState.record_error("v2 flag enabled but instance missing")
            v2_info["instance"] = "missing"
    else:
        s = v2_mgr.stats()
        v2_info = {
            "enabled": v2_enabled,  # el flag (compat) — puede ser False con el manager corriendo
            "running": True,  # la instancia EXISTE y procesa books (aunque el flag esté off)
            "books_initialized": s.get("initialized_tickers", 0),
            "sids_tracked": len(v2_mgr._tickers_by_sid),
            "sids_recovering": len(v2_mgr._recovering),
            # sids en circuit breaker (recovery deshabilitada → books STALE hasta que venza el
            # backoff de reintento, 2026-07-21; antes era hasta el redeploy): el dato que faltaba
            # para ver el sid=1 muerto por timeout_x5. retry_in = countdown del próximo reintento.
            "sids_disabled": sorted(v2_mgr._recovery_disabled_sids),
            "recovery_retry_in_sec": s.get("recovery_retry_in_sec", {}),
            "gaps_last_60s": s.get("gaps_last_60s", 0),
            "last_gap_at": s.get("last_gap_at"),
        }

    # Local: el RiskManager importa BotState de este módulo → import diferido evita el ciclo.
    from src.risk.manager import RiskManager

    return {
        "bot": {
            "started_at": BotState.started_at.isoformat(),
            "uptime_hours": round((now - BotState.started_at).total_seconds() / 3600, 2),
            "is_paused": BotState.is_paused,
            "pause_reason": BotState.pause_reason,
            "capture_running": BotState.capture_running,
            "ws_connected": (
                BotState.last_ws_message is not None
                and (now - BotState.last_ws_message).total_seconds() < 60
            ),
            "last_ws_message": (
                BotState.last_ws_message.isoformat() if BotState.last_ws_message else None
            ),
            "tracked_markets": BotState.tracked_markets_count,
            "last_error": BotState.current_error(),
            "last_error_at": (
                BotState.last_error_at.isoformat() if BotState.last_error_at else None
            ),
        },
        "config": {
            "environment": settings.KALSHI_ENV,
            "trading_enabled": settings.TRADING_ENABLED,
            "active_capital_usd": settings.ACTIVE_CAPITAL_USD,
            "motors_enabled": {
                "motor_1_arbitrage": settings.MOTOR_1_ARBITRAGE_ENABLED,
                "motor_2_sportsbook": settings.MOTOR_2_SPORTSBOOK_ENABLED,
                "motor_2_execution": settings.MOTOR_2_EXECUTION_ENABLED,
                "motor_3_clv": settings.MOTOR_3_CLV_ENABLED,
                "motor_3_execution": settings.MOTOR_3_EXECUTION_ENABLED,
                "motor_5_mm": settings.MOTOR_MM_ENABLED,
                "motor_5_execution": settings.MOTOR_MM_EXECUTION_ENABLED,
            },
            # Incidente 2026-07-25: el disco llegó a 0.03GB sin que llegara UNA alerta —
            # Telegram no estaba configurado y nada lo decía. Visible acá para que el
            # chequeo post-deploy lo vea (false = NINGÚN aviso del bot va a llegar).
            "telegram_alerts_configured": settings.telegram_configured,
        },
        "capital": RiskManager.capital_status(),
        "today": {
            "trades_count": len(trades_today),
            "winning": sum(1 for t in trades_today if t.pnl_cents and t.pnl_cents > 0),
            "losing": sum(1 for t in trades_today if t.pnl_cents and t.pnl_cents < 0),
            "pending": sum(1 for t in trades_today if t.status == "pending"),
            "pnl_cents": sum(t.pnl_cents or 0 for t in trades_today),
        },
        "week": {
            "trades_count": len(trades_week),
            "pnl_cents": sum(t.pnl_cents or 0 for t in trades_week),
        },
        "last_settled_day": (
            {
                "date": last_pnl.date,
                "pnl": round(last_pnl.pnl, 2),
                "pnl_pct": round(last_pnl.pnl_pct, 2),
                "trades": last_pnl.trades_count,
            }
            if last_pnl
            else None
        ),
        "recent_risk_events": [
            {
                "type": r.event_type,
                "severity": r.severity,
                "message": r.message,
                "at": r.triggered_at.isoformat(),
            }
            for r in recent_risks
        ],
        "orderbook_manager_v2": v2_info,
    }


# =====================================================
# Admin endpoints
# =====================================================


@app.post("/admin/pause")
async def pause_bot(reason: str = Query(..., min_length=1, max_length=200)) -> dict[str, str]:
    """
    Pausa el bot (deja de tradear pero el container sigue vivo).
    Útil para verificar algo sin tener que matar el deploy.
    """
    BotState.is_paused = True
    BotState.pause_reason = reason
    logger.warning(f"Bot pausado manualmente: {reason}")

    # Registrar en DB
    with get_session() as s:
        event = RiskEvent(
            event_type="manual_pause",
            severity="warning",
            message=f"Manual pause: {reason}",
        )
        s.add(event)
        s.commit()

    return {"status": "paused", "reason": reason}


@app.post("/admin/resume")
async def resume_bot() -> dict[str, str]:
    """Reanuda trading después de pausa manual.

    Auditoría 2026-07-07 (P1): si el kill-switch PERSISTENTE está engaged (stop-loss o
    rollback abortado), este endpoint NO puede levantarlo — un curl saltearía la
    verificación de posiciones=0 de scripts/clear_kill_switch.py y además la pausa
    volvería sola en el próximo redeploy (_rehydrate_kill_switch), dejando el bot en un
    estado a medias. FAIL-CLOSED: si la DB no se puede leer, tampoco se resume.
    """
    from src.storage.models import kill_switch_engaged

    try:
        engaged, ks_reason = kill_switch_engaged()
    except Exception as exc:
        logger.error(f"/admin/resume: no se pudo leer el kill-switch: {exc} (fail-closed)")
        raise HTTPException(
            status_code=503, detail="No se pudo verificar el kill-switch — resume denegado"
        ) from exc
    if engaged:
        logger.warning(f"/admin/resume RECHAZADO: kill-switch persistente engaged ({ks_reason})")
        raise HTTPException(
            status_code=409,
            detail=(
                f"Kill-switch persistente engaged: {ks_reason}. "
                "Levantarlo SOLO con scripts/clear_kill_switch.py (verifica posiciones=0)."
            ),
        )

    if not BotState.is_paused:
        return {"status": "already_running"}

    BotState.is_paused = False
    BotState.pause_reason = None
    logger.info("Bot resumido manualmente")

    with get_session() as s:
        event = RiskEvent(
            event_type="manual_resume",
            severity="info",
            message="Manual resume",
        )
        s.add(event)
        s.commit()

    return {"status": "running"}


@app.get("/admin/stats")
async def stats() -> dict[str, Any]:
    """Estadísticas operacionales."""
    with get_session() as s:
        total_trades = s.exec(select(Trade)).all()
        risk_events = s.exec(select(RiskEvent)).all()

    return {
        "total_trades_ever": len(total_trades),
        "total_risk_events": len(risk_events),
        "uptime_seconds": int((datetime.now(UTC) - BotState.started_at).total_seconds()),
    }


@app.get("/stats/daily")
async def stats_daily(days: int = Query(default=30, ge=1, le=120)) -> dict[str, Any]:
    """
    Conteos diarios de telemetría (read-only) — continuidad de captura y
    actividad de motores por HTTP, sin terminal ni SQL (lo consume el agente
    web; patrón portado de Polybot). Un día ausente = agujero de captura.

    INCIDENTE 2026-07-28 (el bot se congeló al consultar este endpoint): los
    filtros eran `WHERE date(captured_at) >= :cutoff`. Envolver la columna en una
    función ANULA el índice de `captured_at` → full scan de `market_snapshots`,
    que para entonces crecía a 13M filas/día. El `date()` sigue en el
    SELECT/GROUP BY (ahí hace falta), pero el WHERE compara la columna DESNUDA:
    los naive UTC se guardan como ISO ('YYYY-MM-DD HH:MM:SS.ffffff'), así que el
    orden lexicográfico contra 'YYYY-MM-DD' coincide con el cronológico y el
    índice se puede usar. Regla: nunca envolver en función la columna del WHERE.
    """
    from sqlalchemy import text as _text

    cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    per_day: dict[str, dict[str, Any]] = {}

    def _fill(s: Any, sql: str, keys: list[str]) -> None:
        for row in s.execute(_text(sql), {"cutoff": cutoff}):
            bucket = per_day.setdefault(str(row[0]), {})
            for i, key in enumerate(keys, start=1):
                bucket[key] = row[i]

    with get_session() as s:
        _fill(
            s,
            "SELECT date(captured_at), COUNT(*) FROM market_snapshots "
            "WHERE captured_at >= :cutoff GROUP BY 1",
            ["market_snapshots"],
        )
        _fill(
            s,
            "SELECT date(received_at), COUNT(*) FROM orderbook_events "
            "WHERE received_at >= :cutoff GROUP BY 1",
            ["orderbook_events"],
        )
        _fill(
            s,
            "SELECT date(created_at), COUNT(*) FROM edge_windows "
            "WHERE created_at >= :cutoff GROUP BY 1",
            ["edge_windows_total"],
        )
        # Desglose por kind (binary/multi_outcome/consensus/linemove/ofi/spillover)
        for row in s.execute(
            _text(
                "SELECT date(created_at), COALESCE(kind, 'binary'), COUNT(*) "
                "FROM edge_windows WHERE created_at >= :cutoff GROUP BY 1, 2"
            ),
            {"cutoff": cutoff},
        ):
            per_day.setdefault(str(row[0]), {})[f"edges_{row[1]}"] = row[2]
        _fill(
            s,
            "SELECT date(created_at), COUNT(*), COALESCE(SUM(signals), 0) "
            "FROM motor2_funnel_snapshots WHERE created_at >= :cutoff GROUP BY 1",
            ["m2_funnel_cycles", "m2_signals"],
        )
        _fill(
            s,
            "SELECT date(created_at), COUNT(*), COALESCE(SUM(fills), 0) "
            "FROM mm_funnel_snapshots WHERE created_at >= :cutoff GROUP BY 1",
            ["mm_funnel_cycles", "mm_fills"],
        )
        _fill(
            s,
            "SELECT date(recorded_at), COUNT(*) FROM analyst_verdicts "
            "WHERE recorded_at >= :cutoff GROUP BY 1",
            ["analyst_verdicts"],
        )

    return {
        "days_requested": days,
        "cutoff": cutoff,
        "generated_at": datetime.now(UTC).isoformat(),
        "daily": dict(sorted(per_day.items())),
    }


#: Qué MIDE realmente la columna `edge_pct` en cada kind de edge_windows.
#: NO todos guardan un porcentaje — el nombre de la columna miente para M8 y M9:
#:   - binary / multi_outcome (M1, REST): edge neto post-fee, en % → comparable con 8pp
#:   - ofi (M8): el Z-SCORE de la señal OFI (shadow.py `edge_pct=p.signal.zscore`)
#:   - spillover / spillover_exec (M9): el MOVE del trigger, en CENTAVOS
#: Aplicarles los buckets de puntos porcentuales daba lecturas absurdas y
#: "sospechosos >8pp" que no son data podrida sino z=9 perfectamente normal
#: (detectado 2026-07-28: max 2678.83 en ofi, y los buckets >0/>1/>3 idénticos
#: porque el detector solo emite con |z| >= z_min, así que no existe señal entre
#: 0 y el umbral — un agujero de diseño, no una distribución).
_EDGE_UNITS: dict[str, str] = {
    "binary": "pct",
    "multi_outcome": "pct",
    "consensus": "pct",
    "ofi": "zscore",
    "spillover": "cents",
    "spillover_exec": "cents",
}
_UNIT_LABEL = {
    "pct": "edge neto post-fee en puntos porcentuales",
    "zscore": "z-score de la señal OFI (NO es un %)",
    "cents": "movimiento del trigger en centavos (NO es un %)",
    "desconocido": "unidad no declarada para este kind",
}


@app.get("/stats/edges")
async def stats_edges(days: int = Query(default=30, ge=1, le=120)) -> dict[str, Any]:
    """
    Distribución de la señal por kind sobre edge_windows (read-only). Responde
    "¿cuánta señal dio M8/M9/consensus esta semana y de qué tamaño?" de un GET.

    OJO CON LA UNIDAD (fix 2026-07-28): la columna se llama `edge_pct` pero solo
    los kinds binarios guardan un porcentaje. M8 guarda un z-score y M9 centavos.
    Cada bloque declara su `unit`, y los buckets en puntos porcentuales —
    incluido el guardarraíl `gt_8pp_sospechosos` — SOLO se calculan donde la
    unidad es `pct`. Filas con valor NULL (pre-deploy) se excluyen de los buckets
    pero se cuentan en rows_total.
    """
    from sqlalchemy import text as _text

    cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    by_kind: dict[str, dict[str, Any]] = {}
    with get_session() as s:
        for row in s.execute(
            _text(
                "SELECT COALESCE(kind, 'binary') AS k, COUNT(*), "
                "COUNT(edge_pct), ROUND(AVG(edge_pct), 4), ROUND(MAX(edge_pct), 4), "
                "SUM(CASE WHEN edge_pct > 0 THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN edge_pct > 1 THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN edge_pct > 3 THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN edge_pct > 8 THEN 1 ELSE 0 END) "
                "FROM edge_windows WHERE created_at >= :cutoff GROUP BY 1"
            ),
            {"cutoff": cutoff},
        ):
            kind = str(row[0])
            unit = _EDGE_UNITS.get(kind, "desconocido")
            block: dict[str, Any] = {
                "unit": unit,
                "unit_desc": _UNIT_LABEL[unit],
                "rows_total": row[1],
                "rows_with_value": row[2],
                "value_avg": row[3],
                "value_max": row[4],
                "gt_0": row[5] or 0,
            }
            if unit == "pct":
                # Solo acá los puntos porcentuales significan algo.
                block["gt_1pp"] = row[6] or 0
                block["gt_3pp"] = row[7] or 0
                block["gt_8pp_sospechosos"] = row[8] or 0
            else:
                block["nota_unidad"] = (
                    f"buckets en pp OMITIDOS: esta serie no está en %, está en {unit}. "
                    "Comparar contra el guardarraíl de 8pp no tiene sentido acá."
                )
            by_kind[kind] = block
        # Ranking SOLO de los kinds que están en %: mezclar unidades hacía que los
        # z-scores de M8 coparan el top-10 y se leyeran como "edges enormes".
        pct_kinds = tuple(k for k, u in _EDGE_UNITS.items() if u == "pct")
        top = [
            {
                "created_at": str(row[0]),
                "ticker": row[1],
                "kind": row[2],
                "edge_pct": row[3],
            }
            for row in s.execute(
                _text(
                    "SELECT created_at, market_ticker, COALESCE(kind, 'binary'), "
                    "ROUND(edge_pct, 4) FROM edge_windows "
                    "WHERE created_at >= :cutoff AND edge_pct IS NOT NULL "
                    f"AND COALESCE(kind, 'binary') IN ({','.join(repr(k) for k in pct_kinds)}) "
                    "ORDER BY edge_pct DESC LIMIT 10"
                ),
                {"cutoff": cutoff},
            )
        ]

    return {
        "days_requested": days,
        "cutoff": cutoff,
        "generated_at": datetime.now(UTC).isoformat(),
        "by_kind": by_kind,
        "top_10_edges": top,
        "note": (
            "CADA kind declara su unit: solo 'pct' es edge neto post-fee en puntos "
            "porcentuales. 'ofi' guarda z-scores y 'spillover' centavos — para esos "
            "los buckets en pp se omiten a proposito y el top_10 los excluye. "
            "gt_8pp_sospechosos: en binarios liquidos >8pp suele ser data podrida, no "
            "oportunidad (guardarrail de plausibilidad), y solo aplica a unit='pct'. "
            "Filas con valor NULL son pre-deploy del campo."
        ),
    }


def _pnl_significance(n: int, pnl_usd: float, sum_sq_cents: float) -> dict[str, Any]:
    """
    ¿El PnL por trade se distingue de CERO? Devuelve error estándar y t.

    POR QUÉ (2026-07-28): los dos umbrales que probamos para "ruido" fallan por el
    mismo motivo — son números elegidos a dedo sobre la MEDIA, sin mirar la
    dispersión.
      - absoluto ($0.15/trade): depende del sizing y del capital del momento;
        significa cosas distintas en meses distintos.
      - relativo (2 × fee): con los fees de Kalshi el umbral queda en centavos
        ($0.028 para M2), así que aprueba casi cualquier media positiva. Medido en
        producción: el piso absoluto tapaba al relativo en 2 de 3 motores.

    La pregunta real no es "¿la media supera X?" sino "¿esta media es distinguible
    de cero con esta cantidad de trades y esta varianza?". Eso es `t = media / EE`,
    y no depende ni del capital ni del fee. |t| < 2 ≈ indistinguible de cero al 95%:
    ruido, por muy positiva que se vea la media.

    n < 30 → se devuelve `t` igual pero marcado como muestra chica: con 15 trades
    (caso REST) ningún estadístico salva la decisión, hace falta más muestra.
    """
    if n < 2:
        return {"pnl_per_trade_stderr_usd": None, "pnl_t_stat": None, "muestra_suficiente": False}
    mean_cents = (pnl_usd * 100) / n
    # Varianza muestral vía suma de cuadrados: E[x²] − (E[x])², corregida por n−1.
    var = max(0.0, (sum_sq_cents - n * mean_cents**2) / (n - 1))
    stderr_cents = (var / n) ** 0.5
    return {
        "pnl_per_trade_stderr_usd": round(stderr_cents / 100, 4),
        "pnl_t_stat": round(mean_cents / stderr_cents, 2) if stderr_cents > 0 else None,
        "muestra_suficiente": n >= 30,
    }


@app.get("/stats/motors")
async def stats_motors(days: int = Query(default=30, ge=1, le=180)) -> dict[str, Any]:
    """
    PnL REALIZADO por motor en una ventana (read-only). El stop-loss es global y
    el neto ENMASCARA qué motor sangra (auditoría 2026-07-18: M2 -$432 mientras
    M1 +$34). Este corte es el instrumento para evaluar motores de a uno —
    imprescindible cuando corren varios en ejecución a la vez.

    Por motor: trades settleados, PnL neto USD, fees, win-rate, PnL por trade y
    el peor día. `verdict_hint` es descriptivo (sangra/ruido/positivo), NO gatea
    nada: la decisión de apagar un motor es humana.
    """
    from sqlalchemy import text as _text

    cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    motors: dict[str, dict[str, Any]] = {}
    with get_session() as s:
        for row in s.execute(
            _text(
                "SELECT strategy, COUNT(*), "
                "ROUND(COALESCE(SUM(pnl_cents), 0) / 100.0, 2), "
                "ROUND(COALESCE(SUM(fees_cents), 0) / 100.0, 2), "
                "SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN pnl_cents < 0 THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN fees_cents IS NULL THEN 1 ELSE 0 END), "
                # Suma de cuadrados: con COUNT y SUM alcanza para el desvío estándar
                # del PnL por trade, y con eso para saber si la media se distingue de
                # cero. Sin esto, "ruido" es un umbral elegido a dedo (ver abajo).
                "COALESCE(SUM(pnl_cents * pnl_cents), 0) "
                "FROM trades WHERE settled_at IS NOT NULL "
                "AND settled_at >= :cutoff GROUP BY 1"
            ),
            {"cutoff": cutoff},
        ):
            strategy, n, pnl, fees, wins, losses, fees_null, sum_sq = row
            n = n or 0
            fees_null = fees_null or 0
            with_fee = n - fees_null
            motors[str(strategy)] = {
                "settled_trades": n,
                "pnl_usd": pnl,
                "fees_usd": fees,
                # Sin esto un motor que NO grabó el fee se ve idéntico a uno que no pagó
                # ninguno (M1: 519 trades con fees $0.00 el 2026-07-28). El criterio de
                # cierre del mes compara PnL/trade contra el fee promedio: con el fee sin
                # registrar esa comparación es contra CERO y aprueba cualquier cosa.
                "fees_missing_trades": fees_null,
                "fees_coverage_pct": round(with_fee / n * 100, 1) if n else None,
                "fee_per_trade_usd": round(fees / with_fee, 4) if with_fee > 0 else None,
                "wins": wins or 0,
                "losses": losses or 0,
                "win_rate_pct": round((wins or 0) / n * 100, 1) if n else None,
                "pnl_per_trade_usd": round(pnl / n, 4) if n else None,
                **_pnl_significance(n, pnl, sum_sq or 0),
            }

        # Peor día por motor: dónde se concentró la sangría
        for row in s.execute(
            _text(
                "SELECT strategy, date(settled_at) AS d, "
                "ROUND(SUM(pnl_cents) / 100.0, 2) AS day_pnl "
                "FROM trades WHERE settled_at IS NOT NULL AND settled_at >= :cutoff "
                "GROUP BY 1, 2 ORDER BY day_pnl ASC"
            ),
            {"cutoff": cutoff},
        ):
            bucket = motors.get(str(row[0]))
            if bucket is not None and "worst_day" not in bucket:
                bucket["worst_day"] = {"date": str(row[1]), "pnl_usd": row[2]}

    for data in motors.values():
        pnl = data["pnl_usd"] or 0.0
        t_stat = data["pnl_t_stat"]
        # Ruido = la media del PnL/trade NO se distingue de cero. Ver _pnl_significance:
        # los umbrales en dólares (absoluto o 2×fee) se descartaron porque dependen del
        # capital y del fee, y en producción el piso absoluto terminaba tapando al
        # relativo en 2 de 3 motores — o sea, el criterio "relativo" no actuaba.
        if pnl < -20:
            data["verdict_hint"] = "sangra"
        elif not data["muestra_suficiente"]:
            data["verdict_hint"] = (
                f"muestra insuficiente ({data['settled_trades']} trades settleados; "
                "hacen falta >=30 para distinguir la media de cero)"
            )
        elif t_stat is not None and abs(t_stat) < 2:
            data["verdict_hint"] = f"ruido (t={t_stat:+.2f}: indistinguible de cero)"
        elif pnl > 0:
            data["verdict_hint"] = f"positivo (t={t_stat:+.2f})" if t_stat else "positivo"
        else:
            data["verdict_hint"] = "negativo leve"
        # La cobertura de fees ya no gatea el veredicto (el criterio no usa el fee),
        # pero un motor sin fees registrados tiene el PnL bien y el COSTO invisible:
        # se sigue diciendo, porque cambia cómo se lee `pnl_usd`.
        if data["fees_missing_trades"]:
            data["verdict_hint"] += (
                f" · OJO: fee sin registrar en {data['fees_missing_trades']}/"
                f"{data['settled_trades']} trades"
            )

    total = round(sum(m["pnl_usd"] or 0.0 for m in motors.values()), 2)
    return {
        "days_requested": days,
        "cutoff": cutoff,
        "generated_at": datetime.now(UTC).isoformat(),
        "net_pnl_usd": total,
        "by_motor": dict(sorted(motors.items(), key=lambda kv: kv[1]["pnl_usd"] or 0.0)),
        "note": (
            "PnL realizado de trades settleados. El neto global enmascara motores "
            "individuales: mirar by_motor (ordenado del peor al mejor). "
            "verdict_hint es descriptivo, no gatea nada. 'ruido' = |pnl_t_stat| < 2, "
            "o sea la media del PnL/trade no se distingue de cero con esta muestra "
            "(no es un umbral en dolares: no depende del capital ni del fee). "
            "fees_coverage_pct < 100 significa que el COSTO de ese motor esta "
            "subregistrado — el PnL es correcto igual: son filas anteriores al fix "
            "de fees del 2026-07-28."
        ),
    }
