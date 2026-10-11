# Panel único de motores — alcance e integración pendiente

11 de octubre de 2026. El panel es parte del receptor local de simulación US500. No se desplegó sobre BotKalshi. El catálogo se contrastó contra `codex/live-readiness-20261006` en `c14312867a0a07bf3ccbddbd63a0b909d2f8fb5a`; ese SHA no permite inferir el estado remoto actual.

## Recorrido

Abrir `/motores` (también `/menu` o `/start`), elegir un motor y usar su pantalla. US500 mantiene activación de simulación con confirmación de un solo uso y pausa durable inmediata. Todos los detalles permiten volver al menú; actualizar muestra el permiso vigente del replay.

Los controles de M1, M2, M3, M5, M6, REST, M8 y M9 y el control general están **desconectados**. Ofrecen botones rotulados «Activar · sin conexión» y «Desactivar · sin conexión» para explicar la disponibilidad, pero no aplican cambios. No existe «activar todo» funcional. La pantalla lo declara antes de pulsarlos y repite el resultado después.

## Lo implementado

- `bot_motor_panel.py`: catálogo puro, textos y teclados. No importa motores, clientes financieros, variables de entorno ni credenciales.
- `us500_control.py`: ruteo sobre el mismo control/cursor/outbox, autenticación privada exacta y deduplicación. Callback desconectado registra `REJECT_DISCONNECTED`; no cambia permiso, revisión, reserva ni confirmación pendiente de US500.
- La identidad y los esquemas del control/outbox existentes se conservan. No se crea otro consumidor `getUpdates`, base de control ni puente al runtime remoto.
- `/us500` y callbacks anteriores siguen funcionando. El estado dice «permiso de replay», porque ese permiso no inicia un proceso ni representa el estado de riesgo o exposición.
- El supervisor local mantiene sus límites, pausas de ciclo de vida y exclusión de consumidores. No se extendió su duración ni se instalaron servicios.

## Contrato para una integración posterior

Esta entrega NO implementa los adaptadores ni la activación real. Cada adaptador necesitará informar por separado `adapter_connection`, `process_state`, `operator_permission`, `risk_pause`, `exposure`, fuente, entorno y momento de observación. Sin conexión: estado `UNKNOWN`, no `OFF`; sin evidencia de exposición: `null`, no cero. Datos vencidos deben identificarse como tales.

| Motor | Separación necesaria para un control correcto |
|---|---|
| M1 | Observación y entradas; mantener conciliación de patas y exposición incierta. Su activación continúa fuera del alcance. |
| M2 | Distinguir entrada, venta y generación de fair. Apagar el ciclo completo también detiene salidas y el fair de M5. |
| M3 | Gestiona salidas existentes. No ofrecer su apagado como una pausa inocua de nuevas compras. |
| M5 | Pausar cotizaciones puede intentar retirar quotes; mantener reservas y conciliación. No declarar cancelación hasta confirmación terminal. |
| M6 | Observación dentro del ciclo M2; no existe ejecutor. |
| REST | Entradas de arbitraje separadas de conciliación y liquidación contable. |
| M8 / M9 | Observadores; no existe ejecutor. |
| US500 | El permiso pausa todo el replay, incluidos hechos contables ficticios. No es una pausa de entradas con liquidación activa. |

Una futura «pausa de nuevas entradas» general debe conservar las tareas de salida/conciliación que correspondan. No enlazarla simplemente a `/admin/pause`: el endpoint heredado no establece por sí mismo el kill-switch durable y también bloquea ventas M2/M3. No traducir un botón a edición de `.env` o reinicio: varios ejecutores se construyen sólo al arrancar. Confirmar aplicación requiere respuesta del proceso, identidad/SHA del destino y lectura posterior; una intención guardada no equivale a un cambio aplicado.

Rutas contrastadas en el candidato M2/M5: `src/runner.py`, `src/utils/config.py`, `src/monitoring/dashboard.py`, `src/monitoring/health.py`, `src/strategies/motor_3_clv/executor.py` y `src/strategies/motor_5_mm/engine.py` (confirmar las rutas del checkout antes de integración). El receptor remoto deberá coexistir mediante un único router, no dos pollers con el mismo bot.

## Verificación

`tests/test_bot_motor_panel.py` recorre catálogo, disponibilidad, todos los botones desconectados, propietario/chat, entradas desconocidas, idempotencia, conservación de confirmaciones, control general y reinicio. Los tests previos siguen cubriendo caducidad, pausas, exclusión entre consumidores, fallos de SQLite y outbox UNKNOWN.

Ejecutar desde esta carpeta `python3 verify_offline.py`, `python3 verify_package.py` y Ruff0.14.0. Sólo la prueba visual conectada documentada por el operador demuestra recepción de Telegram; los tests offline no acreditan entrega ni control remoto. No se ha reproducido la suite M2/M5 al cambiar sólo este módulo aislado.


## Actualización V2.3 — receptor cloud y estados

La nueva instalación aislada está documentada en `TELEGRAM_CLOUD.md`. Permite operación
continua del receptor sin Mac, estados de sólo lectura del runtime y permiso paper
US500. Las descripciones anteriores del panel desconectado/local son históricas.
No incorpora activación real ni ejecutor automático. M6/M8/M9/REST carecen de señal
específica en el API actual; el panel lo declara como desconocido.
