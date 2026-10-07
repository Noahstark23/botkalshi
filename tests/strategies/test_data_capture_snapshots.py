"""La cuota REST limita cada ciclo, pero no debe excluir siempre los mismos mercados."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.strategies.data_capture import DataCaptureService


@pytest.fixture
def capture():
    settings = SimpleNamespace(MOTOR_8_OFI_ENABLED=False, MOTOR_9_SPILLOVER_ENABLED=False)
    client = AsyncMock()
    client.get_market.return_value = {"market": {}}
    client.__aenter__.return_value = client
    with (
        patch("src.strategies.data_capture.get_settings", return_value=settings),
        patch("src.strategies.data_capture.KalshiWebSocket"),
        patch("src.strategies.data_capture.KalshiRestClient", return_value=client),
        patch("src.strategies.data_capture.get_session", return_value=MagicMock()),
        patch("src.strategies.data_capture.DiskGuard.diagnostics_allowed", return_value=True),
    ):
        yield DataCaptureService(), client


async def sample(service, client):
    client.get_market.reset_mock()
    await service._take_snapshots()
    return [call.args[0] for call in client.get_market.await_args_list]


async def test_three_cycles_cover_120_markets_without_increasing_request_budget(capture):
    service, client = capture
    service._tracked_tickers = {f"FAKE-{i:03}" for i in range(120)}
    cycles = [await sample(service, client) for _ in range(3)]
    assert all(len(cycle) == len(set(cycle)) == 50 for cycle in cycles)
    assert set().union(*map(set, cycles)) == service._tracked_tickers
    assert cycles[0] == sorted(service._tracked_tickers)[:50]


@pytest.mark.parametrize("size", [0, 3, 50])
async def test_small_universe_is_sampled_once_per_cycle(capture, size):
    service, client = capture
    service._tracked_tickers = {f"FAKE-{i:03}" for i in range(size)}
    for _ in range(2):
        selected = await sample(service, client)
        assert len(selected) == size
        assert set(selected) == service._tracked_tickers


async def test_discovery_changes_do_not_reset_rotation(capture):
    service, client = capture
    service.MAX_TICKERS_PER_SNAPSHOT_CYCLE = 2
    service._tracked_tickers = {"B", "C", "D", "E"}
    assert await sample(service, client) == ["B", "C"]
    service._tracked_tickers = {"A", "D", "E", "F"}
    assert await sample(service, client) == ["D", "E"]
    assert await sample(service, client) == ["F", "A"]


async def test_failed_requests_do_not_starve_other_markets(capture):
    service, client = capture
    service.MAX_TICKERS_PER_SNAPSHOT_CYCLE = 2
    service._tracked_tickers = {"A", "B", "C", "D"}
    client.get_market.side_effect = RuntimeError("fake upstream failure")
    assert await sample(service, client) == ["A", "B"]
    client.get_market.side_effect = None
    assert await sample(service, client) == ["C", "D"]
    assert await sample(service, client) == ["A", "B"]


async def test_disk_guard_and_client_open_failure_do_not_advance_rotation(capture):
    service, client = capture
    service.MAX_TICKERS_PER_SNAPSHOT_CYCLE = 2
    service._tracked_tickers = {"A", "B", "C", "D"}
    assert await sample(service, client) == ["A", "B"]
    with patch("src.strategies.data_capture.DiskGuard.diagnostics_allowed", return_value=False):
        assert await sample(service, client) == []
    client.__aenter__.side_effect = RuntimeError("fake open failure")
    with pytest.raises(RuntimeError, match="fake open failure"):
        await sample(service, client)
    client.__aenter__.side_effect = None
    assert await sample(service, client) == ["C", "D"]
