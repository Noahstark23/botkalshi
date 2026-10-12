# Revisión diferencial V2.7.1 — 12 de octubre de 2026

Base:9c9c3f3cbe7e5c0f8b69cd8acef46b882d23e471. Claude confirmó recepción de V2.7
y entregó revisión estática, sin repetir suites. Se contrastaron sus propuestas.

458 pruebas y dos recorridos locales offline, Ruff0.14.0 y formato correctos.
CI y efectividad de la instalación se registran por SHA en el informe operativo.
Evidencias nuevas en evidence/V2_7_1, históricas sin alteraciones.

- CI-01 residual: el verificador arranca con python -I, sin importar un pathlib.py
  inyectado antes de detectar archivo extra. La regresión verifica ese rechazo.
- TG-07: filtros de syscalls/arquitectura, namespace de usuarios/hostname y rutas
  Coolify/Docker inaccesibles. El panel deniega también la IP pública propia del
  host actual; las direcciones se revalidan al instalar. Requiere prueba efectiva
  de cgroup y servicio, no sólo inspección de la unidad.
- F2: se cuentan settlements opcionales declarados/procesados y se etiquetan
  como escenarios no contrastados con el venue. El límite<1e9 no prueba origen
  económico; no se inventa una tolerancia contra mark.
- DOC-01:5bps maker en OpenAPI es un ejemplo de formato, no tarifa vigente.
- Evaluación larga: regresión que agota eventos en el plan en memoria y comprueba
  que la DB persistente no fue creada. Se conserva orden íntegro part01..NN,
  sin seleccionar/renombrar tramos para forzar una evaluación.

TG-06 sigue limitado a10updates/ciclo en orden. La espera429 respeta retry_after;
paper está pausado y no hay runner. No se promete parada prioritaria de un motor
real. TG-08 es documental, sin habilitar controles reales. F1 conserva bloqueo
UNKNOWN. TG-02 mitiga al consumidor mediante caché separada; no añade auth al
runtime financiero, cuyo alcance sigue separado.

La captura está abierta. Evaluar su mercado requiere cierre/auditoría completos,
costes/funding/condiciones respaldados y protocolo previo. Ninguna prueba técnica
ni captura parcial demuestra rentabilidad o preparación real.
