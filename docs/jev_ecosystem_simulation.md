# Simulación aislada Odds → M2 → M5 con filtro Jev

Este experimento comprueba conexiones y contabilidad con 22 trayectorias ficticias.
No es un backtest histórico ni una medición de rentabilidad. No cambia `src/`,
settings, despliegues, cuentas ni órdenes. Jev juzga si un texto anuncia una suspensión;
no estima probabilidades deportivas, precios justos ni ganancias.

## Reproducir

Desde el checkout, con el Python y dependencias del proyecto:

```sh
# Preparación gratuita: congela hash del fixture, prompt y política.
python -m scripts.jev_ecosystem_judgments --output preparacion-nueva.json

# Opcional: 6 textos únicos × 2 repeticiones = máximo 12 consultas sin reintentos.
# Lee únicamente ~/.config/botkalshi/typesafe-api-key; nunca pasar la clave por CLI.
python -m scripts.jev_ecosystem_judgments --api --output juicios-nuevos.json

# Reusar los juicios ya guardados NO vuelve a gastar API.
python -m scripts.jev_ecosystem_replay \
  --cases tests/fixtures/jev_ecosystem_cases.json \
  --judgments juicios-nuevos.json --output replay-nuevo.json
```

Cada salida exige un archivo nuevo. El worker de replay debe ejecutarse como proceso
dedicado: limpia el entorno, deshabilita dotenv, bloquea sockets y constructores de
clientes reales antes de importar motores. Usa y destruye una SQLite temporal por
escenario y brazo. Conserva quotes y fills exportados en JSON.

## Qué código se ejercita

1. Parsers reales de Odds/Kalshi y `find_signals` de M2 calculan consenso no-vig sobre
   tres casas ficticias. Se conservan fecha, kickoff, identidad y procedencia.
2. El harness publica explícitamente sólo PHI en `FairValueBook`, dentro del proceso
   aislado. No modifica `FakeOddsSource.is_live` ni el guard del poller productivo.
3. `Motor5Engine._tick` real consume fair y books ficticios. `SeriesFeePolicy` real
   recibe metadatos ficticios de serie/evento, con multiplicador MLB 0.5 como supuesto
   de prueba. No se consultó ni validó una tarifa vigente de la cuenta.
4. Quoter, inferencia de fills por cruce estricto, profundidad observable, inventario,
   comisiones y ledger usan el código existente. Máximo un contrato observable por
   fill, half-spread 5¢, retiro por salto 5¢, mínimo tres casas, edad máxima 15 minutos,
   TTL 600 segundos y exclusión de los últimos 120 segundos antes del partido.
5. La comparación usa los mismos frames y parámetros. El filtro Jev sólo actúa antes
   de la primera quote; no cambia precios, cantidad ni cierres de posiciones existentes.

`exchange_environment=production` es el metadato que exige el constructor F1 maker.
`trading_enabled=False`, executor/reconciler ausentes y bloqueo de red se comprueban.
Ninguna fila exportada constituye una orden en producción.

## Política congelada para Jev

Modelo `jev-1.13.0`, dos solicitudes idénticas por texto. Permitir sólo si ambas
probabilidades de suspensión son ≤0.1; bloquear si ambas ≥0.9. Una diferencia >0.02,
respuesta faltante o caso intermedio produce REVIEW y cero quotes en este experimento.
Estos umbrales son experimentales, no están calibrados para noticias reales.

Se envían exclusivamente equipos/fecha y texto. Etiquetas, IDs descriptivos, precios
futuros y PnL permanecen locales. La API no se consulta dentro del motor: primero se
archivan juicios, después se ejecuta el replay offline. Se validan hashes del fixture y
prompt, modelo y coherencia entre probabilidades y decisión. Los hashes vinculan
archivos; no equivalen a una firma de autenticidad del proveedor.

Hay seis textos evaluados, no 22 observaciones independientes de inteligencia. El
mismo anuncio de suspensión se prueba con una trayectoria ganadora y otra perdedora:
permite observar tanto una pérdida evitada como una ganancia a la que se renuncia.
Falta de noticia y caída de Jev son fallos simulados, no incidentes del proveedor.

En el caso de kickoff cercano se adelanta la hora estructurada del evento para
ejercitar el guard, conservando la noticia inicial que menciona las 19:00 UTC.
Jev sólo juzga suspensión por equipos/fecha; no verifica consistencia de horarios.
El motor usa exclusivamente el kickoff estructurado para su exclusión temporal.

## Lectura de resultados y límites

`realized_cents` existe sólo con inventario neto cero y trayectoria válida.
`mtm_cents` de una posición abierta usa el último mid observable y no incluye fee de
salida; nunca se presenta como ganancia cobrada. El libro ausente deja el resultado
sin valoración válida; si desaparece con quotes expuestas, la época queda inválida.
El cierre del harness observa el último intervalo, no inventa una liquidación.

El replay no mide prioridad de cola, ejecución real, latencia de proveedores, slippage
real, disponibilidad de cuotas, autenticidad de noticias ni PnL fuera de muestra.
Tampoco ejercita el daemon completo, recuperación, cash real, órdenes, reconciliador
o settlement. Los markouts que requieran más horizonte quedan pendientes.
Los totales de escenarios elegidos a mano no permiten estimar ROI o ingresos diarios.

La API informa tokens; su costo estimado se reporta aparte usando la tarifa consultada
de $0.042 por millón de tokens de entrada. No incluye suscripción Odds, infraestructura
ni gastos reales de operar. Un error con consumo desconocido se registra como tal.

Fuentes consultadas el 6 de octubre de 2026:
[semántica Noul](https://docs.typesafe.ai/primitives/noul),
[modelos y tarifa](https://docs.typesafe.ai/models),
[campos de The Odds API](https://the-odds-api.com/liveapi/guides/v4/).
