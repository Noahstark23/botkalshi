# Evaluación independiente US500 — protocolo local v1

Fecha: 11 de octubre de 2026. Revisión inicial de código: `80109bc10eba7abc099d1effe0c66790d4f22634`. Este protocolo describe una evaluación offline y prospectiva del simulador financiero. No autoriza red, órdenes, activación, despliegue ni cambios en el bot deportivo. Los resultados corresponden al modelo paper y a sus supuestos; no acreditan ejecución, condiciones de cuenta o ventaja del venue.

## Estado de la evidencia

El caso de `example_session.py` es sintético: diez capturas inventadas a lo largo de nueve segundos. Su resultado histórico USD100 → USD98.6643 sirve como regresión contable, no como muestra de mercado. Las fixtures que declaran `synthetic=false` en tests también contienen datos inventados, como explica `tests/test_us500_session.py`.

La captura pública documentada en `VERIFICACION_V2.md` obtuvo mercado cerrado: un GET HTTP200 y pausa, sin libro posterior. No constituye una sesión abierta. Trades, velas y funding históricos, aunque una consulta agote su paginación, no reconstruyen por sí solos la profundidad y los marks necesarios para el runner.

La revisión independiente focal ejecutó 92 pruebas de `test_us500_ledger`, `test_us500_runner` y `test_us500_session`, con sockets bloqueados. Esto verifica comportamiento local; no añade observaciones ni muestras económicas. No se consultaron cuentas, bases históricas o red durante esta revisión.

## 1. Congelar la evaluación antes de recibir sus resultados

Identificador del protocolo: `us500-independent-evaluation-v1`. Estas reglas se mantienen para toda una evaluación. Una modificación genera otra versión y otra evaluación; no se reinterpreta retroactivamente un resultado.

Antes de incorporar una nueva muestra, guardar un manifiesto con:

- `evaluation_id`, versión y hash de este protocolo, SHA del código, revisión del modelo, versiones de Python y dependencias.
- Configuración exacta y hash de `momentum-v1`, ledger, fees, slippage, margen, unidad, tick y política de funding/costes. No ajustar esos parámetros después de mirar el conjunto reservado para evaluación.
- Lista de datasets ya inspeccionados o usados para desarrollo. El ejemplo y todas las fixtures existentes pertenecen a desarrollo; sus resultados no se reclasifican como independientes.
- Política explícita para formar sesiones, ventana horaria y zona, rango solicitado, cadencia pretendida, gaps máximos y condiciones de inclusión/exclusión. Se fijan antes de recolectar; no se filtran luego los días desfavorables.
- Separación cronológica entre desarrollo y evaluación. No repartir aleatoriamente frames o fills vecinos entre conjuntos. El umbral de separación y cualquier período de exclusión se justifican según la dependencia temporal de la estrategia y se registran, no se optimizan con el resultado.
- Unidad de análisis económica elegida, comparador, método de incertidumbre y regla de finalización de la recolección. Esta entrega no fija un número universal de sesiones ni una rentabilidad mínima: faltan observaciones para justificarlo.
- Procedencia de cada supuesto económico y qué evidencia permitiría sustituirlo. Un hash fija contenido; no certifica quién lo produjo ni que su origen sea auténtico.

Cambiar una hipótesis tras observar sus resultados es investigación adicional. Sus resultados anteriores quedan conservados; la hipótesis revisada requiere otra muestra posterior reservada.

## 2. Puerta de procedencia y cobertura

Conservar capturas completas con cuerpos, hashes, URLs oficiales permitidas y tiempos de solicitud/recepción. Recalcular integridad desde las entradas; no confiar en un campo `analysis` guardado. Separar siempre rango solicitado, rango recibido y rango realmente utilizable.

| Comprobación | Resultado exigido para usar la sesión |
|---|---|
| Procedencia | Distinguir `synthetic`, `observed_unverified_origin` y declaración ausente/conflictiva. No mezclar clases dentro de una sesión. `source_authenticity_verified` permanece falso. |
| Identidad | Entorno, ticker, market ID, versión, exchange, clase de activo, tamaño, multiplicador, tick y fracciones consistentes en todas las capturas. Validar también al invocar el runner directamente. |
| Integridad | Hashes recalculados de ambos cuerpos y metadata consistente con el manifiesto. Una contradicción bloquea, aunque se haya regenerado un hash individual. |
| Tiempo | Capturas crecientes y no superpuestas; mark de liquidación presente y vigente; gaps y antigüedad explícitos. No ordenar, interpolar o rellenar silenciosamente. |
| Liquidez | Libro completo requerido por el modelo, lados válidos, profundidad observada y cantidades representables. Una vela o un trade no sustituye el libro. |
| Costes | Funding y costes explícitos por intervalo, tiempos y procedencia. Los valores ejecutados deben coincidir con `session_interval_cost_evidence`; cero es un supuesto registrado, no ausencia de dato. |
| Cobertura | Reportar capturas pedidas, recibidas, admitidas, bloqueadas, rango y duración observados, mayor gap y motivo de exclusión. No inferir cobertura total de un HTTP200 o cursor agotado. |

Cada exclusión conserva ID, hash y motivo; no desaparece del denominador. Un cierre de mercado, ausencia de actividad o dato faltante son estados distintos. Si falta calendario vigente, la clasificación de la causa del gap permanece desconocida.

Una sesión estructuralmente utilizable todavía puede ser insuficiente para evaluar una estrategia. Capturas distintas no garantizan sesiones económicamente independientes; sesiones con ventanas o posiciones solapadas deben identificarse como relacionadas.

## 3. Puerta de contabilidad y modelo

Ejecutar el replay con base nueva aislada y configuración congelada. Repetirlo y extender un prefijo debe conservar exactamente dinero, exposición y eventos, sin duplicar fees, funding o settlement. Guardar hashes de entrada y reporte, revisión del modelo y código usado.

La comprobación económica independiente usa los eventos suministrados y aritmética decimal para reconciliar:

```text
cash = initial_cash + realized_pnl - fees + signed_funding - infrastructure_costs
equity = cash + unrealized_pnl
net_pnl = equity - initial_cash
notional = abs(signed_api_quantity) * risk_mark_price
```

`variation_margin_usd` ya forma parte de `realized_pnl_usd`: no se suma de nuevo. Un settlement mueve PnL latente a cash y rebasa coste, conservando equity al mismo mark. Las reservas restringen margen disponible; no son una pérdida ni un gasto. Verificar largos, cortos, parciales, cierres y reversal; cerrar primero no autoriza simular apertura contraria en el mismo instante.

El informe económico debe conservar por separado PnL realizado, latente, fees, funding, infraestructura, exposición residual y reservas. El PnL ya incorpora precios hipotéticos adversos; no descontar por segunda vez un spread/slippage informativo. Margen y liquidación siguen siendo hipótesis locales: breach pausa, sin fabricar una liquidación o un cierre final.

El funding histórico es una tasa, mientras que el runner consume flujo USD firmado por intervalo. La conversión requiere cantidad expuesta, mark de settlement y secuencia temporal explícitos; una tasa no prueba el pago de una cuenta. Los marks observados tampoco prueban margen o condiciones económicas aplicables a esa cuenta.

La ejecución usa una captura posterior a la señal, profundidad visible y coste adverso configurado. Todo fill permanece hipotético: no demuestra prioridad, cola, latencia, ausencia de impacto ni ejecución futura. Una sesión incompleta o pausada conserva su resultado parcial, pero no se promociona a evaluación económica completa.

## 4. Evaluación económica sin multiplicar artificialmente la muestra

Presentar primero resultados de cada sesión admitida y de todas las sesiones excluidas. Agregar por la unidad preregistrada; no contar cada frame, fill parcial o settlement como observación independiente. Un round trip tampoco garantiza independencia respecto del siguiente. El campo actual `independent_settled_samples=0` no se incrementa por pasar tests o cerrar posiciones sintéticas.

Comparar con permanecer sin exposición y con costes de infraestructura definidos explícitamente para ese comparador. Cualquier comparador adicional debe quedar fijado antes de la evaluación, usar sólo información disponible hasta cada instante y llevar costes y riesgo equivalentes. No seleccionar después el comparador que deje mejor al motor.

Reportar PnL y costes en USD, retorno relativo al capital inicial **ficticio** del escenario, exposición y drawdown sobre observaciones disponibles. El drawdown muestreado no revela el máximo intraintervalo. No anualizar nueve segundos ni extrapolar ganancias de fixtures a diciembre.

Para intervalos de incertidumbre o pruebas estadísticas, justificar primero la unidad y dependencia temporal. No calcular un t-stat tratando fills correlacionados como muestras independientes. Si se exploran varios parámetros, ventanas o hipótesis, declarar todos los intentos y reservar una nueva evaluación; el mejor resultado exploratorio no es validación.

La conclusión puede ser falta de datos, resultado inconcluso o ausencia de ventaja bajo el modelo. Incluso un resultado paper positivo fuera de muestra no demuestra fills reales, condiciones de cuenta, ausencia de riesgo o preparación para operar. No existe aprobación automática por beneficio positivo o por un t-stat aislado.

## 5. Reporte separado de datos y resultados

El reporte de la base revisada conserva hashes, contabilidad, exposición y límites, pero no exponía la procedencia de los frames. El cambio local que acompaña esta revisión añade `input_provenance`: conteos de frames declarados sintéticos/observados, frames solicitados/procesados, tiempos extremos y verificación de evidencia ensamblada. Mantiene falsas autenticidad, cobertura de sesión completa e independencia. Un frame procesado puede estar bloqueado: ese conteo no equivale a frames utilizables.

La siguiente tabla amplía el contrato propuesto para una evaluación futura. El bloque parcial anterior no implementa todavía todos estos campos ni constituye un auditor completo de cobertura:

| Bloque propuesto | Campos o valores mínimos |
|---|---|
| Identidad de evaluación | `evaluation_id`, `protocol_sha256`, `code_sha`, `model_revision`, hashes de dataset/estrategia/configuración. |
| Procedencia | `provenance_class`, `declared_synthetic`, `source_authenticity_verified=false`, `environment`, `ticker`, `instrument_metadata_sha256`. |
| Cobertura | `requested_range_ms` o `null` si no fue declarado; `observed_range_ms`, `usable_range_ms`, `frame_count`, `usable_frame_count`, `blocked_frame_count`, `max_observed_gap_ms`, lista de exclusiones con motivo. |
| Independencia | `partition` (`development`/`evaluation`/`unassigned`), `evaluation_unit`, IDs de sesiones relacionadas, `independence_assessment` (`not_assessed`/`documented_assumption`), sin afirmar independencia por simple conteo. |
| Costes y economía | Hash de evidencia de costes, condición `explicit_scenario_inputs`, conciliación de cash/equity, PnL neto, subtotales, reservas, residual, baseline preregistrado. |
| Alcance y conclusión | `economic_path_complete_under_explicit_assumptions`, `actual_orders=0`, `actual_fills=0`, `venue_liquidation_replicated=false`, `strategy_validated=false`, `readiness_claim=none`, motivos de insuficiencia. |

La procedencia debe derivarse de los frames validados, no de una etiqueta libre del informe. No confundir un hash que cambia con una prueba de origen. Mantener campos desconocidos en `null`/`not_assessed`, en vez de convertirlos en cero o verdadero.

## 6. Pendiente local prioritario y aceptación

La prioridad inmediata de esta revisión fue cerrar la validación de identidad y evidencia económica al entrar directamente al runner. El cambio local añade esas comprobaciones; su cierre técnico exige pruebas integradas y el SHA final. El siguiente pendiente local es producir un reporte offline separado de procedencia/cobertura con el contrato anterior. No requiere datos privados ni operación real.

Casos de aceptación: metadata que cambia con hashes válidos; contradicción entre coste del frame y evidencia del intervalo; origen sintético contra observado declarado; captura repetida o solapada; gap; muestra vacía/bloqueada; replay repetido; exposición residual; intento de reclasificar desarrollo como evaluación. Los casos inventados siguen rotulados como tests, incluyendo los que usan forma de datos observados.

Lo externo que sigue abierto es obtener sesiones abiertas completas con procedencia documentada, fijar un diseño de muestra defendible y ejecutar la evaluación preregistrada. La autorización de Perps, condiciones de cuenta, conciliación privada y transmisión real pertenecen a otro alcance. Este documento no los da por resueltos ni los necesita para mejorar el simulador.
