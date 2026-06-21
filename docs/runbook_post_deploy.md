# Runbook H-04 — Validación post-deploy

> **Cuándo:** después de cada `redeploy` en Coolify (cada merge desplegado).
> **Objetivo:** confirmar en ~2 min que el container nuevo arrancó sano y el bot opera.
> **Validado en vivo:** 2026-06-21 (deploy del endpoint V2 #91 + Fase 0 higiene #96).

Antes de empezar, anotá el **container nuevo** y su **hora de arranque** (todos los greps de
"post-deploy" se filtran por esa hora — si no, contás eventos viejos como nuevos):

```bash
# Coolify → logs, o:
docker ps --filter name=kalshi-bot   # ID + STATUS (Up …)
```

---

## Check 1 — Órdenes V2 entran (sin 410 reales)

```bash
# Órdenes colocadas (deben aparecer si Motor 2 tiene señales):
grep -c "Placing order (V2)" <logs>

# 410 REALES → debe ser 0:
grep -E "→ 410:|deprecated_v1_order_endpoint" <logs>
```

> ⚠️ **GOTCHA (verificado 2026-06-21):** NO grepear `410` suelto. Los tickers traen fecha/hora
> en el nombre (ej. `KXMLBGAME-26JUN24**1410**CLECWS` = 14:10) → un `grep 410` da miles de
> falsos positivos. El 410 real es una línea HTTP del cliente: `→ 410:` o el code
> `deprecated_v1_order_endpoint`. Si ese grep da 0, las órdenes entran bien.

**OK si:** `Placing order (V2)` > 0 (con señales) **y** 410 reales = 0.

---

## Check 2 — `exposure_cents` poblado (fix #96)

```bash
sqlite3 /app/data/trades.db \
  "SELECT COUNT(*) total, COUNT(exposure_cents) poblados FROM portfolio_positions;"

# Auto-diagnóstico: si algún campo de exposición no se resolvió, esto lista las keys reales:
grep "motor3.poller.exposure_unresolved" <logs>
```

**OK si:** `total == poblados` (todas con exposición) **y** `exposure_unresolved` = 0 líneas.
**Si NO:** la línea `exposure_unresolved ... keys=[...]` muestra el nombre real del campo →
agregarlo a `_money_to_cents` en `motor_3_clv/poller.py` (follow-up de 1 línea).

---

## Check 3 — Gaps de Motor 1 sin CRITICAL (fix #96)

```bash
# Gaps que dispararon CRITICAL DESPUÉS de la hora de arranque del container → debe ser 0:
grep "gap detected" <logs> | grep CRITICAL

# Los gaps normales auto-recuperados ahora son INFO (esto SÍ puede aparecer, es benigno):
grep "gap detected (auto-recovery)" <logs>
```

**OK si:** 0 CRITICAL post-deploy. Los `(auto-recovery)` en INFO son esperados (resync sano).
La **escalada por frecuencia anormal** sigue activa (Telegram `sid_gap_warning`/`critical` a
≥5 / ≥20 gaps en 60s) — si llega esa alerta, ahí sí investigar.

---

## Check 4 — Motor 2 / Motor 3 operando

```bash
# Motor 2: últimas órdenes/fills (MLB y/o Mundial):
grep -E "Placing order \(V2\)|motor2.bet FILLED" <logs> | tail

# Motor 3: trackeo de cartera (posiciones con close_time):
grep "MOTOR 3 DIAG" <logs> | tail -1
```

> **GOTCHA (verificado 2026-06-21):** el conteo de posiciones se ve en el log
> **`motor3.engine`** (línea `[MOTOR 3 DIAG] posiciones=N con_close_time=N`), no busques un
> `motor3.poller` con el total — el poller puebla la DB directo.

**OK si:** hay órdenes recientes **y** `MOTOR 3 DIAG` muestra `posiciones=N con_close_time=N`
(con `sin_close_time=0`).

---

## Check 5 — (HUMANO) confirmar una orden en el dashboard de Kalshi

El único check que NO se puede automatizar desde el bot (requiere la cuenta de Kalshi). Tras
el primer fill, tomá una orden del log:

```
Placing order (V2): buy 5 yes KX...-CHC @ 44c → side=bid price=$0.44
```

…y confirmá en **kalshi.com → Portfolio** que la posición figure con **ese lado y ese precio**
(YES a $0.44). Esto valida el mapeo V2 (`yes/no/buy/sell → bid/ask + precio en dólares`) con
ojos humanos. **Hacelo al menos en el primer deploy tras un cambio en `place_order`.**

---

## Resumen — todo verde =

- V2: órdenes entran, 0 errores 410 reales.
- DB: `portfolio_positions` con exposición poblada.
- Motor 1: sin gaps CRITICAL nuevos.
- Motor 2/3: operando y trackeando.
- (humano) la orden de muestra figura con lado/precio correctos en Kalshi.

> Tip operativo (fix #96, punto 1): **agrupar deploys**. Cada redeploy reinicia el container;
> juntar varios merges en un solo deploy reduce el ruido de reinicios y de re-validación.
