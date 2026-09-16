# 00 — Estado y bloqueos verificables

Fecha de inspección: 2026-09-16. Base de código de esta entrega: `2f74795b1161d3fe92dbfcfcc08c30f237fd9ee8`. Las tareas son especificaciones; este documento no afirma que estén implementadas.

## Referencias comprobadas

| Elemento | Estado al preparar la entrega |
|---|---|
| Repositorio | `Noahstark23/botkalshi`. |
| PR #255 | Abierto, borrador, sin fusionar; rama `feat/observer-risk-guard-20260916`. |
| HEAD de código revisado | `2f74795b1161d3fe92dbfcfcc08c30f237fd9ee8`. |
| Base de PR #255 | `feat/digitalocean-shadow-20260915`, SHA `f2f1c326a822f0ac01a6cb859f7d35d4d8f32b69`, trabajo de PR #254. |
| Observador público | `infra/digitalocean-shadow/collector.py`; sin lector de cuenta autenticado. |
| Extensión de PR #255 | `pagination.py`, `research_runner.py`, `risk_guard.py`, tests y documentación. |
| Lector de cuenta / puente IA | No verificados como implementados o conectados. |
| Pruebas de este handoff | No se ha ejecutado la suite del bot al preparar estos MD. |
| Despliegue y heartbeat | No comprobados con una nueva inspección remota en esta entrega. |

Consultar también `docs/operations/BOT_MEMORY.md`, `RUNBOOK.md`, `VALIDACION.md` e `INTEGRATIONS.md`, reconociendo sus fechas. Los nuevos documentos no certifican el contenido de documentos antiguos.

## Correcciones a afirmaciones demasiado amplias

El guard actual es un prototipo de cálculo, no un firewall financiero validado. En `build_status`, `capital_reconciled_at` y `source` sólo se exigen como cadenas no vacías. No valida su antigüedad, autenticidad, continuidad del ledger, ventanas de día/semana, efectivo reservado ni límite conjunto por tesis. No puede llamar RECONCILED a un JSON arbitrario. La unidad calculada sin estado persistente puede subir después de una recuperación; debe respetar la prohibición de aumento automático.

La paginación actual consulta una serie, por defecto `KXMLBGAME`, y filtra por `close_time`; eso no es un barrido multideporte ni prueba de que el encuentro no comenzó. Detecta un cursor repetido inmediatamente, no todos los ciclos A-B-A. Los límites de páginas/libros y la ausencia de timestamps individuales requieren pruebas.

`coverage.json` se escribe separado del paquete; el wrapper calcula riesgo después del ciclo que publica health. Todavía debe demostrarse coherencia entre ciclo, cobertura, paquete y riesgo, especialmente si falla una escritura. Un heartbeat fresco no vuelve fresco un libro viejo.

El workflow CI observado filtra push a main/develop y PR dirigido a main. No asumir ejecución ni aprobación por abrir un PR contra una rama feature. El instalador descubre tests en `infra/digitalocean-shadow/tests/`; el archivo separado `test_collector.py` exige una ejecución explícita adicional.

## Infraestructura: conservar, no reconstruir

La consulta de la sesión anterior devolvió el Droplet dedicado `botkalshi-research-sfo3` activo. Es evidencia de VM, no de servicio sano. No hay nueva prueba del SHA instalado, rutas de releases, heartbeat o archivos del host. La falta de terminal o un intento de conexión fallido no demuestra que SSH esté cerrado globalmente. El comando de instalación sugerido anteriormente no es un comando listo para ejecutar sin inspección del host y reconciliación del parche pendiente.

## Candidato pendiente de otra sesión

El checkpoint privado `KALSHI-PUNTO-DE-REANUDACION.md` conserva el paquete `prdry.tgz`: tamaño esperado 18833 bytes, SHA-256 `2cc552d7a22326320c6cf00c433f0f3dcc6f13ba3f8115e13e2ffbbe55133b04`. Base reportada `f2f1c326a822f0ac01a6cb859f7d35d4d8f32b69`; rama local `fix/collector-pagination-coverage`, commit abreviado reportado `8c0e72f`, no recibido para esta entrega.

El paquete contiene según el manifiesto collector.py, reporting.py y dryrun.py; no se inspeccionaron sus bytes aquí. No atribuirle los tests de la base. No recuperar por base64/pantalla u otra interfaz para eludir el bloqueo de extracción. Si llega mediante una ruta autorizada, verificar tamaño/hash y rutas antes de extraer sin ejecución en un entorno aislado. No repetir el hotfix ni borrar trabajo remoto. Se puede avanzar en fixtures de P0 sin sustituir el candidato no recibido; antes de integrar cambios solapados, comparar ambos explícitamente.

Fuentes: [referencias de código y documentación](07-FUENTES.md). El checkpoint privado es contexto de continuidad, no un archivo público incluido en este paquete.
