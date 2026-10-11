# Verificación US500 V2 — 11 de octubre de 2026

Base revisada por Claude: `0fd3bf74551ca2586add9fa915b54c8d555e073f`. Esta revisión mantiene la rama separada `codex/us500-paper-v1-20261011`, sin merge/deploy y sin modificar M2/M5 o el ejecutor de producción.

## Verificado localmente

- Python 3.14.8: **238 pruebas aprobadas**, con DNS y operaciones de socket bloqueadas; log en `evidence/V2/TESTS.log`. Corrida final sin ResourceWarning. Los transportes Telegram son falsos.
- Ruff 0.14.0 (`E,F,I`, salvo longitud E501): 0 hallazgos. Formato aislado verificado en **20 archivos Python**. Logs `RUFF.log`, `FORMAT.log`.
- Ambos recorridos completos dentro de `verify_offline.py`: continuidad/reinicio financiero y permiso de operador mediante mensajes/botones simulados.
- Resultado del caso sintético: USD100 → USD98.6643; fees USD0.1893, infraestructura USD0.010; posición y reservas finales cero; reinicio idéntico.
- Pausa del recorrido de botones con posición ficticia de 2 unidades: la DB financiera queda sin cambios mientras está deshabilitado. Finaliza con paper deshabilitado y sin exposición.
- Reservas previas, parciales, UNKNOWN y caída se ejercitan en tests específicos. El demo original de diez frames no contiene todos esos casos.

## Cambios y defectos reproducidos

1. Coste acumulado de posición en vez de reconstruir PnL desde un promedio redondeado. Regresiones long/short y parcial→variation margin→cierre. Claude confirmó independientemente el defecto en V1.
2. Variation margin explícito: mueve PnL a cash y rebasa coste; conserva equity al mismo mark, no rejuvenece mark y no se cobra dos veces al cerrar/reiniciar.
3. Tasas de margen por evento: validación, persistencia, pausa y reservas conservadoras; la configuración inicial permanece inmutable.
4. Capturas → sesión: identidad completa, antigüedad, no solapamiento, costes/funding explícitos, presupuesto GET durable, recuperación sin repetir solicitudes ambiguas.
5. Control Telegram paper: autorización exacta, confirmación única, pausa y bloqueo por evento. Se corrigió que omitir el control permitía continuar una base ya vinculada.
6. Transporte: se corrigieron IDs de callbacks válidos antes rechazados y un agotamiento del registro que podía dejar paper habilitado sin recibir pausa. Fallos de recepción/capacidad intentan pausa local; un fallo al guardarla queda `PAUSE_UNCONFIRMED`.
7. CI dedicado incluye manifiesto, lint/formato, suite y recorridos offline en Python 3.10–3.14. La ejecución remota y su SHA deben verificarse en GitHub; este archivo no los da por aprobados anticipadamente.

El runner fija revisión de modelo 2. Una DB V1 se rechaza antes de escribir: no se migra su coste o estado implícitamente. El ledger puede leer estado antiguo identificando coste derivado del promedio; eso no prueba recuperación exacta de precisión perdida. Conservar evidencia y exposición de cualquier base anterior.

## Observación conectada real, limitada

`evidence/V2/PUBLIC_COLLECTION_PLAN.json` acotó el ensayo a máximo 2 GET anónimos oficiales y cero órdenes. `PUBLIC_COLLECTION.json` registra **1 GET**, HTTP200 en `/v1/markets/KXUS500PERP`, mercado cerrado, `COLLECTION_MARKET_CLOSED` y pausa durable. No se consultó libro tras el cierre; quedó 1 GET sin consumir. No se reintentó ni se usaron claves.

La respuesta confirma el formato de la ficha (índice, versión, tick y unidades), no habilitación de cuenta, libros ejecutables, liquidez actual ni ventaja económica. No existen fills reales ni sesiones abiertas completas derivadas de este ensayo.

## Pendientes que esta entrega no cierra

- Revisión externa completa de Claude sobre el nuevo SHA.
- Datos de sesiones abiertas completas y evaluación independiente; estrategia sin ventaja demostrada.
- Permisos Perps, condiciones de cuenta y especificación contractual/económica vigente.
- Liquidación/margen reales del venue, conciliación de cuenta y ejecutor real.
- Integrar el menú en un bot Telegram dedicado o en el único router existente y verificar recepción en el teléfono. No se instaló un poller, servicio ni token.

`TELEGRAM_PAPER.md` deja los comandos y límites concretos. No se consumió Jev/Odds, no se cargaron claves de producción ni se alteraron flags reales. Los logs V1 son históricos; el manifiesto V2 contiene las huellas de esta revisión.
