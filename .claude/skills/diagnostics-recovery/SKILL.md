---
name: diagnostics-recovery
description: Cómo diagnosticar el bot SIN creerle al primer verde — falsos "healthy" documentados, qué campo del /status miente y cuándo, dónde viven los errores reales, y las reglas de los scripts de diagnóstico. Usar al auditar logs, interpretar /status, investigar "el bot no hace nada", o escribir un script diag_* nuevo.
---

# Diagnostics & Recovery — no creerle al primer verde

Los falsos "healthy" ya costaron sesiones enteras de arqueología. Reglas con factura:

## La regla que engloba a todas (destilada de TRES falsos-healthy)

`ws_alive` verde por ventana de gracia; el preflight "todo listo" con el bot pausado
(punto ciego del estado runtime); `/health` healthy con 0/229 books inicializados
durante 9.5 horas (2026-07-31). Tres parches puntuales después, la regla general:
**toda señal de salud debe ser FALSABLE y distinguir "está bien" de "no lo sé"** — un
check que devuelve verde cuando no puede evaluar da confianza falsa, peor que no tener
check. Al escribir o auditar un check: (a) ¿qué estado del mundo lo pone en rojo? — si
no existe, no es un check; (b) "no evaluable" se reporta como estado propio, jamás como
éxito; (c) toda gracia (warm-up, boot) es ACOTADA y con su porqué escrito. El fail-open
sigue siendo para la LECTURA del trading (un hiccup no apaga el bot); la salud
REPORTADA no hereda ese default.

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
5. **Ningún contador solo mide los gaps CRUDOS del feed** (post-#204, anti-espiral):
   la supresión NO registra el gap (deliberado — a 166/min ese contador era el flood de
   Telegram), así que `gaps_last_60s` solo cuenta los que ARRANCARON recovery; y
   `recoveries_suppressed_total` suma TRES paths (gap de secuencia, cuarentena por
   desync, cuarentena por incoherencia — los reintentos internos NO suman: bypass
   `internal=True`, pineado por test). Tasa cruda ≈ `gaps_last_60s` + Δsupresiones por
   minuto, discriminando con `grep -c v2.desync_quarantine` / `v2.book_incoherent` en
   la misma ventana. Métrica nueva sin la frase "sube si y solo si X" escrita = métrica
   que no decide nada (desarrollo-bot, regla 12).

## Dónde viven los errores reales

- **`BotState.last_error` / `current_error()`** (con TTL de 15min — un error viejo se
  limpia solo; "sin error" ≠ "nunca hubo"). Todo 401/429/`SidGapError`/desync DEBE
  registrarse ahí (`record_error`); una excepción tragada en nivel debug es un bug.
- **Logs greppables por convención**: `risk.sl_status` (frenos en vivo), `motor2.funnel`,
  `motor5.funnel`, `v2.recovery_*` / `v2.desync_quarantine` / `v2.bootstrap_buffer_capped`,
  `motor5.book_shape` / `motor5.book_error`, `odds_api: CUOTA AGOTADA`, `[MOTOR N SHADOW]`.
- **`risk_events`** (DB): kill_switch, daily_stop, rollbacks — el rastro persistente.
- **Clasificación ESTRICTA de errores de API**: leer el status code Y el texto literal
  antes de mapear. Un 401 no es un 429 (auth ≠ rate limit), y el mismo código cambia de
  significado por el payload: el 401 `OUT_OF_USAGE_CREDITS` de The Odds API es CUOTA (se
  arma el breaker mensual), no credenciales; el 409 de Kalshi solo es "FOK sin volumen"
  si trae `fill_or_kill_insufficient_resting_volume`; el code 15 del WS llegó con el
  payload ANIDADO y el parser plano lo convirtió en ruido (#186). Mapear por código a
  secas ya cegó el bot días enteros.
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
