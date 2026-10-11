# US500: ingestión pública de historia

Verificación documental: 11 de octubre de 2026. Este módulo solamente consulta datos públicos y verifica evidencia local. No contiene rutas de órdenes, cuentas, transferencias ni autenticación.

## Contrato Python

```python
from us500_history import PublicHistoryReader, analyze_history, load_history

reader = PublicHistoryReader(environment="public", max_pages=3)
dataset = reader.capture(resource="trades", start_ms=1791744840000, end_ms=1791744960000)
report = analyze_history(dataset)  # Offline; recalcula, no confía en dataset["analysis"].
```

Recursos admitidos: `trades`, `candles`, `funding`. Entornos: `public` y `demo`. Los tickers permitidos son respectivamente `KXUS500PERP` y `KXUS500PERP1`; se importan del núcleo local. El cliente es de un solo uso y comparte su presupuesto entre todas sus páginas/ventanas. `transport=` permite inyectar un transporte falso para tests.

## CLI

```text
python3 us500_history.py capture-history --environment public --resource trades --start-ms START --end-ms END --max-pages 3 --output NUEVO_ARCHIVO.json
python3 us500_history.py verify --input ARCHIVO.json --output NUEVO_INFORME.json
```

`analyze` es alias de `verify`. Los outputs se crean exclusivamente: no se reemplazan archivos ni enlaces simbólicos. Salida 0 significa evidencia estructuralmente válida y solicitudes agotadas; salida 2 significa error o captura parcial, cuyo archivo queda conservado cuando pudo construirse. Ni salida 0 ni `retrieval_complete=true` certifican historia suficiente o rentabilidad.

## Interfaces oficiales comprobadas

| Recurso | Ruta después de `/trade-api/v2` | Paginación y unidades |
|---|---|---|
| Trades | `/margin/trades` | `ticker`, `min_ts`, `max_ts`, `limit=1000`, cursor opaco. Instantes REST en segundos; el timestamp del registro es RFC3339. [Referencia](https://docs.kalshi.com/margin-rest/market/get-trades). |
| Velas | `/margin/markets/{ticker}/candlesticks` | `start_ts`, `end_ts`, `period_interval=1`, `include_latest_before_start=false`. La API también documenta 60/1440 minutos, pero este módulo fija un minuto. `end_period_ts` está en segundos. [Referencia](https://docs.kalshi.com/margin-rest/market/get-market-candlesticks). |
| Funding histórico | `/margin/funding_rates/historical` | `ticker`, `start_ts`, `end_ts` expresados en segundos; no se documenta cursor. Tasa firmada numérica, marca decimal y fecha RFC3339. [Referencia](https://docs.kalshi.com/margin-rest/funding/get-historical-funding-rates). |

Hosts fijos: `external-api.kalshi.com` y `external-api.demo.kalshi.co`. Ninguno requiere autenticación en estas referencias. [Separación Perps](https://docs.kalshi.com/margin).

Los rangos se reciben en milisegundos. La solicitud REST amplía los bordes un segundo y el cliente filtra de nuevo el rango exacto inclusivo; así no confunde milisegundos con segundos ni pierde bordes por la conversión. Las velas se seleccionan por su **final** de intervalo, no por toda la actividad contenida en la vela. Velas/funding se parten en ventanas de 24 horas; trades utiliza el cursor del servidor. No se inventan endpoints históricos adicionales.

## Integridad y cobertura

- Cada página guarda URL, ventana solicitada, cursor entrante, tiempos de solicitud/recepción, bytes originales en base64, SHA-256 de esos bytes, cuerpo y hash canónico. La verificación vuelve a decodificar los bytes y comprueba cuerpo, URL y secuencia.
- Los duplicados idénticos se cuentan una vez; conflicto de ID/fecha o cursor repetido detiene la captura y deja `INCOMPLETE`. No hay reintentos automáticos.
- Registros normalizados y ordenados en `analysis.records`; los importes/tasas usan `Decimal`. El JSON conserva decimales como texto. La tasa de funding numérica de la API se decodifica directamente a Decimal, nunca mediante float.
- `requested_range_ms` y `observed_range_ms` son distintos. Vacíos, registros fuera de ventana, páginas incompletas y presupuesto agotado se declaran por separado.
- Para velas se informan fronteras de minuto esperadas y huecos sin rellenar precios. Falta verificar calendario; un hueco puede ser cierre, ausencia de actividad o datos faltantes. Funding/trades no tienen una cadencia de filas garantizada: `gap_count=null`, no cero.
- `retrieval_complete` sólo significa ventanas consultadas y cursor agotado. `history_sufficient=false`, `source_authenticity_verified=false` y `strategy_validated=false` permanecen explícitos. Los hashes permiten detectar cambios, no acreditan al creador del archivo.

## Límites locales

Máximo 20 solicitudes por instancia, máximo 366 días solicitados, timeout 5 s y respuesta de hasta 2 MB; el presupuesto predeterminado es 3. Sin redirecciones, proxies de entorno, cookies, claves ni cabeceras de cuenta. Los endpoints y los tickers se construyen en código, sin URLs arbitrarias. La retención real de Kalshi y los límites efectivos del acceso anónimo no están garantizados por este módulo.

## Verificación offline

```text
python3 -m unittest discover -s tests -p 'test_us500_history.py' -v
```

Todos los tests inyectan respuestas falsas y bloquean sockets. No se realizaron consultas de mercados durante la implementación de este módulo; cualquier prueba conectada posterior debe identificarse por separado en la evidencia general.
