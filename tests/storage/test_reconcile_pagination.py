"""reconcile_pending_orders (M1/M2/rest, boot y executor M1) debe recorrer el cursor.

GET /portfolio/orders es paginado (cursor requerido en la respuesta; "" = última
página). Con una sola página, un intent terminal visible en la página 2 queda
`unresolved` para siempre y el boot activa un kill-switch durable sin causa real.
Ausencia tras el listado completo, cursor inválido/repetido, fallo de página o
identidad duplicada NO prueban nada: el intent sigue pending y la reserva intacta.
Fixtures ficticias, sin red ni clientes reales.
"""

from datetime import UTC, datetime, timedelta
from fractions import Fraction
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlmodel import create_engine, select

import src.storage.models as models
from src.monitoring.health import BotState
from src.runner import ProductionRunner
from src.storage.order_reconciliation import reconcile_pending_orders
from src.storage.quantities import confirmed_fill, quantity_fields
from src.utils.config import Settings

M1 = "motor_1_arbitrage"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path}/pag.db")
    models.SQLModel.metadata.create_all(db)
    monkeypatch.setattr(models, "_engine", db)
    monkeypatch.setattr(BotState, "is_paused", False)
    monkeypatch.setattr(BotState, "pause_reason", None)
    monkeypatch.setattr("socket.socket.connect", Mock(side_effect=AssertionError("offline")))
    yield
    db.dispose()


def intent(coid, *, strategy=M1, ticker="KX-T", count=2):
    with models.get_session() as s:
        s.add(
            models.Trade(
                client_order_id=coid,
                ticker=ticker,
                side="yes",
                action="buy",
                price_cents=45,
                strategy=strategy,
                status="pending",
                placed_at=datetime.now(UTC) - timedelta(seconds=120),
                **quantity_fields(count),
            )
        )
        s.commit()


def row(coid):
    with models.get_session() as s:
        return s.exec(select(models.Trade).where(models.Trade.client_order_id == coid)).one()


def order(coid, *, status, fill, remaining="0.00", ticker="KX-T", oid=None):
    return {
        "client_order_id": coid,
        "order_id": oid or f"oid-{coid}",
        "ticker": ticker,
        "side": "yes",
        "action": "buy",
        "status": status,
        "fill_count_fp": fill,
        "remaining_count_fp": remaining,
    }


def filler(n, prefix="old"):
    return [order(f"{prefix}-{i}", status="executed", fill="1.00") for i in range(n)]


class PagedClient:
    """Fake de GET /portfolio/orders paginado por cursor; registra cada request."""

    def __init__(self, pages, *, fail_on="<nunca>"):
        self.pages = pages
        self.fail_on = fail_on
        self.calls = []
        self.place_order = AsyncMock(side_effect=AssertionError("sin órdenes"))
        self.cancel_order = AsyncMock(side_effect=AssertionError("sin cancelaciones"))

    async def get_orders(self, **kwargs):
        self.calls.append(kwargs)
        cursor = kwargs.get("cursor")
        if cursor == self.fail_on:
            raise RuntimeError("página caída")
        return self.pages[cursor]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def test_terminal_on_second_page_is_resolved():
    intent("m1-a")
    client = PagedClient(
        {
            None: {"orders": filler(100), "cursor": "c2"},
            "c2": {
                "orders": [order("m1-a", status="canceled", fill="0.00", remaining="0.00")],
                "cursor": "",
            },
        }
    )
    result = await reconcile_pending_orders(client, strategies=(M1,))
    assert result.unresolved == ()
    assert row("m1-a").status == "cancelled"
    assert [c.get("cursor") for c in client.calls][-1] == "c2"


async def test_partial_resting_on_second_page_keeps_reserve_and_records_floor():
    intent("m1-b")
    client = PagedClient(
        {
            None: {"orders": filler(100), "cursor": "c2"},
            "c2": {
                "orders": [order("m1-b", status="resting", fill="0.50", remaining="1.50")],
                "cursor": "",
            },
        }
    )
    result = await reconcile_pending_orders(client, strategies=(M1,))
    r = row("m1-b")
    assert result.unresolved == ("m1-b",)
    assert r.status == "pending" and confirmed_fill(r) == Fraction(1, 2)


async def test_absent_after_complete_listing_stays_pending():
    """Ausencia ≠ cancelación: terminales viejas migran a /historical/orders."""
    intent("m1-c")
    client = PagedClient(
        {
            None: {"orders": filler(100), "cursor": "c2"},
            "c2": {"orders": filler(3, prefix="new"), "cursor": ""},
        }
    )
    result = await reconcile_pending_orders(client, strategies=(M1,))
    assert result.unresolved == ("m1-c",)
    assert row("m1-c").status == "pending"


@pytest.mark.parametrize(
    "pages,fail_on",
    [
        # cursor repetido (ciclo)
        (
            {None: {"orders": filler(2), "cursor": "c2"}, "c2": {"orders": [], "cursor": "c2"}},
            "<nunca>",
        ),
        # cursor ausente (respuesta malformada respecto del contrato)
        ({None: {"orders": filler(2)}}, "<nunca>"),
        # orders no-lista
        ({None: {"orders": None, "cursor": ""}}, "<nunca>"),
        # fallo de red en la página 2, con la orden terminal en la página 1
        ({None: {"orders": [order("m1-d", status="canceled", fill="0.00")], "cursor": "c2"}}, "c2"),
        # order_id repetido entre páginas (lectura no atómica)
        (
            {
                None: {"orders": [order("m1-d", status="canceled", fill="0.00")], "cursor": "c2"},
                "c2": {
                    "orders": [order("otro", status="canceled", fill="0.00", oid="oid-m1-d")],
                    "cursor": "",
                },
            },
            "<nunca>",
        ),
    ],
)
async def test_incomplete_or_malformed_sweep_concludes_nothing(pages, fail_on):
    intent("m1-d")
    client = PagedClient(pages, fail_on=fail_on)
    result = await reconcile_pending_orders(client, strategies=(M1,))
    assert result.unresolved == ("m1-d",)
    assert row("m1-d").status == "pending"


async def test_contradictory_identity_across_pages_concludes_nothing():
    intent("m1-e")
    client = PagedClient(
        {
            None: {
                "orders": [order("m1-e", status="canceled", fill="0.00", oid="k1")],
                "cursor": "c2",
            },
            "c2": {
                "orders": [order("m1-e", status="executed", fill="2.00", oid="k2")],
                "cursor": "",
            },
        }
    )
    result = await reconcile_pending_orders(client, strategies=(M1,))
    assert result.unresolved == ("m1-e",)
    assert row("m1-e").status == "pending"


async def test_other_motor_rows_untouched_by_m1_sweep():
    intent("m1-f")
    intent("m5mm-x", strategy="motor_5_mm")
    intent("m2-x", strategy="motor_2_consensus")
    client = PagedClient(
        {
            None: {"orders": filler(100), "cursor": "c2"},
            "c2": {
                "orders": [
                    order("m1-f", status="canceled", fill="0.00"),
                    order("m5mm-x", status="canceled", fill="0.00"),
                    order("m2-x", status="canceled", fill="0.00"),
                ],
                "cursor": "",
            },
        }
    )
    await reconcile_pending_orders(client, strategies=(M1,))
    assert row("m1-f").status == "cancelled"
    assert row("m5mm-x").status == "pending" and row("m2-x").status == "pending"


async def test_boot_with_terminal_on_second_page_does_not_engage_kill_switch():
    intent("m1-g")
    client = PagedClient(
        {
            None: {"orders": filler(100), "cursor": "c2"},
            "c2": {"orders": [order("m1-g", status="canceled", fill="0.00")], "cursor": ""},
        }
    )
    settings = Settings.model_construct(
        TRADING_ENABLED=False,
        MOTOR_1_ARBITRAGE_ENABLED=True,
        MOTOR_2_SPORTSBOOK_ENABLED=False,
        MOTOR_REST_ENABLED=False,
    )
    with (
        patch("src.runner.get_settings", return_value=settings),
        patch("src.runner.KalshiRestClient", return_value=client),
        patch("src.runner.ArbitrageExecutor", side_effect=AssertionError("executor forbidden")),
    ):
        await ProductionRunner()._reconcile_on_boot()
    assert row("m1-g").status == "cancelled"
    assert not BotState.is_paused and not models.kill_switch_engaged()[0]
    client.place_order.assert_not_called()
    client.cancel_order.assert_not_called()


# --- Casos de la revisión independiente (Noel): terminal en la página 1, y la página 2
# contradice / falla / repite cursor → sin veredicto; página 1 vacía → se recupera.


def _canceled_full(coid):
    o = order(coid, status="canceled", fill="0.00", remaining="0.00", oid="K1")
    o["initial_count_fp"] = "1.00"
    return o


@pytest.mark.parametrize(
    "second,fail_on",
    [
        (
            {
                "orders": [
                    order("m1-r", status="resting", fill="0.25", remaining="0.75", oid="K1")
                ],
                "cursor": "",
            },
            "<nunca>",
        ),
        (None, "next"),
        ({"orders": [], "cursor": "next"}, "<nunca>"),
    ],
    ids=["a_resting_contradice", "b_pagina2_falla", "c_cursor_repetido"],
)
async def test_review_terminal_first_page_then_incomplete_or_contradictory(second, fail_on):
    intent("m1-r", count=1)
    pages = {None: {"orders": [_canceled_full("m1-r")], "cursor": "next"}}
    if second is not None:
        pages["next"] = second
    client = PagedClient(pages, fail_on=fail_on)
    result = await reconcile_pending_orders(client, strategies=(M1,))
    assert result.unresolved == ("m1-r",)
    assert row("m1-r").status == "pending"


async def test_review_empty_first_page_then_executed_is_recovered():
    intent("m1-s", count=1)
    executed = order("m1-s", status="executed", fill="1.00", remaining="0.00")
    executed["initial_count_fp"] = "1.00"
    client = PagedClient(
        {None: {"orders": [], "cursor": "next"}, "next": {"orders": [executed], "cursor": ""}}
    )
    result = await reconcile_pending_orders(client, strategies=(M1,))
    assert result.unresolved == ()
    assert row("m1-s").status == "filled"


async def test_sweep_has_total_page_budget_not_per_ticker():
    """Muchos tickers: UN barrido, ≤100 llamadas get_orders (páginas) en total, sin veredicto."""
    for i in range(30):
        intent(f"m1-t{i}", ticker=f"KX-T{i}")
    pages = {None: {"orders": [], "cursor": "p1"}}
    pages.update({f"p{k}": {"orders": [], "cursor": f"p{k + 1}"} for k in range(1, 200)})
    client = PagedClient(pages)
    result = await reconcile_pending_orders(client, strategies=(M1,))
    assert len(client.calls) == 100
    assert len(result.unresolved) == 30
