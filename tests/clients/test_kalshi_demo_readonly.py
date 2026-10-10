"""Transporte local, sin claves reales: el puerto M5 no puede mutar cuentas."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from src.clients import kalshi_demo_readonly as mod


@pytest.fixture
def setup(monkeypatch):
    settings = SimpleNamespace(
        KALSHI_ENV="demo",
        rest_url=mod.DEMO_ROOT,
        KALSHI_PRIVATE_KEY_PATH="unused",
        KALSHI_API_KEY_ID="fixture",
        clave_privada_env=lambda: None,
        **dict.fromkeys(mod.EXECUTION_FLAGS, False),
    )
    signer = MagicMock()
    signer.return_value.sign.return_value = {}
    monkeypatch.setattr(mod, "get_settings", lambda: settings)
    monkeypatch.setattr(mod, "KalshiSigner", signer)
    return settings, signer


def transport(monkeypatch, handler):
    original = httpx.AsyncClient
    options = []

    def factory(**kwargs):
        options.append(kwargs)
        return original(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(mod.httpx, "AsyncClient", factory)
    return options


async def test_open_error_does_not_expose_signer_details(setup):
    _, signer = setup
    signer.side_effect = ValueError("sensitive-credential-detail")
    client = mod.KalshiDemoReadOnlyClient()
    with pytest.raises(mod.DemoReadError, match="^CLIENT_OPEN_FAILED$"):
        await client.__aenter__()
    assert client._signer is client._http is None


@pytest.mark.parametrize("flag", mod.EXECUTION_FLAGS)
def test_execution_rejected_before_signer(setup, flag):
    settings, signer = setup
    setattr(settings, flag, True)
    with pytest.raises(mod.DemoReadError, match="^DEMO_READ_ONLY_REQUIRED$"):
        mod.KalshiDemoReadOnlyClient()
    signer.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("KALSHI_ENV", "production"),
        ("rest_url", "https://api.elections.kalshi.com/trade-api/v2"),
        ("rest_url", mod.DEMO_ROOT + "/"),
    ],
)
async def test_configuration_rechecked_before_open(setup, field, value):
    settings, signer = setup
    client = mod.KalshiDemoReadOnlyClient()
    setattr(settings, field, value)
    with pytest.raises(mod.DemoReadError, match="^DEMO_READ_ONLY_REQUIRED$"):
        await client.__aenter__()
    signer.assert_not_called()


async def test_only_four_get_routes_fixed_host_and_closed_lifecycle(setup, monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    options = transport(monkeypatch, respond)
    client = mod.KalshiDemoReadOnlyClient()
    for method in ("place_order", "cancel_order", "request", "post", "delete"):
        assert not hasattr(client, method)
    async with client:
        await client.get_series("KXNCAAFGAME")
        await client.get_event("KXNCAAFGAME-26OCT10UGAALA")
        await client.get_market("KXNCAAFGAME-26OCT10UGAALA-UGA")
        await client.get_orderbook("KXNCAAFGAME-26OCT10UGAALA-UGA", depth=10)
        with pytest.raises(mod.DemoReadError, match="CLIENT_ALREADY_OPEN"):
            await client.__aenter__()
    assert [r.method for r in requests] == ["GET"] * 4
    assert all(str(r.url).startswith(mod.DEMO_ROOT + "/") for r in requests)
    assert requests[-1].url.params["depth"] == "10"
    assert options[0]["trust_env"] is False and options[0]["follow_redirects"] is False
    assert client._http is client._signer is None
    with pytest.raises(mod.DemoReadError, match="CLIENT_NOT_OPEN"):
        await client.get_series("KXNFLGAME")


@pytest.mark.parametrize(
    "identifier", ["..", ".", "../portfolio/orders", "A/B", "A?x=1", "https://evil", "", None]
)
async def test_invalid_identifiers_never_reach_transport(setup, monkeypatch, identifier):
    requests = []
    transport(monkeypatch, lambda request: requests.append(request))
    async with mod.KalshiDemoReadOnlyClient() as client:
        with pytest.raises(mod.DemoReadError, match="INVALID_IDENTIFIER"):
            await client.get_market(identifier)
    assert not requests


@pytest.mark.parametrize("depth", [0, 101, True, 2.5, "10"])
async def test_invalid_depth_rejected(setup, depth):
    client = mod.KalshiDemoReadOnlyClient()
    with pytest.raises(mod.DemoReadError, match="READ_DEPTH_INVALID"):
        await client.get_orderbook("A", depth=depth)


@pytest.mark.parametrize("status", [301, 302, 401, 429, 500])
async def test_no_redirect_or_retry_and_error_body_not_exposed(setup, monkeypatch, status):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, text="sensitive-body", headers={"Location": "https://other"})

    transport(monkeypatch, respond)
    async with mod.KalshiDemoReadOnlyClient() as client:
        with pytest.raises(mod.DemoReadError, match=f"^READ_HTTP_{status}$"):
            await client.get_series("KXNFLGAME")
    assert len(requests) == 1


@pytest.mark.parametrize(
    "body,error",
    [
        (b"[]", "READ_PAYLOAD_INVALID"),
        (b'{"error":"sensitive"}', "READ_PAYLOAD_INVALID"),
        (b"invalid-secret", "READ_FAILED"),
        (b"x" * 33, "READ_RESPONSE_LIMIT"),
    ],
)
async def test_payload_is_bounded_and_errors_sanitized(setup, monkeypatch, body, error):
    monkeypatch.setattr(mod, "MAX_BYTES", 32)
    transport(monkeypatch, lambda request: httpx.Response(200, content=body))
    async with mod.KalshiDemoReadOnlyClient() as client:
        with pytest.raises(mod.DemoReadError, match=f"^{error}$"):
            await client.get_series("KXNFLGAME")


async def test_timeout_budget_and_blocked_routes(setup, monkeypatch):
    calls = []

    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("sensitive-transport-detail")

    transport(monkeypatch, timeout)
    monkeypatch.setattr(mod, "MAX_REQUESTS", 1)
    async with mod.KalshiDemoReadOnlyClient() as client:
        with pytest.raises(mod.DemoReadError, match="^READ_ROUTE_BLOCKED$"):
            await client._get("/portfolio/orders")
        with pytest.raises(mod.DemoReadError, match="^READ_FAILED$"):
            await client.get_series("KXNFLGAME")
        with pytest.raises(mod.DemoReadError, match="^READ_BUDGET_EXHAUSTED$"):
            await client.get_series("KXNFLGAME")
    assert len(calls) == 1
