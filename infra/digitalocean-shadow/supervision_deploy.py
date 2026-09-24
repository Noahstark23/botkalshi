"""Installer / rollback / verifier of the read-only supervision units (S1/S2/S3).

Encargo ASTRA-DEPLOY-SUPERVISION-20260924. Same-host flow: the live runtime writes its
private snapshot; a root pre-step copies the evidence into the supervisor's inbox; the
audit and reader units consume that inbox with no network. This installer:

  install SHA --dedicated-research-host [--dry-run]
      verifies the release checkout at SHA (clean, exact HEAD), runs the focal tests with
      the SYSTEM python, creates the `botkalshi-supervisor` user and its 0700 state,
      backs up the previous state + units (0600 tar), writes an env TEMPLATE only if
      none exists (every key commented out: the units block until the owner fills it),
      renders the units to the release path, records RELEASE / RELEASE.previous, and runs
      `systemctl daemon-reload`. It does NOT enable or start any timer.
  rollback --dedicated-research-host [--dry-run]
      re-renders the units from RELEASE.previous (its release must still exist); state
      data is preserved (the audit is idempotent), the swap is recorded.
  verify
      read-only: rendered SHA, permissions (0700 dirs / 0600 files), env file shape,
      timers enabled or not. Changes nothing.

Boundaries (test-guarded): the only systemctl verbs used are `daemon-reload` and
`is-enabled`; nothing starts/stops/restarts/enables/disables the live bot, the research
collector or any timer; nothing sets flags, touches risk, calls set_pause, /admin/*,
clear_kill_switch, activation scripts, order clients or executors; no secret is read.
Stdlib only. `BOTKALSHI_SUPERVISION_TEST_ROOT` redirects every path under a prefix and
skips root/chown — for tests only.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tarfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

USER = "botkalshi-supervisor"
UNITS = (
    "botkalshi-supervision-audit.service",
    "botkalshi-supervision-audit.timer",
    "botkalshi-supervision-reader.service",
    "botkalshi-supervision-reader.timer",
)
TIMERS = ("botkalshi-supervision-audit.timer", "botkalshi-supervision-reader.timer")
STATE_SUBDIRS = ("inbox", "audit", "alerts", "reader")
FOCAL_TESTS = (
    "tests.test_production_audit",
    "tests.test_supervision_reader",
    "tests.test_supervision_deploy",
)
ENV_TEMPLATE = """\
# botkalshi supervision — paths of the LIVE runtime's evidence (read-only use).
# Only these two keys are accepted; no credential belongs here. Both stay commented
# until the owner fills them in: the units block (ENV_INCOMPLETE) until then.
# BOTKALSHI_LIVE_DB=/absolute/path/to/trades.db
# BOTKALSHI_LIVE_SNAPSHOT=/absolute/path/to/supervision/latest.json
"""
_SHA_RE = re.compile(r"[0-9a-f]{40}")
ALLOWED_SYSTEMCTL = frozenset({"daemon-reload", "is-enabled"})


class DeployError(Exception):
    pass


Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _default_runner(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=600, check=False, **kwargs)


class Deployer:
    def __init__(
        self,
        *,
        runner: Runner = _default_runner,
        root: Path | None = None,
        dry_run: bool = False,
        out=sys.stdout,
    ) -> None:
        test_root = os.environ.get("BOTKALSHI_SUPERVISION_TEST_ROOT")
        self.root = root or (Path(test_root) if test_root else Path("/"))
        self.testing = self.root != Path("/")
        self.runner = runner
        self.dry_run = dry_run
        self.out = out
        self.plan: list[str] = []

    # -- paths --------------------------------------------------------------------
    def p(self, absolute: str) -> Path:
        return self.root / absolute.lstrip("/")

    @property
    def releases(self) -> Path:
        return self.p("/opt/botkalshi-research/releases")

    @property
    def state(self) -> Path:
        return self.p("/var/lib/botkalshi-supervision")

    @property
    def units_dir(self) -> Path:
        return self.p("/etc/systemd/system")

    @property
    def env_file(self) -> Path:
        return self.p("/etc/botkalshi-supervision.env")

    @property
    def backups(self) -> Path:
        return self.p("/var/backups/botkalshi-supervision")

    # -- helpers ------------------------------------------------------------------
    def say(self, text: str) -> None:
        self.plan.append(text)
        print(text, file=self.out)

    def run(
        self,
        argv: list[str],
        *,
        mutating: bool,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        if argv[0].endswith("systemctl") and argv[1] not in ALLOWED_SYSTEMCTL:
            raise DeployError(f"systemctl verb not allowed: {argv[1]}")
        if mutating and self.dry_run:
            self.say(f"DRY-RUN would run: {' '.join(argv)}")
            return subprocess.CompletedProcess(argv, 0, "", "")
        kwargs = {}
        if cwd is not None:
            kwargs["cwd"] = str(cwd)
        if env is not None:
            kwargs["env"] = env
        return self.runner(argv, **kwargs)

    def write(self, path: Path, data: bytes, mode: int) -> None:
        if self.dry_run:
            self.say(f"DRY-RUN would write {path} ({oct(mode)})")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)

    def chown_user(self, path: Path) -> None:
        if self.testing or self.dry_run:
            return
        import pwd

        entry = pwd.getpwnam(USER)
        os.chown(path, entry.pw_uid, entry.pw_gid)

    # -- checks -------------------------------------------------------------------
    def require_privileges(self) -> None:
        if not self.testing and os.geteuid() != 0:
            raise DeployError("requires root on the dedicated research droplet")

    def release_dir(self, sha: str) -> Path:
        if not _SHA_RE.fullmatch(sha):
            raise DeployError("SHA must be a full 40-hex commit")
        release = self.releases / sha
        if not (release / ".git").exists():
            raise DeployError("release checkout missing (install it with install.sh first)")
        head = self.run(["git", "-C", str(release), "rev-parse", "HEAD"], mutating=False)
        if head.returncode != 0 or head.stdout.strip() != sha:
            raise DeployError("release HEAD does not match SHA")
        dirty = self.run(
            ["git", "-C", str(release), "status", "--porcelain", "--untracked-files=all"],
            mutating=False,
        )
        if dirty.returncode != 0 or dirty.stdout.strip():
            raise DeployError("release checkout is not clean")
        for unit in UNITS:
            if not (release / "infra/digitalocean-shadow" / unit).is_file():
                raise DeployError(f"release has no {unit}")
        return release

    def run_focal_tests(self, release: Path) -> None:
        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}
        done = self.run(
            ["/usr/bin/python3", "-m", "unittest", *FOCAL_TESTS],
            mutating=False,
            cwd=release / "infra/digitalocean-shadow",
            env=env,
        )
        if done.returncode != 0:
            raise DeployError("focal tests failed with the system python; nothing installed")
        self.say("focal tests OK with /usr/bin/python3")

    def current_release(self) -> dict | None:
        path = self.state / "RELEASE"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    # -- actions ------------------------------------------------------------------
    def ensure_user(self) -> None:
        if self.run(["id", "-u", USER], mutating=False).returncode == 0:
            return
        self.run(
            ["useradd", "--system", "--home", "/nonexistent", "--shell", "/usr/sbin/nologin", USER],
            mutating=True,
        )

    def ensure_state(self) -> None:
        for directory in (self.state, *(self.state / s for s in STATE_SUBDIRS)):
            if self.dry_run:
                self.say(f"DRY-RUN would ensure {directory} (0700, {USER})")
                continue
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
            self.chown_user(directory)

    def backup(self, label: str) -> Path | None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = self.backups / f"{stamp}-{label}.tar.gz"
        members = [self.state] + [
            self.units_dir / u for u in UNITS if (self.units_dir / u).exists()
        ]
        if not any(m.exists() for m in members):
            return None
        if self.dry_run:
            self.say(f"DRY-RUN would back up state and units to {target} (0600)")
            return target
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for member in members:
                if member.exists():
                    tar.add(member, arcname=str(member.relative_to(self.root)))
        self.backups.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.backups, 0o700)
        self.write(target, buffer.getvalue(), 0o600)
        self.say(f"backup written: {target}")
        return target

    def ensure_env_template(self) -> None:
        if self.env_file.exists():
            self.say("env file kept as is (never overwritten)")
            return
        self.write(self.env_file, ENV_TEMPLATE.encode(), 0o600)
        self.say("env TEMPLATE written; the units block until the owner fills it")

    def render_units(self, release: Path) -> None:
        for unit in UNITS:
            text = (release / "infra/digitalocean-shadow" / unit).read_text(encoding="utf-8")
            # Same convention as install.sh: /opt/botkalshi/ → the release's REAL path.
            real_release = "/" + release.relative_to(self.root).as_posix() + "/"
            text = text.replace("/opt/botkalshi/", real_release)
            self.write(self.units_dir / unit, text.encode(), 0o644)

    def record(self, sha: str, previous: dict | None) -> None:
        now = datetime.now(UTC).isoformat()
        if previous is not None:
            self.write(self.state / "RELEASE.previous", json.dumps(previous).encode(), 0o600)
        self.write(
            self.state / "RELEASE", json.dumps({"sha": sha, "installed_at": now}).encode(), 0o600
        )

    def install(self, sha: str) -> dict:
        self.require_privileges()
        release = self.release_dir(sha)
        self.run_focal_tests(release)
        previous = self.current_release()
        self.ensure_user()
        self.backup((previous or {}).get("sha", "none")[:12])
        self.ensure_state()
        self.ensure_env_template()
        self.render_units(release)
        self.record(sha, previous)
        self.run(["systemctl", "daemon-reload"], mutating=True)
        self.say("installed; timers NOT enabled. Enabling them is an owner decision.")
        return {
            "installed": sha,
            "previous": (previous or {}).get("sha"),
            "dry_run": self.dry_run,
            "timers_enabled": False,
        }

    def rollback(self) -> dict:
        self.require_privileges()
        current = self.current_release()
        try:
            previous = json.loads((self.state / "RELEASE.previous").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DeployError("no RELEASE.previous to roll back to") from exc
        release = self.release_dir(previous["sha"])
        self.backup(f"rollback-from-{(current or {}).get('sha', 'none')[:12]}")
        self.render_units(release)
        self.record(previous["sha"], current)
        self.run(["systemctl", "daemon-reload"], mutating=True)
        self.say(f"rolled back units to {previous['sha']}; state preserved")
        return {
            "installed": previous["sha"],
            "previous": (current or {}).get("sha"),
            "dry_run": self.dry_run,
        }

    def verify(self) -> dict:
        problems: list[str] = []
        current = self.current_release()
        if current is None:
            problems.append("NOT_INSTALLED")
        for directory in (self.state, *(self.state / s for s in STATE_SUBDIRS)):
            if directory.exists() and (directory.stat().st_mode & 0o777) != 0o700:
                problems.append(f"DIR_MODE:{directory.name}")
        for path in self.state.rglob("*"):
            if path.is_file() and (path.stat().st_mode & 0o077):
                problems.append(f"FILE_MODE:{path.name}")
        if self.env_file.exists() and (self.env_file.stat().st_mode & 0o022):
            problems.append("ENV_WRITABLE_BY_OTHERS")
        rendered = None
        unit = self.units_dir / "botkalshi-supervision-audit.service"
        if unit.exists():
            match = re.search(r"releases/([0-9a-f]{40})/", unit.read_text(encoding="utf-8"))
            rendered = match.group(1) if match else None
            if current and rendered != current.get("sha"):
                problems.append("UNITS_NOT_AT_RECORDED_SHA")
        timers = {}
        for timer in TIMERS:
            done = self.run(["systemctl", "is-enabled", timer], mutating=False)
            timers[timer] = (done.stdout or "").strip() or "unknown"
        return {
            "recorded_sha": (current or {}).get("sha"),
            "units_sha": rendered,
            "timers": timers,
            "problems": problems,
            "ok": not problems,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    inst = sub.add_parser("install")
    inst.add_argument("sha")
    inst.add_argument("--dedicated-research-host", action="store_true", required=True)
    inst.add_argument("--dry-run", action="store_true")
    rb = sub.add_parser("rollback")
    rb.add_argument("--dedicated-research-host", action="store_true", required=True)
    rb.add_argument("--dry-run", action="store_true")
    sub.add_parser("verify")
    args = parser.parse_args(argv)
    deployer = Deployer(dry_run=getattr(args, "dry_run", False))
    try:
        if args.command == "install":
            result = deployer.install(args.sha)
        elif args.command == "rollback":
            result = deployer.rollback()
        else:
            result = deployer.verify()
    except DeployError as exc:
        print(json.dumps({"status": "REFUSED", "reason": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok", True) else 3


if __name__ == "__main__":
    raise SystemExit(main())
