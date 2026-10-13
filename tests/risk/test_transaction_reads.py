"""Fuentes locales en SQLite temporal: misma transacción, cero escrituras implícitas."""

import math
from datetime import UTC, datetime, timedelta
from fractions import Fraction

import pytest
from sqlalchemy import event, text
from sqlmodel import Session, SQLModel, create_engine, select

import src.risk.manager as manager
import src.storage.models as models
import src.strategies.motor_5_mm.incidents as incidents
from src.risk.manager import RiskManager, _RiskMark
from src.storage.accounting import EXIT_ALLOCATED, REALIZED_PNL
from src.storage.models import OperationalState, Trade
from src.utils.config import Settings

NOW = datetime(2026, 10, 13, 12)


def trade(coid="open", **values):
    data = {
        "client_order_id": coid,
        "ticker": "KX-E-A",
        "side": "yes",
        "action": "buy",
        "count": 1,
        "filled_count": 1,
        "price_cents": 30,
        "fees_cents": 0,
        "strategy": "motor_2_consensus",
        "status": "filled",
        "placed_at": NOW,
    }
    return Trade(**(data | values))


@pytest.fixture
def source(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    SQLModel.metadata.create_all(engine)
    # Lectores sin cliente: defaults tipados, sin validar ni leer credenciales.
    settings = Settings.model_construct(KALSHI_API_KEY_ID="offline-test-only").model_copy(
        update={
            "MAX_DAILY_LOSS_PCT": 1.0,
            "MAX_DAILY_LOSS_FLOOR_USD": 0.0,
            "MAX_WEEKLY_LOSS_PCT": 50.0,
            "MAX_WEEKLY_LOSS_FLOOR_USD": 0.0,
            "MAX_MONTHLY_LOSS_PCT": 50.0,
            "MAX_MONTHLY_LOSS_FLOOR_USD": 0.0,
            "ROLLING_DRAWDOWN_STOP_ENABLED": False,
            "UNREALIZED_MARK_TTL_SEC": 30.0,
        }
    )
    monkeypatch.setattr(manager, "get_settings", lambda: settings)
    risk = RiskManager()
    monkeypatch.setattr(RiskManager, "_marks", {})
    monkeypatch.setattr(incidents, "_current_execution_owner", None)

    def forbidden(*args, **kwargs):
        raise AssertionError("sesión anidada/fallback/efecto prohibido")

    for module in (manager, models, incidents):
        monkeypatch.setattr(module, "get_session", forbidden)
    monkeypatch.setattr(risk, "_get_effective_capital_usd", forbidden)
    with Session(engine) as session:
        session.exec(text("BEGIN IMMEDIATE"))
        yield risk, session, engine
        session.rollback()
    engine.dispose()


def test_sources_see_uncommitted_flushed_rows_without_owning_transaction(source):
    risk, session, engine = source
    session.add_all(
        [
            OperationalState(key="kill_switch", value="engaged", reason="hold"),
            OperationalState(key="mm_quotes_paused", value="paused", reason="hold"),
            OperationalState(key=incidents.INCIDENT_KEY, value="blocked", reason="hold"),
            trade(),
            trade(
                "loss",
                status="settled",
                pnl_cents=-200,
                settled_at=NOW,
                accounting_role=REALIZED_PNL,
            ),
        ]
    )
    session.flush()
    statements = []
    event.listen(
        engine, "before_cursor_execute", lambda c, cu, st, p, ctx, many: statements.append(st)
    )
    assert models.kill_switch_engaged(session=session) == (True, "hold")
    assert models.mm_quotes_paused(session=session) == (True, "hold")
    assert incidents.incident_status(session=session) == (True, "hold")
    assert risk._get_current_exposure_usd(session=session) == 0.30
    stops = risk._stop_loss_snapshot(session=session, now=NOW, capital_usd=100)
    assert risk.first_stop_loss_breach(stops)["name"] == "Diario"
    assert risk._unrealized_snapshot(session=session, now=NOW)["unknown"]
    assert all(st.lstrip().upper().startswith("SELECT") for st in statements)
    assert session.connection().connection.driver_connection.in_transaction
    assert not session.new and not session.dirty and not session.deleted
    session.rollback()
    assert session.exec(select(Trade)).all() == []
    assert session.exec(select(OperationalState)).all() == []


@pytest.mark.parametrize("mutation", ["new", "dirty", "deleted"])
def test_unflushed_financial_state_blocks_without_autoflush(source, mutation):
    risk, session, engine = source
    row = trade()
    if mutation == "new":
        session.add(row)
    else:
        session.add(row)
        session.flush()
        if mutation == "dirty":
            row.count = 9
        else:
            session.delete(row)
    writes = []
    event.listen(engine, "before_cursor_execute", lambda c, cu, st, p, ctx, many: writes.append(st))
    assert math.isinf(risk._get_current_exposure_usd(session=session))
    assert incidents.incident_status(session=session)[0] is True
    for read in (models.kill_switch_engaged, models.mm_quotes_paused):
        with pytest.raises(ValueError, match="UNFLUSHED_FINANCIAL_STATE"):
            read(session=session)
    with pytest.raises(ValueError, match="UNFLUSHED_FINANCIAL_STATE"):
        risk._stop_loss_snapshot(session=session, now=NOW, capital_usd=100)
    with pytest.raises(ValueError, match="UNFLUSHED_FINANCIAL_STATE"):
        risk._unrealized_snapshot(session=session, now=NOW)
    assert not writes
    assert row in getattr(session, mutation)


def test_multi_outcome_sibling_check_uses_same_transaction(source):
    risk, session, _ = source
    for i in range(3):
        session.add(
            trade(
                str(i),
                ticker=f"KX-E-{i}",
                strategy="motor_rest_arb",
                notes="arb_id=11111111-1111-4111-8111-111111111111",
            )
        )
    session.flush()
    assert risk._get_current_exposure_usd(session=session) == 0
    # Esta pata no existe para otra conexión: jamás descontar un hedge incompleto.
    session.add(
        trade(
            "pending",
            ticker="KX-E-3",
            strategy="motor_rest_arb",
            status="pending",
            notes="arb_id=11111111-1111-4111-8111-111111111111",
        )
    )
    session.flush()
    assert risk._get_current_exposure_usd(session=session) == pytest.approx(1.2)


@pytest.mark.parametrize("capital", [None, 0, -1, float("nan"), float("inf"), True])
def test_borrowed_stop_snapshot_never_uses_capital_fallback(source, capital):
    risk, session, _ = source
    with pytest.raises(ValueError, match="CAPITAL"):
        risk._stop_loss_snapshot(session=session, now=NOW, capital_usd=capital)


def test_mtm_keeps_acquisition_time_and_expires_without_refresh(source):
    risk, session, _ = source
    session.add(trade())
    session.flush()
    observed = NOW - timedelta(seconds=12)
    RiskManager._marks[("KX-E-A", "yes")] = _RiskMark(28, 32, observed, Fraction(1))
    snap = risk._unrealized_snapshot(session=session, now=NOW)
    assert snap["oldest_mark_at"] == observed
    assert snap["covered"] == 1
    assert (
        risk._unrealized_snapshot(session=session, now=NOW + timedelta(seconds=5))["oldest_mark_at"]
        == observed
    )
    expired = risk._unrealized_snapshot(session=session, now=NOW + timedelta(seconds=19))
    assert expired["pnl_cents"] is None
    assert "STALE_MARK" in expired["unknown"][0]
    assert RiskManager._marks[("KX-E-A", "yes")].observed_at == observed


def test_m5_owner_is_same_process_even_when_session_is_borrowed(source, monkeypatch):
    _, session, _ = source
    session.add(OperationalState(key=incidents.SESSION_KEY, value="active", reason="owner"))
    session.flush()
    assert incidents.incident_status(session=session)[0]
    monkeypatch.setattr(incidents, "_current_execution_owner", "owner")
    assert incidents.incident_status(session=session) == (False, None)
    monkeypatch.setattr(incidents, "_current_execution_owner", "different")
    assert incidents.incident_status(session=session)[0]


def test_experiment_reads_same_session_and_missing_commitment_is_unknown(source):
    risk, session, _ = source
    risk.settings = risk.settings.model_copy(
        update={
            "EXPERIMENT_BANK200_ENABLED": True,
            "EXPERIMENT_START_AT": (NOW - timedelta(days=1)).replace(tzinfo=UTC).isoformat(),
        }
    )
    snap = risk._experiment_accounting_snapshot(session=session, now=NOW, capital_usd=100)
    assert snap["valid"] and snap["daily_new_risk_usd"] == 0
    session.add(trade())
    session.flush()
    missing = risk._experiment_accounting_snapshot(session=session, now=NOW, capital_usd=100)
    assert not missing["valid"]
    assert "compromiso original" in missing["reason"]


@pytest.mark.parametrize(
    "values",
    [
        {"settled_at": None, "pnl_cents": 0},
        {"settled_at": NOW, "pnl_cents": None},
        {"settled_at": NOW + timedelta(seconds=1), "pnl_cents": 0},
    ],
)
def test_borrowed_stop_snapshot_rejects_unknown_settlement(source, values):
    risk, session, _ = source
    session.add(trade(status="settled", accounting_role=REALIZED_PNL, **values))
    session.flush()
    with pytest.raises(ValueError, match="UNKNOWN_SETTLEMENT"):
        risk._stop_loss_snapshot(session=session, now=NOW, capital_usd=100)


def test_cash_read_keeps_acquisition_time_and_never_invents_funds(source, monkeypatch):
    risk, _, _ = source
    risk.settings = risk.settings.model_copy(update={"BALANCE_MAX_AGE_SECONDS": 30})
    observed = NOW - timedelta(seconds=12)
    for key, value in (
        ("_cached_capital_usd", 100),
        ("_last_raw_balance_usd", 80),
        ("_last_balance_at", observed),
    ):
        monkeypatch.setattr(RiskManager, key, value)
    snap = risk.cash_observation(now=NOW)
    assert snap.amount_usd == 80
    assert snap.acquired_at == observed
    assert risk.cash_observation(now=NOW + timedelta(seconds=5)) == snap
    assert risk.cash_observation(now=NOW + timedelta(seconds=19)) is None
    assert risk.cash_observation(now=observed - timedelta(seconds=1)) is None
    monkeypatch.setattr(RiskManager, "_last_raw_balance_usd", None)
    assert risk.cash_observation(now=NOW) is None
    assert RiskManager._last_balance_at == observed


def test_exit_allocation_is_not_a_second_pnl_or_an_unknown(source):
    risk, session, _ = source
    session.add(
        trade(
            "exit",
            strategy="motor_3_clv",
            action="sell",
            status="settled",
            settled_at=NOW,
            pnl_cents=None,
            accounting_role=EXIT_ALLOCATED,
        )
    )
    session.add(
        trade(
            "closed-buy",
            status="settled",
            settled_at=NOW,
            pnl_cents=-200,
            accounting_role=REALIZED_PNL,
        )
    )
    session.flush()
    snap = risk._stop_loss_snapshot(session=session, now=NOW, capital_usd=100)
    assert all(w["pnl_usd"] == -2 for w in snap["windows"])


@pytest.mark.parametrize(
    "values",
    [
        {"accounting_role": None, "pnl_cents": 0},
        {"accounting_role": EXIT_ALLOCATED, "action": "sell", "strategy": "motor_5_mm"},
        {
            "accounting_role": EXIT_ALLOCATED,
            "action": "sell",
            "strategy": "motor_3_clv",
            "pnl_cents": 0,
        },
        {"accounting_role": REALIZED_PNL, "pnl_cents": None},
        {"accounting_role": REALIZED_PNL, "pnl_cents": 0, "settled_at": None},
        {"accounting_role": REALIZED_PNL, "pnl_cents": 0, "settled_at": NOW + timedelta(seconds=1)},
    ],
)
def test_borrowed_readers_reject_legacy_or_contradictory_accounting(source, values):
    risk, session, _ = source
    risk.settings = risk.settings.model_copy(
        update={
            "EXPERIMENT_BANK200_ENABLED": True,
            "EXPERIMENT_START_AT": (NOW - timedelta(days=1)).replace(tzinfo=UTC).isoformat(),
        }
    )
    session.add(
        trade(
            **(
                {"status": "settled", "settled_at": NOW, "placed_at": NOW - timedelta(days=2)}
                | values
            )
        )
    )
    session.flush()
    with pytest.raises(ValueError, match="UNKNOWN_SETTLEMENT"):
        risk._stop_loss_snapshot(session=session, now=NOW, capital_usd=100)
    snap = risk._experiment_accounting_snapshot(session=session, now=NOW, capital_usd=100)
    assert not snap["valid"] and snap["reason"].startswith("UNKNOWN_SETTLEMENT")


def test_experiment_and_stops_include_m5_sell_pnl_but_not_allocated_exit(source):
    risk, session, _ = source
    risk.settings = risk.settings.model_copy(
        update={
            "EXPERIMENT_BANK200_ENABLED": True,
            "EXPERIMENT_START_AT": (NOW - timedelta(days=1)).replace(tzinfo=UTC).isoformat(),
        }
    )
    session.add_all(
        [
            trade(
                "m5",
                placed_at=NOW - timedelta(days=2),
                strategy="motor_5_mm",
                action="sell",
                status="settled",
                settled_at=NOW,
                pnl_cents=-23,
                accounting_role=REALIZED_PNL,
            ),
            trade(
                "exit",
                strategy="motor_3_clv",
                action="sell",
                status="settled",
                settled_at=NOW,
                accounting_role=EXIT_ALLOCATED,
            ),
            trade(
                "closed",
                placed_at=NOW - timedelta(days=2),
                status="settled",
                settled_at=NOW,
                pnl_cents=-200,
                accounting_role=REALIZED_PNL,
            ),
        ]
    )
    session.flush()
    snap = risk._experiment_accounting_snapshot(session=session, now=NOW, capital_usd=100)
    assert snap["valid"] and snap["cumulative_pnl_usd"] == -2.23
    assert snap["weekly_pnl_usd"] == -2.23
    stops = risk._stop_loss_snapshot(session=session, now=NOW, capital_usd=100)
    assert all(w["pnl_usd"] == -2.23 for w in stops["windows"])
