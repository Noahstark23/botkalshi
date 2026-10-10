"""Contrato de datos NFL/NCAAF para replay local, sin señales ni ejecución.

Registro intencionalmente acotado a pares observados en Demo. No hay
fuzzy matching ni alias globales por ciudad. Identidad, fecha y tarifa reconocidas
NO prueban equivalencia de reglas con una casa: esa prueba permanece explícita.
Fuentes de nombres: nfl.com/teams, seminoles.com/sports/football,
gocards.com/sports/football. No se importan componentes operativos del bot.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

SERIES = {"KXNFLGAME": "americanfootball_nfl", "KXNCAAFGAME": "americanfootball_ncaaf"}
ROSTERS = {
    "americanfootball_nfl": {
        "PHI": ("Philadelphia", "Philadelphia Eagles"),
        "JAC": ("Jacksonville", "Jacksonville Jaguars"),
    },
    "americanfootball_ncaaf": {
        "FSU": ("Florida St.", "Florida State", "Florida State Seminoles"),
        "LOU": ("Louisville", "Louisville Cardinals"),
        "UGA": ("Georgia", "Georgia Bulldogs"),
        "ALA": ("Alabama", "Alabama Crimson Tide"),
        "STAN": ("Stanford", "Stanford Cardinal"),
        "ND": ("Notre Dame", "Notre Dame Fighting Irish"),
    },
}
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


class ContractError(ValueError):
    """Datos insuficientes, desconocidos o contradictorios."""


def _time(raw):
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise ContractError("TIME_INVALID") from exc
    if value.tzinfo is None:
        raise ContractError("TIME_NAIVE")
    return value.astimezone(UTC)


def _code(sport, name):
    if not isinstance(name, str) or not name.isascii() or len(name) > 100:
        raise ContractError("NAME_INVALID")
    key = " ".join(name.casefold().split())
    matches = [
        code for code, names in ROSTERS[sport].items() if key in {n.casefold() for n in names}
    ]
    if len(matches) != 1:
        raise ContractError("NAME_UNKNOWN_OR_AMBIGUOUS")
    return matches[0]


def _multiplier(raw):
    if type(raw) not in (str, int, float) or len(str(raw)) > 16:
        raise ContractError("FEE_MULTIPLIER_INVALID")
    try:
        value = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ContractError("FEE_MULTIPLIER_INVALID") from exc
    if not value.is_finite() or not 0 < value <= 100:
        raise ContractError("FEE_MULTIPLIER_INVALID")
    return str(value)


def _milestone(payload, ticker, prefix, team_ids):
    """Inicio documentado, no occurrence/expiration ni una resta fija de horas."""
    if not isinstance(payload, dict) or payload.get("cursor") != "":
        raise ContractError("MILESTONES_MISSING_OR_INCOMPLETE")
    rows = payload.get("milestones")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ContractError("MILESTONES_CARDINALITY_INVALID")
    candidates = []
    for row in rows:
        if not isinstance(row, dict):
            raise ContractError("MILESTONE_SHAPE_INVALID")
        if row.get("type") == "football_game" and ticker in row.get("primary_event_tickers", []):
            candidates.append(row)
    if len(candidates) != 1:
        raise ContractError("MILESTONE_NOT_UNIQUE")
    row = candidates[0]
    details = row.get("details")
    if (
        not isinstance(details, dict)
        or row.get("category") != "Sports"
        or ticker not in row.get("related_event_tickers", [])
        or details.get("main_game_event_ticker") != ticker
        or details.get("league") != {"KXNFLGAME": "NFL", "KXNCAAFGAME": "NCAAFB"}[prefix]
        or details.get("status") != "scheduled"
        or not isinstance(row.get("id"), str)
        or not row["id"]
    ):
        raise ContractError("MILESTONE_IDENTITY_OR_STATUS_INVALID")
    if set(team_ids) != {details.get("home_team_id"), details.get("away_team_id")}:
        raise ContractError("MILESTONE_TEAM_ID_CONFLICT")
    _time(row.get("last_updated_ts"))
    return row


def inspect_event(payload, series, *, milestones=None):
    """Valida captura pública; no presume equivalencia de liquidación con Odds."""
    if not isinstance(payload, dict) or not isinstance(payload.get("event"), dict):
        raise ContractError("EVENT_SHAPE_INVALID")
    event = payload["event"]
    prefix = event.get("series_ticker")
    if prefix not in SERIES or series.get("ticker") != prefix:
        raise ContractError("SERIES_MISMATCH")
    sport = SERIES[prefix]
    ticker = event.get("event_ticker")
    if not isinstance(ticker, str) or not ticker.startswith(prefix + "-"):
        raise ContractError("EVENT_ID_INVALID")
    markets = payload.get("markets")
    if not isinstance(markets, list) or len(markets) != 2:
        raise ContractError("MARKET_CARDINALITY_INVALID")
    codes, team_ids, rules_hashes, outcomes, occurrences = [], [], [], {}, {}
    for market in markets:
        if (
            not isinstance(market, dict)
            or market.get("event_ticker") != ticker
            or market.get("status") != "active"
        ):
            raise ContractError("MARKET_IDENTITY_OR_STATUS_INVALID")
        code = _code(sport, market.get("yes_sub_title"))
        if market.get("ticker") != ticker + "-" + code:
            raise ContractError("TICKER_TEAM_CONTRADICTION")
        codes.append(code)
        outcomes[market["ticker"]] = ROSTERS[sport][code][-1]
        strike = market.get("custom_strike")
        if (
            not isinstance(strike, dict)
            or not isinstance(strike.get("football_team"), str)
            or not strike["football_team"]
        ):
            raise ContractError("TEAM_ID_MISSING")
        team_ids.append(strike["football_team"])
        occurrences[market["ticker"]] = market.get("occurrence_datetime")
        if not all(
            isinstance(market.get(k), str) and market[k]
            for k in ("rules_primary", "rules_secondary")
        ):
            raise ContractError("SETTLEMENT_RULES_MISSING")
        rules = market["rules_primary"] + "\n" + market["rules_secondary"]
        rules_hashes.append(hashlib.sha256(rules.encode()).hexdigest())
    if len(set(codes)) != 2 or len(set(team_ids)) != 2:
        raise ContractError("DUPLICATE_TEAM")
    milestone = _milestone(milestones, ticker, prefix, team_ids)
    kickoff = _time(milestone["start_date"])
    local = kickoff.astimezone(ZoneInfo("America/New_York"))
    stamp = f"{local.year % 100:02d}{MONTHS[local.month - 1]}{local.day:02d}"
    suffix = ticker[len(prefix) + 1 :]
    if suffix not in {stamp + codes[0] + codes[1], stamp + codes[1] + codes[0]}:
        raise ContractError("EVENT_DATE_OR_PARTICIPANTS_CONFLICT")
    fee_type, multiplier, source = series.get("fee_type"), series.get("fee_multiplier"), "series"
    override_type, override_multiplier = (
        event.get("fee_type_override"),
        event.get("fee_multiplier_override"),
    )
    # Validar también la base: un override no oculta una entrada rota.
    if fee_type not in {"quadratic", "quadratic_with_maker_fees"}:
        raise ContractError("FEE_TYPE_UNSUPPORTED")
    _multiplier(multiplier)
    if (override_type is None) != (override_multiplier is None):
        raise ContractError("FEE_OVERRIDE_PARTIAL")
    if override_type is not None:
        fee_type, multiplier, source = override_type, override_multiplier, "event_override"
    if fee_type not in {"quadratic", "quadratic_with_maker_fees"}:
        raise ContractError("FEE_TYPE_UNSUPPORTED")
    return {
        "mode": "SIMULATION_ONLY",
        "execution_authorized": False,
        "event_ticker": ticker,
        "sport_key": sport,
        "participant_codes": sorted(codes),
        "canonical_outcomes": outcomes,
        "kickoff": kickoff.isoformat(),
        "kickoff_source": "milestone.start_date",
        "milestone_id": milestone["id"],
        "milestone_updated_at": milestone["last_updated_ts"],
        "market_occurrence_datetimes": occurrences,
        "participant_team_ids": sorted(team_ids),
        "fee_type": fee_type,
        "fee_multiplier": _multiplier(multiplier),
        "fee_source": source,
        "rules_sha256": rules_hashes,
        "event_equivalence_verified": False,
        "scope": "CURATED_PARTICIPANTS_NOT_FULL_LEAGUE_COVERAGE",
    }


def compare_odds(contract, odds, *, observed_at, now):
    """Compara identidad y horario, conserva evidencia de antigüedad y reglas ausentes."""
    if odds.get("sport_key") != contract["sport_key"]:
        raise ContractError("LEAGUE_MISMATCH")
    sport = contract["sport_key"]
    codes = sorted([_code(sport, odds.get(k)) for k in ("home_team", "away_team")])
    if codes != contract["participant_codes"]:
        raise ContractError("PARTICIPANTS_MISMATCH")
    if _time(odds.get("commence_time")) != _time(contract["kickoff"]):
        raise ContractError("KICKOFF_MISMATCH")
    age = (_time(now) - _time(observed_at)).total_seconds()
    reasons = ["SPORTSBOOK_SETTLEMENT_RULES_NOT_PROVIDED"]
    if not 0 <= age <= 600:
        reasons.append("ODDS_SNAPSHOT_STALE_OR_FUTURE")
    if _time(contract["kickoff"]) <= _time(now):
        reasons.append("EVENT_ALREADY_STARTED")
    return {
        "participants_and_kickoff_match": True,
        "odds_age_seconds": age,
        "comparison_status": "IDENTITY_MATCH_RULE_EQUIVALENCE_UNVERIFIED",
        "blockers": reasons,
        "fair_or_signal_authorized": False,
        "execution_authorized": False,
    }


def inspect_odds_books(contract, odds, *, observed_at, now):
    """Antigüedad de cada mercado h2h, no sólo del archivo que lo contiene.

    `usable_quote_data` significa forma/precio/reloj válidos; jamás equivalencia de
    reglas. No se calcula consenso ni se publican probabilidades con esta función.
    """
    identity = compare_odds(contract, odds, observed_at=observed_at, now=now)
    books = odds.get("bookmakers")
    if not isinstance(books, list) or len(books) > 100:
        raise ContractError("BOOKMAKERS_INVALID")
    seen, reports = set(), []
    for book in books:
        if not isinstance(book, dict) or not isinstance(book.get("key"), str) or not book["key"]:
            raise ContractError("BOOKMAKER_ID_INVALID")
        key = book["key"]
        if key in seen:
            raise ContractError("DUPLICATE_BOOKMAKER")
        seen.add(key)
        blockers = [
            r for r in identity["blockers"] if r != "SPORTSBOOK_SETTLEMENT_RULES_NOT_PROVIDED"
        ]
        age = None
        try:
            markets = book.get("markets")
            if not isinstance(markets, list) or len(markets) != 1 or markets[0].get("key") != "h2h":
                raise ContractError("UNEXPECTED_MARKET_SCOPE")
            market = markets[0]
            age = (_time(now) - _time(market.get("last_update"))).total_seconds()
            if not 0 <= age <= 600:
                blockers.append("BOOK_ODDS_STALE_OR_FUTURE")
            outcomes = market.get("outcomes")
            if not isinstance(outcomes, list) or len(outcomes) != 2:
                raise ContractError("ODDS_OUTCOME_CARDINALITY_INVALID")
            codes = []
            for outcome in outcomes:
                if not isinstance(outcome, dict):
                    raise ContractError("ODDS_OUTCOME_INVALID")
                codes.append(_code(contract["sport_key"], outcome.get("name")))
                price = outcome.get("price")
                if type(price) not in (float, int, str) or len(str(price)) > 24:
                    raise ContractError("ODDS_PRICE_INVALID")
                value = Decimal(str(price))
                if not value.is_finite() or not 1 < value <= 10000:
                    raise ContractError("ODDS_PRICE_INVALID")
            if sorted(codes) != contract["participant_codes"]:
                raise ContractError("ODDS_OUTCOME_TEAM_CONFLICT")
        except ContractError as exc:
            blockers.append(str(exc))
        except (InvalidOperation, AttributeError, TypeError):
            blockers.append("ODDS_SHAPE_INVALID")
        reports.append(
            {
                "bookmaker": key,
                "age_seconds": age,
                "blockers": blockers,
                "usable_quote_data": not blockers,
                "fair_or_signal_authorized": False,
            }
        )
    return {
        "books": reports,
        "usable_quote_data_count": sum(r["usable_quote_data"] for r in reports),
        "execution_authorized": False,
        "rule_equivalence_verified": False,
    }
