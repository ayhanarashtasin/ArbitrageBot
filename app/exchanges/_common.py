"""Shared plumbing for the direct-venue PUBLIC market data adapters (Phase 3).

Nothing in here signs a request, sends a key or touches an account/order endpoint. The adapters that
build on it only ever call public market-data endpoints.

Error policy (project.md sections 53 and 54):
- reads are retried with bounded exponential backoff (at most MAX_RETRIES_CAP retries) on timeouts,
  transport errors, HTTP 5xx and HTTP 408 (documented backend timeout) only;
- HTTP 418/429 (rate limit / IP ban) and 403/451 (WAF, IP rate limit or region block) are NEVER
  retried and never worked around: the adapter raises ExchangeRequestError with a clear message;
- error messages carry the venue name, the endpoint path, the status code and the venue's own short
  error text. They never carry request headers, cookies or full URLs.
"""

import asyncio
import json
import logging
import time
from collections import Counter
from abc import abstractmethod
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from app.domain.market import BookLevel, InstrumentMeta, utcnow

from .base import ExchangeAdapter, ExchangeError, ExchangeRequestError

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0
# Connecting gets far less time than reading. Measured live 2026-10-02: from this network about one in four NEW TCP
# connections to Binance's CloudFront edge never completed (SYN lost; every other venue 10/10), and each one held the
# trade cycle for the full 10 s before the retry. A healthy connect takes 20-150 ms, so 3 s only cuts off dead ones.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 3.0
# httpx closes an idle connection after 5 s by default. The engine polls each venue every 5-15 s, so it reconnected
# on almost every cycle and met the lost-connect problem above each time. Measured side by side at a 5 s cadence:
# 5 s keep-alive -> 12 of 40 requests hung, 60 s keep-alive -> 1 of 46.
DEFAULT_KEEPALIVE_EXPIRY_SECONDS = 60.0
MAX_RETRIES_CAP = 2  # hard upper bound; a larger max_retries is a programming error
DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_SECONDS = 0.5  # doubles per retry: 0.5s, 1.0s
DEFAULT_INSTRUMENT_TTL_SECONDS = 600.0  # list_instruments() cache: 10 minutes
USER_AGENT = "funding-arbitrage-bot/0.1 (public market data)"

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# Quote assets that are treated as 1 USD when converting a quote-denominated 24h turnover to USD.
# For any other quote asset volume_24h_usd is None: no exchange rate is assumed.
USD_LIKE_QUOTES = frozenset({"USD", "USDT", "USDC", "USD1"})

# Candle durations in milliseconds, keyed by the project-wide timeframe string.
TIMEFRAME_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 3 * 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 3_600_000,
    "2h": 2 * 3_600_000,
    "4h": 4 * 3_600_000,
    "6h": 6 * 3_600_000,
    "8h": 8 * 3_600_000,
    "12h": 12 * 3_600_000,
    "1d": 86_400_000,
}


# ---------------------------------------------------------------------------------------------
# value helpers
# ---------------------------------------------------------------------------------------------
def dec(value: Any) -> Decimal:
    """Exact Decimal from a venue string/int. Rejects None, bool, empty strings, NaN and Infinity."""
    if value is None or isinstance(value, bool):
        raise ValueError(f"not a number: {value!r}")
    result = value if isinstance(value, Decimal) else Decimal(str(value))
    if not result.is_finite():
        raise ValueError(f"not a finite number: {value!r}")
    return result


def opt_dec(value: Any) -> Decimal | None:
    """Like dec() but None/'' (venue 'no value') -> None."""
    if value is None or value == "":
        return None
    return dec(value)


def ms_to_dt(value: Any) -> datetime:
    """Unix epoch milliseconds -> timezone-aware UTC datetime (exact, no float rounding)."""
    return EPOCH + timedelta(milliseconds=int(value))


def opt_ms_to_dt(value: Any) -> datetime | None:
    """Epoch milliseconds -> datetime; None, '', 0 and '0' (venue 'no time') -> None."""
    if value is None or value == "":
        return None
    ms = int(value)
    return ms_to_dt(ms) if ms > 0 else None


def usd_volume(quote: str, quote_volume: Decimal | None) -> Decimal | None:
    if quote_volume is None or quote.upper() not in USD_LIKE_QUOTES:
        return None
    return quote_volume


@contextmanager
def parsing(venue: str, what: str) -> Iterator[None]:
    """Turn a malformed/unexpected response shape into ExchangeRequestError (never a bare KeyError)."""
    try:
        yield
    except ExchangeError:
        raise
    except (KeyError, IndexError, TypeError, ValueError, ArithmeticError, AttributeError) as exc:
        raise ExchangeRequestError(
            f"{venue}: unexpected {what} response ({type(exc).__name__}: {exc})"
        ) from exc


def book_levels(rows: Any, *, descending: bool, depth: int, price_of=None, qty_of=None) -> tuple[BookLevel, ...]:
    """Parse venue book rows into BookLevels: best price first, zero-size rows dropped, trimmed to depth.

    Sorting is done here (not trusted from the venue) so the OrderBook invariant - bids descending,
    asks ascending - always holds. Quantities are passed through unchanged: every supported venue
    quotes public book sizes in BASE asset units (contract multiplier 1).
    """
    price_of = price_of or (lambda row: row[0])
    qty_of = qty_of or (lambda row: row[1])
    parsed = [BookLevel(price=dec(price_of(r)), quantity=dec(qty_of(r))) for r in rows]
    parsed = [lvl for lvl in parsed if lvl.quantity > 0]
    parsed.sort(key=lambda lvl: lvl.price, reverse=descending)
    return tuple(parsed[:depth])


def is_closed(open_time: datetime, timeframe: str, now: datetime) -> bool:
    return open_time + timedelta(milliseconds=TIMEFRAME_MS[timeframe]) <= now


def check_positive(name: str, value: int, maximum: int | None = None) -> None:
    if value < 1 or (maximum is not None and value > maximum):
        bound = f"between 1 and {maximum}" if maximum is not None else ">= 1"
        raise ValueError(f"{name} must be {bound}, got {value}")


# ---------------------------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------------------------
def _error_detail(resp: httpx.Response) -> str:
    """Short venue error text from a non-200 body (public endpoints only; no headers)."""
    try:
        body = json.loads(resp.content)
    except ValueError:
        text = resp.text.strip().replace("\n", " ")
        return text[:120]
    if isinstance(body, dict):
        parts = []
        for key in ("code", "retCode", "msg", "retMsg", "error"):
            if key in body and body[key] not in (None, ""):
                parts.append(f"{key}={str(body[key])[:100]}")
        return ", ".join(parts)
    return str(body)[:120]


class PublicHttp:
    """Async JSON client for one venue's public REST API."""

    def __init__(
        self,
        venue: str,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        if not 0 <= max_retries <= MAX_RETRIES_CAP:
            raise ValueError(f"max_retries must be between 0 and {MAX_RETRIES_CAP}")
        self._venue = venue
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._sleep = sleep
        self._counts = Counter()
        self._errors = Counter()
        self._statuses = Counter()
        self._last_request_ms = 0.0
        self._total_request_ms = 0.0
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout, connect=min(timeout, DEFAULT_CONNECT_TIMEOUT_SECONDS)),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20,
                                keepalive_expiry=DEFAULT_KEEPALIVE_EXPIRY_SECONDS),
            transport=transport,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post_json(self, path: str, body: dict[str, Any]) -> Any:
        """POST a JSON body. Used for read-only queries (Hyperliquid /info) - never for orders."""
        return await self._request("POST", path, json_body=body)

    async def _request(self, method: str, path: str, *, params=None, json_body=None) -> Any:
        started = time.monotonic()
        self._counts["requests"] += 1
        try:
            return await self._read(method, path, params=params, json_body=json_body)
        except ExchangeRequestError as exc:
            self._counts["failed_requests"] += 1
            log.warning("%s: %s %s request failed after %.0fms: %s", self._venue, method, path,
                        (time.monotonic() - started) * 1000, exc)
            raise
        finally:
            self._last_request_ms = (time.monotonic() - started) * 1000
            self._total_request_ms += self._last_request_ms

    def diagnostics(self) -> dict:
        """Bounded counters for this process; no URLs, symbols, credentials or response bodies."""
        return {**dict(self._counts), "errors": dict(self._errors), "http_statuses": dict(self._statuses),
                "last_request_ms": round(self._last_request_ms, 1),
                "total_request_ms": round(self._total_request_ms, 1)}

    async def _read(self, method: str, path: str, *, params=None, json_body=None) -> Any:
        venue = self._venue
        attempts = self._max_retries + 1
        for attempt in range(attempts):
            last = attempt == attempts - 1
            started = time.monotonic()
            self._counts["attempts"] += 1
            try:
                resp = await self._http.request(method, path, params=params, json=json_body)
            except httpx.TimeoutException as exc:
                reason = type(exc).__name__
                self._errors[reason] += 1
                if last:
                    raise ExchangeRequestError(f"{venue}: request timed out ({reason}) ({method} {path})") from exc
            except httpx.TransportError as exc:
                reason = type(exc).__name__
                self._errors[reason] += 1
                if last:
                    raise ExchangeRequestError(
                        f"{venue}: connection failed ({type(exc).__name__}) ({method} {path})"
                    ) from exc
            except httpx.HTTPError as exc:  # not transport-level: never retried
                raise ExchangeRequestError(
                    f"{venue}: HTTP client error ({type(exc).__name__}) ({method} {path})"
                ) from exc
            else:
                status = resp.status_code
                self._statuses[str(status)] += 1
                reason = f"HTTP {status}"
                if status in (418, 429):
                    retry_after = resp.headers.get("Retry-After", "")
                    hint = f"; venue asks to wait {retry_after}s" if retry_after.isdigit() else ""
                    kind = "IP banned for repeated rate-limit violations" if status == 418 else "rate limit exceeded"
                    raise ExchangeRequestError(
                        f"{venue}: {kind} (HTTP {status}) on {method} {path}{hint}; not retried"
                    )
                if status in (403, 451):
                    detail = _error_detail(resp)
                    raise ExchangeRequestError(
                        f"{venue}: access refused (HTTP {status}: WAF, IP rate limit or region block) "
                        f"on {method} {path}{'; ' + detail if detail else ''}; not retried"
                    )
                if status >= 500 or status == 408:  # 408: Binance "backend response timeout"
                    self._errors[reason] += 1
                    if last:
                        raise ExchangeRequestError(f"{venue}: server error (HTTP {status}) on {method} {path}")
                elif status != 200:
                    detail = _error_detail(resp)
                    raise ExchangeRequestError(
                        f"{venue}: HTTP {status} on {method} {path}{'; ' + detail if detail else ''}"
                    )
                else:
                    try:
                        result = json.loads(resp.content, parse_float=Decimal)
                    except ValueError as exc:
                        raise ExchangeRequestError(f"{venue}: malformed JSON from {method} {path}") from exc
                    if attempt:
                        self._counts["recovered_requests"] += 1
                        log.info("%s: %s %s recovered on attempt %d/%d", venue, method, path, attempt + 1, attempts)
                    return result
            delay = self._backoff * (2**attempt)
            self._counts["retries"] += 1
            log.warning("%s: %s %s failed (%s, %.0fms), retry %d/%d in %.1fs", venue, method, path, reason,
                        (time.monotonic() - started) * 1000, attempt + 1, attempts - 1, delay)
            await self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------------------------
# instrument cache
# ---------------------------------------------------------------------------------------------
class InstrumentCache:
    """Caches one loader result for `ttl` seconds. Concurrent callers share a single in-flight load.

    A failed refresh never serves stale data (fail closed): the error propagates and the next
    call tries again.
    """

    def __init__(
        self,
        loader: Callable[[], Awaitable[list[InstrumentMeta]]],
        ttl_seconds: float,
        clock: Callable[[], float],
    ):
        self._loader = loader
        self._ttl = ttl_seconds
        self._clock = clock
        self._value: list[InstrumentMeta] | None = None
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    def _fresh(self) -> bool:
        return self._value is not None and (self._clock() - self._loaded_at) < self._ttl

    async def get(self) -> list[InstrumentMeta]:
        if not self._fresh():
            async with self._lock:
                if not self._fresh():
                    value = await self._loader()
                    self._value, self._loaded_at = value, self._clock()
        assert self._value is not None
        return list(self._value)

    def invalidate(self) -> None:
        self._value = None


class PublicMarketDataAdapter(ExchangeAdapter):
    """Common constructor + instrument cache. Account/order methods stay ExchangeNotSupported."""

    def __init__(
        self,
        *,
        base_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        instrument_ttl_seconds: float = DEFAULT_INSTRUMENT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.base_url = base_url
        self._now = now
        self._http = PublicHttp(
            self.name, base_url, transport=transport, timeout=timeout, max_retries=max_retries,
            backoff_seconds=backoff_seconds, sleep=sleep,
        )
        self._instruments = InstrumentCache(self._load_instruments, instrument_ttl_seconds, clock)
        self._candle_cache: dict[tuple, tuple[int, list]] = {}
        self._candle_reads: dict[tuple, asyncio.Task] = {}

    async def get_closed_candles(self, symbol: str, timeframe: str = "1m", limit: int = 200):
        """Share closed-candle reads only until the next candle boundary. Never caches executable books."""
        seconds = TIMEFRAME_MS.get(timeframe, 60_000) / 1000
        bucket = int(self._now().timestamp() // seconds)
        key = (symbol, timeframe, limit)
        cached = self._candle_cache.get(key)
        if cached is not None and cached[0] == bucket:
            return list(cached[1])

        async def load():
            values = await self.get_candles(symbol, timeframe, limit)
            if values and values[-1].open_time.timestamp() + seconds == bucket * seconds:
                self._candle_cache[key] = (bucket, list(values))
                while len(self._candle_cache) > 64:
                    self._candle_cache.pop(next(iter(self._candle_cache)))
            return values

        read_key = (*key, bucket)
        task = self._candle_reads.get(read_key)
        if task is None or task.done():
            task = self._candle_reads[read_key] = asyncio.create_task(load())
            def finished(done):
                if self._candle_reads.get(read_key) is done:
                    self._candle_reads.pop(read_key, None)
                if not done.cancelled():
                    done.exception()  # retrieves failures even if every waiting scan was cancelled
            task.add_done_callback(finished)
        return list(await asyncio.shield(task))

    @abstractmethod
    async def _load_instruments(self) -> list[InstrumentMeta]: ...

    async def list_instruments(self) -> list[InstrumentMeta]:
        """All perpetuals the venue lists, cached per adapter instance (default 10 minutes)."""
        return await self._instruments.get()

    async def close(self) -> None:
        tasks = list(self._candle_reads.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._candle_reads.clear()
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()
