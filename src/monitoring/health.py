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
            # sids en circuit breaker (recovery deshabilitada → books STALE hasta el redeploy):
            # el dato que faltaba para ver el sid=1 muerto por timeout_x5. Antes, invisible.
            "sids_disabled": sorted(v2_mgr._recovery_disabled_sids),
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
