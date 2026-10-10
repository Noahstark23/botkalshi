#!/bin/bash
# Stages and installs only the shadow AI/Telegram integrations. The collector is
# never restarted, stopped, disabled, reconfigured, or moved to another release.
set -euo pipefail
set +x
unset PYTHONOPTIMIZE PYTHONPATH PYTHONHOME PYTHONINSPECT
export PATH='/usr/sbin:/usr/bin:/sbin:/bin'
umask 077
export LC_ALL=C

REPO_URL='https://github.com/Noahstark23/botkalshi.git'
BASE='/opt/botkalshi-research'
DATA='/var/lib/botkalshi-research'
STATE='/var/lib/botkalshi-integrations'
ASSISTANT_STATE="$STATE/assistant"
TELEGRAM_STATE="$STATE/telegram"
AI_ENV='/etc/botkalshi-assistant.env'
AI_CREDENTIAL_DIR='/etc/botkalshi-assistant'
AI_KEY_FILE="$AI_CREDENTIAL_DIR/openai_api_key"
TELEGRAM_CREDENTIAL_DIR='/etc/botkalshi-telegram'
TELEGRAM_TOKEN_FILE="$TELEGRAM_CREDENTIAL_DIR/telegram_bot_token"
TELEGRAM_CHAT_FILE="$TELEGRAM_CREDENTIAL_DIR/telegram_chat_id"
PRIVILEGED_HELPER='/usr/libexec/botkalshi-prepare-integration-access'
SHA="${1:-}"
HOST_CONFIRMATION="${2:-}"
SIMULATION_FLAG="${3:-}"
TMP_DIR=''
MUTATION_STARTED=0
INSTALL_COMPLETE=0

die() {
  echo "$1" >&2
  exit "${2:-2}"
}

verify_collector_identity() {
  local pid=$1
  local expected_uid=$2
  local expected_gid=$3
  [[ "$(systemctl show botkalshi-research.service -p User --value)" == 'botkalshi' ]] \
    || die 'El collector no declara User=botkalshi.' 3
  [[ "$(systemctl show botkalshi-research.service -p Group --value)" == 'botkalshi' ]] \
    || die 'El collector no declara Group=botkalshi.' 3
  /usr/bin/python3 -I - "$pid" "$expected_uid" "$expected_gid" <<'PY'
from pathlib import Path
import sys

pid, expected_uid, expected_gid = map(int, sys.argv[1:])
try:
    fields = {}
    for line in Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines():
        name, separator, value = line.partition(":")
        if separator and name in {"Uid", "Gid"}:
            fields[name] = [int(part) for part in value.split()]
except (OSError, UnicodeError, ValueError):
    raise SystemExit("No se pudo verificar la identidad efectiva del collector.") from None
if len(fields.get("Uid", [])) != 4 or any(value != expected_uid for value in fields["Uid"]):
    raise SystemExit("El proceso collector no ejecuta con el UID botkalshi.")
if len(fields.get("Gid", [])) != 4 or any(value != expected_gid for value in fields["Gid"]):
    raise SystemExit("El proceso collector no ejecuta con el GID botkalshi.")
PY
}

cleanup() {
  local exit_code=$?
  set +e
  if [[ -n "$TMP_DIR" && -d "$TMP_DIR" ]]; then
    rm -f -- \
      "$TMP_DIR/botkalshi-assistant.service" \
      "$TMP_DIR/botkalshi-assistant.timer" \
      "$TMP_DIR/botkalshi-telegram.service" \
      "$TMP_DIR/botkalshi-telegram.timer" \
      "$TMP_DIR/botkalshi-telegram-recovery.service" \
      "$TMP_DIR/botkalshi-telegram-ai-failure.service" \
      "$TMP_DIR/verify/botkalshi-assistant.service" \
      "$TMP_DIR/verify/botkalshi-assistant.timer" \
      "$TMP_DIR/verify/botkalshi-telegram.service" \
      "$TMP_DIR/verify/botkalshi-telegram.timer" \
      "$TMP_DIR/verify/botkalshi-telegram-recovery.service" \
      "$TMP_DIR/verify/botkalshi-telegram-ai-failure.service" \
      "$TMP_DIR/status.json" \
      "$TMP_DIR/assessment-before.json" \
      "$TMP_DIR/assessment.json" \
      "$TMP_DIR/assistant.env" \
      "$TMP_DIR/openai.key" \
      "$TMP_DIR/telegram.token" \
      "$TMP_DIR/telegram.chat"
    rmdir -- "$TMP_DIR/verify" 2>/dev/null || true
    rmdir -- "$TMP_DIR" 2>/dev/null || true
  fi
  if (( MUTATION_STARTED == 1 && INSTALL_COMPLETE == 0 )); then
    systemctl disable --now botkalshi-assistant.timer botkalshi-telegram.timer \
      >/dev/null 2>&1 || true
    echo 'Integraciones detenidas por instalación incompleta; collector intacto.' >&2
  fi
  exit "$exit_code"
}
trap cleanup EXIT

if [[ ! "$SHA" =~ ^[0-9a-f]{40}$ \
  || "$HOST_CONFIRMATION" != '--dedicated-research-host' \
  || "$SIMULATION_FLAG" != '--simulation-only' \
  || $# -ne 3 ]]; then
  die 'Uso: bash install-assistant-integrations.sh SHA --dedicated-research-host --simulation-only'
fi
[[ "$(id -u)" == 0 ]] || die 'Requiere consola root del Droplet dedicado.'
[[ -x /usr/bin/flock ]] || die 'Falta /usr/bin/flock para serializar la instalación.' 3
[[ -d /run && ! -L /run ]] || die 'Directorio /run inválido para lock de instalación.' 3
INSTALL_LOCK='/run/botkalshi-integrations-install.lock'
[[ ! -L "$INSTALL_LOCK" ]] || die 'Lock de instalación simbólico rechazado.' 3
exec 9>>"$INSTALL_LOCK"
/usr/bin/flock --exclusive --nonblock 9 \
  || die 'Ya existe otra instalación de integraciones en curso.' 4
[[ -f "/proc/$$/fd/9" \
  && "$(stat -Lc '%u:%g:%a:%h' "/proc/$$/fd/9")" == '0:0:600:1' ]] \
  || die 'Lock de instalación tiene metadatos inseguros.' 3

/usr/bin/python3 -I - <<'PY'
from pathlib import Path

parts = dict(
    line.split("=", 1)
    for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines()
    if "=" in line
)
if parts.get("ID", "").strip('"') != "ubuntu":
    raise SystemExit("Solo Ubuntu probado como destino")
if parts.get("VERSION_ID", "").strip('"') != "24.04":
    raise SystemExit("Destino requerido: Ubuntu 24.04")
PY

systemctl is-active --quiet botkalshi-research.service \
  || die 'El collector research debe estar activo antes de instalar integraciones.' 3
COLLECTOR_PID_BEFORE="$(systemctl show botkalshi-research.service -p MainPID --value)"
COLLECTOR_EXEC_BEFORE="$(systemctl show botkalshi-research.service -p ExecStart --value)"
[[ "$COLLECTOR_PID_BEFORE" =~ ^[1-9][0-9]*$ ]] \
  || die 'No se pudo fijar el PID activo del collector.' 3
[[ -n "$COLLECTOR_EXEC_BEFORE" ]] || die 'No se pudo fijar ExecStart del collector.' 3
COLLECTOR_UID_EXPECTED="$(id -u botkalshi)"
COLLECTOR_GID_EXPECTED="$(id -g botkalshi)"
[[ "$COLLECTOR_UID_EXPECTED" != 0 \
  && "$COLLECTOR_GID_EXPECTED" != 0 \
  && "$(id -gn botkalshi)" == 'botkalshi' ]] \
  || die 'La identidad primaria del collector no es segura.' 3
verify_collector_identity \
  "$COLLECTOR_PID_BEFORE" "$COLLECTOR_UID_EXPECTED" "$COLLECTOR_GID_EXPECTED"

missing_packages=()
[[ -x /usr/bin/git ]] || missing_packages+=(git)
[[ -x /usr/bin/python3 ]] || missing_packages+=(python3)
[[ -x /usr/bin/setfacl ]] || missing_packages+=(acl)
[[ -r /etc/ssl/certs/ca-certificates.crt ]] || missing_packages+=(ca-certificates)
if (( ${#missing_packages[@]} > 0 )); then
  export DEBIAN_FRONTEND=noninteractive
  export NEEDRESTART_MODE=l
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends --no-upgrade \
    "${missing_packages[@]}" >/dev/null
fi
[[ -x /usr/bin/git && -x /usr/bin/python3 && -x /usr/bin/setfacl \
  && -r /etc/ssl/certs/ca-certificates.crt ]] \
  || die 'Faltan prerrequisitos locales después de la instalación.' 3
systemctl is-active --quiet botkalshi-research.service \
  || die 'El collector dejó de estar activo durante prerrequisitos.' 3
[[ "$(systemctl show botkalshi-research.service -p MainPID --value)" == "$COLLECTOR_PID_BEFORE" ]] \
  || die 'El PID del collector cambió durante prerrequisitos.' 3
[[ "$(systemctl show botkalshi-research.service -p ExecStart --value)" == "$COLLECTOR_EXEC_BEFORE" ]] \
  || die 'ExecStart del collector cambió durante prerrequisitos.' 3
verify_collector_identity \
  "$COLLECTOR_PID_BEFORE" "$COLLECTOR_UID_EXPECTED" "$COLLECTOR_GID_EXPECTED"

/usr/bin/python3 -I - <<'PY'
from pathlib import Path
import stat

for path in (
    Path("/opt"),
    Path("/opt/botkalshi-research"),
    Path("/opt/botkalshi-research/releases"),
    Path("/usr"),
    Path("/usr/libexec"),
):
    if not path.exists():
        continue
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise SystemExit("Ancestro de releases inválido.")
    if info.st_uid != 0 or info.st_gid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
        raise SystemExit("Ancestro de releases tiene propietario o permisos inseguros.")
PY
install -d -o root -g root -m 0755 "$BASE" "$BASE/releases"
RELEASE="$BASE/releases/$SHA"
for path in "$BASE" "$BASE/releases" "$RELEASE" "$DATA" "$STATE" "$ASSISTANT_STATE" \
  "$TELEGRAM_STATE" "$AI_ENV" "$AI_CREDENTIAL_DIR" "$AI_KEY_FILE" \
  "$TELEGRAM_CREDENTIAL_DIR" "$TELEGRAM_TOKEN_FILE" "$TELEGRAM_CHAT_FILE" \
  "$PRIVILEGED_HELPER"; do
  [[ ! -L "$path" ]] || die 'Ruta simbólica rechazada durante instalación.'
done

assert_root_owned_tree() {
  local candidate=$1
  local unsafe symlink hardlink special
  unsafe="$(find "$candidate" -xdev \
    \( \( -type f -o -type d \) \
       \( ! -user root -o ! -group root -o -perm /022 \) \) \
    -print -quit)"
  [[ -z "$unsafe" ]] || die 'Release preexistente tiene propietario o permisos inseguros.'
  symlink="$(find "$candidate" -xdev -type l -print -quit)"
  [[ -z "$symlink" ]] || die 'Release con enlaces simbólicos rechazada.'
  hardlink="$(find "$candidate" -xdev -type f -links +1 -print -quit)"
  [[ -z "$hardlink" ]] || die 'Release con archivos enlazados múltiples rechazada.'
  special="$(find "$candidate" -xdev ! -type f ! -type d ! -type l -print -quit)"
  [[ -z "$special" ]] || die 'Release contiene un tipo de archivo no permitido.'
}

safe_release_git() {
  git \
    -c core.fsmonitor=false \
    -c core.untrackedCache=false \
    -c core.hooksPath=/dev/null \
    -C "$RELEASE" "$@"
}

if [[ -e "$RELEASE" ]]; then
  [[ -d "$RELEASE/.git" ]] || die 'Destino de release existente no es un checkout válido.'
  assert_root_owned_tree "$RELEASE"
  [[ "$(safe_release_git rev-parse HEAD)" == "$SHA" ]] || die 'SHA de release existente no coincide.'
  [[ "$(safe_release_git remote get-url origin)" == "$REPO_URL" ]] || die 'Origen de release inesperado.'
  [[ -z "$(safe_release_git status --porcelain=v1 --untracked-files=all --ignored --ignore-submodules=all)" ]] \
    || die 'Release modificada, ignorada o no rastreada; no se ejecuta.'
else
  STAGING="$(mktemp -d "$BASE/releases/.integration-staging.XXXXXX")"
  # Una descarga fallida se conserva para inspección; no se borra evidencia.
  git -C "$STAGING" init -q
  git -C "$STAGING" remote add origin "$REPO_URL"
  git -C "$STAGING" fetch --depth 1 origin "$SHA"
  git -C "$STAGING" checkout --detach FETCH_HEAD
  [[ "$(git -C "$STAGING" rev-parse HEAD)" == "$SHA" ]] || die 'La descarga no coincide con el SHA solicitado.'
  [[ "$(git -C "$STAGING" remote get-url origin)" == "$REPO_URL" ]] || die 'Origen descargado inesperado.'
  find "$STAGING" -type d -exec chmod 0755 {} +
  find "$STAGING" -type f -exec chmod a+rX,u+w,go-w {} +
  mv "$STAGING" "$RELEASE"
fi

assert_root_owned_tree "$RELEASE"

# A later systemd ExecStartPre is copied from this release into /usr/libexec;
# normalize the immutable checkout first so no service identity can alter code
# that root will execute.
find "$RELEASE" -type d -exec chown root:root {} +
find "$RELEASE" -type f -exec chown root:root {} +
find "$RELEASE" -type d -exec chmod 0755 {} +
find "$RELEASE" -type f -exec chmod a+rX,u+w,go-w {} +

for relative in \
  assistant_bridge.py \
  telegram_notifier.py \
  prepare-integration-access.sh \
  botkalshi-assistant.service \
  botkalshi-assistant.timer \
  botkalshi-telegram.service \
  botkalshi-telegram.timer \
  botkalshi-telegram-recovery.service \
  botkalshi-telegram-ai-failure.service; do
  [[ -f "$RELEASE/infra/digitalocean-shadow/$relative" ]] \
    || die "Release incompleta: falta $relative."
done
[[ ! -L "$RELEASE/infra/digitalocean-shadow/prepare-integration-access.sh" ]]
[[ "$(stat -c '%U:%G:%a' "$RELEASE/infra/digitalocean-shadow/prepare-integration-access.sh")" == 'root:root:755' ]] \
  || die 'Helper privilegiado no tiene propietario/modo seguros.'

PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -I -m unittest discover \
  -s "$RELEASE/infra/digitalocean-shadow/tests" -p 'test_*.py' -v

ensure_service_user() {
  local user=$1
  if id "$user" >/dev/null 2>&1; then
    local entry
    entry="$(getent passwd "$user")"
    [[ "$entry" == *:/nonexistent:/usr/sbin/nologin ]] \
      || die "Usuario preexistente $user no cumple el perfil aislado."
    [[ "$(id -gn "$user")" == "$user" ]] \
      || die "Usuario preexistente $user no tiene grupo primario aislado."
  else
    useradd --system --user-group --home-dir /nonexistent --shell /usr/sbin/nologin "$user"
  fi
}
ensure_service_user botkalshi-ai
ensure_service_user botkalshi-telegram
usermod --append --groups botkalshi-ai botkalshi-telegram
COLLECTOR_UID="$COLLECTOR_UID_EXPECTED"
AI_UID="$(id -u botkalshi-ai)"
AI_GID="$(id -g botkalshi-ai)"
TELEGRAM_UID="$(id -u botkalshi-telegram)"
TELEGRAM_GID="$(id -g botkalshi-telegram)"
[[ "$COLLECTOR_UID" != 0 && "$AI_UID" != 0 && "$TELEGRAM_UID" != 0 \
  && "$COLLECTOR_GID_EXPECTED" != 0 && "$AI_GID" != 0 && "$TELEGRAM_GID" != 0 ]] \
  || die 'Las identidades y grupos de servicio nunca pueden ser root.'
SYSTEM_UID_LIMIT="$(awk '$1 == "UID_MIN" {print $2; exit}' /etc/login.defs)"
SYSTEM_GID_LIMIT="$(awk '$1 == "GID_MIN" {print $2; exit}' /etc/login.defs)"
[[ "$SYSTEM_UID_LIMIT" =~ ^[1-9][0-9]*$ \
  && "$SYSTEM_GID_LIMIT" =~ ^[1-9][0-9]*$ \
  && "$AI_UID" -lt "$SYSTEM_UID_LIMIT" \
  && "$TELEGRAM_UID" -lt "$SYSTEM_UID_LIMIT" \
  && "$AI_GID" -lt "$SYSTEM_GID_LIMIT" \
  && "$TELEGRAM_GID" -lt "$SYSTEM_GID_LIMIT" ]] \
  || die 'IA y Telegram deben usar UIDs/GIDs de sistema.'
[[ "$COLLECTOR_UID" != "$AI_UID" \
  && "$COLLECTOR_UID" != "$TELEGRAM_UID" \
  && "$AI_UID" != "$TELEGRAM_UID" ]] \
  || die 'Collector, IA y Telegram requieren UIDs distintos.'
[[ "$COLLECTOR_GID_EXPECTED" != "$AI_GID" \
  && "$COLLECTOR_GID_EXPECTED" != "$TELEGRAM_GID" \
  && "$AI_GID" != "$TELEGRAM_GID" ]] \
  || die 'Collector, IA y Telegram requieren GIDs primarios distintos.'
/usr/bin/python3 -I - \
  "botkalshi:$COLLECTOR_UID" "botkalshi-ai:$AI_UID" \
  "botkalshi-telegram:$TELEGRAM_UID" <<'PY'
import pwd
import sys

entries = pwd.getpwall()
for expected in sys.argv[1:]:
    name, raw_uid = expected.rsplit(":", 1)
    matches = [entry.pw_name for entry in entries if entry.pw_uid == int(raw_uid)]
    if matches != [name]:
        raise SystemExit("Un UID de servicio está compartido con otra identidad.")
PY
/usr/bin/python3 -I - \
  "botkalshi:$COLLECTOR_GID_EXPECTED" "botkalshi-ai:$AI_GID" \
  "botkalshi-telegram:$TELEGRAM_GID" <<'PY'
import grp
import sys

entries = grp.getgrall()
for expected in sys.argv[1:]:
    name, raw_gid = expected.rsplit(":", 1)
    matches = [entry.gr_name for entry in entries if entry.gr_gid == int(raw_gid)]
    if matches != [name]:
        raise SystemExit("Un GID de servicio está compartido con otro grupo.")
PY
[[ "$(id -Gn botkalshi-ai)" == 'botkalshi-ai' ]] \
  || die 'La identidad IA tiene grupos suplementarios inesperados.'
TELEGRAM_GROUPS="$(id -Gn botkalshi-telegram | tr ' ' '\n' | sort | tr '\n' ' ')"
[[ "$TELEGRAM_GROUPS" == 'botkalshi-ai botkalshi-telegram ' ]] \
  || die 'La identidad Telegram tiene grupos suplementarios inesperados.'
/usr/bin/python3 -I - "$AI_GID" <<'PY'
import os
import pwd
import sys

shared_gid = int(sys.argv[1])
allowed = {"botkalshi-ai", "botkalshi-telegram"}
readers = set()
for entry in pwd.getpwall():
    try:
        groups = os.getgrouplist(entry.pw_name, entry.pw_gid)
    except OSError:
        raise SystemExit("No se pudo enumerar membresía del grupo IA.") from None
    if shared_gid in groups:
        readers.add(entry.pw_name)
if readers != allowed:
    raise SystemExit("El grupo compartido IA contiene identidades no autorizadas.")
PY

# Quiesce only the integration jobs before touching their user-owned state.
# The research collector stays active and its PID/ExecStart are checked below.
MUTATION_STARTED=1
systemctl stop botkalshi-assistant.timer botkalshi-telegram.timer \
  botkalshi-assistant.service botkalshi-telegram.service \
  botkalshi-telegram-recovery.service botkalshi-telegram-ai-failure.service \
  >/dev/null 2>&1 || true
for integration_unit in \
  botkalshi-assistant.service \
  botkalshi-telegram.service \
  botkalshi-telegram-recovery.service \
  botkalshi-telegram-ai-failure.service; do
  integration_state="$(systemctl show "$integration_unit" -p ActiveState --value 2>/dev/null || true)"
  [[ "$integration_state" != active \
    && "$integration_state" != activating \
    && "$integration_state" != deactivating \
    && "$integration_state" != reloading ]] \
    || die "No se pudo detener $integration_unit antes de migrar estado." 3
done

install -d -o root -g root -m 0711 "$STATE"
install -d -o botkalshi-ai -g botkalshi-ai -m 0750 "$ASSISTANT_STATE"
install -d -o botkalshi-telegram -g botkalshi-telegram -m 0700 "$TELEGRAM_STATE"
/usr/bin/python3 -I - \
  "$ASSISTANT_STATE" "$AI_UID" "$AI_GID" \
  "$TELEGRAM_STATE" "$TELEGRAM_UID" "$TELEGRAM_GID" <<'PY'
import os
import stat
import sys


def migrate(directory: str, uid: int, gid: int, files: dict[str, int]) -> None:
    directory_fd = os.open(
        directory,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        directory_info = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_info.st_mode):
            raise SystemExit("Directorio de estado inválido.")
        for name, mode in files.items():
            try:
                file_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                continue
            except OSError:
                raise SystemExit("Entrada de estado existente no es segura.") from None
            try:
                info = os.fstat(file_fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise SystemExit("Entrada de estado existente no es un archivo regular único.")
                os.fchown(file_fd, uid, gid)
                os.fchmod(file_fd, mode)
            finally:
                os.close(file_fd)
    finally:
        os.close(directory_fd)


migrate(
    sys.argv[1],
    int(sys.argv[2]),
    int(sys.argv[3]),
    {
        "control-state.json": 0o640,
        "assessment-latest.json": 0o640,
        "control-state.lock": 0o600,
        "assessment.lock": 0o600,
    },
)
migrate(
    sys.argv[4],
    int(sys.argv[5]),
    int(sys.argv[6]),
    {"telegram-state.json": 0o600, "notify.lock": 0o600},
)
PY
install -d -o root -g root -m 0700 "$AI_CREDENTIAL_DIR" "$TELEGRAM_CREDENTIAL_DIR"

# Refresh access only on the four allow-listed artifacts. The helper is also an
# ExecStartPre because the collector replaces those files atomically each cycle.
"$RELEASE/infra/digitalocean-shadow/prepare-integration-access.sh" ai bootstrap
"$RELEASE/infra/digitalocean-shadow/prepare-integration-access.sh" telegram bootstrap

validate_model_environment() {
  /usr/bin/python3 -I - "$1" <<'PY'
from pathlib import Path
import re
import stat
import sys

path = Path(sys.argv[1])
try:
    info = path.lstat()
    raw = path.read_text(encoding="utf-8")
except (OSError, UnicodeError):
    raise SystemExit("Configuración de modelo ausente o ilegible.")
if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
    raise SystemExit("Configuración de modelo debe ser un archivo regular no enlazado.")
if info.st_uid != 0 or info.st_gid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
    raise SystemExit("Configuración de modelo debe ser root:root modo 0600.")
lines = raw.splitlines()
if len(lines) != 1:
    raise SystemExit("Configuración de modelo debe contener una sola variable.")
key, separator, value = lines[0].partition("=")
if key != "BOTKALSHI_OPENAI_MODEL" or not separator:
    raise SystemExit("Configuración de modelo contiene un nombre no permitido.")
if not re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", value):
    raise SystemExit("Modelo OpenAI tiene formato inválido.")
PY
}

validate_credential_file() {
  /usr/bin/python3 -I - "$1" "$2" <<'PY'
from pathlib import Path
import re
import stat
import sys

path = Path(sys.argv[1])
kind = sys.argv[2]
try:
    info = path.lstat()
    raw = path.read_text(encoding="utf-8")
except (OSError, UnicodeError):
    raise SystemExit("Credencial ausente o ilegible; valor no mostrado.")
if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
    raise SystemExit("Credencial debe ser un archivo regular no enlazado.")
if info.st_uid != 0 or info.st_gid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
    raise SystemExit("Credencial debe ser root:root modo 0600.")
if not raw.endswith("\n") or "\n" in raw[:-1] or len(raw.encode("utf-8")) > 4096:
    raise SystemExit("Credencial tiene formato inválido; valor no mostrado.")
value = raw[:-1]
if kind == "openai":
    valid = re.fullmatch(r"[A-Za-z0-9._-]{20,512}", value) is not None
elif kind == "telegram-token":
    valid = re.fullmatch(r"[0-9]{5,16}:[A-Za-z0-9_-]{20,128}", value) is not None
elif kind == "telegram-chat":
    valid = re.fullmatch(r"-?[0-9]{1,20}", value) is not None
    if valid:
        parsed = int(value)
        valid = parsed != 0 and -(2**63) <= parsed <= 2**63 - 1 and str(parsed) == value
else:
    raise SystemExit("Tipo de credencial inválido.")
if not valid:
    raise SystemExit("Credencial tiene formato inválido; valor no mostrado.")
PY
}

create_assistant_environment() {
  if [[ -e "$AI_ENV" || -e "$AI_KEY_FILE" ]]; then
    [[ -e "$AI_ENV" && -e "$AI_KEY_FILE" ]] \
      || die 'Configuración IA parcial; no se sobreescribe automáticamente.'
    validate_model_environment "$AI_ENV"
    validate_credential_file "$AI_KEY_FILE" openai
    return
  fi
  [[ -t 0 ]] || die 'Faltan credenciales IA; ejecuta desde una consola TTY segura.'
  local api_key model
  IFS= read -r -s -p 'OPENAI_API_KEY (oculta): ' api_key || die 'No se recibió la clave OpenAI.'
  printf '\n'
  IFS= read -r -p 'BOTKALSHI_OPENAI_MODEL [gpt-5.6-terra]: ' model \
    || die 'No se recibió el modelo OpenAI.'
  model="${model:-gpt-5.6-terra}"
  [[ "$api_key" =~ ^[A-Za-z0-9._-]{20,512}$ ]] \
    || die 'Formato de clave OpenAI rechazado; valor no mostrado.'
  [[ "$model" =~ ^[A-Za-z0-9._:-]{1,80}$ ]] || die 'Formato de modelo OpenAI rechazado.'
  printf 'BOTKALSHI_OPENAI_MODEL=%s\n' "$model" > "$TMP_DIR/assistant.env"
  printf '%s\n' "$api_key" > "$TMP_DIR/openai.key"
  validate_model_environment "$TMP_DIR/assistant.env"
  validate_credential_file "$TMP_DIR/openai.key" openai
  install -o root -g root -m 0600 "$TMP_DIR/assistant.env" "$AI_ENV"
  install -o root -g root -m 0600 "$TMP_DIR/openai.key" "$AI_KEY_FILE"
  unset api_key model
  validate_model_environment "$AI_ENV"
  validate_credential_file "$AI_KEY_FILE" openai
}

create_telegram_environment() {
  if [[ -e "$TELEGRAM_TOKEN_FILE" || -e "$TELEGRAM_CHAT_FILE" ]]; then
    [[ -e "$TELEGRAM_TOKEN_FILE" && -e "$TELEGRAM_CHAT_FILE" ]] \
      || die 'Credenciales Telegram parciales; no se sobreescriben automáticamente.'
    validate_credential_file "$TELEGRAM_TOKEN_FILE" telegram-token
    validate_credential_file "$TELEGRAM_CHAT_FILE" telegram-chat
    return
  fi
  [[ -t 0 ]] || die 'Faltan credenciales Telegram; ejecuta desde una consola TTY segura.'
  local bot_token chat_id
  IFS= read -r -s -p 'TELEGRAM_BOT_TOKEN (oculto): ' bot_token || die 'No se recibió el token Telegram.'
  printf '\n'
  IFS= read -r -p 'TELEGRAM_CHAT_ID numérico: ' chat_id || die 'No se recibió el chat ID Telegram.'
  [[ "$bot_token" =~ ^[0-9]{5,16}:[A-Za-z0-9_-]{20,128}$ ]] \
    || die 'Formato de token Telegram rechazado; valor no mostrado.'
  [[ "$chat_id" =~ ^-?[0-9]{1,20}$ ]] || die 'El chat ID Telegram debe ser numérico.'
  printf '%s\n' "$bot_token" > "$TMP_DIR/telegram.token"
  printf '%s\n' "$chat_id" > "$TMP_DIR/telegram.chat"
  validate_credential_file "$TMP_DIR/telegram.token" telegram-token
  validate_credential_file "$TMP_DIR/telegram.chat" telegram-chat
  install -o root -g root -m 0600 "$TMP_DIR/telegram.token" "$TELEGRAM_TOKEN_FILE"
  install -o root -g root -m 0600 "$TMP_DIR/telegram.chat" "$TELEGRAM_CHAT_FILE"
  unset bot_token chat_id
  validate_credential_file "$TELEGRAM_TOKEN_FILE" telegram-token
  validate_credential_file "$TELEGRAM_CHAT_FILE" telegram-chat
}

TMP_DIR="$(mktemp -d /tmp/botkalshi-integrations.XXXXXX)"
install -d -o root -g root -m 0700 "$TMP_DIR/verify"
for unit in \
  botkalshi-assistant.service \
  botkalshi-assistant.timer \
  botkalshi-telegram.service \
  botkalshi-telegram.timer \
  botkalshi-telegram-recovery.service \
  botkalshi-telegram-ai-failure.service; do
  sed "s|/opt/botkalshi/|$RELEASE/|g" \
    "$RELEASE/infra/digitalocean-shadow/$unit" > "$TMP_DIR/$unit"
  sed "s|/usr/libexec/botkalshi-prepare-integration-access|$RELEASE/infra/digitalocean-shadow/prepare-integration-access.sh|g" \
    "$TMP_DIR/$unit" > "$TMP_DIR/verify/$unit"
done
systemd-analyze verify \
  "$TMP_DIR/verify/botkalshi-assistant.service" \
  "$TMP_DIR/verify/botkalshi-assistant.timer" \
  "$TMP_DIR/verify/botkalshi-telegram.service" \
  "$TMP_DIR/verify/botkalshi-telegram.timer" \
  "$TMP_DIR/verify/botkalshi-telegram-recovery.service" \
  "$TMP_DIR/verify/botkalshi-telegram-ai-failure.service"

runuser -u botkalshi-ai -- /usr/bin/python3 \
  "$RELEASE/infra/digitalocean-shadow/assistant_bridge.py" \
  --data "$DATA" --state-data "$STATE" status > "$TMP_DIR/status.json"
CONTROL_ACTION="$(/usr/bin/python3 -I - "$TMP_DIR/status.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    status = json.load(source)
if status.get("cycle", {}).get("technical_status") != "VERIFIED":
    raise SystemExit("El ciclo research no está verificado.")
capabilities = status.get("capabilities", {})
if capabilities.get("place_order") is not False:
    raise SystemExit("La capacidad place_order debe permanecer bloqueada.")
if capabilities.get("cancel_order") is not False:
    raise SystemExit("La capacidad cancel_order debe permanecer bloqueada.")
if capabilities.get("move_money") is not False:
    raise SystemExit("La capacidad move_money debe permanecer bloqueada.")
control = status["control"]
if control["paused"] is False:
    print("READY")
elif control["paused"] is True and control.get("updated_by") == "fail-closed-default":
    print("INITIALIZE")
else:
    raise SystemExit("Pausa IA explícita detectada; no se reemplaza durante instalación.")
PY
)"
if [[ "$CONTROL_ACTION" == 'INITIALIZE' ]]; then
  runuser -u botkalshi-ai -- /usr/bin/python3 \
    "$RELEASE/infra/digitalocean-shadow/assistant_bridge.py" \
    --data "$DATA" --state-data "$STATE" \
    resume-simulation --confirm SIMULATION_ONLY >/dev/null
elif [[ "$CONTROL_ACTION" != 'READY' ]]; then
  die 'Estado de control IA inesperado.' 3
fi
runuser -u botkalshi-ai -- /usr/bin/python3 \
  "$RELEASE/infra/digitalocean-shadow/assistant_bridge.py" \
  --data "$DATA" --state-data "$STATE" status > "$TMP_DIR/status.json"
/usr/bin/python3 -I - "$TMP_DIR/status.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    status = json.load(source)
if status.get("cycle", {}).get("technical_status") != "VERIFIED":
    raise SystemExit("El ciclo research no está verificado.")
if status.get("control", {}).get("paused") is not False:
    raise SystemExit("La simulación IA no quedó activa.")
capabilities = status.get("capabilities", {})
if capabilities.get("place_order") is not False:
    raise SystemExit("La capacidad place_order debe permanecer bloqueada.")
if capabilities.get("cancel_order") is not False:
    raise SystemExit("La capacidad cancel_order debe permanecer bloqueada.")
if capabilities.get("move_money") is not False:
    raise SystemExit("La capacidad move_money debe permanecer bloqueada.")
PY

create_assistant_environment
create_telegram_environment

# From here on a failure still disables only these two integration timers.
install -d -o root -g root -m 0755 /usr/libexec
install -o root -g root -m 0755 \
  "$RELEASE/infra/digitalocean-shadow/prepare-integration-access.sh" \
  "$PRIVILEGED_HELPER"
[[ "$(stat -c '%F:%U:%G:%a:%h' "$PRIVILEGED_HELPER")" == 'regular file:root:root:755:1' ]] \
  || die 'Helper privilegiado instalado con metadatos inseguros.'
install -o root -g root -m 0644 "$TMP_DIR/botkalshi-assistant.service" \
  /etc/systemd/system/botkalshi-assistant.service
install -o root -g root -m 0644 "$TMP_DIR/botkalshi-assistant.timer" \
  /etc/systemd/system/botkalshi-assistant.timer
install -o root -g root -m 0644 "$TMP_DIR/botkalshi-telegram.service" \
  /etc/systemd/system/botkalshi-telegram.service
install -o root -g root -m 0644 "$TMP_DIR/botkalshi-telegram.timer" \
  /etc/systemd/system/botkalshi-telegram.timer
install -o root -g root -m 0644 "$TMP_DIR/botkalshi-telegram-recovery.service" \
  /etc/systemd/system/botkalshi-telegram-recovery.service
install -o root -g root -m 0644 "$TMP_DIR/botkalshi-telegram-ai-failure.service" \
  /etc/systemd/system/botkalshi-telegram-ai-failure.service
systemctl daemon-reload

read_telegram_delivery() {
  /usr/bin/python3 -I - "$TELEGRAM_STATE/telegram-state.json" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
if not path.exists():
    print("0 NONE")
    raise SystemExit(0)
if path.is_symlink() or not path.is_file():
    raise SystemExit("Estado Telegram no es un archivo regular.")
with path.open(encoding="utf-8") as source:
    state = json.load(source)
if state.get("schema_version") != "botkalshi-telegram-state-v2":
    raise SystemExit("Estado Telegram tiene schema inválido.")
message_id = state.get("last_message_id")
event = state.get("last_event")
if type(message_id) is not int or message_id < 1 or not isinstance(event, str):
    raise SystemExit("Estado Telegram no contiene entrega confirmada.")
if state.get("execution_authorized") is not False:
    raise SystemExit("Estado Telegram intentó escalar autoridad.")
if state.get("order_capability_present") is not False:
    raise SystemExit("Estado Telegram intentó declarar órdenes.")
if state.get("last_ai_health") not in {"MISSING", "STALE", "OFFLINE", "ACTIVE"}:
    raise SystemExit("Estado Telegram contiene salud IA inválida.")
if type(state.get("last_control_paused")) is not bool:
    raise SystemExit("Estado Telegram contiene control IA inválido.")
print(message_id, event)
PY
}

read -r TELEGRAM_MESSAGE_BEFORE TELEGRAM_EVENT_BEFORE < <(read_telegram_delivery)
ASSESSMENT_ID_BEFORE='NONE'
if runuser -u botkalshi-ai -- /usr/bin/python3 \
  "$RELEASE/infra/digitalocean-shadow/assistant_bridge.py" \
  --data "$DATA" --state-data "$STATE" \
  latest-assessment > "$TMP_DIR/assessment-before.json" 2>/dev/null; then
  ASSESSMENT_ID_BEFORE="$(/usr/bin/python3 -I - "$TMP_DIR/assessment-before.json" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    assessment = json.load(source)
assessment_id = assessment.get("assessment_id")
if not isinstance(assessment_id, str) or not re.fullmatch(
    r"assessment-[0-9]{8}T[0-9]{12}Z", assessment_id
):
    print("INVALID")
else:
    print(assessment_id)
PY
)"
fi
SMOKE_STARTED_AT="$(/usr/bin/python3 -I - <<'PY'
from datetime import UTC, datetime

print(datetime.now(UTC).isoformat())
PY
)"
systemctl start botkalshi-assistant.service
[[ "$(systemctl show botkalshi-assistant.service -p Result --value)" == 'success' ]] \
  || die 'La evaluación inicial OpenAI no terminó correctamente.' 3
runuser -u botkalshi-ai -- /usr/bin/python3 \
  "$RELEASE/infra/digitalocean-shadow/assistant_bridge.py" \
  --data "$DATA" --state-data "$STATE" latest-assessment > "$TMP_DIR/assessment.json"
/usr/bin/python3 -I - \
  "$TMP_DIR/assessment.json" "$ASSESSMENT_ID_BEFORE" "$SMOKE_STARTED_AT" <<'PY'
from datetime import UTC, datetime, timedelta
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    assessment = json.load(source)
assessment_id = assessment.get("assessment_id")
if not isinstance(assessment_id, str) or not re.fullmatch(
    r"assessment-[0-9]{8}T[0-9]{12}Z", assessment_id
):
    raise SystemExit("La evaluación inicial no tiene un identificador válido.")
if assessment_id == sys.argv[2]:
    raise SystemExit("La evaluación inicial reutilizó el estado previo.")
try:
    created_at = datetime.fromisoformat(assessment.get("created_at", ""))
    not_before = datetime.fromisoformat(sys.argv[3])
except (TypeError, ValueError):
    raise SystemExit("La evaluación inicial no tiene una hora válida.") from None
if created_at.tzinfo is None or not_before.tzinfo is None:
    raise SystemExit("La evaluación inicial requiere una hora con zona.")
now = datetime.now(UTC)
if created_at.astimezone(UTC) < not_before.astimezone(UTC) or created_at > now + timedelta(seconds=5):
    raise SystemExit("La evaluación inicial no es fresca.")
if assessment.get("provider") != "openai":
    raise SystemExit("La evaluación inicial no usó el proveedor OpenAI.")
if assessment.get("execution_authorized") is not False:
    raise SystemExit("La evaluación intentó escalar autoridad de ejecución.")
if assessment.get("order_capability_present") is not False:
    raise SystemExit("La evaluación expuso capacidad de órdenes.")
PY

# OnSuccess requests the normal notifier. Starting it explicitly also waits for
# any queued job and is harmless because auto mode deduplicates.
systemctl start botkalshi-telegram.service
[[ "$(systemctl show botkalshi-telegram.service -p Result --value)" == 'success' ]] \
  || die 'La prueba inicial de Telegram no terminó correctamente.' 3
read -r TELEGRAM_MESSAGE_AFTER TELEGRAM_EVENT_AFTER < <(read_telegram_delivery)
if [[ "$TELEGRAM_MESSAGE_AFTER" == "$TELEGRAM_MESSAGE_BEFORE" \
  || "$TELEGRAM_EVENT_AFTER" != 'recovery' ]]; then
  systemctl start botkalshi-telegram-recovery.service
  [[ "$(systemctl show botkalshi-telegram-recovery.service -p Result --value)" == 'success' ]] \
    || die 'Telegram no confirmó el mensaje de recuperación.' 3
  read -r TELEGRAM_MESSAGE_AFTER TELEGRAM_EVENT_AFTER < <(read_telegram_delivery)
fi
[[ "$TELEGRAM_MESSAGE_AFTER" != "$TELEGRAM_MESSAGE_BEFORE" \
  && "$TELEGRAM_MESSAGE_AFTER" =~ ^[1-9][0-9]*$ \
  && "$TELEGRAM_EVENT_AFTER" == 'recovery' ]] \
  || die 'No se verificó una entrega Telegram nueva de recuperación.' 3

COLLECTOR_PID_AFTER="$(systemctl show botkalshi-research.service -p MainPID --value)"
COLLECTOR_EXEC_AFTER="$(systemctl show botkalshi-research.service -p ExecStart --value)"
systemctl is-active --quiet botkalshi-research.service \
  || die 'El collector dejó de estar activo durante la instalación.' 3
[[ "$COLLECTOR_PID_AFTER" == "$COLLECTOR_PID_BEFORE" ]] \
  || die 'El PID del collector cambió; integraciones no habilitadas.' 3
[[ "$COLLECTOR_EXEC_AFTER" == "$COLLECTOR_EXEC_BEFORE" ]] \
  || die 'ExecStart del collector cambió; integraciones no habilitadas.' 3
verify_collector_identity \
  "$COLLECTOR_PID_AFTER" "$COLLECTOR_UID_EXPECTED" "$COLLECTOR_GID_EXPECTED"

systemctl enable botkalshi-assistant.timer botkalshi-telegram.timer >/dev/null
systemctl start botkalshi-assistant.timer botkalshi-telegram.timer
systemctl is-active --quiet botkalshi-assistant.timer
systemctl is-active --quiet botkalshi-telegram.timer
[[ "$(systemctl is-enabled botkalshi-assistant.timer)" == 'enabled' ]]
[[ "$(systemctl is-enabled botkalshi-telegram.timer)" == 'enabled' ]]
[[ "$(systemctl show botkalshi-research.service -p MainPID --value)" == "$COLLECTOR_PID_BEFORE" ]]
[[ "$(systemctl show botkalshi-research.service -p ExecStart --value)" == "$COLLECTOR_EXEC_BEFORE" ]]
verify_collector_identity \
  "$COLLECTOR_PID_BEFORE" "$COLLECTOR_UID_EXPECTED" "$COLLECTOR_GID_EXPECTED"

INSTALL_COMPLETE=1
echo 'INTEGRATIONS_OPERATIONAL=true'
echo "INTEGRATIONS_RELEASE=$SHA"
echo 'AI_PROVIDER=openai'
echo 'AI_INTERVAL=15min'
echo 'TELEGRAM_INTERVAL=5min'
echo 'TELEGRAM_INITIAL_RUN=delivered'
echo "TELEGRAM_MESSAGE_ID=$TELEGRAM_MESSAGE_AFTER"
echo "COLLECTOR_PID_UNCHANGED=$COLLECTOR_PID_BEFORE"
echo 'COLLECTOR_EXECSTART_UNCHANGED=true'
echo 'EXECUTION_AUTHORIZED=false'
echo 'REAL_TRADING=false'
