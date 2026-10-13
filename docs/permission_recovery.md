# Recuperación explícita de identidad y parada Telegram

Candidato opcional, sin conexión automática al runner ni instalación. Sólo prepara
una transición local para bases provisionadas con este esquema. No autoriza órdenes,
flags, despliegues, nuevos permisos humanos ni reutilización de resultados UNKNOWN.

## Ledger de permisos

`PermissionBinding` conserva JSON canónico completo, digest y generación. Cada apply
registra el digest y la generación reales junto al digest del Intent en la misma
transacción. No se deriva un binding incompleto del Intent. JSON duplicado, campos
extra, tipos erróneos y formas no canónicas se rechazan.

El operador debe inspeccionar el ledger, detener escritores y autorizar la transición
exacta antes de invocar `rebind_instance(expected_previous_digest, expected_generation)`
en un adaptador construido para la nueva identidad. El CAS evita una revisión obsoleta;
no acredita autorización ni demuestra que murió el proceso anterior. Constructor,
provision, Telegram y runner nunca invocan esa operación. No hay CLI mutante instalada.

Bajo `BEGIN IMMEDIATE` se reconstruye el binding anterior desde la DB, se comprueban
historia, reloj y CAS, y se exige igualdad exacta de actor/chat/entorno/scopes,
boot_allowed, límites y capacidades. Sólo cambian SHA y nonce de instancia. No se
reutiliza ningún nonce de la cadena conservada. Hay como máximo 128 transiciones;
llenarla bloquea otra transición y no autoriza borrar historial o elevar el límite.
Cada recuperación con nonce nuevo tras reinicio o deploy consume una transición;
el tope cuenta recuperaciones, no días, y no demuestra operación indefinida.

Entradas M1/M2/REST y quotes M5 habilitadas pasan a disabled, revisión +1, con una
referencia `(generación, scope)` al mapa canónico `resets_json` de su transición.
Ese efecto no es un comando humano ni gasta o renueva su capacidad. Scopes ya
inhabilitados permanecen iguales. Salidas M2/M3 conservan estado, revisión y comando
original: se mantiene la política protectora, no se inventa aprobación humana. Las
admisiones de salida todavía exigen sus gates existentes. El latch, su primera
atribución, comandos, admisiones, trades, pausas y contadores no se alteran.

Una transacción de admisión anterior se serializa con el corte; si ganó el lock,
su Trade y atribución se conservan. Después del commit el adaptador antiguo falla
por binding al observar, aplicar, admitir o parar. Nueva habilitación de entradas
requiere otro comando ligado a identidad y revisión actuales. M5 disabled sólo
cierra admisiones: no cancela quotes ni prueba que se retiraron.

`historical_write(intent)` lee bajo una transacción sin avanzar reloj ni hacer red.
Comprueba Intent exacto, digest, scope, revisión/estado y binding original registrado
en la cadena. Devuelve prueba de escritura con `current_permission_proven=false` y
`quotes_retired_proven=false`; una fila ausente devuelve falta de evidencia. No
terminaliza CLAIMED/UNKNOWN ni cambia el journal. No existe inferencia NOT_APPLIED.
Los comandos viejos CONFIRMED no pueden atravesar el control de identidad/TTL/CAS.

La cadena no es una firma externa contra administradores que restauren una copia
antigua completa o modifiquen coherentemente toda la DB. No protege contra rollback
de backups ni convierte falta de filas en certeza. Rebuild copia íntegros metadatos,
comandos, transiciones y admisiones. No backfillea esquemas antiguos ni reconstruye
atribución que nunca se guardó; esos formatos fallan y requieren migración revisada.

## Router de parada, misma DB

Después del corte de permisos, `recovery_announcement(auth, generation=...)` emite una atestación
HMAC de dominio `entry-stop-rebind-v1`, con identidades anterior/nueva, ambos digests
completos de permisos, generación y TTL de 60 segundos. Se produce desde un
corte persistido y validado; si se omite generación elige el último; no cambia permisos ni prueba gates o cuenta. La clave
es la dedicada del canal, nunca un token Telegram o credencial financiera. Es
un autenticador simétrico compartido: no demuestra origen exclusivo del runtime
ni resiste que otro poseedor de la clave fabrique una atestación.

`TelegramEntryStopRouter.rebind_instance(...)` es una operación local explícita sobre
un archivo existente: valida propietario, modo, inode, HMAC, TTL, identidad estable,
target exacto, cadena, CAS del config anterior y generación. No crea otra DB, modifica
ACL ni descubre/adopta identidades automáticamente. La cadena del router comienza en
la generación cero del ledger y sigue cortes uno a uno. Si se atrasó, el dueño
actual puede atestiguar de nuevo cada corte conservado con TTL fresco: se adopta
toda la cadena con el consumidor detenido, sin perder historia ni saltar cortes.
Un destino intermedio puede estar muerto y no acredita disponibilidad; comprobar
que se alcanzó la identidad actual antes de habilitar el consumidor. Una fila
ausente o dañada bloquea; no se crea otro journal para eludirla.

Se conservan updates, high-water, contador, solicitudes, incertidumbre y publicaciones.
La confirmación pendiente se archiva en la transición con su expiración y pierde
vigencia: no se aplica a otra instancia. Una respuesta cacheada antigua se muestra
como histórica, y un recibo de otra instancia no prueba el estado actual. Consultar
con un pedido nuevo permite verificar el latch original sin reenviar la parada.
StopRequest mantiene SHA/nonce completos. Un consumidor antiguo que siga vivo
fallará con PANEL_CONFIG_CHANGED y puede atascar el mismo update en el transporte;
la recuperación exige detenerlo y construir el router nuevo conservando su DB. La transición y la publicación al buzón
se serializan con el mismo lock; el intento se guarda antes del I/O y un crash o
fallo posterior conserva PUBLISH_UNCONFIRMED. No hay reintento implícito.

Las transiciones del router tienen también tope 128, conservan su evidencia HMAC y
no renuevan las 2000 solicitudes de stop ni la política de deduplicación. No tocan
la DB/cursor/outbox US500. La compatibilidad con el panel 22522c3 se prueba mediante
su fuente inmutable con Telegram ficticio; no acredita instalación cloud.

## Orden y límites pendientes

Con escritores detenidos: migración de esquema revisada si corresponde, aceptación
explícita del esquema en el fence financiero, revinculación de recibos y permisos,
atestación y transición del router, y verificación de estado. Ninguno de estos pasos
es automático ni está ejecutado por estas pruebas. Entre caída y recuperación local
la parada por Telegram puede no estar disponible; no se promete disponibilidad por
tener un botón ni se propone levantar o modificar un kill-switch como sustituto.

Siguen pendientes identidad de cuenta/fixtures, proveedor financiero GateFacts,
composición bid/ask frente al fence global, reserva de capacidad para salidas,
recuperación de reloj, migración legacy, aislamiento UID/ACL/PrivateUsers, clave
instalada y ensamblado del único consumidor Telegram. Estas pruebas temporales no
permiten declarar los motores listos ni activar dinero real o Demo.
