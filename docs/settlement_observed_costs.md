# Settlement con importes observados

La reproducción local del 8 de octubre de 2026 mostró una discrepancia entre el
ledger de fills M5 y el settlement común. Un BUY ficticio de 10 contratos, límite
50c, coste acumulado USD 4.09 y comisión USD 0.03 dejaba `fill_price_cents=None`
porque su precio medio (40.9c) no era entero. El settlement recurría al límite:
calculaba USD 4.97 en vez de USD 5.88 cuando ganaba, y USD -5.03 en vez de
USD -4.12 cuando perdía. Estos importes son un caso de prueba, no una observación
de la cuenta.

## Contrato contable

- Para BUY, `fill_cost_usd_fp` representa el coste TOTAL de la cantidad ejecutada;
  `fees_usd_fp` representa la comisión TOTAL. No se multiplican de nuevo por cantidad.
- Cada importe observado disponible prevalece sobre su estimación. Un campo FP
  presente pero mal formado, fuera de límites o contradictorio bloquea el grupo;
  no equivale a un campo ausente. Cero observado sí es válido, incluso tras un split.
- El cálculo conserva hasta seis decimales USD. `pnl_cents` mantiene el piso para
  lectores legacy; `fees_cents` ausente recibe el techo de la comisión exacta.
  Un espejo de fee ya presente debe coincidir con el piso o techo del FP.
- `settlement_observed_costs` indica que ambos totales BUY estaban disponibles.
  `settlement_estimated` indica que faltaba al menos uno. No se fabrican valores FP
  para completar datos ausentes. La marca y `arb_id` sobreviven a notas largas.
- SELL conserva la fórmula histórica de la posición opuesta cuando falta coste
  observado, con marca de estimación. Se rechaza un `fill_cost_usd_fp` en SELL:
  no está acreditado si ese campo representa ingreso o colateral para este ledger.
- Se consultan las resoluciones antes de abrir la transacción de escritura.
  Bajo `BEGIN IMMEDIATE` se relee y valida el grupo seleccionado completo. Si una
  fila cambió de estado, se cerró por CLV o cambió de identidad de mercado/grupo,
  el poller no cierra las restantes ni cuenta transiciones ajenas.

No se recalculan automáticamente filas históricas ya `settled`. Los agregados de
riesgo existentes siguen sumando centavos enteros; este cambio no convierte esos
agregados en sumas de microUSD ni modifica límites, estrategias o activación.

## Captura contable M2 y M5

`src/storage/order_costs.py` registra cantidad y totales del mismo acumulado.
M5 conserva sus imports mediante el reexport de `ledger.py`. M2 utiliza el mismo
registro tanto en ACK terminal/incierto como en recuperación GET, antes de reducir
la cantidad de la fila a su porción ejecutada. Los campos de identidad presentes
deben corresponder a la intención; GET los revalida bajo el bloqueo de escritura.

M2 conserva su fallback legacy sólo si la respuesta no contiene ninguna de las
cuatro claves monetarias, no había FP previos y no hay cuarentena. Los FP siguen
vacíos: el settlement no etiqueta esa estimación como coste observado. Una cantidad
nueva sin importes nunca hereda los totales de una cantidad anterior.

Un importe explícito inválido, contradictorio o regresivo activa
`fill_cost_quarantined`. La cantidad confirmada se conserva; el dinero dudoso se
descarta. La marca persiste al reabrir SQLite y repetir respuestas; no se levanta
automáticamente con otro mensaje. M2 conserva `pending`, la reserva original y
su pausa ante esos ACK; GET los mantiene inconclusos y settlement los rechaza.
Un ACK terminal cero pasa por el mismo escritor y no puede borrar un fill positivo
guardado mientras se esperaba esa respuesta.

Límite: la comparación monetaria utiliza el FP previo disponible. Si una respuesta
sin datos ya lo eliminó, no se conserva otro piso monetario histórico. La cuarentena
durable cubre contradicciones detectadas; no sustituye un archivo completo de fills.
SELL continúa sin interpretación acreditada de ingresos/collateral.

Campos contrastados con la [documentación oficial de órdenes de Kalshi](https://docs.kalshi.com/api-reference/orders/get-order).
La documentación del formato no equivale a observar un fill conectado de este candidato.

## Evidencia offline

`tests/strategies/motor_rest_arb/test_settlement_observed_costs.py` reproduce el
fallo original y cubre precisión, datos inválidos, fees ausentes/cero, SELL,
procedencia de estimaciones, atomicidad, carrera con CLV e idempotencia.

`tests/integration/test_m2_m5_simulated_lifecycle.py` integra los ejecutores,
riesgo, compromisos, conciliadores y settlement con SQLite temporal y un
transporte ficticio sin red. Comprueba reservas compartidas, respuesta ambigua sin
reenvío, fill durante cancelación tardía, cantidades regresivas, nuevas instancias
y conexiones, resolución ganadora/perdedora y presupuesto original conservado.

Esta integración no arranca ProductionRunner ni mata un proceso del sistema
operativo. M2 y M5 conservan los totales inventados por el transporte cuando existen;
las respuestas sin importes siguen etiquetadas como estimaciones. Los tests de
`test_observed_accounting.py` y `test_order_costs.py` cubren la captura, cuarentena,
ACK cero tardío, identidad y recuperación sin reenvío. Pasar estos escenarios no prueba
fills conectados, conciliación de una cuenta, ventaja económica ni instalación
del candidato. No habilita ejecución real.
