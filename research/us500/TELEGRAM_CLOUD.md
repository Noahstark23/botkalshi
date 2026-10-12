# Telegram en el servidor

Receptor independiente del Mac. Servicio nuevo `botkalshi-panel.service`, usuario
sin privilegios `botkalshi-panel`, código `/opt/botkalshi-panel/current`, estado
`/var/lib/botkalshi-panel`. No depende de una sesión SSH abierta.

Alcance: menú de todos los motores, copia saneada de estados del runtime
técnico del droplet, y permiso de simulación US500. Desde V2.7 el panel lee
`/var/lib/botkalshi-status/status.json`; el productor separado consulta
`/health`, `/status` y `/stats/edges?days=1`. No contiene un ejecutor,
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
respuestas cuya entrega quedó UNKNOWN. Si quedan mensajes fallidos o respuestas ambiguas,
el receptor continúa atendiendo nuevas consultas en DEGRADED, con paper pausado.
Una excepción es el rechazo explícito del ACK de un botón vencido: se conserva
como FAILED diagnóstico sin bloquear recepción ni paper; nunca se reintenta.
Un ACK ambiguo (UNKNOWN) sigue bloqueando. No borra esas evidencias. Una intervención posterior debe resolver el incidente.
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

Validación del host: systemd255 entrega credenciales descifradas root:root0440
con acceso por ACL al usuario del servicio, dentro de un montaje de sólo lectura.
El lector admite0440 sólo bajo `/run/credentials/<unidad>/telegram-token`, con el
directorio exacto indicado por systemd y montaje de sólo lectura. Rechaza archivos
ordinarios0440, escritura por grupo y lectura pública. La primera validación de
modo y luego la suposición del grupo del servicio fallaron antes de polling. La
corrección fue comprobada con getMe/getWebhookInfo en el propio host: PREFLIGHT_OK.

## Actividad registrada (V2.4, transporte actualizado en V2.7)

Desde V2.7 el productor sin token consulta `/health` y `/status` cada15s;
`/stats/edges?days=1` sólo tras obtener estado válido, como máximo cada300s,
incluyendo fallos. Las consultas no dependen de mensajes de Telegram. La caché se revalida por fecha y caducidad;
no conserva un dato anterior después de un fallo. Un reinicio comienza una caché
nueva. Ventana: inicio UTC del día anterior hasta la fecha de generación.

M6/REST/M8/M9 muestran cantidad de registros de linemove/multi_outcome/ofi/spillover.
No indican tareas activas, flags ni órdenes: esos campos continúan desconocidos.
Cero filas puede incluir ausencia de señales o retención; no demuestra apagado.
No se consulta /stats/daily ni se escanean archivos/DBs desde el receptor.


## Correcciones V2.7

- TG-01: un update válido mayor de32KiB consume únicamente ID/hash, sin guardar
  contenido, llamar al control ni responder. El siguiente update puede continuar.
  El límite HTTP global y el rechazo de IDs inválidos/conflictivos permanecen.
- TG-03: HTTP429 con retry_after entero entre1 y86400s pausa paper y espera el
  plazo del proveedor (mínimo backoff local), con heartbeat durante la espera.
  Continúa getUpdates. Si ocurrió al enviar, conserva el mensaje como FAILED y
  no lo reenvía; posteriores consultas funcionan pero un sendMessage fallido
  bloquea paper hasta revisión. UNKNOWN nunca se reintenta ni elimina.
- TG-04: retroceso de reloj conserva pausa y cursor, invalida confirmaciones y
  reintenta con backoff hasta que el reloj alcance el último tiempo aceptado.
  No rebaja las comprobaciones temporales ni mueve timestamps hacia atrás.
- TG-05: /health503 sólo se acepta con envoltorio detail/status=unhealthy y
  timestamp válido; permite mostrar la pausa indicada por /status. Fresh significa
  lectura reciente, no salud ni actividad individual. Otros503 son desconocidos.
- TG-02: usuario productor separado, sin token Telegram, publica JSON saneado
  con TTL. El consumidor no puede escribirlo; rechaza symlinks, permisos inseguros,
  archivos sobredimensionados y caché vencida. systemd bloquea al panel loopback,
  link-local y redes privadas salvo DNS127.0.0.53. Requiere BPF/cgroup operativo:
  validar denegación TCP al runtime y conectividad Telegram antes de afirmar cierre.
  El productor sólo usa GET fijos, pero no añade autenticación al runtime; esa
  superficie del servicio financiero requiere una revisión separada.

Instalación V2.7: crear usuario botkalshi-status y directorio0755, instalar y
validar ambas unidades. Exportador sin credenciales financieras ni Telegram,
StateDirectory propio, CPU5%, memoria48MiB. Arrancar primero exportador; comprobar
caché fresca, luego detener sólo panel, respaldar control/outbox/cursor actuales,
actualizar su release/unidad y arrancar una vez con su token cifrado existente.
Conservar bases y registros inciertos; no copiar un cursor antiguo. La Mac sigue
retirada. La captura y runtime/research conservan unidades, releases y procesos.

La separación no garantiza disponibilidad perfecta ni estados que el runtime no
publica. M6/REST/M8/M9 siguen con flags/tareas individuales desconocidos. Cualquier
función real continúa fuera del panel y de esta reparación.

Referencia: [Telegram ResponseParameters](https://core.telegram.org/bots/api#responseparameters).

## Revisión diferencial V2.7.1

TG-07: ambas unidades añaden PrivateUsers, ProtectHostname, SystemCallFilter
@system-service, SystemCallArchitectures=native y ocultan /data/coolify y
/var/lib/docker. El panel también deniega 143.110.151.94/32, dirección pública
del host verificada el12oct2026. Revalidar las direcciones propias al migrar;
esto no es una lista exclusiva de destinos Telegram. Validar filtros efectivos
y lectura de caché después del reinicio aislado.

TG-06 queda como limitación explícita: lectura ordenada de10 updates por ciclo,
sin prioridad especial de pausa dentro de un backlog. Durante429 el permiso ya
está pausado y no existe runner automático. Respetar retry_after del proveedor,
incluso si supera300s: recortarlo provocaría consultas antes del plazo autorizado.
Una futura integración de ejecución requeriría su propio control de parada;
este panel no ofrece esa garantía ni está autorizado a controlar trading real.
