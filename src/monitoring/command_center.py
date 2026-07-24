"""
Centro de comando de Telegram — Nivel 0 (C1): SOLO LECTURA.

Nace del incidente 2026-07-12: todo el diagnóstico (rollbacks, desyncs, funnel, residuales,
disco) exigió scripts en el container; estos comandos ponen esa foto en el chat. Cada builder
es READ-ONLY (abre su propia sesión y solo LEE), best-effort (un fallo devuelve texto de
error, jamás rompe el loop) y NO acepta argumentos que muten nada.

⛔ Nivel 0 por diseño: acá NO hay pausar/reanudar/flags (eso es C2/C3, con tiers y
confirmación — ver plan). El kill-switch persistente JAMÁS se toca por chat.
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime, time, timedelta

from sqlmodel import col, select

from src.monitoring.health import BotState
from src.storage.disk_guard import DiskGuard
from src.storage.models import (
    Motor2FunnelSnapshot,
    PortfolioPosition,
    RiskEvent,
    Trade,
    get_session,
)
from src.utils.config import get_settings


def _naive_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def build_incidentes_text(limit: int = 10) -> str:
    """Últimos RiskEvents (rollbacks, breaker, kill-switch, daily_stop) — lo primero que
    hay que mirar en un incidente."""
    with get_session() as s:
        events = list(
            s.exec(select(RiskEvent).order_by(col(RiskEvent.triggered_at).desc()).limit(limit))
        )
    if not events:
        return "🟢 *Incidentes*: sin eventos de riesgo registrados."
    icon = {"critical": "🔴", "warning": "🟠", "info": "🔵"}
    lines = [f"🚨 *Últimos {len(events)} eventos de riesgo*"]
    for e in events:
        ts = e.triggered_at.strftime("%m-%d %H:%M") if e.triggered_at else "?"
        lines.append(f"{icon.get(e.severity, '⚪')} `{ts}` *{e.event_type}* — {e.message[:120]}")
    return "\n".join(lines)


def build_salud_text() -> str:
    """Salud del feed: pausa, último error, y el estado de los books V2 (stale/desync/
    recovery) — habría mostrado los 635 desyncs de AZLAD al instante."""
    lines = ["🩺 *Salud del bot*"]
    lines.append(
        "⏸️ EN PAUSA: " + (BotState.pause_reason or "?") if BotState.is_paused else "▶️ Corriendo"
    )
    if BotState.last_error:
        ts = BotState.last_error_at.strftime("%H:%M:%S") if BotState.last_error_at else "?"
        lines.append(f"⚠️ Último error (`{ts}`): {str(BotState.last_error)[:150]}")
    else:
        lines.append("✅ Sin errores registrados")
    v2 = BotState.v2_manager
    if v2 is None:
        lines.append("📚 Books V2: (manager no activo)")
    else:
        try:
            st = v2.stats()
            stale = st.get("stale_tickers", 0)
            flag = "🔴" if stale else "✅"
            lines.append(
                f"📚 Books V2: tracked=`{st.get('tracked_tickers')}` "
                f"init=`{st.get('initialized_tickers')}` {flag} stale=`{stale}` "
                f"recovering=`{st.get('recovering_sids')}` gaps60s=`{st.get('gaps_last_60s')}`"
            )
        except Exception as exc:
            lines.append(f"📚 Books V2: error leyendo stats ({type(exc).__name__})")
    return "\n".join(lines)


def build_funnel_text() -> str:
    """Último snapshot del funnel de M2 — el veredicto de señales=0 sin correr scripts."""
    with get_session() as s:
        snap = s.exec(
            select(Motor2FunnelSnapshot).order_by(col(Motor2FunnelSnapshot.created_at).desc())
        ).first()
    if snap is None:
        return "📊 *Funnel M2*: sin snapshots todavía (¿M2 corriendo con odds reales?)."
    ts = snap.created_at.strftime("%m-%d %H:%M") if snap.created_at else "?"
    best = f"{snap.best_net_edge_pp:+.2f}pp" if snap.best_net_edge_pp > -1 else "nada evaluado"
    return "\n".join(
        [
            f"📊 *Funnel M2* (`{ts}`)",
            f"odds=`{snap.odds_total}` (in-play skip=`{snap.started_skip}`) · "
            f"kalshi=`{snap.kalshi_total}` · matched=`{snap.events_matched}`",
            f"rejects: absent=`{snap.reject_absent}` card=`{snap.reject_cardinality}` "
            f"names=`{snap.reject_names}` no_fair=`{snap.reject_no_fair}`",
            f"best edge neto: *{best}* · señales: *{snap.signals}*",
        ]
    )


def build_pnl_text() -> str:
    """PnL realizado por ventana vs sus límites de stop-loss — cuánto colchón queda antes
    del freno (misma matemática de ventanas/límites que el RiskManager, sin efectos)."""
    settings = get_settings()
    from src.risk.manager import RiskManager

    capital = RiskManager.capital_status().get("effective_usd") or settings.ACTIVE_CAPITAL_USD

    now = _naive_now()
    today = datetime.combine(now.date(), time.min)
    week = datetime.combine(now.date() - timedelta(days=now.weekday()), time.min)
    month = datetime.combine(now.date().replace(day=1), time.min)
    start = min(week, month)
    with get_session() as s:
        trades = list(
            s.exec(select(Trade).where(Trade.status == "settled", col(Trade.settled_at) >= start))
        )

    def _pnl(since: datetime) -> float:
        return (
            sum((t.pnl_cents or 0) for t in trades if t.settled_at and t.settled_at >= since)
            / 100.0
        )

    windows = [
        ("Diario", _pnl(today), settings.MAX_DAILY_LOSS_PCT, settings.MAX_DAILY_LOSS_FLOOR_USD),
        ("Semanal", _pnl(week), settings.MAX_WEEKLY_LOSS_PCT, settings.MAX_WEEKLY_LOSS_FLOOR_USD),
        (
            "Mensual",
            _pnl(month),
            settings.MAX_MONTHLY_LOSS_PCT,
            settings.MAX_MONTHLY_LOSS_FLOOR_USD,
        ),
    ]
    lines = [f"💰 *PnL realizado vs stop-loss* (capital efectivo `${capital:.2f}`)"]
    for name, pnl, pct, floor in windows:
        limit = max(capital * pct / 100.0, floor)
        used = (abs(pnl) / limit * 100.0) if pnl < 0 and limit else 0.0
        flag = "🔴" if used >= 100 else ("🟠" if used >= 60 else "🟢")
        lines.append(f"{flag} {name}: `${pnl:+.2f}` / límite `-${limit:.2f}` ({used:.0f}% usado)")
    return "\n".join(lines)


def build_posiciones_text() -> str:
    """Posiciones abiertas (portfolio sync) + patas filled sin settle (residuales candidatos)."""
    with get_session() as s:
        positions = list(s.exec(select(PortfolioPosition)))
        filled = list(s.exec(select(Trade).where(Trade.status == "filled")))
    lines = [f"📦 *Posiciones abiertas*: {len(positions)}"]
    for p in positions[:15]:
        exp = f" (${p.exposure_cents / 100.0:.2f})" if p.exposure_cents else ""
        lines.append(f"• `{p.ticker}` {p.side} ×{p.count}{exp}")
    if len(positions) > 15:
        lines.append(f"… y {len(positions) - 15} más")
    if filled:
        exposure = sum(t.price_cents * t.count for t in filled) / 100.0
        lines.append(
            f"⏳ Patas `filled` sin settle: {len(filled)} (exposición bruta `${exposure:.2f}`)"
        )
    return "\n".join(lines)


def build_disco_text() -> str:
    """DiskGuard + tamaños reales de la DB — el incidente de disco entero, en el chat."""
    guard = DiskGuard.snapshot()
    url = get_settings().DATABASE_URL
    db = url.split("sqlite:///", 1)[-1] if "sqlite:///" in url else "/app/data/trades.db"

    def _size(p: str) -> str:
        try:
            n = os.path.getsize(p)
        except OSError:
            return "—"
        return f"{n / 1e9:.2f}GB" if n >= 1e9 else f"{n / 1e6:.1f}MB"

    flag = {"ok": "🟢", "warn": "🟠", "critical": "🔴"}.get(str(guard["state"]), "⚪")
    lines = [
        f"💾 *Disco* {flag} estado=`{guard['state']}` "
        f"libre=`{guard['free_gb']}GB` telemetría=`{'ON' if guard['diagnostics_allowed'] else 'DESCARTANDO'}`",
        f"DB: `{_size(db)}` · WAL: `{_size(db + '-wal')}`",
    ]
    try:
        usage = shutil.disk_usage(os.path.dirname(db) or ".")
        pct = usage.used / usage.total * 100 if usage.total else 0
        lines.append(f"Mount: `{usage.used / 1e9:.1f}/{usage.total / 1e9:.1f}GB` ({pct:.0f}%)")
    except OSError:
        lines.append("Mount: (no legible)")
    return "\n".join(lines)


def build_ayuda_text() -> str:
    return "\n".join(
        [
            "🎛 *Centro de comando (solo lectura)*",
            "/dashboard — la foto completa",
            "/incidentes — últimos eventos de riesgo",
            "/salud — feed WS, books stale/desync, errores",
            "/funnel — embudo de Motor 2 (último ciclo)",
            "/pnl — PnL por ventana vs stop-loss",
            "/posiciones — abiertas + residuales",
            "/disco — DiskGuard + tamaño DB",
            "_Nivel 0: nada de esto muta el bot. Pausar/reanudar: ver plan C2/C3._",
        ]
    )


# Registro del centro de comando: comando → builder (todos READ-ONLY, sin argumentos).
COMMAND_BUILDERS: dict[str, object] = {
    "/incidentes": build_incidentes_text,
    "/salud": build_salud_text,
    "/funnel": build_funnel_text,
    "/pnl": build_pnl_text,
    "/posiciones": build_posiciones_text,
    "/disco": build_disco_text,
    "/ayuda": build_ayuda_text,
    "/help": build_ayuda_text,
}
