# Recolector público separado

`botkalshi-us500-data.service` ejecuta un paso del plan inmutable. No es un
ejecutor de inversiones ni un receptor Telegram. No tiene credenciales,
acceso a bases financieras, control de motores ni capacidad de enviar órdenes.

Requiere usuario propio sin shell, código publicado verificado, plan JSON
root:root de sólo lectura en `/etc/botkalshi-us500-data/plan.json` y directorio
de estado exclusivo `/var/lib/botkalshi-us500-data/session`.

El operador instala un timer de fechas **concretas**, sólo dentro de la ventana
del plan, a una llamada por minuto. `Persistent=false`: una caída no dispara
recuperación de consultas pasadas. El código decide el slot usando hora UTC,
reserva presupuesto antes de HTTP y no vuelve a consultar un slot interrumpido.
Después del final se permite una invocación local sin red para consolidar huecos.
No usar un timer diario indefinido ni aumentar presupuesto de una DB existente.

La hora del servidor debe estar sincronizada. Inspeccionar antes recursos,
servicios ajenos y hashes. Correr `verify_offline.py` sin red antes de instalar.
Verificar `systemd-analyze verify`, permisos y una invocación previa a apertura
sin GET, si todavía hay tiempo. Confirmar capturas y cobertura leyendo `status.json`
sin arrancar otro proceso. Si se llegó tarde, registrar las muestras perdidas.

Para parar: detener el timer y el servicio **de datos**. Conservar plan, journal,
capturas y estados inciertos. No tocar `botkalshi-runtime`, `botkalshi-research`,
`botkalshi-panel`, Coolify ni el receptor Mac retirado. El límite de disco lógico
se aplica antes de cada consulta; límite CPU/memoria y timeout los aplica systemd.

Una ventana cubierta a un minuto no prueba un flujo continuo ni una sesión del
exchange completa. Contrastar apertura/cierre observados y huecos. No inventar
funding, costes, libro o marcas faltantes para fabricar un replay exitoso.
