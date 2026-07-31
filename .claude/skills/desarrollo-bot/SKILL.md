---
name: desarrollo-bot
description: Las lecciones de desarrollo GRANDES del bot, destiladas de los incidentes que costaron plata real (jun-jul 2026) — verificar antes de implementar, nada sin tope, medir antes de construir, una sola matemática, detectado ≠ capturable, presupuesto del hot path, "por construcción" exige assert, estado degradado con salida, verificación falsable, torniquete antes que cirugía. Usar al diseñar CUALQUIER feature, integración o motor nuevo: cada regla acá abajo tiene una factura que la respalda.
---

# Desarrollo del bot — las lecciones que costaron plata

Cada regla tiene su factura. No son estilo: son incidentes.

## 1. Verificar antes de implementar (la regla madre)

Los briefs llegan equivocados con frecuencia DOCUMENTADA: el "796% consumido" era un bug
del dashboard (dos matemáticas), no del freno; el "bug de ejecución" de rest_arb era
inejecutabilidad estructural (la query de `leg_states` lo probó ANTES de escribir el diff
inútil); el PR #55 "solo necesitaba rebase" y era 100% redundante con main. **Leer el
código y los datos reales primero; el diff se escribe después del discriminador, no antes.**
Dos agentes pueden tener razón sobre momentos distintos — reconciliar por línea de tiempo.

## 2. Nada sin tope (tres facturas: disco, RAM, red)

- `orderbook_events` sin retención → **57GB de disco**, bot caído.
- `_bootstrap_buffer` sin maxlen → **OOM crash-loop cada 75min** (container 1GB al 99.99%).
- `get_snapshot` de 223 tickers en UNA request → el WS la dropea entera → circuit breaker.
- `get_odds` sin caché + burst 60s + 2 regiones → **20k créditos quemados en días**.

Toda estructura que crece (buffer, caché, pendings, deque, tabla) nace con tope + descarte
documentado de qué se pierde al toparse. Toda request se dimensiona (chunking). Toda
integración externa nace con: **caché TTL + breaker de cuota + costo por unidad documentado
en el Field** (regiones ×créditos, cadencia ×créditos). El breaker/caché de un cliente que
se recrea por ciclo va en estado de CLASE, no de instancia.

## 3. Medir antes de construir (shadow auto-validante primero)

M2 ejecutó por intuición: **−$433** (edge real 0.15pp vs umbral 3pp — jamás lo tuvo).
M8/M9 midieron primero: **~$0** y a las semanas dan veredicto con t-stat. El patrón:
detector PURO + shadow que captura la referencia AL instante de la señal y mide el
resultado DESPUÉS (EdgeWindow kind propio) + gate F2 escrito de antemano + "archivar es
resultado válido". **Un motor que ejecuta sin edge medido es una donación al mercado.**

## 4. Detectado ≠ capturable

rest_arb detectaba 3.13pp de edge y realizó **7.7% win-rate** (73% rollback): el FOK no
llena 3-4 patas atómicas en books finos. El follow de M9 se mide en el mid pero se ejecuta
al ask. **Todo F2→F3 exige validar contra el precio EJECUTABLE (ask + fee real a count
real), no contra el detectado.** A veces no hay fix: el edge existe y es incapturable —
archivar también ahí.

## 5. Una sola matemática (freno ↔ observabilidad)

El dashboard recalculaba stop-losses con ventanas rolling sin pisos → "796% consumido" de
un freno sano → el operador persiguió un fantasma. **El check que decide y el status que
muestra salen del MISMO snapshot/función.** Dos implementaciones del mismo número siempre
divergen.

## 6. CI verde + el call site pineado

El merge de #155 con CI rojo costó **tres regresiones** (fee flag, burst, Motor 6 — las dos
últimas invisibles por SEMANAS: env vars inertes). Reglas: jamás mergear con CI rojo; todo
kwarg que el runner threadea a un componente lleva un test que pinea el CALL SITE (no solo
el componente); todo `settings.X` referenciado debe existir en Settings (test sistémico
`test_settings_wiring`).

## 7. Fail-safe direccional (a dónde caer cuando algo falla)

LECTURA falla abierta (un hiccup no apaga el bot). VENTA/CIERRE falla cerrada (ante la
duda, no vender). Book corrupto falla cerrado (cuarentena: mejor ciego que fantasma).
Datos stale NO se sirven (caché vencida durante cuota agotada → [] — un fair sobre cuotas
de hace horas es peor que no tener fair). Telemetría se descarta antes que el trading.

## 8. La decisión de negocio no es un diff

Apagar M2, activar el rolling stop, subir el límite del container: cambios de estado en
producción con dinero real → **decisión y ejecución del operador**, con los riesgos
repetidos aunque ya se hayan hablado (la lección 2026-07-07: proceder "como rutina" con
bugs conocidos costó ~$140). El código deja todo listo detrás de flags default-off.

## 9. El hot path tiene presupuesto (frecuencia × costo ANTES de commitear)

El handler del WS corre a ~200 msg/s. Un `logger.info` por snapshot ahí = **400MB/día**
de log rotando cada 40min, y el sink SINCRÓNICO bloqueaba el event loop en cada línea —
el logging alimentaba los gaps que generaban los snapshots. Y `_start_recovery` sin
debounce re-bootstrapeaba ~200 tickers POR GAP: **5 bootstraps/seg, 38.365 recoveries
en 9h, books jamás inicializados** (espiral 2026-07-31). La regla: para cualquier línea
dentro de un loop de alta frecuencia, escribir la multiplicación (frecuencia × costo)
antes de commitearla; todo lo que dispara I/O desde un hot path nace con rate-limit/
debounce/nivel DEBUG. Es la hermana de la regla 2: aquella acota el ESPACIO, esta la
FRECUENCIA.

## 10. "Por construcción" sin assert es una creencia, no una garantía

`executor.py` documentaba que el pre-check de depth "sería un no-op (count =
min(available_size) ya, por construcción)". El comentario es VERDADERO sobre el book
local — y ahí está el bug: el book local no es la fuente de verdad, y la carrera de
33ms del FOK vivía exactamente en esa distancia. La revalidación T-0 heredó el mismo
error de fondo (re-leer la MISMA copia local: 2 rollbacks con 0 `revalidation_skip`
lo probaron en producción). Al escribir "por construcción"/"ya garantizado": ¿está el
assert al lado? ¿cuál es la fuente de verdad de este dato, y mi chequeo LA consulta o
consulta mi copia? Revalidar contra tu propia memoria no rompe la cadena causal.

## 11. Estado degradado: la entrada y la SALIDA en el mismo PR

El circuit breaker de M1 ponía `is_paused=true` y el único camino de vuelta era un
endpoint manual: **12 horas muerto por $0.21** de rollbacks limpios. Ninguna transición
a modo degradado (pausa, disable, cuarentena) se mergea sin su condición de salida
definida y testeada en el MISMO PR: auto-recuperación acotada, backoff con reintento,
o "manual" EXPLÍCITO con la alerta que lo diga. Y clasificar el evento antes de
contarlo: un rollback limpio de 2¢ es costo operativo, una huérfana es riesgo — meter
ambos en el mismo contador hizo al breaker hipersensible primero e inerte después.

## 12. Verificación falsable (métricas Y comandos)

Toda métrica nueva nace con una frase escrita: **"sube si y solo si pasa X"** — si no
podés escribirla, no sirve para decidir (`recoveries_suppressed_total` nació como
"discriminador de gaps" y suma tres paths distintos; hoy NINGÚN contador solo mide los
gaps crudos — ver diagnostics-recovery). Y todo comando de verificación en un PR o
reporte lleva sus DOS salidas: qué resultado CONFIRMA y qué resultado REFUTA — un grep
garantizado a devolver cero (`revalidation_skip` con 0 trades) no verifica nada. El
formato que ya funcionó: "skips > 0 con rollbacks ≈ 0 = causa raíz muerta".

## 13. Torniquete antes que cirugía

Con producción sangrando, primero la mitigación que NO requiere merge (env var
existente, flag, nivel de log) y después el fix. El log de 400MB/día tenía torniquete
inmediato (`LOG_FILE_LEVEL=WARNING`, un env flip que ya existía) y en su lugar esperó
un PR + deploy completo. Orden en incidente: torniquete → forense → cirugía. La
aplicación es del operador con lista literal `NOMBRE=valor` (Workflow 4 de botkalshi).

## 14. La deuda #1: banco de pruebas de secuencias (replay)

Hoy el único entorno de test del hot path es producción de madrugada: cada fix del
manager V2 se valida con fuego real porque no hay dónde reproducir una tormenta. OJO
con la premisa fácil "el stream ya está en los logs": **falso** — INFO trae resumen por
snapshot (ticker/seq/3 niveles), los payloads completos son DEBUG (default INFO desde
#190), los deltas no se loguean y `orderbook_events` está OFF por default. Lo que SÍ se
reconstruye de un log INFO es el CONTROL-FLOW (secuencia sid/seq/gaps/recoveries con
timestamps): suficiente para replay de la mecánica de recovery con books sintéticos.
El harness completo pide dos piezas: captura ACOTADA de envelopes crudos detrás de un
flag (respetando la regla 2 y DiskGuard) + reconstructor log→secuencia. Hasta que
exista, todo fix de hot path declara en su PR cómo se validó y qué NO pudo validarse
sin producción.
