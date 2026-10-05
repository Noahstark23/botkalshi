# Reporte financiero readonly, candidato de instalación

Este paquete está separado de los motores y no los activa. Se instala solamente
tras revisión y autorización de Noel. No modifica la whitelist SSH existente.
Los archivos systemd son plantillas; todavía no se verificaron en Ubuntu ni se
instalaron. El acceso completo sigue siendo exclusivo de Noel.

## Qué aporta

- `python -B -m scripts.check_portfolio --json --subaccount 0`: cash y valor de
  cartera reportados, posiciones fraccionarias, resting, fills/fees efectivos y
  liquidaciones. Recorrido de páginas acotado y exclusivo a la subcuenta indicada.
- `python -B -m scripts.export_operator_report --output /ruta/privada/report.json`:
  publica el resultado completo mediante reemplazo atómico, archivo 0600, sin DB.
- `python -B -m scripts.read_operator_report /ruta/privada/report.json`: sin red ni
  llaves; exige esquema completo y rechaza un informe con más de 600 segundos.
- `python -B -m scripts.check_performance periodo.json`: resultado del período
  excluyendo aportes/retiros y restando costos pagados fuera de la cuenta una vez.

`OK` significa lectura válida, nunca autorización de órdenes ni estrategia rentable.
Datos incompletos producen ATENCION y totales desconocidos. Las lecturas no son
atómicas. Un `updated_ts` de saldo antiguo puede indicar que no hubo movimientos;
la caducidad se mide desde la consulta, no desde la última modificación del saldo.
El fingerprint es SHA256 del Key ID, que no se imprime. Rotar la llave exige una
nueva base de medición o reconciliar explícitamente las fuentes; no unirlas a ciegas.

## Resultado por período y aportes mensuales

El JSON de entrada contiene `opening` y `closing`, los snapshots originales;
`cash_flows_complete: true` tras revisar el registro completo; y
`boundary_activity_excluded: true` tras comprobar que no hubo operaciones ni flujos
mientras se tomaban los snapshots. No basta escribir estos campos: son confirmaciones
sobre evidencia externa. Los exports y este cálculo no verifican automáticamente
el registro de depósitos, retiros ni costos.

`cash_flows` contiene movimientos con `id` único, `at` ISO con timezone, `amount_usd`
como string decimal y `kind`: `deposit`, `withdrawal` o `external_cost`. Los depósitos
son el monto realmente acreditado, los retiros el monto realmente debitado. Incluir
también transferencias entre subcuentas como entradas/salidas del ámbito medido. Un costo
externo exige `paid_outside_account: true`; no agregar aquí fees ya debitadas por Kalshi.
Incluir movimientos desde el comienzo del snapshot inicial hasta el final del último;
si un flujo ocurre durante cualquiera de las lecturas, el cálculo queda desconocido
hasta repetir/reconciliar la frontera. Los movimientos intermedios deben ser exhaustivos.

Se calcula solo cuando ambos snapshots tienen cero posiciones, exposición, órdenes
resting y valor de cartera pendiente, con misma fuente/subcuenta y saldo estable.
Con posiciones abiertas exige una valuación independiente y devuelve ATENCION; no
inventa precios. El campo `account_net_pnl_usd` corresponde a toda la cuenta, no a un motor individual
ni exclusivamente a trading. Otros ingresos de la cuenta no prueban alpha de un motor.

Ejemplo sintético: cash inicial 200, aporte acreditado 200, cash final 420 ->
resultado de cuenta 20. Hosting pagado fuera de la cuenta 6 -> resultado del proyecto
14. Con cash final 390 -> pérdida de cuenta 10, aunque el saldo total haya aumentado.
No se vuelven a descontar comisiones ya reflejadas en el cash.

## Instalación preparada para revisión, no ejecutada

1. Revisar commit/paquete y recursos. La plantilla usa usuario `botkalshi-runtime`,
   sus credenciales existentes y Python de su venv. Confirmar permisos, cuota API compartida y que la
   subcuenta 0 corresponde a la fuente prevista. No copiar ni mostrar llaves.
2. Instalar un release sellado bajo `/opt/botkalshi-operator-report/releases/<SHA>`
   y su propio `current`, propiedad de root. No mover `current` del runtime existente.
   La ruta del paquete entra por `PYTHONPATH`; no instala dependencias adicionales.
3. Revisar la plantilla de servicio/timer y validar con `systemd-analyze verify` en
   Ubuntu. Confirmar RAM/CPU/disco con el resto de workloads antes de iniciar.
4. Ejecutar una exportación manual autorizada. Validar propietario, 0600 y ausencia
   de secretos. El marcador `/etc/botkalshi-runtime/OPERATOR_REPORT_REVIEWED` pertenece
   a Noel; su ausencia impide el arranque de la unidad.
5. Si Noel autoriza periodicidad, instalar/habilitar el timer de cinco minutos.
   Comprobar dos exports frescos y simular un error: ATENCION o TTL, nunca OK viejo.
6. Noel puede agregar UN comando exacto, por ejemplo `operator-report`, al wrapper
   SSH. Su implementación interna fija programa, entorno y path; no acepta argumentos,
   shell, URLs o paths del solicitante. Ejecuta Python con `-B` y PYTHONPATH del release
   readonly para leer `/var/lib/botkalshi-operator-report/report.json`. Probar que el
   comando funciona y que las restricciones anteriores siguen vigentes. No se entrega
   la llave full a la IA ni se intenta un comando bloqueado por un canal alternativo.

La falla del reporte no gatea trading y no borra posiciones. Su timer tiene presupuesto
independiente y no debe reiniciar el bot. Reversión: Noel detiene/deshabilita únicamente
este timer/servicio y retira el comando nuevo de la whitelist. Conservar snapshots
necesarios para auditoría; los servicios y datos financieros originales no se sustituyen.

## Límites y fuentes

No certifica kill-switch, flags, SHA del proceso Coolify ni salud de sus motores. No
cubre automáticamente fills archivados fuera del endpoint reciente. Se retiraron del
CLI viejo la atribución aproximada por ticker, el desglose inventado de portfolio value
con revenue histórico y la comparación contra bankroll configurable. La atribución
por motor requiere un ledger conciliado por order_id y pruebas de su procedencia.

Contrato API verificado el 29 de septiembre de 2026: [balance](https://docs.kalshi.com/api-reference/portfolio/get-balance),
[posiciones](https://docs.kalshi.com/api-reference/portfolio/get-positions),
[órdenes](https://docs.kalshi.com/api-reference/orders/get-orders),
[fills](https://docs.kalshi.com/api-reference/portfolio/get-fills),
[liquidaciones](https://docs.kalshi.com/api-reference/portfolio/get-settlements).
`fee_cost` de fills está denominado en dólares; jamás se multiplica el cargo por cantidad.
Los helpers de ejecución/calculo de fees de los motores no fueron alterados.
