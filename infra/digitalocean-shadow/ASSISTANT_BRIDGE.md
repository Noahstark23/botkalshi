# Puente botkalshi ↔ asistente

Estado: implementación local, no desplegada. El puente administra observación y pausas de la capa de análisis; **no administra dinero ni órdenes**.

## Contrato de seguridad

- Lee cuatro archivos del mismo ciclo: `health.json`, `packets/latest.json`, `coverage.json` y `risk-status.json`.
- Rechaza ciclos mezclados, viejos, futuros, incompletos, con conteos incompatibles o flags de autoridad que no sean el booleano `false`.
- La IA no recibe `KALSHI_API_KEY_ID`, clave privada ni variables de ejecución.
- Salida obligatoria mediante JSON Schema. El bot vuelve a validar el resultado localmente.
- Comandos permitidos: `NOOP`, `PAUSE_RESEARCH`, `REQUEST_HUMAN_REVIEW` y `REFRESH_PUBLIC_SNAPSHOT`.
- Sólo `PAUSE_RESEARCH` puede aplicarse automáticamente. Reanudar requiere al operador y la confirmación literal `SIMULATION_ONLY`.
- No existen comandos de compra, venta, cancelación, transferencia, shell ni modificación de riesgo.

## Uso local sin API

El control nace pausado. Comprobar el estado:

```bash
python3 infra/digitalocean-shadow/assistant_bridge.py \
  --data /var/lib/botkalshi-research status
```

Habilitar únicamente evaluaciones de simulación:

```bash
python3 infra/digitalocean-shadow/assistant_bridge.py \
  --data /var/lib/botkalshi-research \
  resume-simulation --confirm SIMULATION_ONLY
```

Probar el contrato sin realizar una llamada externa:

```bash
python3 infra/digitalocean-shadow/assistant_bridge.py \
  --data /var/lib/botkalshi-research assess --provider offline
```

Pausar:

```bash
python3 infra/digitalocean-shadow/assistant_bridge.py \
  --data /var/lib/botkalshi-research \
  pause --reason "revisión manual solicitada"
```

## Evaluación mediante OpenAI API

ChatGPT y OpenAI API tienen facturación/configuración separadas. El proceso necesita una clave de API propia y un modelo elegido explícitamente; no debe heredar el archivo de entorno de Kalshi.

Ejemplo de entorno separado:

```text
OPENAI_API_KEY=...
BOTKALSHI_OPENAI_MODEL=...
TRADING_ENABLED=false
MOTOR_MM_EXECUTION_ENABLED=false
MOTOR_1_EXECUTION_ENABLED=false
MOTOR_2_EXECUTION_ENABLED=false
MOTOR_2_ENTRY_EXECUTION_ENABLED=false
MOTOR_3_EXECUTION_ENABLED=false
MOTOR_REST_EXECUTION_ENABLED=false
```

Ejecución manual:

```bash
python3 infra/digitalocean-shadow/assistant_bridge.py \
  --data /var/lib/botkalshi-research assess --provider openai
```

La solicitud usa Responses API con `text.format`/`json_schema`, `strict=true`, `store=false` y un máximo de salida acotado. Se envían como máximo veinte mercados y cinco niveles por lado. No se envían el snapshot de cuenta, claves, identificadores de cuenta ni texto arbitrario almacenado en `assessment`.

Los archivos resultantes son:

```text
assistant/control-state.json
assistant/assessment-latest.json
```

La evaluación conserva hashes de los cuatro artefactos fuente y el ID de ciclo. Una evaluación no es una señal validada, un fill ni una autorización financiera.

## Lector de cuenta independiente

En una sesión autorizada que tenga credenciales de sólo lectura/alcance apropiado:

```bash
python3 scripts/read_research_account.py \
  --output /var/lib/botkalshi-research/account-snapshot.json
```

Ese comando pagina balance, posiciones y fills y escribe un snapshot saneado. El archivo declara `reconciled=false`, `real_entry_eligible=false` y `execution_authorized=false`. El colector público y el proceso de IA no deben ejecutarse con esas credenciales.

## Pendiente antes de dinero real

Ledger por IDs, fees y settlements; reservas atómicas; órdenes pending/UNKNOWN; exposición por tesis; P&L por día/semana en America/Los_Angeles; pausa persistente con desbloqueo explícito; evaluación económica; demo E2E; revisión independiente; despliegue y autorización separada. Ninguno de esos requisitos se sustituye por una respuesta del modelo.
