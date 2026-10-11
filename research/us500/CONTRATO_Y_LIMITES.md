# US500 V1 — contrato, unidades y límites del modelo local

Fecha de revisión: 11 de octubre de 2026. Alcance: documentación pública oficial y revisión de archivos locales. No se consultaron cuentas, claves, balances, permisos privados ni endpoints de mercado durante esta revisión; no se enviaron órdenes.

Este documento define el contrato de aceptación de un motor financiero **paper local**. Aprobarlo acredita comportamiento del software bajo sus entradas y supuestos. No acredita habilitación de Perps, reproducción exacta del margen del exchange, fills reales, ventaja económica ni preparación de producción.

## 1. Identidad del producto y jerarquía de fuentes

La CFTC registra `US500PERP` como futuro financiero sobre índice bursátil, aprobado el 3 de octubre de 2026. Kalshi anunció US 500 Perpetual el 6 de octubre. Es un producto financiero distinto de los contratos binarios deportivos. [Registro CFTC](https://www.cftc.gov/IndustryOversight/IndustryFilings/TradingOrganizationProducts/62926), [anuncio de Kalshi](https://pro.kalshi.com/pro/updates/us-500-perpetual).

El filing de agosto identifica MerQube US Large Cap, un índice de precios; no representa una participación en un ETF ni el S&P 500. Describe liquidación en efectivo, sin vencimiento, y compensación por Kalshi Klear. El contrato base tiene multiplicador de US$1 por punto y tick de 0,05 puntos; su mínimo contractual inicial es 0,0001 contratos, sujeto a especificación del mercado. La negociación está prevista de domingo 18:00 a viernes 17:00 ET, con posibles suspensiones. Funding al cierre regular del índice, normalmente 16:00 ET, ajustado por cierre anticipado. Su fórmula inicial promedia primas de minutos válidos; excluye intervalos sin cálculo vigente o sin operaciones. La banda cero es 0,002% y el límite ±2%. El precio de liquidación utiliza una jerarquía: VWAP de 60 segundos, puntos medios, cambio del índice y contingencia reglamentaria. Márgenes inicial y de mantenimiento son dependientes del riesgo. **Estas son reglas del filing inicial; no se verificaron todas las enmiendas posteriores.** [Filing oficial, apéndice A](https://www.cftc.gov/filings/ptc/ptc08182617972.pdf).

Para una implementación conectada, conciliar: contrato vigente y enmiendas → esquema API vigente → metadata del instrumento y parámetros efectivos → observaciones fechadas. Un ejemplo genérico de Bitcoin, una página comercial o el fixture de una prueba no reemplazan la regla específica de US500.

## 2. Unidades: no multiplicar dos veces por `contract_size`

La documentación API define precios como USD, `tick_size` como incremento monetario y `reference_price` como referencia ya escalada por contrato. El schema `MarginRiskPosition` explicita nocional absoluto = `abs(qty) * mark_price`. `contract_size` es texto de seis decimales; `underlying_multiplier` describe unidades subyacentes por unidad de tamaño contractual. Las cantidades API tienen granularidad 0,01 cuando se admiten fracciones; con `fractional_trading_enabled=false`, exigir enteros. Conservar los precios como decimales y comprobar el tick; las respuestas pueden tener hasta seis decimales. [Esquema oficial Perps](https://docs.kalshi.com/perps_openapi.yaml), [ficha de mercado](https://docs.kalshi.com/margin-rest/market/get-market).

Convención interna del motor, una vez normalizada la entrada:

```text
q       = cantidad firmada en unidades API (larga positiva; corta negativa)
p       = USD por unidad API
notional_usd = abs(q) * p
unrealized_pnl_usd = q * (mark_usd_per_api_unit - cost_basis_usd_per_api_unit)
```

**No multiplicar esas fórmulas otra vez por `contract_size` ni `underlying_multiplier`.** La conversión desde puntos del índice es un paso separado, explícito y validado; no inferirla porque el precio parezca pequeño. Para cierre parcial, usar únicamente la cantidad cerrada y el precio de cada fill. No tratar una entrada corta como ingreso libre del nocional ni una entrada larga como compra spot que consume todo el nocional.

La observación pública archivada por trabajo anterior contiene `contract_size=0.001000`, `underlying_multiplier=1.000000`, `tick_size=0.0001` y fracciones deshabilitadas. Es metadata observada del API, no demostración de equivalencia con el contrato base del filing. Los identificadores de ese archivo son `KXUS500PERP` en público y `KXUS500PERP1` para Demo según la investigación anterior. No confundirlos con el nombre regulatorio `US500PERP`; no sustituir tickers entre entornos. Archivo: `OBSERVACION_PUBLICA_2026-10-11.json` e `INVESTIGACION_Y_PLAN.md`.

## 3. Margen, costes y liquidación: qué puede afirmar V1

La API pública de riesgo distingue umbral de entrada a cola de liquidación, umbral de liquidación y multiplicador de margen inicial. El margen inicial es mantenimiento por ese multiplicador. La documentación advierte que el apalancamiento estimado varía con tamaño; las métricas individuales pueden faltar cuando hay compensación de portfolio. Una fracción fija elegida en un archivo local **no reproduce** esa metodología. [Parámetros de riesgo](https://docs.kalshi.com/margin-rest/risk/get-risk-parameters), [riesgo de cuenta](https://docs.kalshi.com/margin-rest/risk/get-risk).

Un simulador puede aceptar porcentajes inicial/mantenimiento, cargos de liquidación y reglas de reserva **como política configurable de escenario**, guardando su versión y procedencia. Debe rotularlos `local_model`, nunca `venue_equivalent`. El disparo de liquidación simulada debe conservar el mark que lo produjo, y el cierre usar un precio ejecutable del libro con profundidad y costes. Si no hay profundidad suficiente, queda exposición residual explícita; no inventar un cierre completo al mark. Un gap puede producir déficit: no truncarlo silenciosamente a cero ni afirmar pérdida máxima igual al margen aportado. [Explicación oficial de margen y límites](https://help.kalshi.com/en/articles/15357594-how-margin-works), [liquidación](https://help.kalshi.com/en/articles/15357646-understanding-liquidation).

La tarifa comercial consultada muestra 0,120% taker y 0,020% maker para nivel inicial, sobre nocional y en ambas patas. Eso no determina la tarifa de esta cuenta ni su redondeo aplicable. La API tiene tasas efectivas por mercado y tarifario por cuenta habilitada. El motor necesita tarifas de escenario explícitas, redondeo versionado y desglose entre comisión, spread/slippage y funding. [Tarifario](https://kalshi.com/fee-schedule?product=perps), [fees](https://help.kalshi.com/en/articles/16071417-perps-fees-explained), [tasas efectivas](https://docs.kalshi.com/margin-rest/fees/get-fee-tiers).

## 4. Funding y secuencia temporal

Mantener distintos `reference_price`, `settlement_mark_price` y `liquidation_mark_price`, cada uno con su timestamp. No sustituir uno por otro silenciosamente. Un midpoint no es fill ni precio autorizado de liquidación.

La documentación genérica del estimador de funding describe un promedio temporal de un período todavía abierto; su valor puede cambiar hasta el cierre. El archivo inicial de US500 describe una metodología específica por minuto. Esa diferencia impide certificar una réplica del exchange usando sólo una fórmula local genérica. Para replay, dar preferencia a tasas históricas finales con mercado, hora y mark explícitos. La historia de pagos de cuenta distingue importe recibido positivo y pagado negativo. [Estimación](https://docs.kalshi.com/margin-rest/funding/get-funding-rate-estimate), [tasas históricas](https://docs.kalshi.com/margin-rest/funding/get-historical-funding-rates), [pagos](https://docs.kalshi.com/margin-rest/funding/get-funding-history).

Modelo paper lineal propuesto, con tasa final expresada como fracción y signo largo/corto:

```text
funding_cashflow_usd = -q_at_event * settlement_mark_usd_per_api_unit * funding_rate
```

Debe documentarse si una posición abierta exactamente a la hora del funding participa; ante secuencia ambigua, rechazar o exigir orden explícito, nunca seleccionar el resultado favorable. No cobrar funding antes de abrir ni después de cerrar. Repetir el mismo identificador no puede duplicar el pago; mismo identificador con contenido diferente es conflicto. Falta de tasas no significa tasa cero. El calendario debe manejar `America/New_York`, cambio de horario, fines de semana, feriados y cierres anticipados, o exigir eventos finales explícitos sin calcular el calendario.

## 5. Aceptación técnica del motor paper local

Esta lista es criterio de revisión, **no una declaración de pruebas aprobadas**. El informe de integración debe enlazar cada punto con tests y resultados reproducibles.

- [ ] Ejecuta offline con datos versionados, sin signer, credenciales, llamadas autenticadas, órdenes externas ni acceso a las DB históricas del bot.
- [ ] Validaciones de esquema/ticker/entorno, decimal finito, signo, granularidad, tick, tiempos y metadata; datos contradictorios bloquean el cálculo.
- [ ] No hay consumo duplicado de `contract_size`; casos largos y cortos validan notional/P&L con fórmulas independientes del código.
- [ ] Reserva antes del fill; parciales y cancelaciones liberan sólo lo no ejecutado; fondos libres, reservas y posición permanecen reconciliados.
- [ ] Entrada, incremento, cierre parcial, cierre total y reversal se definen explícitamente; una reducción no abre involuntariamente la posición contraria.
- [ ] Fees, slippage y funding permanecen separados y tienen supuestos obligatorios; resultados negativos y déficits se conservan.
- [ ] Mark periódico, cambios de margen y liquidación parcial/total se simulan con exposición residual y fallos de liquidez visibles.
- [ ] Registro durable/transaccional antes de reconocer efecto; reinicio reproduce dinero/posición exactamente y detecta eventos duplicados o contradictorios.
- [ ] Cobertura temporal, procedencia, hashes y huecos se reportan; no rellena datos futuros ni convierte captura no atómica en historia continua.
- [ ] Un fill maker requiere modelo explícito de cola y prioridad; tocar un precio no garantiza fill. Si no hay modelo maker, declarar alcance taker únicamente.
- [ ] Reporte final separa estado contable, exposición residual, calidad del dataset y métricas de estrategia. `paper_ready` no implica `live_ready` ni rentabilidad.
- [ ] CLI y pruebas de extremo a extremo cubren proceso interrumpido/reanudado, JSON inválido, persistencia fallida, errores de paginación y configuración incompatible.

Un módulo determinista que sólo evalúa escenarios manuales puede quedar técnicamente terminado con ese alcance. Para llamarlo motor de estrategia completo debe existir, además, una regla de señal explícita/versionada y tests sin anticipación; no inferir decisiones de inversión de una cotización, de Jev ni de este documento.

## 6. Pendientes externos que no bloquean construir el simulador

| Pendiente | Evidencia que falta | Qué puede hacerse localmente |
|---|---|---|
| Acceso Perps específico | Solicitud/aprobación, tutorial y permisos de cuenta | Rechazar modo real; fixtures de acceso permitido/denegado |
| Parámetros económicos aplicables | Tarifa, margen, caps y modo aislado/portfolio de cuenta y versión | Escenarios explícitos, cambios de parámetros, fallos cerrados |
| Conciliación contractual | Enmiendas vigentes y unidad API efectiva | Mantener contrato base y APIunit separados; bloquear conversión incierta |
| Dataset independiente suficiente | Trades/libro/funding, cobertura, retención, sesión y licencia | Ingestión/replay de archivos, pruebas sintéticas, detección de faltantes |
| Ventaja económica | Muestra futura o fuera de muestra, costes completos y comparación | Protocolo de evaluación congelado; no declarar beneficio con fixtures |
| Integración real | Estado privado, lifecycle de órdenes, fills, conciliación y revisión de despliegue | Interfaces separadas sin implementación de transmisión en V1 |

Perps requiere una aprobación propia; la cuenta verificada para predicciones no basta. Esta revisión no determina si Noel ya tiene ese acceso. [Proceso oficial](https://help.kalshi.com/en/articles/15357656-applying-for-perpetuals-access).

## 7. Revisión inicial de código y trazabilidad

Se revisaron `us500_engine.py`, `us500_runner.py`, `us500_ledger.py`, `us500_history.py` y sus pruebas disponibles durante la integración. Se comunicaron los siguientes hallazgos; los cambios los realizaron sus respectivos responsables, no el autor de este documento.

| Hallazgo | Efecto | Estado observado al segundo pase |
|---|---|---|
| Cantidades fraccionales más finas que 0,01 | Simular una cantidad no representable por la API | El núcleo contiene `OFF_API_QUANTITY_STEP` y regresión para `0.001`; revisar suite integrada |
| Validar el mark con tick de ejecución | Rechazar un mark válido de mayor precisión | Comprobación independiente en memoria: mark `13.70005` con tick `0.0001` admitido |
| Funding distinto de cero con posición cero | Acreditar beneficio sin exposición | Comprobación independiente: `FUNDING_WITHOUT_POSITION`, cash intacto y sin evento añadido |
| Reutilizar el mismo snapshot como observación siguiente | Generar un fill con el mismo libro que produjo la señal | Reproducido y corregido: mismo capture en el siguiente frame ahora da `REUSED_OR_OVERLAPPING_CAPTURE`, cero fills y cash intacto |

El runner revisado usa una hipótesis momentum congelada y retarda el uso de liquidez hasta una captura posterior. El ledger conserva dinero y exposición al pausar por riesgo; **no implementa la liquidación del venue ni fabrica fills de liquidación**. La historia capturada de trades, velas y funding no contiene por sí sola la trayectoria del libro ni del mark de liquidación necesaria para construir todos los frames del runner. Esa conexión requiere datos adicionales y no se deduce de pasar tests sintéticos. La suite completa y su conteo corresponden al informe de integración; esta revisión no los vuelve a certificar.

Documento técnico oficial consultado sin autenticación: `https://docs.kalshi.com/perps_openapi.yaml`. SHA-256 observado: `27e4b0b76a686f797342f1526a08df5b8f60d8104a81b65c94073d3d89b54308`. Sirve como identificación de la versión leída; el YAML no fue guardado por esta revisión. La guía general confirma hosts Perps propios, restricciones de recursos y rollout por miembro. [Guía Perps](https://docs.kalshi.com/margin).

Este archivo no fija capital real, no recomienda apalancamiento ni promete rendimiento para una fecha. Diciembre no forma parte del criterio técnico de aceptación.
