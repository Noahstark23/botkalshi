# Part B — Gaps pendientes que BLOQUEAN la cuarta ventana de activación

**Fecha:** 2026-06-01
**Origen:** auditoría de robustez de PR #11 (`feat/v2-recovery-supervisor`).
**Estado:** read-only. **No** se modifica `orderbook_manager_v2.py` (prohibido por directiva). `USE_ORDERBOOK_MANAGER_V2=False`.
**Veredicto:** PR #11 implementa la *lógica* de convergencia (code 15, retry/timeout, evicción, cap RAM) y la testea (23/23), pero la auditoría destapó **dos gaps que bloquean activar V2**. Hasta cerrarlos, el merge de PR #11 sigue congelado.

---

## Gap (b) — El supervisor de recovery no está vivo

**Síntoma doble (ambos verificados en código):**

1. **No instanciado.** `_recovery_supervisor()` existe como método en `orderbook_manager_v2.py` pero **nadie lo lanza**. El wiring de V2 en `data_capture.py:486-494` registra los handlers (`ws.on("orderbook_delta", ...)`, etc.) pero **nunca** hace `asyncio.create_task(self._v2_manager._recovery_supervisor())`. → Aunque se active el flag, el supervisor no corre: los `_recovery_deadlines` se registran pero nadie los chequea, y el recovery vuelve a poder colgarse (justo lo que Part B venía a resolver).

2. **Sin callback anti-zombie.** Si el supervisor se lanzara como task suelta y muriera (excepción fuera del `try` por-tick, o cancelación mal manejada), **no hay `add_done_callback` ni integración al `asyncio.wait` cooperativo** que lo detecte. Quedaría como zombie silencioso: el proceso sigue "vivo" pero sin convergencia de recovery — el anti-patrón estructural de la Lección 7 ("el bot dice que corre ≠ el bot corre"), ahora aplicado a una task de fondo.

**Impacto:** sin (1) la convergencia no existe en runtime; sin (2) puede desaparecer en silencio. Bloqueante.

## Gap (c) — La reintegración 00:00 UTC es inexistente (solo docstring)

`_evict_ticker()` documenta *"El discovery diario (00:00 UTC) limpia `_evicted` y re-inicializa"*. Pero:

- **No existe** ninguna tarea de discovery diario / scheduler 00:00 UTC en el código. Grep: las únicas referencias a "00:00 UTC" son de `risk/manager.py` (stop-loss), sin relación. No hay APScheduler/cron wireado para esto.
- **Nada limpia `_evicted` en runtime.** Hay `_evicted.add()`; no hay `.discard()`/`.clear()` salvo dentro de `_handle_code15` (que limpia recoveries, no evicción).

**Impacto:** un ticker evictado (por code 15 escalado, retries agotados, o overflow de buffer) queda **inutilizado en V2 hasta un reinicio manual del proceso**. No hay recuperación automática. Su data sigue capturándose por REST (V1 pasivo), pero V2 nunca lo re-incorpora. Bloqueante para una operación 24/7 desatendida.

---

## Diseño read-only — Cableado anti-zombie del supervisor (Gap b)

**Objetivo:** que el supervisor de recovery viva dentro del mismo bloque cooperativo de `data_capture.py:508-527` que `ws_task`/`snap_task`, de modo que **su muerte tire abajo el runner de forma controlada** (Lección 7: nada de `gather(..., return_exceptions=True)` que trague; supervisor explícito que reporta a `BotState.record_error` y re-leva).

**Estructura propuesta (texto, sin codear en el archivo):**

1. **Wrapper supervisado**, gemelo de `_run_ws_supervised`/`_run_snapshots_supervised`: un nuevo `_run_recovery_supervised(self)` que:
   - `await self._v2_manager._recovery_supervisor()`
   - en `except asyncio.CancelledError: raise` (shutdown limpio).
   - en `except Exception as e:` → `BotState.record_error(f"recovery_supervisor crashed: ...")` + `logger.exception(...)` + **re-raise** (fail-loud, igual que los otros dos wrappers).

2. **Creación condicional de la task**, junto a `ws_task`/`snap_task` (líneas 509-511), **solo si V2 está activo**:
   ```
   tasks = [ws_task, snap_task, stop_task]
   if self._v2_manager is not None:
       recovery_task = asyncio.create_task(self._run_recovery_supervised(), name="recovery_supervisor")
       tasks.append(recovery_task)
   ```
   (Con el flag en False, `self._v2_manager is None` → la task no se crea; cero impacto en V1.)

3. **Integración al `asyncio.wait(FIRST_COMPLETED)`** (línea 514): pasar `tasks` en vez de la lista fija. Así, si `recovery_supervisor` termina (crash o fin), `FIRST_COMPLETED` despierta, se cancelan las pendientes, y el bloque `for t in done` ya existente loggea el crash. El runner cae de forma controlada y el supervisor de `runner.py` lo reinicia/reporta — exactamente el patrón de Lección 7.

**Por qué no `add_done_callback` suelto:** el `asyncio.wait` cooperativo ya implementado es superior — un callback solo loggearía; el `wait` **tira abajo el ciclo** para que el restart ocurra arriba. Reusar el patrón existente es más simple y consistente que agregar un mecanismo paralelo.

**Nota:** este cableado vive en `data_capture.py`, **no** en `orderbook_manager_v2.py` (que queda intacto). Cuando MOTOR_1 esté activo, el dueño del manager es `runner.py`; ahí habrá que replicar el mismo cableado.

---

## Diseño read-only — Cooldown / reintegración de evicción (Gap c)

> ⚠️ La directiva del CTO definió esta tarea como "DISEÑO TÉCNICO READ-ONLY DEL COOLDOWN DE EVICCIÓN (GAP C)" pero **el texto llegó truncado** (cortado tras el encabezado). El siguiente diseño se infiere del nombre y del docstring de `_evict_ticker`. **Pendiente de confirmación de requisitos.**

**Objetivo:** que un ticker evictado se reintegre automáticamente, sin reinicio manual. Dos enfoques, no excluyentes:

**Opción A — Reintegración diaria 00:00 UTC (lo que prometía el docstring):**
- Una tarea periódica (en el bloque cooperativo de `data_capture.py`, o un scheduler) que al cruzar 00:00 UTC:
  1. `self._v2_manager` limpia `_evicted` (método público nuevo a diseñar, p.ej. `reset_evictions()` — pero esto tocaría el manager, hoy prohibido → queda para cuando se levante la prohibición).
  2. Re-suscribe / fuerza snapshot fresco de los tickers reintegrados para reconstruir sus books.
- Ventaja: alineado con el ciclo de discovery diario ya mencionado. Desventaja: hasta 24h de inutilización en V2 para un ticker evictado a las 00:01.

**Opción B — Cooldown por ticker (reintegración más ágil):**
- Al evictar, registrar `_evicted_at[ticker] = monotonic()`. El supervisor (ya existente, una vez wireado por Gap b) chequea por tick: si `monotonic() - _evicted_at[ticker] > EVICTION_COOLDOWN_SEC` (p.ej. 300s, calibrable), saca el ticker de `_evicted`, lo borra de `_books`, y dispara un snapshot fresco → se rebootea por el flujo normal de bootstrap (ya robusto por Part A).
- Ventaja: recuperación en minutos, no horas; reusa el supervisor que Gap b ya pondría a correr. Desventaja: un ticker que falla de forma persistente entraría en ciclos evict→cooldown→evict (mitigable con backoff exponencial del cooldown o un cap de reintegraciones/día).

**Recomendación (a validar):** **Opción B con backoff**, porque (i) recupera sin esperar al cambio de día, (ii) se apoya en el supervisor que de todos modos hay que wirear para Gap b, y (iii) el bootstrap post-Part-A ya maneja la reconstrucción de un book desde cero de forma segura. La Opción A puede coexistir como "reset duro" diario de respaldo.

**Ambas opciones requieren tocar `orderbook_manager_v2.py`** (método de reintegración + tracking de `_evicted_at`), hoy **prohibido**. Por lo tanto: diseño documentado, implementación diferida hasta que se autorice modificar el manager.

---

## Resumen de bloqueo

| Gap | Estado | Toca manager? | Bloquea V2? |
|---|---|---|---|
| (b) supervisor no vivo + sin anti-zombie | diseñado, no implementado | wiring en `data_capture.py` (no el manager) | **Sí** |
| (c) reintegración 00:00 UTC inexistente | diseñado (Opc. A/B), no implementado | **Sí** (prohibido hoy) | **Sí** |

Mientras estos dos gaps no se cierren, **PR #11 no debe mergearse para activar V2**. El flag permanece `False`.
