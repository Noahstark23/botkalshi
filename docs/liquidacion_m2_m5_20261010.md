# Liquidación M2/M5: revisión local del 10 de octubre de 2026

El simulador distingue el pago contractual de una devolución de la apuesta.
`football_settlement_review.py` compara nueve dimensiones y separa liga,
temporada regular y periodo del mercado. Ninguna coincidencia parcial certifica
el fair de M2 ni habilita M5. No hay cambios en ejecutores, flags ni producción.

## Fuentes y alcance de la revisión

- [Contrato FOOTBALLGAMEWIN](https://assets.kalshi.com/contract_terms/FOOTBALLGAMEWIN.pdf),
  observado 2026-10-10 19:20:57 UTC, SHA-256
  `57b57e1ad96466e413e9274c0d2598225997e35d71262eb63c4ae13858b81d38`.
  El documento incluye prórroga. Sin strike de empate, contempla 50¢ para cada
  lado; con strike Tie, el pago de los equipos cambia. Contempla precio fijado por
  Kalshi ante determinadas cancelaciones, suspensiones, forfeits previos y cambios
  de sede. La revisión conserva las excepciones de final oficial y resultado ya
  determinado. No calcula ese precio a partir del último trade. Sólo se aplica
  esta lectura a los seis textos exactos ya revisados, como referencia actual;
  no demuestra qué versión del PDF estaba publicada en la captura anterior.
- [Fanatics MA](https://sportsbook.fanatics.com/legal/ma/house-rules/):
  moneyline 2-way incluye prórroga y devuelve la apuesta si hay empate. La semana
  regular NFL va de jueves a miércoles; NCAA, de martes a lunes. Playoffs tienen
  excepciones distintas. Se revisaron también cancelación sin reprogramación,
  forfeit previo, reversión de localía, replay completo y correcciones. El umbral
  de suspensión queda sin completar: el párrafo refiere un tiempo sin precisarlo.
- [FanDuel NJ, 31-mar-2026](https://d38ayms4az88sz.cloudfront.net/SB/NJ/2026-03-31T12-47-12.html):
  prórroga incluida, devolución en empate. Se separan suspensión con plazo de 24h
  y reprogramación de 60h. La referencia NFL a la semana define el kickoff inicial;
  no exige terminar en esa semana. No se extiende esa referencia NFL a NCAA.
  Quedan sin completar la reversión nominal de localía y correcciones.
- DraftKings conserva únicamente la ficha parcial de moneyline ya revisada.
  La ayuda general de fútbol encontrada y reglas antiguas tienen umbrales
  diferentes; no se combinan versiones para completar una ficha actual.

Las fuentes y sus hashes están en el informe local de evidencias. Las cuotas
regionales `us` archivadas no contienen el vínculo a estado/versión contractual.
La ampliación investigó las once casas: hay nueve fichas parciales para NFL y
ocho para NCAA. Eso no demuestra equivalencia ni ventaja. Se conservan estos
alcances, sin extrapolar reglas de otros tipos de apuesta:

- [BetOnline](https://help.betonline.ag/en/articles/185191-football-rules) y
  [LowVig](https://help.lowvig.ag/en/articles/204902-football-rules): incluyen OT,
  requieren 55 minutos y conservan fecha y sede previstas. La regla de 48h que
  aparece al final pertenece a fútbol australiano. Empate de moneyline queda
  desconocido; teasers y props no lo resuelven. LowVig se leyó con el lector web;
  la descarga directa dio 403 y no se inventó un hash de bytes archivados.
- [Bovada](https://www.bovada.lv/help/common-faq/football-betting-rules): incluye
  OT y considera oficial tras 50 minutos. La tolerancia de 48h no precisa allí
  si significa empezar o completar; no se iguala a la ventana de Kalshi.
- [BetUS](https://www.betus.com.pa/rules/sportsbook/football/): incluye OT,
  exige fecha/sede originales y 55 minutos, salvo apuestas ya resueltas. Disputas
  posteriores que cambian el resultado no alteran la liquidación descrita.
- [BetMGM, ayuda NFL](https://sports.betmgm.com/en/blog/nfl/what-happens-in-nfl-betting-for-a-tie-game-bm06/):
  devolución por empate en moneyline 2-way. No completa un contrato estatal ni
  se hereda a NCAA.
- [BetRivers NY](https://ny.betrivers.com/?page=eula&tos=SPORTSBOOKRULES),
  documento 13-ene-2026 leído en el DOM público: OT incluida, devolución en empate,
  semana jueves–miércoles; exige menos de cinco minutos restantes, con excepción
  NCAA de duración abreviada acordada y resultados ya determinados. Se leyeron
  también cancelación, forfeit, correcciones y replay. Fuente visible sin copia
  de bytes original; estado y versión de la cuota siguen sin verificar.
- [MyBookie](https://www.mybookie.ag/sportsbook-rules/) remite a reglas específicas
  de deporte; su norma general no basta para establecer moneyline americano.
  [Caesars Indiana](https://www.caesars.com/content/dam/uin/house-rules/uin-winners-circle-house-rules.pdf)
  es una referencia de 2022, no evidencia de la versión aplicable a estas cuotas.
  Ambas conservan estado no revisado contractualmente y motivo explícito.

## Captura de liquidación en el simulador

El nuevo evento `settlement_snapshot` se admite sólo en `OFFLINE_SCENARIO` del
stream. Tiene `target` M2 o M5, `ticker`, hash de reglas esperado, objeto `market`,
`observed_at` y, para M5, `quote_id`. El diario guarda los datos antes del abono.
El hash esperado es una declaración del productor; no autentica el origen ni
prueba por sí solo las reglas vigentes al abrir la posición.

El adaptador exige identidad exacta y estos campos del
[OpenAPI oficial](https://docs.kalshi.com/openapi.yaml):

- `status=finalized`, fecha de liquidación con zona horaria no futura;
- `settlement_value_dollars` explícito y coherente con `result=yes/no/scalar`;
- contrato de US$1 sin adelantos de liquidación; precios exactos, sin float;
- mismo ticker que la quote M5 y actividad ficticia anterior a la liquidación.

`determined` ya puede contener precio. Cierre, marcador deportivo, último trade,
resultado sin pago o devolución de una casa no bastan para abonar. El adaptador
rechaza fracciones de centavo porque el banco ficticio existente sólo representa
centavos enteros; nunca las redondea ni simula comisiones de liquidación no medidas.
Los contratos simulados son enteros. No admite avances ni neteo de cuenta.

M2 paga únicamente contratos llenados; NO recibe 100 menos el pago YES. M5 exige
cancelación previamente observada y usa sus fills hipotéticos. Una quote OPEN o
UNKNOWN conserva sus reservas; este adaptador no la cancela. Se reutiliza la
protección compartida contra dos pagos diferentes para el mismo ticker.

Cada evento abona un motor en su transacción existente. El stream puede quedar
entre M2 y M5 si falla: conserva el avance y reanuda idempotentemente, sin reclamar
atomicidad conjunta ni borrar posiciones aún pendientes. Los eventos declarados
legacy `settle`/`m5_settle` siguen disponibles para escenarios sintéticos y no
constituyen evidencia de liquidación conectada.

## Qué demuestra la validación

Pruebas offline cubren YES, NO, 50¢, 37¢, extremos, fills parciales de ambos
motores, comisiones previas, caída entre abono y checkpoint, contradicciones,
identidad equivocada, actividad posterior y reservas con UNKNOWN.

El ejemplo de conservación termina en US$100.30 desde US$100 ficticios por cruces
fabricados como fixture; ese resultado no es rentabilidad, muestra de mercado ni
prueba de fills Demo. Los snapshots reales archivados se auditan por separado.

Siguen pendientes la correspondencia de reglas/versiones de las cuotas, datos
frescos compatibles y evidencia prospectiva independiente. La confirmación de la
orden IOC Demo depende de Kalshi y no se sustituye con estos tests. La reserva
ficticia histórica de 66¢ no se toca. Nada de este trabajo activa trading.
