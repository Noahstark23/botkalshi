# Recibos de posiciones y órdenes recientes

Preparación opcional para el futuro proveedor de hechos de permisos. No se invoca
desde el runner, Telegram, el poller o las migraciones. No provisiona cuentas,
credenciales, servicios, flags ni bases existentes al importar. No acredita que
los motores estén listos: falta composición con GateFacts, cash, dependencias,
atribución, transporte y validación de las fuentes del entorno autorizado.

## Adquisición y persistencia

`FinancialReceiptStore.collect` recorre posiciones y órdenes recientes con
subcuenta explícita. Comprueba host del wrapper y transporte, entorno, identificador
del firmante y referencia de credencial antes/después de cada página. El binding
incluye referencia de evidencia de cuenta, SHA e instancia. Esa referencia es
declarada: `account_identity_verified` sigue siendo falso.

Antes de cualquier GET confirma `COLLECTING`, incrementa generación y borra el
payload anterior. Sólo confirma `COMPLETE` si ambos recorridos terminan con cursor
vacío explícito, sin ciclos, duplicados, contradicciones o cambios locales. Incluye
los tiempos originales, metadatos/digests de páginas y filas saneadas en un solo
registro durable; el lector verifica el hash del texto almacenado. Un fallo,
cancelación o resultado de una generación anterior no restaura el recibo viejo.

Los límites son 100 páginas entre ambos recorridos, 100 filas por página, 8 MiB
acumulados de JSON codificado y 30 segundos desde el inicio. No son un límite de
bytes del transporte antes de deserializar ni un conteo de reintentos GET internos
del cliente. También se limita el payload persistido. Se comparan reloj de pared
y monotónico; la vigencia restante se consume durante la adquisición. Un barrido
lento o una cuenta con muchas órdenes puede no producir recibos. No se filtran
órdenes terminales para forzar un pase ni se amplían límites automáticamente.

La caché es propia del recibo. No reemplaza ni certifica `PortfolioPosition`, no
purga posiciones por una respuesta vacía, no reconcilia trades/reservas y no libera
UNKNOWN. `historical_orders_complete` y `remote_atomic` permanecen falsos: obtener
todas las páginas del endpoint reciente no acredita archivo histórico ni una
captura simultánea del estado remoto. Sin fixture saneado del entorno concreto,
la compatibilidad real del formato sigue pendiente. Los tests usan datos ficticios.

## Revisión monotónica del ledger

`financial_fence` cubre INSERT/UPDATE/DELETE de trades, compromisos de riesgo,
posiciones, admisiones y estado operativo. Incluye todas sus columnas, también
notas/cuarentenas y actualizaciones sin cambio de valor. La secuencia detecta ABA
local; rollback revierte también la secuencia. Se verifican DDL, definiciones y
conjunto exacto de triggers y versión de esquema. No es protección contra un
administrador que restaure/manipule deliberadamente la base.

La lectura usa la Session y transacción del caller sin autoflush, commit ni red;
rechaza entidades financieras sin flush. La futura admisión debe efectuarla dentro
de su `BEGIN IMMEDIATE`. Cualquier escritura vigilada invalida el recibo global:
una admisión bid seguida de ask M5 necesita resolver esa composición primero.
Tampoco se acredita disponibilidad con el churn de M3/poller. No se conecta este
recibo directamente como `gates_clear`. Al saturar la secuencia, se bloquean recibos
pero continúan las escrituras protectoras, sin resetear el contador.

`rebuild_db.py` conserva ambas tablas, copia datos antes de crear triggers y verifica
sus definiciones y contenido. No intercambia la DB ni inicia servicios. El protocolo
de reconstrucción requiere escritores detenidos; no es una copia coherente de una
base viva. Una instancia vieja rechaza sustitución del archivo por inode.

## Reinicio y migración explícitos

Abrir con un SHA/nonce distinto bloquea read/collect/provision; no se reutiliza el
nonce viejo para hacer pasar el reloj monotónico. Con escritores detenidos,
`rebind_instance(previous, expected_generation)` permite una transición CAS del
binding completo anterior al nuevo, conservando entorno/host/credencial/cuenta/
subcuenta. Vacía el recibo, avanza generación y conserva ledger/secuencia/reloj.
Una adquisición de la instancia anterior no puede publicar después de la transición.
No hay CLI ni llamada automática; la autorización y provisión de un despliegue
deben resolver quién puede ejecutarla. Otra cuenta/subcuenta no se acepta por esta vía.

Una migración altera el sello DDL y bloquea hasta revisión. Dentro de una transacción
`BEGIN IMMEDIATE`, con escritores detenidos, `accept_reviewed_schema` puede aceptar
el hash exacto revisado de las tablas, comprobando época/secuencia previa y triggers
íntegros. Conserva época, incrementa secuencia y recrea/verifica la guarda dentro de
la misma transacción. No repara triggers perdidos. El caller debe hacer rollback
ante cualquier excepción; no confirmar parcialmente. El hash esperado debe venir
de la migración revisada, nunca calcularse de la base instalada para aprobarla a
ciegas. Los recibos viejos permanecen inválidos y hace falta una adquisición nueva.

No hay recuperación automática de reloj adelantado, identidad estable cambiada,
contador/generación agotados, tablas/recibos perdidos o triggers ausentes. Esos
procedimientos requieren revisión separada, preservando evidencia e incertidumbre.
