"""The signed half of a venue: account, positions, orders (project.md phase 8).

Every venue client turns its own API into ONE shape, in BASE asset units and USD amounts, so the live leg
(app/execution/live.py) never sees a venue's contract sizes, side codes or status names.

Safety rules shared by every client:
- Order placement is NEVER retried automatically. A timeout, transport failure or HTTP 5xx on an order request means
  "outcome unknown": the caller queries the order by its client order id (pair_executor does exactly that).
- "The order does not exist" is reported as OrderNotFound only when the venue says so explicitly. Anything else that
  prevents an answer is an ExchangeRequestError, so an unknown outcome is never mistaken for "not placed".
- An explicit refusal (insufficient margin, reduce-only would open, size too small, ...) is OrderRejected.
- Keys, secrets, passphrases, signatures and full URLs never appear in an error message or a log line.
- Reads (GET) are retried at most twice on timeouts, transport errors and 5xx; rate-limit answers are never retried.
"""

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx

from app.domain.market import (
    FeeSchedule,
    InstrumentMeta,
    OrderState,
    PositionState,
    Side,
)
from app.exchanges._common import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_KEEPALIVE_EXPIRY_SECONDS,
)
from app.exchanges.base import ExchangeError, ExchangeRequestError

log = logging.getLogger(__name__)
D = Decimal

PRIVATE_TIMEOUT_SECONDS = 10.0
READ_RETRIES = 2
READ_BACKOFF_SECONDS = 0.5
USER_AGENT = "funding-arbitrage-bot/0.1"


class OrderRejected(ExchangeError):
    """The venue explicitly refused the order: nothing was placed."""


class OrderNotFound(ExchangeError):
    """The venue explicitly says it has no order with this id."""


class AuthError(ExchangeError):
    """Key, signature, permission or IP whitelist problem. Never retried; a human must fix the key."""


@dataclass(frozen=True)
class AccountSnapshot:
    equity_usd: Decimal  # the futures account's total equity (wallet + unrealised P&L) in USD terms
    available_usd: Decimal  # margin free for new positions
    maintenance_margin_usd: Decimal | None  # None = the venue does not report it

    @property
    def margin_ratio(self) -> Decimal | None:
        """Maintenance margin / equity, the venues' margin ratio (1 = liquidation)."""
        if self.maintenance_margin_usd is None:
            return None
        return self.maintenance_margin_usd / self.equity_usd if self.equity_usd > 0 else D(1)


@dataclass(frozen=True)
class PrivateFill:
    """One execution, normalised. quantity is BASE units; fee is in fee_asset (positive = paid)."""

    trade_id: str
    order_id: str
    client_order_id: str | None
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    fee: Decimal
    fee_asset: str
    time: datetime


@dataclass(frozen=True)
class FundingIncome:
    """One funding settlement on the account, positive = received."""

    time: datetime
    symbol: str
    amount: Decimal
    asset: str
    ref: str  # the venue's id for the record, so it is never counted twice


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    ok: bool
    detail: str


@dataclass
class Readiness:
    venue: str
    checks: list[ReadinessCheck] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str) -> bool:
        self.checks.append(ReadinessCheck(name, ok, detail))
        return ok

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)


class PrivateClient(ABC):
    """Signed access to one venue's USDT-margined perpetuals, one-way position mode, isolated margin."""

    name: str  # the PUBLIC adapter name of the same venue, e.g. "binance_usdt_m"
    settle_assets: frozenset[str] = frozenset({"USDT"})  # live trading is limited to these settlement assets
    # "No such order" right after an unanswered request is believed only after this long: the venue may still be
    # processing it (Binance: only safe once the request's recvWindow has passed).
    not_found_grace_seconds: float = 2.0
    offset_ms: int = 0  # venue clock minus local clock, measured by readiness() and used for every signature

    def now_ms(self) -> int:
        import time
        return int(time.time() * 1000) + self.offset_ms

    @abstractmethod
    async def readiness(self) -> Readiness:
        """Read-only checks that the key works and the account is set up the way the bot needs. Never trades."""

    @abstractmethod
    async def account(self) -> AccountSnapshot: ...

    @abstractmethod
    async def position(self, meta: InstrumentMeta) -> PositionState | None:
        """The venue's own position in this symbol, signed BASE units; None when flat."""

    @abstractmethod
    async def open_orders(self, meta: InstrumentMeta) -> list[OrderState]: ...

    @abstractmethod
    async def prepare_symbol(self, meta: InstrumentMeta, leverage: Decimal) -> None:
        """Isolated margin and `leverage` for this symbol, in one-way mode. Raises if that cannot be guaranteed."""

    @abstractmethod
    async def place_market_order(self, meta: InstrumentMeta, side: Side, quantity: Decimal, *, client_order_id: str,
                                 reduce_only: bool) -> str:
        """Send a MARKET order for `quantity` BASE units. Returns the venue order id. Never retried."""

    @abstractmethod
    async def get_order(self, meta: InstrumentMeta, client_order_id: str) -> OrderState:
        """The order's current state (BASE units). OrderNotFound only when the venue explicitly says so."""

    @abstractmethod
    async def fills(self, meta: InstrumentMeta, *, order_id: str | None = None,
                    since: datetime | None = None) -> list[PrivateFill]: ...

    @abstractmethod
    async def cancel_order(self, meta: InstrumentMeta, client_order_id: str) -> None: ...

    @abstractmethod
    async def fee_schedule(self, meta: InstrumentMeta) -> FeeSchedule: ...

    @abstractmethod
    async def funding_income(self, meta: InstrumentMeta, since: datetime) -> list[FundingIncome]: ...

    async def close(self) -> None:
        return None


def to_contracts(meta: InstrumentMeta, quantity: Decimal) -> Decimal:
    """BASE units -> venue units (contracts; equal to BASE units when contract_multiplier is 1).

    The quantity must be a whole number of the venue's lots: the executor sizes in lots, so anything else is a bug and
    is refused before it reaches the venue."""
    lots = quantity / meta.qty_step
    if lots != lots.to_integral_value():
        raise OrderRejected(f"{meta.venue}: {quantity} is not a whole number of lots of {meta.qty_step}")
    return quantity / meta.contract_multiplier


def plain(value: Decimal) -> str:
    """A decimal as venues accept it: no exponent, no trailing zeros ("10", "0.001", never "1E+1")."""
    text = format(value.normalize(), "f")
    return text


def query_string(params: dict[str, Any] | None, *, sort: bool = False) -> str:
    """`a=1&b=2` exactly as it will be sent and signed. Values are str() of the given objects; None values are dropped.
    Only URL-safe characters are expected (symbols, numbers, ids); anything else is percent-encoded."""
    from urllib.parse import quote
    if not params:
        return ""
    items = [(k, v) for k, v in params.items() if v is not None]
    if sort:
        items.sort(key=lambda kv: kv[0])
    return "&".join(f"{quote(str(k), safe='-_.')}={quote(str(v), safe='-_.')}" for k, v in items)


# signer(method, path, query, body_text) -> (headers, query actually sent). The body is sent exactly as signed.
Signer = Callable[[str, str, str, str], tuple[dict[str, str], str]]


class SignedHttp:
    """Async JSON client for one venue's signed REST API.

    The query string is built here once (`query_string`), signed by the venue's signer and sent byte for byte as signed
    (Binance appends its signature to it). httpx never re-encodes it.
    """

    def __init__(self, venue: str, base_url: str, signer: Signer, *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = PRIVATE_TIMEOUT_SECONDS,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.venue = venue
        self._sign = signer
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            base_url=base_url, transport=transport,
            timeout=httpx.Timeout(timeout, connect=min(timeout, DEFAULT_CONNECT_TIMEOUT_SECONDS)),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=10,
                                keepalive_expiry=DEFAULT_KEEPALIVE_EXPIRY_SECONDS),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"})

    async def aclose(self) -> None:
        await self._http.aclose()

    async def request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                      body: dict[str, Any] | list | None = None, order: bool = False,
                      sort_params: bool = False) -> httpx.Response:
        """Send a signed request and return the raw response (the venue client reads its own envelope).

        order=True marks a request that changes state (place / cancel / settings): it is never retried, and any
        failure to get an answer is "outcome unknown" (ExchangeRequestError).
        """
        body_text = json.dumps(body, separators=(",", ":")) if body is not None else ""
        query = query_string(params, sort=sort_params)
        attempts = 1 if (order or method != "GET") else READ_RETRIES + 1
        for attempt in range(attempts):
            last = attempt == attempts - 1
            headers, sent_query = self._sign(method, path, query, body_text)  # re-signed: timestamps move on retry
            if body is not None:
                headers = {**headers, "Content-Type": "application/json"}
            url = f"{path}?{sent_query}" if sent_query else path
            try:
                resp = await self._http.request(method, url, headers=headers,
                                                content=body_text.encode() if body is not None else None)
            except httpx.TimeoutException as exc:
                if last:
                    raise ExchangeRequestError(f"{self.venue}: request timed out ({method} {path})") from exc
            except httpx.TransportError as exc:
                if last:
                    raise ExchangeRequestError(f"{self.venue}: connection failed ({type(exc).__name__}) "
                                               f"({method} {path})") from exc
            except httpx.HTTPError as exc:
                raise ExchangeRequestError(f"{self.venue}: HTTP client error ({type(exc).__name__}) "
                                           f"({method} {path})") from exc
            else:
                if resp.status_code in (418, 429):
                    raise ExchangeRequestError(f"{self.venue}: rate limited (HTTP {resp.status_code}) on {method} "
                                               f"{path}; not retried")
                if resp.status_code >= 500 or resp.status_code == 408:
                    if last:
                        raise ExchangeRequestError(f"{self.venue}: server error (HTTP {resp.status_code}) on {method} "
                                                   f"{path}: outcome unknown")
                else:
                    return resp
            delay = READ_BACKOFF_SECONDS * (2 ** attempt)
            log.warning("%s: %s %s failed, retry %d/%d in %.1fs", self.venue, method, path, attempt + 1,
                        attempts - 1, delay)
            await self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover


def json_body(venue: str, resp: httpx.Response, what: str) -> Any:
    try:
        return json.loads(resp.content, parse_float=Decimal)
    except ValueError as exc:
        raise ExchangeRequestError(f"{venue}: malformed JSON from {what} (HTTP {resp.status_code})") from exc
