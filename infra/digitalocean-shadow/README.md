# DigitalOcean: investigación, IA y avisos técnicos

Este paquete opera en modo `SHADOW_READONLY`: no contiene capacidad para comprar,
vender, cancelar órdenes ni mover dinero. `collector.py` captura una serie de
mercados públicos y produce paquetes; no es un barrido multiactivo completo.
`reporting.py` crea borradores y `cycle_verifier.py` comprueba que health, packet,
coverage y risk pertenecen al mismo ciclo.

`assistant_bridge.py` es el canal acotado para un evaluador de IA. Puede leer un
ciclo verificado, escribir una evaluación estructurada y activar una pausa
defensiva. `telegram_notifier.py` envía únicamente estado técnico y decisiones
permitidas. Ninguno recibe claves de Kalshi, dispone de herramientas de orden o
puede habilitar trading real. Ver [puente del asistente](ASSISTANT_BRIDGE.md).

## Estado y documentación

Leer [memoria operativa](../../docs/operations/BOT_MEMORY.md), [runbook](../../docs/operations/RUNBOOK.md), [integraciones](../../docs/operations/INTEGRATIONS.md) y [contenido](../../docs/operations/CONTENT.md).

Una VM activa, un commit publicado o tests locales no demuestran que una versión
esté operando en Ubuntu/systemd. La integración IA+Telegram sólo se considera
instalada después de ejecutar su instalador, completar sus dos pruebas externas y
comprobar sus timers. La documentación no sustituye esa verificación remota.

## Instalación revisada

El comando antiguo sin argumentos queda sustituido. Desde un checkout revisado, en la consola del Droplet dedicado Ubuntu 24.04:

```bash
REV="$(git rev-parse HEAD)"
bash infra/digitalocean-shadow/install.sh "$REV" --dedicated-research-host
```

El instalador usa `/opt/botkalshi-research/releases/$REV`, conserva directorios existentes, ejecuta tests y requiere nueva captura después del arranque. Si no se verifica en 180 segundos, detiene el servicio sin borrar datos. No configura firewall/SSH ni añade claves. APIs pagadas requieren revisión aparte y no se habilitan por esta instalación.

Para verificar y producir borradores: seguir RUNBOOK.md. No usar el compose/runner legacy ni copiar claves históricas.

## Instalación independiente de IA y Telegram

La integración se instala aparte del collector. Requiere que
`botkalshi-research.service` ya esté activo en el Droplet dedicado Ubuntu 24.04,
una revisión de 40 caracteres disponible en el repositorio y una consola root con
TTY. Desde un checkout de la revisión que se quiere instalar:

```bash
REV="$(git rev-parse HEAD)"
bash infra/digitalocean-shadow/install-assistant-integrations.sh \
  "$REV" --dedicated-research-host --simulation-only
```

`--simulation-only` es una confirmación obligatoria. Además, el instalador ejecuta
internamente `resume-simulation --confirm SIMULATION_ONLY`; sólo habilita
evaluaciones de investigación. No cambia flags de ejecución, no concede autoridad
financiera y no reinicia ni reconfigura el collector. Antes y después compara el
PID y `ExecStart` de `botkalshi-research.service`.

Si aún no existen, el instalador solicita en la consola la clave de OpenAI, el
modelo, el token de Telegram y el chat ID numérico. La clave y el token se leen de
forma oculta. No pegarlos en el chat, en el repositorio, en variables exportadas ni
como argumentos del comando.

Los secretos no se cargan como variables de entorno del servicio. Quedan en
directorios `root:root` con modo `0700` y archivos `root:root` con modo `0600`:

- `/etc/botkalshi-assistant/openai_api_key`.
- `/etc/botkalshi-telegram/telegram_bot_token`.
- `/etc/botkalshi-telegram/telegram_chat_id`.

systemd los entrega a cada proceso mediante `LoadCredential`. El modelo, que no
es secreto, es la única configuración de `/etc/botkalshi-assistant.env` como
`BOTKALSHI_OPENAI_MODEL`.

El instalador crea y conserva tres identidades separadas:

| Proceso | Usuario | Configuración/credenciales | Estado escribible |
|---|---|---|---|
| Collector | `botkalshi` | `/etc/botkalshi-research.env` | Datos generales de investigación |
| Evaluador IA | `botkalshi-ai` | Modelo en `/etc/botkalshi-assistant.env`; clave por `LoadCredential` | `/var/lib/botkalshi-integrations/assistant` |
| Notificador | `botkalshi-telegram` | Token y chat ID por `LoadCredential` | `/var/lib/botkalshi-integrations/telegram` |

Antes de cada ejecución, `prepare-integration-access.sh` concede lectura sólo a
`health.json`, `coverage.json`, `risk-status.json` y `packets/latest.json`. Las
unidades también ocultan la configuración y las credenciales ajenas a cada
proceso.

Las unidades instaladas son:

- `botkalshi-assistant.service` con `botkalshi-assistant.timer`: evaluación OpenAI
  15 minutos después de que termine la anterior, con demora inicial de 2 minutos
  y jitter de hasta 60 segundos.
- `botkalshi-telegram.service` con `botkalshi-telegram.timer`: comprobación cada
  5 minutos después de que termine la anterior, con demora inicial de 90 segundos
  y jitter de hasta 30 segundos.

Ambos timers usan `Persistent=false`; no recuperan en ráfaga ciclos omitidos. La
comprobación de Telegram no implica un mensaje cada cinco minutos: persiste una
huella en `/var/lib/botkalshi-integrations/telegram/telegram-state.json` y suprime
estados sin cambios. Envía recuperación (“Botkalshi volvió”), bloqueos técnicos
nuevos, evaluaciones nuevas distintas de `NO_ACTION` y cambios de salud de IA
entre `MISSING`, `STALE`, `OFFLINE` y `ACTIVE`. Una evaluación se considera fresca
durante un máximo de 35 minutos.

Cada ejecución satisfactoria de `botkalshi-assistant.service` activa el chequeo de
Telegram; un fallo de esa unidad activa
`botkalshi-telegram-ai-failure.service`, que emite una alerta saneada y deduplicada.
Cuando la IA vuelve a `ACTIVE`, el modo automático emite la recuperación. La
unidad `botkalshi-telegram-recovery.service` se usa sólo durante la instalación
para forzar y comprobar el mensaje inicial.

Durante la instalación se ejecuta primero una evaluación real con proveedor
`openai` y después una entrega real a Telegram. Los timers sólo se habilitan si
ambas terminan bien, Telegram devuelve un `message_id` válido y el collector
conserva el mismo PID y `ExecStart`. No asumir éxito hasta observar al final:

```text
INTEGRATIONS_OPERATIONAL=true
AI_PROVIDER=openai
AI_INTERVAL=15min
TELEGRAM_INTERVAL=5min
TELEGRAM_INITIAL_RUN=delivered
COLLECTOR_EXECSTART_UNCHANGED=true
EXECUTION_AUTHORIZED=false
REAL_TRADING=false
```

Verificación posterior que no muestra secretos:

```bash
systemctl is-active botkalshi-research.service
systemctl is-active botkalshi-assistant.timer botkalshi-telegram.timer
systemctl is-enabled botkalshi-assistant.timer botkalshi-telegram.timer
systemctl list-timers botkalshi-assistant.timer botkalshi-telegram.timer --no-pager
journalctl -u botkalshi-assistant.service -u botkalshi-telegram.service -n 80 --no-pager
```

## Qué falta

Matching, reglas, fees, quotes ejecutables, fuentes independientes, timestamps por libro, presupuesto persistente de proveedores y evaluación del experimento. Un reporte `CAPTURE_VERIFIED_LOCAL` confirma solamente esos archivos, no rentabilidad, una llamada de IA ni entrega a Telegram. Un paquete o borrador no equivale a mensaje enviado, canal creado o video publicado.

El lector de cuenta está separado en `scripts/read_research_account.py`. Sólo ejecuta GET de balance, posiciones y fills, y produce evidencia saneada con `reconciled=false`; no habilita dinero real. Integrar un ledger conciliado, reservas, posiciones UNKNOWN y pausas persistentes sigue siendo requisito antes de cualquier discusión de ejecución.
