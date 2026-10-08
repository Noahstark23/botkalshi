"""Integración offline M2/M5: riesgo, intents, conciliación y settlement reales.

SQLite vive sólo en tmp_path. El transporte y las resoluciones son ficticios;
no se construye un cliente HTTP y los sockets están bloqueados. Los Settings
locales mantienen trading/ejecución apagados: se ejercitan directamente los
componentes con dobles, no los gates ni el ciclo completo de ProductionRunner.
El reinicio reemplaza instancias/conexiones SQLite, no es un proceso SO nuevo.
M2 conserva su contabilidad estimada de límite/fee; sólo los costes M5 aportados
por el doble representan evidencia observada dentro de esta simulación.
"""

from __future__ import annotations

import asyncio
import os
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from unittest.mock import AsyncMock, Mock

import pytest
from freezegun import freeze_time
from sqlmodel import create_engine, select

import src.storage.models as models
import src.utils.config as config
from src.clients.kalshi_rest import KalshiOrderConfirmationError
from src.math.quantities import fixed_count
from src.monitoring.health import BotState
from src.risk.commitments import daily_committed_cents
from src.risk.exposure import open_risk_cents
from src.risk.manager import RiskManager
from src.storage.order_reconciliation import reconcile_pending_orders
from src.storage.quantities import confirmed_fill, requested_quantity
from src.strategies.motor_2_consensus.detector import ConsensusSignal
from src.strategies.motor_2_consensus.executor import Motor2Executor
from src.strategies.motor_5_mm.executor import Motor5Executor
from src.strategies.motor_5_mm.fee_policy import SeriesFeePolicy
from src.strategies.motor_5_mm.incidents import incident_status
from src.strategies.motor_5_mm.quoter import QuoteSet
from src.strategies.motor_5_mm.reconciler import MMReconciler
from src.strategies.motor_rest_arb.settlement import MarketResolution, SettlementPoller

NOW = datetime(2026, 10, 8, 16, tzinfo=UTC)
START = NOW - timedelta(hours=1)
M2_TICKER = "KXMLBGAME-26OCT08FIXTUREA-PHI"
M5_TICKER = "KXMLBGAME-26OCT08FIXTUREB-NYM"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Mismo almacenamiento por caso, ninguna credencial ni base histórica."""
    monkeypatch.chdir(tmp_path)
    for key in tuple(os.environ):
        if key.startswith(("KALSHI_", "ODDS_", "TELEGRAM_", "JEV_")):
            monkeypatch.delenv(key)
    denied = Mock(side_effect=AssertionError("M2_M5_LIFECYCLE_OFFLINE_ONLY"))
    for target in (
        "socket.socket.connect",
        "socket.socket.connect_ex",
        "socket.create_connection",
        "socket.getaddrinfo",
        "src.clients.kalshi_rest.KalshiRestClient.__init__",
    ):
        monkeypatch.setattr(target, denied)
    url = f"sqlite:///{tmp_path}/lifecycle.db"
    engines = []

    def reopen():
        if engines:
            engines[-1].dispose()
        engine = create_engine(url)
        models.SQLModel.metadata.create_all(engine)
        monkeypatch.setattr(models, "_engine", engine)
        engines.append(engine)

    reopen()
    settings = config.Settings.model_construct(
        KALSHI_ENV="demo",
        KALSHI_API_KEY_ID="",
        KALSHI_PRIVATE_KEY_PATH="",
        ODDS_API_KEY="",
        DATABASE_URL=url,
        TRADING_ENABLED=False,
        MOTOR_2_ENTRY_EXECUTION_ENABLED=False,
        MOTOR_MM_EXECUTION_ENABLED=False,
        EXPERIMENT_DYNAMIC_MULTI_ENABLED=True,
        EXPERIMENT_START_AT=START.isoformat(),
        EXPERIMENT_BALANCE_MAX_AGE_SEC=60,
        BALANCE_MAX_AGE_SECONDS=60,
        BALANCE_REFRESH_SECONDS=30,
        ACTIVE_CAPITAL_USD=100,
        CAPITAL_CAP_USD=5000,
        CAPITAL_FLOOR_USD=40.5,
        CAPITAL_SAFETY_FACTOR_PCT=90,
        CAPITAL_SMOOTHING_PCT=0,
        DYNAMIC_CAPITAL_ENABLED=True,
        MAX_TRADE_SIZE_USD=1,
        MAX_TRADE_SIZE_PCT=3,
        MAX_SIMULTANEOUS_EXPOSURE_PCT=10,
        MAX_EVENT_DIRECTIONAL_EXPOSURE_USD=1,
        MAX_DAILY_LOSS_FLOOR_USD=0,
        MAX_WEEKLY_LOSS_FLOOR_USD=0,
        MAX_MONTHLY_LOSS_FLOOR_USD=0,
        DAILY_STOP_ENTRIES_ONLY=False,
        UNREALIZED_STOP_ENABLED=True,
    )
    monkeypatch.setattr(config, "_settings", settings)
    monkeypatch.setattr(RiskManager, "_check_lock", asyncio.Lock())
    monkeypatch.setattr(RiskManager, "_marks", {})
    monkeypatch.setattr(RiskManager, "_cached_capital_usd", 100.0)
    monkeypatch.setattr(RiskManager, "_last_raw_balance_usd", 100.0)
    monkeypatch.setattr(RiskManager, "_last_balance_at", NOW.replace(tzinfo=None))
    monkeypatch.setattr(Motor5Executor, "_locks", {})
    monkeypatch.setattr("src.strategies.motor_5_mm.incidents._current_execution_owner", None)
    monkeypatch.setattr(BotState, "is_paused", False)
    monkeypatch.setattr(BotState, "pause_reason", None)
    monkeypatch.setattr("src.risk.manager.alert_risk_event", AsyncMock())
    monkeypatch.setattr("src.monitoring.telegram_alerts.send_alert", AsyncMock())
    with freeze_time(NOW, real_asyncio=True):
        yield reopen
    for engine in engines:
        engine.dispose()
    denied.assert_not_called()


def rows():
    with models.get_session() as session:
        return list(session.exec(select(models.Trade).order_by(models.Trade.id)))


def row_for(ticker):
    return next(row for row in rows() if row.ticker == ticker)


def commitments():
    with models.get_session() as session:
        return {
            row.client_order_id: row.model_dump()
            for row in session.exec(select(models.RiskCommitment))
        }


def daily():
    with models.get_session() as session:
        return daily_committed_cents(session, start=START, now=NOW)


def signal(ticker=M2_TICKER):
    return ConsensusSignal(
        market_ticker=ticker,
        event_key=ticker.rsplit("-", 1)[0],
        kalshi_side="YES",
        odds_api_fair_prob=0.5,
        kalshi_price_cents=40,
        edge_pct=0.05,
        recommended_size_usd=0.4,
    )


def quote(ticker=M5_TICKER):
    return QuoteSet(ticker, fair_prob=0.5, bid_cents=40, ask_cents=None, size=1)


class FictionalExchange:
    """Estado externo inventado; cada cliente nuevo comparte sólo este doble."""

    def __init__(self, *, ambiguous=False):
        self.orders = {}
        self.posts = []
        self.cancels = []
        self.visible = not ambiguous
        self.ambiguous = ambiguous
        self.late_cancel = False

    def observe(self, ticker, *, fill, status, gross=None, fees=None):
        order = next(order for order in self.orders.values() if order["ticker"] == ticker)
        order.update(
            status=status,
            fill_count_fp=fixed_count(fill),
            remaining_count_fp=fixed_count(1 - Fraction(fill) if status == "resting" else 0),
        )
        for key in (
            "maker_fill_cost_dollars",
            "taker_fill_cost_dollars",
            "maker_fees_dollars",
            "taker_fees_dollars",
        ):
            order.pop(key, None)
        if gross is not None:
            order.update(maker_fill_cost_dollars=gross, taker_fill_cost_dollars="0.000000")
        if fees is not None:
            order.update(maker_fees_dollars=fees, taker_fees_dollars="0.000000")
        return deepcopy(order)


class OfflineClient:
    def __init__(self, exchange):
        self.exchange = exchange

    async def get_orderbook(self, ticker):
        # El ask YES efectivo es 100 - bid NO = 40; pasa el parser real M2.
        return {"orderbook": {"yes": [[39, 1]], "no": [[60, 1]]}}

    async def get_series(self, series):
        return {"series": {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": "0.5"}}

    async def get_event(self, event_ticker):
        return {"event": {"event_ticker": event_ticker, "series_ticker": "KXMLBGAME"}}

    async def place_order(self, **call):
        # Esta frontera ficticia acredita commit SQLite ANTES del supuesto POST.
        with models.get_session() as session:
            row = session.exec(
                select(models.Trade).where(models.Trade.client_order_id == call["client_order_id"])
            ).one()
            original = session.get(models.RiskCommitment, row.commitment_origin_id)
            assert row.status == "pending" and requested_quantity(row) == call["count"] == 1
            assert original is not None and original.count_fp == "1.00"
            assert original.risk_cents == 40 and original.fee_reserve_cents == 2
            assert original.client_order_id == row.client_order_id
        assert not config.get_settings().TRADING_ENABLED
        assert call["side"] == "yes" and call["action"] == "buy"
        self.exchange.posts.append(deepcopy(call))
        ioc = call["time_in_force"] == "immediate_or_cancel"
        if not ioc:
            assert call["time_in_force"] == "gtc" and call["post_only"] is True
        order = {
            "order_id": f"fixture-order-{len(self.exchange.posts)}",
            "client_order_id": call["client_order_id"],
            "ticker": call["ticker"],
            "side": "yes",
            "action": "buy",
            "initial_count_fp": "1.00",
            "fill_count_fp": "0.25" if ioc and not self.exchange.ambiguous else "0.00",
            "remaining_count_fp": "0.00" if ioc else "1.00",
            "status": "canceled" if ioc else "resting",
        }
        self.exchange.orders[call["client_order_id"]] = order
        if self.exchange.ambiguous:
            raise TimeoutError("respuesta ficticia perdida tras aceptación")
        return {"order": deepcopy(order)}

    async def get_all_orders(self, **kwargs):
        return deepcopy(list(self.exchange.orders.values())) if self.exchange.visible else []

    async def get_orders(self, **kwargs):
        return {"orders": await self.get_all_orders(), "cursor": ""}

    async def cancel_order(self, order_id):
        self.exchange.cancels.append(order_id)
        order = next(o for o in self.exchange.orders.values() if o["order_id"] == order_id)
        if self.exchange.late_cancel:
            observed = self.exchange.observe(
                order["ticker"], fill="0.50", status="resting", gross="0.195000", fees="0.001000"
            )
            raise KalshiOrderConfirmationError(
                "cancelación ficticia aún no terminal", observed_order=observed
            )
        raise AssertionError("cancelación no planificada en fixture")


async def fees_for(client, ticker):
    return await SeriesFeePolicy(client).observe(ticker, event_ticker=ticker.rsplit("-", 1)[0])


def mark(ticker):
    RiskManager.record_mark(
        ticker, "yes", 39, ask_cents=40, observed_at=NOW, fee_multiplier=Fraction(1, 2)
    )


class FictionalResolution:
    def __init__(self, results):
        self.results = results
        self.calls = []

    async def get_resolution(self, ticker):
        self.calls.append(ticker)
        result = self.results.get(ticker)
        return MarketResolution(ticker, result) if result else None


@pytest.mark.parametrize("m5_result,m5_pnl", [("no", "-0.293625"), ("yes", "0.456375")])
async def test_shared_partial_late_cancel_restart_and_settlement(isolated, m5_result, m5_pnl):
    exchange = FictionalExchange()
    client = OfflineClient(exchange)
    risk = RiskManager()
    outcome = await Motor2Executor(client, risk).execute(signal())
    assert outcome.filled and outcome.filled_count == Fraction(1, 4)
    assert open_risk_cents(row_for(M2_TICKER)) == 10
    assert daily() == 42  # Cantidad original 1 @ 40c + reserva fee 2c.
    mark(M2_TICKER)

    maker = Motor5Executor(client, risk, max_exposure_usd=3)
    # El dueño M2 del ticker bloquea M5 en la misma DB, sin permiso ficticio de riesgo.
    assert (
        await maker.sync_quotes(quote(M2_TICKER), fee_observation=await fees_for(client, M2_TICKER))
        == "risk_blocked"
    )
    assert len(exchange.posts) == 1 and len(commitments()) == 1
    assert (
        await maker.sync_quotes(quote(), fee_observation=await fees_for(client, M5_TICKER))
        == "synced"
    )
    original = commitments()
    assert {entry["strategy"] for entry in original.values()} == {"motor_2_consensus", "motor_5_mm"}
    assert daily() == 84 and risk._get_current_exposure_usd() == 0.50

    exchange.observe(M5_TICKER, fill="0.25", status="resting", gross="0.097500", fees="0.000500")
    report = await MMReconciler(client).reconcile(live_coids=maker.live_coids())
    assert report.complete and report.still_resting == 1
    assert confirmed_fill(row_for(M5_TICKER)) == Fraction(1, 4)
    assert open_risk_cents(row_for(M5_TICKER)) == 40  # Resting conserva toda la reserva.
    exchange.late_cancel = True
    await maker.retire_ticker(M5_TICKER)
    assert len(exchange.cancels) == 1
    assert incident_status()[0] and maker.live_coids()
    assert row_for(M5_TICKER).status == "pending"
    assert confirmed_fill(row_for(M5_TICKER)) == Fraction(1, 2)
    assert open_risk_cents(row_for(M5_TICKER)) == 40 and daily() == 84

    isolated()  # Cierra conexiones y vuelve a abrir exactamente la DB temporal.
    client = OfflineClient(exchange)
    restarted = Motor5Executor(client, RiskManager(), max_exposure_usd=3)
    assert restarted.live_coids() == set() and incident_status()[0]
    assert (
        await restarted.sync_quotes(quote(), fee_observation=await fees_for(client, M5_TICKER))
        == "corrupted"
    )
    assert len(exchange.posts) == 2
    # Un GET terminal regresivo no borra el fill observado durante DELETE.
    exchange.observe(M5_TICKER, fill="0.25", status="canceled")
    uncertain = await MMReconciler(client).reconcile(live_coids=set())
    assert not uncertain.complete and uncertain.discrepancies == 1
    assert row_for(M5_TICKER).status == "pending"
    assert confirmed_fill(row_for(M5_TICKER)) == Fraction(1, 2)
    assert open_risk_cents(row_for(M5_TICKER)) == 40
    exchange.observe(M5_TICKER, fill="0.75", status="canceled", gross="0.292500", fees="0.001125")
    terminal = await MMReconciler(client).reconcile(live_coids=set())
    assert terminal.complete and terminal.resolved_filled == 1
    m5 = row_for(M5_TICKER)
    assert m5.status == "filled" and confirmed_fill(m5) == Fraction(3, 4)
    assert (m5.fill_cost_usd_fp, m5.fees_usd_fp) == ("0.292500", "0.001125")
    assert open_risk_cents(m5) == 30 and RiskManager()._get_current_exposure_usd() == 0.40
    unchanged_rows = [row.model_dump() for row in rows()]
    again = await MMReconciler(client).reconcile(live_coids=set())
    assert again.complete and again.pending_checked == 0
    assert [row.model_dump() for row in rows()] == unchanged_rows
    assert commitments() == original and daily() == 84

    source = FictionalResolution({M2_TICKER: "yes"})
    poller = SettlementPoller(source)
    assert await poller.settle_once() == 1  # M5 no resuelto aún, nunca se inventa el resultado.
    assert row_for(M5_TICKER).status == "filled"
    m2 = row_for(M2_TICKER)
    assert m2.pnl_usd_fp == "0.1400"  # Payout .25 - límite .10 - fee estimada .01.
    assert "settlement_estimated" in m2.notes
    source.results[M5_TICKER] = m5_result
    assert await poller.settle_once() == 1
    m5 = row_for(M5_TICKER)
    assert Fraction(m5.pnl_usd_fp) == Fraction(m5_pnl)
    assert m5.pnl_cents == (-30 if m5_result == "no" else 45)
    assert m5.fees_cents == 1  # Espejo conservador; .001125 USD se usa exacto en PnL.
    assert "settlement_estimated" not in m5.notes
    assert RiskManager()._get_current_exposure_usd() == 0
    settled_rows = [row.model_dump() for row in rows()]
    resolved_calls = len(source.calls)
    assert await SettlementPoller(source).settle_once() == 0
    assert len(source.calls) == resolved_calls
    assert [row.model_dump() for row in rows()] == settled_rows
    assert commitments() == original and daily() == 84
    assert len(exchange.posts) == 2 and len(exchange.cancels) == 1
    assert incident_status()[0]  # Conciliar/settlear no reconoce automáticamente el incidente.


@pytest.mark.parametrize("motor", ["M2", "M5"])
async def test_ambiguous_create_keeps_budget_and_never_reposts_on_restart(isolated, motor):
    ticker = M2_TICKER if motor == "M2" else M5_TICKER
    exchange = FictionalExchange(ambiguous=True)
    client = OfflineClient(exchange)
    if motor == "M2":
        initial = await Motor2Executor(client, RiskManager()).execute(signal())
        assert initial.placed and initial.uncertain
    else:
        maker = Motor5Executor(client, RiskManager(), max_exposure_usd=3)
        assert (
            await maker.sync_quotes(quote(), fee_observation=await fees_for(client, ticker))
            == "corrupted"
        )
    original = commitments()
    assert len(exchange.posts) == 1 and daily() == 42
    assert row_for(ticker).status == "pending" and open_risk_cents(row_for(ticker)) == 40

    isolated()
    client = OfflineClient(exchange)
    if motor == "M2":
        repeat = await Motor2Executor(client, RiskManager()).execute(signal())
        assert not repeat.placed and repeat.reason == "already_open"
    else:
        fresh = Motor5Executor(client, RiskManager(), max_exposure_usd=3)
        assert (
            await fresh.sync_quotes(quote(), fee_observation=await fees_for(client, ticker))
            == "corrupted"
        )

    async def reconcile():
        if motor == "M2":
            result = await reconcile_pending_orders(client, strategies=("motor_2_consensus",))
            return not result.unresolved
        return (await MMReconciler(client).reconcile()).complete

    assert not await reconcile()  # Ausencia temporal no acredita cancelación ni cero fills.
    assert row_for(ticker).status == "pending" and open_risk_cents(row_for(ticker)) == 40
    assert commitments() == original and daily() == 42 and len(exchange.posts) == 1
    source = FictionalResolution({ticker: "yes"})
    assert await SettlementPoller(source).settle_once() == 0
    assert source.calls == []  # Settlement no debe liberar una intención todavía desconocida.

    exchange.visible = True
    exchange.observe(ticker, fill="0.25", status="resting", gross="0.097500", fees="0.000500")
    await reconcile()
    assert row_for(ticker).status == "pending"
    assert confirmed_fill(row_for(ticker)) == Fraction(1, 4)
    assert open_risk_cents(row_for(ticker)) == 40
    isolated()
    client = OfflineClient(exchange)
    exchange.observe(ticker, fill="0.00", status="canceled")
    assert not await reconcile()
    assert row_for(ticker).status == "pending"
    assert confirmed_fill(row_for(ticker)) == Fraction(1, 4)
    exchange.observe(ticker, fill="0.50", status="canceled", gross="0.195000", fees="0.001001")
    assert await reconcile()
    assert row_for(ticker).status == "filled"
    assert confirmed_fill(row_for(ticker)) == Fraction(1, 2)
    assert open_risk_cents(row_for(ticker)) == 20
    assert await SettlementPoller(source).settle_once() == 1
    expected = Fraction("0.2900" if motor == "M2" else "0.303999")
    assert Fraction(row_for(ticker).pnl_usd_fp) == expected
    assert RiskManager()._get_current_exposure_usd() == 0
    assert await reconcile() and await SettlementPoller(source).settle_once() == 0
    assert commitments() == original and daily() == 42
    assert len(exchange.posts) == 1 and exchange.cancels == []
