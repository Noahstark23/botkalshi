"""Auditoría pura de reglas revisadas: no convierte cuotas en decisiones.

Las fichas son evidencia manual con fuente/jurisdicción; no hay extracción LLM
ni reglas inferidas del nombre de una casa. Una coincidencia parcial no autoriza
fair, señal ni ejecución. No modela probabilidad de empate o cancelación.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

DIMENSIONS = (
    "overtime",
    "draw",
    "postponement",
    "cancellation",
    "suspension",
    "forfeit_before_start",
    "venue_reversal",
    "result_corrections",
    "replay_from_start",
)
KALSHI_TERMS_URL = "https://assets.kalshi.com/contract_terms/FOOTBALLGAMEWIN.pdf"
KALSHI_TERMS_SHA256 = "57b57e1ad96466e413e9274c0d2598225997e35d71262eb63c4ae13858b81d38"


@dataclass(frozen=True)
class RuleProfile:
    source_url: str
    jurisdiction: str
    overtime: str | None
    draw: str | None
    postponement: str | None
    cancellation: str | None
    suspension: str | None = None
    forfeit_before_start: str | None = None
    venue_reversal: str | None = None
    result_corrections: str | None = None
    replay_from_start: str | None = None
    league: str | None = None
    phase: str | None = None
    market_period: str | None = None
    source_sha256: str | None = None


def compare_rules(kalshi: RuleProfile, sportsbook: RuleProfile, *, quote_jurisdiction: str | None):
    """Compara dimensiones; UNKNOWN se conserva aun si ambos lados lo desconocen."""
    differences, missing = [], []
    for field in DIMENSIONS:
        left, right = getattr(kalshi, field), getattr(sportsbook, field)
        if left is None or right is None:
            missing.append(field)
        elif left != right:
            differences.append(field)
    blockers = []
    if not kalshi.source_url or not sportsbook.source_url:
        blockers.append("RULE_SOURCE_MISSING")
    if not quote_jurisdiction or quote_jurisdiction != sportsbook.jurisdiction:
        blockers.append("QUOTE_RULE_JURISDICTION_UNVERIFIED")
    scope_missing = [
        f
        for f in ("league", "phase", "market_period")
        if getattr(kalshi, f) is None or getattr(sportsbook, f) is None
    ]
    scope_differences = [
        f
        for f in ("league", "phase", "market_period")
        if f not in scope_missing and getattr(kalshi, f) != getattr(sportsbook, f)
    ]
    if scope_missing:
        blockers.append("RULE_SCOPE_INCOMPLETE")
    if scope_differences:
        blockers.append("RULE_SCOPE_MISMATCH")
    if missing:
        blockers.append("RULE_DIMENSIONS_INCOMPLETE")
    if differences:
        blockers.append("SETTLEMENT_PAYOFFS_DIFFER")
    return {
        "dimensions_compared": len(DIMENSIONS),
        "differences": differences,
        "missing_dimensions": missing,
        "scope_differences": scope_differences,
        "missing_scope": scope_missing,
        "blockers": blockers,
        "review_status": "DIFFERENT"
        if differences
        else "INCOMPLETE"
        if blockers
        else "DIMENSIONS_MATCH",
        "full_contract_equivalence_verified": False,
        "fair_or_signal_authorized": False,
        "execution_authorized": False,
    }


# Fuentes descargadas y fijadas por hash el 10-oct-2026. El feed 'us' de Odds
# no vincula cada cuota a un estado ni a una versión contractual. Las fichas
# siguientes sólo cubren moneyline 2-way de partido completo en temporada regular.
FANATICS_MA = RuleProfile(
    source_url="https://sportsbook.fanatics.com/legal/ma/house-rules/",
    jurisdiction="MA",
    overtime="included",
    draw="refund_stake",
    postponement="starts_in_nfl_week_thursday_wednesday",
    cancellation="refund_stake",  # Cancelado sin reprogramación; no incluye suspensión.
    # El texto refiere a 'indicated time' sin precisar allí el mínimo: no inventar 55.
    suspension=None,
    forfeit_before_start="refund_stake",
    venue_reversal="refund_stake",
    result_corrections="official_result_24h_with_fallback_resettlement_exceptions",
    replay_from_start="refund_stake",
    league="nfl",
    phase="regular_season",
    market_period="full_game_2way",
    source_sha256="8b139f3c0796bd180cb2dc7999b02db104e1085a38b33dc1d094d5667c77101c",
)
FANATICS_MA_NCAAF = replace(
    FANATICS_MA, league="ncaaf", postponement="starts_in_ncaa_week_tuesday_monday"
)

# Ayuda oficial de DraftKings: confirma el push en 2-way moneyline, pero no se
# completa overtime/postergación/jurisdicción desde una página general.
DRAFTKINGS_MONEYLINE_HELP = RuleProfile(
    source_url="https://help.draftkings.com/hc/en-us/articles/4405230614547-What-is-a-Moneyline-bet-US",
    jurisdiction="US_UNSPECIFIED",
    overtime=None,
    draw="refund_stake",
    postponement=None,
    cancellation=None,
)

# Versión NJ fechada 31-mar-2026. La semana NFL define la referencia del kickoff,
# NO un requisito adicional de terminar dentro de la semana. El texto no define
# esa referencia para NCAA: su ventana queda desconocida, no se hereda la de NFL.
FANDUEL_NJ = RuleProfile(
    source_url="https://d38ayms4az88sz.cloudfront.net/SB/NJ/2026-03-31T12-47-12.html",
    jurisdiction="NJ",
    overtime="included",
    draw="refund_stake",
    postponement="resume_or_reschedule_60h_from_current_nfl_schedule_kickoff",
    cancellation="refund_stake",
    suspension="before_10min_fourth_quarter_incomplete_24h_refund_unless_determined",
    forfeit_before_start="refund_stake",
    venue_reversal=None,  # Su regla habla de estadio/país, no de toda reversión nominal.
    result_corrections=None,
    league="nfl",
    phase="regular_season",
    market_period="full_game_2way",
    source_sha256="50c5de80caf8eb739c975ec1d9cc695a577e3227b4c3eb5194c46ff1259c5408",
)
FANDUEL_NJ_NCAAF = replace(FANDUEL_NJ, league="ncaaf", postponement=None)

SPORTSBOOK_PROFILES = {
    ("fanatics", "nfl"): FANATICS_MA,
    ("fanatics", "ncaaf"): FANATICS_MA_NCAAF,
    ("fanduel", "nfl"): FANDUEL_NJ,
    ("fanduel", "ncaaf"): FANDUEL_NJ_NCAAF,
    ("draftkings", "nfl"): DRAFTKINGS_MONEYLINE_HELP,
    ("draftkings", "ncaaf"): DRAFTKINGS_MONEYLINE_HELP,
}

# Revisión explícita del texto EXACTO capturado en Demo el 10-oct-2026. Nunca se
# generaliza a otra fecha, equipo, serie, liga o nueva versión del contrato.
REVIEWED_RULE_HASHES = {
    "KXNFLGAME-26OCT11PHIJAC-JAC": "aa685a5417fec58f84afda7297f7e72d5df702a705e84b3b40bbbedbc27f125a",
    "KXNFLGAME-26OCT11PHIJAC-PHI": "40c5059f8a43ac50a168a8a16ca1842cffd6b1b7235a19ea7b42a5e0e245a97d",
    "KXNCAAFGAME-26OCT10UGAALA-ALA": "f704842d6fb736ac5faf3b7faa330d5a2477a6c5019d7ae2b52788569e87d0d2",
    "KXNCAAFGAME-26OCT10UGAALA-UGA": "396db2be6c94c97b41f202c71af68cc38c2a0d22b0277c9777f78fcb96f3907b",
    "KXNCAAFGAME-26OCT10STANND-ND": "2a6a1d57260612bf2a9838b6996119efd2354459d4d8456aa9a430b1a9b2c4b8",
    "KXNCAAFGAME-26OCT10STANND-STAN": "baa933f1bcc449d550ffa4cfd75c3d05655bcd16cbc7c900d4db50e3b27e18b0",
}


def review_capture_rules(payload, odds):
    """Explica rechazos de capturas validadas; no publica fair ni certificaciones.

    El feed regional de Odds no vincula cada cuota a una jurisdicción/versionado
    de reglas. Incluso las casas con fichas revisadas conservan ese bloqueo.
    """
    event = payload["event"]["event_ticker"]
    markets = payload["markets"]
    expected = {t for t in REVIEWED_RULE_HASHES if t.rsplit("-", 1)[0] == event}
    tickers = [m["ticker"] for m in markets]
    reviewed = len(tickers) == 2 and len(set(tickers)) == 2 and set(tickers) == expected
    hashes = {}
    for market in markets:
        rules = market["rules_primary"] + "\n" + market["rules_secondary"]
        hashes[market["ticker"]] = hashlib.sha256(rules.encode()).hexdigest()
        reviewed &= hashes[market["ticker"]] == REVIEWED_RULE_HASHES.get(market["ticker"])
    league = "nfl" if event.startswith("KXNFLGAME-") else "ncaaf"
    # Interpretación del documento de referencia ACTUAL junto con los seis textos
    # allowlisted. No prueba que el PDF tuviera ese hash en la captura anterior.
    # En estos pares completos no hay strike Tie. No se extiende a playoffs/3-way.
    kalshi = RuleProfile(
        source_url=KALSHI_TERMS_URL,
        jurisdiction="DEMO_US",
        overtime="included",
        draw="split_50",
        postponement="starts_within_48_hours",
        cancellation="fair_price",
        suspension="48h_55min_or_official_final_with_determined_outcome_exceptions",
        forfeit_before_start="fair_price",
        venue_reversal="fair_price",
        result_corrections="first_official_result_corrections_before_expiration_only",
        replay_from_start=None,
        league=league,
        phase="regular_season",
        market_period="full_game_2way",
        source_sha256=KALSHI_TERMS_SHA256,
    )
    reports = []
    blockers = {"CONTRACT_TERMS_VERSION_AT_CAPTURE_UNVERIFIED", "QUOTE_RULE_VERSION_UNVERIFIED"}
    if not reviewed:
        blockers.add("KALSHI_RULE_TEXT_NOT_REVIEWED")
    seen = set()
    for book in odds["bookmakers"]:
        key = book["key"]
        if not isinstance(key, str) or not key or key in seen:
            raise ValueError("BOOKMAKER_ID_INVALID_OR_DUPLICATE")
        seen.add(key)
        profile = SPORTSBOOK_PROFILES.get((key, league))
        if profile is None or not reviewed:
            reasons = ["SPORTSBOOK_RULE_PROFILE_NOT_REVIEWED"] if profile is None else []
            if not reviewed:
                reasons.append("KALSHI_RULE_TEXT_NOT_REVIEWED")
            result = {"review_status": "UNREVIEWED", "blockers": reasons}
        else:
            result = compare_rules(kalshi, profile, quote_jurisdiction=None)
        reports.append(
            {
                "bookmaker": key,
                "source_url": profile.source_url if profile else None,
                "reviewed_jurisdiction": profile.jurisdiction if profile else None,
                "reviewed_league": profile.league if profile else None,
                "source_sha256": profile.source_sha256 if profile else None,
                "quote_jurisdiction": None,
                **result,
                "fair_or_signal_authorized": False,
                "execution_authorized": False,
            }
        )
        blockers.update(result["blockers"])
    if not reports:
        blockers.add("NO_SPORTSBOOK_QUOTES")
    return {
        "reviewed_at": "2026-10-10",
        "kalshi_exact_text_reviewed": reviewed,
        "reference_terms_url": KALSHI_TERMS_URL,
        "reference_terms_sha256": KALSHI_TERMS_SHA256,
        "reference_terms_observed_at": "2026-10-10T19:20:57Z",
        "reference_terms_version_at_capture_verified": False,
        "rules_sha256_by_ticker": hashes,
        "bookmakers": reports,
        "blockers": sorted(blockers),
        "scope": "NINE_DIMENSIONS_CURRENT_REFERENCE_NOT_FULL_CONTRACT_CERTIFICATION",
        "full_contract_equivalence_verified": False,
        "fair_or_signal_authorized": False,
        "execution_authorized": False,
    }
