# Puente botkalshi ↔ asistente

Estado: implementación preparada para instalación independiente. No se debe afirmar
que está desplegada u operativa hasta ejecutar y verificar
`install-assistant-integrations.sh` en el host. El puente administra observación y
pausas de la capa de análisis; **no administra dinero ni órdenes**.

## Contrato de seguridad

- Lee cuatro archivos del mismo ciclo: `health.json`, `packets/latest.json`, `coverage.json` y `risk-status.json`.
- Rechaza ciclos mezclados, viejos, futuros, incompletos, con conteos incompatibles o flags de autoridad que no sean el booleano `false`.
- La IA no recibe `KALSHI_API_KEY_ID`, clave privada ni variables de ejecución.
- Salida obligatoria mediante JSON Schema. El bot vuelve a validar el resultado localmente.
- Decisiones permitidas: `NO_ACTION`, `PAUSE_RESEARCH`,
  `REQUEST_HUMAN_REVIEW` y `REFRESH_PUBLIC_SNAPSHOT`. Los comandos estructurados
  correspondientes siguen limitados a `NOOP`, `PAUSE_RESEARCH`,
  `REQUEST_HUMAN_REVIEW` y `REFRESH_PUBLIC_SNAPSHOT`.
- Sólo `PAUSE_RESEARCH` puede aplicarse automáticamente. Reanudar requiere al operador y la confirmación literal `SIMULATION_ONLY`.
- No existen comandos de compra, venta, cancelación, transferencia, shell ni modificación de riesgo.

## Separación en el host

El collector permanece como `botkalshi`. Las integraciones usan dos identidades
sin login, estado separado y credenciales independientes:

| Integración | Usuario systemd | Configuración y credenciales | Frecuencia |
|---|---|---|---|
| Evaluación OpenAI | `botkalshi-ai` | Modelo en `/etc/botkalshi-assistant.env`; clave por `LoadCredential` | Cada 15 minutos tras finalizar |
| Avisos Telegram | `botkalshi-telegram` | Token y chat ID por `LoadCredential` | Cada 5 minutos tras finalizar |

`botkalshi-assistant.service` no recibe token de Telegram y
`botkalshi-telegram.service` no recibe clave OpenAI. Ambas unidades eliminan del
entorno las credenciales de Kalshi, fijan todos los flags de ejecución en `false`
y no exponen capacidad de orden, cancelación, movimiento de dinero o shell.

`prepare-integration-access.sh` refresca ACL de lectura únicamente para los cuatro
artefactos verificados que reemplaza atómicamente el collector. El evaluador
escribe en `/var/lib/botkalshi-integrations/assistant`; el notificador sólo escribe
en `/var/lib/botkalshi-integrations/telegram`. Ambos leen los insumos técnicos de
`/var/lib/botkalshi-research`.

## Uso local sin API

El control nace pausado. Comprobar el estado:

```bash
python3 infra/digitalocean-shadow/assistant_bridge.py \
  --data /var/lib/botkalshi-research \
  --state-data /var/lib/botkalshi-integrations status
```

Obtener el snapshot público, verificado y acotado que puede consumir un asistente:

```bash
python3 infra/digitalocean-shadow/assistant_bridge.py \
  --data /var/lib/botkalshi-research \
  --state-data /var/lib/botkalshi-integrations snapshot
```

Leer la última evaluación después de volver a comprobar que no reclama autoridad
de ejecución:

```bash
python3 infra/digitalocean-shadow/assistant_bridge.py \
  --data /var/lib/botkalshi-research \
  --state-data /var/lib/botkalshi-integrations latest-assessment
```

Habilitar únicamente evaluaciones de simulación:

```bash
python3 infra/digitalocean-shadow/assistant_bridge.py \
  --data /var/lib/botkalshi-research \
  --state-data /var/lib/botkalshi-integrations \
  resume-simulation --confirm SIMULATION_ONLY
```

Probar el contrato sin realizar una llamada externa:

```bash
python3 infra/digitalocean-shadow/assistant_bridge.py \
  --data /var/lib/botkalshi-research \
  --state-data /var/lib/botkalshi-integrations \
  assess --provider offline
```

Pausar:

```bash
python3 infra/digitalocean-shadow/assistant_bridge.py \
  --data /var/lib/botkalshi-research \
  --state-data /var/lib/botkalshi-integrations \
  pause --reason "revisión manual solicitada"
```

## Evaluación mediante OpenAI API

ChatGPT y OpenAI API tienen facturación/configuración separadas. El proceso necesita
una clave de API propia y un modelo elegido explícitamente; no hereda el archivo de
entorno de Kalshi.

En la unidad instalada, `/etc/botkalshi-assistant.env` contiene únicamente la
configuración no secreta `BOTKALSHI_OPENAI_MODEL`. La clave reside sola en
`/etc/botkalshi-assistant/openai_api_key`, archivo `root:root` con modo `0600`
dentro de un directorio `0700`, y systemd la entrega como la credencial privada
`openai_api_key` mediante `LoadCredential`. Los flags de ejecución se fijan en
`false` directamente en la unidad.

Ejecución manual:

```bash
systemctl start botkalshi-assistant.service
```

En el host instalado se usa la unidad para conservar la entrega aislada de la
credencial; no se exporta la clave antes de invocar Python.

La solicitud usa Responses API con `text.format`/`json_schema`, `strict=true`, `store=false` y un máximo de salida acotado. Se envían como máximo veinte mercados y cinco niveles por lado. No se envían el snapshot de cuenta, claves, identificadores de cuenta ni texto arbitrario almacenado en `assessment`.

Los archivos resultantes son:

```text
/var/lib/botkalshi-integrations/assistant/control-state.json
/var/lib/botkalshi-integrations/assistant/assessment-latest.json
```

La evaluación conserva hashes de los cuatro artefactos fuente y el ID de ciclo. Una evaluación no es una señal validada, un fill ni una autorización financiera.

## Avisos técnicos mediante Telegram

`telegram_notifier.py` consume el estado técnico verificado y una evaluación con
antigüedad máxima de 35 minutos. Una evaluación fresca de un ciclo anterior puede
motivar revisión humana, pero el mensaje identifica siempre el ciclo evaluado;
una evaluación vieja, futura o con fecha inválida no se adjunta. No retransmite
resúmenes libres del modelo, títulos o tickers de mercado. Construye texto plano
con campos permitidos y siempre incluye `Trading real: BLOQUEADO` y
`Capacidad de órdenes: NO DISPONIBLE`.

La salud de IA se clasifica así:

- `MISSING`: no existe evaluación y el control no está pausado.
- `STALE`: la evaluación supera 35 minutos, es futura o su fecha no es válida.
- `OFFLINE`: el control está pausado, el proveedor de la evaluación fresca no es
  `openai` o falló `botkalshi-assistant.service`.
- `ACTIVE`: el control no está pausado y existe una evaluación `openai` de como
  máximo 35 minutos.

En modo automático envía sólo estos eventos:

- Primera observación verificada o transición de `BLOCKED` a `VERIFIED`:
  “Botkalshi volvió”.
- Primera observación bloqueada, transición a `BLOCKED` o cambio del conjunto de
  bloqueos técnicos.
- Cambio de salud de IA a `MISSING`, `STALE` u `OFFLINE`, y recuperación al volver
  a `ACTIVE`.
- Fallo de `botkalshi-assistant.service`, por medio de
  `botkalshi-telegram-ai-failure.service`, con un aviso saneado y deduplicado.
- Nueva evaluación fresca cuya decisión no sea `NO_ACTION`; si pertenece a otro
  ciclo, el aviso muestra explícitamente ese ciclo evaluado.

El timer revisa cada cinco minutos, pero el archivo
`/var/lib/botkalshi-integrations/telegram/telegram-state.json` evita repetir
una entrega ya confirmada. La solicitud sólo cuenta como entregada si Telegram
responde HTTP 200, `ok=true`, un `message_id` positivo y el mismo chat numérico.
Si la respuesta es ambigua no escribe estado de éxito, por lo que una ejecución
posterior puede reintentar.

El token y el chat ID se guardan, respectivamente, en
`/etc/botkalshi-telegram/telegram_bot_token` y
`/etc/botkalshi-telegram/telegram_chat_id`. Ambos son archivos `root:root` con modo
`0600` dentro de un directorio `0700`; systemd los entrega mediante
`LoadCredential`. El chat ID debe ser numérico y no se aceptan alias mutables como
`@canal`. No poner el token en variables exportadas, argumentos, logs, el
repositorio o este chat.

## Instalación de ambas integraciones

En una consola root con TTY del Droplet dedicado Ubuntu 24.04 y con el collector
activo:

```bash
REV="$(git rev-parse HEAD)"
bash infra/digitalocean-shadow/install-assistant-integrations.sh \
  "$REV" --dedicated-research-host --simulation-only
```

Si los archivos de credenciales no existen, el instalador pide la clave OpenAI y
el token Telegram de forma oculta, y pide el modelo y chat ID sin guardar valores
en el historial del shell. Después exige internamente la confirmación literal
`SIMULATION_ONLY`, valida un ciclo `VERIFIED`, ejecuta una evaluación OpenAI y una
entrega inicial forzada a Telegram. Esa entrega exige además IA `ACTIVE` y la
confirmación interna literal `INSTALLATION_TEST`. Sólo entonces habilita
`botkalshi-assistant.timer` y `botkalshi-telegram.timer`.

El instalador es independiente de `install.sh`: no detiene, reinicia, habilita ni
reconfigura `botkalshi-research.service`. Comprueba que el PID y `ExecStart` del
collector no cambiaron. Una instalación incompleta detiene únicamente los dos
timers de integración. Aun con instalación correcta, se conserva:

```text
EXECUTION_AUTHORIZED=false
REAL_TRADING=false
```

La mera existencia de las unidades o los archivos no acredita una instalación.
Para considerarla operativa deben verse la salida
`INTEGRATIONS_OPERATIONAL=true`, `AI_PROVIDER=openai` y
`TELEGRAM_INITIAL_RUN=delivered`, además de ambos timers activos y habilitados.

## Conector opcional de ChatGPT/Codex

El código del plugin personal `botkalshi-control` traduce cuatro herramientas MCP
a los comandos fijos `status`, `snapshot`, `latest-assessment` y `pause`. Su mera
existencia no demuestra que el conector esté instalado, autorizado ni conectado
por SSH. Si se configura, puede ejecutar el puente en el mismo host o mediante SSH
en modo batch con verificación estricta de la clave del host. No acepta comandos
arbitrarios ni publica una herramienta de reanudación.

Para un host remoto se configuran, fuera del repositorio:

```text
BOTKALSHI_SSH_TARGET=usuario@host
BOTKALSHI_REMOTE_BRIDGE_SCRIPT=/ruta/absoluta/assistant_bridge.py
BOTKALSHI_REMOTE_DATA_DIR=/var/lib/botkalshi-research
```

La conexión sólo se podrá declarar operativa después de verificar una llamada real
al bridge con las rutas de datos y estado desplegadas, además de que SSH batch esté
autorizado para una cuenta restringida y la clave del host ya se encuentre
verificada. Crear o configurar el plugin no realiza ni acredita esos cambios.

## Lector de cuenta independiente

En una sesión autorizada que tenga credenciales de sólo lectura/alcance apropiado:

```bash
python3 scripts/read_research_account.py \
  --output /var/lib/botkalshi-research/account-snapshot.json
```

Ese comando pagina balance, posiciones y fills y escribe un snapshot saneado. El archivo declara `reconciled=false`, `real_entry_eligible=false` y `execution_authorized=false`. El colector público y el proceso de IA no deben ejecutarse con esas credenciales.

## Pendiente antes de dinero real

Ledger por IDs, fees y settlements; reservas atómicas; órdenes pending/UNKNOWN; exposición por tesis; P&L por día/semana en America/Los_Angeles; pausa persistente con desbloqueo explícito; evaluación económica; demo E2E; revisión independiente; despliegue y autorización separada. Ninguno de esos requisitos se sustituye por una respuesta del modelo.
