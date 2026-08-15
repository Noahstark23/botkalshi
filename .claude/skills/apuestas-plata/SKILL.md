---
name: apuestas-plata
description: El plan de reestructuración 2026-08-12 y SUS TABLEROS DE LECTURA — las 3 apuestas de plata (maker M5 con fee real, arb cross-venue Kalshi↔Polymarket, tesis offline NO-bias/lag), sus gates pre-registrados con plazos, la lista matar/congelar, y las queries EXACTAS para leer cada marcador sin redescubrir nada. Usar en CADA parte diario, al evaluar cualquier gate, al proponer activaciones de M5/F2, o cuando el operador pregunte "¿cómo vamos?". Este es el telescopio — la sesión que no lo usa está adivinando.
---

# Apuestas de plata — el plan y sus tableros

**Origen:** plan de reestructuración 2026-08-12 (dossier: 4 investigaciones del mundo +
3 auditorías del código, 815k tokens, síntesis verificada contra el repo). El operador
pidió "un bot que trabaje"; el dossier respondió: en un exchange sin limiting el edge
solo puede ser (a) ser maker, (b) ser más rápido in-play, (c) saber algo del settlement.
Las 3 apuestas son una de cada una. **Todo lo taker en zona media está muerto por
aritmética** (breakeven 2.3-4.3pp vs techo medido 0.15-0.31pp) — no re-litigar.

**Meta honesta:** con ~$400 no existe "negocio" (la economía cierra ~$5k). La meta de
los 60 días es **la primera evidencia de edge capturable** → recapitalizar con números.
Nadie del dossier convirtió $400 en ingreso; nadie gana tomando liquidez en deportes de
Kalshi; la mesa propia de Kalshi admitió no ser rentable. Expectativa calibrada ahí.

## APUESTA 1 — Maker M5 con fee real (EN CURSO desde 2026-08-13)

**La historia del fee:** el quoter cobraba fee de TAKER (0.07) a ambas patas post_only
("fee fantasma", 4× de más). El dossier creyó "maker $0"; la verificación contra el
[PDF oficial](https://kalshi.com/docs/kalshi-fee-schedule.pdf) (July 7, 2026) lo REFUTÓ:
maker deportes = **¼ del taker** (0.0175, multiplicador 1, fila "KXMLBGAME | 1 | 1").
`kalshi_maker_fee_cents` en fees.py la implementa exacta. Round-trip maker ≈ 1¢/contrato
a size=10 → **spreads ≥2¢ rentables** (taker exigía ≥4¢).

**Activación:** `MOTOR_MM_FEES_AS_MAKER=true` (env, aplicada por el operador tras la
verificación del 13-ago). El gate corre desde ese boot.

**GATE (pre-registrado, 2-3 semanas desde el env):**
- **PASA** = mtm neto positivo **Y** markout medio por fill > −(spread capturado / 2)
  → discusión F2 (quotes reales chicas), con los P1 de protección cerrados antes.
- **FALLA** = markout peor que eso → los fills son tóxicos (selección adversa), el
  spread capture no existe a esta escala → **apagar M5 Y el ciclo M2 + The Odds API**.
- Ambos resultados son veredictos válidos. NO extender el plazo sin causa escrita.

**El tablero — UNA línea, no queries ad-hoc (2026-08-14):**
```bash
python3 scripts/tablero_gate.py            # en el container; --db y --half-spread si hace falta
```
⚠️ **Las queries sueltas ya produjeron una lectura falsa**: un tablero ad-hoc reportó
markout2 −11.09¢ sobre "n=11" cuando NUEVE filas eran residuo del bug del mark congelado
(#235) — muestra limpia real n=2. El filtro vive en el script, con tests. Si necesitás
una query nueva, agregala AL SCRIPT; no la improvises en el container.

- **Columnas auto-descriptivas (2026-08-14):** `fee_model` (taker|maker) + `fee_effective_cents` dicen qué contabilidad rigió CADA fill; `fee_cents` es la referencia taker (permite derivar ambas de la misma fila). NULL = tramo pre-columna. **No sumar `fee_cents` crudo con el modelo maker activo.**
- **markout2 (T+5min) es el juez PRINCIPAL; markout1 (T+30s) secundario** (fijado
  2026-08-14 ANTES de tener muestra): la selección adversa que mata a un maker chico es
  la SOSTENIDA. Los primeros datos limpios muestran por qué — fills calmos con markout1
  **+2.5/+0.5** que a T+5min terminan en **−9.0/−9.5**. El script se NIEGA a declarar
  veredicto con n<100: un promedio de n chico es una anécdota, no un resultado.
- **CLV = juez SECUNDARIO** (Propuesta 1 del plan M2, #231): (cierre − precio − fee) signado. Gate propio n≥100: CLV medio > 0 con signo estable semanal. **El markout MANDA, el CLV EXPLICA** — prohibido invertirlo (markout tóxico + CLV lindo = fills tóxicos, punto). Diagnostica el confound del fair degradado: markout OK + CLV negativo → el problema es el fair (palanca: burst solo para el fair).
- **Blindaje (#230):** `MOTOR_MM_JUMP_RETREAT_CENTS=5` retira la quote si el mark saltó ≥5¢; los fills que el salto igual causó quedan etiquetados (`mark_jump_cents`) — el gate juzga la economía de los CALMOS por separado. `skip_jump` en el funnel.
- Retención de `mm_shadow_fills`: **30d** (era 7 — la poda borraba la muestra del gate a mitad de período; corregido 13-ago).
- El mtm en RAM se resetea por deploy — el juez es la TABLA, no el funnel.
- Riesgo distintivo: pick-off in-play (fair con latencia de segundos-minutos). Primeros datos: 9 fills MLB in-vivo con markout −7.3¢/−11.5¢ — la firma que el blindaje vino a frenar.
- **LIP**: KXMLBGAME NO califica (0 pools single-game sobre 3.823 programas, verificado 13-ago). Poll barato: `GET https://api.elections.kalshi.com/trade-api/v2/incentive_programs?status=active`.

### Si el gate FALLA: dos palancas ANTES de archivar (pre-registradas 2026-08-14)

Bartlett/O'Hara (SSRN 6615739, 41,6M trades) mostró que el revenue del MM **no viene del
spread** sino del sesgo conductual del retail, y que depende de la CATEGORÍA:
single-name (props de jugador) **1,91¢/contrato** vs broad-based (ganador del juego)
**0,82¢**. M5 cotiza solo KXMLBGAME = broad-based. Y el fee de maker ∝ p(1−p) es MÁXIMO
en el centro → **zona muerta 38¢-62¢** donde el margen teórico es ≤0 antes de cualquier
selección adversa (`scripts/tablero_gate.py` lo reporta; los 4 primeros fills limpios
cayeron 4/4 ahí).

Por eso, si el markout falla, la primera pregunta NO es "¿el maker sirve?" sino "¿sirve
en esta categoría y en esta banda?". Orden de las palancas:
1. **Cotizar solo las colas** (fair <35¢ o >65¢): el fee cae a la mitad y el margen
   teórico se vuelve positivo. Barato: es un filtro en el universo del quoter.
2. **Cambiar de categoría a single-name** (props): 2,3× el edge por contrato — con la
   advertencia del propio paper: más flujo INFORMADO. Se mide antes de creerle.
Recién si las dos fallan, M5 se archiva con su ciclo. **Archivar sin probar (1) es
tirar el experimento por un parámetro.**

## APUESTA 1-bis — M1 converge acá (veredicto estructural 2026-08-13)

**El arb intra-ticker de M1 es INCAPTURABLE POR DISEÑO, no por mala ejecución.** Kalshi
tiene UN book: `yes_ask+no_ask<100 ⇔ book auto-cruzado`, y el MATCHING ENGINE consume el
cruce al arribo (mintea el par él mismo — el rol que en Polymarket ocupan los bots de
mint/merge con atomicidad on-chain que Kalshi no tiene). Las ventanas de 30-140ms son
deltas WS de un cruce YA muerto: 0/46 capturas y 6.7% de fill bilateral **no son una
carrera perdida por latencia — es una carrera de 0ms contra el engine**. Ni batch (no
atómico), ni order groups (throttle), ni FIX, ni VPS lo cambian. La única captura
Kalshi-compatible del fenómeno **es maker — ya existe y es la Apuesta 1**.

**Gate del entierro con número propio (#232):** cada ventana binaria se re-chequea a
T+200ms/T+1s en el book en memoria → `edge_windows.survived_200ms/_1s`. **n≥200 y <5%
sobrevive → ⚫ de M1 cerrado el día 30 sin re-litigio** (salvo cambio documentado del
matching de Kalshi). Predicción: ~0%.
```bash
python3 -c 'import sqlite3;c=sqlite3.connect("file:/app/data/trades.db?mode=ro",uri=True);[print(r) for r in c.execute("select count(*) n, sum(survived_200ms) vivas_200ms, sum(survived_1s) vivas_1s from edge_windows where kind=\"binary\" and survived_200ms is not null")]'
```
**VEREDICTO POR AUSENCIA (pre-registrado 2026-08-14 — el n≥200 NO va a llegar):** la
última ventana binaria es del **12-ago 03:50**; #225 (aritmética exacta del feed) se
mergeó **03:54:36** — cuatro minutos después, y van 67h+ con CERO ventanas. Los "arbs"
de M1 eran, en buena parte, **artefactos de nuestros propios bugs de feed** (la colisión
sub-céntimo inflaba niveles y fabricaba cruces fantasma). El backfill es IMPOSIBLE (la
supervivencia exige re-chequear el book vivo a T+200ms; no se reconstruye de una fila).
Criterio: **si al día 30 hay <10 ventanas binarias nuevas con feed exacto, el ⚫ de M1 se
cierra por AUSENCIA DEL FENÓMENO**, documentando esta correlación. Confirmación
independiente: los near-miss del REST se paran SIEMPRE en el bin de 1¢ — el mercado no
deja plata sobre la mesa ni por un tick.

**Decisiones ya tomadas (día 15, no re-litigar):** NO bajar la banda a 1.0-1.5pp (más
señales-artefacto contra la misma carrera imposible); NO apagar M1 intra-mes (el día 30
cierra con el t medido + este fundamento). M1 queda como **detector de flujo cruzante /
selector de universo para M5**, no como brazo de captura.

## APUESTA 2 — Arb cross-venue Kalshi↔Polymarket US (F1 por construir)

Polymarket US es DCM legal desde dic-2025 con CLOB API. Gaps 0.5-2¢ en deportes;
taker-taker NO cierra (breakeven ~2.5¢); la variante viable es **maker-Polymarket
(rebate +0.20%) + hedge taker-Kalshi al fill** (breakeven <1¢). Estructuralmente =
M5 con inventario hedgeado en otro venue. **Solo medición** (feed público, sin cuenta):
paquete propio `EdgeWindow kind='xvenue'`, matcher por datos estructurados (plantilla
del de M2), spread EJECUTABLE (ask+ask+fees+depth — jamás el mid: lección M9), y el
prerequisito no negociable: **diff literal de rules** (settlement mismatch por
lluvia/suspensión = pérdidas de 5 cifras documentadas; clima → excluir).

**GATE (3-4 semanas de shadow):** ≥3 ventanas/día ejecutables a ≥0.5¢ netos con depth
≥50 contratos y duración ≥5s → discutir cuenta + ~$200 en Polymarket. Menos → archivar
con tabla. Techo honesto si gradúa: $3-10/día — prueba, no ingreso.

## Veredicto de monetización de M2 (workflow 2026-08-13) — cerrado

**M2 se monetiza A TRAVÉS de la Apuesta 1: su fair es ancla de cotización, no señal de
ataque.** La única monetización taker documentada del insumo es contra SOFT BOOKS
(cuentas humanas, bans en semanas — fuera del alcance de un bot de exchange). Contra
Kalshi, consenso-vs-consenso en venue líquido no deja edge (nuestro 0.15pp/35k es la
propiedad esperada, no un bug). La variante "M2v2 maker sub-pp" quedó DESCARTADA con 5
razones (tick 1¢ = sub-pp inexpresable; el único wallet público del juego perdió $3.2k
en el residual direccional con edges 20× los nuestros; microestructura predice fills
anti-correlacionados con retorno; ruido de composición de la mediana nunca medido;
consumiría el insumo cuyo gate ya corre). **No construir motor nuevo sobre este fair.**

## APUESTA 3 — Dos tesis gratis offline (queries, no motores)

- **3a NO-bias/longshot:** el retail sobre-compra YES barato (Bartlett/O'Hara, 41.6M
  trades). Test: SQL sobre settlements propios de KXMLBGAME — ¿el lado <20¢ paga menos
  que su precio? Advertencia: el bias vive en colas donde manda el piso de 15¢ de fee
  relativa; y vender NO caro = vender seguros (riesgo de ruina con $400 salvo caps
  durísimos). El veredicto sale de settlements, no de intuición.
- **3b Lag in-play:** MLB StatsAPI (gratis) timestampeado vs el book V2 — ¿cuánto tarda
  Kalshi en digerir un evento de scoring? Lag mediano >segundos → hay ventana, diseñar
  F1 (medir depth junto con lag). Sub-segundo → archivar sin gastar.
- **PRIORIDAD 1 de las offline (2026-08-14):** el NO-bias es el ÚNICO edge con
  evidencia académica robusta Y aritmética de fee favorable — Becker (72,1M trades):
  NO le gana a YES en **69 de 99** niveles de precio; a 1¢ el EV de YES es −41% vs
  +23% del NO. Y vive en los extremos, donde el fee p(1−p) es MÍNIMO (justo lo
  contrario del maker en el centro). Riesgo repetido: vender NO caro = vender seguros
  (ruina con \$400 salvo caps durísimos) — el veredicto sale de settlements propios.
- **3a-bis (banda de FAVORITOS, del workflow M2):** el único retorno positivo
  documentado académicamente en Kalshi es comprar favoritos >50¢ (Whelan/Bürgi/Deng) —
  espejo del NO-bias. Misma query, bandas >50/>70/>90¢ sobre settlements propios. Si da
  positivo, la traducción NO es motor nuevo: es sesgar el quoting de M5 con el
  `edge_skew_cents` que ya existe.
- **GATE:** 2 semanas para ambas; solo la que muestre número positivo pasa a F1 formal.

## MATAR / CONGELAR (estado)

- ✅ Matado: fallback ¼ Kelly (PENDIENTE de PR al 13-ago — verificar antes de asumir),
  V1 manager (ídem), docs que mienten (ídem). Verificar en git antes de re-matar.
- **Día 30 del mes de operación:** REST ejecución y M1 ejecución se deciden por sus
  criterios pre-registrados en `docs/mes_de_operacion.env` — no re-litigar antes ni
  después. M2 direccional: terminal por aritmética; queda solo como fábrica de fair
  para M5, y muere con M5 si la Apuesta 1 falla.
- **PERPS de Kalshi — ⚫ DESCARTADO 2026-08-15 SIN CONSTRUIR** (ni siquiera F1). No se
  lanzó "hoy": salió el 3-jun-2026, >$5.500M de nocional — no hay ventaja de
  first-mover. Rompe el axioma de PÉRDIDA ACOTADA sobre el que está construido todo el
  aparato de seguridad, y nuestros frenos fallarían **silenciosos diciendo OK**: los
  caps del RiskManager son por COSTO (`manager.py:393-396` — el mismo cheque que aprueba
  \$200 aprueba L×\$200 de nocional), el stop-loss lee solo `status='settled'`
  (`manager.py:558-560`) y un perp NUNCA settlea → las cuatro ventanas reportarían "0%
  consumido" mientras la cuenta se vacía; no existe primitiva de FLATTEN en el repo; y
  el lazo de riesgo corre a 60-300s contra liquidaciones que ocurren entre dos lecturas.
  Con \$400: BTC 6.2x liquida a −16,1% (y el 10-oct-2025 hubo −14% intradía; SOL −40%).
  Kalshi documenta que la auto-liquidación NO es un stop garantizado y puede dejar SALDO
  NEGATIVO — no es perder el proyecto, es perderlo con deuda. La variante neutral es
  INCONSTRUIBLE dentro de Kalshi (no lista spot); a \$400 daría \$16-36/AÑO contra
  \$1,76-2,56 de round-trip. **Reapertura solo con las TRES juntas:** (1) Kalshi
  documenta negative-balance protection, (2) ≥\$5.000 de dinero NUEVO y segregado, (3)
  basis persistente medido por nosotros.
- ⚠️ **Deuda de seguridad que dejó el lanzamiento:** `kalshi_rest.py` deja pasar `sell`
  con `TRADING_ENABLED=false` (salida protectora). Es seguro SOLO porque nuestro
  universo es binario. Si algún día entra un producto con posición CORTA, ese guard se
  cierra ANTES de que exista su executor — si no, la Capa C se vuelve el agujero por
  donde sale un corto apalancado con el trading apagado. Ya está acotado en el
  comentario y pinneado por test.
- Congelados (no tocar, no revivir): M6/M8/M9 (registro barato + plantilla Motor N),
  Kalshi↔sportsbook, steam chasing, microservicios (el monolito es superior a esta
  escala — polybot Java/Kafka: 917★, cero alpha).
- **Cirugía condicionada al día 30:** si se borran motores, ANTES extraer settlement.py
  de motor_rest_arb/ y el V2+orderbook.py de motor_1_arbitrage/ a paquetes neutros
  (hoy la infra vive en el domicilio de los motores archivables).

## Reglas de lectura del telescopio

1. Todo veredicto de gate sale de la TABLA (SQL sobre settlements/fills reales), jamás
   del funnel en RAM ni de una ventana corta. Ventana corta = orientación, no veredicto.
2. Cero cambios de umbral/flag sin el gate cumplido o refutado — las fechas de arriba
   son compromisos pre-registrados.
3. El agente web es el brazo de lectura del container (queries `mode=ro` + /status);
   este agente verifica contra código y git. Dos fuentes con razón sobre momentos
   distintos → reconciliar por línea de tiempo antes de acusar.
4. Si un gate FALLA, archivar es victoria barata (costó ~$0 y compró certeza). La
   frase prohibida es "démosle una semana más sin datos nuevos".
