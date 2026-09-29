"""Isolated bootstrap for botkalshi. No legacy imports, signing or execution.

Stdlib only, Python >=3.12. Default service is OFFLINE. Public capture is an
explicit, bounded one-shot diagnostic, NOT an M2/M5 run or full market scan.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import ssl
import sys
import tempfile
import time
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, build_opener
import uuid

VERSION = "mac-lab-bootstrap-v1"
ORIGIN = "https://external-api.kalshi.com/trade-api/v2"
MAX_BODY = 2_000_000
MAX_DB = 2_000_000_000
TABLES = ("mm_shadow_fills", "mm_experiment_runs")
SECRET_NAMES = (
    "KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY", "KALSHI_PRIVATE_KEY_PATH",
    "ODDS_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN",
)
SAFE_FALSE = ("", "0", "false", "off", "no")


class LabError(RuntimeError):
    """Safe error: never contains a provider body or credential value."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def safety_check(environ: dict[str, str] | os._Environ[str] | None = None) -> None:
    env = os.environ if environ is None else environ
    if any(env.get(name, "").strip() for name in SECRET_NAMES):
        raise LabError("Credenciales detectadas: este laboratorio no las admite.")
    for key, value in env.items():
        if key == "TRADING_ENABLED" or key.endswith("EXECUTION_ENABLED"):
            if value.strip().lower() not in SAFE_FALSE:
                raise LabError("Flag de ejecucion incompatible con el laboratorio.")


def atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


@contextlib.contextmanager
def writer_lock(data: Path):
    data.mkdir(parents=True, exist_ok=True)
    with (data / "writer.lock").open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LabError("Otra instancia escribe aqui. Detenla antes de continuar.") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read_db(path: Path) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink():
        raise LabError("Snapshot inexistente, no regular o enlace simbolico.")
    if path.stat().st_size > MAX_DB:
        raise LabError("Snapshot supera el limite de 2 GB del inspector inicial.")
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA trusted_schema=OFF")
    con.enable_load_extension(False)
    deadline = time.monotonic() + 10
    con.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    return con


def inspect_snapshot(path: Path) -> dict:
    """Metadata/counts only; never return account rows, fills, SQL or paths."""
    # Inspect consistent standalone snapshots, not a partial copy of a live WAL DB.
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-journal")):
        raise LabError("Se requiere snapshot consistente; hay WAL/journal junto al archivo.")
    with contextlib.closing(read_db(path)) as con:
        con.execute("BEGIN")
        check = con.execute("PRAGMA quick_check(1)").fetchone()
        if check is None or check[0] != "ok":
            raise LabError("Snapshot no supera quick_check.")
        found = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name IN (?, ?)", TABLES
        )}
        result = {}
        for name in TABLES:
            # Names come from the fixed allowlist, never user input.
            result[name] = {
                "present": name in found,
                "rows": con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                if name in found else None,
            }
        con.rollback()
    return {"integrity": "ok", "tables": result, "economic_gate": "NOT_EVALUATED",
            "note": "Conteos no validan una cohorte, fills, saldo ni rentabilidad."}


def make_fixture(data: Path) -> Path:
    folder = data / "fixtures"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "synthetic-m5.sqlite3"
    if not path.exists():
        with contextlib.closing(sqlite3.connect(path)) as con:
            con.executescript("""
                CREATE TABLE mm_shadow_fills (id INTEGER PRIMARY KEY, metric_version TEXT);
                CREATE TABLE mm_experiment_runs (id INTEGER PRIMARY KEY, status TEXT);
                INSERT INTO mm_shadow_fills VALUES (1, 'SYNTHETIC_NOT_A_FILL');
                INSERT INTO mm_experiment_runs VALUES (1, 'synthetic');
            """)
            con.commit()
    return path


def save_report(data: Path, kind: str, payload: dict) -> dict:
    report = {
        "version": VERSION, "report_id": uuid.uuid4().hex, "generated_at": utc_now(),
        "kind": kind, "synthetic": kind == "SYNTHETIC",
        "legacy_motors_running": False, "execution_enabled": False,
        "account_data_connected": False, "result": payload,
    }
    text = json_text(report)
    # This DB is only a bootstrap report log, never the legacy financial ledger.
    with contextlib.closing(sqlite3.connect(data / "bootstrap.sqlite3")) as con:
        con.execute("CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY, kind TEXT, "
                    "created_at TEXT, sha256 TEXT, body TEXT)")
        con.execute("INSERT INTO reports VALUES (?, ?, ?, ?, ?)", (
            report["report_id"], kind, report["generated_at"],
            hashlib.sha256(text.encode()).hexdigest(), text,
        ))
        con.commit()
    atomic_text(data / "reports" / "latest.json", text)
    atomic_text(data / "reports" / "latest.md", (
        "# botkalshi — infraestructura local\n\n"
        f"**Tipo: {kind}.** Fecha UTC: {report['generated_at']}.\n\n"
        "M2/M5 no arrancados. Ordenes deshabilitadas. Sin datos de cuenta.\n\n"
        "Este reporte diagnostica infraestructura; no es un analisis de oportunidades.\n\n"
        "```json\n" + json_text(payload) + "```\n"
    ))
    return report


def offline_once(data: Path) -> dict:
    return save_report(data, "SYNTHETIC", inspect_snapshot(make_fixture(data)))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise LabError("Redireccion de proveedor bloqueada.")


class PublicReader:
    """GET-only fixed-origin diagnostics; no auth, proxies, redirects or retries."""
    def __init__(self):
        self.calls = 0
        self.opener = build_opener(
            ProxyHandler({}), NoRedirect(), HTTPSHandler(context=ssl.create_default_context())
        )

    @staticmethod
    def validated_url(method: str, path: str, params: dict[str, object] | None = None) -> str:
        params = {} if params is None else params
        if method != "GET":
            raise LabError("Solo GET publico permitido.")
        if path == "/markets":
            expected = {"series_ticker": "KXMLBGAME", "status": "open", "limit": 5}
        elif path == "/series/KXMLBGAME":
            expected = {}
        elif re.fullmatch(r"/markets/[A-Z0-9][A-Z0-9-]{0,127}/orderbook", path):
            expected = {"depth": 5}
        else:
            raise LabError("Ruta fuera de la lista de lecturas publicas.")
        if params != expected:
            raise LabError("Parametros fuera del contrato acotado.")
        return ORIGIN + path + ("?" + urlencode(params) if params else "")

    def get(self, path: str, params: dict[str, object] | None = None) -> dict:
        url = self.validated_url("GET", path, params)
        if self.calls >= 7:
            raise LabError("Limite de siete consultas por captura alcanzado.")
        self.calls += 1
        try:
            with self.opener.open(url, timeout=10) as response:
                raw = response.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise LabError("Respuesta demasiado grande.")
            def reject_constant(value):
                raise ValueError("non-finite")
            # Preserve decimal representations; this utility does not value contracts.
            body = json.loads(raw, parse_float=str, parse_constant=reject_constant)
        except HTTPError as exc:
            raise LabError(f"Proveedor HTTP {exc.code}; no se reintenta.") from None
        except (URLError, TimeoutError, OSError, ValueError, UnicodeError):
            raise LabError("Fallo de red o formato; captura incompleta, sin reintentos.") from None
        if not isinstance(body, dict):
            raise LabError("Respuesta JSON no es objeto.")
        return body


def capture_public(data: Path) -> dict:
    reader = PublicReader()
    payload: dict = {
        "provider": "Kalshi public REST", "series": "KXMLBGAME",
        "coverage": "PARTIAL_DIAGNOSTIC_MAX_5_NOT_A_SLATE_SCAN", "requests": 0,
        "markets": [], "recommendation": None, "status": "INCOMPLETE",
    }
    try:
        series = reader.get("/series/KXMLBGAME").get("series", {})
        if not isinstance(series, dict):
            raise LabError("Metadatos de serie invalidos.")
        # Observation only; never assert a fee schedule or executable price.
        payload["fee_metadata_observed"] = {
            k: series.get(k) for k in ("fee_type", "fee_multiplier")
        }
        body = reader.get("/markets", {"series_ticker": "KXMLBGAME", "status": "open", "limit": 5})
        markets = body.get("markets")
        if not isinstance(markets, list) or len(markets) > 5:
            raise LabError("Lista de mercados fuera del limite.")
        payload["more_pages"] = bool(body.get("cursor"))
        for market in markets:
            if not isinstance(market, dict):
                raise LabError("Mercado malformado.")
            ticker = market.get("ticker")
            if not isinstance(ticker, str) or not re.fullmatch(r"KXMLBGAME-[A-Z0-9-]{1,110}", ticker):
                raise LabError("Ticker inesperado.")
            book = reader.get(f"/markets/{ticker}/orderbook", {"depth": 5})
            fp = book.get("orderbook_fp", book.get("orderbook"))
            if not isinstance(fp, dict):
                raise LabError("Libro sin formato conocido.")
            # Keep only bounded numeric price/quantity levels, not arbitrary provider text.
            levels = {}
            for side in ("yes", "no", "yes_dollars", "no_dollars"):
                rows = fp.get(side)
                if rows is None:
                    continue
                if not isinstance(rows, list) or len(rows) > 5:
                    raise LabError("Profundidad fuera del limite.")
                if any(not isinstance(row, list) or len(row) != 2 or any(
                    not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", str(v)) for v in row
                ) for row in rows):
                    raise LabError("Nivel numerico invalido.")
                levels[side] = rows
            payload["markets"].append({
                "ticker": ticker, "received_at": utc_now(), "orderbook_levels": levels,
                "executable_ask_verified": False, "independent_probability": None,
            })
        payload["status"] = "CAPTURED_NOT_ANALYZED"
    except LabError as exc:
        payload["error"] = str(exc)
    payload["requests"] = reader.calls
    report = save_report(data, "PUBLIC_DIAGNOSTIC", payload)
    if payload["status"] != "CAPTURED_NOT_ANALYZED":
        raise LabError("Captura incompleta. Motivo saneado guardado en el reporte.")
    return report


def backup_database(source: Path, destination: Path) -> dict:
    if destination.exists() or source.resolve() == destination.resolve():
        raise LabError("Backup requiere un destino nuevo.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with contextlib.closing(read_db(source)) as src, contextlib.closing(sqlite3.connect(destination)) as dst:
            deadline = time.monotonic() + 15
            def progress(status, remaining, total):
                if time.monotonic() > deadline:
                    raise LabError("Backup excede tiempo permitido.")
            src.backup(dst, pages=64, progress=progress, sleep=0.1)
            if dst.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
                raise LabError("Backup no supera quick_check.")
        with destination.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        return {"status": "VERIFIED_SQLITE_COPY", "sha256": digest}
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def health(data: Path, max_age: float = 45) -> dict:
    record = json.loads((data / "health.json").read_text())
    age = (datetime.now(UTC) - datetime.fromisoformat(record["updated_at"])).total_seconds()
    if not record.get("running") or not 0 <= age <= max_age:
        raise LabError("Heartbeat ausente, detenido o vencido.")
    return record


def serve(data: Path, *, max_ticks: int | None = None, interval: float = 10) -> None:
    stop = False
    def request_stop(signum, frame):
        nonlocal stop
        stop = True
    old = {s: signal.signal(s, request_stop) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        with writer_lock(data):
            report = offline_once(data)
            ticks = 0
            print("OFFLINE: fixture sintetica cargada; sin red, motores ni claves.", flush=True)
            try:
                while not stop:
                    atomic_text(data / "health.json", json_text({
                        "running": True, "updated_at": utc_now(), "mode": "OFFLINE",
                        "report_id": report["report_id"], "execution_enabled": False,
                    }))
                    ticks += 1
                    if max_ticks is not None and ticks >= max_ticks:
                        break
                    time.sleep(interval)
            finally:
                atomic_text(data / "health.json", json_text({
                    "running": False, "updated_at": utc_now(), "mode": "OFFLINE",
                    "execution_enabled": False,
                }))
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("serve", "offline-once", "status", "healthcheck", "inspect", "backup", "public-once"))
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--allow-public-data", action="store_true")
    args = parser.parse_args()
    try:
        safety_check()
        if args.command == "healthcheck":
            health(args.data)
            return 0
        if args.command == "status":
            print(json_text(health(args.data)))
            return 0
        if args.command == "serve":
            serve(args.data)
            return 0
        with writer_lock(args.data):
            if args.command == "offline-once":
                result = offline_once(args.data)
            elif args.command == "inspect":
                if args.snapshot is None:
                    raise LabError("Falta --snapshot; no se crea una DB sustituta.")
                result = save_report(args.data, "USER_SNAPSHOT_METADATA", inspect_snapshot(args.snapshot))
            elif args.command == "backup":
                destination = args.data / "backups" / f"bootstrap-{uuid.uuid4().hex}.sqlite3"
                result = backup_database(args.data / "bootstrap.sqlite3", destination)
            else:
                if not args.allow_public_data:
                    raise LabError("Consulta publica requiere --allow-public-data explicito.")
                result = capture_public(args.data)
            print(json_text(result))
        return 0
    except (LabError, sqlite3.Error, OSError, ValueError) as exc:
        # Do not print exception payloads from SQLite/files/providers unless sanitized.
        print(str(exc) if isinstance(exc, LabError) else "Error local de datos; operacion detenida.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
