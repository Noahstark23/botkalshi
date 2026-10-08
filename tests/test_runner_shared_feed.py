"""Runner selects paid snapshots without creating another Odds HTTP client."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.runner import ProductionRunner
from src.utils.config import Settings


async def test_shared_feed_is_live_and_never_constructs_odds_http(tmp_path):
    settings = Settings.model_construct(
        TRADING_ENABLED=False,
        MOTOR_2_SPORTSBOOK_ENABLED=True,
        ODDS_API_KEY="",
        ODDS_SHARED_SNAPSHOT_DIR=str(tmp_path),
        ODDS_SHARED_SNAPSHOT_MAX_AGE_SEC=600,
        ODDS_API_REGIONS="us",
        ODDS_API_SPORT_KEYS="baseball_mlb",
        MOTOR2_SERIES="KXMLBGAME",
    )
    with patch("src.runner.get_settings", return_value=settings):
        runner = ProductionRunner()
    runner._capture = MagicMock()
    runner._capture.motor2_event_universe.return_value = {}
    poller = MagicMock(run=AsyncMock())
    with (
        patch("src.runner.asyncio.sleep", new=AsyncMock()),
        patch("src.runner.RiskManager"),
        patch.object(runner, "_build_motor6_shadow", return_value=None),
        patch(
            "src.strategies.motor_2_consensus.poller.Motor2ShadowPoller", return_value=poller
        ) as factory,
        patch("src.clients.odds_api.OddsAPIClient.__init__", side_effect=AssertionError("no HTTP")),
    ):
        await runner._run_motor2_shadow()
        source = factory.call_args.args[1]
        assert source.is_live
        # Missing snapshot produces no candidates; no fallback to HTTP or fake events.
        assert await source.fetch() == []
        assert factory.call_args.kwargs.get("executor") is None


async def test_position_mark_failure_reaches_required_supervisor():
    with patch("src.runner.get_settings", return_value=Settings.model_construct()):
        runner = ProductionRunner()
    worker = MagicMock(refresh=AsyncMock(side_effect=RuntimeError("inventory unavailable")))
    with (
        patch("src.runner.KalshiRestClient", return_value=AsyncMock()),
        patch("src.monitoring.position_marks.PositionMarkRefresher", return_value=worker),
        pytest.raises(RuntimeError, match="inventory unavailable"),
    ):
        await runner._run_position_marks()


@pytest.mark.parametrize("multi", [False, True])
async def test_exit_marks_have_explicit_owner_without_global_config(multi):
    from src.strategies.motor_2_consensus.exit_engine import Motor2ExitEngine

    engine = Motor2ExitEngine(publish_bid_marks=not multi)
    engine._client = MagicMock(
        get_orderbook=AsyncMock(return_value={"orderbook": {"yes": [["0.60", "2.00"]]}})
    )
    with (
        patch("src.risk.manager.get_settings", side_effect=AssertionError("no config read")),
        patch("src.risk.manager.RiskManager.record_mark") as mark,
    ):
        assert await engine._current_bid("KXMLBGAME-OFFLINE-A", "yes", {}) == 60
    assert mark.call_count == (0 if multi else 1)


@pytest.mark.parametrize("multi", [False, True])
async def test_runner_preserves_complete_marks_in_multi_simulation(multi):
    settings = Settings.model_construct(
        TRADING_ENABLED=False,
        MOTOR_2_SPORTSBOOK_ENABLED=True,
        EXPERIMENT_DYNAMIC_MULTI_ENABLED=multi,
    )
    with patch("src.runner.get_settings", return_value=settings):
        runner = ProductionRunner()
    with (
        patch("src.runner.asyncio.sleep", new=AsyncMock()),
        patch(
            "src.strategies.motor_2_consensus.exit_engine.Motor2ExitEngine",
            return_value=MagicMock(run=AsyncMock()),
        ) as factory,
    ):
        await runner._run_motor2_exits()
    assert factory.call_args.kwargs["publish_bid_marks"] is (not multi)
    assert factory.call_args.kwargs["trading_enabled"] is False
