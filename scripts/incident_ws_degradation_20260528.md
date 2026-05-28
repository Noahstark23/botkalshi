# TICKET V1-CRÍTICO: WS feed degradación escalonada ~3h (28-may)

**Severidad:** Alta (post-mortem — incidente cerrado, V1 sano al cierre)
**Estado:** Investigación pendiente (discovery read-only no iniciado)
**Detectado:** 2026-05-28, vía last_error "WS zombie: no messages for 1475s" @ 08:59:59

## Resumen
WS feed (orderbook_events) sufrió degradación escalonada de ~3h mientras
ws_connected=True todo el tiempo. El detector zombie solo capturó los
últimos ~24 min. Recovery a las 09:00, completo en términos prácticos
(slots 13-16 UTC del 28 dentro de la mediana del baseline de 11 días).

## Evidencia — throughput orderbook_events por hora (28-may)
```
04h: 13.470  (OK)
05h:  6.275  (caída -50%)
06h:  7.018  (degradado)
07h:    454  (blackout -98%)
08h:      9  (blackout total)
09h:  7.576  (recovery, burst de catch-up: 115/309/79/238/116 por minuto)
10h+: 7k-13k (normalizado)
```

## Confirmado NO valle de mercado
Comparación 05-08 UTC × 4 días: días 25/26/27 tuvieron 12k-27k/h en esa
franja. Solo el 28 colapsó. Refutada la hipótesis de curva diaria.

## Forma de la curva: ESCALONADA, no binaria
Caída progresiva 13k→6k→7k→454→9, no un cliff. Sugiere degradación
acumulativa, no muerte súbita de conexión.

## Preguntas abiertas para discovery (kalshi_ws.py + monitoring/)
1. ¿Qué reanudó el feed a las 09:00? (reconexión natural Kalshi / detector
   forzó algo no instrumentado / TCP nunca murió y Kalshi resumió)
2. ¿Por qué la caída es escalonada y no binaria? Hipótesis a contrastar:
   - resubscribe parcial progresivo (tickers cayéndose uno a uno)
   - rate limiting creciente del lado Kalshi
   - memory leak / backpressure acumulándose en dispatcher
   - reconexiones repetidas con menos tickers cada vez
3. ¿Por qué el detector setea last_error pero NO escala a risk_events,
   NO alerta Telegram, NO fuerza reconexión? (regresión de defensa Lección 7)
4. ¿Por qué el detector subdetecta? Capturó 24min de un incidente de 3h.
   Threshold mal calibrado o chequea el canal equivocado.
5. **¿El 18-may tuvo un microblackout no detectado?** (13h=5.196, 15h=4.832
   — anómalamente bajo vs baseline. Investigar si fue incidente silencioso
   o día genuinamente tranquilo. NO asumir que fue normal.)

## last_error sticky (deuda secundaria)
last_error no tiene TTL de auto-clear. Un error de hace 7h queda colgado
en /status, puede enmascarar uno nuevo.

## Relación con Lección 7
Esto es regresión parcial de la defensa post-blackout de 11h (mayo 13-14).
La decisión derivada de Lección 7 decía: "ws_connected refleja estado real
validado por heartbeat" y "N fallos consecutivos ≥5 → Telegram obligatorio".
Ninguna de las dos se cumplió hoy. Verificar si la defensa se implementó
incompleta o regresó.

## NO incluir todavía
Detector de throughput-drop: buena idea pero requiere baseline por-hora
(la varianza es 4.8k-24.7k en la misma franja). Threshold absoluto =
falsos positivos constantes. Es diseño, no spec. Diferir hasta entender
la curva real del feed.

## Próximo paso
Discovery read-only de `kalshi_ws.py` + `src/monitoring/` contra las 5
preguntas de arriba. De ahí sale el fix, y de ahí sale Lección 10.
