# Revisión US500 V2.2.1 en GitHub

Rama: `codex/us500-paper-v1-20261011`. Base main: `9bf685d345844d12eb5e46c17aad0bbae5462d72`. V1 está en `0fd3bf74551ca2586add9fa915b54c8d555e073f`; revisar el diff posterior para V2.

M2/M5 permanecen separados en `codex/live-readiness-20261006` @ `c14312867a0a07bf3ccbddbd63a0b909d2f8fb5a`. No combinar ramas automáticamente. US500 no importa ni altera flags o ejecutores del bot existente. V2 agrega únicamente un workflow independiente de pruebas offline, además del módulo de investigación.

```sh
cd research/us500
python3 verify_package.py
python3 verify_offline.py
python3 run_demo.py --directory /tmp/us500-revision-nueva
python3 run_control_demo.py --directory /tmp/us500-botones-nueva
```

Usar directorios nuevos y no escribir en evidencias históricas. La revisión V2 exige DB nuevas, no migra las V1. La suite local tiene 282 pruebas aprobadas, dos recorridos y Ruff limpio; el nuevo CI debe comprobarse por SHA en GitHub.

Cambios: coste agregado exacto, variation margin/tasas de escenario, capturas→sesiones y presupuesto durable, botones paper con autenticación/confirmación/pausa, permiso por evento y transporte con cursor/outbox. Verificar especialmente las regresiones de omisión del control, capacidades agotadas, IDs de callbacks opacos, respuestas ambiguas y recuperación tras fallo.

Leer [README](us500/README.md), [verificación V2](us500/VERIFICACION_V2.md), [correcciones V2.1](us500/VERIFICACION_V2_1.md), [Telegram paper](us500/TELEGRAM_PAPER.md), [sesiones](us500/SESIONES.md) y [contrato histórico](us500/CONTRATO_Y_LIMITES.md). Los parámetros de margen siguen siendo hipótesis; la nueva contabilidad no replica por sí sola la liquidación Kalshi.

El ejemplo sintético conserva el resultado USD100→USD98.6643, sin ventaja económica demostrada. El colector nuevo guardó una ficha pública con mercado cerrado tras 1 GET y pausó. No hubo órdenes, credenciales Kalshi ni despliegue de producción. V2.1 sí verifica entrega y callbacks Telegram reales contra un control/ledger local de simulación, con receptor acotado y estado final pausado. Permisos/condiciones Perps, datos de sesiones abiertas, evaluación independiente, conciliación real y ejecutor real siguen pendientes.

Entregable: hallazgos reproducibles con archivo/línea, evidencia consultada, comandos/resultados y dependencias externas. Mantener revisión separada de activación, merge y despliegue.

V2.1 corrige los dos hallazgos de Claude sobre HTTPException y coerción numérico/texto en hashes/archivos JSON. Agrega supervisor local con locks y pausas al terminar/fallar. Revisar regresiones de números con exponente, precisión Decimal y conservación de capturas/presupuesto ante protocolo HTTP incompleto. Las fuentes públicas archivadas y sus límites se describen en [FUENTES_ARCHIVADAS](us500/FUENTES_ARCHIVADAS.md).

V2.2 agrega [panel de todos los motores](us500/PANEL_TODOS_MOTORES.md) dentro del mismo receptor. `/motores`, `/menu` y `/start` muestran nueve motores; `/us500` conserva control paper con confirmación. M1/M2/M3/M5/M6/REST/M8/M9 y el control general están desconectados y no mutan nada. No confundir estos botones con una integración funcional al bot remoto. Contrastar permisos, estado del proceso y riesgo por separado; no conectar la futura pausa de entradas al `/admin/pause` heredado, que también bloquea ventas. M2/M5 no se importan ni se combinan en esta rama.

V2.2.1 corrige pérdida del código de error al detenerse el poll. El [diagnóstico](us500/DIAGNOSTICO_TELEGRAM.md) documenta el receptor local FAILED, pausa confirmada y outbox sin ambiguos. La causa histórica no se guardó y no se inventa. `last_poll_error` ahora conserva sólo códigos admitidos, sin reintentos ni datos sensibles. El receptor permanece detenido; el arreglo no equivale a recuperación operativa.
