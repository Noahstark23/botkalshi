#!/bin/bash
# macOS bash 3.2 compatible. No sudo, no root compose, no .env sourcing.
set -euo pipefail
umask 077
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
STATE="$HERE/.local"

check_docker() {
  command -v docker >/dev/null 2>&1 || { echo 'Instala/inicia Docker Desktop en el Mac; no se instala automaticamente.' >&2; exit 2; }
  case "${DOCKER_HOST:-}" in ''|unix://*) ;; *) echo 'DOCKER_HOST remoto rechazado.' >&2; exit 2;; esac
  local endpoint
  endpoint="$(docker context inspect --format '{{.Endpoints.docker.Host}}')"
  case "$endpoint" in unix://*) ;; *) echo 'Se requiere contexto Docker local, no un host remoto.' >&2; exit 2;; esac
  docker info --format '{{.OSType}}' | grep -qx linux || { echo 'Inicia el motor Linux de Docker Desktop.' >&2; exit 2; }
  docker compose version >/dev/null
}

prepare() {
  check_docker
  mkdir -p "$STATE/reports"
  if [ ! -f "$STATE/build.env" ]; then
    # First image download requires Internet; runtime OFFLINE has no networking.
    docker pull python:3.12-slim-bookworm
    local digest
    digest="$(docker image inspect python:3.12-slim-bookworm --format '{{index .RepoDigests 0}}')"
    printf '%s\n' "$digest" | grep -Eq '^(docker.io/library/)?python@sha256:[0-9a-f]{64}$' || { echo 'Digest no verificable; abortado.' >&2; exit 2; }
    printf 'LAB_PYTHON_IMAGE=%s\n' "$digest" > "$STATE/build.env"
  fi
  grep -Eq '^LAB_PYTHON_IMAGE=(docker.io/library/)?python@sha256:[0-9a-f]{64}$' "$STATE/build.env" || { echo 'build.env no contiene digest valido.' >&2; exit 2; }
}

compose() {
  # --env-file prevents loading a repo/account .env. Clear overriding variables.
  env -u COMPOSE_FILE -u COMPOSE_PROFILES -u LAB_PYTHON_IMAGE \
    docker compose --env-file "$STATE/build.env" -p noel-kalshi-lab \
    -f "$HERE/compose.yaml" "$@"
}

export_reports() {
  mkdir -p "$STATE/reports"
  local ext temp
  for ext in json md; do
    temp="$(mktemp "$STATE/reports/.exportXXXXXX")"
    if compose run --rm --no-deps --entrypoint cat lab "/data/reports/latest.$ext" > "$temp"; then
      mv "$temp" "$STATE/reports/latest.$ext"
    else
      rm -f "$temp"
      echo 'No hay un reporte exportable; los anteriores se conservan.' >&2
      return 2
    fi
  done
  echo "Reportes: $STATE/reports"
}

case "${1:-help}" in
  doctor)
    check_docker
    printf 'Docker local disponible. El Mac no ha sido modificado por esta comprobacion.\n'
    ;;
  prepare)
    prepare
    compose config --quiet
    compose build
    ;;
  up)
    prepare
    compose config --quiet
    compose up --build -d --no-deps lab
    echo 'Contenedor solicitado en OFFLINE. Comprueba: bash infra/mac-lab/lab.sh status'
    ;;
  test)
    prepare
    compose build
    compose run --rm --no-deps --entrypoint python lab -m unittest discover -s tests -v
    ;;
  status|logs|stop)
    check_docker
    test -f "$STATE/build.env" || { echo 'Laboratorio no preparado.' >&2; exit 2; }
    case "$1" in
      status) compose ps; compose exec -T lab python /opt/lab/runtime.py status --data /data;;
      logs) compose logs --tail 80 lab;;
      stop) compose stop lab;;
    esac
    ;;
  export)
    check_docker
    test -f "$STATE/build.env" || { echo 'Laboratorio no preparado.' >&2; exit 2; }
    export_reports
    ;;
  offline-once|backup|inspect|public-once)
    prepare
    compose build
    # One writer at a time. Stop only this lab, never other projects/volumes.
    compose stop lab
    case "$1" in
      public-once)
        compose -f "$HERE/compose.public.yaml" run --rm --no-deps lab
        ;;
      inspect)
        test "$#" -eq 2 || { echo 'Uso: lab.sh inspect /ruta/snapshot-consistente.sqlite3' >&2; exit 2; }
        test -f "$2" && test ! -L "$2" || { echo 'Snapshot inexistente o enlace simbolico.' >&2; exit 2; }
        for suffix in -wal -journal; do
          test ! -e "$2$suffix" || { echo 'Crea un backup SQLite consistente primero.' >&2; exit 2; }
        done
        source="$(cd "$(dirname "$2")" && pwd -P)/$(basename "$2")"
        case "$source" in *:*|*$'\n'*) echo 'Ruta no soportada.' >&2; exit 2;; esac
        compose run --rm --no-deps -v "$source:/snapshot/input.sqlite3:ro" lab \
          inspect --snapshot /snapshot/input.sqlite3 --data /data
        ;;
      *) compose run --rm --no-deps lab "$1" --data /data;;
    esac
    test "$1" = backup || export_reports
    ;;
  *)
    echo 'Uso: lab.sh {doctor|prepare|test|up|status|logs|export|stop|offline-once|backup|inspect SNAPSHOT|public-once}'
    echo 'up = OFFLINE sintetico. public-once = hasta 7 lecturas publicas, sin motores ni claves.'
    ;;
esac
