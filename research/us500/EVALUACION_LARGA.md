# Evaluación larga offline — V2.7

`us500_long_evaluation.py` ensambla hasta8 tramos consecutivos en una sola sesión
de hasta8.192 frames. No lee el capturador vivo, usa red, completa datos faltantes
ni empieza automáticamente una estrategia. No modifica los planes de captura.

## Entrada y ejecución

Archivo JSON de hasta64.000.000bytes, schema `us500-long-evaluation-v1` y cinco
campos exactos: `schema`, `run_id`, `ledger`, `strategy`, `segments`. Los parámetros
de ledger/estrategia conservan los contratos de `us500_session.py`. Cada segmento
contiene exactamente `name` (`part01`, `part02`, … en orden), `snapshots` y
`interval_costs`. Los snapshots conservan cuerpos, tiempos, identidad y hashes
originales. No transformar velas/trades en profundidad.

Cada snapshot tiene un coste explícito con `from_ms`, `at_ms`,
`funding_cashflow_usd`, `infrastructure_cost_usd`, `provenance_note`. El primer
intervalo comienza en su propia fecha; los siguientes comienzan en la fecha del
snapshot anterior, incluso entre segmentos. Cero debe ser un supuesto o hecho
identificado; nunca se completa implícitamente. Las condiciones económicas
reales y los eventos de settlement/margen siguen requiriendo evidencia externa.
Este ensamblador no infiere ni programa esos eventos opcionales del runner.

```sh
python3 us500_long_evaluation.py --input evaluacion-fijada.json \
  --database evaluacion.sqlite --output resultado-nuevo.json
```

Usar una base nueva para la evaluación aprobada y conservarla al reanudar. El
output no se sobrescribe. Las pruebas se ejecutan en SQLite temporal. Una entrada
excesiva se rechaza explícitamente; no se trunca para acomodarla al límite.

## Continuidad verificada

Todos los segmentos pasan por una validación global de identidad, secuencia,
gaps, no solapamiento y costes. Comparten capital, configuración, señal pendiente,
warmup, posición, reservas, PnL y secuencia de eventos. Los hashes de segmentos se
fijan en la sesión. Alterar un tramo ya comprometido se rechaza sin resetear dinero.
Una interrupción entre commits puede reanudar con la misma entrada/base; los
eventos idempotentes no duplican cobros. No se fuerza un cierre al final del tramo.
Los límites adicionales son100.000 eventos y8.192 órdenes ficticias por ledger.

La regresión nueva usa7.141 observaciones **inventadas** separadas por60s, a lo
largo de119h, repartidas1500/1500/1500/1500/1141. Comprueba posición cruzando
tres bordes, costes totales7.141USD y funding ficticio−0.03USD, reinicio idéntico
y lectura de un archivo superior al antiguo límite HTTP. Una prueba adicional
interrumpe justo en el inicio de un segmento y contrasta con ejecución limpia.
Son verificaciones del software, no resultados de mercado.

## Cierre pendiente

Los7.141 slots planificados incluyen un intento de observar cierre; no equivalen
a7.141 libros utilizables ni una historia continua. Tras terminar los cinco
tramos se deben auditar estados terminales, todos los hashes, identidad, horarios,
gaps, GET/disco, cierre observado y límites. No convertir UNKNOWN, cierre temprano
o slots perdidos en muestras. El gate de captura continúa bloqueando ante UNKNOWN.

Antes de evaluar mercado, fijar protocolo/configuración/hash y justificar cada
coste/funding; identificar supuestos de fees, slippage y margen. La evidencia
actual no verifica tarifa de cuenta, funding de posiciones hipotéticas ni
independencia estadística. `capture_audit_required=true` y
`complete_market_session_verified=false` siguen explícitos: el ensamblado no
certifica cobertura. Aplicar `EVALUACION_INDEPENDIENTE.md`; no ajustar parámetros
tras observar resultados y llamar a eso validación prospectiva.

El resultado sintético histórico sigue100→98.6643USD. Esta capacidad mayor no
demuestra rentabilidad, permisos Perps ni preparación para operaciones reales.
