#!/usr/bin/env bash
set -euo pipefail
umask 077
REPO_URL="https://github.com/Noahstark23/botkalshi.git"
BRANCH="feat/digitalocean-shadow-20260915"
ROOT="/opt/botkalshi"
DATA="/var/lib/botkalshi-research"

if [ "$(id -u)" -ne 0 ]; then echo "run as root" >&2; exit 2; fi
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git ca-certificates python3 ufw >/dev/null

if ! id botkalshi >/dev/null 2>&1; then
  useradd --system --home /nonexistent --shell /usr/sbin/nologin botkalshi
fi
install -d -o botkalshi -g botkalshi -m 700 "$DATA" "$DATA/packets"

if [ -d "$ROOT/.git" ]; then
  git -C "$ROOT" fetch --depth 1 origin "$BRANCH"
  git -C "$ROOT" checkout -f FETCH_HEAD
else
  rm -rf "$ROOT"
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$ROOT"
fi
chown -R root:root "$ROOT"
chmod -R go-w "$ROOT"

install -m 0644 "$ROOT/infra/digitalocean-shadow/botkalshi-research.service" /etc/systemd/system/botkalshi-research.service
if [ ! -f /etc/botkalshi-research.env ]; then
  cat > /etc/botkalshi-research.env <<'ENV'
BOTKALSHI_SERIES=KXMLBGAME
BOTKALSHI_POLL_SECONDS=60
BOTKALSHI_ODDS_POLL_SECONDS=300
BOTKALSHI_MAX_MARKETS=40
TRADING_ENABLED=false
MOTOR_MM_EXECUTION_ENABLED=false
# ODDS_API_KEY is intentionally absent. Configure locally later; never commit it.
ENV
  chmod 0600 /etc/botkalshi-research.env
fi

ufw allow OpenSSH >/dev/null
ufw --force enable >/dev/null
systemctl daemon-reload
systemctl enable --now botkalshi-research.service
sleep 3
systemctl --no-pager --full status botkalshi-research.service || true
python3 - <<'PY'
import json, pathlib, sys
p=pathlib.Path('/var/lib/botkalshi-research/health.json')
if not p.exists():
    print('health.json not created yet; check journalctl -u botkalshi-research', file=sys.stderr); sys.exit(3)
x=json.loads(p.read_text())
assert x['mode']=='SHADOW_READONLY' and x['execution_authorized'] is False
print(json.dumps(x, indent=2))
PY
