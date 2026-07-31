---
name: motor-8-ofi
description: Motor 8 — OFI. ⚫ ARCHIVADO 2026-07-28 (n=30.108, +0.02¢, RUIDO) — NO evaluar F2/F3; usar solo como registro del diseño o al tocar el tracker que sigue midiendo en shadow. La tesis tiene DOS reservas documentadas: books finos y flujo informado.
---

> ## ⚫ VEREDICTO FINAL: ARCHIVADO (2026-07-28)
> Con la muestra completa del feed sano: **n=30.108, media +0.02¢ a T+60, 6% positivos,
> t=2.0 → RUIDO**. El flujo no predice el precio a esta microescala. La "promesa" inicial
> (p50 +3.18pp con n=141) era ARTEFACTO del feed ciego — con solo mercados raros visibles,
> la muestra chica lucía fuerte. Costó $0 refutarla (el patrón shadow-first funcionando).
> **NO revivir sin re-derivar la tesis a otra escala/umbral con una observación medida.**
> El shadow puede seguir corriendo (telemetría barata); ejecutarlo, no.

# Motor 8 — Order Flow Imbalance (OFI)

**Tesis a validar:** un desequilibrio anómalo del flujo de órdenes (z-score del OFI de la
ventana corta contra su propia historia por ticker) **precede** al movimiento del precio.
Propuesta original del operador (2026-07-13); la infraestructura encaja perfecto (los
deltas ya fluyen por el V2), la tesis se valida en F1. Protocolo general: `botkalshi`.

## Reservas de la tesis (el shadow existe para MEDIRLAS, no para ignorarlas)

1. **Books finos**: en Kalshi deportes una orden de 50 contratos "es" el book — el
   "desequilibrio masivo" puede ser UNA persona, no presión estadística. La teoría OFI
   viene de mercados profundos; acá puede no transferir.
2. **Flujo informado**: cerca del partido, quien empuja el book suele saber algo
   (lineups). Ir CONTRA ese flujo es adverse selection — el riesgo anotado en la
   propuesta. Por eso el shadow **NO asume dirección**: registra presión y move real;
   F2 decide contrarian vs momentum vs nada, con datos.

## Arquitectura (F1)

```
src/strategies/motor_8_ofi/
├── detector.py   # OfiTracker — PURO: ventana rodante + z-score; reloj INYECTADO
└── shadow.py     # Motor8OfiShadow: señal + AUTO-MEDICIÓN a T+30/T+60 → EdgeWindow kind="ofi"
```

- **Pasajero del handler de deltas** de `data_capture._on_orderbook_delta` — cero API
  extra, **cero persistencia de deltas** (la lección de los 57GB: el OFI se computa en
  memoria y solo se graban las SEÑALES con su resultado).
- Los mids salen del `OrderbookManagerV2` en memoria (`_mid_of`): un book en cuarentena/
  stale devuelve None → **la señal se descarta antes que medir basura** (sinergia directa
  con la cuarentena de desync).
- Las mediciones maduran empujadas por el propio flujo de deltas (sin task nuevo); con
  grace de 120s — book caído toda la ventana → señal descartada y contada en `stats()`.
- Semántica del flujo: `side=yes, delta>0` = interés comprador YES (+); `side=no` = (−).
- Baseline muestreado PRE-delta (un spike no se normaliza a sí mismo) + cooldown por
  ticker (anti-ráfaga: una señal por episodio).

## Mapping de EdgeWindow kind="ofi" (documentado porque reusa campos del Motor REST)

`edge_pct` = z-score · `count` = OFI neto (contratos) · `gross_spread_cents` = move a
T+30 · `magnitude_cents` = move a T+60 — ambos en ¢ **firmados DESDE la presión**:
move > 0 = el precio siguió a la presión (momentum gana); < 0 = la contradijo
(contrarian gana). Retención: `edge_windows` ya se poda a 30 días.

## Env vars

`MOTOR_8_OFI_ENABLED` (off) · `MOTOR_8_OFI_WINDOW_SEC` (60) · `MOTOR_8_OFI_Z_MIN` (3.0) ·
`MOTOR_8_OFI_MIN_BASELINE` (200) · `MOTOR_8_OFI_COOLDOWN_SEC` (120). Requiere el manager
V2 presente (`USE_ORDERBOOK_MANAGER_V2` o M1 enabled) para poder medir mids.

## Gate F2 (escribirlo ANTES de mirar — está escrito acá)

Tras 1-2 semanas de F1 con partidos:
```sql
SELECT count(*), avg(magnitude_cents), avg(gross_spread_cents),
       sum(CASE WHEN magnitude_cents > 0 THEN 1 ELSE 0 END) AS momentum_wins,
       sum(CASE WHEN magnitude_cents < 0 THEN 1 ELSE 0 END) AS contrarian_wins
FROM edge_windows WHERE kind='ofi';
```
- `avg(move60)` claramente ≠ 0 y consistente en signo, con N ≥ ~50 → hay señal
  direccional; la magnitud media vs fees+spread decide si es tradeable (recordar: para
  capturarla hay que cruzar el spread — el move tiene que superar spread+fee, no solo 0).
- N ínfimo (cooldown+z=3 filtran casi todo) → aflojar `Z_MIN` a 2.5 UNA vez y repetir.
- `avg ≈ 0` o signo inestable → **archivar** (resultado válido; costó $0).
- F3 (si algún día llega): executor IOC con TODOS los guards heredados + la reserva #2
  (flujo informado) reevaluada con los datos por franja horaria (pre-partido vs lejos).
