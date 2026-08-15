"""
Sonda READ-ONLY de los perpetuos de Kalshi — medir antes de opinar.

POR QUÉ EXISTE (2026-08-15): la investigación concluyó que los perps NO son para este
bot (rompen el axioma de pérdida acotada; los frenos reportarían 0% mientras la cuenta
se vacía; la variante neutral es inconstruible sin spot). El operador quiere decidirlo
con datos PROPIOS y no con la conclusión de un dossier — y tiene razón: el estándar de
este proyecto es medir. Esta sonda produce esa evidencia a costo CERO.

QUÉ ES Y QUÉ NO ES:
  - Es un script one-shot, sin tabla, sin task en el runner, sin tocar src/. Market data
    PÚBLICO: no firma, no usa la cuenta de margen, no coloca ni una orden.
  - NO es un motor y no debe convertirse en uno sin pasar los gates de capital y riesgo
    documentados en .claude/skills/apuestas-plata (reapertura exige las TRES condiciones).
  - NO simula liquidación ni margen. Por lo tanto sus números NO autorizan ninguna
    extrapolación a PnL: miden la OPORTUNIDAD BRUTA, no lo que sobreviviría.

GATE PRE-REGISTRADO (se imprime ANTES de los datos, a propósito — el criterio no se
elige después de ver el resultado):
    p50 de |basis| > (round-trip taker en bps) + 2 × spread cruzado,
    con duración mediana del desvío > 5s.
Predicción explícita del dossier: p50 va a dar 1-2 bps. Si da eso, el tema se cierra
con número propio. Si da 30+ bps sostenidos, hay algo que nadie está arbitrando y ahí
sí vale la conversación de capital.

USO (en el container):
    python3 scripts/sonda_perps.py --descubrir          # mapea qué endpoints responden
    python3 scripts/sonda_perps.py --minutos 60         # muestrea y reporta
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.request

BASE = "https://api.elections.kalshi.com/trade-api/v2"

# HOSTS candidatos (ampliado 2026-08-15 tras la primera corrida del agente): los perps
# NO están bajo api.elections.kalshi.com/trade-api/v2 — `/perpetuals`, `/perpetual_markets`
# y `/perps` dieron 404 mientras `/markets` y `/exchange/status` daban 200 en la MISMA
# corrida. O sea: no es red ni auth, el recurso no vive en esa base. Si los perps tienen
# API pública, está en otro host o bajo otro prefijo — la sonda barre la matriz completa
# en vez de asumir. Un 404 en TODA la matriz también es un resultado (sin API no hay
# motor posible, con o sin edge).
HOSTS = (
    "https://api.elections.kalshi.com/trade-api/v2",
    "https://api.kalshi.com/trade-api/v2",
    "https://trading-api.kalshi.com/trade-api/v2",
    "https://api.elections.kalshi.com/trade-api/v1",
)

# Candidatos de endpoint: la doc de perps estuvo inaccesible desde el entorno de
# desarrollo, así que la sonda DESCUBRE en vez de asumir. El primero que responda 200
# con JSON usable gana, y el script reporta cuál fue (nadie hereda un path inventado).
CANDIDATOS_MERCADOS = (
    "/margin/markets",
    "/perpetuals/markets",
    "/perpetuals",  # 404 confirmado en el host actual (caso de prueba negativo)
    "/perpetual_markets",  # 404 confirmado
    "/perps",  # 404 confirmado
    "/markets?series_ticker=BTCPERP",
    "/markets?tickers=BTCPERP",
)

# Control POSITIVO: si este responde 200 y los de perps 404, el diagnóstico es
# "el recurso no existe acá", no "el host está caído / falta auth". Sin este control
# un barrido de 404 no distingue las dos causas — y son veredictos distintos.
CONTROL_POSITIVO = "/exchange/status"
CANDIDATOS_ORDERBOOK = (
    "/margin/markets/{t}/orderbook",
    "/perpetuals/markets/{t}/orderbook",
    "/markets/{t}/orderbook",
)
CANDIDATOS_FUNDING = ("/margin/funding_rates", "/perpetuals/funding_rates")

# Round-trip taker asumido si la sonda no puede leerlo del schedule (bps sobre nocional).
# Es un SUPUESTO explícito, no un dato: el gate lo imprime como tal.
ROUND_TRIP_TAKER_BPS = 24.0


def _get(path: str, timeout: float = 10.0, base: str = BASE) -> tuple[int, dict | None]:
    url = path if path.startswith("http") else f"{base}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return r.status, json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001 — la sonda reporta, no rompe
        return 0, {"error": f"{type(exc).__name__}: {exc}"}


def descubrir() -> None:
    """Barre la matriz HOST × PATH. Primera corrida obligatoria.

    Cada host se prueba primero con un CONTROL POSITIVO (`/exchange/status`): si el
    control responde 200 y los paths de perps 404, el diagnóstico es "el recurso no
    existe en esta base". Si el control TAMBIÉN falla, el host está caído o no es el
    correcto — son dos veredictos distintos y un barrido sin control los confunde."""
    print("DESCUBRIMIENTO de endpoints de perps (public market data, sin firma)\n")
    hallados: list[str] = []
    for base in HOSTS:
        estado_control, _ = _get(CONTROL_POSITIVO, base=base)
        etiqueta = "control OK" if estado_control == 200 else f"control {estado_control or 'ERR'}"
        print(f"[{base}]  ({etiqueta})")
        if estado_control != 200:
            print("  host no responde el control → los 404 de abajo NO son concluyentes\n")
            continue
        for grupo, candidatos in (
            ("mercados", CANDIDATOS_MERCADOS),
            ("funding", CANDIDATOS_FUNDING),
        ):
            for c in candidatos:
                status, body = _get(c, base=base)
                muestra = ""
                if status == 200 and isinstance(body, dict):
                    muestra = f" claves={sorted(body.keys())[:6]}"
                    hallados.append(f"{base}{c}")
                elif body and "error" in body:
                    muestra = f" {body['error'][:50]}"
                print(f"  {status or 'ERR':>3}  [{grupo}] {c}{muestra}")
        print()

    if hallados:
        print("ENDPOINTS VIVOS:")
        for h in hallados:
            print(f"  {h}")
    else:
        print(
            "NINGÚN endpoint de perps respondió 200 en hosts cuyo CONTROL sí responde.\n"
            "Eso YA es un veredicto: los perps no están en el API pública que este bot\n"
            "puede consumir — sin API no hay motor posible, con o sin edge. Antes de\n"
            "declararlo definitivo, reportar si algún host falló el control (esos 404\n"
            "no cuentan) y revisar docs.kalshi.com por un prefijo que no esté en HOSTS."
        )


def _basis_bps(mid: float, referencia: float) -> float:
    return 10_000 * (mid - referencia) / referencia if referencia else 0.0


def muestrear(ticker: str, minutos: float, cada_seg: float) -> None:
    print("GATE PRE-REGISTRADO (impreso ANTES de los datos, a propósito):")
    print(f"  p50 |basis| > {ROUND_TRIP_TAKER_BPS:.0f} bps (round-trip taker SUPUESTO) + 2×spread")
    print("  y duración mediana del desvío > 5s.")
    print("  Predicción del dossier: p50 = 1-2 bps → si da eso, el tema se cierra.\n")

    ruta_ob = None
    for c in CANDIDATOS_ORDERBOOK:
        status, _ = _get(c.format(t=ticker))
        if status == 200:
            ruta_ob = c
            break
    if ruta_ob is None:
        print("Sin endpoint de orderbook que responda → correr primero --descubrir")
        return

    fin = time.time() + minutos * 60
    bases: list[float] = []
    spreads: list[float] = []
    n = 0
    while time.time() < fin:
        status, body = _get(ruta_ob.format(t=ticker))
        if status == 200 and isinstance(body, dict):
            ob = body.get("orderbook", body)
            bid = ob.get("best_bid") or ob.get("bid")
            ask = ob.get("best_ask") or ob.get("ask")
            ref = body.get("reference_price") or ob.get("index_price")
            if bid and ask:
                mid = (float(bid) + float(ask)) / 2
                spreads.append(10_000 * (float(ask) - float(bid)) / mid if mid else 0.0)
                if ref:
                    bases.append(abs(_basis_bps(mid, float(ref))))
            n += 1
        time.sleep(cada_seg)

    print(f"MUESTRAS: {n} sobre {minutos:.0f} min (ticker {ticker}, endpoint {ruta_ob})")
    if not bases:
        print(
            "  sin `reference_price` en la respuesta → la sonda no puede medir basis.\n"
            "  Reportar el shape crudo del orderbook para ajustar el parser."
        )
    else:
        bases.sort()
        p50 = statistics.median(bases)
        p90 = bases[int(0.9 * (len(bases) - 1))]
        sp50 = statistics.median(spreads) if spreads else 0.0
        umbral = ROUND_TRIP_TAKER_BPS + 2 * sp50
        print(f"  |basis| p50={p50:.2f} bps  p90={p90:.2f} bps")
        print(f"  spread p50={sp50:.2f} bps  →  umbral del gate {umbral:.2f} bps")
        print()
        if p50 > umbral:
            print("  ⚠️ p50 SUPERA el umbral — hay algo que medir en serio (falta duración).")
        else:
            print(
                f"  VEREDICTO: p50 {p50:.2f} bps ≤ umbral {umbral:.2f} bps — el desvío NO paga\n"
                "  el round-trip. Tema cerrado con dato propio, como predijo el dossier."
            )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--descubrir", action="store_true", help="mapear endpoints y salir")
    ap.add_argument("--ticker", default="BTCPERP")
    ap.add_argument("--minutos", type=float, default=30.0)
    ap.add_argument("--cada", type=float, default=15.0, help="segundos entre muestras")
    args = ap.parse_args()
    if args.descubrir:
        descubrir()
    else:
        muestrear(args.ticker, args.minutos, args.cada)


if __name__ == "__main__":
    main()
