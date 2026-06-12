# Post-mortem — Incidente de discovery (2026-06-12)

**Severidad:** captura de datos caída (`Tracking 0 markets`) en horario de Mundial.
**Duración:** ~19:13 → 19:39 UTC (~26 min, hasta el rollback).
**Impacto:** pérdida de ~26 min de captura de eventos del Mundial. **Cero impacto de
capital** (`TRADING_ENABLED=false`, 0 trades).

## Resumen

El deploy de `main` (commit `48d6a65`, merge de PR #42) trajo el discovery por
paginación amplia de PR #41. En el primer ciclo en producción, ese discovery **agotó
el cap de 200 páginas (40.000 markets) sin encontrar un solo market deportivo** →
`Tracking 0`, y cayó en el loop de reintento vacío (backoff 5s). Se resolvió con
rollback de Coolify a la imagen previa (`83793cd`, PR #30, discovery por series
exactas), que restauró `Tracking 300 markets`.

## Línea de tiempo (UTC)

| Hora | Evento |
|---|---|
| 17:43 | Build sano corriendo (`83793cd`/#30, 301 markets, series exactas) |
| ~19:08 | Deploy de `48d6a65`/#42 (auto-deploy tras merge) → discovery de #41 activo |
| 19:13:42 | 1er ciclo de discovery: `Discovery cortado en 200 páginas` → `Tracking 0 markets` → loop de reintento vacío |
| 19:37:38 | **Rollback** de Coolify a `83793cd`/#30 |
| 19:39:31 | `Tracking 300 markets` — captura restaurada |

## Causa raíz (con evidencia dura, no inferencia)

El discovery de #41 listaba **todos** los markets abiertos vía `list_markets` paginado
y filtraba por prefijo localmente. La estimación de diseño ("pocos miles a ~20k markets
abiertos") quedó **falsificada por la medición real**.

**Evidencia — curl forense contra `/markets` (5 páginas × 200 = 1000 markets, contenedor sano):**

```
paginas: 5 | muestra: 1000 | deportivos: 0
top prefijos: [('KXMVES', 836), ('KXMVEC', 164)]
```

- **0 deportivos en las primeras 1000 posiciones (0.0%).**
- Las primeras 1000 posiciones están **100% dominadas por dos familias no-deportivas**:
  `KXMVES` (~84%) y `KXMVEC` (~16%) — series tipo *mention/votes* (`KXMV*`).
- El cursor avanza (5 páginas distintas) pero nunca alcanza los deportivos.
  Extrapolado al cap (200 páginas × 200 = ~40k posiciones), el discovery agota el techo
  y aun así trackea 0 deportivos.

**Conclusión:** paginar `/markets` global **entierra** los markets deportivos detrás de
familias no-deportivas masivas → ni 40k posiciones los alcanzan. El prefiltro
server-side que el Paso 0 de P2 marcó como *fallback* era **requisito**.

## Lo que funcionó (defensas que evitaron algo peor)

- **Cap de seguridad** (`DISCOVERY_MAX_PAGES`): evitó un loop de paginación infinito.
- **Retry con backoff** del discovery vacío: no martilló la API.
- **`TRADING_ENABLED=false`**: el incidente fue de captura, nunca de capital.
- **Rollback de Coolify** (panel Configuration → Rollback): re-deploya la imagen previa
  local, sin editar source/flags — botón de pánico confiable.
- El resto del deploy `#42` arrancó **sano**: migración de EdgeWindow corrió
  (3× `ALTER TABLE`), settlement poller arrancó (no-op, 0 pendientes), kill-switch
  rehidratado, 0 `database is locked`.

## Resolución

1. **Inmediata:** rollback de Coolify a `83793cd`/#30 (pin a imagen previa). Captura
   restaurada. `main` deja de auto-trackearse mientras dura el pin.
2. **Fix-forward (PR #43):** restaurar el discovery por **series exactas** (#30) dentro
   del framework de #41 (conservando el re-discovery periódico y toda FASE 0), hasta
   tener el prefiltro server-side.
3. **P2 v2 (pendiente):** discovery server-side vía `GET /series?category=…`. Confirmado
   viable: `/series?category=Sports` → **2189 series** (colección chica), filtrable por
   prefijo de familia → `list_markets(series_ticker=…)` por serie. Decenas de requests,
   sin depender del orden de paginación global.

## Lecciones

1. **Una estimación de volumen no es una medición.** El diseño marcó el conteo de
   markets como `[medir en el primer ciclo]` y el fallback server-side como condicional;
   el primer ciclo en producción ERA el test, y falsificó la estimación. Para discovery
   sobre catálogos de terceros: **no asumir el orden ni el volumen de paginación.**
2. **merge ≈ deploy.** En este Coolify, mergear a `main` dispara build/deploy. Todo PR
   que toca runtime se trata como un deploy → no se mergea sin OK explícito del owner,
   y los fixes de incidente llevan `⚠️ NO mergear sin OK` en el título.
3. **El rollback por imagen previa es el botón de pánico correcto** — no editar el
   source commit a mano (más sucio, toca settings/git).
