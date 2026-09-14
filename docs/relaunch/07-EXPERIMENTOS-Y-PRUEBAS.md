# 07. Experimentos y validación

## M5 existente: preservar la cohorte

La métrica actual `f1-v2-bbo-depth` usa cotización bilateral, profundidad suficiente, referencia M2 con procedencia/edad y política de fees observada. No reimplementar ni desactivar esos controles. Conservar metric_version, fair_method_version, experiment_id, policy_hash y cadena de runs clean/invalid.

El gate existente exige como mínimo 500 fills hipotéticos, 100 eventos y 30 días, límite inferior de bootstrap 95% positivo, mediana diaria positiva, concentración por evento <=20% de fills y cero violaciones. Son condiciones del proyecto, no una prueba universal de ventaja. La revisión a 30 días del inicio confirmado no obliga a operar ni eleva capital.

La medición `markout futuro - fee de entrada por contrato` no es P&L realizable. No sumar de nuevo el spread. Deben estudiarse precio de salida ejecutable, profundidad, costos de cierre, inventario sin resolver, selección adversa y señales censuradas. Un cruce de precio observado en snapshots no prueba un fill propio: faltan cola, latencia y secuencia entre observaciones.

## Diseño adicional

Congelar hipótesis, parámetros, universo, semillas y criterio antes de nuevos datos. Separar entrenamiento/desarrollo de evaluación temporal posterior. Bootstrap agrupado por evento/día cuando proceda; no tratar contratos correlacionados como muestras independientes. Reportar multiplicidad y todas las hipótesis probadas, no elegir retrospectivamente la que aprobó.

Usar escenarios de fills conservador, intermedio y optimista claramente diferenciados; incertidumbre de cola no resuelta no se etiqueta ejecución real. Comparar costos/slippage/latencia adversos y caída de fuentes. Si la cota económica favorable ya es negativa, detener esa hipótesis: no rescatarla cambiando reglas o doblando capital.

Si una cohorte existente válida alcanza evidencia suficiente, analizarla antes de pagar por repetir captura. Si no existe snapshot accesible, demostrar el evaluador con fixture sintética y declarar no evaluable el resultado histórico. No crear una DB vacía llamada producción.

## Matriz mínima de regresiones

| Grupo | Aceptación |
|---|---|
| K01 | Fills 0.01 y 5.50 conservan precisión, precio/fees; cantidades de orden y fills reconciliadas |
| K02 | Cancel fallida/timeout mantiene UNKNOWN y reserva; fill en carrera se contabiliza |
| K03 | Error de listado no borra cuarentena ni habilita propuestas |
| K04 | Pausa, límites diario/semanal/experimental y balance desconocido bloquean toda apertura |
| K05 | Abrir NO desde ask YES calcula collateral correcto; reducción de YES es distinta |
| K06 | Excepción/SIGTERM conservan intención y estado; limpieza mientras transporte utilizable |
| K07 | Trading off bloquea también sell que abre exposición; reduce_only validado separadamente |
| K08 | Cada endpoint V2 y cuerpo/respuesta se validan contra contrato externo, no solo mocks propios |
| Paginación | Más de 200 órdenes, deduplicación, fills parciales y órdenes antiguas no desaparecen |
| Carrera | Dos propuestas simultáneas no gastan el mismo headroom |
| Tiempo | Medianoche LA, cambio DST, quote vencida, timestamp futuro, reloj desalineado |
| Datos | Orden de mensajes/gaps, libro cruzado, eventos mal emparejados y cambio de rules_hash |
| IA | JSON inválido, evidencia inventada, prompt injection, timeout y presupuesto agotado |
| Infra | Sin red en offline, sin escrituras en shadow, reinicio, disk-full y restore completo |

Las reproducciones históricas prueban defectos, no correcciones: transformarlas en tests de comportamiento esperado. Tests de dinero deben fallar cuando se reintroduce el bug; añadir mutation testing acotado. No hacer mypy bloqueante sobre miles de errores sin baseline: nuevos módulos estrictos primero y reducción de deuda registrada.

## Puertas

G0: evidencia/configuración/seguridad de lectura. G1: captura y replay reproducibles sin órdenes. G2: evaluación económica fuera de muestra y sensibilidad. G3: K01–K08 y mecánica demo verificadas con autorización específica. G4: revisión independiente y decisión separada sobre cualquier dinero real; **no autorizada por este plan**.

Ningún PASS estadístico activa otro modo. Demo demuestra mecánica, no liquidez ni rentabilidad de producción.
