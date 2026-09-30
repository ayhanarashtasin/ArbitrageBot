"""HTTP transport for the Zero Delta Dash endpoint (async, bounded retries on reads)."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.core.config import ZeroDeltaSettings

from .exceptions import (
    ZeroDeltaAuthError,
    ZeroDeltaConfigError,
    ZeroDeltaParseError,
    ZeroDeltaRateLimitError,
    ZeroDeltaServerError,
    ZeroDeltaTimeoutError,
)

log = logging.getLogger(__name__)


class ZeroDeltaClient:
    def __init__(
        self,
        settings: ZeroDeltaSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        if settings.session_cookie is None:
            raise ZeroDeltaConfigError("ZERO_DELTA_SESSION_COOKIE is not set")
        self._settings = settings
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=settings.request_timeout_seconds,
            transport=transport,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; funding-arbitrage-bot)",
                "Accept": "application/json",
                "Origin": settings.base_url,
                "Referer": f"{settings.base_url}/explore",
            },
            cookies={"session": settings.session_cookie.get_secret_value()},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def post_dash(self, body: dict[str, Any]) -> Any:
        """POST a callback payload and return decoded JSON.

        Retries timeouts, transport errors and 5xx with exponential backoff. 401/403/429 are never
        retried. Error messages carry the status code only, never headers, cookies or bodies.
        """
        attempts = self._settings.max_retries + 1
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                resp = await self._http.post(self._settings.dash_update_path, json=body)
            except httpx.TimeoutException as exc:
                if last:
                    raise ZeroDeltaTimeoutError("Zero Delta request timed out") from exc
            except httpx.TransportError as exc:
                if last:
                    raise ZeroDeltaTimeoutError(
                        f"Zero Delta connection failed ({type(exc).__name__})"
                    ) from exc
            else:
                status = resp.status_code
                if status in (401, 403):
                    raise ZeroDeltaAuthError(status)
                if status == 429:
                    raise ZeroDeltaRateLimitError("Zero Delta rate limit hit (HTTP 429)")
                if status >= 500:
                    if last:
                        raise ZeroDeltaServerError(status)
                elif status != 200:
                    raise ZeroDeltaParseError(f"Unexpected HTTP {status} from Zero Delta")
                else:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        raise ZeroDeltaParseError("Zero Delta returned malformed JSON") from exc
            delay = self._settings.retry_backoff_seconds * (2**attempt)
            log.warning(
                "Zero Delta request failed, retry %d/%d in %.1fs", attempt + 1, attempts - 1, delay
            )
            await self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover
