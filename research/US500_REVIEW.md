# Revisión US500 V2 en GitHub

Rama: `codex/us500-paper-v1-20261011`. Base main: `9bf685d345844d12eb5e46c17aad0bbae5462d72`. V1 está en `0fd3bf74551ca2586add9fa915b54c8d555e073f`; revisar el diff posterior para V2.

M2/M5 permanecen separados en `codex/live-readiness-20261006` @ `c14312867a0a07bf3ccbddbd63a0b909d2f8fb5a`. No combinar ramas automáticamente. US500 no importa ni altera flags o ejecutores del bot existente. V2 agrega únicamente un workflow independiente de pruebas offline, además del módulo de investigación.

```sh
cd research/us500
python3 verify_package.py
python3 verify_offline.py
python3 run_demo.py --directory /tmp/us500-revision-nueva
python3 run_control_demo.py --directory /tmp/us500-botones-nueva
```

Usar directorios nuevos y no escribir en evidencias históricas. La revisión V2 exige DB nuevas, no migra las V1. La suite local tiene 238 pruebas aprobadas, dos recorridos y Ruff limpio; el nuevo CI debe comprobarse por SHA en GitHub.

Cambios: coste agregado exacto, variation margin/tasas de escenario, capturas→sesiones y presupuesto durable, botones paper con autenticación/confirmación/pausa, permiso por evento y transporte con cursor/outbox. Verificar especialmente las regresiones de omisión del control, capacidades agotadas, IDs de callbacks opacos, respuestas ambiguas y recuperación tras fallo.

Leer [README](us500/README.md), [verificación V2](us500/VERIFICACION_V2.md), [Telegram paper](us500/TELEGRAM_PAPER.md), [sesiones](us500/SESIONES.md) y [contrato histórico](us500/CONTRATO_Y_LIMITES.md). Los parámetros de margen siguen siendo hipótesis; la nueva contabilidad no replica por sí sola la liquidación Kalshi.

El ejemplo sintético conserva el resultado USD100→USD98.6643, sin ventaja económica demostrada. El colector nuevo guardó una ficha pública con mercado cerrado tras 1 GET y pausó. No hubo órdenes, claves de cuenta, Telegram real ni despliegue. Permisos/condiciones Perps, datos de sesiones abiertas, evaluación independiente, conciliación real y ejecutor real siguen pendientes.

Entregable: hallazgos reproducibles con archivo/línea, evidencia consultada, comandos/resultados y dependencias externas. Mantener revisión separada de activación, merge y despliegue.
