# 02. Arquitectura y límites

## ADR-001: reutilizar, no reconstruir

Un monolito modular Python con SQLite y un solo escritor es suficiente para el laboratorio inicial. Mantener REST/WS, normalización, M2 como referencia, M5 como experimento y observabilidad ya existentes. No introducir Kubernetes, Kafka, una base vectorial o nuevos motores para justificar el relanzamiento.

Rutas existentes confirmadas por la auditoría y el SHA: `src/clients/kalshi_rest.py`, `src/risk/manager.py`, `src/strategies/motor_5_mm/{engine,executor,reconciler,fee_policy}.py`, `src/strategies/fair_value_book.py`, `scripts/tablero_gate.py`.

## ADR-002: separar dos preguntas

**Laboratorio M5:** hipótesis congelada de proveer liquidez prepartido, una serie inicialmente, parámetros/cohorte identificados. Los cambios que sugiera una IA no se aplican a mitad de cohorte.

**Radar contextual:** consume paquetes de observación para contrastar noticias, reglas, fuentes y cobertura. Puede investigar múltiples activos; no es un nuevo motor direccional ni hereda autorización de M5. Sus candidatos no son fills.

```text
Fuentes autorizadas -> colectores REST/WS existentes -> validación de libro/edad/fees
                                                  -> M2 referencia -> M5 shadow
                                                  -> escritor SQLite
                                                  -> snapshot/paquete saneado
                                                       -> revisión IA opcional
                                                       -> informe <=3 candidatas

ledger confirmado -> política determinista -> evaluación de tamaño SIMULADA

Executor de órdenes: FUERA del perfil inicial; sin enlace desde la IA.
```

## ADR-003: fronteras de autoridad

El colector identifica eventos y conserva la fuente. M2 calcula referencias documentadas, no verdad económica. M5 produce mediciones hipotéticas. El ledger distingue observación, propuesta, orden, fill y liquidación. La IA puede devolver WAIT, REJECT o REVIEW; no APPROVE_TRADE, órdenes, cambios de política, código ejecutable ni movimientos financieros.

El cálculo final de precio/costos/riesgo es determinista y se repite sobre un snapshot vigente. Revisión humana no transforma por sí sola un executor inseguro en uno seguro. En este alcance no existe ruta habilitada a producción.

## ADR-004: datos y procesos

Un proceso escritor serializa observaciones, decisiones y reservas. API de reportes/MCP solo lee exportaciones saneadas o snapshots. Nunca dos instancias escribiendo/ejecutando sobre la misma cuenta por un redeploy. Identificar instance_id, run_id, commit_sha, policy_hash y experiment_id.

Backpressure: cola acotada, contadores de descarte y pausa de evaluación si se pierde continuidad. Tras reconexión: invalidar libro hasta nuevo snapshot y reconciliación de secuencia. No continuar calculando sobre un libro parcialmente actualizado.

## Cobertura por capacidades

El registro deberá contener MLB, NFL, NCAAF, Champions, Liga MX, Leagues Cup, acciones, oro, cripto, FX y Kalshi financiero. Estados por proveedor: disponible, pendiente de implementación, sin eventos verificados, datos insuficientes o no revisado. Para un informe general, verificar todos y comparar como máximo tres candidatos totales después del barrido.

MVP automatizado: M5 de la serie existente de MLB. El resto comienza como catálogo/reportes, no como conectores supuestamente completos. Un adaptador faltante se declara pendiente; no se confunde con ausencia de valor. No comprar datos para ampliar cobertura sin una decisión de presupuesto.

## Interfaces a implementar, no existentes por estos MD

`export_research_packet(snapshot)`, `validate_market_mapping(packet)`, `evaluate_proposal(packet, policy, ledger)`, `read_research_report(report_id)` y `record_assessment(packet_hash, assessment)` son contratos propuestos. Se implementarán alrededor de los módulos existentes; no crear un segundo ledger independiente que contradiga al primero.
