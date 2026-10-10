"""Une capturas archivadas de Demo/Odds con el diario incremental de simulación.

Sólo lee archivos regulares, verifica hashes y conserva el reloj original. No
consulta APIs ni refresca timestamps. Los hashes detectan alteraciones respecto
del manifiesto; no son firmas del proveedor. El consumidor vuelve a validar los
contratos y registra los rechazos sin convertir estas observaciones en señales.
"""

import hashlib
import json
import re
from pathlib import Path

import football_replay_contract as football
from paper_stream import StreamError, read_json


def load_capture(directory: Path) -> list[dict]:
    directory = Path(directory)
    if directory.is_symlink():
        raise StreamError("CAPTURE_DIRECTORY_SYMLINK")
    capture_raw = read_json(directory / "captura.json")
    milestones_raw = read_json(directory / "milestones-captura.json")
    capture, milestones = json.loads(capture_raw), json.loads(milestones_raw)
    if (
        capture.get("status") != "COMPLETE"
        or capture.get("orders") != 0
        or capture.get("production_accessed") is not False
        or milestones.get("scope") != "DEMO_PUBLIC_GET_ONLY"
        or milestones.get("orders") != 0
    ):
        raise StreamError("CAPTURE_SCOPE_INVALID")
    requests, milestone_requests = capture["requests"], milestones["requests"]
    if (
        not isinstance(requests, list)
        or not 1 <= len(requests) <= 20
        or not isinstance(milestone_requests, list)
        or not 1 <= len(milestone_requests) <= 20
    ):
        raise StreamError("CAPTURE_COUNT_INVALID")
    all_records = requests + milestone_requests
    seen, values, source_hashes = (
        set(),
        {},
        [hashlib.sha256(raw).hexdigest() for raw in (capture_raw, milestones_raw)],
    )
    for record in all_records:
        name = record["file"]
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+\.json", name)
            or name in seen
            or record.get("state") != "CAPTURED"
            or record.get("status", record.get("http_status")) != 200
        ):
            raise StreamError("CAPTURE_RECORD_INVALID")
        seen.add(name)
        raw = read_json(directory / name)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != record["sha256"]:
            raise StreamError("CAPTURE_HASH_MISMATCH")
        source_hashes.append(digest)
        values[name] = json.loads(raw)
    as_of = max(football._time(row["at"]) for row in all_records).isoformat()
    series, events, odds, partial = {}, {}, {}, {}
    for record in requests:
        data = values[record["file"]]
        provider, path = record["provider"], record["path"]
        if provider == "demo" and path.startswith("/series/"):
            key = data["series"]["ticker"]
            if key in series or path != f"/series/{key}":
                raise StreamError("SERIES_DUPLICATE_OR_PATH_CONFLICT")
            series[key] = data["series"]
        elif provider == "demo" and path == "/events":
            if not isinstance(data.get("cursor"), str):
                raise StreamError("EVENT_PAGINATION_UNKNOWN")
            for event in data["events"]:
                key = event["event_ticker"]
                if key in events:
                    raise StreamError("EVENT_DUPLICATE")
                events[key], partial[key] = event, bool(data["cursor"])
        elif provider == "odds" and path.endswith("/odds"):
            sport = path.split("/")[3]
            if sport in odds:
                raise StreamError("ODDS_DUPLICATE")
            odds[sport] = data, record
        elif provider != "odds" or path != "/v4/sports":
            raise StreamError("SOURCE_ROUTE_UNEXPECTED")
    output, tickers = [], set()
    for record in milestone_requests:
        ticker = record["event_ticker"]
        if ticker in tickers or record["path"] != "/milestones":
            raise StreamError("MILESTONE_DUPLICATE_OR_PATH_INVALID")
        tickers.add(ticker)
        event = events[ticker]
        prefix = event["series_ticker"]
        payload = {"event": event, "markets": event["markets"]}
        milestone = values[record["file"]]
        contract = football.inspect_event(payload, series[prefix], milestones=milestone)
        items, odds_record = odds[contract["sport_key"]]
        matches = [
            item
            for item in items
            if {item["home_team"], item["away_team"]}
            == set(contract["canonical_outcomes"].values())
        ]
        if len(matches) != 1:
            raise StreamError("ODDS_EVENT_NOT_UNIQUE")
        output.append(
            {
                "type": "football_snapshot",
                "payload": payload,
                "series": series[prefix],
                "milestones": milestone,
                "odds": matches[0],
                "observed_at": odds_record["at"],
                "evaluated_at": as_of,
                "source_hashes": sorted(source_hashes),
                "partial_universe": partial[ticker],
            }
        )
    return output
