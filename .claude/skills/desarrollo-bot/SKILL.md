---
name: desarrollo-bot
description: Las lecciones de desarrollo GRANDES del bot, destiladas de los incidentes que costaron plata real (jun-jul 2026) — verificar antes de implementar, nada sin tope, medir antes de construir, una sola matemática, presupuesto de APIs externas, detectado ≠ capturable. Usar al diseñar CUALQUIER feature, integración o motor nuevo: cada regla acá abajo tiene una factura que la respalda.
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
