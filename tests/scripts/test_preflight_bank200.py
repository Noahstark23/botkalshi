"""El preflight no confunde datos desconocidos con una cuenta vacía.

Pruebas unitarias sin red ni credenciales; el cliente falso solo admite lecturas.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from scripts import preflight_bank200 as preflight


@pytest.fixture
def ready_settings(monkeypatch):
    settings = SimpleNamespace(
        KALSHI_ENV="production",
        EXPERIMENT_BANK200_ENABLED=True,
        EXPERIMENT_BANK_CONFIRMED_USD=200,
        TRADING_ENABLED=False,
        MOTOR_1_ARBITRAGE_ENABLED=True,
        MOTOR_1_EXECUTION_ENABLED=True,
        MOTOR_3_CLV_ENABLED=True,
        MOTOR_3_EXECUTION_ENABLED=True,
        MOTOR_3_MANAGES_ORPHANS=True,
        MOTOR_2_ENTRY_EXECUTION_ENABLED=False,
        MOTOR_REST_EXECUTION_ENABLED=False,
        MOTOR_MM_EXECUTION_ENABLED=False,
    )
    monkeypatch.setattr(preflight, "get_settings", lambda: settings)
    return settings


@pytest.fixture
def account_client(monkeypatch):
    class ReadOnlyClient:
        balance = {"balance": 20000}
        position_pages = [{"market_positions": [], "cursor": ""}]
        orders = {"orders": [], "cursor": ""}

        def __init__(self):
            self.calls = []
            self.page_index = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get_balance(self):
            self.calls.append(("balance",))
            return self.balance

        async def get_positions(self, *, limit, cursor):
            self.calls.append(("positions", limit, cursor))
            page = self.position_pages[self.page_index]
            self.page_index += 1
            return page

        async def get_orders(self, *, limit):
            self.calls.append(("orders", limit))
            return self.orders

    client = ReadOnlyClient()
    monkeypatch.setattr(preflight, "KalshiRestClient", lambda: client)
    return client


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "invalid",
        True,
        [],
        {},
        "NaN",
        "sNaN",
        "Infinity",
        "-Infinity",
        float("nan"),
        float("inf"),
    ],
)
async def test_unknown_position_is_no_go(raw, ready_settings, account_client, capsys):
    account_client.position_pages = [{"market_positions": [{"position_fp": raw}]}]
    assert await preflight.main() == 2
    output = capsys.readouterr().out
    assert "NO-GO" in output
    assert "READY_TO_ARM" not in output


@pytest.mark.parametrize("row", [{}, {"position_fp": None, "position": 0}, {"position": "NaN"}])
async def test_missing_or_invalid_preferred_position_never_falls_back(
    row, ready_settings, account_client, capsys
):
    account_client.position_pages = [{"market_positions": [row]}]
    assert await preflight.main() == 2
    assert "READY_TO_ARM" not in capsys.readouterr().out


@pytest.mark.parametrize("raw", ["0.1", "-0.1", "0.00000000000000000001", 0.49, -0.49, 1, -1])
async def test_every_nonzero_position_blocks_without_rounding(
    raw, ready_settings, account_client, capsys
):
    account_client.position_pages = [{"market_positions": [{"position_fp": raw}]}]
    assert preflight._position_count({"position_fp": raw}) == Decimal(str(raw))
    assert await preflight.main() == 2
    assert "1 posición(es) abierta(s)" in capsys.readouterr().out


@pytest.mark.parametrize(
    "raw", [None, "", True, [], {}, "NaN", "Infinity", "-Infinity", float("nan"), -1, "20000.1"]
)
async def test_unknown_or_invalid_balance_is_no_go(raw, ready_settings, account_client, capsys):
    account_client.balance = {"balance": raw}
    assert await preflight.main() == 2
    assert "READY_TO_ARM" not in capsys.readouterr().out


@pytest.mark.parametrize("payload", [None, [], {}, "balance", {"portfolio_value": 20000}])
async def test_unknown_balance_structure_is_no_go(payload, ready_settings, account_client):
    account_client.balance = payload
    assert await preflight.main() == 2


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"market_positions": None},
        {"market_positions": {}},
        {"market_positions": ""},
        {"market_positions": [None]},
        {"market_positions": [], "cursor": 0},
        {"market_positions": [], "cursor": False},
        {"market_positions": [], "cursor": " "},
    ],
)
async def test_unknown_positions_structure_is_no_go(payload, ready_settings, account_client):
    account_client.position_pages = [payload]
    assert await preflight.main() == 2


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"orders": None},
        {"orders": {}},
        {"orders": ""},
        {"orders": [None]},
        {"orders": [], "cursor": False},
    ],
)
async def test_unknown_orders_structure_is_no_go(payload, ready_settings, account_client):
    account_client.orders = payload
    assert await preflight.main() == 2


@pytest.mark.parametrize("status", [None, "", "unknown", "filled", True, [], {}])
async def test_unknown_order_state_is_no_go(status, ready_settings, account_client, capsys):
    account_client.orders = {"orders": [{"status": status}]}
    assert await preflight.main() == 2
    assert "READY_TO_ARM" not in capsys.readouterr().out


@pytest.mark.parametrize("status", ["resting", "pending", "open", "RESTING"])
async def test_open_order_blocks(status, ready_settings, account_client, capsys):
    account_client.orders = {"orders": [{"status": status}]}
    assert await preflight.main() == 2
    assert "1 orden(es) abierta(s)" in capsys.readouterr().out


@pytest.mark.parametrize(
    "payload",
    [{"orders": [], "cursor": "next"}, {"orders": [{"status": "executed"}] * 100, "cursor": ""}],
)
async def test_incomplete_orders_are_no_go_even_if_first_page_is_closed(
    payload, ready_settings, account_client
):
    account_client.orders = payload
    assert await preflight.main() == 2


async def test_positions_follow_cursor_and_find_later_exposure(
    ready_settings, account_client, capsys
):
    account_client.position_pages = [
        {"market_positions": [], "cursor": "next"},
        {"market_positions": [{"position_fp": "0.01"}], "cursor": ""},
    ]
    assert await preflight.main() == 2
    assert ("positions", 100, "next") in account_client.calls
    assert "1 posición(es) abierta(s)" in capsys.readouterr().out


async def test_repeated_cursor_blocks_instead_of_looping(ready_settings, account_client):
    account_client.position_pages = [{"market_positions": [], "cursor": "repeat"}] * 2
    assert await preflight.main() == 2
    assert account_client.page_index == 2


async def test_position_pagination_has_a_bound(monkeypatch, ready_settings, account_client):
    monkeypatch.setattr(preflight, "_MAX_POSITION_PAGES", 2)
    account_client.position_pages = [
        {"market_positions": [], "cursor": "first"},
        {"market_positions": [], "cursor": "second"},
    ]
    assert await preflight.main() == 2
    assert account_client.page_index == 2


async def test_full_position_page_without_cursor_is_inconclusive(ready_settings, account_client):
    account_client.position_pages = [{"market_positions": [{"position": 0}] * 100}]
    assert await preflight.main() == 2


@pytest.mark.parametrize("field", ["market_positions", "positions"])
async def test_known_empty_account_remains_read_only_and_does_not_arm(
    field, ready_settings, account_client, capsys
):
    account_client.balance = {"balance": "20000"}
    account_client.position_pages = [{field: [{"position_fp": "0.00"}, {"position": 0}]}]
    account_client.orders = {"orders": [{"status": "executed"}, {"status": "canceled"}]}
    assert await preflight.main() == 0
    assert ready_settings.TRADING_ENABLED is False
    assert account_client.calls == [("balance",), ("positions", 100, None), ("orders", 100)]
    output = capsys.readouterr().out
    assert "READY_TO_ARM" in output
    assert "cash_exchange=$200.00" in output


async def test_known_insufficient_cash_blocks(ready_settings, account_client, capsys):
    account_client.balance = {"balance": 19999}
    assert await preflight.main() == 2
    assert "cash Kalshi $199.99 < $200" in capsys.readouterr().out


async def test_read_exception_does_not_leak_response_or_secret(
    ready_settings, account_client, capsys
):
    async def fail():
        raise RuntimeError("SECRET_RESPONSE_DO_NOT_PRINT")

    account_client.get_balance = fail
    assert await preflight.main() == 2
    output = capsys.readouterr().out
    assert "RuntimeError" in output
    assert "SECRET_RESPONSE_DO_NOT_PRINT" not in output


async def test_trading_on_is_no_go_without_changing_setting(ready_settings, account_client, capsys):
    ready_settings.TRADING_ENABLED = True
    assert await preflight.main() == 2
    assert ready_settings.TRADING_ENABLED is True
    assert "TRADING_ENABLED debe seguir false" in capsys.readouterr().out
