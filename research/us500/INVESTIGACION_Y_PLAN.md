# US500: investigación y preparación del motor

> Documento histórico de v0.1, anterior a V1/V2. Las referencias a conectores, simulador o estrategia todavía no implementados describen ese corte. Para el estado vigente ver README.md y VERIFICACION_V2_1.md.

Fecha de corte: 2026-10-11. Tres agentes investigaron producto, API y arquitectura; el agente principal contrastó fuentes y construyó este núcleo local. Diciembre queda fuera de alcance por indicación posterior de Noel.

## Producto identificado

Kalshi anunció **US 500 Perpetual** el 6 de octubre. El registro CFTC de US500PERP lo clasifica como futuro financiero sobre índice de acciones, con estado aprobado el 3 de octubre. Es un producto financiero distinto de los contratos binarios deportivos. [Anuncio](https://pro.kalshi.com/pro/updates/us-500-perpetual), [registro CFTC](https://www.cftc.gov/IndustryOversight/IndustryFilings/TradingOrganizationProducts/62926).

El filing inicial identifica **MerQube US Large Cap**, no el S&P 500 ni un ETF. Describe liquidación en efectivo, ausencia de vencimiento, negociación de domingo 18:00 a viernes 17:00 ET y financiación al cierre regular del índice. La versión inicial expresa un contrato base de US$1 por punto; eso debe conciliarse con la unidad API actual. No se certificó que el filing incluya todas las enmiendas vigentes. [Filing oficial, apéndice A](https://www.cftc.gov/filings/ptc/ptc08182617972.pdf).

La tarifa pública inicial consultada muestra 0,120% taker y 0,020% maker por ejecución, sobre nocional; la cuenta concreta puede tener otra tarifa. Como ejemplo matemático, dos patas taker al mismo nocional representan aproximadamente 24 puntos básicos de comisiones antes de otros costes. No es una predicción de rendimiento. [Tarifario](https://kalshi.com/fee-schedule?product=perps), [explicación de comisiones](https://help.kalshi.com/en/articles/16071417-perps-fees-explained).

La habilitación de Perps requiere una solicitud de cuenta de margen y aprobación propias. La residencia en EE. UU. y una cuenta de predicciones previamente verificada no demuestran ese permiso. No se consultó ninguna cuenta en este trabajo. [Requisitos](https://help.kalshi.com/en/articles/15357656-applying-for-perpetuals-access).

## API y evidencia pública

| Dato | Hallazgo al corte | Estado |
|---|---|---|
| Identificador público | `KXUS500PERP`, exchange 1 | Observado por agente y captura local |
| Identificador Demo | `KXUS500PERP1`, exchange 0 | Observado por agente en catálogo |
| Parámetros API | contract size 0.001000; multiplicador 1.000000; tick 0.0001; fracciones deshabilitadas | Observados; falta conciliación contractual |
| Mercado abierto | `status=active` con `schedule.is_open=false` | No apto para simular una ejecución contemporánea |
| Cotización extrema | Ask de catálogo 922337203685477.5807; no se confirmó su semántica | Rechazar como precio utilizable |
| Ficha/libro | Dos respuestas públicas HTTP 200 guardadas localmente | Observación puntual, no sesión completa |
| Libro REST | Dos lados bid/ask; normalización propia ordena precios | Sin timestamp del libro ni snapshot atómico |

Fuentes de esquemas: [ficha de mercado](https://docs.kalshi.com/margin-rest/market/get-market), [libro](https://docs.kalshi.com/margin-rest/market/get-market-orderbook). La evidencia exacta de esta sesión está en `OBSERVACION_PUBLICA_2026-10-11.json`; el resultado fue `INCOMPLETE_OR_UNUSABLE / MARKET_CLOSED`.

Perps usa `/trade-api/v2/margin/` bajo `external-api.kalshi.com`; Demo tiene host propio. WebSocket es separado y requiere autenticación. La guía confirma diferencias frente a eventos: no hay batch de órdenes, posiciones de cola ni el modelo de eventos/series. Compartir convenciones de autenticación no vuelve compatibles los modelos económicos. [Guía Perps](https://docs.kalshi.com/margin), [WebSocket](https://docs.kalshi.com/margin-ws/websockets/websocket-connection).

Hay documentación de trades paginados, velas e histórico de funding; no se verificó la retención suficiente para estudiar una estrategia. Estos conectores no están implementados en v0.1. [Trades](https://docs.kalshi.com/margin-rest/market/get-trades), [velas](https://docs.kalshi.com/margin-rest/market/get-market-candlesticks), [funding histórico](https://docs.kalshi.com/margin-rest/funding/get-historical-funding-rates).

Los límites efectivos de cuenta tienen endpoint específico de Perps. No se copiaron cuotas de Predictions ni se consultaron cuotas privadas. [Límites Perps](https://docs.kalshi.com/margin-rest/account/get-perps-account-api-limits). No se verificó licencia de redistribución de los datos; cualquier producto empresarial que los comparta necesita revisar los términos aplicables. [Developer Agreement, sección 3](https://kalshi-public-docs.s3.amazonaws.com/Kalshi-Developer-Agreement.pdf).

## Arquitectura adoptada

```text
Dos recursos públicos de Perps
        ↓
Captura versionada y conservada
        ↓
Validaciones deterministas de datos y unidades
        ├── Informe descriptivo con límites de evidencia
        └── Escenario hipotético de entrada/salida y costes explícitos

Documentos oficiales → revisión semántica opcional con Jev → evidencia para revisión
```

El paquete no tiene conexión con ProductionRunner, la configuración global ni las DB de BotKalshi. En esta versión tampoco hay un módulo que decida comprar/vender. La prioridad técnica es evitar resultados ficticios favorables producidos por errores de unidades, precio obsoleto, costes omitidos o datos de un mercado cerrado.

### Qué se puede reutilizar del proyecto

Repositorio revisado: `/Users/stark/Documents/ChatGPT/kalshi bot/botkalshi-live-readiness-20261006`.

Rama: `codex/live-readiness-20261006`. SHA observado: `c14312867a0a07bf3ccbddbd63a0b909d2f8fb5a`. Árbol limpio al inicio. Sólo se inspeccionó, sin editarlo.

- Patrones de decimales, paginación incompleta explícita y evidencia de `src/analytics/portfolio_report.py`.
- Patrón de lector acotado de `src/clients/kalshi_demo_readonly.py`, evitando su signer y configuración global.
- Pruebas aisladas y snapshots identificados de `scripts/test_simulator_isolated.py` y `scripts/demo_multi_readiness.py`.

No se reutiliza la fórmula de fees binarios `P × (1−P)`, precios YES/NO limitados a un dólar, liquidaciones de eventos ni reservas binarias de `simulation_bank.py` o `m5_paper_ledger.py`. Un futuro requiere posición firmada, nocional, financiación, mark, equity y margen propios.

`scripts/sonda_perps.py` es un explorador anterior con hosts candidatos y floats; no es el cliente actual. `scripts/jev_simulation.py` contiene escenarios sintéticos US500, no un motor de futuros ni evidencia de mercado. Sus resultados no se transfieren a esta validación.

Investigación anterior preservada: `/Users/stark/Downloads/botkalshi-P0-cierre/INVESTIGACION_US500_2026-10-06.md`. Sus snapshots y propuesta experimental son históricos; no se presentan como nuevos resultados.

## Jev, Odds API y recursos existentes

Se aplicó la skill TypeSafe del proyecto para definir una función acotada de Jev: revisar si una afirmación sobre el contrato está respaldada, contradicha o no cubierta por un documento. El código valida números, fechas, unidades y estados. La salida tipada de Jev no demuestra verdad contractual, ventaja financiera ni habilitación de cuenta. El diseño sigue el patrón oficial de comprobación de citas; la integración está propuesta, no ejecutada. [Cookbook TypeSafe](https://docs.typesafe.ai/cookbooks/citation_check).

The Odds API aporta cuotas deportivas y no forma parte de este flujo bursátil. No se consumieron sus créditos ni los de Jev. No se crearon servidores, suscripciones ni tareas recurrentes. El núcleo utiliza Python estándar en el Mac. El coste monetario incremental del uso de herramientas de investigación no se midió; no se declara coste total cero.

## Pendientes y condiciones de cierre

| Capa | Qué falta | Evidencia necesaria |
|---|---|---|
| Especificación del instrumento | Unidades, mínimo, tick y redondeos vigentes; modos de margen | Documentos actuales conciliados con respuesta API |
| Dataset | Trades/velas/funding, paginación y reanudación, cobertura por sesión | Captura consistente, sin duplicados/gaps ocultos, con hashes |
| Simulación temporal | Evolución de posición, cash, mark, financiación efectiva y liquidación | Pruebas adversas y conciliación contable; entradas/salidas no bastan |
| Hipótesis de estrategia | Regla numérica congelada y prueba fuera de muestra | Datos independientes y costes completos; resultados negativos conservados |
| Modelo económico | Fees reales, slippage, financiación e infraestructura | Tarifa de cuenta verificada y supuestos versionados |
| Acceso | Habilitación específica Perps y permisos Demo | Consulta de cuenta autorizada en trabajo separado; no inferir del catálogo |
| Integración de aplicación | Diseño separado del dominio deportivo y revisión | Revisión de código, pruebas integradas y aprobación correspondiente |
| Producción | No preparada por este paquete | No deducir preparación real de tests locales |

El siguiente trabajo técnico independiente del acceso de cuenta es completar ingestión histórica y validar la trayectoria de margen/funding en un simulador financiero. Una regla de inversión sólo puede evaluarse después con una muestra que permita medir costes y resultados, sin prometer beneficio.

La cuenta histórica de aproximadamente US$100 no se leyó ni se asignó a este módulo. No se determinaron tamaño de posición, exposición recomendada o rentabilidad esperada para Noel.
