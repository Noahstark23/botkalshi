# Telegram en el servidor

Receptor independiente del Mac. Servicio nuevo `botkalshi-panel.service`, usuario
sin privilegios `botkalshi-panel`, código `/opt/botkalshi-panel/current`, estado
`/var/lib/botkalshi-panel`. No depende de una sesión SSH abierta.

Alcance: menú de todos los motores, lectura de `/health` y `/status` del runtime
técnico del droplet, y permiso de simulación US500. No contiene un ejecutor,
estrategia automática, shell por Telegram ni acceso a credenciales financieras.
No controla Coolify ni habilita operaciones reales. Un flag de configuración
no demuestra que la tarea esté corriendo; el panel distingue esos conceptos.
M6/M8/M9 y algunos campos de otros motores no están expuestos por el runtime:
aparecen como desconocidos. Los errores y datos vencidos no se presentan como OFF.

## Funcionamiento continuo

`us500_telegram_cloud.py` usa el modo persistente explícito del supervisor.
El supervisor acotado anterior conserva sus valores por defecto y contrato.
El receptor cloud no tiene límite de ciclos ni el plazo del conector local.
systemd lo inicia al arrancar el servidor y reinicia caídas inesperadas con demora.
Una caída de red durante el preflight devuelve75, mantiene paper pausado y permite
el reinicio demorado. No hay límite de intentos por outage; errores definitivos no
entran en ese lazo. Los fallos definitivos de identidad, configuración o base terminan con código78
para evitar reinicios ciegos. Esos incidentes requieren revisión.

Los timeouts y errores transitorios de la consulta `getUpdates` pausan el permiso
paper y reanudan la lectura con esperas 3/6/12/24/48/60 segundos. Nunca reenvían
respuestas cuya entrega quedó UNKNOWN. Si quedan respuestas ambiguas o fallidas,
el receptor continúa atendiendo nuevas consultas en DEGRADED, con paper pausado.
No borra esas evidencias. Una intervención posterior debe resolver el incidente.
Este proceso no inicia ningún replay; el permiso es sólo una condición de acceso.

La poda transaccional conserva los500 updates más recientes y elimina únicamente
historial anterior al cursor totalmente entregado; preserva toda entrega pendiente,
fallida o ambigua. Los updates antiguos siguen bloqueados por el cursor durable.
Un desborde causado por incidencias sin resolver se detiene explícitamente.

## Instalación y migración

1. Verificar host y que los antiguos consumidores de este bot estén detenidos.
   Nunca iniciar dos `getUpdates` con el mismo token.
2. Publicar y verificar la versión aislada; ejecutar el módulo offline y Ruff0.14.0.
3. Crear usuario/código/estado propios; no editar servicios ni entornos financieros.
4. Detener el receptor Mac, comprobar pausa durable y bases sanas. Copiar mediante
   SQLite backup las dos bases conservando identidad, cursor y outbox. No copiar
   la DB de trading ni archivos `.env`. Mantener el original local archivado.
5. Transferir el token únicamente por stdin de SSH autenticado y cifrarlo con
   `systemd-creds`, nombre `telegram-token`, en
   `/etc/botkalshi-panel/telegram-token.cred`. Nunca usar argumentos, logs o Git
   para secretos. `LoadCredentialEncrypted` entrega el secreto descifrado en el
   directorio de credenciales del servicio en memoria.
6. Verificar la unidad y activar **sólo** `botkalshi-panel.service`. La credencial
   debe corresponder al bot esperado y no debe existir webhook. No se modifica
   webhook, token, notificador Nortex ni servicio de trading.
7. Probar `/motores`, detalles y controles paper con la Mac sin consumidor.
   Reiniciar únicamente este receptor y verificar cursor/identidad/pausa.

## Diagnóstico y reversión

`systemctl status botkalshi-panel.service` y
`/var/lib/botkalshi-panel/telegram-service-status.json` muestran heartbeat,
fase, código de error saneado y contadores. RUNNING es recepción disponible;
RECOVERING es fallo transitorio; DEGRADED conserva una respuesta no confirmada.
La instalación para operación continua no demuestra disponibilidad de24horas.

Para revertir, detener/deshabilitar exclusivamente `botkalshi-panel.service`.
Conservar sus bases. No volver al receptor Mac con una copia vieja del cursor:
respaldar el estado actual del cloud y migrarlo primero. No ejecutar ambos.
No reiniciar ni tocar `botkalshi-runtime`, `botkalshi-research` o Coolify.

Referencia de entrega/cursor: [Telegram Bot API getUpdates](https://core.telegram.org/bots/api#getupdates).

Validación del host: systemd255 entrega credenciales descifradas root:grupo-del-servicio
con modo0440. El lector admite esa combinación exacta; rechaza otro grupo, permisos
públicos o escritura por grupo. El primer arranque falló antes del polling por una
validación que sólo admitía0400/0600; se corrigió con regresiones específicas.
