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
cuatro claves monetarias, no había FP previos ni pisos monetarios y no hay cuarentena. Los FP siguen
vacíos: el settlement no etiqueta esa estimación como coste observado. Una cantidad
nueva sin importes nunca hereda los totales de una cantidad anterior.

Un importe explícito inválido, contradictorio o regresivo activa
`fill_cost_quarantined`. La cantidad confirmada se conserva; el dinero dudoso se
descarta. La marca persiste al reabrir SQLite y repetir respuestas; no se levanta
automáticamente con otro mensaje. M2 conserva `pending`, la reserva original y
su pausa ante esos ACK; GET los mantiene inconclusos y settlement los rechaza.
Un ACK terminal cero pasa por el mismo escritor y no puede borrar un fill positivo
guardado mientras se esperaba esa respuesta.

Cada total tiene su último piso observado y su cantidad asociada:
`observed_cost_floor_usd_fp` / `observed_cost_floor_count_fp` y
`observed_fee_floor_usd_fp` / `observed_fee_floor_count_fp`. Los pares son
independientes: una respuesta puede actualizar la comisión sin aportar coste.
Ausencia o cuarentena no borran la historia. A igual cantidad el importe debe ser
idéntico; a mayor cantidad nunca puede disminuir. Los pisos no son costes del
acumulado actual y no se suman a posiciones, reservas o PnL.

Las columnas nuevas son nullable y la migración no inventa observaciones para
filas históricas. Un FP previo válido sólo inicia su piso con la cantidad ya
confirmada, nunca con el intent solicitado. Los pares incompletos o contradictorios
requieren revisión. Un split contable reduce la cantidad confirmada y elimina los
pisos de la orden original en el remanente; los importes se reparten conservando
su total y la hija no hereda pisos de otra cantidad. Terminalizar el mismo fill
parcial conserva su historia. Una entrada en cuarentena no puede repartirse ni
cerrarse mediante M3 o reducción: toda la transacción queda sin aplicar.

Esto no reconstruye importes borrados antes de instalar el cambio ni sustituye un
archivo completo de fills. SELL continúa sin interpretación acreditada de ingresos/collateral.

## Salidas anticipadas y reducciones

El finalizador compartido de M2/M3 y el de reducciones conservan una comisión
SELL exacta sólo si `fees_usd_fp` está validada, su espejo es coherente y su
cantidad confirmada coincide con toda la venta que se cierra. Una comisión
presente pero inválida o ligada a otra cantidad rechaza la transacción; no se
sustituye por la fórmula. El reparto FIFO conserva microUSD y deja el residuo
en el último tramo. Ausencia de FP conserva la fórmula histórica como estimación.

El precio de esas salidas sigue siendo el límite, no un precio ejecutado
acreditado. Tanto el SELL como los BUY donde se escribe el PnL llevan
`exit_accounting_estimated exit_price=limit`, más `exit_fee=observed_fp` o
`exit_fee=formula`. El lector debe conservar esta distinción aunque todos los
costes de entrada sean observados. `fill_cost_usd_fp` de SELL no se interpreta
como ingreso ni reemplaza al precio. No se añadió consulta de fills ni captura
automática nueva de datos SELL a partir del ACK.

Los finalizadores validan antes de cerrar, escriben bajo una sola transacción y
aceptan un replay coincidente sin repartir ni liquidar nuevamente. La atribución
`reduction_for=` de órdenes pendientes permanece intacta. No se reescriben
cierres históricos; este contrato se prueba con datos ficticios en
`tests/integration/test_exit_accounting_provenance.py`.

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

`tests/storage/test_order_cost_history.py` cubre ausencias intermedias, regresiones
posteriores, pisos independientes, filas legacy, reapertura SQLite, cuarentena y
splits. `test_migrations.py` comprueba migración repetible sin modificar importes
históricos; `test_fractional_lifecycle.py` comprueba conservación en ambas salidas
y rechazo atómico de una entrada en cuarentena.
