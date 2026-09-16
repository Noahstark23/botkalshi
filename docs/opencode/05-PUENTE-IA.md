# 05 — Puente de evidencia hacia el asistente

## Qué significa darle ojos al asistente

Significa que una sesión autorizada puede recibir datos actuales, identificados y verificables. No significa que el modelo viva en el Droplet, herede conectores de ChatGPT, tenga memoria automática de otros chats o esté vigilando posiciones continuamente. Un informe programado tampoco ejecuta stops.

Primera integración: exportación privada de archivos saneados para lectura en una sesión autorizada, sin nuevas APIs de pago. Etapa posterior opcional: servicio de lectura autenticado/MCP o API con permisos y presupuesto propios, después de verificar acceso y contrato del cliente. ChatGPT y la plataforma API tienen facturación separada; no convertir una suscripción en uso programático no autorizado. [S11](07-FUENTES.md).

## Contrato de paquete propuesto

Implementar esquema versionado, validación estricta, tamaño máximo, rechazo de claves duplicadas/NaN/Infinity y allowlist de campos. Ejemplo de estructura, no datos reales ni archivo de configuración activo:

```json
{
  "schema_version": "kalshi-assistant-evidence-v1-proposed",
  "packet_id": "SYNTHETIC-NOT-A-LIVE-PACKET",
  "cycle_id": "SYNTHETIC-CYCLE",
  "code_sha": null,
  "generated_at": null,
  "timezone": "America/Los_Angeles",
  "mode": "UNCONFIRMED",
  "execution_authorized": false,
  "coverage": [],
  "market_observations": [],
  "bank": {
    "reference_budget_usd": "200.00",
    "state": "PENDING_RECONCILIATION",
    "cash_verified_usd": null,
    "last_reconciled_capital_usd": null,
    "reconciled_at": null,
    "ledger_manifest_id": null
  },
  "risk": {"real_entry_eligible": false},
  "assessments": [],
  "warnings": ["Ejemplo estructural incompleto; no es evidencia de mercado ni de cuenta."]
}
```

El ejemplo debe fallar si se presenta como paquete fresco real. En producción validada, cada dato tendrá timestamps reales y evidencia; una categoría sin datos seguirá figurando con estado explícito. Los manifiestos vinculan hashes, cycle_id y versiones de política/esquema. Rechazar incoherencia, mezcla de ciclos, timestamps futuros y artefactos viejos aunque health sea reciente.

## Funciones a implementar, no herramientas ya disponibles

| Función acotada propuesta | Salida y autoridad |
|---|---|
| `get_research_health()` | Estado y edad, sin secretos o control de procesos. |
| `get_coverage(report_date)` | Las 11 categorías y su evidencia/faltantes. |
| `get_market_observation(observation_id)` | Libro y reglas de una observación existente, no URL libre. |
| `get_bank_reconciliation(snapshot_id)` | Resumen privado mínimo, cobertura y discrepancias. |
| `evaluate_simulated_proposal(proposal)` | Cálculo ilustrativo con razones; no reserva REAL ni orden. |
| `read_assessment(assessment_id)` | Evaluación persistida, no historial de fills inventado. |

No exponer shell, SQL libre, path arbitrario, firmante, place_order, cancel_order, set_limits o set_balance. IDs/argumentos tipados y acotados. Persistir evaluaciones por un módulo local auditado vinculado al paquete, no mediante escritura arbitraria del modelo en el ledger REAL. Cualquier componente que use SQLite accede con privilegios mínimos y no mediante consultas generadas libremente por la IA.

## Contrato del informe en español

Comenzar con estado del bank y decisión CONSIDERAR/ESPERAR/NO OPERAR. Mostrar tabla de capital nominal, último capital conciliado/fecha, efectivo, reservas, posiciones/coste/valor realizable, exposición, costes y P&L; desconocidos se rotulan NO VERIFICADO. Antes de candidatas, mostrar Cobertura revisada con las 11 categorías del observador. No ocultar una categoría para simplificar el informe.

Máximo tres candidatas TOTALES: evento/activo y hora, instrumento exacto, tesis, precio observado y timestamp/proveedor, coste neto, umbral de entrada, cantidad/presupuesto, pérdida máxima o riesgo planificado diferenciados, horizonte y estado SIMULACIÓN/REAL PENDIENTE/ESPERAR/DESCARTAR. Para las mejores: argumento a favor, principal riesgo e invalidación. Ninguna se convierte automáticamente en operación.

Con datos insuficientes escribir Sin entrada recomendada. No objetivos diarios de ganancias, parlays por defecto, récords inventados ni promesas de rentabilidad. Resultados y desglose por categoría sólo de registros verificables; separar CLV comparable de resultado/P&L. No heredar cuotas, horarios o indicadores antiguos. Un buen resultado MLB no sesga el barrido ni la unidad.

## Prueba de conexión

La integración se considera probada sólo al exportar un paquete válido, recibirlo mediante un canal autorizado, comparar ID/hash/timestamps y rechazar un fixture stale o malicioso. Publicar estos MD, devolver un JSON local o tener un conector de precios no demuestra acceso a posiciones. Mantener el puente como NO_CONECTADO hasta esa prueba; no publicar endpoint ni cuenta privada en el repo.
