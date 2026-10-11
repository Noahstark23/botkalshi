# Menú US500 paper por Telegram

Implementación aislada y revisable. La conexión local del 11 de octubre se documenta en VERIFICACION_V2_1.md. `/motores`, `/menu` y `/start` abren el panel de todos los motores: M1, M2, M3, M5, M6, REST, M8, M9 y US500. Cada motor tiene su pantalla y navegación de regreso. `/us500` abre directamente **Activar simulación US500**, **Pausar US500** y **Estado**.

**Sólo US500 tiene adaptador conectado**, limitado al permiso de replay local. Los otros ocho motores y el control general muestran «conexión pendiente / estado no consultado». Sus botones están identificados «sin conexión» y responden «No se aplicó ningún cambio». No son interruptores funcionales de esos motores. El panel no lee ni escribe flags del bot remoto y no habilita órdenes reales. El alcance y la integración pendiente están en `PANEL_TODOS_MOTORES.md`.

## Comportamiento

- Comienza deshabilitado. Dueño y chat privado se fijan por IDs exactos; se rechazan grupos, otros usuarios, bots y mensajes reenviados.
- Activar presenta una confirmación única, con vencimiento de 60 segundos, ligada al usuario/chat/revisión. Sólo confirmar habilita paper.
- Pausar se aplica inmediatamente en SQLite e invalida confirmaciones pendientes. No liquida posiciones, cancela órdenes ni libera reservas. Una pausa de riesgo en el ledger permanece aunque el dueño reactive paper.
- El runner y la pausa usan el mismo bloqueo por evento: después de confirmarse una pausa no se aplica otro evento. Una pausa entre reserva y fill conserva esa reserva y la reanudación usa los mismos IDs.
- Updates repetidos no activan dos veces. El outbox guarda antes de avanzar el cursor y registra cada intento antes de enviar. Entregas ambiguas quedan UNKNOWN y no se reenvían automáticamente.
- Al agotar capacidad o fallar la recepción/procesamiento, se intenta una pausa local durable. Si no se logra confirmarla, se informa `PAUSE_UNCONFIRMED`; no se afirma una pausa que no se haya guardado.

El estado del menú informa el permiso del operador, no salud del mercado, actividad de un proceso ni ausencia de exposición. Los reportes del runner contienen cash, posición, reservas y pausas de riesgo. No interpretar «simulación habilitada» como «ejecutor corriendo».

## Supervisor local acotado

`us500_telegram_service.py` ofrece `TelegramPaperService(..., credential_provider=...).run(max_cycles, interval_seconds, stop_event)`. Importarlo no inicia procesos ni lee secretos. Tiene locks por carpeta, identidad de control y bot; otro receptor local que use este protocolo es rechazado. No detecta por sí solo consumidores remotos: comprobar ausencia de webhook/receptor antes de conectar.

El supervisor pausa al arrancar, al finalizar y ante fallos. Escribe estado atómico con heartbeat, PID y códigos saneados. `pause_cause` distingue pausa del ciclo de vida; el código compartido TELEGRAM_UNAVAILABLE en la DB no demuestra una caída de red. Una salida ambigua no se reenvía. No instala servicios ni lanza el runner. El CLI requiere un máximo de ciclos explícito (hasta 10.000). No usarlo como bucle ilimitado ni autoactivar al reiniciar.

El campo aditivo `last_poll_error` conserva un código admitido cuando el poll se detiene; `null` significa que no se observó ese fallo durante la ejecución actual. No contiene mensajes crudos ni reconstruye causas antiguas. El seguimiento posterior encontró el receptor local detenido; ver `DIAGNOSTICO_TELEGRAM.md`. Un menú fijado no demuestra que el receptor siga atendiendo botones.

## Recorrido sin cuenta ni claves

```sh
python3 run_control_demo.py --directory /tmp/us500-botones-revision-nueva
```

Genera `BUTTON_TRANSCRIPT.json` con textos/botones y `SUMMARY.json` con la prueba completa. Usa dueño ficticio y jamás accede a Telegram.

## Integración separada de producción

Debe existir un solo receptor `getUpdates` por bot. Puede usarse un bot dedicado o uno existente que no tenga receptor ni webhook, después de verificarlo. Otros componentes que sólo envían `sendMessage` pueden coexistir sin cambiarse. Si ya existe un router, la integración exige una revisión separada; no instalar un poller que compita con él. La conexión local verificada reutiliza un bot con emisor de avisos, sin modificar ese emisor ni servicios de producción.

En los CLI el token sólo se lee al invocarlos explícitamente, desde `US500_PAPER_TELEGRAM_TOKEN`. No acepta tokens por argumentos, no carga `.env` ni credenciales Kalshi. No pegar el token en comandos, archivos de prueba o informes. Una vez que un operador configure de manera segura un token dedicado y sus propios IDs:

```sh
python3 us500_telegram.py --control-db /RUTA/NUEVA/control.sqlite3 --outbox-db /RUTA/NUEVA/outbox.sqlite3 --owner-user-id ID_USUARIO --owner-chat-id ID_CHAT_PRIVADO
```

Hace **un solo ciclo acotado**, no instala servicio, scheduler ni bucle. La verificación local conectada usó la credencial Telegram ya guardada, sólo en memoria; las pruebas automatizadas no usan credenciales. Presupuesto por ciclo: hasta 10 updates y 10 envíos; los timeouts de socket no garantizan un plazo estricto frente a bloqueos del sistema/DNS. Historial del adaptador: hasta 2.000 updates y 4.000 salidas; al alcanzar el límite se pausa y se conserva evidencia. Revisar capacidad antes de un funcionamiento continuo.

El proceso que consuma una sesión ficticia debe compartir la misma DB de control:

```sh
python3 us500_runner.py --input SESION.json --database /RUTA/NUEVA/paper.sqlite3 --output INFORME_NUEVO.json --control-database /RUTA/NUEVA/control.sqlite3 --owner-user-id ID_USUARIO --owner-chat-id ID_CHAT_PRIVADO
```

No cambia el estado a habilitado: sólo procesa si el permiso confirmado ya existe. Omitir el control en una base vinculada se rechaza. Los archivos de salida deben ser nuevos. Las tres bases tienen propietarios y propósitos distintos; no reutilizar DB históricas.

Referencia del protocolo: [Telegram Bot API](https://core.telegram.org/bots/api#getupdates) y [CallbackQuery](https://core.telegram.org/bots/api#callbackquery). La cobertura offline prueba el protocolo con transportes falsos; no demuestra entrega al teléfono.


## Actualización V2.3 — receptor cloud y estados

La nueva instalación aislada está documentada en `TELEGRAM_CLOUD.md`. Permite operación
continua del receptor sin Mac, estados de sólo lectura del runtime y permiso paper
US500. Las descripciones anteriores del panel desconectado/local son históricas.
No incorpora activación real ni ejecutor automático. M6/M8/M9/REST carecen de señal
específica en el API actual; el panel lo declara como desconocido.
