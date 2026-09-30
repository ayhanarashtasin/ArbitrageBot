"""Shared test plumbing: recorded-fixture loading and a MockTransport harness (no network)."""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "exchanges"

# A fixed wall-clock instant for adapters that need "now" (funding schedule, candle closure).
NOW = datetime(2026, 9, 29, 15, 26, 0, tzinfo=UTC)


def load(venue: str, name: str) -> Any:
    return json.loads((FIXTURES / venue / name).read_text(encoding="utf-8"))


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def json_response(payload: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


class Harness:
    """Runs an adapter action against a handler, recording requests, backoff sleeps and the clock."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self.monotonic = 0.0  # advance to expire the instrument cache

    def run(self, adapter_cls, handler: Callable[[httpx.Request], httpx.Response],
            action: Callable[[Any], Any], now: datetime = NOW, **kwargs) -> Any:
        async def sleep(delay: float) -> None:
            self.sleeps.append(delay)

        def wrapped(request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            return handler(request)

        async def go():
            adapter = adapter_cls(
                transport=httpx.MockTransport(wrapped), sleep=sleep, now=lambda: now,
                clock=lambda: self.monotonic, **kwargs,
            )
            try:
                return await action(adapter)
            finally:
                await adapter.close()

        return asyncio.run(go())

    def paths(self) -> list[str]:
        return [r.url.path for r in self.calls]

    def query(self, index: int) -> dict[str, str]:
        return dict(self.calls[index].url.params)

    def body(self, index: int) -> dict[str, Any]:
        return json.loads(self.calls[index].content)


def by_path(routes: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    """Handler serving routes[path] (a payload or a callable(request)->payload/Response)."""
    def handler(request: httpx.Request) -> httpx.Response:
        route = routes.get(request.url.path)
        if route is None:
            return httpx.Response(404, json={"error": f"no route {request.url.path}"})
        value = route(request) if callable(route) else route
        return value if isinstance(value, httpx.Response) else json_response(value)
    return handler


def by_type(routes: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    """Hyperliquid handler: POST /info routed on the JSON body's "type"."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == "/info", request.url
        body = json.loads(request.content)
        route = routes.get(body["type"])
        if route is None:
            return httpx.Response(422, text="Failed to deserialize the JSON body into the target type")
        value = route(body) if callable(route) else route
        return value if isinstance(value, httpx.Response) else json_response(value)
    return handler
