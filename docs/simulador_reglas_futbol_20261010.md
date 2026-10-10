# Revisión de reglas y libros de fútbol — 10 de octubre de 2026

Estado: **rechazo explicado en simulación; validación positiva M2→M5 no demostrada**.
Los cambios sólo amplían el consumidor local `CONNECTED_CAPTURE`. No cambian
`src/`, variables de ejecución, servicios o credenciales. No publican fair ni
inician motores. Las fuentes de red se guardan primero y se reproducen sin red.

## Contratos revisados

Se revisaron seis contratos de tres partidos: PHI–JAC, UGA–ALA y STAN–ND.
`football_settlement_review.REVIEWED_RULE_HASHES` liga cada interpretación a su
ticker y al SHA256 exacto de `rules_primary + "\n" + rules_secondary`. Otro texto,
pareja, fecha o contrato queda sin revisar. Esto no es una firma del proveedor ni
una certificación completa de liquidación.

En los contratos NFL capturados, el empate reparte 50¢ por contrato y la ventana
de inicio tras aplazamiento es 48 horas. En los NCAAF capturados se explicita
precio justo ante cancelación o falta de inicio dentro de esa ventana. No se
deduce overtime, suspensión, cambio de sede, duración mínima o ausencia de empate
de un título o del nombre de la liga. La revisión de cuatro dimensiones es parcial.

## Casas y vinculación de cuotas

- [Fanatics Massachusetts](https://sportsbook.fanatics.com/legal/ma/house-rules/):
  moneyline de dos resultados incluye overtime, devuelve la apuesta en empate y
  aplica ventanas de semana deportiva a los aplazamientos. Su semana NFL y NCAA
  difiere. Las condiciones de interrupción y sede requieren revisión adicional.
- [FanDuel New Jersey, 31-mar-2026](https://d38ayms4az88sz.cloudfront.net/SB/NJ/2026-03-31T12-47-12.html):
  devuelve la apuesta ante empate; sus reglas de aplazamiento/reanudación utilizan
  una ventana de 60 horas y una restricción de semana NFL cuando corresponde.
  La regla de 55 minutos y la de suspensión de 24 horas son condiciones distintas;
  no se sustituyen por la ventana de 48 horas de Kalshi.
- [Ayuda de moneyline de DraftKings](https://help.draftkings.com/hc/en-us/articles/4405230614547-What-is-a-Moneyline-bet-US):
  evidencia parcial de devolución en empate. No completa el reglamento NFL/NCAA
  ni su versión/jurisdicción. La página de reglas de fútbol consultada no entregó
  el cuerpo necesario para ampliar la ficha.

El feed regional `us` archivado no liga cada cuota a esas versiones de reglas.
Los perfiles no se aplican como certificación nacional. Otras ocho casas de la
muestra quedan expresamente sin perfil revisado. Usar `h2h_3_way` tampoco demuestra
por sí solo mismo período de juego o mismas excepciones de liquidación.

Una cuota que devuelve la apuesta ante empate no demuestra la misma probabilidad
de pago que un contrato que abona 50¢. No se inventó probabilidad de empate,
cancelación ni precio justo para fabricar equivalencia.

## Libros y tarifa M5

`connected_book_review.inspect_book` conserva profundidad fraccional y obtiene el
ask YES del bid NO complementario. Rechaza libros unilaterales, cruzados, duplicados,
precios subcentavo no soportados y recepción mayor a 10 segundos o futura. El reloj
es de recepción REST: no demuestra secuencia del exchange ni fills.

`probe_m5_fee_policy` ejecuta `SeriesFeePolicy` contra metadatos archivados mediante
un objeto que sólo expone dos lecturas locales. No construye un cliente REST ni
un motor. Demo NFL declara `quadratic`, rechazado por la política actual; NCAAF
carece de aserción de serie. Las pruebas sintéticas de tarifa admitida no cambian
esos resultados observados. No se modifica esa política en este cambio.

## Evidencia y alcance

El consumidor guarda reglas, motivos y libros junto al checkpoint durable; una
repetición no genera reservas ni operaciones. Los nuevos tests entran en el job
`simulator-offline` existente. El resultado local no prueba una ejecución remota
de CI ni un recorrido de órdenes/fills en Demo.

Fuentes API: [libro fixed-point](https://docs.kalshi.com/api-reference/market/get-market-orderbook),
[milestones](https://docs.kalshi.com/api-reference/milestone/get-milestones),
[tipos de mercado Odds](https://the-odds-api.com/sports-odds-data/betting-markets.html).
Para obtener el horario se usa `related_event_ticker`; `primary_event_ticker` no
es un filtro de ese endpoint. Paginación incompleta no se trata como ausencia.

El paquete externo `REGLAS_M2_M5_CONECTADO_2026-10-10` conserva capturas originales,
correcciones, hashes, resultados de pruebas y limitaciones. No contiene claves.

## Continuación: soporte de tarifas Demo en modo de lectura

El diagnóstico anterior corresponde al commit `711c5b8`. Se agregó posteriormente
un modo explícito de validación sin órdenes con soporte NFL/NCAAF `quadratic`;
véase [contrato del modo Demo](m5_demo_readonly_validation.md). El rechazo por reglas
M2 y la falta de una validación conectada positiva permanecen. El soporte nuevo no
se activa por configuración del runner y no modifica la política operativa default.

## Continuación: identidad en el detector M2 real

El inspector de capturas reconocía NCAAF, pero `find_signals` todavía no conocía
`KXNCAAFGAME`. Además, la serie NFL aceptaba cualquier liga `americanfootball_*`
y el parser REST entregaba nombres cortos sin los aliases de fútbol necesarios.
Se corrigieron en el candidato local las tres discrepancias: liga exacta NFL/NCAAF,
aliases limitados a los ocho equipos observados y comprobación independiente del
código del ticker. Otros equipos siguen sin cobertura certificada. Los aliases
de MLB y el comportamiento histórico sin `sport_key` se conservan.

La regresión reproduce nombres originales mediante `_parse_event_quotes`, luego
ejecuta `find_signals` con fixtures sintéticas. Equipos intercambiados, otra liga
y otra fecha no producen fair ni señales. El control positivo verifica que un
rechazo constante no pueda ocultar la falla. Este test de identidad no demuestra
equivalencia de liquidación ni publica en `FairValueBook`.

El replay de las capturas archivadas del 10-oct usa el parser real y confirma
`events_matched=1` tanto para PHI–JAC como para UGA–ALA. STAN–ND conserva ausencia
de cuotas y libro no operable. En un diagnóstico que **supone pagos binarios
equivalentes sin haberlos demostrado**, el mejor margen neto por contrato es
−2¢ y aproximadamente −2,247¢, respectivamente. No constituye fair certificado,
backtest de rentabilidad ni muestra de ocho partidos: son ocho lados sobre dos
partidos con cuotas, de tres partidos inspeccionados. No hubo fills ni resultados
liquidados en esta medición.

Los rechazos de reglas y frescura siguen vigentes en `CONNECTED_CAPTURE`; no se
conectó ese diagnóstico numérico al canal de fair M2→M5. No se habilitaron series,
flags, órdenes o servicios. La evidencia externa está en
`M2_COSTE_CAPTURA_2026-10-10`, conservando la reproducción previa a la corrección.
