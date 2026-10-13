# Permisos durables en la admisión de órdenes

El candidato incorpora `DurablePermissionAdapter` y puntos de integración opcionales
para M1, M2 entradas, M2/M3 salidas, REST entradas y M5 quotes. No está provisionado
ni conectado al runner o Telegram. Su existencia no indica que un motor esté listo.

## Qué significa aplicar una pausa

El punto de admisión es el commit de la reserva y su atribución. Permisos y reservas
usan el mismo archivo SQLite y el mismo write lock. Si una pausa gana el lock primero,
la reserva posterior falla; si la reserva gana primero, permanece admitida bajo la
revisión anterior. La pausa no revoca órdenes admitidas, cambia sus reintentos, cancela
quotes, libera UNKNOWN ni modifica hedges o límites. El recibo histórico APPLIED no
significa que no puedan llegar fills o envíos de órdenes admitidas previamente.

El contrato de `CommandChannel` para M5 se conserva: deshabilitar puede cerrar nuevas
admisiones, pero APPLIED requiere observar retirada de las quotes y ausencia de
incertidumbres. Este adaptador no ejecuta esa retirada. La conciliación del comando es
una lectura de hechos, nunca un segundo apply. Para salidas M2/M3 se rechaza deshabilitar
protección mientras haya posiciones, órdenes resting o incertidumbre. La pausa global y
la conciliación manual de M3 conservan su política anterior.

## Persistencia y rechazo conservador

`PermissionBinding` fija propietario, chat, entorno, SHA, instancia, hash de límites,
scopes realmente integrados y techo permitido al boot. `provision()` es una operación
explícita sobre un ledger ya existente: todos los permisos nacen deshabilitados. El
constructor, el runner y la admisión no provisionan nada. Reabrir no resetea permisos;
un binding diferente o filas incompletas bloquean. Una nueva instancia/release requiere
un procedimiento de migración/revinculación aún pendiente; no se reconstruye autoridad
mediante defaults. El inode del ledger se comprueba antes de operar; esto no defiende
contra un administrador que restaura o modifica contenidos en el mismo inode.

Una provisión nueva crea esquema y estado inicial en la misma transacción. Si ya
existe cualquiera de las cuatro tablas, exige las cuatro y metadatos coherentes;
no recrea una tabla perdida ni un meta borrado. Cada operación contrasta los conteos
de comandos y atribuciones con los contadores durables. Esto detecta pérdidas de
filas, no una reescritura administrativa coherente de filas y contadores ni el borrado
de las cuatro tablas antes de una nueva provisión explícita. Los conteos recorren
historia acotada; no se ha medido su coste al máximo de capacidad.

Cada apply revalida identidad, revisión, plazo y hechos bajo el lock, y confirma juntos
permiso/revisión/command_id y el registro del comando. Un ID ya usado no vuelve a aplicar.
El permiso se contrasta además con su registro atribuible. El journal autenticado queda
en una DB distinta: su claim previo y conciliación posterior conservan UNKNOWN si el
ACK se pierde entre ambas bases.

`admit()` añade las filas Trade y `motor_permission_admissions` dentro de la transacción
del executor, después de sus controles financieros. Cada atribución contiene scope,
revisión, command_id, SHA, instancia, hash de límites, tiempo y digest del intent original.
No se reescribe al recibir fills o dividir una posición. En el perfil multi, la reserva
RiskCommitment comparte ese commit. Un fallo de cualquier insert revierte el conjunto.
Los controles de inventario, dinero, capital, límites y capas A/C siguen siendo obligatorios.
Las cuatro tablas del backend se conservan completas y sus conteos se verifican en `rebuild_db.py`;
no son diagnóstico sujeto a retención. Reconstruir el archivo no autoriza un swap ni
revincula la identidad del adaptador vivo. El journal del canal sigue en su DB separada.

Los contadores son finitos: por binding, hasta 1.000 comandos por defecto (máximo 10.000)
y 100.000 admisiones como máximo, sin poda ni reciclaje automático. Agotar comandos
bloquea también un nuevo comando de pausa; agotar admisiones bloquea reservas. Esto exige
una vía operativa de pausa independiente y retención/reaprovisionamiento antes de instalar.
No se declara operación indefinida ni se borra incertidumbre para recuperar capacidad.

La capacidad de admisiones es compartida por los seis scopes: la rotación de M5
puede agotarla y bloquear también salidas M2/M3. No existe aún reserva de capacidad
para salidas. La pausa por scope exige hechos disponibles y frescos, incluso al
deshabilitar entradas; datos degradados impiden aplicar esa pausa. Las admisiones
de salidas también exigen gates limpios y cero UNKNOWN en el scope. El impacto sobre
otros tickers depende de cómo el proveedor delimite esos hechos. Estas restricciones
conservadoras permanecen; no constituyen una vía de parada operativa completa.

Un salto del reloj hacia adelante puede persistir un `last_seen_ms` futuro: cuando
el reloj retrocede al tiempo correcto se rechazan observación, comandos y admisiones,
incluidas salidas. No hay reparación automática ni procedimiento de recuperación
aprobado. Asimismo, vincular la instancia al UUID de telemetría por proceso bloquea
tras un reinicio hasta una revinculación aún no diseñada. Ambos límites impiden la
instalación operativa de este candidato.

Después de adquirir el lock se verifica `in_transaction` en la conexión SQLite real,
no sólo el estado de SQLAlchemy. Una reserva en AUTOCOMMIT sin transacción explícita
se rechaza antes de añadir orden o atribución. La integración sigue obligada a mantener
la misma transacción hasta el commit. En REST y M5 no-multi esto no traslada sus
controles financieros anteriores al interior de la reserva; esa coordinación sigue pendiente.

## Hechos financieros: integración pendiente

El proveedor síncrono `GateFacts(session, scope, now)` debe obtener hechos autoritativos
locales bajo la transacción recibida, sin red, awaits, commit ni mutación de permisos.
Debe cubrir capital confirmado/frescura, pausas en memoria, reconciliación de exposición,
incidentes, dependencias y requisitos específicos del scope. Los hechos vencidos,
desconocidos o no disponibles bloquean. El adaptador además lee el kill-switch durable
bajo la misma transacción; no lo levanta ni repara. Cero filas no prueba cuenta vacía.

El poller de posiciones M3 rechaza recorridos truncados, páginas/cursores inválidos,
identidades duplicadas y cantidades inválidas antes de actualizar o purgar la caché.
Tiene un límite de 20 páginas de 100 posiciones y 30 segundos para el recorrido. Un
fallo o cancelación interrumpe la confirmación de vacíos consecutivos. Esto preserva
la caché previa, sin renovar su fecha; el engine conserva su política de usar caché
tras un fallo de sync. No existe todavía un recibo durable de barrido completo ni
un snapshot atómico de cuenta. Dos vacíos completos permiten la purga de caché según
la regla existente, pero no resuelven UNKNOWN ni acreditan readiness financiero.

El contrato terminal exige cursor string vacío explícito y lista de posiciones. La
[documentación oficial de Get Positions](https://docs.kalshi.com/api-reference/portfolio/get-positions)
consultada el 13-oct-2026 describe un array requerido y cursor string con valor vacío
al terminar, pero no marca cursor como requerido. Falta un fixture real saneado de
la última página/cuenta vacía antes de merge o instalación: si el entorno omite el
cursor o entrega null, el sync se rechaza y conserva caché anterior. No se acredita
compatibilidad de cuenta mediante mocks ni se relaja el rechazo para forzar pase.

**Ese proveedor del runtime aún no está implementado.** Los tests lo sustituyen por
hechos ficticios explícitos. Por tanto, el backend SQLite y los hooks probados no
acreditan una fuente financiera conectada ni control operativo. Ningún scope se registra
automáticamente a partir de flags. M2 fair y M6/M8/M9 no son admitidos por este backend;
US500 conserva su permiso paper independiente.

### Lectores locales preparados para esa transacción

`incident_status(session=)` y `mm_quotes_paused(session=)` admiten la Session del
caller, igual que el kill-switch. La pausa de quotes sólo admite ausencia inicial,
`clear` o `paused`; otro valor falla y el engine conserva su bloqueo ante error.
Los snapshots de exposición (incluida la consulta de patas hermanas REST), stops
realizados, presupuesto experimental y MTM admiten esa misma Session. No abren una
segunda, no hacen autoflush, commit o rollback y conservan las matemáticas comunes.
Un cambio financiero ORM aún sin flush es desconocido, no se omite del SELECT ni
se escribe desde el lector. El caller adquiere su lock y lee antes de añadir la
nueva reserva; estas funciones por sí solas no adquieren un lock ni concilian cuenta.

Los snapshots de stops y experimento con Session prestada exigen capital explícito
finito y positivo. Eso evita el fallback diagnóstico; todavía corresponde al proveedor
verificar procedencia/frescura/vinculación del capital. Los stops prestados rechazan
settlement sin fecha, fecha futura o rol contable desconocido/contradictorio.
La comparación pura de breach es
compartida con el check async, que conserva en exclusiva alertas y latch. El diario
suave sigue siendo un bloqueo de entradas; no se cambia la política de salidas.

`Trade.accounting_role` identifica quién porta el PnL: `realized_pnl` en BUY
liquidadas y entradas SELL de M5; `exit_allocated` en SELL de salida M2/M3 o rollback
M1/REST cuyo resultado ya está asignado a las BUY cerradas. Esos escritores estampan
el rol dentro del mismo commit del cierre, incluso al dividir una BUY parcial.
Los lectores prestados de stops/experimento no duplican ese resultado ni tratan
todo SELL con PnL NULL como cero. Una marca previa contradictoria aborta el cierre.
La migración sólo añade una columna nullable: no etiqueta ni reescribe historia.
Un settlement legacy sin rol dentro de la ventana consultada sigue bloqueando;
uno sin fecha bloquea aunque pudiera ser antiguo. Antes de instalar hace falta
auditar y atribuir esa historia con evidencia; no existe backfill automático.
Las ramas legacy sin Session prestada conservan su política anterior.

`cash_observation()` conserva `_last_balance_at` y el menor entre cash crudo y
suavizado; no renueva el tiempo al consultar. Es caché local, sin recibo de identidad
de cuenta/entorno, y su fecha actual corresponde al refresco previo existente. El
MTM expone la adquisición del mark más antiguo realmente usado; si no usa ninguno,
devuelve `None`, que no acredita valoración de una cuenta vacía. Ninguna de estas
lecturas convierte cero filas locales en prueba de conciliación completa.

`motor_3_clv.attribution` comparte con el executor las BUY atribuibles por ticker,
lado y política explícita (`entry_origin`, `include_motor1_orphans`), ordenadas FIFO.
Conserva la exclusión de hedges M1 y filas cerradas. Cantidades confirmadas en cero
se excluyen; cantidades desconocidas o contradictorias se rechazan, incluso en
el lado opuesto M1 necesario para determinar orfandad. No basta `count > 0` para
acreditar el inventario. `attributable_quantity` usa la Session prestada y el mismo
guard contra autoflush; el selector usado durante el cierre conserva la transacción
del escritor. Cero es sólo inventario local atribuible: no prueba ausencia de
pending, cartera vacía ni reconciliación de la cuenta.

Siguen sin existir fuentes durables de barrido completo de posiciones/órdenes y
de quotes resting confirmadas. Un `pending` M5 no se convierte en resting por
edad o presencia en memoria: con el adaptador conservador bloquea nuevas quotes.
La composición del proveedor y sus recibos de datos ausentes siguen pendientes.
Estos lectores no autorizan ensamblar ni
instalar controles de Telegram o declarar motores listos.

### Intención durable de cancelación M5

Antes del DELETE de reemplazo normal, el candidato confirma
`Trade.cancel_requested_at_ms` bajo `BEGIN IMMEDIATE`. El helper atómico valida
coid/order_id, estrategia M5 y status pendiente; un fallo impide ese reemplazo.
El retiro protector (`cancel_all` y huérfanas) prepara cada objetivo por separado:
una identidad/status desfasado excluye sólo esa orden. Un fallo de DB o reloj
revoca el dueño sano, intenta enclavar incidente y permite el DELETE de los IDs
ya conocidos, preservando el error hasta terminar los intentos protectores.
No puede declararse cierre sano; un fallo del latch no evita retirar las demás.
La sesión activa durable previa sigue bloqueando el reinicio si la DB cayó.
Sólo terminales íntegros persistidos liberan reservas; un error o ausencia las
conserva. Si el batch observa un fill/cuarentena que no se logra persistir, se
pueden intentar los DELETE protectores restantes, pero ninguna respuesta posterior
de ese retiro libera reservas o rebaja esa evidencia; queda incidente y error al
supervisor para revisión operativa. No hay garantía de retiro cuando red/API/DB fallan ni se evita un fill
que gane la carrera. La intervención operativa independiente sigue pendiente. El error diferido de
una huérfana se propaga después de intentar retirar también las fantasmas; un
latch fallido no impide el DELETE de éstas ni de las siguientes. Esto exige haber
obtenido un barrido válido y leído el ledger: DB ilegible no acredita identidad
ni permite reconstruir ese conjunto. La pasada de fantasmas se realiza antes de
procesar las filas conocidas: un fallo posterior al persistir un fill o un
incidente de esas filas no omite su retiro. Dicho fallo sigue siendo fatal,
conserva las reservas y requiere recuperación operativa.

La primera fecha no se renueva en reintentos ni se elimina. Un `pending` marcado
no cuenta como resting sano aunque el barrido posterior lo vea resting; el camino
de huérfanas puede seguir intentando retirarlo conforme a la política anterior.
Una respuesta terminal íntegra y persistida cambia status y conserva el marcador
histórico. No se liberan reservas por edad, ausencia o reinicio. Las fantasmas sin
ledger conservan el incidente durable y nunca acreditan un barrido limpio: no se
crea una Trade ficticia para ellas. NULL legacy no prueba ausencia histórica de
cancelaciones y la migración no inventa fechas. Esto prepara una condición necesaria
para recibos de resting; no implementa esos recibos ni valida identidad de cuenta.

Los executors conservan el camino anterior cuando se construyen sin adaptador. Si se
les pasa uno, fallo/ausencia de permiso o DB nunca vuelven al camino anterior. Falta que
el ensamblado de runtime valide que todos los executors esperados reciben el mismo
binding y que sus capas A/C y techo boot coinciden. No hay env var de activación nueva.

## Evidencia esperada antes de conexión

Pruebas con SQLite temporal y red externa bloqueada cubren CAS concurrente, pausa vs
reserva, reinicio/ACK perdido, expiración/frescura, corrupción de permiso, reemplazo del
ledger, techo/binding, salidas con exposición, capacidad finita y rollback de orden,
atribución y presupuesto. El cliente de órdenes es ficticio. Faltan proveedor real,
ensamblado de runtime, transporte aislado, UI/confirmación Telegram y recorrido Demo en
entorno confirmado. No instalar ni afirmar preparación de los motores a partir de estas
pruebas; la IOC Demo UNKNOWN previa conserva su reserva.
