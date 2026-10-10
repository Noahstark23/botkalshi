#!/bin/bash
# Fixed, root-only systemd preflight. It opens only allow-listed collector
# artifacts with O_NOFOLLOW and grants ACLs to those pinned inodes.
set -euo pipefail
export PATH='/usr/sbin:/usr/bin:/sbin:/bin'
umask 077

ROLE="${1:-}"
MODE="${2:-files}"

[[ "$(id -u)" == 0 ]] || { echo 'ACL preflight requires root.' >&2; exit 3; }
case "$ROLE" in
  ai) READER='botkalshi-ai' ;;
  telegram) READER='botkalshi-telegram' ;;
  *) echo 'ACL preflight role must be ai or telegram.' >&2; exit 2 ;;
esac
[[ "$MODE" == files || "$MODE" == bootstrap ]] \
  || { echo 'ACL preflight mode must be files or bootstrap.' >&2; exit 2; }
[[ $# -le 2 ]] || { echo 'ACL preflight received extra arguments.' >&2; exit 2; }

exec /usr/bin/python3 -I - "$READER" "$MODE" <<'PY'
import os
import pwd
import stat
import subprocess
import sys


DATA = "/var/lib/botkalshi-research"
SETFACL = "/usr/bin/setfacl"
reader = sys.argv[1]
mode = sys.argv[2]
bootstrap = mode == "bootstrap"

try:
    reader_uid = pwd.getpwnam(reader).pw_uid
    collector_uid = pwd.getpwnam("botkalshi").pw_uid
except KeyError:
    raise SystemExit("Integration identity is missing.") from None
if reader_uid == 0 or collector_uid == 0:
    raise SystemExit("Integration and collector identities must be unprivileged.")


def grant(fd: int, permissions: str) -> None:
    try:
        subprocess.run(
            [
                SETFACL,
                "-m",
                f"u:{reader_uid}:{permissions}",
                "--",
                f"/proc/self/fd/{fd}",
            ],
            check=True,
            close_fds=True,
            pass_fds=(fd,),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"},
        )
    except (OSError, subprocess.CalledProcessError):
        raise SystemExit("Could not grant bounded integration access.") from None


def open_directory(path: str, *, dir_fd: int | None = None) -> int | None:
    try:
        fd = os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=dir_fd,
        )
    except FileNotFoundError:
        return None
    except OSError:
        raise SystemExit("Research input directory is invalid.") from None
    info = os.fstat(fd)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != collector_uid
        or not 2 <= info.st_nlink <= 1024
    ):
        os.close(fd)
        raise SystemExit("Research input directory is invalid.")
    return fd


def grant_file(directory_fd: int, name: str) -> None:
    try:
        fd = os.open(
            name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory_fd,
        )
    except FileNotFoundError:
        return
    except OSError:
        raise SystemExit("Research artifact path is invalid.") from None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != collector_uid
        ):
            raise SystemExit("Research artifact inode is invalid.")
        grant(fd, "r--")
    finally:
        os.close(fd)


data_fd = open_directory(DATA)
if data_fd is None:
    raise SystemExit(0)
try:
    if bootstrap:
        grant(data_fd, "--x")
    packets_fd = open_directory("packets", dir_fd=data_fd)
    try:
        if packets_fd is not None and bootstrap:
            grant(packets_fd, "--x")
        for filename in ("health.json", "coverage.json", "risk-status.json"):
            grant_file(data_fd, filename)
        if packets_fd is not None:
            grant_file(packets_fd, "latest.json")
    finally:
        if packets_fd is not None:
            os.close(packets_fd)
finally:
    os.close(data_fd)
PY
