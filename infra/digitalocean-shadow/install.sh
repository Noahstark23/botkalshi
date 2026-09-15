#!/usr/bin/env bash
# Installs only the research collector; no keys, accounts or trade execution.
# Explicit immutable revision; never removes an existing checkout or data directory.
set -euo pipefail
umask 077
REPO_URL='https://github.com/Noahstark23/botkalshi.git'
SHA="${1:-}"
if [[ ! "$SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo 'Uso: bash install.sh COMMIT_SHA_COMPLETO --dedicated-research-host' >&2; exit 2
fi
if [[ "${2:-}" != '--dedicated-research-host' ]]; then
  echo 'Confirma que es el Droplet dedicado, no Nortex, con --dedicated-research-host.' >&2; exit 2
fi
[[ "$(id -u)" == 0 ]] || { echo 'Requiere consola root del Droplet dedicado.' >&2; exit 2; }
/usr/bin/python3 - <<'PY'
from pathlib import Path
parts = dict(line.split('=',1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
assert parts.get('ID','').strip('"') == 'ubuntu', 'Solo Ubuntu probado como destino'
assert parts.get('VERSION_ID','').strip('"') == '24.04', 'Destino requerido: Ubuntu 24.04'
PY
BASE='/opt/botkalshi-research'
RELEASE="$BASE/releases/$SHA"
DATA='/var/lib/botkalshi-research'
for path in "$BASE" "$BASE/releases" "$RELEASE" "$DATA" /etc/botkalshi-research.env; do
  [[ ! -L "$path" ]] || { echo 'Ruta de instalación simbólica rechazada.' >&2; exit 2; }
done
# Do not import an existing paid-data or financial credential by accident.
if [[ -e /etc/botkalshi-research.env ]]; then
  /usr/bin/python3 - <<'PY'
from pathlib import Path
blocked = {'ODDS_API_KEY','OPENAI_API_KEY','ANTHROPIC_API_KEY','KALSHI_API_KEY_ID',
           'KALSHI_PRIVATE_KEY','KALSHI_PRIVATE_KEY_PATH','PYTHONPATH','PYTHONHOME'}
for line in Path('/etc/botkalshi-research.env').read_text().splitlines():
    line=line.strip()
    if not line or line.startswith('#'): continue
    key, sep, value = line.partition('=')
    key=key.strip()
    if not sep or key in blocked:
        raise SystemExit('Archivo de entorno requiere revisión local; no se muestran valores.')
    if (key == 'TRADING_ENABLED' or key.endswith('EXECUTION_ENABLED')) and value.strip().strip('"\'').lower() not in ('','false','0','no','off'):
        raise SystemExit('Configuración de ejecución rechazada.')
PY
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git ca-certificates python3 >/dev/null
install -d -o root -g root -m 0755 "$BASE" "$BASE/releases"
if [[ -e "$RELEASE" ]]; then
  [[ -d "$RELEASE/.git" ]] || { echo 'Destino existente no es una release válida.' >&2; exit 2; }
  [[ "$(git -C "$RELEASE" rev-parse HEAD)" == "$SHA" ]] || exit 2
  [[ "$(git -C "$RELEASE" remote get-url origin)" == "$REPO_URL" ]] || exit 2
  [[ -z "$(git -C "$RELEASE" status --porcelain --untracked-files=all)" ]] || { echo 'Release modificada; no se sobreescribe.' >&2; exit 2; }
else
  STAGING="$(mktemp -d "$BASE/releases/.staging.XXXXXX")"
  # A failed staging directory is retained for inspection; no rm -rf anywhere.
  git -C "$STAGING" init -q
  git -C "$STAGING" remote add origin "$REPO_URL"
  git -C "$STAGING" fetch --depth 1 origin "$SHA"
  git -C "$STAGING" checkout --detach FETCH_HEAD
  [[ "$(git -C "$STAGING" rev-parse HEAD)" == "$SHA" ]] || exit 2
  find "$STAGING" -type d -exec chmod 0755 {} +
  find "$STAGING" -type f -exec chmod a+rX,u+w,go-w {} +
  mv "$STAGING" "$RELEASE"
fi
[[ -f "$RELEASE/infra/digitalocean-shadow/reporting.py" ]] || { echo 'Release sin verificador; no se instala.' >&2; exit 2; }
# Do not create bytecode or invoke legacy runner/pytest configurations.
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest discover \
  -s "$RELEASE/infra/digitalocean-shadow/tests" -p 'test_*.py' -v
if ! id botkalshi >/dev/null 2>&1; then
  useradd --system --home /nonexistent --shell /usr/sbin/nologin botkalshi
fi
install -d -o botkalshi -g botkalshi -m 0700 "$DATA" "$DATA/packets" "$DATA/drafts"
if [[ ! -e /etc/botkalshi-research.env ]]; then
  cat > /etc/botkalshi-research.env <<'ENV'
BOTKALSHI_SERIES=KXMLBGAME
BOTKALSHI_POLL_SECONDS=60
BOTKALSHI_ODDS_POLL_SECONDS=300
BOTKALSHI_MAX_MARKETS=40
TRADING_ENABLED=false
MOTOR_MM_EXECUTION_ENABLED=false
PYTHONDONTWRITEBYTECODE=1
# No paid API or financial keys are configured by this installer.
ENV
  chmod 0600 /etc/botkalshi-research.env
fi
UNIT_FILE="$RELEASE/infra/digitalocean-shadow/botkalshi-research.service"
[[ -f "$UNIT_FILE" ]] || exit 2
TMP_UNIT="$(mktemp)"
sed "s|/opt/botkalshi/|$RELEASE/|g" "$UNIT_FILE" > "$TMP_UNIT"
install -m 0644 "$TMP_UNIT" /etc/systemd/system/botkalshi-research.service
rm -- "$TMP_UNIT"
# Application exposes no inbound port. Existing SSH/firewall configuration is untouched.
# Host firewall/SSH hardening must be reviewed separately on the intended machine.
STARTED_AT="$(/usr/bin/python3 -c 'from datetime import UTC,datetime; print(datetime.now(UTC).isoformat())')"
systemctl daemon-reload
systemctl restart botkalshi-research.service
DEADLINE=$((SECONDS + 180))
while (( SECONDS < DEADLINE )); do
  if systemctl is-active --quiet botkalshi-research.service && \
     runuser -u botkalshi -- /usr/bin/python3 "$RELEASE/infra/digitalocean-shadow/reporting.py" \
       --data "$DATA" --after "$STARTED_AT" --max-age 180 >/dev/null 2>&1; then
    systemctl enable botkalshi-research.service >/dev/null
    runuser -u botkalshi -- /usr/bin/python3 "$RELEASE/infra/digitalocean-shadow/reporting.py" \
       --data "$DATA" --after "$STARTED_AT" --max-age 180 --output "$DATA/drafts"
    echo 'Captura local comprobada. Reportes BORRADOR; sin WhatsApp, Muse o trading conectados.'
    exit 0
  fi
  sleep 3
done
systemctl disable --now botkalshi-research.service >/dev/null 2>&1 || true
echo 'Sin captura nueva verificada en 180 s. Servicio detenido; datos y releases conservados.' >&2
echo 'No declara la API caída ni borra evidencia. Revisar estado y red desde la consola.' >&2
exit 3
