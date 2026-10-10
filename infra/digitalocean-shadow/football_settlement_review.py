"""Auditoría pura de reglas revisadas: no convierte cuotas en decisiones.

Las fichas son evidencia manual con fuente/jurisdicción; no hay extracción LLM
ni reglas inferidas del nombre de una casa. Una coincidencia parcial no autoriza
fair, señal ni ejecución. No modela probabilidad de empate o cancelación.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class RuleProfile:
    source_url: str
    jurisdiction: str
    overtime: str | None
    draw: str | None
    postponement: str | None
    cancellation: str | None


def compare_rules(kalshi: RuleProfile, sportsbook: RuleProfile, *, quote_jurisdiction: str | None):
    """Compara dimensiones; UNKNOWN se conserva aun si ambos lados lo desconocen."""
    differences, missing = [], []
    for field in ("overtime", "draw", "postponement", "cancellation"):
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
    if missing:
        blockers.append("RULE_DIMENSIONS_INCOMPLETE")
    if differences:
        blockers.append("SETTLEMENT_PAYOFFS_DIFFER")
    return {
        "dimensions_compared": 4,
        "differences": differences,
        "missing_dimensions": missing,
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


# Fuente oficial revisada el 10-oct-2026. Es la versión MA; el feed 'us' de Odds
# no afirma que estas sean las reglas/jurisdicción de cada quote que entrega.
FANATICS_MA = RuleProfile(
    source_url="https://sportsbook.fanatics.com/legal/ma/house-rules/",
    jurisdiction="MA",
    overtime="included",
    draw="refund_stake",
    postponement="same_scheduling_week",
    cancellation=None,
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

# Versión NJ fechada 31-mar-2026. La ventana no convierte las reglas de suspensión,
# sede o duración mínima en equivalentes; éstas siguen fuera de estas 4 dimensiones.
FANDUEL_NJ = RuleProfile(
    source_url="https://d38ayms4az88sz.cloudfront.net/SB/NJ/2026-03-31T12-47-12.html",
    jurisdiction="NJ",
    overtime="included",
    draw="refund_stake",
    postponement="resumed_or_rescheduled_within_60_hours_same_nfl_week",
    cancellation=None,
)

SPORTSBOOK_PROFILES = {
    "fanatics": FANATICS_MA,
    "fanduel": FANDUEL_NJ,
    "draftkings": DRAFTKINGS_MONEYLINE_HELP,
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
    nfl = event.startswith("KXNFLGAME-")
    kalshi = RuleProfile(
        source_url="https://external-api.demo.kalshi.co/trade-api/v2/events/" + event,
        jurisdiction="DEMO_US",
        overtime=None,  # No se deduce de 'official final result'.
        draw="split_50" if nfl else None,
        postponement="starts_within_48_hours",
        cancellation=None if nfl else "fair_price",
    )
    reports = []
    blockers = set() if reviewed else {"KALSHI_RULE_TEXT_NOT_REVIEWED"}
    seen = set()
    for book in odds["bookmakers"]:
        key = book["key"]
        if not isinstance(key, str) or not key or key in seen:
            raise ValueError("BOOKMAKER_ID_INVALID_OR_DUPLICATE")
        seen.add(key)
        profile = SPORTSBOOK_PROFILES.get(key)
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
        "rules_sha256_by_ticker": hashes,
        "bookmakers": reports,
        "blockers": sorted(blockers),
        "scope": "FOUR_DIMENSIONS_ONLY_NOT_FULL_CONTRACT_CERTIFICATION",
        "full_contract_equivalence_verified": False,
        "fair_or_signal_authorized": False,
        "execution_authorized": False,
    }
