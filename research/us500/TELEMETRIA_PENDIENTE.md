# Telemetría pendiente del runtime para Telegram

Preparado el 11 de octubre de 2026. **Estado: contrato y parser probados localmente; instrumentación no instalada.** No se modificó ni reinició el runtime financiero. No se consultaron cuentas, credenciales, posiciones, órdenes ni bases de datos.

## Qué se puede conectar hoy

El receptor separado puede seguir leyendo `/health`, `/status` y los conteos de señales ya incorporados de `/stats/edges?days=1`. Esos conteos no demuestran configuración ni ejecución de un componente. Un cero no significa apagado.

El nuevo `parse_runtime_telemetry(status.get("motor_telemetry"), now_ms=...)` permite al receptor aceptar un bloque opcional futuro sin invalidar los campos antiguos. Si falta, devuelve `available=False, reason="NOT_INSTALLED"` y estados desconocidos. En el runtime actual falta ese bloque: **no se puede completar con veracidad la tarea propia de M1/M2/M3 ni la instancia/callback de M6/REST/M8/M9 sólo cambiando Telegram**. Añadir estos hechos requiere un cambio de observabilidad revisado y desplegado en ese runtime. No requiere ni autoriza activar ningún motor.

## Evidencia y alcance del código revisado

Base del runtime confirmada por el agente principal mediante la ruta resuelta del despliegue: `83075d4d747212a1517552010b64fbda0cad0d8a`. Mi revisión fue únicamente sobre copias locales en `/Users/stark/Downloads/botkalshi-P0-cierre/US500_ESTADOS_20261011/`; no ejecuté SSH ni hice solicitudes de mercado. Base del candidato al iniciar: `74c7de6`.

| Archivo copiado | SHA256 de contenido revisado |
|---|---|
| `runtime_src_runner.py` | `758f1264181b47aed42933918508fd6a1e67418e28b11b55c65b51cd15ae84c6` |
| `runtime_src_monitoring_health.py` | `a8b13416c6d0db14d747b17321e76dbb344f9dc041e987668638025d371560ba` |
| `runtime_src_strategies_data_capture.py` | `f64821156aaae71fb057b7956edc89df582b48b88edfb5f348cc3f8602670a5f` |
| `runtime_src_strategies_motor_1_arbitrage_engine.py` | `5a38ad1f094c00c643804af3386a842d2e98d383e8a3cfba1e5bdabef44f16d2` |
| `runtime_src_strategies_motor_2_consensus_poller.py` | `bf9c2da365fd873701d84be9f972d6e31726f3b5cf20b85ceb6e561f069cfc2c` |
| `runtime_src_strategies_motor_3_clv_engine.py` | `da2d1f7487b45e9d48b0deb8ebaf37c116d128f6a84053426a9fedb1e91b9129` |
| `runtime_src_strategies_motor_3_clv_poller.py` | `e42a869b9b966fb9355c87ec80e8ad7044244b23dde5dda48baa03834ca3ec5d` |
| `runtime_src_strategies_motor_rest_arb_engine.py` | `54b25d18edfdf27cbc48e468b5e903c5d953240d2014a2a3a54704ddaa436026` |
| `runtime_src_strategies_motor_8_ofi_shadow.py` | `bc5adb4774c1799f076711c05c0ca72dd61c8745fcce4f355bc1c191720fbe5a` |
| `runtime_src_strategies_motor_9_spillover_shadow.py` | `9b09b0d98f9adcb485c044fd94c0f7649dd7d1ee0f08716c68b83d9d583dba21` |

La fuente del brazo de salidas M2 no fue copiada en este lote; su tarea y constructor sí aparecen en `runner.py:484-512`. Debe verificarse el archivo `motor_2_consensus/exit_engine.py` del SHA exacto antes de instrumentar su tick; el archivo del candidato no sustituye esa comprobación.

## Contrato preparado

Código independiente: `runtime_telemetry_contract.py`. Pruebas: `tests/test_runtime_telemetry_contract.py`. Sólo stdlib; sin imports del bot, red, DB, entorno, logs ni llamadas financieras. Almacena diez componentes fijos, cuatro observaciones por componente y dependencias de lista fija; no acumula eventos, tickers, precios ni errores libres.

`RuntimeTelemetry(source_sha=<SHA completo de la versión instrumentada>, instance_id=<uuid4().hex nuevo por proceso>, started_at_ms=<UTC ms>)` identifica una instancia. **No reutilizar el SHA base 83075… para describir un binario nuevo instrumentado.** La revisión real del nuevo artefacto debe venir de metadata inmutable del build; no hay un lector de metadata confiable implementado en este contrato.

- `configure(flags, now_ms=...)`: conserva sólo booleanos exactos de `FLAGS`. Debe recibir una proyección de estos nombres, nunca `vars(settings)`. Configuración ausente permanece `null`. `TRADING_ENABLED` se muestra separado y no se combina para inventar permiso efectivo.
- `begin_component(name, now_ms=..., instantiated=None)`: nueva generación, sin afirmar existencia del engine. Pasar `instantiated=True` sólo después de que su constructor devuelva. Devuelve un token entero para los demás hooks. Los tokens antiguos no pueden actualizar una instancia reemplazada.
- `observe(name, generation, field_name, value, now_ms=...)`: campos permitidos `instantiated`, `task_running`, `tick_started`, `tick_returned`. `task_running` sólo corresponde a loops; en callbacks es `null/NOT_APPLICABLE`. `tick_started` sólo acepta `True`. `tick_returned=True` significa que la llamada regresó, **no que evaluó correctamente, produjo señal, llenó una orden o ganó dinero**. M8/M9 capturan errores internamente.
- `dependency(name, generation, dependency, available, now_ms=...)`: sólo dependencias fijas y booleanos explícitos. Instancia disponible no demuestra datos válidos. Un book sano debe conservar sus verificaciones existentes; este módulo no lo evalúa.
- `snapshot(now_ms=...)`: copia JSON independiente. No actualiza artificialmente observaciones al leer. Tareas caducan a 30 segundos; instancia, ticks y dependencias a 300 segundos. Caducidad significa desconocido, no detenido. Estos TTL son de presentación y **no sustituyen** los gates de salud financieros.
- `best_effort(hook, ...)`: captura únicamente fallos de instrumentación y no guarda el mensaje de excepción. Rodear sólo hooks de telemetría, nunca la llamada financiera. Las excepciones originales de motores deben conservar exactamente su propagación y manejo previo.

Esquema del bloque opcional `/status.motor_telemetry`:

```text
schema = runtime-motor-telemetry-v1
source = {commit_sha, instance_id, started_at_ms, sampled_at_ms}
configuration_at_ms = UTC ms | null
global_trading_configured = bool | null
motors[m1,m2,m3,m5,m6,rest,m8,m9] = {
  configured: bool|null,
  execution_entry_configured: bool|null,
  execution_sell_configured: bool|null,
  execution_implementation: PRESENT|ABSENT|UNKNOWN
}
components[capture,m1_loop,m2_poll,m2_exit,m3_loop,m5_loop,
           m6_observer,rest_observer,m8_observer,m9_observer] = {
  motor, task_model: loop|callback, generation,
  instantiated, task_running, tick_started, tick_returned,
  dependencies: {<dependencia fija>: observación}
}
observación = {value: bool|null, observed_at_ms: UTC ms|null,
              freshness: FRESH|UNKNOWN|STALE|CLOCK_REGRESSION|NOT_APPLICABLE}
```

`execution_implementation=ABSENT` para M6/M8/M9 es un hecho del código revisado. Sus campos de ejecución configurada permanecen `null`: no existen flags de ejecución que justifiquen `false`. En M2, `MOTOR_2_ENTRY_EXECUTION_ENABLED` corresponde a entradas y `MOTOR_2_EXECUTION_ENABLED` a ventas. M1 detección no equivale a compras; M3 sólo tiene el flag de ventas en este contrato. REST no tiene una tarea de detección independiente; sus tareas transitorias de ejecución no se muestran ni se observan.

Parser `parse_runtime_telemetry(block, now_ms=...)`: salida `{available, reason, source, configuration_at_ms, global_trading_configured, motors, components}`. Razones: `FRESH`, `NOT_INSTALLED`, `INVALID`, `STALE`. Rechaza esquema/tipos/miembros extra, SHA no completo, timestamp booleano, identidad inválida, generación contradictoria, flags inexistentes, tareas inventadas para callbacks, más de 65.536 caracteres JSON ASCII y estructuras excesivas (2048 nodos/12 niveles). Valida fechas y revalida cada TTL al leer. Edad máxima del bloque 30 segundos; tolerancia de reloj futuro 5 segundos. Cada resultado inválido borra todos los hechos derivados; no existe caché de ON anterior. No devuelve texto desconocido del servidor. La ausencia o invalidez del bloque no debe afectar la lectura legacy independiente.

## Hooks mínimos propuestos sobre las fuentes exactas

| Componente | Punto real observado | Hook necesario y significado |
|---|---|---|
| `capture` | `runner.py:136`, `data_capture.py:982` | Generación al crear el servicio; instancia sólo tras constructor. Guardar referencia a su task supervisora y observar `not task.done()`; no propagar ese valor a los motores pasajeros. |
| `m1_loop` | `runner.py:303-342`, `motor_1_arbitrage/engine.py:120-130` | Generación para `motor1_arb`; tarea supervisora independiente; instancia tras constructor; antes de `await self._tick()` observar inicio, al retornar observar retorno; dependencia `_v2_manager` explícita. |
| `m2_poll` | `runner.py:363-482`, `motor_2_consensus/poller.py:400-411` | Generación de `motor2_shadow` distinta de salidas; instancia `poller`; tick alrededor de `await self.poll_once()`; `odds_live` desde el objeto fuente, sin publicar clave ni regiones. |
| `m2_exit` | `runner.py:484-512` | Task `motor2_exits` propia. Constructor `Motor2ExitEngine` observado por separado. Pendiente copiar/verificar el `exit_engine.py` desplegado para el hook de tick exacto. |
| `m3_loop` | `runner.py:206-235`, `motor_3_clv/engine.py:99-108` | Task `motor3_clv`; hooks en `Motor3Engine._loop`, **no** en `Motor3PositionPoller.run`: el engine llama `sync_once()` y no arranca ese loop alternativo. |
| `m5_loop` | `runner.py:237-301`, `health.py:80-97` | Ya existen task y timestamps propios; adaptar esos hechos con edad sin sustituir los checks originales. La fuente completa del engine M5 no forma parte de este lote. |
| `m6_observer` | `runner.py:344-361`, `motor_2_consensus/poller.py:176-180` | Instancia tras `Motor6LineMoveShadow`; inicio/retorno alrededor de `self._linemove.observe(...)`; dependencias M2 poll y odds reales independientes. Sin task propia. |
| `rest_observer` | `data_capture.py:972-976`, `motor_rest_arb/engine.py:168` | Instancia después de `RestArbEngine(...)`; `ticker_handler=True` sólo después de registrar ese callback; inicio/retorno de `on_ticker`. No usar `capture_running` como actividad REST. |
| `m8_observer` | `data_capture.py:236-247,395-396`, `motor_8_ofi/shadow.py:73` | Instancia al terminar constructor; llamada `observe_delta` mide recepción efectiva, sin derivarla de conexión WS. `orderbook_v2` por referencia real, `delta_handler` por registro del handler. |
| `m9_observer` | `data_capture.py:251-265,402-403`, `motor_9_spillover/shadow.py:104` | Igual separación; hook sobre `observe`. Retorno no demuestra que había midpoint utilizable o señal; función puede regresar temprano o absorber excepción. |

No agregar un poll de mercado, consultas DB, inspección de reservas ni lecturas de órdenes para producir estas señales. Mantener referencias de tareas, generaciones y objetos ya existentes; al construir un `/status`, se pueden renovar probes de referencias exactas. Nunca renovar tick desde el endpoint: el tick se escribe únicamente donde se ejecuta la llamada. Nunca envolver sólo el log “arrancado” como prueba de tarea actual.

Ejemplo de patrón de hook, **propuesta, no instalado**:

```python
# En el mismo objeto ya construido; generation fue asignado para ESTA instancia.
best_effort(telemetry.observe, component, generation,
            "tick_started", True, now_ms=utc_ms())
try:
    await self._tick()  # llamada original; no se cambia su lógica ni su await
except BaseException:
    best_effort(telemetry.observe, component, generation,
                "tick_returned", False, now_ms=utc_ms())
    raise  # conserva CancelledError y el manejo externo existente
else:
    best_effort(telemetry.observe, component, generation,
                "tick_returned", True, now_ms=utc_ms())
```

## Parche aislado de exportación para revisión

El diff de abajo se genera contra el `health.py` copiado y sólo agrega el bloque opcional. **No es el parche completo de instrumentación.** Antes de aplicarlo en un checkout aislado autorizado: copiar el módulo probado a `src/monitoring/runtime_telemetry_contract.py`, revisar el build metadata y añadir los hooks de la tabla. El diff por sí solo deja `motor_telemetry=null`; eso es deliberado y el parser no inventa estados. No se aplicó el diff a ningún checkout del runtime ni a producción.

```diff
--- a/src/monitoring/health.py
+++ b/src/monitoring/health.py
@@ -22,6 +22,7 @@
 from loguru import logger
 from sqlmodel import select
 
+from src.monitoring.runtime_telemetry_contract import best_effort
 from src.storage.models import DailyPnL, RiskEvent, Trade, get_session
 from src.utils.config import get_settings
 
@@ -53,6 +54,7 @@
     Servicios externos (capture, strategies) actualizan este estado.
     """
 
+    motor_telemetry = None  # Optional in-memory registry; no implicit runtime facts.
     started_at: datetime = datetime.now(UTC)
     last_ws_message: datetime | None = None
     ws_connected: bool = False
@@ -401,6 +403,13 @@
     from src.risk.manager import RiskManager
 
     return {
+        "motor_telemetry": (
+            best_effort(
+                BotState.motor_telemetry.snapshot, now_ms=int(now.timestamp() * 1000)
+            )
+            if BotState.motor_telemetry is not None
+            else None
+        ),
         "bot": {
             "started_at": BotState.started_at.isoformat(),
             "uptime_hours": round((now - BotState.started_at).total_seconds() / 3600, 2),
```

## Validación y pasos restantes

- 22 pruebas locales del contrato/parser: estados desconocidos, configuración exacta, M2 entradas/ventas, caducidad, callbacks sin task, dependencias, generaciones, reinicio, concurrencia, copia independiente, ausencia legacy, fuente futura/vencida, estructura inválida/sensible y red bloqueada. Ruff 0.14.0 (`E,F,I`, ignorando sólo E501) sin hallazgos.
- La suite no prueba una integración instalada ni que los hooks correspondan en ejecución a cada task/instancia. Faltan pruebas del patch en un checkout aislado del runtime, con tareas falsas y callbacks inyectados, verificando que retornos/excepciones/cancelación originales quedan idénticos y que fallos de instrumentación no alteran el motor.
- Preparar metadata del SHA completo del **nuevo** build y nueva identidad de proceso; no usar variables secretas ni identificadores de cuenta. Copiar y verificar el fuente desplegado de salidas M2 y, si se cambia M5, su engine exacto.
- Añadir hooks con el alcance de la tabla y un test que confronte cada referencia task/objeto con su componente. No inferir actividad de flags, logs o conteos. Correr suite y lint del runtime en aislamiento.
- Revisar el diff completo. El despliegue/reinicio del runtime financiero queda pendiente de autorización específica y fuera de este trabajo. Después de un despliegue autorizado, verificar el bloque con GET, SHA/instancia/frescura y un reinicio controlado; recién entonces Telegram podrá presentar los hechos nuevos como observados.
- La integración opcional del receptor/panel la gestiona el agente principal en archivos separados; este entregable no los modifica. Sus tests y cualquier publicación del receptor deben reportarse por separado de los 22 tests aquí indicados.
