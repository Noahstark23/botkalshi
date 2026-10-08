"""Exportador público con filesystem temporal; no servicios, claves ni red."""

from __future__ import annotations

import json
import os
import socket
import stat
from datetime import timedelta

import pytest

from scripts import export_shared_odds as export
from src.clients.odds_snapshot import OddsSnapshotClient
from tests.clients.test_odds_snapshot import FILES, NOW, fixture, write


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(os, "environ", {})

    def deny(*args, **kwargs):
        raise AssertionError("EXPORT_NO_NETWORK_SETTINGS_OR_CHOWN")

    for target in ("socket.connect", "socket.connect_ex"):
        monkeypatch.setattr(socket.socket, target.split(".")[1], deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr("httpx.AsyncClient", deny)
    monkeypatch.setattr("src.clients.odds_api.OddsAPIClient.__init__", deny)
    monkeypatch.setattr("src.clients.odds_api.get_settings", deny)
    monkeypatch.setattr(os, "chown", deny)
    monkeypatch.setattr(os, "fchown", deny)


@pytest.fixture
def directories(tmp_path):
    root = tmp_path.resolve()
    source, destination = root / "private-source", root / "public-share"
    source.mkdir(mode=0o700)
    destination.mkdir(mode=0o750)
    destination.chmod(0o750)
    for sport in FILES:
        write(source, fixture(sport), sport).chmod(0o600)
    return source, destination


async def run(directories, **kwargs):
    return await export.export_snapshots(*directories, clock=lambda: NOW, **kwargs)


async def test_only_four_public_snapshots_are_exported_and_readable(directories):
    source, destination = directories
    (source / "simulation-bank.sqlite3").write_bytes(b"DO_NOT_COPY_PRIVATE_DB")
    (source / "odds-api-key").write_text("DO_NOT_COPY_FAKE_KEY")
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    result = await run(directories)
    assert result["status"] == "OK"
    assert set(result["sports"].values()) == {"EXPORTED"}
    assert {p.name for p in destination.iterdir()} == set(FILES.values())
    assert stat.S_IMODE(destination.stat().st_mode) == 0o750
    for sport, filename in FILES.items():
        path = destination / filename
        assert stat.S_IMODE(path.stat().st_mode) == 0o640
        assert path.stat().st_gid == destination.stat().st_gid
        async with OddsSnapshotClient(destination, clock=lambda: NOW) as client:
            snapshot = await client.read_snapshot(sport)
        assert snapshot.fetched_at == NOW - timedelta(seconds=60)
        assert snapshot.events[0].bookmakers[0].last_update == NOW - timedelta(seconds=90)
    assert before == {p.name: p.read_bytes() for p in source.iterdir()}
    assert "DO_NOT_COPY" not in "".join(p.read_text() for p in destination.iterdir())


async def test_allowlist_strips_extra_metadata_at_every_level(directories):
    source, destination = directories
    doc = fixture()
    doc["apiKey"] = "SECRET_RAW_METADATA"
    doc["events"][0]["credentials"] = "SECRET_RAW_METADATA"
    book = doc["events"][0]["bookmakers"][0]
    book["private"] = "SECRET_RAW_METADATA"
    book["markets"][0]["private"] = "SECRET_RAW_METADATA"
    book["markets"][0]["outcomes"][0]["private"] = "SECRET_RAW_METADATA"
    write(source, doc)
    assert (await run(directories))["status"] == "OK"
    text = (destination / FILES["baseball_mlb"]).read_text()
    assert "SECRET_RAW_METADATA" not in text
    output = json.loads(text)
    assert set(output) == {"fetched_at", "sport", "params", "events"}
    assert set(output["events"][0]) == {
        "id",
        "sport_key",
        "home_team",
        "away_team",
        "commence_time",
        "bookmakers",
    }
    assert set(output["events"][0]["bookmakers"][0]) == {"key", "last_update", "markets"}


async def test_identical_export_does_not_replace_or_change_mtime(directories, monkeypatch):
    assert (await run(directories))["status"] == "OK"
    destination = directories[1]
    before = {
        p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
        for p in destination.iterdir()
    }
    monkeypatch.setattr(os, "replace", lambda *a, **kw: pytest.fail("unchanged file replaced"))
    result = await run(directories)
    assert set(result["sports"].values()) == {"UNCHANGED"}
    assert before == {
        p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
        for p in destination.iterdir()
    }


@pytest.mark.parametrize(
    "kind", ["stale", "future", "invalid", "missing", "wrong_params", "stale_books", "empty"]
)
async def test_invalid_or_empty_source_preserves_previous_copy_with_original_dates(
    directories, kind
):
    source, destination = directories
    await run(directories)
    path = destination / FILES["baseball_mlb"]
    before = path.read_bytes(), path.stat().st_mtime_ns
    doc = fixture()
    if kind in {"stale", "future"}:
        doc["fetched_at"] = (NOW - timedelta(seconds=601 if kind == "stale" else -1)).isoformat()
    if kind == "wrong_params":
        doc["params"]["regions"] = "eu"
    if kind == "stale_books":
        for book in doc["events"][0]["bookmakers"]:
            book["last_update"] = (NOW - timedelta(seconds=601)).isoformat()
    if kind == "empty":
        doc["events"] = []
    source_path = write(source, doc)
    if kind == "invalid":
        source_path.write_text("invalid SECRET_DO_NOT_ECHO")
    if kind == "missing":
        source_path.unlink()
    result = await run(directories)
    assert result["status"] == "PARTIAL"
    assert result["sports"]["baseball_mlb"] in {"SOURCE_REJECTED", "SOURCE_EMPTY"}
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("side", ["source", "destination"])
@pytest.mark.parametrize("kind", ["symlink", "fifo", "hardlink"])
async def test_unsafe_source_or_destination_file_is_not_followed_or_replaced(
    directories, side, kind
):
    source, destination = directories
    await run(directories)
    path = (source if side == "source" else destination) / FILES["baseball_mlb"]
    original = path.read_bytes()
    target = directories[0].parent / "private-target"
    target.write_bytes(original)
    path.unlink()
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        os.link(target, path)
    result = await run(directories)
    assert result["status"] == "PARTIAL"
    assert result["sports"]["baseball_mlb"] == (
        "SOURCE_REJECTED" if side == "source" else "DESTINATION_REJECTED"
    )
    assert target.read_bytes() == original
    assert path.is_symlink() if kind == "symlink" else True


async def test_destination_collision_with_unrelated_file_is_preserved(directories):
    path = directories[1] / FILES["baseball_mlb"]
    path.write_text("PRIVATE_FILE_COLLISION")
    path.chmod(0o640)
    result = await run(directories)
    assert result["sports"]["baseball_mlb"] == "DESTINATION_REJECTED"
    assert path.read_text() == "PRIVATE_FILE_COLLISION"


@pytest.mark.parametrize(
    "case",
    ["same", "descendant", "ancestor", "symlink", "intermediate_symlink", "relative", "mode"],
)
async def test_directory_collisions_and_unsafe_routes_rejected(directories, case):
    source, destination = directories
    if case == "same":
        destination = source
    elif case == "descendant":
        destination = source / "subdir"
    elif case == "ancestor":
        destination = source.parent
    elif case in {"symlink", "intermediate_symlink"}:
        link = destination.parent / "alias"
        link.symlink_to(destination, target_is_directory=True)
        if case == "intermediate_symlink":
            (destination / "child").mkdir()
            destination = link / "child"
        else:
            destination = link
    elif case == "relative":
        destination = "relative"
    elif case == "mode":
        destination.chmod(0o777)
    with pytest.raises(export.ExportError):
        await export.export_snapshots(source, destination, clock=lambda: NOW)


async def test_explicit_permissions_ignore_restrictive_process_umask(directories):
    previous = os.umask(0o077)
    try:
        assert (await run(directories))["status"] == "OK"
    finally:
        os.umask(previous)
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o640 for p in directories[1].iterdir())


async def test_atomic_publish_fsyncs_files_and_directory_and_cleans_temporary(
    directories, monkeypatch
):
    real_fsync = os.fsync
    synced = []

    def track(fd):
        synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", track)
    assert (await run(directories))["status"] == "OK"
    assert synced.count(True) == synced.count(False) == 4
    assert not list(directories[1].glob(".odds-export-*"))


async def test_failed_replace_retains_previous_data_and_cleans_temporary(directories, monkeypatch):
    await run(directories)
    source, destination = directories
    doc = fixture()
    doc["events"][0]["id"] = "updated"
    write(source, doc)
    path = destination / FILES["baseball_mlb"]
    before = path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("SECRET_OS_ERROR")

    monkeypatch.setattr(os, "replace", fail)
    result = await run(directories)
    assert result["sports"]["baseball_mlb"] == "DESTINATION_REJECTED"
    assert path.read_bytes() == before and not list(destination.glob(".odds-export-*"))
    assert "SECRET" not in json.dumps(result)


def test_cli_error_is_sanitized_and_performs_no_service_action(capsys):
    assert export.main(["--source-dir", "relative-SECRET", "--destination-dir", "/elsewhere"]) == 2
    output = capsys.readouterr()
    assert "SECRET" not in output.out + output.err
    assert json.loads(output.out) == {"status": "REJECTED", "reason": "PATH_UNSAFE"}
