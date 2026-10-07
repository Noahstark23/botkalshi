"""Frontera Demo y mecanismo M5 real con SQLite/RSA efímeros; cero red."""

import asyncio
import copy
import json
import os
import socket
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlmodel import create_engine, select

from scripts import demo_runner_execution as demo
from src.monitoring.health import BotState
from src.risk.manager import RiskManager
from src.storage import models
from src.strategies.motor_5_mm import incidents
from src.strategies.motor_5_mm.executor import Motor5Executor
from src.utils import config

TICKER = "KXHIGHTOKC-26OCT08-B88.5"
EVENT = "KXHIGHTOKC-26OCT08"


class Exchange:
    def __init__(self):
        self.requests, self.errors = [], []
        self.order = None
        self.cancel_terminal = True
        self.absent = False
        self.post_error = None
        self.fill = "0.00"
        self.cash = "250.00"
        self.position = "0.00"
        self.fee_type = "quadratic_with_maker_fees"
        self.multiplier = 1
        self.cursor = ""
        self.ack_minimal = False
        self.journal = None
        self.order_metadata = {}
        self.fill_metadata = {}
        self.position_rows = None
        self.fills_rows = None

    def handle(self, request):
        try:
            return self._handle(request)
        except BaseException as exc:
            self.errors.append(exc)
            raise

    def _handle(self, request):
        path = request.url.path.removeprefix("/trade-api/v2")
        self.requests.append((request.method, path))
        assert request.url.host == "external-api.demo.kalshi.co"
        if request.method == "POST":
            body = json.loads(request.content)
            with models.get_session() as session:
                rows = list(session.exec(select(models.Trade)))
                assert len(rows) == 1 and rows[0].status == "pending"
                assert rows[0].client_order_id == body["client_order_id"]
            saved = json.loads(self.journal.path.read_text())
            assert saved["post_attempts"] == 1 and saved["intent_committed_before_post"]
            assert body["count"] == "1" and body["post_only"] is True
            self.order = {
                "order_id": "offline-order",
                "client_order_id": body["client_order_id"],
                "ticker": TICKER,
                "status": "resting",
                "side": "yes",
                "action": "buy",
                "outcome_side": "yes",
                "book_side": "bid",
                "initial_count_fp": "1.00",
                "fill_count_fp": self.fill,
                "remaining_count_fp": str(1 - Decimal(self.fill)),
                "subaccount_number": 0,
                "exchange_index": 0,
                **self.order_metadata,
            }
            if self.post_error == "timeout":
                raise httpx.ReadTimeout("SECRET_BODY", request=request)
            if self.post_error:
                return httpx.Response(self.post_error, text="SECRET_BODY")
            ack = self.order
            if self.ack_minimal:
                ack = {
                    key: value
                    for key, value in ack.items()
                    if key not in {"ticker", "subaccount_number", "side", "action"}
                }
            return httpx.Response(201, json=ack)
        if request.method == "DELETE":
            if self.cancel_terminal:
                self.order.update(status="canceled", remaining_count_fp="0.00")
            ack = {"order_id": "offline-order", "reduced_by": "1.00"}
            return httpx.Response(200, json={"orders": [ack]} if path.endswith("batched") else ack)
        if path == "/portfolio/balance":
            data = {"balance_dollars": self.cash}
        elif path == "/portfolio/orders":
            rows = [] if self.order is None or self.absent else [self.order]
            if request.url.params.get("status") == "resting":
                rows = [row for row in rows if row["status"] == "resting"]
            data = {"orders": rows, "cursor": ""}
        elif path == "/portfolio/orders/offline-order":
            data = {"order": self.order}
        elif path == "/portfolio/positions":
            data = {
                "market_positions": (
                    self.position_rows
                    if self.position_rows is not None
                    else [{"ticker": TICKER, "position_fp": self.position}]
                ),
                "cursor": self.cursor,
            }
        elif path == "/portfolio/fills":
            fills = (
                []
                if Decimal(self.fill) == 0
                else [
                    {
                        "order_id": "offline-order",
                        "fill_id": "offline-fill",
                        "ticker": TICKER,
                        "count_fp": self.fill,
                        **self.fill_metadata,
                    }
                ]
            )
            data = {"fills": fills if self.fills_rows is None else self.fills_rows, "cursor": ""}
        elif path == f"/markets/{TICKER}":
            data = {"market": {"ticker": TICKER, "event_ticker": EVENT, "status": "active"}}
        elif path == f"/events/{EVENT}":
            data = {"event": {"event_ticker": EVENT, "series_ticker": "KXHIGHTOKC"}}
        elif path == "/series/KXHIGHTOKC":
            data = {
                "series": {
                    "ticker": "KXHIGHTOKC",
                    "fee_type": self.fee_type,
                    "fee_multiplier": self.multiplier,
                }
            }
        elif path.endswith("/orderbook"):
            data = {
                "orderbook_fp": {"yes_dollars": [["0.29", "96"]], "no_dollars": [["0.69", "1"]]}
            }
        else:
            raise AssertionError("UNEXPECTED_ROUTE")
        return httpx.Response(200, json=data)


@pytest.fixture
def lab(tmp_path, monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("REAL_NETWORK_FORBIDDEN")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setattr(config.Settings, "model_config", dict(config.Settings.model_config))
    monkeypatch.setattr(config, "_settings", None)
    credentials = tmp_path / "credentials"
    credentials.mkdir(mode=0o700)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = credentials / "kalshi_private_key.pem"
    pem.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    pem.chmod(0o600)
    keyid = credentials / "key_id.txt"
    keyid.write_text("offline-demo-key-123")
    keyid.chmod(0o600)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    settings = demo.configure(workspace, credentials)
    monkeypatch.setattr(models, "_engine", create_engine(settings.DATABASE_URL))
    models.init_db()
    for name, value in vars(BotState).copy().items():
        if not name.startswith("_") and not callable(value) and not isinstance(value, classmethod):
            monkeypatch.setattr(BotState, name, copy.deepcopy(value))
    for name in ("_last_balance_at", "_cached_capital_usd", "_last_raw_balance_usd"):
        monkeypatch.setattr(RiskManager, name, None)
    monkeypatch.setattr(RiskManager, "_check_lock", asyncio.Lock())
    monkeypatch.setattr(RiskManager, "_marks", {})
    monkeypatch.setattr(incidents, "_current_execution_owner", None)
    monkeypatch.setattr(Motor5Executor, "_locks", {})
    sleep = asyncio.sleep

    async def fast_sleep(delay, *args, **kwargs):
        return await sleep(0 if delay == 20 else delay, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    args = SimpleNamespace(ticker=TICKER, price_cents=1, hold_seconds=0)
    state = demo.plan(args)
    state["created_at_unix"] = 1
    journal = demo.Journal(workspace / "journal.json", state, recover=False)
    exchange = Exchange()
    exchange.journal = journal
    yield SimpleNamespace(
        args=args,
        journal=journal,
        exchange=exchange,
        transport=httpx.MockTransport(exchange.handle),
        settings=settings,
        credentials=credentials,
        workspace=workspace,
    )
    models.get_engine().dispose()


def test_dry_plan_reads_no_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(demo, "worker", lambda *_: pytest.fail("worker forbidden"))
    monkeypatch.setattr(demo.subprocess, "run", lambda *_args, **_kw: pytest.fail("subprocess"))
    assert demo.main(["--workspace", str(tmp_path / "not_created")]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["post_attempts"] == 0 and result["cap_cents"] == 30
    assert not (tmp_path / "not_created").exists()


def test_config_isolated_and_demo_only(lab):
    assert lab.settings.rest_url == demo.DEMO_ROOT
    assert lab.settings.KALSHI_ENV == "demo"
    assert lab.settings.TRADING_ENABLED and lab.settings.MOTOR_MM_EXECUTION_ENABLED
    assert not lab.settings.MOTOR_1_ARBITRAGE_ENABLED
    assert not lab.settings.MOTOR_2_SPORTSBOOK_ENABLED
    assert not lab.settings.MOTOR_REST_ENABLED
    assert not lab.settings.ODDS_API_KEY and not lab.settings.TELEGRAM_BOT_TOKEN
    assert config.Settings.model_config["env_file"] is None


def test_manual_override_does_not_validate_autonomous_profile_or_production(lab):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        config.Settings.model_validate(lab.settings.model_dump())
    base = lab.settings.model_copy(
        update={
            "KALSHI_ENV": "production",
            "TRADING_ENABLED": False,
            "MOTOR_MM_ENABLED": False,
            "MOTOR_MM_EXECUTION_ENABLED": False,
        }
    )
    with pytest.raises(PermissionError, match="DEMO_DISABLED_BASE"):
        demo.manual_demo_settings(base, lab.workspace)


@pytest.mark.parametrize("extra", [{"subaccount": 1}, {"exchange_index": 1}, {"post_only": 1}])
async def test_post_guard_rejects_extra_routing_or_bool_coercion(lab, extra):
    from src.clients.kalshi_rest import build_order_body

    with models.get_session() as session:
        session.add(
            models.Trade(
                client_order_id="m5mm_offline",
                ticker=TICKER,
                side="yes",
                action="buy",
                count=1,
                price_cents=1,
                strategy="motor_5_mm",
                status="pending",
            )
        )
        session.commit()
    lab.journal.state["preflight_ok"] = True
    body = build_order_body(
        ticker=TICKER,
        side="yes",
        action="buy",
        count=1,
        yes_price=1,
        client_order_id="m5mm_offline",
        post_only=True,
    )
    body.update(extra)
    async with demo.client_factory(lab.journal, transport=lab.transport)() as client:
        with pytest.raises(PermissionError):
            await client._request("POST", "/portfolio/events/orders", json=body)
    assert lab.journal.state["post_attempts"] == 0 and not lab.exchange.requests


async def test_real_runner_executor_trade_reconciler_terminal(lab):
    result = await demo.run_session(lab.args, lab.journal, transport=lab.transport)
    assert not lab.exchange.errors
    assert result["status"] == "TERMINAL_NO_FILL", result
    assert result["account_reconciled"] and result["runner_shutdown_clean"]
    assert result["executor_constructed_by_runner"]
    assert result["ledger_after_intent"]["exposure_usd"] == pytest.approx(0.01)
    assert result["ledger_after"]["exposure_usd"] == 0
    assert result["ledger_after"]["trades"][0]["status"] == "cancelled"
    assert sum(method == "POST" for method, _ in lab.exchange.requests) == 1


async def test_unknown_cancel_restart_preserves_reservation_and_never_posts_twice(lab):
    lab.exchange.cancel_terminal = False
    first = await demo.run_session(lab.args, lab.journal, transport=lab.transport)
    assert first["status"] == "UNCONFIRMED_RECOVER_REQUIRED"
    assert first["ledger_after"]["exposure_usd"] == pytest.approx(0.01)
    assert first["ledger_after"]["incident_blocked"]
    models.get_engine().dispose()
    incidents._current_execution_owner = None
    lab.journal = demo.Journal(
        lab.journal.path, json.loads(lab.journal.path.read_text()), recover=True
    )
    lab.exchange.journal = lab.journal
    lab.exchange.cancel_terminal = True
    result = await demo.run_session(lab.args, lab.journal, transport=lab.transport)
    assert not lab.exchange.errors
    assert result["status"] == "TERMINAL_NO_FILL", result
    assert result["ledger_after"]["incident_blocked"]  # Recuperar nunca limpia incidente.
    assert result["post_attempts"] == 1
    assert sum(method == "POST" for method, _ in lab.exchange.requests) == 1


@pytest.mark.parametrize("error", [429, 500, "timeout"])
async def test_post_one_attempt_ambiguous_response(lab, error):
    lab.exchange.post_error = error
    lab.exchange.absent = True
    result = await demo.run_session(lab.args, lab.journal, transport=lab.transport)
    assert result["status"] == "UNCONFIRMED_RECOVER_REQUIRED"
    assert result["ledger_after"]["exposure_usd"] == pytest.approx(0.01)
    assert result["post_attempts"] == 1
    assert sum(method == "POST" for method, _ in lab.exchange.requests) == 1
    assert "SECRET_BODY" not in lab.journal.path.read_text()


@pytest.mark.parametrize(
    "field,value",
    [
        ("fee_type", "unknown"),
        ("multiplier", 2),
        ("multiplier", "NaN"),
        ("position", "1"),
        ("cursor", None),
    ],
)
async def test_preflight_rejects_uncertain_budget_account_or_pagination(lab, field, value):
    setattr(lab.exchange, field, value)
    with pytest.raises(ValueError):
        await demo.run_session(lab.args, lab.journal, transport=lab.transport)
    assert not any(method == "POST" for method, _ in lab.exchange.requests)


@pytest.mark.parametrize(
    "flag", ["MOTOR_MM_ENABLED", "TRADING_ENABLED", "MOTOR_MM_EXECUTION_ENABLED"]
)
async def test_real_gate_prevents_executor(lab, flag):
    setattr(lab.settings, flag, False)
    with pytest.raises(PermissionError, match="RUNNER_GATE_CLOSED"):
        await demo.run_session(lab.args, lab.journal, transport=lab.transport)
    assert not lab.exchange.requests


async def test_unrelated_cancel_and_host_are_denied(lab):
    factory = demo.client_factory(lab.journal, transport=lab.transport)
    async with factory() as client:
        with pytest.raises(PermissionError):
            await client._request(
                "DELETE", "/portfolio/events/orders/foreign", params={"subaccount": 0}
            )
        client.base_url = "https://api.elections.kalshi.com/trade-api/v2"
        with pytest.raises(PermissionError, match="DEMO_HOST_REQUIRED"):
            await client.get_balance()
    assert not lab.exchange.requests


async def test_partial_is_exact_but_financial_reconciliation_not_claimed(lab):
    lab.exchange.fill = "0.25"
    lab.exchange.position = "0.00"
    original = lab.exchange._handle

    def update_after_post(request):
        response = original(request)
        if request.method == "POST":
            lab.exchange.position = "0.25"
            lab.exchange.cash = "249.9975"
        return response

    lab.exchange._handle = update_after_post
    result = await demo.run_session(lab.args, lab.journal, transport=lab.transport)
    assert not lab.exchange.errors
    assert result["status"] == "TERMINAL_ACCOUNT_UNCONFIRMED", result
    assert result["terminal_order_confirmed"]
    assert result["partial_observed"]
    assert result["ledger_after"]["trades"][0]["filled_count"] == "0.25"
    assert result["own_fills_count"] is None  # La fila incompleta no acredita contabilidad.
    assert not result["account_reconciled"] and result["position_may_remain"]


async def test_minimal_create_ack_does_not_learn_unverified_order_id(lab):
    lab.exchange.ack_minimal = True
    result = await demo.run_session(lab.args, lab.journal, transport=lab.transport)
    assert not lab.exchange.errors
    assert result["post_attempts"] == 1
    assert result["order_ids"] == ["offline-order"]  # Acreditada por GET posterior.


async def test_terminal_with_changed_cash_is_not_financially_reconciled(lab):
    original = lab.exchange._handle

    def changed_cash(request):
        response = original(request)
        if request.method == "POST":
            lab.exchange.cash = "249.99"
        return response

    lab.exchange._handle = changed_cash
    result = await demo.run_session(lab.args, lab.journal, transport=lab.transport)
    assert result["status"] == "TERMINAL_ACCOUNT_UNCONFIRMED"
    assert not result["account_reconciled"]


async def test_recovery_rejects_journal_order_id_disagreeing_with_ledger(lab):
    await demo.run_session(lab.args, lab.journal, transport=lab.transport)
    state = dict(lab.journal.state, order_ids=["foreign-order"])
    journal = demo.Journal(lab.journal.path, state, recover=True)
    calls = len(lab.exchange.requests)
    with pytest.raises(ValueError, match="RECOVERY_ORDER_ID_MISMATCH"):
        await demo.run_session(lab.args, journal, transport=lab.transport)
    assert len(lab.exchange.requests) == calls


def test_complete_worker_main_with_generated_credentials_and_real_components(
    lab, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    credentials = home / ".config/botkalshi/demo"
    credentials.mkdir(parents=True, mode=0o700)
    for name in ("key_id.txt", "kalshi_private_key.pem"):
        path = credentials / name
        path.write_bytes((lab.credentials / name).read_bytes())
        path.chmod(0o600)
    monkeypatch.setattr(Path, "home", lambda: home)
    models.get_engine().dispose()
    monkeypatch.setattr(models, "_engine", None)
    original = demo.client_factory

    def mock_factory(journal, **kwargs):
        lab.exchange.journal = journal
        return original(journal, transport=lab.transport)

    monkeypatch.setattr(demo, "client_factory", mock_factory)
    workspace = tmp_path / "worker-state"
    assert (
        demo.main(["--workspace", str(workspace), "--hold-seconds", "0", "--worker", "--execute"])
        == 0
    )
    report = json.loads((workspace / "report.json").read_text())
    assert report["status"] == "TERMINAL_NO_FILL", report
    assert report["post_attempts"] == 1
    assert report["config_profile"] == "manual_demo_override_not_F1"
    assert not report["autonomous_profile_boot_validated"]
    assert not lab.exchange.errors
    # Un segundo proceso lógico recupera la misma SQLite; jamás otro POST.
    models.get_engine().dispose()
    monkeypatch.setattr(models, "_engine", None)
    incidents._current_execution_owner = None
    assert (
        demo.main(["--workspace", str(workspace), "--hold-seconds", "0", "--worker", "--recover"])
        == 0
    )
    assert sum(method == "POST" for method, _ in lab.exchange.requests) == 1


def positive_fixture(lab, *, quantity="0.25", price="0.0080", fee="0.000100"):
    """Truth sintética independiente: fill, acumulado Order, posición y débito exactos."""
    gross = Decimal(quantity) * Decimal(price)
    lab.exchange.fill = quantity
    lab.exchange.fill_metadata = {
        "outcome_side": "yes",
        "book_side": "bid",
        "side": "yes",
        "action": "buy",
        "subaccount_number": 0,
        "is_taker": False,
        "yes_price_dollars": price,
        "fee_cost": fee,
    }
    lab.exchange.order_metadata = {
        "maker_fill_cost_dollars": f"{gross:.6f}",
        "taker_fill_cost_dollars": "0.000000",
        "maker_fees_dollars": fee,
        "taker_fees_dollars": "0.000000",
    }
    original = lab.exchange._handle

    def after_post(request):
        response = original(request)
        if request.method == "POST":
            lab.exchange.position = quantity
            # El endpoint de saldo publica cuatro decimales, incluso con fills a seis.
            lab.exchange.cash = f"{Decimal('250.00') - gross - Decimal(fee):.4f}"
        return response

    lab.exchange._handle = after_post


@pytest.mark.parametrize("quantity", ["0.25", "1.00"])
async def test_positive_fill_accounting_exact_and_independent_of_incident(lab, quantity):
    positive_fixture(lab, quantity=quantity)
    result = await demo.run_session(lab.args, lab.journal, transport=lab.transport)
    assert not lab.exchange.errors
    assert result["status"] == "TERMINAL_WITH_FILL", result
    assert result["account_reconciled"] and result["ledger_costs_reconciled"]
    assert result["observed_positions"] == {TICKER: quantity}
    assert result["own_fills_summary"]["fees_usd"] == "0.000100"
    assert result["account_comparison"] == "exact_no_rounding"
    assert result["position_may_remain"]  # Un balance conciliado nunca liquida el fill.
    assert result["partial_observed"] == (quantity == "0.25")
    assert not result["autonomous_profile_boot_validated"]
    assert sum(method == "POST" for method, _ in lab.exchange.requests) == 1


@pytest.mark.parametrize(
    "fault",
    [
        "fee_missing",
        "fee_negative",
        "fee_bool",
        "fee_nan",
        "fee_float",
        "fee_precision",
        "fee_budget",
        "price_above_limit",
        "price_precision",
        "price_legacy_conflict",
        "count_conflict",
        "wrong_side",
        "wrong_book_side",
        "wrong_subaccount",
        "wrong_coid",
        "taker",
        "ledger_fee_missing",
        "ledger_fee_conflict",
        "ledger_gross_conflict",
        "position_opposite",
        "position_conflict",
        "position_duplicate",
        "other_position",
        "cash_microdifference",
        "duplicate_fill",
        "missing_fill",
        "extra_fill",
    ],
)
async def test_positive_accounting_refuses_missing_or_conflicting_evidence(lab, fault):
    positive_fixture(lab)
    fill, order = lab.exchange.fill_metadata, lab.exchange.order_metadata
    mutations = {
        "fee_negative": ("fee_cost", "-0.0001"),
        "fee_bool": ("fee_cost", True),
        "fee_nan": ("fee_cost", "NaN"),
        "fee_float": ("fee_cost", 0.0001),
        "fee_precision": ("fee_cost", "0.0000001"),
        "fee_budget": ("fee_cost", "0.010001"),
        "price_above_limit": ("yes_price_dollars", "0.0101"),
        "price_precision": ("yes_price_dollars", "0.00801"),
        "price_legacy_conflict": ("yes_price", 1),
        "count_conflict": ("count", 1),
        "wrong_side": ("outcome_side", "no"),
        "wrong_book_side": ("book_side", "ask"),
        "wrong_subaccount": ("subaccount_number", 1),
        "wrong_coid": ("client_order_id", "unrelated-intent"),
        "taker": ("is_taker", True),
    }
    if fault in mutations:
        key, value = mutations[fault]
        fill[key] = value
    elif fault == "fee_missing":
        del fill["fee_cost"]
    elif fault == "ledger_fee_missing":
        del order["maker_fees_dollars"]
    elif fault == "ledger_fee_conflict":
        order["maker_fees_dollars"] = "0.000200"
    elif fault == "ledger_gross_conflict":
        order["maker_fill_cost_dollars"] = "0.002100"
    original = lab.exchange._handle

    def inject(request):
        response = original(request)
        path = request.url.path
        if path.endswith("/positions") and lab.exchange.order is not None:
            rows = [{"ticker": TICKER, "position_fp": "0.25"}]
            if fault == "position_opposite":
                rows[0]["position_fp"] = "-0.25"
            elif fault == "position_conflict":
                rows[0]["position"] = 1
            elif fault == "position_duplicate":
                rows *= 2
            elif fault == "other_position":
                rows.append({"ticker": "OTHER", "position_fp": "1.00"})
            return httpx.Response(200, json={"market_positions": rows, "cursor": ""})
        if (
            path.endswith("/balance")
            and lab.exchange.order is not None
            and fault == "cash_microdifference"
        ):
            return httpx.Response(200, json={"balance_dollars": "249.9978"})
        if path.endswith("/fills"):
            payload = response.json()
            if fault == "duplicate_fill":
                payload["fills"] *= 2
            elif fault == "missing_fill":
                payload["fills"] = []
            elif fault == "extra_fill":
                second = dict(payload["fills"][0], fill_id="second-fill")
                payload["fills"].append(second)
            return httpx.Response(200, json=payload)
        return response

    lab.exchange._handle = inject
    result = await demo.run_session(lab.args, lab.journal, transport=lab.transport)
    assert not lab.exchange.errors
    assert result["terminal_order_confirmed"], result
    assert result["status"] == "TERMINAL_ACCOUNT_UNCONFIRMED", result
    assert not result["account_reconciled"] and result["position_may_remain"]
    assert "NaN" not in lab.journal.path.read_text()


async def test_positive_recovery_preserves_pause_and_does_not_post_again(lab):
    positive_fixture(lab)
    lab.exchange.cancel_terminal = False
    first = await demo.run_session(lab.args, lab.journal, transport=lab.transport)
    assert first["status"] == "UNCONFIRMED_RECOVER_REQUIRED"
    assert not first["account_reconciled"]
    assert first["ledger_after"]["incident_blocked"]
    incidents._current_execution_owner = None
    lab.exchange.cancel_terminal = True
    journal = demo.Journal(lab.journal.path, json.loads(lab.journal.path.read_text()), recover=True)
    result = await demo.run_session(lab.args, journal, transport=lab.transport)
    assert result["status"] == "TERMINAL_WITH_FILL", result
    assert result["account_reconciled"] and result["ledger_after"]["incident_blocked"]
    assert result["position_may_remain"]
    assert sum(method == "POST" for method, _ in lab.exchange.requests) == 1


async def test_sub_balance_precision_cost_is_not_rounded_into_a_certificate(lab):
    positive_fixture(lab, fee="0.000125")
    original = lab.exchange._handle

    def rounded_balance(request):
        response = original(request)
        if request.url.path.endswith("/balance") and lab.exchange.order is not None:
            return httpx.Response(200, json={"balance_dollars": "249.9979"})
        return response

    lab.exchange._handle = rounded_balance
    result = await demo.run_session(lab.args, lab.journal, transport=lab.transport)
    assert result["ledger_costs_reconciled"]
    assert result["own_fills_summary"]["fees_usd"] == "0.000125"
    assert not result["account_reconciled"]
