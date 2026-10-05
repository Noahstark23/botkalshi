"""Publicación atómica de un reporte acotado; lectura caducada nunca devuelve OK."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from src.analytics.portfolio_report import (
    ReportDataError,
    json_safe,
    timestamp,
    validate_complete_snapshot,
)

MAX_REPORT_BYTES = 20_000_000


def publish_report(path: Path, report: dict) -> None:
    """Solo reemplaza el archivo final después de escribirlo completo; modo 0600."""
    payload = json.dumps(json_safe(report), ensure_ascii=False).encode()
    if len(payload) > MAX_REPORT_BYTES:
        raise ValueError("Presupuesto de reporte agotado")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".operator-report-", delete=False
        ) as f:
            temporary = Path(f.name)
            os.fchmod(f.fileno(), 0o600)
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_report(path: Path, *, max_age_seconds: int = 600, now: datetime | None = None) -> dict:
    """El wrapper SSH puede ejecutar este lector sin acceso de red ni credenciales."""
    try:
        if not isinstance(max_age_seconds, int) or not 1 <= max_age_seconds <= 3600:
            raise ReportDataError("report: TTL inválido")
        with path.open("rb") as f:
            payload = f.read(MAX_REPORT_BYTES + 1)
        if len(payload) > MAX_REPORT_BYTES:
            raise ReportDataError("report: tamaño fuera de presupuesto")
        report = json.loads(payload, parse_float=Decimal)
        if not isinstance(report, dict) or report.get("schema_version") != 1:
            raise ReportDataError("report: esquema desconocido")
        if report.get("read_only") is not True or report.get("authorizes_trading") is not False:
            raise ReportDataError("report: origen no readonly")
        if report.get("status") not in {"OK", "ATENCION"}:
            raise ReportDataError("report: estado desconocido")
        if report["status"] == "OK":
            validate_complete_snapshot(report)
        observed = timestamp(report.get("collection_finished_at"), "collection_finished_at")
        started = timestamp(report.get("collection_started_at"), "collection_started_at")
        instant = now or datetime.now(UTC)
        if instant.tzinfo is None or observed < started:
            raise ReportDataError("report: intervalo de colección inválido")
        age = (instant - observed).total_seconds()
        if age < -5 or age > max_age_seconds:
            raise ReportDataError("report: ausente, futuro o caducado; estado actual desconocido")
        report["export_age_seconds"] = max(0, age)
        return report
    except ReportDataError as exc:
        reason = str(exc)
    except (OSError, ValueError, TypeError) as exc:
        reason = f"report: no evaluable ({type(exc).__name__})"
    return {
        "schema_version": 1,
        "status": "ATENCION",
        "read_only": True,
        "authorizes_trading": False,
        "errors": [reason],
        "pnl_net_usd": None,
    }
