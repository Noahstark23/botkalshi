---
name: operacion-disco-db
description: Capa de disco/DB del bot — control de crecimiento (gates, retención, WAL, DiskGuard), reclaim de espacio (triage, rebuild, janitor del host) y el runbook completo del incidente disco-lleno 2026-07-10. Usar al diagnosticar disco lleno, DB inflada, WAL creciendo, "readonly database", deploys que fallan por espacio, o al tocar maintenance/disk_guard/rebuild.
---

# Operación de disco y DB (SQLite)

La DB es UN archivo SQLite (`/app/data/trades.db`) en UN disco de 77G compartido con los
overlays de Docker del host (Coolify). Incidente 2026-07-10: `orderbook_events` (una fila
por delta del WS, millones/día, que NADIE lee) llenó 57GB; el WAL llegó a crecer a ~8MB/s;
el disco tocó 96% y el propio deploy del fix falló por `no space left on device`.
Protocolo general: skill `botkalshi`.

## Hechos de SQLite que NO se negocian (costaron el incidente)

1. **`DELETE` NO achica el archivo.** Libera páginas a la freelist (se reusan), pero el
   `.db` mantiene su tamaño. Borrar filas ≠ liberar disco.
2. **`VACUUM` necesita ~2× el tamaño en disco libre** y bloquea la DB entera → con el
   disco lleno se NIEGA. Nunca es el plan de emergencia.
3. **`incremental_vacuum` solo funciona con `auto_vacuum=INCREMENTAL`**, que debe setearse
   ANTES de crear tablas. La DB reconstruida ya lo tiene; una DB `NONE` → no-op inofensivo.
4. **Un `DELETE` grande infla el WAL** (todas las páginas tocadas van al -wal antes del
   commit). Podar una tabla de decenas de GB con poco disco libre LLENA el disco.
5. **`dbstat` escanea TODAS las páginas** → sobre una DB de GB se cuelga minutos. Para
   triage usar `max(rowid)` (instantáneo); `--bytes` solo con DB chica o bot parado.
6. **Una conexión `mode=ro` puede no ver el -wal pendiente.** Antes de rebuild/backup:
   `PRAGMA wal_checkpoint(TRUNCATE)` con el bot PARADO (pliega y trunca el -wal).

## Defensa en profundidad (las 4 capas, en orden)

| Capa | Qué | Dónde |
|---|---|---|
| 1. No escribir basura | `PERSIST_ORDERBOOK_EVENTS=false` (default) — el gate del firehose | `data_capture.py` |
| 2. Retención (lazo abierto) | prune por ventana + `wal_checkpoint` + `incremental_vacuum`, cada `DB_MAINTENANCE_INTERVAL_HOURS` | `storage/maintenance.py` + `_run_db_maintenance` |
| 3. DiskGuard (lazo CERRADO) | mide disco real cada `DISK_GUARD_INTERVAL_MINUTES`; WARN → alerta + poda ya; CRITICAL → **descarta telemetría** (backpressure) | `storage/disk_guard.py` + `_run_disk_guard` |
| 4. Host janitor (cron) | `docker image/builder/container prune` + logs rotados — el cruft de cada deploy de Coolify (~4GB/deploy) | `scripts/host_janitor.sh` en el host |

**Regla de oro de la capa 3:** el estado de trading (trades, risk_events,
operational_state, portfolio_positions) **JAMÁS se gatea**. Solo telemetría
(orderbook_events, market_snapshots). Perder telemetría es gratis; perder un trade cuesta
plata. Fail-safe: la medición del guard falla ABIERTA (un hiccup no apaga telemetría).

## Runbook: disco lleno (probado 2026-07-10, en este orden)

1. **Medir antes de borrar** (read-only): `python scripts/disk_triage.py` (en el
   container) — mount, `.db`/`-wal`/`-shm`, ~filas por tabla, logs. En el host:
   `df -h`, `docker system df`.
2. **Liberar lo gratis sin parar el bot:** `--clean-logs`; en el host los 3 prune de
   Docker (NUNCA el flag de volúmenes — ahí vive la DB).
3. **Si el `.db` pesa GB → rebuild, no DELETE:**
   - Coolify **Stop** (congela escritores).
   - `PRAGMA wal_checkpoint(TRUNCATE)` + anotar `count(trades)` y el kill-switch de
     referencia.
   - `python3 scripts/rebuild_db.py` (stdlib pura — corre con el python del host contra el
     volumen). Crea `trades.db.rebuilt` chica: sagradas ENTERAS, diagnóstico por ventana,
     `orderbook_events` VACÍA, `auto_vacuum=INCREMENTAL`. NO swapea solo.
   - Verificar conteos vieja=nueva (el script los imprime) → swap manual (`mv`).
   - **Gotcha real:** tras el swap como root, `chown 1000:1000` los archivos de la DB o el
     bot arranca con `attempt to write a readonly database` en crash-loop.
   - Start → verificar: healthy, `-wal` no infla, `orderbook_events=0`.
   - Recién con el bot sano un rato: `rm trades.db.OLD` (ahí se libera el disco).
4. **Nunca** borrar el `.OLD` antes de verificar; **nunca** correr el rebuild sin el
   checkpoint previo; **nunca** VACUUM como emergencia.

## Env vars de la capa

`PERSIST_ORDERBOOK_EVENTS` (off) · `DB_MAINTENANCE_ENABLED`/`_INTERVAL_HOURS` (on/6h) ·
`DISK_GUARD_ENABLED`/`_INTERVAL_MINUTES`/`_WARN_FREE_GB`/`_CRITICAL_FREE_GB` (on/5m/5/2).

## Scripts

- `scripts/disk_triage.py` — read-only; `--clean-logs` (seguro con bot arriba); `--bytes`
  (dbstat, lento — solo DB chica o bot parado).
- `scripts/rebuild_db.py` — reclaim one-time (bot PARADO). Verifica sagradas y no swapea.
- `scripts/host_janitor.sh` — cron diario del HOST. Test-guard le prohíbe `--volumes`.
