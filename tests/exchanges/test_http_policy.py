"""Retry / rate-limit / error policy, exercised through every adapter (they share PublicHttp).

project.md 53/54: reads use bounded exponential backoff; rate limits are never retried or worked around.
"""

import httpx
import pytest

from app.exchanges._common import PublicHttp
from app.exchanges.base import ExchangeRequestError
from app.exchanges.binance import BinanceUsdmAdapter
from app.exchanges.bybit import BybitLinearAdapter
from app.exchanges.hyperliquid import HyperliquidAdapter

from .helpers import Harness, load

# (id, adapter class, one cheap read, a healthy 200 payload for that read)
CASES = [
    ("binance", BinanceUsdmAdapter, lambda a: a.get_server_time(), load("binance", "server_time.json")),
    ("bybit", BybitLinearAdapter, lambda a: a.get_server_time(), load("bybit", "server_time.json")),
    ("hyperliquid", HyperliquidAdapter, lambda a: a.get_server_time(), load("hyperliquid", "exchange_status.json")),
]
ALL = pytest.mark.parametrize("cls,read,ok", [c[1:] for c in CASES], ids=[c[0] for c in CASES])


def sequence(*steps):
    """Handler that plays `steps` in order (repeating the last). A step is a Response, a payload dict or
    an exception instance to raise."""
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        step = steps[min(state["n"], len(steps) - 1)]
        state["n"] += 1
        if isinstance(step, Exception):
            raise step
        return step if isinstance(step, httpx.Response) else httpx.Response(200, json=step)
    return handler


@ALL
@pytest.mark.parametrize("status", [429, 418])
def test_rate_limit_is_never_retried(cls, read, ok, status):
    h = Harness()
    with pytest.raises(ExchangeRequestError) as err:
        h.run(cls, sequence(httpx.Response(status, headers={"Retry-After": "30"})), read)
    assert len(h.calls) == 1 and h.sleeps == []
    message = str(err.value)
    assert str(status) in message and "not retried" in message and "30s" in message
    assert "://" not in message  # no URLs, no headers


@ALL
@pytest.mark.parametrize("status", [403, 451])
def test_access_refusal_is_never_retried(cls, read, ok, status):
    h = Harness()
    with pytest.raises(ExchangeRequestError, match=f"HTTP {status}.*not retried"):
        h.run(cls, sequence(httpx.Response(status, text="Forbidden")), read)
    assert len(h.calls) == 1 and h.sleeps == []


@ALL
@pytest.mark.parametrize("status", [500, 502, 503, 408])
def test_server_errors_retry_twice_with_doubling_backoff_then_raise(cls, read, ok, status):
    h = Harness()
    with pytest.raises(ExchangeRequestError, match=f"server error \\(HTTP {status}\\)"):
        h.run(cls, sequence(httpx.Response(status)), read)
    assert len(h.calls) == 3
    assert h.sleeps == [0.5, 1.0]


@ALL
def test_server_error_then_success_recovers(cls, read, ok):
    h = Harness()
    result = h.run(cls, sequence(httpx.Response(502), httpx.Response(503), ok), read)
    assert result is not None and len(h.calls) == 3 and h.sleeps == [0.5, 1.0]
    h2 = Harness()
    h2.run(cls, sequence(httpx.Response(500), ok), read)
    assert len(h2.calls) == 2 and h2.sleeps == [0.5]


@ALL
def test_timeouts_are_retried_then_raised(cls, read, ok):
    h = Harness()
    with pytest.raises(ExchangeRequestError, match="timed out"):
        h.run(cls, sequence(httpx.ReadTimeout("slow")), read)
    assert len(h.calls) == 3 and h.sleeps == [0.5, 1.0]


@ALL
def test_timeout_then_success(cls, read, ok):
    h = Harness()
    h.run(cls, sequence(httpx.ConnectTimeout("slow"), ok), read)
    assert len(h.calls) == 2 and h.sleeps == [0.5]


@ALL
def test_transport_errors_are_retried_then_raised(cls, read, ok):
    h = Harness()
    with pytest.raises(ExchangeRequestError, match="connection failed \\(ConnectError\\)"):
        h.run(cls, sequence(httpx.ConnectError("refused")), read)
    assert len(h.calls) == 3 and h.sleeps == [0.5, 1.0]


@ALL
@pytest.mark.parametrize("body", [b"<html>oops</html>", b"", b'{"truncated": '])
def test_malformed_json_is_an_error_and_not_retried(cls, read, ok, body):
    h = Harness()
    with pytest.raises(ExchangeRequestError, match="malformed JSON"):
        h.run(cls, sequence(httpx.Response(200, content=body)), read)
    assert len(h.calls) == 1 and h.sleeps == []


@ALL
def test_json_of_the_wrong_shape_is_an_error_not_a_crash(cls, read, ok):
    h = Harness()
    with pytest.raises(ExchangeRequestError, match="unexpected"):
        h.run(cls, sequence(httpx.Response(200, json=["not", "an", "object"])), read)
    assert len(h.calls) == 1


@ALL
def test_retry_settings_are_injectable_and_capped(cls, read, ok):
    h = Harness()
    with pytest.raises(ExchangeRequestError):
        h.run(cls, sequence(httpx.Response(500)), read, max_retries=0)
    assert len(h.calls) == 1 and h.sleeps == []
    h2 = Harness()
    with pytest.raises(ExchangeRequestError):
        h2.run(cls, sequence(httpx.Response(500)), read, backoff_seconds=0.1)
    assert h2.sleeps == [0.1, 0.2]
    with pytest.raises(ValueError, match="max_retries"):
        cls(max_retries=3)


def test_public_http_rejects_unbounded_retries():
    with pytest.raises(ValueError):
        PublicHttp("x", "https://example.invalid", max_retries=5)
    with pytest.raises(ValueError):
        PublicHttp("x", "https://example.invalid", max_retries=-1)


@ALL
def test_client_errors_other_than_rate_limits_are_not_retried(cls, read, ok):
    h = Harness()
    with pytest.raises(ExchangeRequestError, match="HTTP 400"):
        h.run(cls, sequence(httpx.Response(400, text="bad request")), read)
    assert len(h.calls) == 1 and h.sleeps == []
