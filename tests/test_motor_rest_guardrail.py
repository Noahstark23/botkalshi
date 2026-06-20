"""
Test dirigido del guardarraíl "pata dura primero" (#85) del Motor REST.

Prueba el comportamiento de RestExecutor.execute() ante los estados de la PATA DURA
(la de mayor price_cents, que se dispara PRIMERO), con foco en el camino crítico que
nunca se observó en vivo: KILL de la dura → no-op (cero patas baratas enviadas, cero
huérfanos). Mockea el KalshiRestClient; NO toca código de producción.

Notas de fidelidad (verificadas contra src/strategies/motor_rest_arb/executor.py):
  - execute() PERSISTE Trade rows → se necesita una DB (fixture temporal autouse, abajo).
  - leg_states se arma en orden [hard, *baratas]; para que `state_of` por índice de
    opp.legs sea válido, los opp con asserts por-pata ponen la dura en opp.legs[0]. El
    test del reordenamiento (hard-first) la pone NO-primera y valida por orden de llamadas.
  - El reconcile de un ERROR_RED usa DOBLE fuente → se mockea get_orders Y get_positions.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import select

import src.storage.models as models
from src.clients.kalshi_rest import KalshiClientError
from src.math.arbitrage import ArbLeg, ArbOpportunity, select_hard_leg
from src.math.fees import kalshi_fee_cents
from src.storage.models import Trade
from src.strategies.motor_rest_arb.executor import LegState, RestExecutor

_FOK_KILL_CODE = "fill_or_kill_insufficient_resting_volume"


# =====================================================
# Fixture: SQLite temporal (execute() persiste Trade rows)
# =====================================================


@pytest.fixture(autouse=True)
def _tmp_db_engine(tmp_path):
    db = tmp_path / "guardrail_tmp.db"
    settings = MagicMock()
    settings.DATABASE_URL = f"sqlite:///{db}"
    models._engine = None
    with patch("src.storage.models.get_settings", return_value=settings):
        engine = models.get_engine()
    models.SQLModel.metadata.create_all(engine)
    yield
    models._engine = None


# =====================================================
# Helpers
# =====================================================


def make_leg(ticker, price_cents, *, side="yes", count=1, available_size=100) -> ArbLeg:
    return ArbLeg(
        market_ticker=ticker,
        side=side,
        price_cents=price_cents,
        count=count,
        available_size=available_size,
    )


def make_opp(legs, *, count=1) -> ArbOpportunity:
    """Envuelve las patas en un ArbOpportunity con campos de profit coherentes (informativos)."""
    legs = tuple(legs)
    total_cost = sum(lg.price_cents * lg.count for lg in legs)
    gross = max(0, (100 - sum(lg.price_cents for lg in legs)) * count)
    fees = sum(kalshi_fee_cents(lg.count, lg.price_cents) for lg in legs)
    net = gross - fees
    return ArbOpportunity(
        legs=legs,
        count=count,
        gross_profit_cents=gross,
        fees_cents=fees,
        net_profit_cents=net,
        edge_pct=(net / total_cost * 100) if total_cost else 0.0,
    )


def make_mock_client(state_by_ticker: dict[str, str], *, count=1):
    """
    Client mock cuyo place_order resuelve por ticker según la tabla de estados:
      'FILL'       → 200 con fill completo (también lo usan los sell de rollback → cierran).
      'KILL_409'   → KalshiClientError(409, code=FOK-sin-volumen)  [KILL determinístico]
      'KILL_NOFILL'→ 200 pero fill_count=0                          [KILL no-fill alternativo]
      'ERROR_RED'  → Exception genérica                             [estado desconocido]
    get_orders/get_positions: por default reportan 'no llena / sin posición' (reconcile→no exposición).
    """
    client = AsyncMock()

    def _place_order(**kw):
        ticker = kw["ticker"]
        st = state_by_ticker[ticker]
        if st == "KILL_409":
            raise KalshiClientError(409, "FOK sin volumen", "", error_code=_FOK_KILL_CODE)
        if st == "ERROR_RED":
            raise RuntimeError("network timeout (desconocido)")
        if st == "KILL_NOFILL":
            return {
                "order": {"order_id": f"OID-{ticker}", "fill_count": 0, "remaining_count": count}
            }
        # FILL (default y rollback-sell)
        return {"order": {"order_id": f"OID-{ticker}", "fill_count": count, "remaining_count": 0}}

    client.place_order = AsyncMock(side_effect=_place_order)
    client.get_orders = AsyncMock(return_value={"orders": [{"status": "canceled"}]})
    client.get_positions = AsyncMock(return_value={"market_positions": []})
    return client


def state_of(opp: ArbOpportunity, outcome, leg: ArbLeg) -> LegState:
    """Estado de una pata. Válido cuando opp.legs está en orden [hard, *baratas] (ver doc)."""
    return outcome.leg_states[opp.legs.index(leg)]


def _all_trades() -> dict[str, Trade]:
    with models.get_session() as s:
        return {t.ticker: t for t in s.exec(select(Trade)).all()}


# =====================================================
# 1-2. select_hard_leg (puro, sin mock)
# =====================================================


def test_select_hard_leg_picks_most_expensive():
    cheap = make_leg("A", 20)
    hard = make_leg("B", 80)
    mid = make_leg("C", 50)
    opp = make_opp([cheap, hard, mid])
    h, others = select_hard_leg(opp)
    assert h is hard
    assert others == [cheap, mid]  # las demás, en orden original de opp.legs


def test_select_hard_leg_tiebreak_smaller_size():
    thick = make_leg("THICK", 50, available_size=100)
    thin = make_leg("THIN", 50, available_size=10)  # mismo precio, más fina
    opp = make_opp([thick, thin])
    h, _ = select_hard_leg(opp)
    assert h is thin


# =====================================================
# 3. EL CRÍTICO — KILL de la dura → no-op (cero patas baratas enviadas)
# =====================================================


@pytest.mark.asyncio
async def test_hard_leg_kill_is_noop():
    hard = make_leg("HARD", 80)
    c1 = make_leg("C1", 15)
    c2 = make_leg("C2", 3)
    opp = make_opp([hard, c1, c2])  # dura primera → state_of válido
    client = make_mock_client({"HARD": "KILL_409", "C1": "FILL", "C2": "FILL"})

    outcome = await RestExecutor(client).execute(opp)

    assert outcome.filled is False
    assert outcome.rollback_triggered is False
    # EL ASSERT NO NEGOCIABLE: solo se envió la pata dura. Cero baratas → cero huérfanos.
    assert client.place_order.await_count == 1
    assert client.place_order.await_args_list[0].kwargs["ticker"] == "HARD"
    assert state_of(opp, outcome, hard) is LegState.KILL

    # Reforzado: las baratas quedan marcadas 'not_sent_hard_first' (NO 'fok_kill') → prueba
    # que NUNCA se enviaron (no son un KILL real). La dura sí fue un KILL real.
    rows = _all_trades()
    assert "not_sent_hard_first" in (rows["C1"].notes or "")
    assert "not_sent_hard_first" in (rows["C2"].notes or "")
    assert "fok_kill" not in (rows["C1"].notes or "")
    assert "fok_kill" in (rows["HARD"].notes or "")


# =====================================================
# 4. FILL de la dura → procede (hard-first aunque la dura NO esté primera en opp.legs)
# =====================================================


@pytest.mark.asyncio
async def test_hard_leg_fill_proceeds():
    c1 = make_leg("C1", 15)
    hard = make_leg("HARD", 80)
    c2 = make_leg("C2", 3)
    opp = make_opp([c1, hard, c2])  # dura NO primera → prueba el REORDENAMIENTO
    client = make_mock_client({"HARD": "FILL", "C1": "FILL", "C2": "FILL"})

    outcome = await RestExecutor(client).execute(opp)

    assert outcome.filled is True
    assert client.place_order.await_count == 3
    # La PRIMERA orden es la dura, sin importar su posición en opp.legs.
    assert client.place_order.await_args_list[0].kwargs["ticker"] == "HARD"
    assert all(s is LegState.FILL for s in outcome.leg_states)


# =====================================================
# 5. ERROR_RED de la dura → no-op + reconcilia (doble fuente)
# =====================================================


@pytest.mark.asyncio
async def test_hard_leg_error_red_is_noop_and_flags_reconcile():
    hard = make_leg("HARD", 80)
    c1 = make_leg("C1", 15)
    opp = make_opp([hard, c1])  # dura primera
    client = make_mock_client({"HARD": "ERROR_RED", "C1": "FILL"})

    outcome = await RestExecutor(client).execute(opp)

    assert outcome.filled is False
    assert outcome.rollback_triggered is False
    assert client.place_order.await_count == 1  # solo se intentó la dura; baratas no se envían
    assert state_of(opp, outcome, hard) is LegState.ERROR_RED
    assert outcome.reconciled is True  # se resolvió el estado desconocido...
    client.get_orders.assert_awaited()  # ...consultando la doble fuente
    client.get_positions.assert_awaited()
    # La barata no enviada queda marcada not_sent_hard_first.
    assert "not_sent_hard_first" in (_all_trades()["C1"].notes or "")


# =====================================================
# 6. Contraste — barata KILL DESPUÉS de que la dura llenó → rollback
# =====================================================


@pytest.mark.asyncio
async def test_cheap_leg_kill_after_hard_fill_triggers_rollback():
    hard = make_leg("HARD", 80)
    c1 = make_leg("C1", 15)
    opp = make_opp([hard, c1])  # dura primera
    client = make_mock_client({"HARD": "FILL", "C1": "KILL_409"})

    outcome = await RestExecutor(client).execute(opp)

    assert outcome.filled is False
    assert outcome.rollback_triggered is True  # la dura quedó expuesta → se cierra
    assert state_of(opp, outcome, hard) is LegState.FILL
    assert state_of(opp, outcome, c1) is LegState.KILL
