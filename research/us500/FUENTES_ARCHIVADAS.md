# Fuentes públicas archivadas de US500

Captura: **11 de octubre de 2026, 20:10:12–20:10:13 UTC**. Alcance: seis GET de documentos públicos ya referenciados en `CONTRATO_Y_LIMITES.md` e `INVESTIGACION_Y_PLAN.md`. Sin autenticación, proxies de entorno, redirecciones, reintentos ni consultas de mercado/cuenta.

**Resultado: cinco fuentes archivadas y una fuente no disponible (HTTP 429).** El YAML OpenAPI coincide byte por byte, por SHA-256, con la versión previamente citada. El PDF CFTC de 32 páginas y el Developer Agreement de 6 páginas se abrieron y extrajeron localmente.

| Fuente y URL original | Archivo local | Estado |
|---|---|---|
| [OpenAPI oficial Perps](https://docs.kalshi.com/perps_openapi.yaml) | [01_perps_openapi.yaml](evidence/SOURCES/01_perps_openapi.yaml) | Archivada; HTTP 200; formato/contenido identificador verificado |
| [Filing CFTC inicial US500](https://www.cftc.gov/filings/ptc/ptc08182617972.pdf) | [02_cftc_ptc08182617972.pdf](evidence/SOURCES/02_cftc_ptc08182617972.pdf) | Archivada; HTTP 200; formato/contenido identificador verificado |
| [Tarifario público Perps](https://kalshi.com/fee-schedule?product=perps) | [03_kalshi_fee_schedule_perps.html.http-error-response](evidence/SOURCES/03_kalshi_fee_schedule_perps.html.http-error-response) | **No disponible: HTTP 429. Archivo es respuesta de error, no tarifario.** |
| [Explicación oficial de comisiones Perps](https://help.kalshi.com/en/articles/16071417-perps-fees-explained) | [04_perps_fees_explained.html](evidence/SOURCES/04_perps_fees_explained.html) | Archivada; HTTP 200; formato/contenido identificador verificado |
| [Developer Agreement público](https://kalshi-public-docs.s3.amazonaws.com/Kalshi-Developer-Agreement.pdf) | [05_developer_agreement.pdf](evidence/SOURCES/05_developer_agreement.pdf) | Archivada; HTTP 200; formato/contenido identificador verificado |
| [Registro CFTC US500PERP](https://www.cftc.gov/IndustryOversight/IndustryFilings/TradingOrganizationProducts/62926) | [06_cftc_product_62926.html](evidence/SOURCES/06_cftc_product_62926.html) | Archivada; HTTP 200; formato/contenido identificador verificado |

## Integridad y trazabilidad

[INDEX.json](evidence/SOURCES/INDEX.json) conserva URL solicitada/efectiva, método, fecha UTC de solicitud/recepción, estado HTTP, Content-Type, Last-Modified cuando fue entregado, número de intentos, tamaño y SHA-256 de cada cuerpo recibido. Los archivos originales se guardaron sin transformar sus bytes. Los `.extracted.txt` son derivados para lectura offline; ante diferencias, revisar el PDF/HTML original.

| Archivo original o respuesta | Bytes | SHA-256 |
|---|---:|---|
| `01_perps_openapi.yaml` | 150288 | `27e4b0b76a686f797342f1526a08df5b8f60d8104a81b65c94073d3d89b54308` |
| `02_cftc_ptc08182617972.pdf` | 442554 | `81d77bf2996bada00a37dedd2748e37641fb0c2c5fa11c16bec6a17fa9934be6` |
| `03_kalshi_fee_schedule_perps.html.http-error-response` | 34145 | `fd4c4112dfb3dc4f9b81d5c4cc8934ae205c726d009200d2fde2ecfe22fb23f1` |
| `04_perps_fees_explained.html` | 122795 | `148a4048556a4cc0f41fa4b26eb8457be7744b29cd490d26ecdebe21d6d8bfbb` |
| `05_developer_agreement.pdf` | 125685 | `9d3e33685c0f0a88d23895685107975b0745283aac96f8bf7d202f5ae4dc1fa6` |
| `06_cftc_product_62926.html` | 31046 | `bd8793777f55c5793a26588dc722474e10373565ff34788df69aab0e67b9b56d` |

Verificación offline desde la raíz de este worktree:

```bash
python3 - <<'PY'
from pathlib import Path
import hashlib, json
root = Path('research/us500/evidence/SOURCES')
index = json.loads((root / 'INDEX.json').read_text())
for item in index['sources']:
    raw = (root / item['file']).read_bytes()
    assert len(raw) == item['bytes']
    assert hashlib.sha256(raw).hexdigest() == item['sha256']
    derivative = item.get('offline_text_derivative')
    if derivative:
        raw = (root / derivative['file']).read_bytes()
        assert len(raw) == derivative['bytes']
        assert hashlib.sha256(raw).hexdigest() == derivative['sha256']
print('6 cuerpos y 4 derivados verificados; 5 fuentes útiles, 1 error HTTP')
PY
```

## Alcance de la revisión offline

- **OpenAPI:** quedó archivada exactamente la versión citada con hash `27e4b0b76a686f797342f1526a08df5b8f60d8104a81b65c94073d3d89b54308`. Permite revisar los esquemas técnicos sin depender del sitio. No acredita comportamiento de una cuenta ni equivalencia económica con el filing.
- **CFTC:** el PDF corresponde al filing inicial del 18 de agosto de 2026. El registro HTML identifica `US500PERP`, estado `Approved` y fecha `2026-10-03`. No se archivaron ni verificaron todas las enmiendas posteriores; no se declara que el filing sea el contrato íntegro actualmente aplicable.
- **Fees:** la explicación oficial archivada respalda cálculo sobre nocional y cobro en apertura/cierre, pero no muestra las tasas `0,120%` y `0,020%`. La descarga directa del tarifario devolvió **429**. Por tanto, esas tasas previamente mencionadas siguen sin respaldo archivado del tarifario en este paquete. No se infieren tarifas privadas, tiers, redondeos ni cargos efectivos.
- **Developer Agreement:** se conserva el PDF público recibido y su versión visible `v1.1`. Capturarlo no determina su vigencia para una cuenta, licencia de redistribución específica ni aceptación de términos.
- **Cobertura documental:** esta selección agotó el máximo de seis descargas. No se archivaron por separado todas las páginas de ayuda/documentación enlazadas en los documentos existentes. El alcance pendiente es esas páginas, el tarifario accesible y cualquier enmienda que corresponda; no se amplió automáticamente la descarga.

No se cambiaron términos, reglas de estrategia, fuentes originales del repositorio, manifiestos existentes ni flags de ejecución. Este archivo no certifica rentabilidad, preparación real ni permisos de Perps.
