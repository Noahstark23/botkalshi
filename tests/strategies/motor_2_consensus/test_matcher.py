"""
Tests del event matcher (Motor 2) — 4 reglas estrictas: normalización, alias,
cardinalidad, comparación por conjuntos. Fixtures de fútbol (1X2) y deportes US.
"""

from __future__ import annotations

import pytest

from src.strategies.motor_2_consensus.matcher import (
    canonical_name,
    match_outcomes,
    normalize_name,
    outcomes_match,
)

# ── Fixtures de payloads (solo los nombres de outcome, que es lo que el matcher compara) ──
WC_KALSHI = ["Argentina", "Mexico", "Draw"]  # 1X2, orden Kalshi
WC_ODDS = ["Mexico", "Draw", "Argentina"]  # 1X2, orden invertido (Odds API)
WC_KALSHI_2WAY = ["Argentina", "Mexico"]  # gana/pierde (sin empate)
MLB_KALSHI = ["New York Yankees", "Boston Red Sox"]
MLB_ODDS = ["Boston Red Sox", "New York Yankees"]  # home/away invertido
NBA_ODDS = ["Los Angeles Lakers", "Boston Celtics"]


# =====================================================
# Regla 1 — normalize_name
# =====================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("New York Yankees", "new york yankees"),
        ("St. Louis", "st louis"),  # puntuación eliminada
        ("Côte d'Ivoire", "cote divoire"),  # apóstrofo fuera + acento plegado
        ("Real   Madrid", "real madrid"),  # espacios colapsados
        ("  Draw  ", "draw"),  # trim
    ],
)
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


@pytest.mark.parametrize(
    ("accented", "plain"),
    [("Perú", "Peru"), ("Cádiz", "Cadiz"), ("México", "Mexico"), ("Bošnjaci", "Bosnjaci")],
)
def test_accent_folding(accented, plain):
    """Hotfix acentos: NFKD pliega diacríticos → la versión acentuada matchea la plana."""
    assert normalize_name(accented) == normalize_name(plain)
    assert canonical_name(accented) == canonical_name(plain)


# =====================================================
# Regla 2 — alias
# =====================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Empate", "draw"),
        ("Tie", "draw"),
        ("X", "draw"),
        ("USA", "united states"),
        ("United States of America", "united states"),
        ("Korea Republic", "south korea"),
        ("Yankees", "new york yankees"),  # nickname MLB poblado (plan 2026-07-02)
    ],
)
def test_canonical_name_resolves_alias(raw, expected):
    assert canonical_name(raw) == expected


# =====================================================
# Mundial 2026 — aliases de selecciones (grafías Kalshi ↔ The Odds API)
# =====================================================


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Türkiye", "Turkey"),  # confirmado en prod
        ("Cabo Verde", "Cape Verde"),
        ("Congo DR", "DR Congo"),
        ("Democratic Republic of the Congo", "DR Congo"),
        ("Korea DPR", "North Korea"),
        ("China PR", "China"),
    ],
)
def test_worldcup_team_alias_pairs_canonicalize_equal(a, b):
    """Mismo país en dos grafías → mismo canónico (el alias es simétrico)."""
    assert canonical_name(a) == canonical_name(b)


def test_worldcup_turkey_real_1x2_matches():
    """Partido real del Mundial: 'Türkiye' (Kalshi) ↔ 'Turkey' (Odds) → matchea."""
    kalshi = ["Türkiye", "Paraguay", "Draw"]
    odds = ["Turkey", "Draw", "Paraguay"]
    assert outcomes_match(kalshi, odds) is True
    assert match_outcomes(kalshi, odds) == {
        "Türkiye": "Turkey",
        "Paraguay": "Paraguay",
        "Draw": "Draw",
    }


def test_dr_congo_does_not_match_republic_of_congo():
    """SALVAGUARDA: RD del Congo y Rep. del Congo son países DISTINTOS → NO deben matchear."""
    assert canonical_name("DR Congo") != canonical_name("Congo")
    assert outcomes_match(["DR Congo", "Draw", "Spain"], ["Congo", "Draw", "Spain"]) is False


# =====================================================
# Regla 3 — cardinalidad estricta
# =====================================================


def test_cardinality_mismatch_2way_vs_3way_rejected():
    """Kalshi 2-way (gana/pierde) vs Odds API 3-way (1X2) → NO matchea."""
    assert outcomes_match(WC_KALSHI_2WAY, WC_ODDS) is False
    assert match_outcomes(WC_KALSHI_2WAY, WC_ODDS) is None


# =====================================================
# Regla 4 — comparación por conjuntos (inversiones + alias + faltantes)
# =====================================================


def test_soccer_1x2_order_inversion_matches():
    """Mismo 1X2 en distinto orden → matchea (sets) y empareja por nombre."""
    assert outcomes_match(WC_KALSHI, WC_ODDS) is True
    pairing = match_outcomes(WC_KALSHI, WC_ODDS)
    assert pairing == {"Argentina": "Argentina", "Mexico": "Mexico", "Draw": "Draw"}


def test_mlb_home_away_inversion_matches():
    """Deporte US 2-way con home/away invertido → matchea por conjunto."""
    assert outcomes_match(MLB_KALSHI, MLB_ODDS) is True
    assert match_outcomes(MLB_KALSHI, MLB_ODDS) == {
        "New York Yankees": "New York Yankees",
        "Boston Red Sox": "Boston Red Sox",
    }


def test_alias_and_draw_resolution_matches():
    """Empate↔Draw y USA↔United States resueltos por alias dentro del set."""
    kalshi = ["USA", "Mexico", "Empate"]
    odds = ["United States", "Draw", "Mexico"]
    assert outcomes_match(kalshi, odds) is True
    pairing = match_outcomes(kalshi, odds)
    assert pairing == {"USA": "United States", "Mexico": "Mexico", "Empate": "Draw"}


def test_missing_team_rejected():
    """Falta/sobra un equipo (mismo len, set distinto) → descarta."""
    assert outcomes_match(["Argentina", "Mexico"], ["Argentina", "Brazil"]) is False
    assert match_outcomes(["Argentina", "Mexico"], ["Argentina", "Brazil"]) is None


def test_punctuation_and_case_differences_match():
    """Diferencias de puntuación/caso no impiden el match (normalización)."""
    assert (
        outcomes_match(
            ["St. Louis Cardinals", "CHICAGO CUBS"], ["St Louis Cardinals", "Chicago Cubs"]
        )
        is True
    )


def test_different_sport_pair_does_not_match():
    """Outcomes de partidos distintos (MLB vs NBA) → no matchea."""
    assert outcomes_match(MLB_KALSHI, NBA_ODDS) is False


def test_duplicate_outcome_names_rejected_as_ambiguous():
    """Nombres duplicados tras canonizar (ambigüedad) → descarta aunque len coincida."""
    # "Tie" y "Draw" colapsan al mismo canónico → el lado tiene 2 nombres → 1 canónico.
    assert outcomes_match(["Draw", "Tie"], ["Draw", "Argentina"]) is False


# =====================================================
# MLB — alias nombre corto (Kalshi) ↔ full (Odds API) + trampa shared-city
# =====================================================


@pytest.mark.parametrize(
    ("kalshi_short", "expected_full"),
    [
        ("Atlanta", "atlanta braves"),
        ("San Francisco", "san francisco giants"),
        ("New York M", "new york mets"),
        ("New York Y", "new york yankees"),
        ("Chicago C", "chicago cubs"),
        ("Chicago WS", "chicago white sox"),
        ("Los Angeles A", "los angeles angels"),
        ("Los Angeles D", "los angeles dodgers"),
        ("A's", "athletics"),  # "A's" → normaliza a 'as' → alias a 'athletics'
        ("St. Louis", "st louis cardinals"),
    ],
)
def test_mlb_short_name_resolves_to_full(kalshi_short, expected_full):
    """Kalshi corto → canónico full de Odds API (vía alias MLB)."""
    assert canonical_name(kalshi_short) == expected_full


def test_mlb_real_game_short_vs_full_matches():
    """Partido real: Kalshi corto ↔ Odds full → matchea por conjunto."""
    assert (
        outcomes_match(["Atlanta", "San Francisco"], ["Atlanta Braves", "San Francisco Giants"])
        is True
    )


@pytest.mark.parametrize(
    ("kalshi", "odds"),
    [
        (["New York M", "Cincinnati"], ["New York Yankees", "Cincinnati Reds"]),  # Mets≠Yankees
        (["Chicago C", "Colorado"], ["Chicago White Sox", "Colorado Rockies"]),  # Cubs≠White Sox
        (
            ["Los Angeles A", "Arizona"],
            ["Los Angeles Dodgers", "Arizona Diamondbacks"],
        ),  # Angels≠Dodgers
    ],
)
def test_mlb_shared_city_wrong_team_does_not_match(kalshi, odds):
    """La trampa shared-city: el equipo equivocado de la misma ciudad NO debe matchear."""
    assert outcomes_match(kalshi, odds) is False


@pytest.mark.parametrize(
    ("kalshi", "odds"),
    [
        (["New York M", "Cincinnati"], ["New York Mets", "Cincinnati Reds"]),
        (["Chicago C", "Colorado"], ["Chicago Cubs", "Colorado Rockies"]),
        (["Los Angeles A", "Arizona"], ["Los Angeles Angels", "Arizona Diamondbacks"]),
    ],
)
def test_mlb_shared_city_correct_team_matches(kalshi, odds):
    """El equipo CORRECTO de la misma ciudad sí matchea (desambiguación por sufijo)."""
    assert outcomes_match(kalshi, odds) is True
