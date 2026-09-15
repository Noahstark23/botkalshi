"""Read-only local verification and draft exports. No network or trading client.

Inputs are untrusted local artifacts from collector.py. Verification concerns
capture health only: it proves neither strategy correctness nor live deployment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from datetime import UTC, datetime
from typing import Any

VERSION = "ops-report-v1"
MAX_INPUT = 4_000_000


class EvidenceError(ValueError):
    """Safe diagnostic without raw user/provider content."""


def timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise EvidenceError("INVALID_TIMESTAMP")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise EvidenceError("INVALID_TIMESTAMP") from None
    if parsed.tzinfo is None:
        raise EvidenceError("NAIVE_TIMESTAMP")
    return parsed.astimezone(UTC)


def read_object(path: Path) -> dict[str, Any]:
    # No symlinks, device files, directories, enormous input or non-finite values.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_INPUT:
            raise EvidenceError("INVALID_INPUT_FILE")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise EvidenceError("INPUT_TOO_LARGE")
    finally:
        os.close(fd)
    def reject_constant(_: str) -> None:
        raise EvidenceError("NON_FINITE_JSON")
    def no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        obj: dict[str, Any] = {}
        for key, value in pairs:
            if key in obj:
                raise EvidenceError("DUPLICATE_JSON_KEY")
            obj[key] = value
        return obj
    try:
        result = json.loads(raw, parse_float=str, parse_constant=reject_constant,
                            object_pairs_hook=no_duplicate_keys)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise EvidenceError("INVALID_JSON") from None
    if not isinstance(result, dict):
        raise EvidenceError("JSON_NOT_OBJECT")
    return result


def evaluate(health: dict[str, Any] | None, packet: dict[str, Any] | None,
             now: datetime, *, after: datetime | None = None,
             max_age: int = 180) -> dict[str, Any]:
    if now.tzinfo is None or after is not None and after.tzinfo is None:
        raise EvidenceError("NAIVE_CHECK_TIME")
    if not 1 <= max_age <= 3600:
        raise EvidenceError("INVALID_MAX_AGE")
    reasons: list[str] = []
    packet_id = None
    count = None
    generated = None
    age = None
    digest = None
    if health is None:
        reasons.append("HEALTH_UNAVAILABLE")
    else:
        if health.get("mode") != "SHADOW_READONLY":
            reasons.append("MODE_NOT_READONLY")
        if health.get("execution_authorized") is not False:
            reasons.append("EXECUTION_FLAG_NOT_FALSE")
        if health.get("running") is not True:
            reasons.append("PROCESS_NOT_REPORTED_RUNNING")
        if health.get("last_error"):
            reasons.append("COLLECTOR_ERROR")
        try:
            when = timestamp(health.get("updated_at"))
            if not 0 <= (now - when).total_seconds() <= max_age:
                reasons.append("HEALTH_STALE_OR_FUTURE")
            if after is not None and when < after:
                reasons.append("HEALTH_BEFORE_START")
        except EvidenceError:
            reasons.append("HEALTH_TIME_INVALID")
    if packet is None:
        reasons.append("PACKET_UNAVAILABLE")
    else:
        if packet.get("schema_version") != "botkalshi-research-packet-v1":
            reasons.append("PACKET_SCHEMA_UNSUPPORTED")
        if packet.get("mode") != "SHADOW_READONLY":
            reasons.append("PACKET_MODE_NOT_READONLY")
        if packet.get("execution_authorized") is not False:
            reasons.append("PACKET_EXECUTION_FLAG_NOT_FALSE")
        raw_id = packet.get("packet_id")
        if not isinstance(raw_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", raw_id):
            reasons.append("PACKET_ID_INVALID")
        else:
            packet_id = raw_id
        try:
            when = timestamp(packet.get("generated_at"))
            generated = when.isoformat()
            age = (now - when).total_seconds()
            if not 0 <= age <= max_age:
                reasons.append("PACKET_STALE_OR_FUTURE")
            if after is not None and when < after:
                reasons.append("PACKET_BEFORE_START")
        except EvidenceError:
            reasons.append("PACKET_TIME_INVALID")
        source = packet.get("kalshi")
        source = source if isinstance(source, dict) else {}
        markets, stated = source.get("markets"), source.get("market_count")
        if (not isinstance(markets, list) or type(stated) is not int or
                not 0 <= stated <= 100 or len(markets) != stated):
            reasons.append("MARKET_COUNT_INVALID")
        else:
            count = stated
        # Do not relay arbitrary news, strings, balances, account IDs or model output.
        try:
            encoded = json.dumps(packet, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=False, allow_nan=False).encode()
            digest = hashlib.sha256(encoded).hexdigest()
        except (ValueError, TypeError, RecursionError):
            reasons.append("PACKET_NOT_SERIALIZABLE")
    if health is not None and packet_id is not None:
        if health.get("last_cycle_id") != packet_id:
            reasons.append("CYCLE_MISMATCH")
        if count is not None and health.get("kalshi_markets") != count:
            reasons.append("HEALTH_COUNT_MISMATCH")
    reasons = sorted(set(reasons))
    return {
        "schema_version": VERSION,
        "checked_at": now.astimezone(UTC).isoformat(),
        "capture_status": "CAPTURE_VERIFIED_LOCAL" if not reasons else "BLOCKED",
        "blockers": reasons,
        "packet_id": packet_id,
        "packet_sha256": digest,
        "packet_generated_at": generated,
        "packet_age_seconds": age,
        "observed_market_count": count,
        "coverage": "PARTIAL_OR_UNVERIFIED_NOT_FULL_SLATE",
        "executable_quotes_verified": False,
        "strategy_validated": False,
        "bank_reconciled": False,
        "real_pnl_usd": None,
        "live_execution_authorized": False,
        "ai_connected": False,
        "whatsapp_delivery": "NOT_SENT",
        "muse_connection": "NOT_CONNECTED",
        "youtube_publication": "NOT_PUBLISHED",
    }


def render_drafts(report: dict[str, Any]) -> dict[str, str]:
    # Text derives solely from evaluate's bounded output, not raw packet text.
    count = report["observed_market_count"]
    text = (
        "Reporte técnico botkalshi — BORRADOR NO ENVIADO\n"
        f"Corte UTC: {report['checked_at']}\n"
        f"Captura: {report['capture_status']}\n"
        f"Mercados observados: {count if count is not None else 'no verificado'}\n"
        "Cobertura parcial/no verificada; no es análisis de oportunidades.\n"
        "No prueba despliegue remoto ni operación de M2/M5.\n"
        "Órdenes reales no autorizadas. Sin P&L conciliado ni señal de inversión.\n"
        "WhatsApp/Muse/YouTube: integración o publicación no ejecutada.\n"
    )
    if report["blockers"]:
        text += "Bloqueos: " + ", ".join(report["blockers"]) + "\n"
    muse = (
        "# Encargo editorial — borrador para Muse, no enviado\n\n"
        "Objetivo: explicar un componente de ingeniería usando evidencia fechada.\n"
        "No inventes operaciones, resultados, capturas del servidor o autorización de acceso.\n"
        "No publiques automáticamente ni conectes cuentas financieras.\n\n"
        "## Evidencia disponible\n\n" + text + "\n"
        "## Entregable\n\n"
        "Guion en español de 60–90 segundos: problema, prueba, limitación y siguiente paso.\n"
        "Título propuesto: Un servidor encendido no es un bot funcionando.\n"
        "Visuales: diagrama original y evidencia saneada; cualquier recreación debe rotularse.\n"
        "Cierra sin señales, enlaces de fondeo, promesas de ingresos o invitación a copiar trades.\n"
        "Verifica capacidades y permisos reales de tu cuenta antes de editar o publicar.\n"
    )
    return {"whatsapp_draft.txt": text, "muse_brief.md": muse,
            "status.md": "# Estado técnico\n\n" + text}


def write_bundle(destination: Path, report: dict[str, Any]) -> Path:
    # Never overwrite a previous observation or create files at arbitrary packet IDs.
    destination.mkdir(parents=True, exist_ok=True)
    bundle = Path(tempfile.mkdtemp(prefix="ops-", dir=destination))
    try:
        files = render_drafts(report)
        files["status.json"] = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        for name, content in files.items():
            path = bundle / name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        (bundle / "COMPLETE").write_text("drafts only; no network delivery\n", encoding="utf-8")
    except Exception:
        # Partial bundles have no COMPLETE marker and must not be consumed.
        raise
    return bundle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--after", type=timestamp)
    parser.add_argument("--max-age", type=int, default=180)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    inputs = []
    for relative in ("health.json", "packets/latest.json"):
        try:
            inputs.append(read_object(args.data / relative))
        except (EvidenceError, OSError):
            inputs.append(None)
    try:
        report = evaluate(inputs[0], inputs[1], datetime.now(UTC), after=args.after,
                          max_age=args.max_age)
        if args.output:
            write_bundle(args.output, report)
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
        return 0 if report["capture_status"] == "CAPTURE_VERIFIED_LOCAL" else 3
    except (EvidenceError, OSError, ValueError):
        print("No se pudo verificar/exportar el estado; no hay confirmación de envío.", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
