"""
Remoción de vig (juice) de cuotas de sportsbook → probabilidades JUSTAS.

El sportsbook cobra su comisión inflando las probabilidades implícitas: la suma de
los inversos de las cuotas decimales es > 1 (el "overround" / vig). Para usar el
consenso como benchmark de "precio justo" (Motor 2) hay que quitar ese vig.

Dos métodos clásicos:
  - MULTIPLICATIVO (proporcional): fair_i = p_i / Σp. Reparte el vig en proporción a
    cada probabilidad. Es el método primario (estable, siempre devuelve probabilidades
    válidas que suman 1). Es el que pide el Motor 2.
  - ADITIVO (equal-margin): fair_i = p_i − (Σp − 1)/n. Resta el vig en partes iguales.
    Conocido por sesgar mal a los longshots (puede dar ≤ 0 en outcomes muy improbables);
    se incluye para comparación/diagnóstico, no como primario.

Trabaja sobre PROBABILIDADES implícitas (no cuotas) para separar parsing de matemática.
`implied_prob()` convierte cuota decimal → probabilidad implícita.

Soporta 2-way (NBA/MLB moneyline) y 3-way (fútbol 1X2: Local/Empate/Visitante) — la
cardinalidad la decide el caller (el matcher); acá solo se normaliza la lista recibida.
"""

from __future__ import annotations

from collections.abc import Sequence


def implied_prob(decimal_odds: float) -> float:
    """
    Probabilidad implícita (con vig) de una cuota DECIMAL. p = 1 / cuota.

    Raises ValueError si la cuota no es > 1.0 (una cuota decimal válida siempre paga
    más que el stake).
    """
    if not decimal_odds > 1.0:
        raise ValueError(f"cuota decimal debe ser > 1.0, got {decimal_odds}")
    return 1.0 / decimal_odds


def _validate(implied: Sequence[float]) -> None:
    if len(implied) < 2:
        raise ValueError(f"se requieren ≥2 outcomes, got {len(implied)}")
    for p in implied:
        if not (0.0 < p < 1.0):
            raise ValueError(f"cada probabilidad implícita debe estar en (0,1), got {p}")


def overround(implied: Sequence[float]) -> float:
    """Vig total = Σp − 1 (≈ 0.04–0.08 típico). Negativo ⇒ no hay vig (o arbitraje)."""
    return sum(implied) - 1.0


def remove_vig_multiplicative(implied: Sequence[float]) -> list[float]:
    """
    Método MULTIPLICATIVO (proporcional): fair_i = p_i / Σp.

    Siempre devuelve probabilidades válidas que SUMAN 1. Es el primario del Motor 2.
    """
    _validate(implied)
    total = sum(implied)
    if total <= 0.0:
        raise ValueError("la suma de probabilidades implícitas debe ser > 0")
    return [p / total for p in implied]


def remove_vig_additive(implied: Sequence[float]) -> list[float]:
    """
    Método ADITIVO (equal-margin): fair_i = p_i − (Σp − 1)/n.

    ⚠️ Puede producir valores ≤ 0 en longshots con vig alto (limitación conocida del
    método). Lanza ValueError si eso pasa: ahí el aditivo NO es apropiado y el caller
    debe usar el multiplicativo. No es el método primario.
    """
    _validate(implied)
    n = len(implied)
    margin_share = overround(implied) / n
    fair = [p - margin_share for p in implied]
    if any(f <= 0.0 for f in fair):
        raise ValueError(
            "el método aditivo produjo una probabilidad ≤ 0 (longshot + vig alto); "
            "usar remove_vig_multiplicative"
        )
    return fair
