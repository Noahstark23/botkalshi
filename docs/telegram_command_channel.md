# Protocolo autenticado y journal de comandos

`src/control/channel.py` implementa la frontera entre un receptor Telegram autorizado
y el consumidor de permisos del runtime. No está instalado ni conectado al runner,
al panel o a un socket. El transporte, el adaptador real de permisos y el recorrido
Telegram siguen pendientes. No hay env var nueva ni clave provisionada. Las pruebas
usan un adaptador CAS ficticio; no equivalen a activar un motor ni a recibir un botón.

## Flujo que implementa

El receptor valida primero usuario/chat/update y prepara un `Intent` con identidad
exacta del operador, entorno, motor/permiso, SHA, instancia, hash de límites, revisión
esperada y caducidad de hasta60s. `Authenticator.seal("request", body)` firma JSON
canónico con HMAC-SHA256 y una clave del canal de al menos32bytes; jamás la clave Kalshi
ni el token Telegram. Entradas de más8KiB, campos extra, claves duplicadas, firmas
incorrectas y operaciones arbitrarias se rechazan antes de admitir un comando.

`handle` acepta sólo `propose`, `confirm` y `status`. Devuelve una respuesta firmada
con dominio separado. La confirmación queda vinculada al Intent exacto. El receptor
debe solicitar un clic humano fresco y validar su update; poseer la clave del canal
es autoridad del receptor y no demuestra por sí mismo que alguien haya pulsado un botón.
`PROPOSED` y `CONFIRMED` no ejecutan el adaptador.

El consumidor local llama `consume`. Valida otra vez fuente/instancia/límites,
revisión, frescura y requisitos del permiso; guarda `CLAIMED` antes de llamar al
adaptador. Dos consumidores que comparten journal no adquieren el mismo comando.
Después exige una observación atribuible: mismo command_id, revisión siguiente,
identidad y estado solicitado. Sólo esa lectura produce `APPLIED`. No-op del adaptador,
respuesta perdida, crash, identidad contradictoria o efecto no atribuible conserva
`CLAIMED`/`UNKNOWN`; consumir otra vez no repite el efecto. `reconcile` sólo observa.
Un recibo APPLIED describe el efecto observado en su fecha, no el permiso actual para
siempre; la ficha debe leer además el estado efectivo fresco del runtime.

## Contrato obligatorio del adaptador real

`RuntimeAdapter.observe(scope)` devuelve hechos del componente y sus gates. No deben
derivarse `gates_clear` o `available` de una bandera, cero señales o un servicio sano.
Incluyen kill-switch durable, capital, incertidumbre, dependencias e identidad reales.
`boot_allowed` es el techo inmutable provisionado por el operador: Telegram no lo eleva.
Todos los valores desconocidos/vencidos bloquean habilitación.

El arranque del runner rechaza una lectura fallida o un valor durable desconocido
del kill-switch antes de lanzar servicios (`KILL_SWITCH_BOOT_UNVERIFIED`, exit1).
Conserva la fila original: no sustituye incertidumbre por clear ni repara la DB.
Incluso en shadow debe poder verificar esa pausa. Un nuevo arranque vuelve a leerla;
no es una vía para levantar un engaged válido. Esta protección de boot no sustituye
la revalidación transaccional posterior que exige el adaptador de permisos.

`apply_if_current` debe revalidar bajo su propia transacción CAS, después del preflight
del canal. Debe persistir permiso/revisión/command_id juntos; nunca levantar kill-switch,
mutar límites, liberar UNKNOWN o iniciar órdenes para habilitar. La prueba de carrera
demuestra por qué comprobar sólo antes de llamar al adaptador no basta. Los permisos
por motor deben gatear los puntos reales de admisión sin sustituir las capas A/C.

Entradas y salidas son scopes distintos. No se admite desactivar protección de salidas
si hay posiciones/resting/incertidumbre; pausar fair M2 requiere que no existan
dependientes activos. Pausar entradas sí puede solicitarse con gates financieros
bloqueados. La pausa M5 sólo se declara aplicada cuando el adaptador observa todas sus
quotes retiradas y sin órdenes inciertas; el canal no realiza esas cancelaciones.
M6/M8/M9 son observación, no ejecutores. US500 conserva su control paper separado.

## Persistencia, autenticación y límites

El journal SQLite es exclusivo del consumidor del runtime, separado de trades/reservas.
Sólo contiene dos tablas de control SQLModel; config/propietario/entorno/clave se fijan
al crearlo. Archivo0600 y directorio del propietario no escribible por grupo/otros.
La apertura rechaza archivos ajenos, symlink del archivo, sustitución de inode y cambio
de configuración. No protege contra un administrador o proceso del mismo usuario que
manipule/restaure bases: requiere permisos y almacenamiento confiables; no se autoriza
rollback de journal ni de permisos. Las claves se suministran en memoria y no se guardan.

Máximo1.000comandos por defecto, configurable1–10.000 y fijo para esa base. Sin poda ni
reutilización de IDs: al llenarse rechaza propuestas nuevas, conserva consultas y comandos
ya confirmados. Antes de provisionarlo debe resolverse retención/rotación operativa sin
borrar incertidumbre y una vía de pausa independiente. Este módulo no acredita operación
indefinida. Una caída de DB antes del claim impide invocar el adaptador; después del efecto
conserva el claim y obliga a conciliar. Retroceso del reloj bloquea hasta recuperar el
último tiempo confirmado. Caducidad se revalida tras observar, no sólo al recibir.

La integración posterior debe mantener el panel aislado del runtime financiero: no
habilitar acceso a `/admin`, entregar claves financieras ni quitar filtros BPF. El
transporte autenticado tendrá destino y presupuesto fijos, clave propia y errores saneados.
No se debe informar éxito por enviar bytes ni habilitar un segundo poller Telegram.

## Evidencia y pendientes

Pruebas aisladas cubren HMAC/dominios/tampering, actor/chat/entorno, identidad/revisión,
TTL/clock, propuestas y confirmaciones sin efecto, carreras de dos consumidores,
reinicio/ACK perdido, persistencia fallida antes/después y conciliación atribuible.
El adaptador usado sólo modifica una estructura de permisos ficticia, sin clientes
financieros. Faltan transporte, adaptador CAS durable real y gates de motores, UI de
propuesta/confirmación con dedup de updates y recorrido conectado dentro del entorno
confirmado. La IOC Demo UNKNOWN y sus reservas siguen intactas. No se declara preparación
real, rentabilidad ni control instalado por aprobar estas pruebas.

## Incremento de backend durable (13-oct-2026)

Existe ahora un backend SQLite y hooks opcionales de admisión descritos en
[durable_motor_permissions.md](durable_motor_permissions.md). Falta el proveedor de
hechos del runtime, ensamblado y transporte: el párrafo inicial sigue describiendo el
estado de instalación. Las pruebas nuevas usan permisos persistidos reales en una DB
temporal y hechos financieros ficticios; no convierten esos hechos en evidencia de cuenta.


## Recuperación de instancia sin reenvíos

El backend conserva ahora el binding completo y su cadena de transiciones en la
misma DB. `historical_write(intent)` devuelve evidencia de una escritura exacta;
no cambia estados del journal, no vuelve a llamar apply y no acredita retirada M5.
CLAIMED/UNKNOWN antiguos permanecen pendientes; una ausencia no se convierte en
NOT_APPLIED. PROPOSED/CONFIRMED antiguos siguen sujetos a identidad, TTL y CAS.
La consulta histórica no sustituye la observación actual del canal.
El router opcional de parada tiene una transición separada autenticada que conserva
su DB e historial, descrita en [permission_recovery.md](permission_recovery.md).
