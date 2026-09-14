# 01. Estado y nivel de ingeniería

## Comprobado el 14 de septiembre de 2026

La consulta a GitHub devuelve `main=bcc365f20a04b7ef3123da18983879b006290bb9`, el mismo SHA de la auditoría del 5 de septiembre. El repositorio es público, no archivado. La respuesta de la rama indica `protected=false`; no se han modificado protecciones ni se ha auditado toda la configuración administrativa.

El PR #251 sigue abierto y en borrador. Añade tests sobre un helper de fees; no resuelve por sí mismo la tarifa efectiva de MLB y no se integra en esta entrega.

Se leyeron el `pyproject.toml`, `docker-compose.yml`, `.github/workflows/ci.yml` y la cabecera/métricas de `scripts/tablero_gate.py`. Hay Python >=3.12, httpx, websockets, Pydantic, SQLModel, FastAPI, APScheduler, Telegram y SQLite. La CI ejecuta lint, formato y pytest; mypy está explícitamente no bloqueante y el smoke de Docker solo importa Settings.

El compose conserva parámetros históricos incompatibles con la política nueva: capital 300, tamaño 5%, exposición 25% y Kelly 0.25. No son los valores de la prueba de referencia 200. **No cambiar porcentajes a ciegas ni asumir que un archivo nuevo sobreescribe el runner.** Hace falta un adaptador de política y prueba integrada.

## Evidencia histórica, no repetida hoy

La auditoría fechada documentó 368 archivos, aproximadamente 23.963 líneas de `src` y 187 archivos de tests. De 1.675 casos, 1.673 pasaron inicialmente y dos de WebSocket local pasaron al corregir el aislamiento de loopback. Esto no es una ejecución nueva ni una garantía de cobertura suficiente.

También reprodujo K01–K08: pérdida de precisión en fills, cancelaciones mal confirmadas, cuarentena levantada por error, bypass de riesgo, exposición incorrecta al abrir NO, limpieza incompleta, ventas permitidas con trading apagado e incompatibilidades de contrato API. El SHA no ha cambiado: no hay una reparación integrada que podamos dar por hecha.

## Nivel profesional: valoración cualitativa

| Dimensión | Valoración | Evidencia pendiente para subir |
|---|---|---|
| Backend e integración | Intermedio-avanzado en alcance | Reproducir y explicar recuperación, concurrencia y contratos |
| Pruebas | Base amplia, efectividad desigual | Regresiones adversariales de los fallos monetarios |
| Operación | Parcialmente industrializada | Arranque real de perfil seguro, restore y medidas de disponibilidad |
| Seguridad | Deuda crítica | Revocación comprobada de credenciales expuestas y mínimo privilegio |
| Investigación cuantitativa | Laboratorio no validado | Cohorte fuera de muestra, fills realizables y costos completos |

**No es un script principiante. Tampoco está acreditado como plataforma financiera lista para producción.** Su complejidad contiene problemas propios de trabajo senior, pero un repositorio grande o generado con IA no acredita automáticamente el nivel personal de su autor. Esa evaluación requiere defender el diseño, escribir/corregir pruebas y resolver incidentes sin depender de un texto memorizado.

No presentarlo como HFT, hedge fund, sistema distribuido a gran escala o estrategia rentable validada. Su descripción defendible es backend asíncrono de investigación de mercados, contabilidad de eventos y control de riesgo en desarrollo.

No se inspeccionaron el servidor anterior, volúmenes, saldo real, órdenes vigentes o claves. El intento de clonar en el entorno de esta entrega no tuvo conectividad; la lectura actual se hizo por el conector de GitHub. No se ejecutó aquí la suite completa ni Docker.
