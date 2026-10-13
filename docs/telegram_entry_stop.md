# ADR: parada independiente y puente Telegram de preparación

Estado: candidato offline; no instalado, ensamblado ni habilitado en servicios.
Fecha: 13-oct-2026. Decisión de Codex con revisión estática de Claude en la sesión existente.

## Problema y decisión

Los comandos normales requieren hechos frescos, revisión CAS y espacio en el historial.
Una parada de nuevas entradas debe seguir siendo posible cuando esos requisitos faltan.
Se añade un latch durable, monotónico, en el mismo ledger que las admisiones. No es el
kill-switch: conserva la política de salidas, conciliación, reservas, UNKNOWN y órdenes
ya admitidas. M5 deja de admitir cotizaciones nuevas, incluidas sus nuevas ventas; no
se afirma que las cotizaciones resting hayan sido retiradas.

La parada usa identidad completa (dueño, chat, entorno, SHA, instancia y límites),
nonce de petición, HMAC con dominio propio y TTL máximo de 60 segundos. Sólo admite
`stop` y `status`; no existe operación de reanudación o habilitación. El recibo se firma
tras el commit. La primera petición que engancha el latch conserva su identidad y
fecha; peticiones posteriores no se atribuyen ese efecto original.

## Fronteras y alternativas

- `DurablePermissionAdapter.entry_stop` serializa con la reserva mediante el lock
  SQLite del ledger. No consulta GateFacts, journal, capacidad, capital ni red.
  Conserva el máximo del reloj durable incluso si la hora retrocede; los controles
  normales siguen bloqueados ante esa regresión.
- `status` abre SQLite `mode=ro` y `query_only`, sin actualizar el reloj ni adquirir
  un lock de escritura. Un fallo de lectura sigue siendo desconocido.
- El latch se comprueba en `admit` antes de los hechos para M1/M2/REST entradas y
  M5 quotes. M2/M3 salidas mantienen su política anterior. Los ejecutores construidos
  sin adaptador no están cubiertos: este cambio no conecta ninguno por sí solo.
- El puente usa dos directorios preexistentes, uno por escritor, sin sockets ni API
  administrativa. El runtime nunca necesita token Telegram y el panel nunca recibe
  credenciales financieras ni acceso al ledger. El exportador de estado no cambia.
- Se descartaron otro `getUpdates`, acceso del panel a `/admin`, y usar la caché de
  telemetría como permiso financiero.

## Transporte y router

`StopMailbox` tiene dos slots fijos, `stop.json` y `status.json`, cada uno limitado a
8192 bytes, más un temporal fijo de escritura por slot. Usa `O_NOFOLLOW`, identidad
inode/dispositivo, dueño previsto, archivos regulares sin hardlinks y permisos sin
escritura de grupo/otros. Publicación por escritura completa, fsync, rename atómico y
fsync del directorio. Un temporal que deja un crash no se borra ni pisa automáticamente.
No hay creación de directorios, credenciales, servicios o montajes en este módulo.

`consume_once` procesa como máximo dos sobres por invocación explícita. Descarta TTL
vencido con `EXPIRED` antes de llamar al ledger; el adaptador vuelve a validar
antes de adquirir el lock y justo antes del commit. Una respuesta
vigente que ya coincide evita repetir el acceso al ledger. Una caída tras el commit
pero antes del recibo puede volver a presentar el mismo stop: el latch es idempotente y
conserva la atribución original. Este mecanismo jamás reintenta una orden financiera.
Un slot defectuoso no impide intentar la consulta del otro slot.

`TelegramEntryStopRouter` compone el `PaperControl` existente. No posee un poller,
HTTP, token ni hilo. Autentica dueño/chat privado y rechaza reenvíos, autores bot e
inline callbacks. Añade una confirmación breve y de un uso. Una DB separada conserva
nonce, respuestas por update y hash del payload; un mismo update no vuelve a publicar.
Cada solicitud de parada queda retenida completa en un historial separado que nunca
se poda. Una solicitud vigente no se vuelve a publicar. Tras vencer, una **nueva
confirmación humana** puede crear otra solicitud con identidad/TTL nuevos, conservando
íntegra la anterior y su estado de publicación incierto. No se reejecuta una orden
financiera ni se borra UNKNOWN. El historial tiene un máximo de 2000 solicitudes,
contador comprobado y presupuesto vinculado a la configuración: agotarlo bloquea
nuevas solicitudes sin eliminar incertidumbre. Las consultas sólo reemplazan su slot
de lectura. El registro del intento y consumo del nonce se confirman **antes** de publicar al
buzón. Un crash en esa frontera puede dejar una parada sin publicar: se muestra
pendiente/UNKNOWN y se consulta de nuevo el estado, sin afirmar envío ni aplicación.

La primera versión del router **rechaza `production`**, no ofrece habilitar motores y
expone `entry_stop_environment=demo`, configuración del puente y publicación durable
`NOT_REQUESTED` / `PUBLISH_UNCONFIRMED` / `PUBLISHED_AWAITING_RECEIPT`.
`entry_stop_control_available` permanece desconocido (`None`); no se deduce de que
el módulo esté configurado. Un fallo de publicación se conserva y se muestra como
degradado. Sólo el recibo verificado permite afirmar la parada.
El snapshot de permiso US500 conserva su ámbito paper. Los módulos compartidos
`wire`, `entry_stop`, `stop_mailbox` y `telegram_entry_stop` son stdlib: no importan
SQLAlchemy, modelos financieros ni ejecutores en el proceso del panel.

El texto de resultado sólo afirma parada registrada con recibo autenticado y fresco.
Un recibo vencido, de otra identidad/instancia/petición o alterado produce UNKNOWN.
La consulta nueva tiene un nonce nuevo; no renueva fechas de evidencia previa ni
levanta permisos. Ausencia de latch no significa motor habilitado o listo.

## Verificación reproducible

La suite de control cubre binding/TTL, presupuestos realmente agotados, hechos
inaccesibles, corrupción y esquema previo, fallo de commit, parada contra una reserva
en curso, conservación de salidas/órdenes previas, reconstrucción del adaptador,
firma/dominios/replay, buzones inválidos, caída de publicación y duplicados Telegram.

`test_panel_compat.py` importa el código exacto de cinco módulos del panel V2.7.1
`22522c3aa70f1b61ff662476cf7257fe3e93c0c1`, contrastado con SHA256. Ejecuta su
`PaperControl`, `PaperTelegram`, validación de payload, cursor y outbox originales en
SQLite temporal, con un solo transporte Telegram ficticio y cero HTTP. Recorre menú,
confirmación, recibo de parada y una entrega UNKNOWN que nunca se reenvía; conserva
el permiso/revisión/control_id US500. El panel fuente no se modifica.

CI obtiene ese commit en `compat-panel` y pasa `--panel-source` a la suite aislada.
Sin esa fuente la prueba de compatibilidad se omite explícitamente; no se presenta
como integración aprobada. No hay fixture de respuesta de cuenta real en esta prueba.

## Condiciones abiertas antes de conectar

1. El esquema anterior de permisos (b629c13/17b7091) es **incompatible** con el latch.
   No hay migración automática ni reparación de tablas. La provisión parcial y la
   reapertura antigua fallan cerradas. Definir una migración explícita revisada si
   existiera un ledger aprovisionado anterior; no resetearlo para instalar.
2. No existe procedimiento aprobado para levantar el latch, revincular instancia,
   recuperar reloj ni ampliar capacidad. No editar DB o identidad manualmente para
   eludirlo. Estos procedimientos requieren evidencia y revisión propias.
3. Definir UID/GID, ACL y montaje del transporte, con `PrivateUsers` y el aislamiento
   del panel reales. La prueba de un usuario en temporales no valida acceso entre
   servicios ni latencia, disco lleno, cuotas o rendimiento cloud. Umask debe permitir
   el lector previsto sin concederle escritura; no relajar BPF/rutas del runtime.
4. Provisionar una clave dedicada separada de Telegram/Kalshi y verificar SHA,
   instancia, límites y entorno en ambos extremos. No se generó ni instaló esa clave.
5. Un único consumidor Telegram debe preservarse. Su capacidad de outbox, política
   UNKNOWN y `retry_after` siguen vigentes: no se promete que un botón llegue si el
   transporte está bloqueado. La independencia demostrada corresponde al latch en
   el runtime, no a la disponibilidad end-to-end de Telegram.
6. Falta proveedor financiero autoritativo de GateFacts, lecturas de riesgo con la
   misma Session, recibos de barridos exhaustivos y cobertura histórica, atribución
   de inventario compartida, ensamblado explícito y pruebas Demo conectadas autorizadas.
   M6/M8/M9 continúan sólo observación; no se inventan flags ni tareas desde conteos.
7. El fixture real saneado de página terminal/cuenta vacía de M3 sigue pendiente.
   La IOC Demo UNKNOWN y su reserva de 66¢ se conservan sin mutación.

Estas pruebas cierran una pieza de preparación. No acreditan motores listos, operación
real, rentabilidad ni permisos para instalar. La captura US500, sus gaps y evaluación
económica permanecen en su protocolo y presupuesto separados.
