#!/bin/bash
# Read-only supervision units (ASTRA-DEPLOY-SUPERVISION-20260924). Thin wrapper: all
# logic, checks and boundaries live in supervision_deploy.py (tested with the system
# python). It never starts, stops, restarts, enables or disables the live bot, the
# research collector or any timer, and it never reads or writes a credential.
#   bash install-supervision.sh install <SHA-40> --dedicated-research-host [--dry-run]
#   bash install-supervision.sh rollback --dedicated-research-host [--dry-run]
#   bash install-supervision.sh verify
set -euo pipefail
unset PYTHONPATH PYTHONHOME PYTHONINSPECT PYTHONOPTIMIZE
export PATH='/usr/sbin:/usr/bin:/sbin:/bin'
umask 077
exec /usr/bin/python3 -I "$(dirname "$(readlink -f "$0")")/supervision_deploy.py" "$@"
