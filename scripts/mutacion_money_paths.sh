#!/usr/bin/env bash
# Mutation testing de los MONEY PATHS (2026-08-05) — ¿los tests muerden?
#
# Qué hace: muta src/math/ (fees, arbitrage, kelly, no_vig) y corre los tests espejo
# por cada mutante. Un mutante que SOBREVIVE = un cambio en la matemática del dinero
# que ningún test detectaría. Primera corrida (2026-08-05): fees.py BLINDADA (sus 5
# "sobrevivientes" eran strings de error + 2 mutantes equivalentes — el guard de
# price==100 es redundante porque la fórmula da 0 sola); arbitrage/kelly/no_vig con
# 67 sobrevivientes a triagear (parte los matan los tests de tests/strategies/ que
# este runner no corre — verificar antes de acusar un hueco).
#
# Dónde correr: LOCAL o CI manual, jamás en el container de producción (muta archivos
# del working tree temporalmente y los revierte; corre pytest N veces).
# Costo: ~2-5 min con tests/math (90 tests en 0.08s). NO gasta APIs ni cuota.
# Uso:
#   pip install -e .[dev]
#   ./scripts/mutacion_money_paths.sh          # corre y muestra el resumen
#   mutmut show <id>                           # inspecciona un sobreviviente
#
# Regla de lectura (la misma de todo el repo): un sobreviviente NO es automáticamente
# un hueco — puede ser (a) mutación cosmética (texto de un error), (b) mutante
# EQUIVALENTE (mismo comportamiento), o (c) cubierto por tests fuera de tests/math.
# Solo el triage manual convierte el número en decisión.
set -euo pipefail
cd "$(dirname "$0")/.."

mutmut run \
  --paths-to-mutate src/math/ \
  --runner "python -m pytest -x -q tests/math/" \
  --tests-dir tests/math/ \
  --no-progress || true  # exit != 0 cuando hay sobrevivientes: el resumen es el output

echo
mutmut results
