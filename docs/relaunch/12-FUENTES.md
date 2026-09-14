# 12. Fuentes y límites de evidencia

Consulta documental: 2026-09-14. Ninguna página de documentación demuestra que un endpoint haya sido probado con la cuenta del operador.

## Repositorio consultado por conector autorizado

- [Base main examinada](https://github.com/Noahstark23/botkalshi/tree/bcc365f20a04b7ef3123da18983879b006290bb9).
- [Dependencias](https://github.com/Noahstark23/botkalshi/blob/bcc365f20a04b7ef3123da18983879b006290bb9/pyproject.toml).
- [Compose existente](https://github.com/Noahstark23/botkalshi/blob/bcc365f20a04b7ef3123da18983879b006290bb9/docker-compose.yml).
- [CI existente](https://github.com/Noahstark23/botkalshi/blob/bcc365f20a04b7ef3123da18983879b006290bb9/.github/workflows/ci.yml).
- [Gate M5](https://github.com/Noahstark23/botkalshi/blob/bcc365f20a04b7ef3123da18983879b006290bb9/scripts/tablero_gate.py).
- [PR 251, abierto/draft al consultar](https://github.com/Noahstark23/botkalshi/pull/251).

## Evidencia histórica del propietario

`AUDITORIA-KALSHI.md`, `PLAN-OPENCODE.md` y `VERIFICACION-KALSHI.md`, revisión fechada 2026-09-05, recuperados de Library. Los defectos, conteos y resultados de tests citados como históricos proceden de estos documentos. No se han repetido aquí sus reproducciones ni reconstruido P&L con datos de cuenta. No se copian estados financieros privados al repositorio público.

## Documentación oficial vigente leída

- [Kalshi: entornos y endpoints](https://docs.kalshi.com/getting_started/api_environments).
- [Kalshi: market data](https://docs.kalshi.com/getting_started/quick_start_market_data).
- [Kalshi: WebSocket y autenticación](https://docs.kalshi.com/getting_started/quick_start_websockets).
- [Kalshi: precisión fija, ticks y fills fraccionarios](https://docs.kalshi.com/getting_started/fixed_point_migration).
- [Kalshi: metadatos de serie](https://docs.kalshi.com/api-reference/market/get-series).
- [Kalshi: crear orden V2](https://docs.kalshi.com/api-reference/orders/create-order-v2).
- [Kalshi: cancelar orden V2](https://docs.kalshi.com/api-reference/orders/cancel-order-v2).
- [OpenAI: facturación separada ChatGPT/API](https://help.openai.com/en/articles/9039756-managing-billing-for-chatgpt-and-the-api-platform).
- [OpenAI: Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
- [OpenAI: MCP/connectors](https://developers.openai.com/api/docs/guides/tools-connectors-mcp).
- [Docker: superficie de seguridad](https://docs.docker.com/engine/security/).
- [SQLite: Online Backup API](https://sqlite.org/backup.html).

Las tarifas efectivas de cada producto, límites de proveedor, elegibilidad desde Nicaragua, precios de VPS y consumo real de API requieren verificación antes de contratarlos/utilizarlos. No se inventan tarifas ni capacidad de cuenta. No se asume que el host alternativo antiguo de Kalshi haya dejado de funcionar: los actuales endpoints recomendados coexisten con hosts compatibles documentados.

## Endpoints de lectura del diseño

REST producción recomendado: `https://external-api.kalshi.com/trade-api/v2`.
WS producción recomendado: `wss://external-api-ws.kalshi.com/trade-api/ws/v2`.
Demo: hosts oficiales separados indicados en la documentación. La selección de producción aquí describe datos, no órdenes autorizadas. Verificar rutas, payloads y respuestas operación por operación; no reemplazar indiscriminadamente todas las URLs por una ruta V2 de creación.
