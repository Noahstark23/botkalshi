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
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from loguru import logger

# Convención de fechas de Kalshi (exchange US): el datestamp del event_key es la fecha
# LOCAL del Este. Un juego a las 10pm ET es "hoy" en el key aunque en UTC ya sea mañana.
ET = ZoneInfo("America/New_York")

_KEY_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}  # fmt: skip

# Segmento fecha del event_key: "26JUN27..." = 2026-06-27; los doubleheaders de MLB
# agregan la hora ET: "26JUN271610..." = 2026-06-27 16:10 ET.
_EVENT_KEY_START_RE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})(\d{4})?")


def parse_event_key_start(event_key: str) -> tuple[date, tuple[int, int] | None] | None:
    """Fecha (y hora ET opcional, doubleheaders) embebida en el event_key de Kalshi.

    "KXMLBGAME-26JUN27NYMPHI"      → (date(2026, 6, 27), None)
    "KXMLBGAME-26JUN271610PHINYM"  → (date(2026, 6, 27), (16, 10))
    Key sin datestamp parseable → None (el caller cae al matching sin fecha).
    """
    parts = event_key.split("-")
    if len(parts) < 2:
        return None
    m = _EVENT_KEY_START_RE.match(parts[1])
    if m is None:
        return None
    yy, mon, dd, hhmm = m.group(1), m.group(2), m.group(3), m.group(4)
    month = _KEY_MONTHS.get(mon)
    if month is None:
        return None
    try:
        d = date(2000 + int(yy), month, int(dd))
    except ValueError:
        return None
    start: tuple[int, int] | None = None
    if hhmm is not None:
        hh, mi = int(hhmm[:2]), int(hhmm[2:])
        if hh < 24 and mi < 60:
            start = (hh, mi)
    return d, start


# Deporte por prefijo de serie Kalshi → prefijos de sport_key de The Odds API. El matcher
# de nombres es global y los aliases ciudad→equipo son de MLB: sin este gate, onboardear
# otro deporte "sin tocar código" (como invita la config) armaba el cross-match catastrófico
# (NBA "Boston" canoniza a "boston red sox" y matchea el set MLB). Serie DESCONOCIDA → no
# filtra (compat), pero se loguea one-shot para agregarla acá al onboardear.
_SERIES_SPORT_PREFIXES: dict[str, tuple[str, ...]] = {
    "KXMLBGAME": ("baseball",),
    "KXWCGAME": ("soccer",),
    "KXWCGROUPWIN": ("soccer",),
    "KXMENWORLDCUP": ("soccer",),
    "KXMWORLDCUP": ("soccer",),
    "KXNBAGAME": ("basketball",),
    "KXNBA": ("basketball",),
    "KXNHLGAME": ("icehockey",),
    "KXNHL": ("icehockey",),
    "KXNFLGAME": ("americanfootball",),
    "KXNFL": ("americanfootball",),
}
_unknown_series_logged: set[str] = set()


def series_sport_compatible(event_key: str, sport_key: str) -> bool:
    """False si la serie del event_key tiene deporte CONOCIDO y el sport_key del odds
    event no le corresponde. Serie desconocida → True (no filtra; log one-shot)."""
    prefix = event_key.split("-", 1)[0]
    sports = _SERIES_SPORT_PREFIXES.get(prefix)
    if sports is None:
        if prefix not in _unknown_series_logged:
            _unknown_series_logged.add(prefix)
            logger.info(
                f"motor2.matcher.serie_sin_deporte prefix={prefix} — agregar a "
                "_SERIES_SPORT_PREFIXES al onboardear (sin el gate, los aliases de ciudad "
                "pueden cruzar deportes)"
            )
        return True
    return any(sport_key.startswith(sp) for sp in sports)


def start_time_et(commence_time: datetime) -> datetime:
    """`commence_time` (aware, o naive asumido UTC) convertido a hora del Este."""
    if commence_time.tzinfo is None:
        commence_time = commence_time.replace(tzinfo=UTC)
    return commence_time.astimezone(ET)


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
    # === Mundial 2026 — selecciones con grafía distinta entre Kalshi y The Odds API.
    # Cada entrada es el MISMO país en dos grafías (oficial vs común): el alias se aplica a
    # AMBOS lados, así que matchea sin importar qué plataforma use cuál. NUNCA mapear dos
    # países distintos (sería el match catastrófico que advierte el docstring).
    "turkiye": "turkey",  # confirmado en prod (Kalshi "Türkiye" ↔ Odds "Turkey")
    "cabo verde": "cape verde",
    "congo dr": "dr congo",  # RD del Congo — NO toca "congo" (Rep. del Congo, otro país)
    "democratic republic of the congo": "dr congo",
    "democratic republic of congo": "dr congo",
    "korea dpr": "north korea",
    "dpr korea": "north korea",
    "china pr": "china",
    # === MLB (2026) — Kalshi usa nombre CORTO (ciudad/abrev), The Odds API el FULL
    # "Ciudad Equipo". Mapea el corto canónico → full canónico (el full ya canoniza a sí
    # mismo, sin entrada). Ciudades compartidas: Kalshi desambigua con sufijo (m/y, c/ws,
    # a/d) → claves DISTINTAS → imposible cruzar Mets↔Yankees con el matcher de igualdad
    # exacta de conjuntos. ⚠️ Alias ciudad→equipo MLB-scoped: si se onboarda otro deporte con
    # las mismas ciudades (NBA Celtics/Lakers...), revisar (idealmente alias por deporte);
    # hoy Motor 2 corre 1 sport_key a la vez, así que es seguro.
    "arizona": "arizona diamondbacks",
    "atlanta": "atlanta braves",
    # Athletics: la franquicia dejó Oakland (Sacramento 2025-27 → Las Vegas); The Odds
    # API puede reportar cualquiera de las variantes con ciudad. Todas son EL MISMO
    # equipo → cero riesgo de cross-match. Kalshi "A's" normaliza a 'as'.
    "as": "athletics",
    "oakland athletics": "athletics",
    "sacramento athletics": "athletics",
    "las vegas athletics": "athletics",
    "baltimore": "baltimore orioles",
    "boston": "boston red sox",
    "chicago c": "chicago cubs",
    "chicago ws": "chicago white sox",
    "cincinnati": "cincinnati reds",
    "cleveland": "cleveland guardians",
    "colorado": "colorado rockies",
    "detroit": "detroit tigers",
    "houston": "houston astros",
    "kansas city": "kansas city royals",
    "los angeles a": "los angeles angels",
    "los angeles d": "los angeles dodgers",
    "miami": "miami marlins",
    "milwaukee": "milwaukee brewers",
    "minnesota": "minnesota twins",
    "new york m": "new york mets",
    "new york y": "new york yankees",
    "philadelphia": "philadelphia phillies",
    "pittsburgh": "pittsburgh pirates",
    "san diego": "san diego padres",
    "san francisco": "san francisco giants",
    "seattle": "seattle mariners",
    "st louis": "st louis cardinals",
    "tampa bay": "tampa bay rays",
    "texas": "texas rangers",
    "toronto": "toronto blue jays",
    "washington": "washington nationals",
    # === MLB — abreviaturas oficiales, nicknames y formas mixtas (plan amarillo
    # 2026-07-02). Evidencia: rej_names=16/49 (33%) en el funnel vivo + probes del
    # operador (cws/sf/sd/lad/laa/"ny yankees"/"white sox"/"padres"/"oakland" MISSING).
    # Cada entrada mapea a UN solo equipo. Nicknames que colisionan con otros deportes
    # (Giants NFL, Cardinals NFL, Rangers NHL) están protegidos por el gate
    # series_sport_compatible, no por esta tabla. Todo VALOR es canónico (test
    # anti-encadenamiento en test_matching_debts.py).
    "ari": "arizona diamondbacks",
    "az": "arizona diamondbacks",
    "diamondbacks": "arizona diamondbacks",
    "dbacks": "arizona diamondbacks",  # "D-backs" normaliza sin guión
    "atl": "atlanta braves",
    "braves": "atlanta braves",
    "oak": "athletics",
    "ath": "athletics",
    "oakland": "athletics",
    "bal": "baltimore orioles",
    "orioles": "baltimore orioles",
    "bos": "boston red sox",
    "red sox": "boston red sox",
    "chc": "chicago cubs",
    "cubs": "chicago cubs",
    "chi cubs": "chicago cubs",
    "cws": "chicago white sox",
    "chw": "chicago white sox",
    "white sox": "chicago white sox",
    "chi white sox": "chicago white sox",
    "cin": "cincinnati reds",
    "reds": "cincinnati reds",
    "cle": "cleveland guardians",
    "guardians": "cleveland guardians",
    "col": "colorado rockies",
    "rockies": "colorado rockies",
    "det": "detroit tigers",
    "tigers": "detroit tigers",
    "hou": "houston astros",
    "astros": "houston astros",
    "kc": "kansas city royals",
    "kcr": "kansas city royals",
    "royals": "kansas city royals",
    "laa": "los angeles angels",
    "la angels": "los angeles angels",
    "angels": "los angeles angels",
    "lad": "los angeles dodgers",
    "la dodgers": "los angeles dodgers",
    "dodgers": "los angeles dodgers",
    "mia": "miami marlins",
    "marlins": "miami marlins",
    "mil": "milwaukee brewers",
    "brewers": "milwaukee brewers",
    "min": "minnesota twins",
    "twins": "minnesota twins",
    "nym": "new york mets",
    "ny mets": "new york mets",
    "mets": "new york mets",
    "nyy": "new york yankees",
    "ny yankees": "new york yankees",
    "yankees": "new york yankees",
    "phi": "philadelphia phillies",
    "phillies": "philadelphia phillies",
    "pit": "pittsburgh pirates",
    "pirates": "pittsburgh pirates",
    "sd": "san diego padres",
    "sdp": "san diego padres",
    "padres": "san diego padres",
    "sf": "san francisco giants",
    "sfg": "san francisco giants",
    "giants": "san francisco giants",
    "sea": "seattle mariners",
    "mariners": "seattle mariners",
    "stl": "st louis cardinals",
    "cardinals": "st louis cardinals",
    "tb": "tampa bay rays",
    "tbr": "tampa bay rays",
    "rays": "tampa bay rays",
    "tex": "texas rangers",
    "rangers": "texas rangers",
    "tor": "toronto blue jays",
    "blue jays": "toronto blue jays",
    "jays": "toronto blue jays",
    "wsh": "washington nationals",
    "was": "washington nationals",
    "nationals": "washington nationals",
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
