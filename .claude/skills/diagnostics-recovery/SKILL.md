---
name: diagnostics-recovery
description: Cómo diagnosticar el bot SIN creerle al primer verde — falsos "healthy" documentados, qué campo del /status miente y cuándo, dónde viven los errores reales, y las reglas de los scripts de diagnóstico. Usar al auditar logs, interpretar /status, investigar "el bot no hace nada", o escribir un script diag_* nuevo.
---

# Diagnostics & Recovery — no creerle al primer verde

Los falsos "healthy" ya costaron sesiones enteras de arqueología. Reglas con factura:

## El /status miente en formas conocidas — validar empíricamente

1. **`capture_running: true` NO prueba que fluyan datos.** Es un flag de arranque de la
   task, no un heartbeat del feed. Validación real: `last_ws_message` reciente + los
   contadores que INCREMENTAN entre dos lecturas (books_initialized, funnel cycles).
   Un WS zombie puede dejar el flag en true con el feed muerto.
2. **`orderbook_manager_v2.enabled` es el FLAG, no el estado** (lección 2026-07-17:
   `enabled: false` escondía un manager corriendo con el sid=1 muerto). Desde #176 el
   bloque reporta si la INSTANCIA existe (`running`) + `sids_disabled` (books stale por
   circuit breaker) + `bootstrap_capped_tickers` (snapshots que no llegan). Un
   `sids_disabled` no vacío = mercados CIEGOS aunque todo lo demás esté verde.
3. **El dashboard y el freno deben decir lo mismo** — si divergen, el bug es de
   observabilidad, no del freno (el "796% fantasma" era el dashboard recalculando con
   otras ventanas). La fuente única es `RiskManager.stop_loss_status()`.
4. **Un heartbeat con `signals=0` recién arrancado no es representativo** — mirar
   ticks/tracked acumulados, no la primera línea post-boot (falso "M1 no encuentra nada"
   del 2026-07-18: sí había encontrado 14, el heartbeat era de un motor re-arrancado).

## Dónde viven los errores reales

- **`BotState.last_error` / `current_error()`** (con TTL de 15min — un error viejo se
  limpia solo; "sin error" ≠ "nunca hubo"). Todo 401/429/`SidGapError`/desync DEBE
  registrarse ahí (`record_error`); una excepción tragada en nivel debug es un bug.
- **Logs greppables por convención**: `risk.sl_status` (frenos en vivo), `motor2.funnel`,
  `motor5.funnel`, `v2.recovery_*` / `v2.desync_quarantine` / `v2.bootstrap_buffer_capped`,
  `motor5.book_shape` / `motor5.book_error`, `odds_api: CUOTA AGOTADA`, `[MOTOR N SHADOW]`.
- **`risk_events`** (DB): kill_switch, daily_stop, rollbacks — el rastro persistente.
- **El buffer del visor de Coolify scrollea**: el boot log se pierde de la vista — para
  líneas de arranque usar `docker logs <container> | grep -m1 ...`, no el visor web.
  Y una línea one-shot puede haber salido ANTES de tu ventana (lección del book_shape:
  por eso los diagnósticos one-shot ahora llevan re-arme con backoff).

## Diagnóstico à la casa (el método que funcionó)

1. **Discriminadores antes que fixes**: formular "si X → causa A; si Y → causa B" y
   correr el pack read-only (greps exactos / SQL) ANTES de escribir un diff. La query de
   `leg_states` ahorró un PR entero sobre algo inarreglable.
2. **Reconciliar por línea de tiempo** cuando dos fuentes se contradicen (deploy vs
   ventana de log, flag vs env efectivo): ambos pueden tener razón sobre momentos
   distintos.
3. **Pedir el env EFECTIVO** (/status o dump) antes de razonar sobre defaults del repo —
   producción puede haber overrideado (lección del informe: recomendé una palanca ya
   accionada).

## Reglas de los scripts de diagnóstico (scripts/)

- **Read-only por contrato** + docstring de uso (dónde correr, qué gasta). Si un modo
  gasta cuota/API, va detrás de un flag explícito (`--live`, `--bytes`).
- **Conexión desde settings/.env, jamás hardcodeada** a un entorno (el mismo script debe
  correr en el container y en local).
- **Salida con veredicto honesto**: capaz de decir "NO EVALUABLE" o "sin datos" en vez
  de un número inventado; los umbrales de significancia explícitos (n mínimo, t-stat).
- SQLite en producción: `mode=ro` puede no ver el WAL sin checkpoint; `dbstat` colgó con
  57GB — estimar por `max(rowid)` primero. Runbook completo: skill `operacion-disco-db`.
