# Incidente del receptor local y diagnóstico seguro

11 de octubre de 2026. Base del cambio: `a2fdc17d75e01653d78103e94e941f44b633403e`.

## Hechos observados

El seguimiento de las 20:44 UTC encontró el receptor local detenido. Su último estado, a las 20:23:30 UTC, indicaba `FAILED / POLL_CYCLE_STOPPED`, 45 ciclos, pausa confirmada y permiso de simulación deshabilitado. El proceso anterior ya no existía. Ambas SQLite pasaron `quick_check`; las 26 respuestas de la outbox figuraban `SENT`, sin pendientes, intentos ambiguos ni confirmaciones por consumir.

El supervisor convertía cualquier resultado STOPPED en `POLL_CYCLE_STOPPED` y descartaba `result.error`. Por tanto **la causa concreta del incidente histórico no quedó registrada**. No se atribuye a red, credencial, API ni otra causa a partir de ese código genérico.

## Corrección

`us500_telegram_service.py` incorpora el campo aditivo `last_poll_error`. Guarda exclusivamente códigos de una lista cerrada mediante el saneador del transporte. Texto desconocido, valores no textuales y estructuras inválidas se convierten en códigos fijos; no se llama a `str`, `repr` o comparaciones de objetos externos para generar el diagnóstico. Las excepciones del poll conservan sólo `POLL_CYCLE_FAILED`.

El campo vale `null` cuando no se observó un fallo del poll durante esa ejecución, y se reinicia al comenzar otra. No reconstruye ni hereda una supuesta causa de ejecuciones anteriores. Los estados antiguos sin ese campo siguen siendo evidencia válida de su versión, con causa no registrada.

Se mantienen pausa durable, cierre, un intento y ausencia de reintentos. No cambia estrategia, capital, ledger, cursor, outbox, autenticación, destino ni duración del receptor. El supervisor no lanza el runner.

## Verificación y estado operativo

282 pruebas del módulo y dos recorridos sintéticos aprobados con red/DNS bloqueados. Ruff0.14.0 limpio y formato válido en24 archivos Python. Revisión focal del agente:42 pruebas de transporte/supervisor, con sockets bloqueados y ResourceWarning tratado como error. Las nuevas regresiones cubren timeout, errores sensibles/desconocidos, valores no textuales y objetos hostiles, persistencia, pausa, cierre, ausencia de retry y reinicio del diagnóstico.

**No se restableció el receptor ni se inició otro consumidor `getUpdates` en este seguimiento.** El menú fijado sigue siendo visible; un receptor detenido no responde a los botones. Permiso US500 permanece deshabilitado. Una futura recuperación deberá conservar identidad/control/cursor/outbox y el límite original de las21:04PDT, verificar exclusividad y entregar una respuesta visible antes de declarar servicio restaurado. Esta corrección diagnóstica por sí sola no prueba que la causa de la caída se haya resuelto.

Sin acceso a producción, credenciales financieras, consultas de mercado, operaciones reales, merge o deploy. Claude permanece pendiente de la ventana de cuota ya documentada; este resultado no es su aprobación final.
