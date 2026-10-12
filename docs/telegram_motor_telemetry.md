# Telemetría individual antes de conectar controles Telegram

El candidato implementa observación de los componentes del runtime para el contrato
`runtime-motor-telemetry-v1`, ya entendido por el panel US500 22522c3. La instalación del
runtime sigue separada de publicar este código. No cambia flags, riesgo, órdenes, cuenta,
pausas, settlement, ni los endpoints administrativos existentes. No instala un consumidor
Telegram ni habilita comandos: autenticación, propuesta/confirmación, deduplicación durable,
canal de comandos y prueba conectada siguen pendientes del objetivo completo.

## Fuente y significado

`SOURCE_COMMIT` es una declaración del artefacto instalado: 40 caracteres hexadecimales
minúsculos. No es una atestación criptográfica de los archivos cargados. Debe fijarse al SHA
que se instala y verificarse junto al artefacto. Vacío o inválido deja el bloque en `null`;
la observabilidad no impide el arranque ni inventa una versión. El runner genera una
identidad aleatoria de instancia; cada nueva instancia empieza sin observaciones previas.
Sólo al entrar en `run()` publica su registro en `BotState`.

`GET /status` agrega `motor_telemetry`. Cada respuesta mantiene separados:

- Flags booleanos de configuración obtenidos por lista cerrada, sin volcar settings.
- Existencia del objeto concreto, tras su construcción. El sondeo usa referencia débil.
- Tarea exacta del loop, inspeccionando `done()` de su propio worker, no la tarea padre.
- Inicio y retorno del método instrumentado, con fechas que GET no refresca.

`tick_returned=true` incluye no-op, ausencia de datos y errores capturados internamente
por el motor. No significa evaluación exitosa, señal, fill, rentabilidad ni permiso para
activar. `false` indica que escapó una excepción o cancelación. Los callbacks no tienen
tarea propia: `task_running` siempre es `null/NOT_APPLICABLE`. No ejecutar un callback
no demuestra que esté apagado. Dependencias aún sin sondeo explícito permanecen UNKNOWN;
no deben usarse como readiness positivo ni derivarse de los flags.

## Puntos realmente conectados

| Componente | Objeto construido en | Método observado |
| --- | --- | --- |
| capture | runner | DataCaptureService._discover_markets |
| m1_loop | runner | Motor1Engine._tick |
| m2_poll | runner | Motor2ShadowPoller.poll_once |
| m2_exit | runner | Motor2ExitEngine._tick |
| m3_loop | runner | Motor3Engine._tick |
| m5_loop | runner | Motor5Engine._tick |
| m6_observer | runner | Motor6LineMoveShadow.observe |
| rest_observer | registro de handlers de captura | RestArbEngine.on_ticker |
| m8_observer | registro de handlers de captura | Motor8OfiShadow.observe_delta |
| m9_observer | registro de handlers de captura | Motor9SpilloverShadow.observe |

La captura reporta retornos de discovery, no continuidad de WebSocket. El estado del
feed, libros, presupuesto, capital y conciliación conserva sus fuentes y gates separados.
Un objeto retirado conserva su token de generación: sus retornos tardíos no pueden pisar
la instancia de reemplazo. Máximo diez bindings, referencias débiles, tamaño constante;
ningún request adicional, bucle, buffer de eventos, argumento del motor o texto de error.

## Lectura y frescura

El registro core es compatible con el emisor de referencia US500. El adaptador del runner
sondea la existencia y tarea al consultar, pero nunca llama al método del motor. El parser
del panel exige snapshot de hasta 30 s, tareas de hasta 30 s y ticks de hasta 300 s; un
loop más lento puede tener tarea viva y retorno vencido. Eso es incertidumbre explícita,
no razón para relajar el TTL ni aumentar frecuencias. Retroceso de reloj, datos inválidos
o fallo de instrumentación dejan `null`/UNKNOWN; no alteran la llamada financiera original.

El exportador ya consume este bloque opcional y el panel lo analiza por separado de los
campos legacy. El aislamiento panel/runtime, permisos de caché y las credenciales no se
modifican. La integración se comprueba localmente contra el parser y presentador de la
revisión 22522c3. No se acredita recepción cloud hasta instalar el runtime por un flujo
independiente autorizado y comprobar el SHA/instancia y la ficha recibida.

## Verificación necesaria para activación

Las pruebas locales cubren vida/fin/cancelación de la tarea exacta, objetos retirados,
reinicio, reloj, datos vencidos, errores del hook, callbacks reales y lectura de /status.
La suite completa comprueba que no cambian los caminos de órdenes y riesgo. Esto no
sustituye el circuito conectado Demo, ni resuelve la IOC UNKNOWN histórica. Su reserva
no se libera ni se reenvía para probar un control. La activación por motor requiere
además comandos autenticados, límites ligados a la propuesta, permisos separados de
entrada/salida, comprobación efectiva del resultado y evidencia Telegram extremo a extremo.
