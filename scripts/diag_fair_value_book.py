#!/usr/bin/env python3
"""
Diagnóstico READ-ONLY del canal Motor 2 → Motor 5 (FairValueBook).

Incidente 2026-07-09: Motor 5 no cotizaba (`motor2.fair_book publicados=N` en los logs
pero el funnel con `fair_fresh=0` sostenido). Causa: doble identidad del módulo
`fair_value_book` por un PYTHONPATH que incluye /app/src además de /app → dos objetos-clase
con dos `_book` distintos (Motor 2 publica en uno, Motor 5 lee el otro vacío).

Este script confirma en el contenedor:
  1. El PYTHONPATH y si /app/src está colado (la causa raíz operativa).
  2. Las claves de sys.modules del módulo (una sola = sano; dos = doble identidad).
  3. Que un publish se ve por AMBAS rutas de import (el blindaje del fix ya mergeado).

Uso (dentro del container):
    python scripts/diag_fair_value_book.py

No escribe nada de red ni DB; solo publica un fair de prueba en el store en memoria y lo
limpia al terminar.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    print("=== PYTHONPATH ===")
    print(f"  env PYTHONPATH = {os.environ.get('PYTHONPATH', '(vacío)')}")
    src_on_path = any(p.rstrip("/").endswith("/src") or p.rstrip("/") == "src" for p in sys.path)
    print(
        f"  ¿'/…/src' en sys.path? = {src_on_path}  "
        f"{'← CAUSA RAÍZ: sacar /app/src del PYTHONPATH' if src_on_path else '(ok)'}"
    )

    print("\n=== import canónico ===")
    from src.strategies.fair_value_book import FairValueBook as Fvb

    keys = sorted(k for k in sys.modules if k.endswith("fair_value_book"))
    print(f"  claves en sys.modules = {keys}")
    print(f"  id(FairValueBook) = {id(Fvb)}")

    # Intentar la SEGUNDA ruta de import (solo resuelve si /app/src está en el path).
    print("\n=== ¿existe la segunda identidad de clase? ===")
    try:
        from strategies.fair_value_book import FairValueBook as FvbAlt  # type: ignore

        distinta = Fvb is not FvbAlt
        print(f"  'strategies.fair_value_book' IMPORTA → id={id(FvbAlt)}")
        print(
            f"  ¿clase distinta a la canónica? = {distinta}  "
            f"{'← doble identidad ACTIVA' if distinta else ''}"
        )
        Fvb.publish({"__diag__": 0.5})
        compartido = FvbAlt.size() >= 1
        print(
            f"  publish por la canónica → visible por la alterna = {compartido}  "
            f"{'(blindaje OK: libro compartido)' if compartido else '← LIBRO PARTIDO (fix no aplicado?)'}"
        )
        Fvb.clear()
    except ModuleNotFoundError:
        print(
            "  'strategies.fair_value_book' NO importa → una sola identidad (sano). "
            "El canal no se parte por esta vía."
        )

    print("\n=== veredicto ===")
    if src_on_path:
        print("  PYTHONPATH incluye /src → la doble identidad es POSIBLE. Con el fix mergeado")
        print("  (store anclado a sys) el fair fluye igual, pero conviene SACAR /app/src del")
        print("  PYTHONPATH en Coolify (dejar solo /app) para eliminar la causa raíz.")
    else:
        print("  PYTHONPATH sano (solo /app). Si el funnel sigue con fair_fresh=0, el problema")
        print("  es OTRO (odds no live → M2 no publica, o el fair está stale > TTL).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
