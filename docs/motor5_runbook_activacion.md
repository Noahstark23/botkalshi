# Motor 5 — Runbook de activación F3 y rollback

> **Regla del runbook 12.5 heredada:** criterios NUMÉRICOS literales, decididos ANTES de
> la ventana. Cero discreción a mitad de incidente: si un criterio dispara, se ejecuta la
> acción escrita, se documenta, y se discute DESPUÉS.
>
> **OK de Noel:** otorgado 2026-07-02 ("tienes mi ok"), documentado en el commit que
> introdujo `MOTOR_MM_F3_ACK`. El OK autoriza la SECUENCIA, no saltársela.

## Secuencia de activación (el orden importa — plan §5)

1. **Gates F2 cerrados** (plan §4): matriz de validación API en demo con evidencia
   cruda (post_only, batched, partial fills, cancel bajo carga), fin de semana en demo
   sin `pending` fantasma, cancel-all < 5 s cronometrado, runbook (este) leído.
2. **Smoke test contra producción**:
   `python scripts/motor5_smoke_test.py --ticker <bajo volumen> --confirm`
   → guardar la salida completa (las 3 respuestas crudas). Si el cancel no se verifica,
   F3 NO arranca.
3. **Canonicalización en `main`**: ✅ (matcher canónico mergeado desde 2026-07-01).
4. **Girar la llave** en el env de Coolify:
   `MOTOR_MM_F3_ACK=NOEL-OK-F3` + `MOTOR_MM_EXECUTION_ENABLED=true`
   (sin la llave, el boot FALLA a propósito — eso es correcto, no un bug).
5. **Redeploy con ventana de supervisión activa de 2-3 h**: Telegram verificado,
   backup de DB < 30 min antes del deploy, y este runbook abierto.

## Config del canary (primera semana)

| Parámetro | Valor canary | Nota |
|---|---|---|
| `MOTOR_MM_MAX_EXPOSURE_USD` | **100** (default) | Techo duro del costo abierto del MM |
| `MOTOR_MM_MAX_INVENTORY_CONTRACTS` | **mitad del valor de demo** | Plan §5 |
| `MOTOR_MM_MAX_TICKERS` | ≤ 5 | Menos superficie mientras se observa |
| `MOTOR_MM_QUOTE_SIZE_CONTRACTS` | ≤ 5 | Sizing chico; se sube solo por config con datos |

## Criterios de ROLLBACK (numéricos, sin discreción)

Acción de rollback = `MOTOR_MM_EXECUTION_ENABLED=false` + redeploy (el motor queda en
shadow; sus resting orders las cancela el `cancel_all` del shutdown o, si el proceso
murió, `scripts/`+UI a mano). Si hay duda del estado: kill-switch primero.

| # | Criterio (medido sobre la ventana de supervisión) | Acción |
|---|---|---|
| R1 | PnL realizado del MM ≤ **−$25** (25% del canary) | Rollback inmediato |
| R2 | `motor5.reconcile` con `discrep>0` en **3 ticks consecutivos** | Rollback inmediato |
| R3 | `pending` fantasma que sobrevive **2 reconciles** | Rollback + investigar |
| R4 | cancel-all medido **> 5 s** en cualquier disparo real | Rollback + investigar |
| R5 | `exec[corrupted=]` > 0 en **> 20%** de los ticks de 1 hora | Pausa (`quotes_paused`) + investigar |
| R6 | Cualquier orden del MM visible en un ticker FUERA del universo con fair fresco | Kill-switch + rollback |
| R7 | 429 de Kalshi pese al throttle (≥ 3 en 10 min) | Pausa + bajar `MOTOR_MM_MAX_TICKERS` |

## Pausas intermedias (antes del rollback total)

- **`quotes_paused`** (persistente): deja de cotizar, sigue gestionando/cancelando.
  Para R5/R7 o cualquier "quiero mirar sin apagar".
- **Kill-switch** (existente): cancel-all < 5 s + freno total. Para R6 o pánico.

## Primera semana (post-ventana)

- Revisión DIARIA del digest (línea `M5 shadow`/live) + `motor5.funnel` con
  `scripts/query_logs.py --pattern "motor5" --since <fecha>`.
- Adverse selection (riesgo §7 del plan): si los fills se concentran justo antes de
  movimientos del fair (medible: fill → cambio de fair > 3pp en el ciclo siguiente en
  > 30% de los fills), el MM cotiza solo mercados lentos o vuelve a shadow. Ese
  resultado TAMBIÉN es éxito del plan.
- Subas de sizing/tickers: SOLO por config, un parámetro a la vez, con el dato del
  funnel como justificación (plan §6).
