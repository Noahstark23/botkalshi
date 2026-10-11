# US500 V2.1 — correcciones y Telegram local

Corte: 11 de octubre de 2026. Base de revisión c97e850. No es una entrega de producción.

## Hallazgos corregidos

- HTTP incompleto/mal formado: `HTTPException` se convierte en un código estable. History conserva páginas previas y exporta el parcial con salida 2. Session registra pausa inmediata, conserva respuestas/presupuesto y no reenvía al reiniciar. Regresiones para IncompleteRead, BadStatusLine y LineTooLong en apertura y lectura.
- Tipos y hashes JSON: Decimal numérico permanece numérico y exacto al guardar/cargar; ya no se transforma en texto válido financiero. Números y cadenas producen hashes distintos. Valores no JSON, claves no textuales, ciclos y no finitos se rechazan antes de crear evidencia. Cinco bodies públicos archivados conservaron sus hashes.
- Supervisor Telegram local: exclusión por carpeta/control/bot; ciclos acotados; pausa al iniciar, finalizar o fallar; estado atómico y errores sin credenciales. No instala servicios, inicia estrategias ni contiene acceso a Kalshi.
- Documentación: investigación v0.1 marcada histórica; política de target=0 por spread excesivo descrita. La captura posterior valida tiempos y hashes, no autenticidad de la fuente.

## Pruebas

`verify_offline.py`: 267 tests y dos recorridos completos, con red bloqueada. Ruff0.14.0 y formato verificados. Evidencia en `evidence/V2_1/`; los logs V1/V2 permanecen históricos. El resultado sintético sigue USD100 → USD98.6643, sin posición/reservas al finalizar y reinicio idéntico.

## Conexión Telegram observada

Se reutilizó el bot privado ya configurado, después de verificar identidad, ausencia de webhook y ausencia de receptor en los servicios revisados. Su uso por otro componente es sólo envío de avisos, que continuó entregándolos durante la prueba. No se modificó ese componente ni producción. La credencial se leyó desde su configuración existente sin copiarla a código, argumentos, logs o evidencias.

Se observaron en Telegram `/us500`, menú, confirmación de activación paper, pausa, reanudación confirmada y estado final pausado. Esos callbacks reales controlaron una DB local: un ensayo financiero **sintético** abrió posición ficticia2, se bloqueó al pausar sin cambiar su DB, completó al reanudar y produjo el mismo resultado al reiniciar. Posición final0, reserva0, órdenes reales0. Resumen saneado en `evidence/V2_1/TELEGRAM_CONNECTED_SUMMARY.json`; IDs privados, bases y detalle operativo permanecen fuera del repositorio.

El receptor local se acotó a ocho horas desde su inicio y máximo10.000 ciclos; requiere el Mac disponible. Los botones sólo cambian el permiso de US500 paper. No hay estrategia autónoma iniciada ni datos actuales conectados por ese permiso. M2/M5 y trading real quedan fuera.

## Fuentes archivadas

Se incorporaron cinco documentos públicos primarios con URL/fecha/hash y derivados de lectura. OpenAPI coincide con el hash previo. El sexto intento, tarifario, recibió HTTP429 y se conserva como diagnóstico; las tasas previamente mencionadas no se validan por esa respuesta. Ver FUENTES_ARCHIVADAS.md. No se consultó un endpoint de mercado ni cuenta para este archivo documental.

## Revisión independiente y pendientes

Claude reprodujo 238 tests de V2 con red bloqueada y confirmó los dos defectos anteriores. Retiró su sospecha sobre residuo de orden1e-77 al verificar que es transitorio, desaparece al cierre total y no altera equity. No declaró terminada la revisión V2. Alcanzó su límite de uso; la aplicación indica reanudación automática a las16:41 America/Los_Angeles. La corrección V2.1 aún requiere su contraste independiente.

Siguen pendientes sesiones abiertas completas, datos reales para parsers históricos, especificación/permisos/fees de cuenta Perps, tarifario accesible y enmiendas contractuales, evaluación fuera de muestra, ejecutor y conciliación/liquidación reales. No se consultó la cuenta ni se consumió Odds/Jev. El modelo de margen es hipotético y no demuestra rentabilidad.
