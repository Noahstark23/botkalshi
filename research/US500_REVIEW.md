# US500 V1 — entrega de revisión en GitHub

Rama: `codex/us500-paper-v1-20261011`.
Base: `9bf685d345844d12eb5e46c17aad0bbae5462d72` (main consultado el 11-oct-2026).

El paquete en [us500/](us500/) contiene 34 archivos idénticos a la entrega local,
incluido su manifiesto SHA256. Su README e informes conservan la fecha y las
limitaciones de aquella entrega: sus frases sobre publicación pendiente describen
el corte anterior a esta rama. Este documento actualiza únicamente la ubicación.

La rama M2/M5 permanece separada en `codex/live-readiness-20261006`, commit
`c14312867a0a07bf3ccbddbd63a0b909d2f8fb5a`; esos cambios no se incluyen en esta rama.
US500 es un paquete autónomo: no cambia el runner, flags, dependencias o workflows
del bot existente. La única DB incluida contiene dinero y eventos sintéticos.

## Reproducir la revisión

Python 3.10 o posterior, sin dependencias para ejecutar ni claves:

```sh
cd research/us500
python3 verify_package.py
python3 -m unittest discover -s tests -v
python3 run_demo.py --directory /tmp/us500-revision-nueva
```

El directorio del demo debe ser nuevo. No usar la DB de evidencia como destino de
nuevos ensayos. Las pruebas trabajan con transportes simulados y bases temporales;
el demo no consulta red ni transmite órdenes.

## Evidencia y límites

- 124 pruebas offline aprobadas; Ruff sin hallazgos. Logs históricos incluidos.
- Reservas, parciales, UNKNOWN, costes, riesgo, pausas durables y replay idempotente.
- Ejemplo ficticio: USD100 → USD98.6643, posición/reserva final cero, reinicio idéntico.
- Lectura pública archivada: mercado cerrado e históricos vacíos en la hora consultada.
- No hay evidencia de rentabilidad. Margen/funding/fees usan supuestos explícitos;
  la liquidación del exchange no se replica y el ejecutor real no está implementado.
- Pendientes: parámetros y acceso Perps de cuenta, datos completos de sesiones y
  evaluación independiente antes de plantear integración real.

Revisar [README](us500/README.md), [verificación](us500/VERIFICACION_V1.md) y
[contrato y límites](us500/CONTRATO_Y_LIMITES.md). Esta publicación sólo permite
revisión: no implica merge, despliegue, cambios de flags ni habilitación de órdenes.
