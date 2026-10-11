# Menú US500 paper por Telegram

Implementación preparada para revisión, no instalada ni conectada al bot existente. `/motores` y `/us500` muestran **Activar simulación US500**, **Pausar US500** y **Estado**. Sólo controla la simulación US500 de esta carpeta; no cambia flags del bot deportivo ni habilita órdenes reales.

## Comportamiento

- Comienza deshabilitado. Dueño y chat privado se fijan por IDs exactos; se rechazan grupos, otros usuarios, bots y mensajes reenviados.
- Activar presenta una confirmación única, con vencimiento de 60 segundos, ligada al usuario/chat/revisión. Sólo confirmar habilita paper.
- Pausar se aplica inmediatamente en SQLite e invalida confirmaciones pendientes. No liquida posiciones, cancela órdenes ni libera reservas. Una pausa de riesgo en el ledger permanece aunque el dueño reactive paper.
- El runner y la pausa usan el mismo bloqueo por evento: después de confirmarse una pausa no se aplica otro evento. Una pausa entre reserva y fill conserva esa reserva y la reanudación usa los mismos IDs.
- Updates repetidos no activan dos veces. El outbox guarda antes de avanzar el cursor y registra cada intento antes de enviar. Entregas ambiguas quedan UNKNOWN y no se reenvían automáticamente.
- Al agotar capacidad o fallar la recepción/procesamiento, se intenta una pausa local durable. Si no se logra confirmarla, se informa `PAUSE_UNCONFIRMED`; no se afirma una pausa que no se haya guardado.

El estado del menú informa el permiso del operador, no salud del mercado, actividad de un proceso ni ausencia de exposición. Los reportes del runner contienen cash, posición, reservas y pausas de riesgo. No interpretar «simulación habilitada» como «ejecutor corriendo».

## Recorrido sin cuenta ni claves

```sh
python3 run_control_demo.py --directory /tmp/us500-botones-revision-nueva
```

Genera `BUTTON_TRANSCRIPT.json` con textos/botones y `SUMMARY.json` con la prueba completa. Usa dueño ficticio y jamás accede a Telegram.

## Integración futura, separada de producción

Existe un adaptador listo para un **bot Telegram dedicado de pruebas**. No ejecutar otro `getUpdates` sobre el token del bot actual: competiría con su receptor. Para reutilizar el menú existente, integrar `PaperControl.handle_update()` en ese único router y mantener los comandos de producción fuera de este módulo. Esta integración no se ha desplegado.

El token sólo se lee al invocar explícitamente el CLI, desde `US500_PAPER_TELEGRAM_TOKEN`. No acepta tokens por argumentos, no carga `.env` ni credenciales Kalshi. No pegar el token en comandos, archivos de prueba o informes. Una vez que un operador configure de manera segura un token dedicado y sus propios IDs:

```sh
python3 us500_telegram.py --control-db /RUTA/NUEVA/control.sqlite3 --outbox-db /RUTA/NUEVA/outbox.sqlite3 --owner-user-id ID_USUARIO --owner-chat-id ID_CHAT_PRIVADO
```

Hace **un solo ciclo acotado**, no instala servicio, scheduler ni bucle. No se ejecutó con claves reales. Presupuesto por ciclo: hasta 10 updates y 10 envíos; los timeouts de socket no garantizan un plazo estricto frente a bloqueos del sistema/DNS. Historial del adaptador: hasta 2.000 updates y 4.000 salidas; al alcanzar el límite se pausa y se conserva evidencia. Revisar capacidad antes de un funcionamiento continuo.

El proceso que consuma una sesión ficticia debe compartir la misma DB de control:

```sh
python3 us500_runner.py --input SESION.json --database /RUTA/NUEVA/paper.sqlite3 --output INFORME_NUEVO.json --control-database /RUTA/NUEVA/control.sqlite3 --owner-user-id ID_USUARIO --owner-chat-id ID_CHAT_PRIVADO
```

No cambia el estado a habilitado: sólo procesa si el permiso confirmado ya existe. Omitir el control en una base vinculada se rechaza. Los archivos de salida deben ser nuevos. Las tres bases tienen propietarios y propósitos distintos; no reutilizar DB históricas.

Referencia del protocolo: [Telegram Bot API](https://core.telegram.org/bots/api#getupdates) y [CallbackQuery](https://core.telegram.org/bots/api#callbackquery). La cobertura offline prueba el protocolo con transportes falsos; no demuestra entrega al teléfono.
