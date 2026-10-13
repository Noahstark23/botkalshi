"""Identidad de equipo por TICKER de Kalshi — segunda fuente, independiente del nombre.

Problema (2026-10-09): el detector asigna el fair de un outcome al market por el
`yes_sub_title` ("New York M") y NADA verifica que ese market sea, de hecho, el del
equipo nombrado. El ticker lo dice por su cuenta: `KXMLBGAME-26JUL15NYMPHI-NYM` → NYM.
Si el sub-título y el código del ticker se contradicen (drift de shape de la API, un
market mal etiquetado, un hermano cruzado), el detector publicaba el fair del equipo A
en el ticker del equipo B: exactamente la "falla silenciosa catastrófica" que el
docstring del matcher declara como principio rector. Con este módulo, contradicción
POSITIVA → el evento entero se descarta (`reject_ticker_team` en el funnel).

Conocimiento POSITIVO solamente (mismo criterio que la purga de #183): un código que
no está en la tabla devuelve UNKNOWN y NO bloquea — el gate solo actúa cuando ambas
fuentes conocen al equipo y discrepan. Así una tabla incompleta nunca apaga el feed de
fair de M5; una tabla equivocada sí se vería como rechazos en el funnel.

Códigos OBSERVADOS en tickers reales del repo (fixtures/tests, jun–jul 2026): AZ, BOS,
COL, HOU, KC, LAD, MIL, NYM, NYY, PHI, SD, SFG, STL, TB, WSH. El resto son las
abreviaturas oficiales MLB y sus variantes; cada código apunta a UN solo equipo, así
que una variante de más no puede cruzar equipos — solo deja de ser UNKNOWN.
"""

from __future__ import annotations

from typing import Literal

from loguru import logger

from src.strategies.motor_2_consensus.matcher import canonical_name

Verdict = Literal["CONSISTENT", "CONTRADICTION", "UNKNOWN"]

# Código de ticker → nombre CANÓNICO (el mismo valor que produce canonical_name).
MLB_TICKER_CODES: dict[str, str] = {
    "AZ": "arizona diamondbacks",
    "ARI": "arizona diamondbacks",
    "ATL": "atlanta braves",
    "ATH": "athletics",
    "OAK": "athletics",
    "BAL": "baltimore orioles",
    "BOS": "boston red sox",
    "CHC": "chicago cubs",
    "CWS": "chicago white sox",
    "CHW": "chicago white sox",
    "CIN": "cincinnati reds",
    "CLE": "cleveland guardians",
    "COL": "colorado rockies",
    "DET": "detroit tigers",
    "HOU": "houston astros",
    "KC": "kansas city royals",
    "LAA": "los angeles angels",
    "LAD": "los angeles dodgers",
    "MIA": "miami marlins",
    "MIL": "milwaukee brewers",
    "MIN": "minnesota twins",
    "NYM": "new york mets",
    "NYY": "new york yankees",
    "PHI": "philadelphia phillies",
    "PIT": "pittsburgh pirates",
    "SD": "san diego padres",
    "SF": "san francisco giants",
    "SFG": "san francisco giants",
    "SEA": "seattle mariners",
    "STL": "st louis cardinals",
    "TB": "tampa bay rays",
    "TEX": "texas rangers",
    "TOR": "toronto blue jays",
    "WSH": "washington nationals",
}

# Códigos observados en las capturas Demo; cobertura parcial, sin deducir otros.
NFL_TICKER_CODES = {
    "ARI": "arizona cardinals",
    "ATL": "atlanta falcons",
    "BAL": "baltimore ravens",
    "BUF": "buffalo bills",
    "CAR": "carolina panthers",
    "CHI": "chicago bears",
    "CLE": "cleveland browns",
    "DAL": "dallas cowboys",
    "DEN": "denver broncos",
    "GB": "green bay packers",
    "HOU": "houston texans",
    "IND": "indianapolis colts",
    "JAC": "jacksonville jaguars",
    "KC": "kansas city chiefs",
    "LAC": "los angeles chargers",
    "LAR": "los angeles rams",
    "LV": "las vegas raiders",
    "NE": "new england patriots",
    "NO": "new orleans saints",
    "NYG": "new york giants",
    "NYJ": "new york jets",
    "PHI": "philadelphia eagles",
    "PIT": "pittsburgh steelers",
    "SEA": "seattle seahawks",
    "SF": "san francisco 49ers",
    "TB": "tampa bay buccaneers",
    "TEN": "tennessee titans",
    "WAS": "washington commanders",
}
NCAAF_TICKER_CODES = {
    "ALA": "alabama crimson tide",
    "UGA": "georgia bulldogs",
    "ND": "notre dame fighting irish",
    "STAN": "stanford cardinal",
    "FSU": "florida state seminoles",
    "LOU": "louisville cardinals",
}

# Serie → (sport_key de los aliases, tabla de códigos). Sin tabla → UNKNOWN.
_SERIES_CODES: dict[str, tuple[str, dict[str, str]]] = {
    "KXMLBGAME": ("baseball", MLB_TICKER_CODES),
    "KXNFLGAME": ("americanfootball_nfl", NFL_TICKER_CODES),
    "KXNCAAFGAME": ("americanfootball_ncaaf", NCAAF_TICKER_CODES),
}

# Outcomes que no son un equipo (empate en 1X2): el ticker no tiene código de club.
_NON_TEAM = {"draw"}


def ticker_team_code(market_ticker: str) -> str | None:
    """Código del outcome: el último segmento de SERIE-EVENTO-CÓDIGO. None si no hay."""
    parts = market_ticker.split("-")
    if len(parts) < 3 or not parts[-1]:
        return None
    return parts[-1].upper()


def check_ticker_team(market_ticker: str, outcome_name: str) -> Verdict:
    """¿El código del ticker y el nombre del outcome describen al MISMO equipo?"""
    series = market_ticker.split("-", 1)[0].upper()
    entry = _SERIES_CODES.get(series)
    code = ticker_team_code(market_ticker)
    if entry is None or code is None:
        return "UNKNOWN"
    sport, table = entry
    expected = table.get(code)
    if expected is None:
        return "UNKNOWN"
    named = canonical_name(outcome_name, sport_key=sport)
    if named in _NON_TEAM:
        return "UNKNOWN"
    # El nombre debe resolver a un equipo CONOCIDO de la misma tabla para poder
    # afirmar contradicción; un nombre sin alias es ignorancia, no evidencia.
    if named not in table.values():
        return "UNKNOWN"
    return "CONSISTENT" if named == expected else "CONTRADICTION"


def event_ticker_contradictions(outcomes: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """[(market_ticker, outcome_name)] → los pares que se contradicen (vacío = OK)."""
    bad = [(t, n) for t, n in outcomes if check_ticker_team(t, n) == "CONTRADICTION"]
    for t, n in bad:
        logger.warning(
            f"motor2.ticker_team_mismatch ticker={t} code={ticker_team_code(t)} "
            f"yes_sub_title={n!r} → evento descartado (el fair de un equipo jamás se "
            "publica en el ticker de otro)"
        )
    return bad
