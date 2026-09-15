# RUNBOOK — despliegue verificable del colector y reportes

Estado de esta entrega: código preparado y pruebas locales; no instalación remota. Reutiliza el Droplet dedicado existente, sin nuevas contrataciones. No arranca `src.runner`, motores de órdenes ni APIs pagadas. Una referencia a Boston no reduce las verificaciones necesarias.

## Correcciones al instalador anterior

El instalador anterior aceptaba la presencia de un health.json sin comprobar edad, error, ID del ciclo o correspondencia con el paquete. Podía aceptar evidencia vieja. También incluía `rm -rf` sobre el checkout y `checkout -f`.

El nuevo requiere SHA completo, usa releases separadas, preserva checkouts/datos, prueba módulos y exige heartbeat y paquete generados después del reinicio, con el mismo ciclo y sin errores. Al no verificarse en 180 segundos, detiene el servicio y conserva datos. No modifica firewall o SSH; su endurecimiento debe revisarse expresamente en la máquina correcta. No declara probado systemd por pasar tests en otro host.

## Antes de instalar

Consola autorizada del Droplet dedicado Ubuntu 24.04, no servidor Nortex. Inspeccionar procesos y datos existentes; no borrar base o volumen. No pegar contraseñas, PEM ni tokens en el chat. Si existe configuración de APIs o cambios locales, el instalador se detiene para revisión en lugar de reutilizarlos.

Desde una copia revisada de la rama, fijar la versión real (SHA de la revisión que incluya reporting.py). No copiar un SHA inventado ni ejecutar desde un main que no incluye estos archivos.

```bash
REV="$(git rev-parse HEAD)"
bash infra/digitalocean-shadow/install.sh "$REV" --dedicated-research-host
```

El comando solo corresponde a la consola del Droplet. No se ha ejecutado allí en esta sesión. Descarga código y paquetes de sistema, no adquiere proveedores de datos. No usar el comando anterior de eliminar `/tmp/botkalshi-bootstrap` a ciegas.

## Verificación independiente posterior

En la consola, consultar unidad y después el verificador desde la release exacta:

```bash
systemctl is-active botkalshi-research.service
python3 /opt/botkalshi-research/releases/"$REV"/infra/digitalocean-shadow/reporting.py \
  --data /var/lib/botkalshi-research --max-age 180
```

`CAPTURE_VERIFIED_LOCAL` confirma coherencia/edad de esos archivos locales, NO estrategia validada, mercado totalmente barrido, quotes ejecutables, M2/M5 activos, saldo conciliado o IA conectada. Lecturas malformadas, futuras, viejas, ausentes o de otro ciclo producen BLOCKED/código 3.

## Reportes

```bash
runuser -u botkalshi -- python3 /opt/botkalshi-research/releases/"$REV"/infra/digitalocean-shadow/reporting.py \
  --data /var/lib/botkalshi-research --output /var/lib/botkalshi-research/drafts
```

Cada ejecución crea un bundle nuevo con status.json, status.md, whatsapp_draft.txt y muse_brief.md. Consumir solo carpetas con COMPLETE. No manda mensajes, no agenda envíos, no conecta cuentas y no llama a una IA. La integración recurrente se habilita únicamente tras acceso del proveedor, remitente/destinatario, cumplimiento y presupuesto verificados; no hay una tarea de WhatsApp creada por este MD.

## Límites del colector actual que no arregla reporting.py

Una sola serie KXMLBGAME, respuesta acotada y sin barrido multiactivo íntegro; `close_time` no prueba evento prepartido. Faltan validación de reglas/fees, precio ejecutable, timestamps por libro, matching y ranking. No utilizar la hora de empaquetado para rejuvenecer referencias cacheadas. APIs de pago siguen sin habilitar; el presupuesto persistente de cuota debe implementarse antes de usarlas. No presentar ausencia de eventos en el paquete como ausencia oficial en una liga.

## Parada y costos

`systemctl stop botkalshi-research.service` detiene el proceso del laboratorio, no borra datos. No elimina el Droplet ni cancela su facturación. Un servidor activo sin captura tiene costo, no evidencia de trabajo. No se ha apagado/eliminado ningún recurso en esta entrega.
