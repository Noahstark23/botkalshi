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

## Diseño read-only — Cooldown de Evicción determinista (Gap c)

**Decisión de diseño (aprobada por CTO):** se descarta el "00:00 UTC" del docstring original y el "backoff" del borrador inferido. El mecanismo es un **cooldown determinista de 3600s por ticker**, controlado por `time.monotonic()`, gestionado dentro del tick de 1s del supervisor (`_check_recovery_timeouts`). **Sin cron ni scheduler externo.**

**Por qué es superior al "00:00 UTC":**
- El horario fijo dejaría un ticker evictado a las 00:01 inutilizado ~24h. El cooldown reintegra cada ticker **1h después de SU propia evicción**, no en un horario global arbitrario.
- Reusa el supervisor loop que Gap b ya pondría a correr (mismo tick de 1s que detecta timeouts de recovery) → cero infraestructura nueva, menos superficie de fallo.
- Es exactamente el patrón del **TTL de `last_error`** ya validado en el watchdog (PR #1): timestamp + expiración por diferencia de tiempo. Patrón conocido, no inventado.

### Flujo de 4 pasos

**(1) Estructura del dict.** Reemplazar `self._evicted: set[str]` por:
```
self._evicted_cooldowns: dict[str, float]   # ticker → monotonic deadline de reintegración
```
La key presente = ticker actualmente evictado; el value = instante (`time.monotonic()`) a partir del cual puede reintegrarse. Migrar de `set` a `dict` mantiene la semántica de membresía (`ticker in self._evicted_cooldowns`) y agrega el timestamp en la misma estructura.

**(2) Penalización de 3600s en `_evict_ticker`.** Al evictar, registrar el deadline:
```
EVICTION_COOLDOWN_SEC = 3600.0   # constante de clase
...
def _evict_ticker(self, ticker):
    self._books[ticker] = None
    self._evicted_cooldowns[ticker] = time.monotonic() + self.EVICTION_COOLDOWN_SEC
    self._bootstrap_buffer.pop(ticker, None)
    logger.critical(...)
```
`monotonic()` (no `time()`/`datetime`) porque es inmune a saltos del reloj del sistema (NTP, DST) — la penalización es una duración relativa, no un instante de calendario.

**(3) Guarda en `handle_message` que protege a los tickers nuevos.** La guarda de descarte cambia de `if ticker in self._evicted` a:
```
if ticker in self._evicted_cooldowns:
    return   # ticker en penalización → descartar delta (data_capture REST sigue en SQLite)
```
**Crítico:** la guarda chequea **membresía explícita en el dict**, NO `_books.get(ticker) is None`. Un ticker **nuevo** (nunca visto) no está en `_evicted_cooldowns` → pasa y sigue el flujo normal de bootstrap. Esto preserva la corrección de Part B: solo los evictados a propósito se descartan; los nuevos arrancan normal. (Es el mismo bug que ya se evitó al elegir un contenedor explícito sobre `book is None`.)

**(4) Limpieza en el tick de 1s del supervisor.** Dentro de `_check_recovery_timeouts` (que ya corre cada `SUPERVISOR_TICK_SEC=1s`), agregar una pasada de reintegración:
```
now = time.monotonic()
ready = [t for t, deadline in self._evicted_cooldowns.items() if now >= deadline]
for ticker in ready:
    del self._evicted_cooldowns[ticker]   # sale de penalización
    self._books.pop(ticker, None)         # borra el book None → vuelve a "nunca visto"
    # el próximo delta del ticker entra por bootstrap normal (encola hasta snapshot);
    # opcional: forzar get_snapshot del ticker para acelerar la reconstrucción.
```
Tras la limpieza, el ticker queda como "nunca visto": su próximo mensaje sigue el bootstrap robusto de Part A (encola pre-snapshot, drena al llegar el snapshot). No hay reconstrucción especial que diseñar — se reusa el camino ya probado.

### Propiedades

- **Determinista:** exactamente 3600s desde cada evicción, sin backoff ni estado acumulado. Predecible para operar y testear.
- **Sin scheduler:** vive en el tick del supervisor existente (Gap b). Si el supervisor no corre (Gap b sin cerrar), la reintegración tampoco → **Gap c depende de Gap b**. Ambos deben cerrarse juntos.
- **Ciclo evict→reintegra→evict acotado:** un ticker que falla de forma persistente se reintegra cada hora, falla, y se vuelve a evictar 1h. Es 1 ciclo/hora por ticker — tolerable. (Si se observa flapping en prod, se puede agregar un cap de reintegraciones/día como mejora futura; no es necesario para el diseño base.)

**Requiere tocar `orderbook_manager_v2.py`** (`_evicted` → `_evicted_cooldowns`, constante, guarda, pasada de limpieza), hoy **prohibido**. Implementación **diferida** hasta que se autorice modificar el manager, en turno separado, tras revisión adversarial de este diseño.

---

## Resumen de bloqueo

| Gap | Estado | Toca manager? | Bloquea V2? |
|---|---|---|---|
| (b) supervisor no vivo + sin anti-zombie | diseñado, no implementado | wiring en `data_capture.py` (no el manager) | **Sí** |
| (c) reintegración de evictados inexistente | diseñado (cooldown determinista 3600s vía `_evicted_cooldowns`), no implementado | **Sí** (prohibido hoy) | **Sí** |

Mientras estos dos gaps no se cierren, **PR #11 no debe mergearse para activar V2**. El flag permanece `False`.

---

## Anexo — Poda arquitectónica V2 (decisión por datos, 2026-06-01)

**Decisión de arquitectura cerrada:** modelo **Híbrido (Detección WS + Ejecución REST)**, enfocado **exclusivamente en mercados NBA** (4-8 mercados, no 38).

**Segmentación por deporte que reduce el scope:** NHL relámpago/descartado, MLB ruido/descartado, soccer oportunismo de cola. Queda NBA como foco.

**Consecuencia para Part B:** al reducir el scope a 4-8 mercados NBA, **el riesgo sistémico de estampida de recoveries que justificaba B1/A2 desaparece**. Con 4-8 tickers, un fallo de supervisor no genera la cascada de re-bootstrap masivo que motivaba el aislamiento in-process complejo.

**Qué se MANTIENE (V2-light):**
- **Part A** (bootstrap buffer) — ya en `main`.
- **Supervisor básico**: timeout + retry + intercepción `code 15`.
- **Gap C**: cooldown in-memory de 1h (`_evicted_cooldowns`, reintegración activa).

**Qué se DESCARTA (sobre-ingeniería para la escala real):**
- ~~**B1** — supervisor del supervisor con backoff y ventanas temporales de conteo de fallos.~~ Eliminado.
- ~~**A2** — modo seguro en `runner.py` con estado durable en disco (contador de arranques persistido).~~ Eliminado.

**El freno al crash-loop NO se delega a un cap de restart de Docker** — ese cap resultó no-soportado en este entorno (ver nota abajo). Se mitiga por otra vía (Telegram + healthcheck + intervención manual).

> Nota histórica: el anexo previo "Cierre de diseño de Gap (b): aislamiento del supervisor (B1+A2)" queda **derogado** por esta poda. Se conserva el registro de las 4 correcciones de wording de (b)/(c) que siguen vigentes (restart = contenedor por Coolify; no persistir cooldowns; reintegración activa obligatoria), pero B1 y A2 ya **no forman parte del diseño**.

### Cap de restart de Docker: NO soportado en este entorno (cerrado)

**Cap de restart de Docker: NO soportado en Coolify** (hardcodea `unless-stopped`, ref. discusión Coolify #10259; no Swarm → `deploy.restart_policy` ignorado). **Decisión: NO implementar wrapper de entrypoint** (= A2 reintroducido, descartado por sobre-ingeniería). A escala de 4-8 mercados NBA, el riesgo de crash-loop es bajo; se mitiga con la alerta de Telegram existente (PR #1) + healthcheck + intervención manual (apagar el flag). El cap automático era nice-to-have, no bloqueante.

Detalle técnico que confirma el cierre (sin necesidad de probar en el entorno):
- El campo `restart:` de Compose solo acepta `no|always|on-failure|unless-stopped` (sin contador) → `on-failure:5` sería sintaxis inválida.
- `deploy.restart_policy.max_attempts` solo lo respeta Docker Swarm; este deploy no es Swarm → se ignora silenciosamente.
- Coolify re-inyecta `unless-stopped` en cada deploy y no expone setting nativo de max-attempts.

`docker-compose.yml` **no se modifica**. No hay wrapper de entrypoint. No hay redeploy.
