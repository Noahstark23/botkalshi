"""
Snapshot de SUPERVISIÓN del runtime principal — contrato versionado, SOLO LECTURA.

Encargo ASTRA-SUPERVISION-20260923 (S1): que un supervisor externo pueda observar y
auditar EL BOT PRODUCTIVO sin API pública, sin endpoints de control y sin credenciales.
El runtime escribe un JSON privado y acotado; el supervisor lo lee por su canal ya
aprobado. Nada acá decide, pausa, reanuda ni ordena: no hay una sola acción ejecutable.

Reglas que este módulo hace cumplir (y los tests fijan):
  - ALLOWLIST de campos: `validate_snapshot` rechaza cualquier clave fuera del contrato y
    cualquier string con forma de secreto (PEM, Bearer, apiKey=, token=…). Nunca se
    vuelca el environment, texto crudo de logs ni el `last_error` literal (puede traer
    URLs/params): solo su clase saneada y su hora.
  - AUSENCIA = UNKNOWN, jamás cero ni sano. Cada dato financiero es una EVIDENCIA con
    valor, unidad exacta (cents enteros / conteos), `as_of`, `source` y `status`
    (OK | STALE | UNKNOWN | ERROR). Un balance que nunca se leyó es UNKNOWN, no $0.
  - La salud agregada es falsable: OK solo si toda la evidencia crítica existe, está
    fresca y es coherente; si algo no se puede evaluar → UNKNOWN; vencido o incoherente
    → DEGRADED (skill diagnostics-recovery: "distinguir está bien de no lo sé").
  - PRODUCCIÓN ≠ SIMULACIÓN: `schema_version` y `ledger_domain="PRODUCTION"` propios.
    Los artefactos de research (SIMULATION_ONLY, capital ficticio) no tienen este
    schema y el lector los rechaza (`supervision_reader`).
  - La DB fuente se abre en `mode=ro` (URI): el lector es estructuralmente incapaz de
    escribirla. Exportación atómica (tmp + fsync + replace), directorio 0700, archivos
    0600, historial acotado.
  - `mode.classification` describe CONFIGURACIÓN, no actividad: un flag encendido no es
    prueba de trading en vivo (nunca se emite LIVE_ACTIVE).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "botkalshi-production-snapshot-v1"
LEDGER_DOMAIN = "PRODUCTION"
PRODUCER = "src.runner/supervision_snapshot"
DEFAULT_MAX_AGE_SEC = 180
FEED_STALE_SEC = 60.0
BOOKS_MIN_INITIALIZED_FRACTION = 0.5  # mismo umbral que /health (BLIND_MIN_INITIALIZED_FRACTION)
POSITIONS_STALE_SEC = 3_600  # sync del PortfolioPoller más viejo que esto: no es "actual"
STALE_PENDING_SEC = 3_600  # una orden pending más vieja que esto es una incoherencia a mirar
RELEASE_ENV_NAMES = ("BOTKALSHI_RELEASE_SHA", "SOURCE_COMMIT")  # allowlist, nada más

_SHA_RE = re.compile(r"[0-9a-f]{40}")
_CODE_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_REASON_RE = re.compile(r"[^A-Za-z0-9 _.,:;%$()/+=<>#-]")
_SECRET_RE = re.compile(
    r"(-----BEGIN|PRIVATE KEY|Bearer\s|apikey=|api_key=|apiKey=|token=|password|passwd|"
    r"secret=|Authorization|KALSHI-ACCESS)",
    re.IGNORECASE,
)
_EVIDENCE_STATUSES = ("OK", "STALE", "UNKNOWN", "ERROR")


# --------------------------------------------------------------------------------------
# Helpers de saneo.
# --------------------------------------------------------------------------------------


def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None:  # convención del repo: naive = UTC
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def _code(value: object) -> str | None:
    """Un identificador corto y seguro (clase de error, tipo de evento), o None."""
    if not isinstance(value, str):
        return None
    head = value.split(":", 1)[0].strip()
    return head if _CODE_RE.fullmatch(head) else "UNCLASSIFIED"


def _reason(value: object) -> str | None:
    """Texto interno corto (razón de pausa/kill-switch) sin caracteres raros ni secretos."""
    if not isinstance(value, str) or not value:
        return None
    text = _REASON_RE.sub("?", value)[:160]
    return "[REDACTED]" if _SECRET_RE.search(text) else text


def contains_secret(text: str) -> bool:
    """Public form of the secret detector, shared by every consumer (alerts, reports)."""
    return bool(_SECRET_RE.search(text))


def release_sha(env: dict[str, str] | os._Environ[str] | None = None) -> str:
    env = os.environ if env is None else env
    for name in RELEASE_ENV_NAMES:
        value = (env.get(name) or "").strip().lower()
        if _SHA_RE.fullmatch(value):
            return value
    return "UNKNOWN"


def evidence(
    value: Any,
    *,
    unit: str,
    source: str,
    as_of: datetime | None,
    status: str,
    note: str | None = None,
) -> dict[str, Any]:
    assert status in _EVIDENCE_STATUSES
    return {
        "value": value if status in ("OK", "STALE") else None,
        "unit": unit,
        "as_of": _iso(as_of),
        "source": source,
        "status": status,
        "note": note,
    }


def _unknown(unit: str, source: str, note: str) -> dict[str, Any]:
    return evidence(None, unit=unit, source=source, as_of=None, status="UNKNOWN", note=note)


# --------------------------------------------------------------------------------------
# Lectura read-only de la DB fuente.
# --------------------------------------------------------------------------------------


def sqlite_path_from_url(url: str) -> Path | None:
    """`sqlite:////app/data/trades.db` → /app/data/trades.db. Otro motor → None."""
    if not url.startswith("sqlite:///"):
        return None
    return Path(url[len("sqlite:///") :])


def read_ledger(db_path: Path | None, *, now: datetime) -> dict[str, Any]:
    """Todo lo que el snapshot necesita de la DB, en UNA conexión `mode=ro`.

    Estructuralmente read-only: el URI `mode=ro` rechaza cualquier escritura. Un fallo
    NO se convierte en ceros: devuelve {"status": "ERROR"/"UNKNOWN", ...} y cada campo
    queda UNKNOWN en el snapshot."""
    if db_path is None:
        return {"status": "UNKNOWN", "note": "DB_NOT_SQLITE"}
    if not db_path.exists():
        return {"status": "UNKNOWN", "note": "DB_FILE_MISSING"}
    naive_now = now.astimezone(UTC).replace(tzinfo=None)
    today = datetime(naive_now.year, naive_now.month, naive_now.day)
    month = datetime(naive_now.year, naive_now.month, 1)
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        return {"status": "ERROR", "note": f"OPEN_FAILED:{type(exc).__name__}"}
    try:

        def one(sql: str, params: tuple = ()) -> Any:
            return con.execute(sql, params).fetchone()

        ks = one("SELECT value, reason, updated_at FROM operational_state WHERE key='kill_switch'")
        mmq = one(
            "SELECT value, reason, updated_at FROM operational_state WHERE key='mm_quotes_paused'"
        )
        by_status = dict(con.execute("SELECT status, COUNT(*) FROM trades GROUP BY status"))
        open_filled = one(
            "SELECT COUNT(*), COALESCE(SUM(COALESCE(filled_count, count) * "
            "COALESCE(fill_price_cents, price_cents)), 0) FROM trades "
            "WHERE status='filled' AND settled_at IS NULL AND closed_by_clv = 0"
        )
        realized = {
            name: one(
                "SELECT COUNT(*), COALESCE(SUM(pnl_cents), 0) FROM trades "
                "WHERE settled_at IS NOT NULL AND settled_at >= ?",
                (start.isoformat(sep=" "),),
            )
            for name, start in (
                ("today", today),
                ("last_7d", today - timedelta(days=7)),
                ("month", month),
            )
        }
        last_trade = one("SELECT MAX(placed_at) FROM trades")[0]
        positions = one("SELECT COUNT(*), MAX(synced_at) FROM portfolio_positions")
        risk = con.execute(
            "SELECT event_type, severity, triggered_at FROM risk_events "
            "WHERE severity IN ('warning','critical') ORDER BY triggered_at DESC LIMIT 5"
        ).fetchall()
        # Coherencia contable (diagnóstico, nunca corrección).
        incoherent = {
            "settled_without_pnl": one(
                "SELECT COUNT(*) FROM trades WHERE status='settled' AND pnl_cents IS NULL"
            )[0],
            "filled_without_fill_price": one(
                "SELECT COUNT(*) FROM trades WHERE status IN ('filled','settled') "
                "AND fill_price_cents IS NULL"
            )[0],
            "fee_unknown_on_filled": one(
                "SELECT COUNT(*) FROM trades WHERE status IN ('filled','settled') "
                "AND fees_cents IS NULL"
            )[0],
            "stale_pending": one(
                "SELECT COUNT(*) FROM trades WHERE status='pending' AND placed_at < ?",
                ((naive_now - timedelta(seconds=STALE_PENDING_SEC)).isoformat(sep=" "),),
            )[0],
        }
    except sqlite3.Error as exc:
        return {"status": "ERROR", "note": f"QUERY_FAILED:{type(exc).__name__}"}
    finally:
        con.close()
    return {
        "status": "OK",
        "read_at": now,
        "kill_switch": ks,
        "mm_quotes_paused": mmq,
        "trades_by_status": {str(k): int(v) for k, v in by_status.items()},
        "open_filled": open_filled,
        "realized": realized,
        "last_trade_at": last_trade,
        "positions": positions,
        "risk_events": risk,
        "incoherent": incoherent,
    }


def _parse_db_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _positions_evidence(row: Any, *, now: datetime) -> dict[str, Any]:
    """Posiciones sincronizadas desde Kalshi. Tabla vacía = ambigua (sin posiciones o
    nunca sincronizada) → UNKNOWN, jamás "0 posiciones"; sync viejo → STALE."""
    source = "portfolio_positions (PortfolioPoller sync)"
    count, synced = (row or (0, None))[0], _parse_db_time((row or (0, None))[1])
    if not count or synced is None:
        return _unknown("count", source, "NO_SYNC_ROWS")
    stale = (now - synced).total_seconds() > POSITIONS_STALE_SEC
    return evidence(
        int(count), unit="count", source=source, as_of=synced, status="STALE" if stale else "OK"
    )


# --------------------------------------------------------------------------------------
# Construcción PURA del snapshot a partir de entradas explícitas.
# --------------------------------------------------------------------------------------


def build_snapshot(
    *,
    now: datetime,
    runtime: dict[str, Any],
    config: dict[str, Any],
    capital: dict[str, Any],
    ledger: dict[str, Any],
    release: str,
    snapshot_id: str | None = None,
    max_age_sec: int = DEFAULT_MAX_AGE_SEC,
) -> dict[str, Any]:
    """Arma el snapshot. PURA: no lee reloj, env, DB ni red — todo llega por argumentos,
    así un test fija cada caso y el runtime no puede colar un dato sin pasar por acá."""
    now = now.astimezone(UTC)
    reasons: list[str] = []
    unknowns: list[str] = []

    # --- proceso y feed ---
    started = runtime.get("started_at")
    last_ws = runtime.get("last_ws_message")
    ws_age = (now - last_ws).total_seconds() if isinstance(last_ws, datetime) else None
    books = runtime.get("books") or {}
    tracked = books.get("tracked")
    initialized = books.get("initialized")
    if not runtime.get("capture_running") or ws_age is None:
        feed_status = "UNKNOWN" if ws_age is None else "STALE"
    elif ws_age >= FEED_STALE_SEC:
        feed_status = "STALE"
    elif isinstance(tracked, int) and tracked > 0 and isinstance(initialized, int):
        feed_status = "OK" if initialized >= tracked * BOOKS_MIN_INITIALIZED_FRACTION else "STALE"
    else:
        feed_status = "UNKNOWN"
    if feed_status != "OK":
        (unknowns if feed_status == "UNKNOWN" else reasons).append(f"FEED_{feed_status}")
    if books.get("sids_disabled"):
        reasons.append("FEED_SIDS_DISABLED")

    # --- balance (evidencia de la interfaz de lectura del RiskManager) ---
    raw_usd = capital.get("raw_balance_usd")
    balance_at = capital.get("balance_at")
    refresh = int(config.get("balance_refresh_seconds") or 0)
    if raw_usd is None or balance_at is None:
        balance = _unknown("cents", "kalshi.get_balance via RiskManager", "NEVER_READ")
        unknowns.append("BALANCE_UNKNOWN")
    else:
        stale = refresh > 0 and (now - balance_at.astimezone(UTC)).total_seconds() > 3 * refresh
        balance = evidence(
            int(round(float(raw_usd) * 100)),
            unit="cents",
            source="kalshi.get_balance via RiskManager",
            as_of=balance_at,
            status="STALE" if stale else "OK",
        )
        if stale:
            reasons.append("BALANCE_STALE")

    # --- ledger (DB en modo ro) ---
    ledger_ok = ledger.get("status") == "OK"
    read_at = ledger.get("read_at")
    src = "trades.db (sqlite mode=ro)"
    if not ledger_ok:
        unknowns.append(f"LEDGER_{ledger.get('status', 'UNKNOWN')}")
        note = ledger.get("note") or "UNREADABLE"
        kill = {
            "state": "UNKNOWN",
            "reason": None,
            "updated_at": None,
            "source": "operational_state",
        }
        mmq = {
            "state": "UNKNOWN",
            "reason": None,
            "updated_at": None,
            "source": "operational_state",
        }
        accounting = {
            "open_orders_pending": _unknown("count", src, note),
            "open_positions_filled": _unknown("count", src, note),
            "open_cost_basis": _unknown("cents", src, note),
            "portfolio_positions": _unknown("count", "portfolio_positions", note),
            "realized_pnl_today": _unknown("cents", src, note),
            "realized_pnl_last_7d": _unknown("cents", src, note),
            "realized_pnl_month": _unknown("cents", src, note),
            "last_trade_at": None,
            "trades_by_status": {},
        }
        coherence = {"status": "UNKNOWN", "checks": {}}
        risk_events: list[dict[str, Any]] = []
    else:
        ks_row = ledger.get("kill_switch")
        kill = {
            "state": "ENGAGED" if ks_row and ks_row[0] == "engaged" else "CLEAR",
            "reason": _reason(ks_row[1]) if ks_row else None,
            "updated_at": _iso(_parse_db_time(ks_row[2])) if ks_row else None,
            "source": "operational_state",
        }
        mm_row = ledger.get("mm_quotes_paused")
        mmq = {
            "state": "PAUSED" if mm_row and mm_row[0] == "paused" else "CLEAR",
            "reason": _reason(mm_row[1]) if mm_row else None,
            "updated_at": _iso(_parse_db_time(mm_row[2])) if mm_row else None,
            "source": "operational_state",
        }
        by_status = ledger.get("trades_by_status") or {}
        open_filled = ledger.get("open_filled") or (0, 0)
        realized = ledger.get("realized") or {}
        positions = ledger.get("positions") or (0, None)

        def ok(value: Any, unit: str, source: str = src) -> dict[str, Any]:
            return evidence(value, unit=unit, source=source, as_of=read_at, status="OK")

        accounting = {
            "open_orders_pending": ok(int(by_status.get("pending", 0)), "count"),
            "open_positions_filled": ok(int(open_filled[0]), "count"),
            "open_cost_basis": ok(int(open_filled[1]), "cents"),
            "portfolio_positions": _positions_evidence(positions, now=now),
            "realized_pnl_today": ok(int(realized.get("today", (0, 0))[1]), "cents"),
            "realized_pnl_last_7d": ok(int(realized.get("last_7d", (0, 0))[1]), "cents"),
            "realized_pnl_month": ok(int(realized.get("month", (0, 0))[1]), "cents"),
            "last_trade_at": _iso(_parse_db_time(ledger.get("last_trade_at"))),
            "trades_by_status": {
                k: int(v) for k, v in sorted(by_status.items()) if _CODE_RE.fullmatch(k)
            },
        }
        checks = {k: int(v) for k, v in (ledger.get("incoherent") or {}).items()}
        bad = sorted(k for k, v in checks.items() if v)
        coherence = {"status": "INCONSISTENT" if bad else "OK", "checks": checks}
        reasons.extend(f"LEDGER_{k.upper()}" for k in bad)
        risk_events = [
            {"type": _code(t), "severity": _code(sev), "at": _iso(_parse_db_time(at))}
            for t, sev, at in ledger.get("risk_events") or []
        ]
        if kill["state"] == "ENGAGED":
            reasons.append("KILL_SWITCH_ENGAGED")

    # --- motores: configuración (flags) + estado runtime observable ---
    flags = config.get("motors") or {}
    motor5 = runtime.get("motor5") or {}
    engines = {
        name: {"enabled": bool(v.get("enabled")), "execution_enabled": bool(v.get("execution"))}
        for name, v in sorted(flags.items())
    }
    engines.setdefault("motor_5_mm", {"enabled": False, "execution_enabled": False})
    engines["motor_5_mm"]["runtime"] = {
        "task_running": bool(motor5.get("task_running")),
        "last_tick": _iso(motor5.get("last_tick")),
        "fair_flow_blocked": bool(motor5.get("fair_flow_blocked")),
        "book_flow_blocked": bool(motor5.get("book_flow_blocked")),
        "fee_policy_blocked": bool(motor5.get("fee_policy_blocked")),
        "experiment_invalid": bool(motor5.get("experiment_invalid")),
    }
    engines.setdefault("motor_1_arbitrage", {"enabled": False, "execution_enabled": False})
    engines["motor_1_arbitrage"]["local_pause"] = runtime.get("motor1_local_pause") is not None

    trading_enabled = bool(config.get("trading_enabled"))
    any_exec = any(e["execution_enabled"] for e in engines.values())
    classification = (
        "EXECUTION_CONFIGURED" if trading_enabled and any_exec else "NO_EXECUTION_CONFIGURED"
    )
    if release == "UNKNOWN":
        unknowns.append("RELEASE_SHA_UNKNOWN")

    status = "UNKNOWN" if unknowns else ("DEGRADED" if reasons else "OK")
    return {
        "schema_version": SCHEMA_VERSION,
        "ledger_domain": LEDGER_DOMAIN,
        "snapshot_id": snapshot_id or uuid.uuid4().hex,
        "producer": PRODUCER,
        "captured_at": now.isoformat(),
        "valid_until": (now + timedelta(seconds=max_age_sec)).isoformat(),
        "release_sha": release,
        "mode": {
            "kalshi_env": _code(config.get("kalshi_env")) or "UNKNOWN",
            "trading_enabled": trading_enabled,
            "classification": classification,
            "note": "configuration only; not evidence of live activity",
        },
        "process": {
            "started_at": _iso(started),
            "uptime_sec": int((now - started).total_seconds())
            if isinstance(started, datetime)
            else None,
            "pid": runtime.get("pid") if isinstance(runtime.get("pid"), int) else None,
            "db_initialized": bool(runtime.get("db_initialized")),
            "last_error_class": _code(runtime.get("last_error")),
            "last_error_at": _iso(runtime.get("last_error_at")),
        },
        "feed": {
            "status": feed_status,
            "capture_running": bool(runtime.get("capture_running")),
            "last_ws_message": _iso(last_ws),
            "ws_age_sec": round(ws_age, 1) if ws_age is not None else None,
            "tracked_markets": tracked if isinstance(tracked, int) else None,
            "books_initialized": initialized if isinstance(initialized, int) else None,
            "sids_disabled": int(books.get("sids_disabled") or 0),
            "gaps_last_60s": books.get("gaps_last_60s")
            if isinstance(books.get("gaps_last_60s"), int)
            else None,
        },
        "controls": {
            "runtime_paused": bool(runtime.get("is_paused")),
            "pause_reason": _reason(runtime.get("pause_reason")),
            "capital_paused": capital.get("is_paused")
            if isinstance(capital.get("is_paused"), bool)
            else None,
            "kill_switch": kill,
            "mm_quotes_paused": mmq,
        },
        "balance": balance,
        "accounting": accounting,
        "coherence": coherence,
        "recent_risk_events": risk_events,
        "health": {
            "status": status,
            "reasons": sorted(set(reasons)),
            "unknown": sorted(set(unknowns)),
        },
    }


# --------------------------------------------------------------------------------------
# Contrato: allowlist + detector de secretos.
# --------------------------------------------------------------------------------------

_EVIDENCE_KEYS = frozenset({"value", "unit", "as_of", "source", "status", "note"})
_STATE_KEYS = frozenset({"state", "reason", "updated_at", "source"})
_TOP = {
    "schema_version",
    "ledger_domain",
    "snapshot_id",
    "producer",
    "captured_at",
    "valid_until",
    "release_sha",
    "mode",
    "process",
    "feed",
    "controls",
    "balance",
    "accounting",
    "coherence",
    "recent_risk_events",
    "health",
}
_SECTIONS = {
    "mode": {"kalshi_env", "trading_enabled", "classification", "note"},
    "process": {
        "started_at",
        "uptime_sec",
        "pid",
        "db_initialized",
        "last_error_class",
        "last_error_at",
    },
    "feed": {
        "status",
        "capture_running",
        "last_ws_message",
        "ws_age_sec",
        "tracked_markets",
        "books_initialized",
        "sids_disabled",
        "gaps_last_60s",
    },
    "controls": {
        "runtime_paused",
        "pause_reason",
        "capital_paused",
        "kill_switch",
        "mm_quotes_paused",
    },
    "accounting": {
        "open_orders_pending",
        "open_positions_filled",
        "open_cost_basis",
        "portfolio_positions",
        "realized_pnl_today",
        "realized_pnl_last_7d",
        "realized_pnl_month",
        "last_trade_at",
        "trades_by_status",
    },
    "coherence": {"status", "checks"},
    "health": {"status", "reasons", "unknown"},
}


def _strings(obj: Any):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def validate_snapshot(doc: Any) -> list[str]:
    """Problemas del documento contra el contrato (lista vacía = válido)."""
    if not isinstance(doc, dict):
        return ["NOT_AN_OBJECT"]
    problems = []
    if set(doc) != _TOP:
        problems.append(f"TOP_KEYS:{sorted(set(doc) ^ _TOP)}")
    if doc.get("schema_version") != SCHEMA_VERSION:
        problems.append("SCHEMA_VERSION")
    if doc.get("ledger_domain") != LEDGER_DOMAIN:
        problems.append("LEDGER_DOMAIN")
    for name, keys in _SECTIONS.items():
        section = doc.get(name)
        if not isinstance(section, dict) or set(section) != keys:
            problems.append(f"SECTION:{name}")
    if not isinstance(doc.get("balance"), dict) or set(doc["balance"]) != _EVIDENCE_KEYS:
        problems.append("SECTION:balance")
    accounting = doc.get("accounting") if isinstance(doc.get("accounting"), dict) else {}
    for key, value in accounting.items():
        if key in ("last_trade_at", "trades_by_status"):
            continue
        if (
            not isinstance(value, dict)
            or set(value) != _EVIDENCE_KEYS
            or (value.get("status") not in _EVIDENCE_STATUSES)
        ):
            problems.append(f"EVIDENCE:{key}")
        elif value["status"] in ("UNKNOWN", "ERROR") and value["value"] is not None:
            problems.append(f"UNKNOWN_WITH_VALUE:{key}")
    controls = doc.get("controls") if isinstance(doc.get("controls"), dict) else {}
    for key in ("kill_switch", "mm_quotes_paused"):
        if not isinstance(controls.get(key), dict) or set(controls[key]) != _STATE_KEYS:
            problems.append(f"STATE:{key}")
    for text in _strings(doc):
        if _SECRET_RE.search(text):
            problems.append("SECRET_PATTERN")
            break
    return problems


# --------------------------------------------------------------------------------------
# Exportación atómica y acotada.
# --------------------------------------------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise


def export_snapshot(doc: dict[str, Any], directory: Path, *, history_max: int) -> Path:
    """Valida y escribe `latest.json` (atómico) + una línea en `history.jsonl` acotado.

    Un documento que no pasa el contrato NO se escribe (ValueError): mejor ningún
    snapshot que uno con un campo no auditado o con forma de secreto."""
    problems = validate_snapshot(doc)
    if problems:
        raise ValueError(f"snapshot rejected by contract: {problems}")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    text = json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False)
    latest = directory / "latest.json"
    _atomic_write(latest, text + "\n")
    history = directory / "history.jsonl"
    lines = history.read_text(encoding="utf-8").splitlines() if history.exists() else []
    lines = [*lines[-(max(1, history_max) - 1) :], text]
    _atomic_write(history, "\n".join(lines) + "\n")
    return latest


# --------------------------------------------------------------------------------------
# Recolección desde el runtime (solo lecturas; la usa el runner).
# --------------------------------------------------------------------------------------


def collect_and_build(*, now: datetime | None = None) -> dict[str, Any]:
    """Lee BotState, la caché del RiskManager, la config y la DB (mode=ro). Sin I/O de red,
    sin escritura a la DB, sin llamar a ningún método que mute estado."""
    from src.monitoring.health import BotState
    from src.risk.manager import RiskManager
    from src.utils.config import get_settings

    now = now or datetime.now(UTC)
    settings = get_settings()
    books: dict[str, Any] = {}
    manager = BotState.v2_manager
    if manager is not None:
        try:
            stats = manager.stats()
            books = {
                "tracked": stats.get("tracked_tickers"),
                "initialized": stats.get("initialized_tickers"),
                "gaps_last_60s": stats.get("gaps_last_60s"),
                "sids_disabled": len(getattr(manager, "_recovery_disabled_sids", ()) or ()),
            }
        except Exception:  # lectura fail-open: sin stats → UNKNOWN, no sano
            books = {}
    runtime = {
        "started_at": BotState.started_at,
        "pid": os.getpid(),
        "db_initialized": BotState.db_initialized,
        "capture_running": BotState.capture_running,
        "last_ws_message": BotState.last_ws_message,
        "is_paused": BotState.is_paused,
        "pause_reason": BotState.pause_reason,
        "motor1_local_pause": BotState.motor1_local_pause,
        "last_error": BotState.last_error,
        "last_error_at": BotState.last_error_at,
        "books": books,
        "motor5": {
            "task_running": BotState.motor5_task_running,
            "last_tick": BotState.last_motor5_tick,
            "fair_flow_blocked": BotState.motor5_no_fair_since is not None,
            "book_flow_blocked": BotState.motor5_book_flow_blocked,
            "fee_policy_blocked": BotState.motor5_fee_policy_blocked,
            "experiment_invalid": BotState.motor5_experiment_invalid,
        },
    }
    config = {
        "kalshi_env": settings.KALSHI_ENV,
        "trading_enabled": settings.TRADING_ENABLED,
        "balance_refresh_seconds": settings.BALANCE_REFRESH_SECONDS,
        "motors": {
            "motor_1_arbitrage": {
                "enabled": settings.MOTOR_1_ARBITRAGE_ENABLED,
                "execution": settings.MOTOR_1_EXECUTION_ENABLED,
            },
            "motor_2_consensus": {
                "enabled": settings.MOTOR_2_SPORTSBOOK_ENABLED,
                "execution": settings.MOTOR_2_EXECUTION_ENABLED
                or settings.MOTOR_2_ENTRY_EXECUTION_ENABLED,
            },
            "motor_3_clv": {
                "enabled": settings.MOTOR_3_CLV_ENABLED,
                "execution": settings.MOTOR_3_EXECUTION_ENABLED,
            },
            "motor_5_mm": {
                "enabled": settings.MOTOR_MM_ENABLED,
                "execution": settings.MOTOR_MM_EXECUTION_ENABLED,
            },
            "motor_rest_arb": {
                "enabled": settings.MOTOR_REST_ENABLED,
                "execution": settings.MOTOR_REST_EXECUTION_ENABLED,
            },
        },
    }
    last_at = RiskManager._last_balance_at
    capital = {
        "raw_balance_usd": RiskManager._last_raw_balance_usd,
        "balance_at": last_at.replace(tzinfo=UTC) if isinstance(last_at, datetime) else None,
        "is_paused": RiskManager.capital_status().get("is_paused"),
    }
    ledger = read_ledger(sqlite_path_from_url(settings.DATABASE_URL), now=now)
    return build_snapshot(
        now=now,
        runtime=runtime,
        config=config,
        capital=capital,
        ledger=ledger,
        release=release_sha(),
        max_age_sec=max(DEFAULT_MAX_AGE_SEC, 3 * settings.SUPERVISION_SNAPSHOT_INTERVAL_SEC),
    )
