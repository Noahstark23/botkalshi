"""
scripts/live_unit_report.py — lectura saneada de la unidad systemd del live (preflight 2026-10-02).

Lo que se verifica acá es lo que hace seguro correrlo en el host del live:
  - JAMÁS ejecuta un verbo que mute systemd (start/stop/restart/daemon-reload/…): el único
    ejecutor valida ANTES de lanzar, y el end-to-end registra cada invocación real.
  - JAMÁS imprime un secreto: ni de Environment= inline, ni de ExecStart, ni de un
    EnvironmentFile (que además NO se abre sin --flags), ni material PEM.
  - Imprime lo que el preflight necesita: estado, enablement (reboot = arranque), drop-ins,
    ExecStart, WD, SHA instalado, fuentes de entorno y flags técnicos de la allowlist.
  - Corre fuera del repo con `python3 -B -I`: solo stdlib.
"""

from __future__ import annotations

import ast
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from scripts import live_unit_report as lur

SCRIPT = Path(lur.__file__)
SECRETS = (
    "SUPERSECRETAPIKEYID-1234567890",
    "tg-bot-token-abcdefghijklmnop",
    "oddsapikeyvalue0123456789abcdef",
    "hunter2password",
)

# ─── guard read-only (estructural) ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "verb",
    [
        "start",
        "stop",
        "restart",
        "reload",
        "try-restart",
        "reload-or-restart",
        "enable",
        "disable",
        "mask",
        "unmask",
        "daemon-reload",
        "kill",
        "reset-failed",
        "set-property",
        "edit",
        "isolate",
        "revert",
        "preset",
    ],
)
def test_systemctl_mutating_verbs_are_refused(verb):
    with pytest.raises(lur.ReadOnlyViolationError):
        lur._assert_readonly(["systemctl", "--no-pager", verb, "botkalshi-live.service"])
    with pytest.raises(lur.ReadOnlyViolationError):
        lur._assert_readonly(["systemctl", "--user", verb, "botkalshi-live.service"])


def test_systemctl_readonly_verbs_are_allowed():
    lur._assert_readonly(["systemctl", "--no-pager", "show", "x.service", "--property=Id"])
    lur._assert_readonly(["systemctl", "--user", "--no-pager", "cat", "x.service"])


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "-C", "/app", "rev-parse", "HEAD"],  # sin --no-optional-locks
        ["git", "--no-optional-locks", "-C", "/app", "checkout", "main"],
        ["git", "--no-optional-locks", "-C", "/app", "pull"],
        ["git", "--no-optional-locks", "-C", "/app", "fetch"],
        ["git", "--no-optional-locks", "-C", "/app", "remote", "set-url", "origin", "x"],
        ["git", "--no-optional-locks", "-C", "/app", "reset", "--hard"],
        ["bash", "-c", "systemctl start botkalshi-live.service"],
        ["sudo", "systemctl", "show", "x"],
        ["sqlite3", "/app/data/trades.db"],
        [],
    ],
)
def test_non_readonly_commands_are_refused(argv):
    with pytest.raises(lur.ReadOnlyViolationError):
        lur._assert_readonly(argv)


def test_git_readonly_commands_are_allowed():
    base = ["git", "--no-optional-locks", "-c", "safe.directory=/app", "-C", "/app"]
    lur._assert_readonly([*base, "rev-parse", "HEAD"])
    lur._assert_readonly([*base, "log", "-1", "--format=%cI"])
    lur._assert_readonly([*base, "status", "--porcelain", "--untracked-files=no"])
    lur._assert_readonly([*base, "remote", "get-url", "origin"])


def test_run_readonly_validates_before_launching(monkeypatch):
    launched = []
    monkeypatch.setattr(lur.subprocess, "run", lambda *a, **k: launched.append(a))
    with pytest.raises(lur.ReadOnlyViolationError):
        lur._run_readonly(["systemctl", "start", "botkalshi-live.service"])
    assert launched == []


def test_subprocess_is_only_called_from_the_guarded_runner():
    """Nadie lanza procesos por fuera de `_run_readonly` (que valida antes)."""
    tree = ast.parse(SCRIPT.read_text())
    offenders = []
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef) or func.name == "_run_readonly":
            continue
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in ("subprocess", "os")
                and node.attr in ("run", "Popen", "call", "check_call", "check_output", "system")
            ) or (isinstance(node, ast.Attribute) and node.attr.startswith(("exec", "spawn"))):
                offenders.append(f"{func.name}: {node.attr}")
    assert offenders == []


def test_script_is_stdlib_only():
    """Corre con `python3 -B -I` en el host, fuera del repo y sin el venv del bot."""
    tree = ast.parse(SCRIPT.read_text())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module.split(".")[0])
    assert mods - set(sys.stdlib_module_names) - {"__future__"} == set()


# ─── saneamiento ─────────────────────────────────────────────────────────────────────────


def test_allowlist_never_collides_with_sensitive_names():
    for name in lur.VALUE_ALLOWLIST:
        assert lur.value_printable(name), name


@pytest.mark.parametrize(
    "name",
    [
        "KALSHI_API_KEY_ID",
        "KALSHI_PRIVATE_KEY",
        "KALSHI_PRIVATE_KEY_PATH",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "ODDS_API_KEY",
        "DB_PASSWORD",
        "MOTOR_X_SECRET_ENABLED",  # gana la redacción aunque matchee el regex de la allowlist
    ],
)
def test_sensitive_names_are_always_redacted(name):
    assert lur.render_assignment(name, "valor-sensible") == f"{name}={lur.SECRET}"


def test_unknown_names_print_only_the_name():
    assert lur.render_assignment("SOME_RANDOM_SETTING", "x") == f"SOME_RANDOM_SETTING={lur.OMITTED}"


def test_allowlisted_flags_print_their_value():
    assert lur.render_assignment("TRADING_ENABLED", "false") == "TRADING_ENABLED=false"
    assert lur.render_assignment("MOTOR_1_EXECUTION_ENABLED", "true") == (
        "MOTOR_1_EXECUTION_ENABLED=true"
    )


def test_database_url_userinfo_is_redacted():
    out = lur.render_assignment("DATABASE_URL", "postgresql://bot:hunter2password@db:5432/x")
    assert "hunter2password" not in out
    assert "db:5432/x" in out


def test_environment_line_with_quotes():
    out = lur.render_env_value(
        '"KALSHI_API_KEY_ID=SUPERSECRETAPIKEYID-1234567890" TRADING_ENABLED=false "X=a b"'
    )
    assert "SUPERSECRETAPIKEYID" not in out
    assert "TRADING_ENABLED=false" in out
    assert f"X={lur.OMITTED}" in out


def test_unparseable_environment_is_fully_redacted():
    assert "secret" not in lur.render_env_value('"A=secret')


def test_execstart_scrubbing():
    text = (
        "/usr/bin/env KALSHI_API_KEY_ID=SUPERSECRETAPIKEYID-1234567890 TRADING_ENABLED=false "
        "/opt/bot/venv/bin/python -m src.runner --token tg-bot-token-abcdefghijklmnop "
        "--api-key=oddsapikeyvalue0123456789abcdef --log-level=INFO "
        "https://user:hunter2password@example.com/x?token=abc"
    )
    out = lur.scrub_free_text(text)
    for secret in SECRETS:
        assert secret not in out
    assert "?token=abc" not in out
    assert "/opt/bot/venv/bin/python -m src.runner" in out
    assert "TRADING_ENABLED=false" in out
    assert "--log-level=INFO" in out


def test_systemctl_show_execstart_shape_is_preserved():
    value = (
        "{ path=/opt/bot/venv/bin/python ; argv[]=/opt/bot/venv/bin/python -m src.runner ; "
        "ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; "
        "status=0/0 }"
    )
    assert lur.scrub_free_text(value) == value
    props = lur.parse_show(f"ExecStart={value}\n")
    assert lur.exec_argv(props) == ["/opt/bot/venv/bin/python", "-m", "src.runner"]


def test_opaque_tokens_are_redacted_but_long_paths_are_not():
    assert lur.scrub_free_text("a3f9c2e1b7d84f0a9e6c5b2d1f8e7a6c") == "<opaco-redactado>"
    long_path = "-/etc/botkalshi/environment_live_files_production"
    assert lur.scrub_free_text(long_path) == long_path


def test_sanitize_unit_text():
    cat = textwrap.dedent(
        """\
        # /etc/systemd/system/botkalshi-live.service
        [Unit]
        Description=botkalshi live
        # token viejo: tg-bot-token-abcdefghijklmnop
        [Service]
        WorkingDirectory=/opt/botkalshi
        Environment="TELEGRAM_BOT_TOKEN=tg-bot-token-abcdefghijklmnop" TRADING_ENABLED=false
        EnvironmentFile=-/etc/botkalshi/live.env
        SetCredential=kalshi:hunter2password
        ExecStart=/opt/botkalshi/venv/bin/python \\
            -m src.runner
        ; KALSHI_API_KEY_ID=SUPERSECRETAPIKEYID-1234567890

        # /etc/systemd/system/botkalshi-live.service.d/override.conf
        [Service]
        Environment=MOTOR_1_EXECUTION_ENABLED=true
        """
    )
    out = "\n".join(lur.sanitize_unit_text(cat))
    for secret in SECRETS:
        assert secret not in out
    assert "# /etc/systemd/system/botkalshi-live.service" in out
    assert "# /etc/systemd/system/botkalshi-live.service.d/override.conf" in out
    assert "EnvironmentFile=-/etc/botkalshi/live.env" in out
    assert f"SetCredential=kalshi:{lur.SECRET}" in out
    assert "ExecStart=/opt/botkalshi/venv/bin/python -m src.runner" in out
    assert "TRADING_ENABLED=false" in out
    assert "Environment=MOTOR_1_EXECUTION_ENABLED=true" in out
    assert out.count("# <comentario omitido>") == 2


def test_final_guard_drops_pem_lines():
    text = "ok\n-----BEGIN PRIVATE KEY-----\nMIIEv\n-----END PRIVATE KEY-----\nfin"
    out = lur.final_guard(text)
    assert "BEGIN" not in out and "END PRIVATE" not in out
    assert out.startswith("ok\n") and out.rstrip().endswith("fin")


def test_parse_environment_files():
    vals = [
        "/etc/botkalshi/live.env (ignore_errors=yes)",
        "/etc/botkalshi/b.env (ignore_errors=no)",
    ]
    assert lur.parse_environment_files(vals) == [
        ("/etc/botkalshi/live.env", True),
        ("/etc/botkalshi/b.env", False),
    ]


def test_sqlite_path():
    assert lur.sqlite_path("sqlite:////app/data/trades.db", "/x") == "/app/data/trades.db"
    assert lur.sqlite_path("sqlite:///data/trades.db", "/opt/bot") == "/opt/bot/data/trades.db"
    assert lur.sqlite_path("postgresql://h/db", "/x") is None


# ─── end-to-end con systemctl falso ──────────────────────────────────────────────────────


def _fake_systemctl(bindir: Path, log: Path, show: str, cat: str) -> None:
    bindir.mkdir()
    (bindir / "show.txt").write_text(show)
    (bindir / "cat.txt").write_text(cat)
    script = bindir / "systemctl"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{log}"\n'
        'for a in "$@"; do\n'
        f'  case "$a" in show) cat "{bindir}/show.txt"; exit 0;; '
        f'cat) cat "{bindir}/cat.txt"; exit 0;; esac\n'
        "done\n"
        "exit 1\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def host(tmp_path):
    """Host simulado: unidad instalada + drop-in + env file con secretos + checkout git."""
    wd = tmp_path / "opt" / "botkalshi"
    wd.mkdir(parents=True)
    git = ["git", "-C", str(wd), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], check=True)
    (wd / "README.md").write_text("x")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    sha = subprocess.run(
        [*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    (wd / ".env").write_text(f"ODDS_API_KEY={SECRETS[2]}\nKALSHI_ENV=demo\n")

    etc = tmp_path / "etc"
    etc.mkdir()
    unit = etc / "botkalshi-live.service"
    unit.write_text("[Service]\nExecStart=/opt/botkalshi/venv/bin/python -m src.runner\n")
    dropin = etc / "override.conf"
    dropin.write_text("[Service]\nEnvironment=TRADING_ENABLED=false\n")
    envfile = etc / "live.env"
    envfile.write_text(
        f"KALSHI_API_KEY_ID={SECRETS[0]}\n"
        f"TELEGRAM_BOT_TOKEN='{SECRETS[1]}'\n"
        "-----BEGIN PRIVATE KEY-----\n"
        "KALSHI_ENV=production\n"
        "MOTOR_1_EXECUTION_ENABLED=true\n"
        "TRADING_ENABLED=false\n"
        f"DATABASE_URL=sqlite:///{tmp_path}/data/trades.db\n"
    )
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "trades.db").write_bytes(b"SQLite format 3\x00")

    show = textwrap.dedent(
        f"""\
        Id=botkalshi-live.service
        LoadState=loaded
        ActiveState=inactive
        SubState=dead
        UnitFileState=enabled
        WantedBy=multi-user.target
        NeedDaemonReload=no
        MainPID=0
        FragmentPath={unit}
        DropInPaths={dropin}
        WorkingDirectory={wd}
        User=bot
        EnvironmentFiles={envfile} (ignore_errors=no)
        Environment="KALSHI_API_KEY_ID={SECRETS[0]}" TRADING_ENABLED=false
        ExecStart={{ path=/opt/botkalshi/venv/bin/python ; argv[]=/opt/botkalshi/venv/bin/python -m src.runner ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }}
        Restart=always
        """
    )
    cat = textwrap.dedent(
        f"""\
        # {unit}
        [Service]
        # clave vieja {SECRETS[3]}
        ExecStart=/opt/botkalshi/venv/bin/python -m src.runner
        EnvironmentFile={envfile}

        # {dropin}
        [Service]
        Environment=TRADING_ENABLED=false
        """
    )
    bindir = tmp_path / "bin"
    log = tmp_path / "systemctl.log"
    _fake_systemctl(bindir, log, show, cat)
    return {"bindir": bindir, "log": log, "sha": sha, "envfile": envfile, "wd": wd}


def _run_script(host, *extra):
    env = {**os.environ, "PATH": f"{host['bindir']}{os.pathsep}{os.environ['PATH']}"}
    proc = subprocess.run(
        [sys.executable, "-B", "-I", str(SCRIPT), "--unit", "botkalshi-live.service", *extra],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_e2e_default_mode_reports_definition_without_secrets(host):
    rc, out = _run_script(host, "--expected-sha", host["sha"][:7])
    assert rc == 0, out
    for secret in SECRETS:
        assert secret not in out
    assert "BEGIN" not in out
    assert "ActiveState: inactive" in out
    assert "UnitFileState=enabled: un reboot del host la ARRANCA" in out
    assert "drop-ins/overrides: 1" in out
    assert f"SHA instalado: {host['sha']}" in out
    assert "MATCH" in out and "MISMATCH" not in out
    assert "modulo: src.runner" in out
    assert "EnvironmentFile/.env NO abiertos" in out
    # el inline NO se presenta como efectivo: el EnvironmentFile (sin abrir) puede pisarlo
    assert "TRADING_ENABLED=false   [Environment=; NO efectivo garantizado" in out
    assert "KALSHI_ENV=<no seteada>   [UNKNOWN sin --flags]" in out
    assert "READ-ONLY" in out


def test_e2e_default_mode_never_opens_env_files(host, monkeypatch):
    """Sin --flags el env file no se abre: ni siquiera se lee para hashearlo."""
    opened = []
    real_open = open

    def spy(path, *a, **k):
        opened.append(str(path))
        return real_open(path, *a, **k)

    monkeypatch.setenv("PATH", f"{host['bindir']}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr("builtins.open", spy)
    assert lur.main(["--unit", "botkalshi-live.service"]) == 0
    assert str(host["envfile"]) not in opened
    assert str(host["wd"] / ".env") not in opened


def test_e2e_flags_mode_prints_only_allowlisted_values(host):
    rc, out = _run_script(host, "--flags")
    assert rc == 0, out
    for secret in SECRETS:
        assert secret not in out
    assert "BEGIN" not in out
    # el env file gana sobre Environment= y sobre el .env de pydantic
    assert "KALSHI_ENV=production" in out
    assert "MOTOR_1_EXECUTION_ENABLED=true" in out
    assert "KALSHI_API_KEY_ID" in out  # el NOMBRE sí aparece (lista de variables)
    assert "trades.db: owner=" in out  # stat de la DB, sin abrirla


def test_e2e_only_readonly_verbs_reach_systemctl(host):
    _run_script(host, "--flags")
    calls = host["log"].read_text().splitlines()
    assert calls, "el systemctl falso no fue invocado"
    for call in calls:
        verbs = [a for a in call.split() if not a.startswith("-")]
        assert verbs[0] in ("show", "cat"), call


def test_e2e_missing_unit_exits_2(tmp_path):
    bindir = tmp_path / "bin"
    log = tmp_path / "log"
    _fake_systemctl(bindir, log, "Id=x.service\nLoadState=not-found\nActiveState=inactive\n", "")
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
    proc = subprocess.run(
        [sys.executable, "-B", "-I", str(SCRIPT), "--unit", "x.service"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 2
    assert "LoadState=not-found" in proc.stdout
