import asyncio
import json

import httpx
import pytest

from app.core.config import ZeroDeltaSettings
from app.providers.zerodelta import (
    ZeroDeltaAuthError,
    ZeroDeltaConfigError,
    ZeroDeltaParseError,
    ZeroDeltaProvider,
    ZeroDeltaRateLimitError,
    ZeroDeltaServerError,
    ZeroDeltaTimeoutError,
)
from app.providers.zerodelta.callbacks import asset_payload
from app.providers.zerodelta.client import ZeroDeltaClient

from .helpers import hours, response, row, trace

SECRET = "super-secret-cookie-value"


def settings(**kw) -> ZeroDeltaSettings:
    return ZeroDeltaSettings(_env_file=None, session_cookie=SECRET, user_id=7, **kw)


async def no_sleep(_):
    return None


def make_client(handler, **kw) -> tuple[ZeroDeltaClient, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request, len(calls))

    client = ZeroDeltaClient(settings(**kw), transport=httpx.MockTransport(wrapped), sleep=no_sleep)
    return client, calls


def post(client):
    async def go():
        try:
            return await client.post_dash({"x": 1})
        finally:
            await client.aclose()
    return asyncio.run(go())


def test_success_sends_cookie_and_json():
    client, calls = make_client(lambda r, n: httpx.Response(200, json={"ok": True}))
    assert post(client) == {"ok": True}
    assert calls[0].headers["cookie"] == f"session={SECRET}"
    assert json.loads(calls[0].content) == {"x": 1}
    assert calls[0].url.path == "/_dash-update-component"


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_not_retried(status):
    client, calls = make_client(lambda r, n: httpx.Response(status, text=SECRET))
    with pytest.raises(ZeroDeltaAuthError) as e:
        post(client)
    assert len(calls) == 1 and SECRET not in str(e.value)


def test_rate_limit_not_retried():
    client, calls = make_client(lambda r, n: httpx.Response(429))
    with pytest.raises(ZeroDeltaRateLimitError):
        post(client)
    assert len(calls) == 1


def test_server_error_retried_then_raised():
    client, calls = make_client(lambda r, n: httpx.Response(503), max_retries=2)
    with pytest.raises(ZeroDeltaServerError):
        post(client)
    assert len(calls) == 3


def test_server_error_then_success():
    client, calls = make_client(
        lambda r, n: httpx.Response(500) if n == 1 else httpx.Response(200, json={"a": 1}))
    assert post(client) == {"a": 1} and len(calls) == 2


def test_timeout_retried_then_raised():
    def boom(r, n):
        raise httpx.ReadTimeout("slow", request=r)
    client, calls = make_client(boom, max_retries=1)
    with pytest.raises(ZeroDeltaTimeoutError):
        post(client)
    assert len(calls) == 2


def test_connection_error_maps_to_timeout_error():
    def boom(r, n):
        raise httpx.ConnectError("down", request=r)
    client, _ = make_client(boom, max_retries=0)
    with pytest.raises(ZeroDeltaTimeoutError):
        post(client)


def test_malformed_json():
    client, _ = make_client(lambda r, n: httpx.Response(200, text="<html>nope"))
    with pytest.raises(ZeroDeltaParseError):
        post(client)


def test_unexpected_status():
    client, _ = make_client(lambda r, n: httpx.Response(404))
    with pytest.raises(ZeroDeltaParseError):
        post(client)


def test_missing_cookie_is_config_error():
    with pytest.raises(ZeroDeltaConfigError):
        ZeroDeltaClient(ZeroDeltaSettings(_env_file=None))


def test_missing_user_id_is_config_error():
    with pytest.raises(ZeroDeltaConfigError):
        asset_payload("BTC", ZeroDeltaSettings(_env_file=None, session_cookie=SECRET))


def test_cookie_never_in_repr():
    assert SECRET not in repr(settings())


# ---- provider on top of the client -----------------------------------------------------------

def test_provider_end_to_end_with_mock_server():
    body = response([row("OKX"), row("Gate", volume_m=1.0)],
                    [trace("OKX", hours(168)), trace("Gate", hours(168))])
    client, calls = make_client(lambda r, n: httpx.Response(200, json=body))
    p = ZeroDeltaProvider(settings(), client=client)

    async def go():
        markets = await p.get_current_markets("btc")
        eligible = await p.get_eligible_markets("btc")
        history = await p.get_hourly_history("btc", days=7)
        await p.aclose()
        return markets, eligible, history

    markets, eligible, history = asyncio.run(go())
    assert {m.venue_name for m in markets} == {"OKX", "Gate"}
    assert [m.snapshot.venue_name for m in eligible] == ["OKX"]
    assert len(history["OKX"]) == 169
    sent = json.loads(calls[0].content)
    assert {"id": "store-selected-coin", "property": "data", "value": "BTC"} in sent["inputs"]


def test_health_check_reports_auth_failure_and_success():
    client, _ = make_client(lambda r, n: httpx.Response(401))
    bad = asyncio.run(ZeroDeltaProvider(settings(), client=client).health_check())
    assert not bad.ok and not bad.auth_ok and SECRET not in (bad.error or "")

    client, _ = make_client(lambda r, n: httpx.Response(200, json={"response": {}}))
    good = asyncio.run(ZeroDeltaProvider(settings(), client=client).health_check())
    assert good.ok and good.auth_ok and good.latency_ms is not None

    client, _ = make_client(lambda r, n: httpx.Response(502), max_retries=0)
    down = asyncio.run(ZeroDeltaProvider(settings(), client=client).health_check())
    assert not down.ok and down.auth_ok


def test_coin_table_payload_declares_selected_coin_state():
    from app.providers.zerodelta.callbacks import coin_table_payload

    body = coin_table_payload(settings(), "eth")
    assert body["state"] == [{"id": "store-selected-coin", "property": "data", "value": "ETH"}]
