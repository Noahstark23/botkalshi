"""Lectura del formato research real con datos inventados; ningún HTTP o secreto."""

from __future__ import annotations

import copy
import json
import os
import socket
import stat
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.clients.odds_snapshot import OddsSnapshotClient, OddsSnapshotError
from src.strategies.motor_2_consensus.sources import LiveOddsSource

NOW = datetime(2026, 10, 8, 18, tzinfo=UTC)
FILES = {
    "baseball_mlb": "odds-latest.json",
    "americanfootball_nfl": "odds-americanfootball_nfl-latest.json",
    "americanfootball_ncaaf": "odds-americanfootball_ncaaf-latest.json",
    "basketball_nba": "odds-basketball_nba-latest.json",
    "icehockey_nhl": "odds-icehockey_nhl-latest.json",
}


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(os, "environ", {})

    def deny(*args, **kwargs):
        raise AssertionError("ODDS_SNAPSHOT_NO_NETWORK_OR_SETTINGS")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr("httpx.AsyncClient", deny)
    monkeypatch.setattr("src.clients.odds_api.OddsAPIClient.__init__", deny)
    monkeypatch.setattr("src.clients.odds_api.get_settings", deny)


def fixture(sport="baseball_mlb"):
    # Shape exacto de sanitize_odds_event + m5_inputs._odds_path, sin schema inventado.
    event = {
        "id": "fictional-event",
        "sport_key": sport,
        "commence_time": (NOW + timedelta(hours=3)).isoformat(),
        "home_team": "Home Team",
        "away_team": "Away Team",
        "bookmakers": [
            {
                "key": key,
                "last_update": (NOW - timedelta(seconds=age)).isoformat(),
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Home Team", "price": 1.9, "point": None},
                            {"name": "Away Team", "price": 1.9, "point": None},
                        ],
                    }
                ],
            }
            for key, age in (("draftkings", 90), ("fanduel", 80), ("betmgm", 70))
        ],
    }
    return {
        "fetched_at": (NOW - timedelta(seconds=60)).isoformat(),
        "sport": sport,
        "params": {"regions": "us", "markets": "h2h", "oddsFormat": "decimal", "dateFormat": "iso"},
        "events": [event],
    }


def write(directory, doc, sport="baseball_mlb"):
    path = directory / FILES[sport]
    path.write_text(json.dumps(doc))
    return path


@pytest.fixture
def snapshot(tmp_path):
    directory = tmp_path.resolve() / "m5"
    directory.mkdir()
    doc = fixture()
    path = write(directory, doc)
    return directory, doc, path


def client(directory, **kwargs):
    return OddsSnapshotClient(directory, clock=lambda: NOW, **kwargs)


@pytest.mark.parametrize("sport", FILES)
async def test_exact_export_format_and_all_sport_paths_preserve_original_dates(snapshot, sport):
    directory, _, _ = snapshot
    doc = fixture(sport)
    original = copy.deepcopy(doc)
    path = write(directory, doc, sport)
    original_bytes = path.read_bytes()
    async with client(directory) as reader:
        events = await reader.get_odds(sport)
    assert len(events) == 1
    event = events[0]
    assert event.id == "fictional-event" and event.sport_key == sport
    assert event.commence_time == NOW + timedelta(hours=3)
    assert [book.last_update for book in event.bookmakers] == [
        NOW - timedelta(seconds=age) for age in (90, 80, 70)
    ]
    assert all(book.markets[0].outcomes[0].price == 1.9 for book in event.bookmakers)
    assert doc == original and path.read_bytes() == original_bytes


async def test_factory_is_compatible_with_live_odds_source_without_http(snapshot):
    directory, _, _ = snapshot
    write(directory, fixture("basketball_nba"), "basketball_nba")
    source = LiveOddsSource(
        ["baseball_mlb", "basketball_nba"], regions="us", client_factory=lambda: client(directory)
    )
    events = await source.fetch()
    assert [event.sport_key for event in events] == ["baseball_mlb", "basketball_nba"]
    # is_live pertenece a la integración que eligió el caller, no al lector de fixtures.
    assert not hasattr(client(directory), "is_live")


async def test_missing_sport_or_invalid_cache_never_falls_back_to_http(snapshot):
    directory, _, path = snapshot
    path.write_text("not json SECRET_FIXTURE")
    source = LiveOddsSource(
        ["baseball_mlb", "basketball_nba"], regions="us", client_factory=lambda: client(directory)
    )
    assert await source.fetch() == []


@pytest.mark.parametrize("age", [-1, 601])
async def test_future_or_expired_snapshot_is_rejected_even_if_books_are_fresh(snapshot, age):
    directory, doc, _ = snapshot
    doc["fetched_at"] = (NOW - timedelta(seconds=age)).isoformat()
    write(directory, doc)
    with pytest.raises(OddsSnapshotError, match="STALE_OR_FUTURE"):
        await client(directory).get_odds("baseball_mlb")


@pytest.mark.parametrize("ttl", [600, 660])
async def test_max_age_boundary_and_clock_are_checked_on_every_read(snapshot, ttl):
    directory, doc, _ = snapshot
    doc["fetched_at"] = (NOW - timedelta(seconds=ttl)).isoformat()
    for book in doc["events"][0]["bookmakers"]:
        book["last_update"] = doc["fetched_at"]
    write(directory, doc)
    now = [NOW]
    reader = OddsSnapshotClient(directory, max_age_seconds=ttl, clock=lambda: now[0])
    assert len(await reader.get_odds("baseball_mlb")) == 1
    now[0] += timedelta(microseconds=1)
    with pytest.raises(OddsSnapshotError, match="STALE_OR_FUTURE"):
        await reader.get_odds("baseball_mlb")


@pytest.mark.parametrize("update", [None, "bad", "2026-10-08T17:59:00", -1, 20, 601])
async def test_bad_book_timestamp_is_discarded_without_refreshing_other_books(snapshot, update):
    directory, doc, _ = snapshot
    if isinstance(update, int):
        # 20s también es posterior a fetched_at: línea imposible en su propio archivo.
        update = (NOW - timedelta(seconds=update)).isoformat()
    doc["events"][0]["bookmakers"][0]["last_update"] = update
    write(directory, doc)
    events = await client(directory).get_odds("baseball_mlb")
    assert [book.key for book in events[0].bookmakers] == ["fanduel", "betmgm"]
    assert events[0].bookmakers[0].last_update == NOW - timedelta(seconds=80)


async def test_event_with_no_usable_books_is_not_returned(snapshot):
    directory, doc, _ = snapshot
    for book in doc["events"][0]["bookmakers"]:
        book.pop("last_update")
    write(directory, doc)
    assert await client(directory).get_odds("baseball_mlb") == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("sport", "basketball_nba"),
        ("events", {}),
        ("fetched_at", None),
        ("fetched_at", "2026-10-08T17:59:00"),
        ("params", {"regions": "us"}),
        (
            "params",
            {
                "regions": "us",
                "markets": "h2h,totals",
                "oddsFormat": "decimal",
                "dateFormat": "iso",
            },
        ),
    ],
)
async def test_invalid_document_identity_params_and_shape_are_rejected(snapshot, field, value):
    directory, doc, _ = snapshot
    doc[field] = value
    write(directory, doc)
    with pytest.raises(OddsSnapshotError):
        await client(directory).get_odds("baseball_mlb")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.update(sport_key="basketball_nba"),
        lambda e: e.update(id=12),
        lambda e: e.update(home_team="Away Team"),
        lambda e: e.update(commence_time="2026-10-08T20:00:00"),
        lambda e: e.update(bookmakers={}),
        lambda e: e["bookmakers"].append(copy.deepcopy(e["bookmakers"][0])),
        lambda e: e["bookmakers"][0]["markets"][0].update(key="totals"),
        lambda e: e["bookmakers"][0]["markets"][0]["outcomes"][0].update(name="Wrong Team"),
        lambda e: e["bookmakers"][0]["markets"][0]["outcomes"][0].update(point=1.5),
    ],
)
async def test_malformed_events_fail_closed(snapshot, mutate):
    directory, doc, _ = snapshot
    mutate(doc["events"][0])
    write(directory, doc)
    with pytest.raises(OddsSnapshotError):
        await client(directory).get_odds("baseball_mlb")


@pytest.mark.parametrize("price", [True, "1.9", 1, -5, float("nan"), float("inf"), 10**400])
async def test_prices_do_not_coerce_boolean_string_or_nonfinite_numbers(snapshot, price):
    directory, doc, _ = snapshot
    doc["events"][0]["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = price
    write(directory, doc)
    with pytest.raises(OddsSnapshotError):
        await client(directory).get_odds("baseball_mlb")


@pytest.mark.parametrize(
    "kind", ["empty", "bad_json", "duplicate_key", "duplicate_event", "size", "events", "books"]
)
async def test_inputs_are_bounded_and_unambiguous(snapshot, kind):
    directory, doc, path = snapshot
    kwargs = {}
    if kind == "duplicate_event":
        doc["events"] *= 2
    if kind == "events":
        kwargs["max_events"] = 1
        second = copy.deepcopy(doc["events"][0])
        second["id"] = "second"
        doc["events"].append(second)
    if kind == "books":
        doc["events"][0]["bookmakers"] *= 22
    write(directory, doc)
    if kind == "empty":
        path.write_bytes(b"")
    if kind == "bad_json":
        path.write_bytes(b"\xffSECRET_FIXTURE")
    if kind == "duplicate_key":
        path.write_text('{"sport":"baseball_mlb","sport":"basketball_nba"}')
    if kind == "size":
        kwargs["max_bytes"] = path.stat().st_size - 1
    with pytest.raises(OddsSnapshotError) as caught:
        await client(directory, **kwargs).get_odds("baseball_mlb")
    assert "SECRET_FIXTURE" not in str(caught.value)
    assert str(path) not in str(caught.value)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_age_seconds": 661},
        {"max_age_seconds": True},
        {"max_age_seconds": float("nan")},
        {"max_bytes": 4_000_001},
        {"max_events": 257},
        {"max_events": True},
    ],
)
def test_constructor_cannot_enlarge_resource_or_freshness_limits(snapshot, kwargs):
    with pytest.raises(OddsSnapshotError, match="CONFIG_INVALID"):
        client(snapshot[0], **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sport_key": "../../private"},
        {"sport_key": "soccer_epl"},
        {"regions": "eu,us"},
        {"markets": "totals"},
        {"odds_format": "american"},
    ],
)
async def test_request_cannot_select_another_path_region_or_market(snapshot, kwargs):
    with pytest.raises(OddsSnapshotError, match="REQUEST_UNSUPPORTED"):
        await client(snapshot[0]).get_odds(**{"sport_key": "baseball_mlb", **kwargs})


@pytest.mark.parametrize(
    "kind", ["missing", "symlink", "fifo", "directory", "hardlink", "world_writable"]
)
async def test_never_opens_unsafe_file_types(snapshot, kind):
    directory, _, path = snapshot
    if kind == "hardlink":
        os.link(path, directory / "other")
    elif kind == "world_writable":
        path.chmod(0o666)
    else:
        path.unlink()
        if kind == "symlink":
            path.symlink_to(directory / "missing-target")
        if kind == "fifo":
            os.mkfifo(path)
        if kind == "directory":
            path.mkdir()
    with pytest.raises(OddsSnapshotError):
        await client(directory).get_odds("baseball_mlb")


async def test_intermediate_symlink_and_world_writable_directory_are_rejected(snapshot, tmp_path):
    directory, _, _ = snapshot
    link = tmp_path.resolve() / "alias"
    link.symlink_to(directory, target_is_directory=True)
    with pytest.raises(OddsSnapshotError):
        await client(link).get_odds("baseball_mlb")
    # También rechazar un symlink en un ancestro, no sólo en el último componente.
    (directory / "nested").mkdir()
    with pytest.raises(OddsSnapshotError):
        await client(link / "nested").get_odds("baseball_mlb")
    directory.chmod(0o777)
    with pytest.raises(OddsSnapshotError, match="PATH_UNSAFE"):
        await client(directory).get_odds("baseball_mlb")


@pytest.mark.parametrize("path", ["relative/m5", "/tmp/../m5", "/tmp/\x00"])
def test_unsafe_path_syntax_rejected(path):
    with pytest.raises(OddsSnapshotError, match="PATH_UNSAFE"):
        client(path)


@pytest.mark.parametrize("clock_value", [None, "now", NOW.replace(tzinfo=None)])
async def test_clock_must_be_aware(snapshot, clock_value):
    reader = OddsSnapshotClient(snapshot[0], clock=lambda: clock_value)
    with pytest.raises(OddsSnapshotError, match="CLOCK_INVALID"):
        await reader.get_odds("baseball_mlb")


async def test_modification_during_read_is_rejected(snapshot, monkeypatch):
    original = os.fstat
    reads = []

    def altered(fd):
        info = original(fd)
        if stat.S_ISREG(info.st_mode):
            reads.append(fd)
            if len(reads) == 2:
                return SimpleNamespace(
                    st_size=info.st_size,
                    st_mtime_ns=info.st_mtime_ns + 1,
                    st_ctime_ns=info.st_ctime_ns,
                )
        return info

    monkeypatch.setattr(os, "fstat", altered)
    with pytest.raises(OddsSnapshotError, match="CHANGED_DURING_READ"):
        await client(snapshot[0]).get_odds("baseball_mlb")


async def test_next_read_reloads_atomic_export_without_memory_cache(snapshot):
    directory, doc, path = snapshot
    reader = client(directory)
    assert (await reader.get_odds("baseball_mlb"))[0].id == "fictional-event"
    doc["events"][0]["id"] = "replacement-event"
    temporary = directory / "replacement.json"
    temporary.write_text(json.dumps(doc))
    os.replace(temporary, path)
    assert (await reader.get_odds("baseball_mlb"))[0].id == "replacement-event"


async def test_public_snapshot_metadata_and_events_come_from_one_read(snapshot, monkeypatch):
    directory, _, _ = snapshot
    reader = client(directory)
    original = reader._read
    calls = []

    def tracked(name):
        calls.append(name)
        return original(name)

    monkeypatch.setattr(reader, "_read", tracked)
    result = await reader.read_snapshot("baseball_mlb")
    assert result.fetched_at == NOW - timedelta(seconds=60)
    assert isinstance(result.events, tuple)
    assert result.events[0].bookmakers[0].last_update == NOW - timedelta(seconds=90)
    assert calls == ["odds-latest.json"]
