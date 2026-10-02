#!/usr/bin/env python3
"""Reporte SANEADO y READ-ONLY de una unidad systemd del bot (definición instalada + overrides).

POR QUÉ (preflight 2026-10-02): el live ya no se describe solo por Coolify — el preflight habla
de `botkalshi-live.service` (inactiva) y `botkalshi-runtime.service`, y NINGUNA de las dos está
versionada en el repo. ExecStart, WorkingDirectory, fuente de entorno, SHA desplegado, DB y
flags quedaron UNKNOWN. Este script es la lectura autorizada que los resuelve SIN secretos y
SIN tocar la unidad: lo que imprime se puede pegar tal cual en el canal de agentes.

QUÉ LEE (todo read-only):
  - `systemctl show` con una ALLOWLIST de propiedades (estado, enablement, ExecStart, WD,
    EnvironmentFiles, drop-ins, triggers, NeedDaemonReload).
  - `systemctl cat`: fragmento + drop-ins (overrides), con cada línea saneada.
  - stat + sha256 de los archivos de la unidad (para vincular esta lectura con las próximas).
  - del WorkingDirectory: `git rev-parse`/`status` con --no-optional-locks (no reescribe el
    index) → SHA instalado y si hay cambios locales.
  - de la DB: SOLO stat (tamaño/mtime/-wal). JAMÁS la abre.

QUÉ NO HACE JAMÁS:
  - start/stop/restart/reload/enable/disable/daemon-reload/kill: el único ejecutor de comandos
    (`_run_readonly`) solo admite `systemctl show|cat` y git de lectura (test-guard).
  - no abre EnvironmentFile ni `.env` salvo `--flags`; y aun con `--flags` imprime valores
    SOLO de una allowlist de flags técnicos (TRADING_ENABLED, MOTOR_*_ENABLED, capital…).
    Cualquier nombre con KEY/TOKEN/SECRET/PASS/PRIVATE/… se redacta SIEMPRE, gane o no la
    allowlist. Del resto de variables se listan solo los NOMBRES.
  - no lee /proc/PID/environ, no consulta Kalshi, no imprime comentarios de los unit files
    (pueden traer cualquier cosa), y una guarda final borra cualquier línea con material PEM.

USO (en el host, con lectura expresamente autorizada; no necesita sudo):
  # sin copiar nada al host — corre en memoria, no deja .pyc:
  ssh HOST 'python3 -B -I - --unit botkalshi-live.service' < scripts/live_unit_report.py
  # varias unidades (comparar live vs runtime) y SHA esperado:
  ssh HOST 'python3 -B -I - --unit botkalshi-live.service --unit botkalshi-runtime.service \\
      --expected-sha 9bf685d' < scripts/live_unit_report.py
  # opt-in: valores de flags NO secretos desde EnvironmentFile/.env (puede requerir sudo si los
  # archivos son 0600 — es una lectura distinta y se autoriza aparte):
  ssh HOST 'sudo python3 -B -I - --unit botkalshi-live.service --flags' < scripts/live_unit_report.py
  # unidad de usuario (systemctl --user):
  ... --user

Stdlib solamente y compatible con python3 >= 3.8: corre con `-I` fuera del repo.
Exit: 0 = reporte producido; 2 = systemctl ausente o ninguna unidad encontrada.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time

# ─── allowlists ──────────────────────────────────────────────────────────────────────────

# Propiedades que se piden a `systemctl show`. Allowlist: lo que no está acá no se pide
# (p. ej. SetCredential podría traer un secreto literal).
SHOW_PROPERTIES = (
    "Id",
    "Names",
    "Description",
    "LoadState",
    "LoadError",
    "ActiveState",
    "SubState",
    "UnitFileState",
    "UnitFilePreset",
    "FragmentPath",
    "SourcePath",
    "DropInPaths",
    "NeedDaemonReload",
    "TriggeredBy",
    "WantedBy",
    "RequiredBy",
    "Requires",
    "Wants",
    "BindsTo",
    "PartOf",
    "After",
    "Conflicts",
    "Type",
    "Restart",
    "RestartUSec",
    "NRestarts",
    "Result",
    "User",
    "Group",
    "DynamicUser",
    "WorkingDirectory",
    "RootDirectory",
    "EnvironmentFiles",
    "Environment",
    "PassEnvironment",
    "UnsetEnvironment",
    "ExecStartPre",
    "ExecStart",
    "ExecStartPost",
    "ExecStop",
    "ExecReload",
    "MainPID",
    "ExecMainPID",
    "ExecMainStartTimestamp",
    "ExecMainExitTimestamp",
    "ExecMainCode",
    "ExecMainStatus",
    "ActiveEnterTimestamp",
    "InactiveEnterTimestamp",
    "StateChangeTimestamp",
    "MemoryMax",
    "TimeoutStartUSec",
    "KillMode",
    "ReadWritePaths",
    "StateDirectory",
)

# Nombres que se redactan SIEMPRE (precede a la allowlist de valores).
SENSITIVE_NAME = re.compile(
    r"KEY|TOKEN|SECRET|PASS|PWD|PRIVATE|CREDENTIAL|AUTH|COOKIE|SESSION|CHAT_ID|PEM|SIGN",
    re.IGNORECASE,
)

# Variables cuyo VALOR es un campo técnico no secreto (lo que el preflight necesita para
# saber si el live puede abrir posiciones). Todo lo demás: solo el nombre.
VALUE_ALLOWLIST = frozenset(
    {
        "KALSHI_ENV",
        "TRADING_ENABLED",
        "MOTOR_3_MANAGES_ORPHANS",
        "EXPERIMENT_BANK200_ENABLED",
        "EXPERIMENT_BANK_CONFIRMED_USD",
        "EXPERIMENT_START_AT",
        "EXPERIMENT_BALANCE_MAX_AGE_SEC",
        "ACTIVE_CAPITAL_USD",
        "DYNAMIC_CAPITAL_ENABLED",
        "CAPITAL_CAP_USD",
        "CAPITAL_FLOOR_USD",
        "CAPITAL_SMOOTHING_PCT",
        "MAX_TRADE_SIZE_USD",
        "MAX_TRADE_SIZE_PCT",
        "MAX_SIMULTANEOUS_EXPOSURE_PCT",
        "MAX_EVENT_DIRECTIONAL_EXPOSURE_USD",
        "DATABASE_URL",
        "LOG_LEVEL",
        "PYTHONPATH",
    }
)
VALUE_ALLOWLIST_RE = re.compile(r"^MOTOR_[A-Z0-9_]+_ENABLED$")

# Default de src/utils/config.py cuando DATABASE_URL no está en ninguna fuente.
CODE_DEFAULT_DATABASE_URL = "sqlite:////app/data/trades.db"

SECRET = "<secreto-redactado>"
OMITTED = "<valor-omitido>"

# ─── ejecución read-only (test-guard) ────────────────────────────────────────────────────

SYSTEMCTL_READONLY_VERBS = frozenset({"show", "cat"})
GIT_READONLY_SUBCOMMANDS = frozenset({"rev-parse", "log", "status", "remote"})


class ReadOnlyViolationError(RuntimeError):
    """Se intentó ejecutar algo que no es una lectura. Bug del script, jamás se ignora."""


def _assert_readonly(argv: list[str]) -> None:
    if not argv:
        raise ReadOnlyViolationError("argv vacío")
    tool = os.path.basename(argv[0])
    if tool == "systemctl":
        verbs = [a for a in argv[1:] if not a.startswith("-")]
        if not verbs or verbs[0] not in SYSTEMCTL_READONLY_VERBS:
            raise ReadOnlyViolationError(
                f"systemctl solo admite {sorted(SYSTEMCTL_READONLY_VERBS)}"
            )
        return
    if tool == "git":
        if "--no-optional-locks" not in argv:
            raise ReadOnlyViolationError("git exige --no-optional-locks (no reescribir el index)")
        rest = argv[1:]
        i = 0
        while i < len(rest) and rest[i].startswith("-"):
            # opciones globales con argumento separado
            i += 2 if rest[i] in ("-c", "-C") else 1
        sub = rest[i] if i < len(rest) else ""
        if sub not in GIT_READONLY_SUBCOMMANDS:
            raise ReadOnlyViolationError(f"git solo admite {sorted(GIT_READONLY_SUBCOMMANDS)}")
        if sub == "remote" and rest[i + 1 : i + 2] != ["get-url"]:
            raise ReadOnlyViolationError("git remote solo admite get-url")
        return
    raise ReadOnlyViolationError(f"herramienta no permitida: {tool}")


def _run_readonly(argv: list[str], timeout: float = 20.0) -> tuple[int, str, str]:
    """ÚNICO punto que ejecuta comandos. Valida ANTES de lanzar."""
    _assert_readonly(argv)
    env = {
        "PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin"),
        "LC_ALL": "C",
        "SYSTEMD_PAGER": "",
        "SYSTEMD_COLORS": "0",
        "PAGER": "cat",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }
    for passthrough in ("HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
        if passthrough in os.environ:  # necesarios para `systemctl --user`
            env[passthrough] = os.environ[passthrough]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", type(exc).__name__
    return proc.returncode, proc.stdout, proc.stderr


# ─── saneamiento ─────────────────────────────────────────────────────────────────────────

_URL_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]+@")
_QUERY_SECRET = re.compile(
    r"(?P<k>[?&;](?:[^=&\s]*(?:key|token|secret|pass|sig|auth)[^=&\s]*))=[^&\s]+", re.I
)
_OPAQUE = re.compile(r"^[A-Za-z0-9+/=_-]{32,}$")
_ASSIGN = re.compile(r"^(?P<k>[A-Za-z_][A-Za-z0-9_]*)=(?P<v>.*)$", re.S)
_OPT_ASSIGN = re.compile(r"^(?P<k>--?[A-Za-z0-9][A-Za-z0-9_.-]*)=(?P<v>.*)$", re.S)
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def _looks_opaque(tok: str) -> bool:
    """Token largo tipo base64/hex/uuid. Una ruta (`/x`, `-/x`, `~/x`) nunca es opaca."""
    body = tok.lstrip("-")
    if body.startswith(("/", "~", ".")):
        return False
    return bool(_OPAQUE.match(body))


def value_printable(name: str) -> bool:
    if SENSITIVE_NAME.search(name):
        return False
    return name in VALUE_ALLOWLIST or bool(VALUE_ALLOWLIST_RE.match(name))


def redact_url(value: str) -> str:
    value = _URL_USERINFO.sub(lambda m: m.group("scheme") + "<redactado>@", value)
    return _QUERY_SECRET.sub(lambda m: m.group("k") + "=" + SECRET, value)


def render_assignment(name: str, value: str) -> str:
    if SENSITIVE_NAME.search(name):
        return f"{name}={SECRET}"
    if value_printable(name):
        return f"{name}={redact_url(value)}"
    return f"{name}={OMITTED}"


def _scrub_token(tok: str, prev: str) -> str:
    if "BEGIN" in tok and "KEY" in tok:
        return SECRET
    if prev.startswith("-") and SENSITIVE_NAME.search(prev) and "=" not in prev:
        return SECRET  # `--token VALOR`
    m = _OPT_ASSIGN.match(tok)
    if m:
        if SENSITIVE_NAME.search(m.group("k")):
            return f"{m.group('k')}={SECRET}"
        return f"{m.group('k')}={_scrub_token(m.group('v'), '')}"
    m = _ASSIGN.match(tok)
    if m:
        key, value = m.group("k"), m.group("v")
        if SENSITIVE_NAME.search(key):
            return f"{key}={SECRET}"
        if _ENV_NAME.match(key):  # `env FOO=bar` / `docker run -e FOO=bar`
            return render_assignment(key, value)
        # `path=/opt/...`, `pid=0` del formato de `systemctl show`: se conserva saneado
        return f"{key}={_scrub_token(value, '')}"
    if _looks_opaque(tok):
        return "<opaco-redactado>"
    return redact_url(tok)


def scrub_free_text(text: str) -> str:
    """Sanea un valor libre (ExecStart, rutas, descripciones) token por token."""
    out: list[str] = []
    prev = ""
    for tok in re.split(r"(\s+)", text):
        if not tok or tok.isspace():
            out.append(tok)
            continue
        out.append(_scrub_token(tok, prev))
        prev = tok
    return "".join(out)


def parse_env_assignments(value: str) -> list[tuple[str, str]] | None:
    """`Environment=` con la sintaxis de systemd (comillas). None si no parsea."""
    try:
        parts = shlex.split(value, posix=True)
    except ValueError:
        return None
    pairs: list[tuple[str, str]] = []
    for part in parts:
        m = _ASSIGN.match(part)
        if not m:
            return None
        pairs.append((m.group("k"), m.group("v")))
    return pairs


def render_env_value(value: str) -> str:
    pairs = parse_env_assignments(value)
    if pairs is None:
        return "<no-parseable: redactado completo>"
    return " ".join(render_assignment(k, v) for k, v in pairs)


_CRED_SECRET_KEYS = frozenset({"SetCredential", "SetCredentialEncrypted"})
_HEADER = re.compile(r"^# /\S+$")


def _join_continuations(text: str) -> list[str]:
    lines: list[str] = []
    buf = ""
    for raw in text.splitlines():
        piece = raw.lstrip() if buf else raw
        if raw.endswith("\\") and not raw.lstrip().startswith(("#", ";")):
            buf += piece[:-1].rstrip() + " "
            continue
        lines.append(buf + piece)
        buf = ""
    if buf:
        lines.append(buf)
    return lines


def sanitize_unit_text(text: str) -> list[str]:
    """Sanea la salida de `systemctl cat` línea por línea."""
    out: list[str] = []
    for line in _join_continuations(text):
        stripped = line.strip()
        if not stripped:
            out.append("")
        elif _HEADER.match(stripped):
            out.append(stripped)  # cabecera que agrega systemctl cat: ruta del archivo
        elif stripped.startswith(("#", ";")):
            out.append("# <comentario omitido>")
        elif stripped.startswith("[") and stripped.endswith("]"):
            out.append(stripped)
        elif "=" in stripped:
            key, _, value = stripped.partition("=")
            key = key.strip()
            value = value.strip()
            if key == "Environment":
                out.append(f"Environment={render_env_value(value)}" if value else "Environment=")
            elif key in _CRED_SECRET_KEYS:
                name = value.split(":", 1)[0]
                out.append(f"{key}={scrub_free_text(name)}:{SECRET}")
            else:
                out.append(f"{key}={scrub_free_text(value)}")
        else:
            out.append(scrub_free_text(stripped))
    return out


def final_guard(text: str) -> str:
    """Última barrera: ninguna línea con material de clave sale del script."""
    safe = []
    for line in text.splitlines():
        if "PRIVATE KEY" in line or "-----BEGIN" in line or "-----END" in line:
            safe.append("<línea eliminada por la guarda final: material de clave>")
        else:
            safe.append(line)
    return "\n".join(safe) + "\n"


# ─── parsing de systemctl show ───────────────────────────────────────────────────────────


def parse_show(text: str) -> dict[str, list[str]]:
    props: dict[str, list[str]] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            props.setdefault(key, []).append(value)
    return props


def first(props: dict[str, list[str]], key: str) -> str:
    vals = props.get(key) or [""]
    return vals[0]


def parse_environment_files(values: list[str]) -> list[tuple[str, bool]]:
    """`EnvironmentFiles=/ruta (ignore_errors=yes)` → [(ruta, opcional)]."""
    files: list[tuple[str, bool]] = []
    for value in values:
        for chunk in re.findall(r"(\S+)(?: \(ignore_errors=(yes|no)\))?", value):
            path, ignore = chunk
            if path.startswith("/"):
                files.append((path, ignore == "yes"))
    return files


_ARGV = re.compile(r"argv\[\]=(?P<argv>.*?) ;")


def exec_argv(props: dict[str, list[str]], key: str = "ExecStart") -> list[str]:
    m = _ARGV.search(first(props, key))
    if not m:
        return []
    try:
        return shlex.split(m.group("argv"))
    except ValueError:
        return m.group("argv").split()


def docker_env_files(argv: list[str]) -> list[str]:
    files = []
    for i, tok in enumerate(argv):
        if tok == "--env-file" and i + 1 < len(argv):
            files.append(argv[i + 1])
        elif tok.startswith("--env-file="):
            files.append(tok.split("=", 1)[1])
    return files


# ─── parsing de env files (solo con --flags) ─────────────────────────────────────────────


def parse_env_file(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        pairs.append((key, value))
    return pairs


# ─── hechos de archivos / git / DB ───────────────────────────────────────────────────────


def _utc(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _owner(st: os.stat_result) -> str:
    try:
        import grp
        import pwd

        return f"{pwd.getpwuid(st.st_uid).pw_name}:{grp.getgrgid(st.st_gid).gr_name}"
    except (ImportError, KeyError):
        return f"{st.st_uid}:{st.st_gid}"


def file_facts(path: str, *, with_hash: bool) -> str:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return f"{path}: NO EXISTE"
    except OSError as exc:
        return f"{path}: no legible ({type(exc).__name__})"
    facts = (
        f"{path}: owner={_owner(st)} mode={stat.filemode(st.st_mode)} "
        f"size={st.st_size} mtime={_utc(st.st_mtime)}"
    )
    if with_hash and stat.S_ISREG(st.st_mode):
        try:
            with open(path, "rb") as fh:
                facts += f" sha256={hashlib.sha256(fh.read()).hexdigest()[:16]}"
        except OSError as exc:
            facts += f" sha256=no-legible({type(exc).__name__})"
    return facts


def _git(wd: str, *args: str) -> tuple[int, str]:
    rc, out, err = _run_readonly(
        ["git", "--no-optional-locks", "-c", f"safe.directory={wd}", "-C", wd, *args]
    )
    return rc, (out.strip() if rc == 0 else (err.strip().splitlines() or ["?"])[-1])


def git_facts(wd: str, expected_sha: str | None) -> list[str]:
    if not wd or not os.path.isdir(wd):
        return [f"WorkingDirectory {wd or '(vacío)'}: NO EXISTE o no es directorio"]
    if shutil.which("git") is None:
        return ["git no disponible en el host: SHA UNKNOWN"]
    rc, sha = _git(wd, "rev-parse", "HEAD")
    if rc != 0:
        return [f"SHA: UNKNOWN — no es un checkout git legible ({redact_url(sha)})"]
    lines = [f"SHA instalado: {sha}"]
    _, branch = _git(wd, "rev-parse", "--abbrev-ref", "HEAD")
    _, date = _git(wd, "log", "-1", "--format=%cI")
    _, dirty = _git(wd, "status", "--porcelain", "--untracked-files=no")
    rc_remote, remote = _git(wd, "remote", "get-url", "origin")
    lines.append(f"branch: {branch}  commit_date: {date}")
    changed = len([ln for ln in dirty.splitlines() if ln.strip()])
    lines.append(f"archivos trackeados modificados: {changed}" + ("  ⚠️ DIRTY" if changed else ""))
    lines.append(f"origin: {redact_url(remote) if rc_remote == 0 else 'sin remote origin'}")
    if expected_sha:
        verdict = "MATCH" if sha.startswith(expected_sha.lower()) else "MISMATCH"
        lines.append(f"SHA esperado {expected_sha}: {verdict}")
    return lines


def sqlite_path(url: str, wd: str) -> str | None:
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return None
    path = url[len(prefix) :].split("?", 1)[0]
    return path if path.startswith("/") else os.path.join(wd or "/", path)


def db_facts(url: str, source: str, wd: str) -> list[str]:
    lines = [f"DATABASE_URL ({source}): {redact_url(url)}"]
    path = sqlite_path(url, wd)
    if path is None:
        lines.append("no es sqlite: no se inspecciona (solo se reporta la URL saneada)")
        return lines
    for suffix in ("", "-wal", "-shm"):
        lines.append("  " + file_facts(path + suffix, with_hash=False))
    lines.append("  (la DB NO se abrió: kill-switch persistente sigue UNKNOWN)")
    return lines


# ─── reporte por unidad ──────────────────────────────────────────────────────────────────


def _systemctl(user: bool, *args: str) -> list[str]:
    argv = ["systemctl"]
    if user:
        argv.append("--user")
    argv.append("--no-pager")
    argv.extend(args)
    return argv


def report_unit(unit: str, *, user: bool, flags: bool, expected_sha: str | None) -> list[str]:
    out = [f"=== UNIT {unit} ==="]
    rc, show_out, show_err = _run_readonly(
        _systemctl(user, "show", unit, "--property=" + ",".join(SHOW_PROPERTIES))
    )
    if rc != 0:
        out.append(f"systemctl show falló (rc={rc}): {scrub_free_text(show_err.strip())}")
        return out
    props = parse_show(show_out)
    load_state = first(props, "LoadState")
    warnings: list[str] = []

    out.append("--- estado ---")
    for key in (
        "LoadState",
        "ActiveState",
        "SubState",
        "UnitFileState",
        "UnitFilePreset",
        "NeedDaemonReload",
        "MainPID",
        "Result",
        "NRestarts",
        "ExecMainStartTimestamp",
        "ExecMainExitTimestamp",
        "ExecMainCode",
        "ExecMainStatus",
        "ActiveEnterTimestamp",
        "InactiveEnterTimestamp",
        "StateChangeTimestamp",
    ):
        out.append(f"{key}: {scrub_free_text(first(props, key))}")
    if load_state != "loaded":
        out.append(f"LoadError: {scrub_free_text(first(props, 'LoadError'))}")
        warnings.append(f"LoadState={load_state}: la unidad no está cargada/encontrada")

    active = first(props, "ActiveState")
    if active in ("active", "activating", "reloading"):
        warnings.append(f"ActiveState={active}: la unidad está CORRIENDO")
    enablement = first(props, "UnitFileState")
    if enablement in ("enabled", "enabled-runtime", "linked", "linked-runtime", "alias"):
        warnings.append(
            f"UnitFileState={enablement}: un reboot del host la ARRANCA "
            f"(WantedBy={first(props, 'WantedBy') or '-'})"
        )
    if first(props, "TriggeredBy"):
        warnings.append(f"TriggeredBy={first(props, 'TriggeredBy')}: otra unidad puede arrancarla")
    if first(props, "NeedDaemonReload") == "yes":
        warnings.append(
            "NeedDaemonReload=yes: el archivo en disco DIFIERE de lo cargado en systemd "
            "(no ejecutar daemon-reload sin decisión del operador)"
        )

    out.append("--- definición efectiva (cargada en systemd) ---")
    for key in (
        "Description",
        "Type",
        "User",
        "Group",
        "DynamicUser",
        "WorkingDirectory",
        "RootDirectory",
        "ExecStartPre",
        "ExecStart",
        "ExecStartPost",
        "ExecStop",
        "ExecReload",
        "Restart",
        "RestartUSec",
        "TimeoutStartUSec",
        "KillMode",
        "MemoryMax",
        "ReadWritePaths",
        "StateDirectory",
        "WantedBy",
        "RequiredBy",
        "TriggeredBy",
        "Requires",
        "Wants",
        "BindsTo",
        "PartOf",
        "After",
        "Conflicts",
        "PassEnvironment",
        "UnsetEnvironment",
    ):
        for value in props.get(key, []):
            if value:
                out.append(f"{key}: {scrub_free_text(value)}")
    for value in props.get("Environment", []):
        if value:
            out.append(f"Environment: {render_env_value(value)}")

    out.append("--- archivos de la unidad (fragmento + drop-ins/overrides) ---")
    fragment = first(props, "FragmentPath")
    dropins = [p for v in props.get("DropInPaths", []) for p in v.split() if p]
    unit_files = ([fragment] if fragment else []) + dropins
    if not unit_files:
        out.append("(ninguno)")
    for path in unit_files:
        out.append(file_facts(path, with_hash=True))
    out.append(f"drop-ins/overrides: {len(dropins)}")
    if first(props, "SourcePath"):
        out.append(f"SourcePath (generada): {first(props, 'SourcePath')}")

    rc_cat, cat_out, cat_err = _run_readonly(_systemctl(user, "cat", unit))
    out.append("--- systemctl cat (saneado: comentarios omitidos, secretos redactados) ---")
    if rc_cat == 0:
        out.extend(sanitize_unit_text(cat_out))
    else:
        out.append(f"systemctl cat falló (rc={rc_cat}): {scrub_free_text(cat_err.strip())}")

    wd = first(props, "WorkingDirectory").lstrip("!-")
    argv = exec_argv(props)
    out.append("--- runtime ---")
    if argv:
        interp = argv[0]
        out.append("interprete/binario: " + file_facts(interp, with_hash=False))
        if "-m" in argv and argv.index("-m") + 1 < len(argv):
            out.append(f"modulo: {argv[argv.index('-m') + 1]}")
        cfg = os.path.join(os.path.dirname(os.path.dirname(interp)), "pyvenv.cfg")
        if os.path.isfile(cfg):
            try:
                with open(cfg, encoding="utf-8") as fh:
                    for line in fh:
                        if line.split("=", 1)[0].strip() in ("version", "version_info"):
                            out.append(f"venv {line.strip()}")
            except OSError as exc:
                out.append(f"pyvenv.cfg no legible ({type(exc).__name__})")
    else:
        out.append("ExecStart: sin argv parseable")

    out.append("--- código instalado (WorkingDirectory) ---")
    out.extend(git_facts(wd, expected_sha))

    out.append("--- fuentes de entorno (orden de precedencia, la última gana) ---")
    env_files = parse_environment_files(props.get("EnvironmentFiles", []))
    env_files += [(p, False) for p in docker_env_files(argv)]
    out.append("1. Environment= inline (arriba, saneado)")
    for i, (path, optional) in enumerate(env_files, start=2):
        tag = " (opcional '-')" if optional else ""
        out.append(f"{i}. EnvironmentFile{tag}: " + file_facts(path, with_hash=False))
    dotenv = os.path.join(wd, ".env") if wd else ""
    out.append(
        "n. pydantic .env del WorkingDirectory (MENOR precedencia que el entorno del proceso): "
        + (file_facts(dotenv, with_hash=False) if dotenv else "WD vacío")
    )

    # Valores: inline siempre (ya es parte de la definición); archivos solo con --flags.
    process_env: dict[str, tuple[str, str]] = {}
    for value in props.get("Environment", []):
        for k, v in parse_env_assignments(value) or []:
            process_env[k] = (v, "Environment=")
    dotenv_vals: dict[str, str] = {}
    out.append("--- flags ---")
    if flags:
        for path, _optional in env_files:
            names = _read_env_into(path, process_env, out)
            if names is not None:
                out.append(f"{path}: {len(names)} variables → {', '.join(sorted(names))}")
        if dotenv and os.path.isfile(dotenv):
            tmp: dict[str, tuple[str, str]] = {}
            names = _read_env_into(dotenv, tmp, out)
            dotenv_vals = {k: v for k, (v, _src) in tmp.items()}
            if names is not None:
                out.append(f"{dotenv}: {len(names)} variables → {', '.join(sorted(names))}")
    else:
        out.append(
            "EnvironmentFile/.env NO abiertos (sin --flags): los flags de abajo solo reflejan "
            "Environment= inline"
        )
    keys = sorted(
        {k for k in list(process_env) + list(dotenv_vals) if value_printable(k)}
        | {"TRADING_ENABLED", "KALSHI_ENV", "DATABASE_URL"}
    )
    # En systemd EnvironmentFile= PISA a Environment=: sin abrirlo, el inline no es el efectivo.
    unverified = "; NO efectivo garantizado: un EnvironmentFile sin abrir puede pisarlo"
    for key in keys:
        if key in process_env:
            val, src = process_env[key]
            caveat = unverified if (not flags and env_files) else ""
            out.append(f"{render_assignment(key, val)}   [{src}{caveat}]")
        elif key in dotenv_vals:
            out.append(f"{render_assignment(key, dotenv_vals[key])}   [.env]")
        else:
            src = "default del código" if flags else "UNKNOWN sin --flags"
            out.append(f"{key}=<no seteada>   [{src}]")

    out.append("--- DB ---")
    if "DATABASE_URL" in process_env:
        url, src = process_env["DATABASE_URL"]
        out.extend(db_facts(url, src, wd))
    elif "DATABASE_URL" in dotenv_vals:
        out.extend(db_facts(dotenv_vals["DATABASE_URL"], ".env", wd))
    elif flags:
        out.extend(db_facts(CODE_DEFAULT_DATABASE_URL, "default del código", wd))
    else:
        out.append(
            f"DATABASE_URL: UNKNOWN sin --flags — stat del default del código "
            f"({CODE_DEFAULT_DATABASE_URL}), NO verificado como el de la unidad:"
        )
        out.extend(db_facts(CODE_DEFAULT_DATABASE_URL, "default del código", wd)[1:])

    out.append("--- WARNINGS ---")
    if warnings:
        out.extend(f"⚠️ {w}" for w in warnings)
    else:
        out.append("(ninguno)")
    return out


def _read_env_into(path: str, sink: dict[str, tuple[str, str]], out: list[str]) -> list[str] | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            pairs = parse_env_file(fh.read())
    except OSError as exc:
        out.append(f"{path}: no legible ({type(exc).__name__}) — ¿requiere sudo?")
        return None
    for k, v in pairs:
        sink[k] = (v, path)
    return [k for k, _v in pairs]


# ─── main ────────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--unit", action="append", help="unidad systemd (repetible)")
    parser.add_argument("--user", action="store_true", help="usar systemctl --user")
    parser.add_argument(
        "--flags",
        action="store_true",
        help="abrir EnvironmentFile/.env e imprimir SOLO valores de la allowlist no secreta",
    )
    parser.add_argument("--expected-sha", help="SHA (o prefijo) esperado del checkout")
    args = parser.parse_args(argv)
    units = args.unit or ["botkalshi-live.service"]

    lines = [
        "BOTKALSHI LIVE UNIT REPORT — READ ONLY",
        f"timestamp_utc: {_utc(time.time())}",
        f"host: {os.uname().nodename if hasattr(os, 'uname') else 'UNKNOWN'}",
        f"python: {sys.version.split()[0]}  uid: {os.getuid() if hasattr(os, 'getuid') else '?'}",
        f"modo: {'--flags (allowlist de valores)' if args.flags else 'definición (sin abrir env files)'}",
    ]
    if shutil.which("systemctl") is None:
        lines.append("systemctl NO disponible en este host: no es el host de la unidad")
        sys.stdout.write(final_guard("\n".join(lines)))
        return 2

    found = 0
    for unit in units:
        unit_lines = report_unit(
            unit, user=args.user, flags=args.flags, expected_sha=args.expected_sha
        )
        if any(line == "LoadState: loaded" for line in unit_lines):
            found += 1
        lines.extend(["", *unit_lines])
    lines.extend(
        [
            "",
            "READ-ONLY: no se inició, detuvo, recargó ni modificó ninguna unidad; "
            "no se abrió la DB; no se consultó Kalshi.",
        ]
    )
    sys.stdout.write(final_guard("\n".join(lines)))
    return 0 if found else 2


if __name__ == "__main__":
    raise SystemExit(main())
