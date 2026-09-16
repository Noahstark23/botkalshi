# 07 — Fuentes, versiones y límites de evidencia

Consulta: 2026-09-16. Documentación primaria revisada al preparar el handoff. Volver a comprobar esquemas/versiones al implementar. Ninguna documentación demuestra que un endpoint haya respondido con la cuenta del operador.

## Código observado

- PR de trabajo: https://github.com/Noahstark23/botkalshi/pull/255
- Base inmutable de código examinada: https://github.com/Noahstark23/botkalshi/tree/2f74795b1161d3fe92dbfcfcc08c30f237fd9ee8
- Guard observado: https://github.com/Noahstark23/botkalshi/blob/2f74795b1161d3fe92dbfcfcc08c30f237fd9ee8/infra/digitalocean-shadow/risk_guard.py
- Paginación: https://github.com/Noahstark23/botkalshi/blob/2f74795b1161d3fe92dbfcfcc08c30f237fd9ee8/infra/digitalocean-shadow/pagination.py
- Wrapper: https://github.com/Noahstark23/botkalshi/blob/2f74795b1161d3fe92dbfcfcc08c30f237fd9ee8/infra/digitalocean-shadow/research_runner.py
- Instalador: https://github.com/Noahstark23/botkalshi/blob/2f74795b1161d3fe92dbfcfcc08c30f237fd9ee8/infra/digitalocean-shadow/install.sh
- Workflow observado: https://github.com/Noahstark23/botkalshi/blob/2f74795b1161d3fe92dbfcfcc08c30f237fd9ee8/.github/workflows/ci.yml

La inspección de código es estática. No se ejecutó el bot ni una conciliación bancaria durante la redacción. El nuevo commit de documentación, cuando se publique, será posterior a la base indicada; no implica otro SHA instalado en el servidor.

## Documentación oficial

| ID | Fuente | Uso limitado |
|---|---|---|
| S1 | https://opencode.ai/docs/rules/ | AGENTS/CLAUDE, prioridad y carga explícita de otros archivos. |
| S2 | https://opencode.ai/docs/permissions/ | Sintaxis V1 y allow/ask/deny; no autoaprobar. |
| S3 | https://opencode.ai/v2/docs/permissions | Sintaxis V2 distinta; comprobar versión instalada. |
| S4 | https://docs.kalshi.com/getting_started/api_environments | Hosts de producción/demo; external-api recomendado, hosts compartidos compatibles. |
| S5 | https://docs.kalshi.com/api-reference/portfolio/get-balance | Unidades, timestamps, alcance y scope de lectura de balance. |
| S6 | https://docs.kalshi.com/api-reference/portfolio/get-positions | Paginación, cuenta/exchange y posiciones. |
| S7 | https://docs.kalshi.com/api-reference/orders/get-orders | GET de órdenes no equivale a facultad de enviarlas. |
| S8 | https://docs.kalshi.com/api-reference/portfolio/get-fills | Fills y separación del histórico. |
| S9 | https://docs.kalshi.com/getting_started/historical_data | Corte e historia de órdenes/fills; evitar huecos. |
| S10 | https://docs.kalshi.com/api-reference/market/get-market-orderbook | Semántica del libro y formato de niveles. |
| S11 | https://help.openai.com/en/articles/9039756-billing-settings-in-chatgpt-vs-platform | Facturación ChatGPT/API separada. |
| S12 | https://docs.kalshi.com/api-reference/portfolio/get-settlements | Liquidaciones de contratos para conciliación. |

No se afirma aquí una comisión vigente concreta, un precio actual, una probabilidad de evento ni un mercado abierto. Esas verificaciones corresponden a cada informe. No incluir páginas de ejemplo o respuestas generadas como fills reales.

## Evidencia privada de continuidad

`KALSHI-PUNTO-DE-REANUDACION.md`, versión 1 de la Library del operador, fue leído para conservar el pendiente prdry.tgz y las restricciones de acceso. Contiene un reporte previo, no una inspección remota nueva. No se publica íntegro ni se incluyen rutas administrativas privadas en este handoff.

La política de referencia USD 200 procede de las instrucciones del operador en esta conversación. No es una recomendación derivada de rendimientos auditados ni un saldo importado. La infraestructura se reutiliza y las nuevas lecturas/servicios permanecen pendientes de implementación, pruebas y autorización aplicable.
