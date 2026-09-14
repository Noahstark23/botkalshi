# 10. CV, aprendizaje y demostración

## Historia profesional defendible

Proyecto de backend asíncrono para investigación de mercados de eventos, con REST/WebSocket, persistencia, procedencia de datos, simulación, pruebas y controles de riesgo. La colaboración con IA se declara; no se presenta el sistema como una estrategia rentable o gestor autorizado.

Texto sugerido, ajustándolo solo a lo que el autor pueda demostrar:

> Desarrollo y mantenimiento de un backend Python de investigación de mercados de eventos con ingestión REST/WebSocket, SQLite, medición de estrategias y pruebas automatizadas. Trabajo en endurecimiento de seguridad, conciliación de órdenes/fills y evaluación reproducible de simulaciones.

Una vez implementados, y no antes, añadir: exportaciones con JSON Schema, integración IA de solo lectura, reproducibilidad con lock/contenedores y recuperación verificada. No anunciar los 1.675 tests como ejecutados personalmente hoy ni afirmar 100% de cobertura por número de archivos.

## Demo reproducible propuesta

Una fixture sintética con dos fuentes discrepantes y una cotización vencida entra en el pipeline. El sistema muestra procedencia/edad, rechaza el precio vencido, calcula costos sin floats y emite WAIT. Un escenario de cancelación UNKNOWN demuestra que la reserva no se libera; otro conserva un fill fraccionario. Reiniciar y reproducir da las mismas observaciones y hashes. Mostrar qué depende de datos reales y qué es simulado.

No enseñar tokens, terminal con entorno completo, cuentas o movimientos personales. Usar nombres y tickers sintéticos explícitos, no un partido inventado presentado como real.

## Preguntas de entrevista que hay que poder responder

Por qué SQLite y un escritor; cómo manejar gaps WS y backpressure; por qué un timeout de orden no autoriza un retry ciego; diferencia entre ask YES y costo de apertura NO; cómo se guardan 5.50 contratos; por qué markout no equivale a P&L; cómo probar fail-closed; qué aporta una IA sin darle claves; por qué una suite verde dejó pasar defectos; cómo revertir migraciones sin perder ledger.

## Escala de aprendizaje

Primero explicar un componente y reproducir un bug. Después escribir su regresión y reparación. Luego demostrar recuperación, métricas y trade-offs. Guardar ADRs, PRs y postmortems breves. El título senior se defiende con estas capacidades, no con cantidad de líneas ni con el tamaño de una racha.

El proyecto puede ser valioso para CV y entretenimiento aunque la hipótesis económica termine rechazada. Mantener hitos pequeños y horas de trabajo delimitadas para que aprender no se convierta en sostener infraestructura costosa sin propósito.
