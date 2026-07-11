#!/usr/bin/env bash
#
# host_janitor.sh — janitor del HOST de Coolify (incidente disco-lleno 2026-07-10).
#
# POR QUE: /dev/vda1 es UN SOLO disco de 77G que comparten los overlays de Docker y el
# volumen con trades.db. Cada deploy de Coolify deja imagenes viejas y build-cache que se
# acumulan (~4GB por vuelta) hasta tapar el disco y tirar el server (no se puede ni escribir
# en /tmp → el propio deploy falla). Este janitor corre por CRON y recupera ese cruft de
# forma periodica, para no tener que comprarle mas disco al bot.
#
# QUE BORRA (solo cruft, jamas datos):
#   - imagenes de Docker sin usar (la del container corriendo SIEMPRE se conserva)
#   - build-cache de Docker
#   - containers frenados
#   - logs rotados (*.gz) de /var/log con mas de RETAIN_DAYS dias
#
# QUE NO TOCA (barreras de seguridad, NO debilitar):
#   - NUNCA le pasa el flag de volumenes a docker prune → el volumen con trades.db queda intacto
#   - no toca la DB ni el container vivo: la basura DENTRO de la DB (orderbook_events y demas
#     diagnostico) la maneja el loop de mantenimiento DENTRO del bot (prune por retencion +
#     wal_checkpoint cada DB_MAINTENANCE_INTERVAL_HOURS). Este janitor es SOLO el host.
#
# OJO: -af tambien borra imagenes que Coolify podria querer para rollback. En un server sin
# disco de sobra ese es el trade-off correcto (recuperar espacio > guardar rollbacks).
#
# INSTALACION (en el host, como root):
#   cp scripts/host_janitor.sh /usr/local/bin/host_janitor.sh
#   chmod +x /usr/local/bin/host_janitor.sh
#   ( crontab -l 2>/dev/null; echo "0 4 * * * /usr/local/bin/host_janitor.sh" ) | crontab -
#   # corre todos los dias 04:00; log acumulado en /var/log/host_janitor.log
#
# Correr a mano una vez para probar:  /usr/local/bin/host_janitor.sh && tail /var/log/host_janitor.log

# Resiliente a proposito: NADA de `set -e`. Un prune que falle NO debe abortar los demas ni
# saltarse el df final — cada paso se guarda y el loop sigue (misma logica fail-open del bot).
set -uo pipefail

LOG="${JANITOR_LOG:-/var/log/host_janitor.log}"
RETAIN_DAYS="${JANITOR_LOG_RETAIN_DAYS:-7}"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

{
  echo "=== $(ts) host_janitor start ==="
  df -h / | awk 'NR==1 || $NF=="/"'

  echo "--- docker image prune -af ---"
  docker image prune -af    || echo "WARN: image prune fallo"
  echo "--- docker builder prune -af ---"
  docker builder prune -af  || echo "WARN: builder prune fallo"
  echo "--- docker container prune -f ---"
  docker container prune -f || echo "WARN: container prune fallo"

  echo "--- logs rotados de /var/log > ${RETAIN_DAYS}d ---"
  find /var/log -type f -name '*.gz' -mtime "+${RETAIN_DAYS}" -delete 2>/dev/null \
    || echo "WARN: limpieza de logs fallo"

  echo "--- despues ---"
  df -h / | awk 'NR==1 || $NF=="/"'
  echo "=== $(ts) host_janitor done ==="
} >> "$LOG" 2>&1
