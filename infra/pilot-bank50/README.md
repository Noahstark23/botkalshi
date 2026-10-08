# Bank50 en el runtime de DigitalOcean

Esta unidad sustituye la unidad de standby del mismo runtime, conservando usuario,
directorio de estado, puerto loopback y límites de recursos. No se ejecuta un
segundo proceso contra la misma DB. La instalación inicial conserva todos los
motores y órdenes apagados, `EXPERIMENT_BANK_CONFIRMED_USD=0` y Bank50 habilitado.

`check_bank50_deployment` valida la configuración efectiva sin leer `.env`, sin
consultar DB ni red. `Settings` puede comprobar el archivo PEM o parsear material
entregado por entorno; la salida nunca contiene valores secretos. Se fijan
entorno/endpoints de producción, DB y health loopback, y se rechazan los motores
ajenos al perfil, consumo de Odds y Telegram y un armado parcial de M1. El chequeo
no prueba saldo, tarifas, autenticación ni rentabilidad.

La fecha de preparación que se escriba en `EXPERIMENT_START_AT` permite validar
la configuración apagada; no representa el comienzo de operaciones financieras.
El titular debe establecer la fecha efectiva si más adelante inicia su ensayo.
El presupuesto histórico no se reinicia cambiando esa fecha.

## Instalación con órdenes apagadas

1. Verificar el SHA y árbol limpio, tests offline y revisión de las rutas apagadas.
2. Guardar copia consistente mediante SQLite backup API, configuración y unidad
   anteriores. Los backups con configuración permanecen privados en el servidor.
3. Probar dos veces las migraciones sobre una copia sin credenciales ni red y
   comparar todas las columnas preexistentes; conservar el resultado.
4. Instalar el release inmutable y dependencias Linux/Python 3.12 fijadas con hashes.
   Una venv dedicada puede estar enlazada desde `.venv` dentro del release.
5. Combinar el perfil de `review.env.example` con la configuración privada existente.
   Mantener todas las ejecuciones/ciclos apagados, ack0 y proveedores extra vacíos;
   escribir una fecha de preparación válida. No modificar ni mostrar las claves.
6. Ejecutar el preflight con el mismo usuario y entorno que cargará systemd; validar
   la unidad con `systemd-analyze verify`. No iniciar si alguno falla.
7. Detener sólo `botkalshi-runtime.service`, obtener otro backup consistente,
   cambiar release/configuración/unidad, hacer daemon-reload y arrancar una vez.
   Conservar el servicio research y su presupuesto por separado.
8. Verificar SHA del directorio del proceso, flags efectivos, health/readiness,
   WebSocket, capital, DB y ausencia de nuevas órdenes locales. Observar recursos
   y reinicios durante al menos 15 minutos; registrar errores y limitaciones.

`STANDBY_REVIEWED` conserva su nombre anterior por compatibilidad del despliegue;
no es una nueva autorización ni certifica operación real.

## Activación posterior del titular

La unidad admite una configuración Bank50 armada sólo si pasa las validaciones
del perfil: ack50, M1 de análisis y ejecución habilitados junto con trading, y
todos los demás ejecutores apagados. No cambia flags automáticamente. Este paquete
se despliega inicialmente apagado; la activación financiera corresponde al titular.

Los pendientes de tarifas efectivas, costes de rollback, conciliación y pruebas
conectadas del perfil están descritos en `docs/pilot_bank50_preparation.md`.
Una instalación sana no cierra esos pendientes. El saldo real puede estar por
debajo del piso y bloquear entradas aunque el proceso esté activo.

## Reversión técnica

Detener primero el runtime. Restaurar su unidad/configuración anteriores desde el
backup privado y el enlace al release anterior; verificar que todos los flags de
órdenes y ciclos están apagados antes de arrancar. Conservar la DB actual: las
migraciones agregan columnas/tablas/índices y no borran datos históricos. No
restaurar automáticamente una DB antigua, porque eliminaría registros posteriores.
La versión anterior sólo se usa apagada; no entiende la nueva política ni todas
las cantidades exactas. Una reversión de código tampoco cancela órdenes externas
ni cierra posiciones.

Si falla el arranque o se observan flags inesperados, detener el runtime y revisar;
no repetir reinicios para saltar un bloqueo ni limpiar el kill-switch durable.
