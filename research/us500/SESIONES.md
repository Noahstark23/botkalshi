# Capturas públicas → sesión US500 paper

`us500_session.py` reúne capturas completas del mercado y del libro público. No reconstruye profundidad con velas/trades, no autentica y no tiene rutas de órdenes. El runner continúa siendo un simulador financiero con hipótesis explícitas.

## Ensamblaje offline

```python
from us500_session import build_session

data = build_session(snapshots, ledger_config, strategy, interval_costs, run_id)
```

El resultado usa `us500-paper-session-v1`, compatible con `us500_runner.validate_session`, `build_plan` y `replay`. No ejecuta el runner automáticamente. `ledger_config` y `strategy` deben ser configuraciones completas admitidas por éste; sus costes, tasas de margen y límites siguen siendo supuestos del simulador, no condiciones de la cuenta Kalshi.

Cada `interval_costs[i]` debe contener exactamente:

```json
{
  "from_ms": 1791744840000,
  "at_ms": 1791744841000,
  "funding_cashflow_usd": "0",
  "infrastructure_cost_usd": "0.001",
  "provenance_note": "Origen o supuesto explícito de ambos importes; no inferidos del mercado."
}
```

`at_ms` es la recepción más tardía entre mercado y libro de esa captura. `from_ms` es el `at_ms` de la captura anterior. Para la primera captura, ambos coinciden y funding debe ser cero explícito, porque no hay intervalo anterior ni posición inicial. Funding posterior es flujo USD firmado, no una tasa: positivo recibido y negativo pagado. No se obtiene automáticamente de tasas históricas, que no prueban flujo de una posición concreta. Un valor cero exige indicarlo; nunca se rellena por omisión.

La nota y los importes se conservan dentro de una copia del snapshot como `session_interval_cost_evidence`. No cambia el snapshot de entrada. La evidencia permanece dentro del frame para conservar la compatibilidad con extensiones de sesión y los hashes del prefijo guardado por el runner.

```text
python3 us500_session.py assemble --input PAQUETE.json --output NUEVA_SESION.json
```

El paquete JSON tiene exactamente las claves `snapshots`, `ledger_config`, `strategy`, `interval_costs`, `run_id`. El destino debe ser nuevo. La carga está limitada a 64 MB; JSON duplicado o no finito se rechaza.

## Validaciones previas

- Snapshot íntegro con ambos hashes canónicos, fuente/host/ticker correctos y libro abierto, válido y reciente según el núcleo.
- Mark de liquidación presente, positivo y con antigüedad de hasta 5 segundos respecto de la captura. No se sustituye por último trade, vela o mid.
- Metadata completa: `market_id`, `market_version`, `exchange_index`, `asset_class=Indices`, ticker, entorno, tick, tamaño, multiplicador y habilitación fraccional. Se fija un hash de esta identidad; cualquier cambio detiene el ensamblaje.
- Capturas estrictamente crecientes, no superpuestas, y separación menor o igual a `ledger_config.max_gap_ms`. No se ordenan silenciosamente ni se eliminan repeticiones.
- No se mezclan capturas con `synthetic=true` y `synthetic=false`. `false` es una declaración de procedencia sin autenticación criptográfica del origen; por eso `source_authenticity_verified=false` siempre.
- No se permite que snapshots afirmen `strategy_validated=true` o `execution_available=true`. El ensamblaje tampoco realiza esas afirmaciones.

## Colector limitado y recuperable

```python
from us500_session import SessionCollector

collector = SessionCollector(
    "NUEVO_JOURNAL.sqlite", collection_id="us500-observaciones-01",
    environment="public", max_gets=8,
)
snapshot = collector.capture_next("observacion-001")
evidence = collector.export()
```

Una llamada intenta un solo snapshot: mercado y, si está abierto, libro. El presupuesto es global para **ese journal**, se comparte entre instancias/reinicios y no puede ampliarse al reabrirlo. Máximo admitido: 128 GET; predeterminado: 8. No hay bucle, programación, sleeps ni reintentos automáticos. Crear otro journal representa otra colección y otro presupuesto, no un reseteo del anterior.

Cada GET queda reservado y confirmado en SQLite **antes** de enviarse. Los fallos también consumen presupuesto. Un cierre de mercado pausa después del GET de mercado y no consulta el libro. Error, metadata conflictiva o captura superpuesta pausan durablemente. Si el proceso cae con captura en curso, el reinicio la deja `UNKNOWN` y pausa con `INTERRUPTED_CAPTURE_NO_RETRY`; no se vuelve a emitir el GET dudoso. No hay comando de desbloqueo automático.

Repetir un `observation_id` completado devuelve el snapshot guardado y verificado, sin consumir GET ni duplicar observaciones. Un ID nuevo continúa usando el presupuesto restante. El journal y su lock son exclusivos; bases ajenas y enlaces simbólicos se rechazan. Los inputs de cuenta, claves, cookies, proxies de entorno y URLs arbitrarias no están admitidos.

```text
python3 us500_session.py capture-one --journal JOURNAL.sqlite --collection-id COLECCION --observation-id OBSERVACION --environment public --max-gets 8 --output NUEVA_EVIDENCIA.json
```

En pausa/error se exporta la evidencia y se devuelve código 2. Este comando público requiere una invocación explícita. Los tests usan transportes falsos; el ensayo conectado separado de V2 está documentado en VERIFICACION_V2.md.

## Evidencia de tests y límites

```text
python3 -m unittest discover -s tests -p 'test_us500_session.py' -v
```

Los tests usan transporte falso y bloquean sockets. La fixture `observed_shape_fixture` contiene datos **inventados** para comprobar estructuralmente una captura que declara `synthetic=false`; no es una observación conectada ni evidencia de mercado. Se prueba caída después de reservar un GET y después de guardar ambos cuerpos, presupuesto persistente, cierre, identidad, gaps, hashes y replay prefijo→completo→repetido sin duplicar dinero.

Este módulo no demuestra beneficio, fills reales, margen real, tasas aplicables a la cuenta ni suficiencia prospectiva. La colección guarda hashes y cuerpos recibidos; los hashes detectan cambios posteriores y no autentican a quien generó un archivo.
