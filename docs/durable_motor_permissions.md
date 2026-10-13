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

**Ese proveedor del runtime aún no está implementado.** Los tests lo sustituyen por
hechos ficticios explícitos. Por tanto, el backend SQLite y los hooks probados no
acreditan una fuente financiera conectada ni control operativo. Ningún scope se registra
automáticamente a partir de flags. M2 fair y M6/M8/M9 no son admitidos por este backend;
US500 conserva su permiso paper independiente.

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
