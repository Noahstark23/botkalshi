"""
Event/outcome matcher cross-platform: Kalshi ↔ The Odds API (Motor 2).

Principio rector: emparejar el partido EQUIVOCADO es una falla silenciosa catastrófica
(apostarías el precio del equipo A contra la probabilidad justa del equipo B). Por eso
el matcher es deliberadamente CONSERVADOR: ante cualquier ambigüedad → descarta.

Cuatro reglas estrictas por default:
  1. Normalización exacta (`normalize_name`): minúsculas, sin puntuación, espacios colapsados.
  2. Tabla de alias en memoria (`TEAM_ALIASES`), aplicada DESPUÉS de la normalización base.
  3. Control de cardinalidad: si los arrays de outcomes tienen distinta longitud
     (ej. Kalshi 2-way vs Odds API 3-way 1X2) → NO matchea.
  4. Comparación por conjuntos: las listas normalizadas → `set` y `==`. Resuelve gratis
     las inversiones de orden (Local/Visitante); si falta un equipo o sobra, los sets
     difieren y se descarta.

Lógica PURAMENTE SÍNCRONA. No toca red, ni capital, ni ejecución.

ACENTOS: `normalize_name` pliega acentos/diacríticos vía NFKD antes del regex, así que
las selecciones acentuadas del Mundial matchean ("Perú"→"peru", "México"→"mexico",
"Côte d'Ivoire"→"cote divoire").
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

# Alias conocidos (clave = nombre YA normalizado por normalize_name → valor canónico).
# Resuelve discrepancias Odds API ↔ Kalshi sin matchear por error. Curada y extensible.
TEAM_ALIASES: dict[str, str] = {
    # Empate (1X2)
    "tie": "draw",
    "empate": "draw",
    "x": "draw",
    # Selecciones — alias frecuentes entre plataformas
    "usa": "united states",
    "united states of america": "united states",
    "us": "united states",
    "uk": "united kingdom",
    "korea republic": "south korea",
    "republic of korea": "south korea",
    "ir iran": "iran",
    "iran islamic republic": "iran",
    "ivory coast": "cote divoire",  # Kalshi (inglés) ↔ "côte d'ivoire" plegado
    "czechia": "czech republic",
}


def normalize_name(name: str) -> str:
    """
    Normalización base: pliega acentos → minúsculas → sin puntuación → espacios colapsados.

    1. NFKD + ASCII-ignore pliega acentos/diacríticos ('Perú'→'Peru', 'Cádiz'→'Cadiz').
    2. `re.sub(r'[^\\w\\s]', '', ...)` elimina toda puntuación (puntos, comas, apóstrofos).
    3. `\\s+`→' ' colapsa espacios múltiples; strip.
    """
    folded = unicodedata.normalize("NFKD", name).encode("ASCII", "ignore").decode("utf-8")
    s = folded.lower()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def canonical_name(name: str) -> str:
    """Nombre canónico: normalización base + resolución de alias (alias gana si existe)."""
    base = normalize_name(name)
    return TEAM_ALIASES.get(base, base)


def _canonical_set(names: Sequence[str]) -> set[str]:
    return {canonical_name(n) for n in names}


def outcomes_match(kalshi_outcomes: Sequence[str], odds_outcomes: Sequence[str]) -> bool:
    """
    True solo si ambos lados describen el MISMO conjunto de outcomes.

    Reglas (todas deben pasar):
      - Cardinalidad idéntica (len == len). 2-way vs 3-way → False.
      - Sin nombres duplicados dentro de un lado tras canonizar (ambigüedad → False).
      - Los conjuntos canónicos son iguales (orden-independiente; falta/sobra equipo → False).
    """
    if len(kalshi_outcomes) != len(odds_outcomes):
        return False  # cardinalidad (ej. Kalshi gana/pierde vs Odds gana/empata/pierde)

    k_set = _canonical_set(kalshi_outcomes)
    o_set = _canonical_set(odds_outcomes)
    # Si canonizar colapsó duplicados, hay ambigüedad → descartar (fail-safe).
    if len(k_set) != len(kalshi_outcomes) or len(o_set) != len(odds_outcomes):
        return False
    return k_set == o_set


def match_outcomes(
    kalshi_outcomes: Sequence[str], odds_outcomes: Sequence[str]
) -> dict[str, str] | None:
    """
    Si los conjuntos matchean, devuelve el emparejamiento {outcome_kalshi → outcome_odds}
    (nombres ORIGINALES, para que el detector recupere el precio de cada lado).
    None si no matchean (cardinalidad, ambigüedad o conjunto distinto).
    """
    if not outcomes_match(kalshi_outcomes, odds_outcomes):
        return None
    odds_by_canon = {canonical_name(o): o for o in odds_outcomes}
    return {k: odds_by_canon[canonical_name(k)] for k in kalshi_outcomes}
