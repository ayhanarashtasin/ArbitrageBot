"""Gate (gate.io) USDT-settled perpetual futures PUBLIC market-data adapter. name = "gate_usdt".

settle=usdt only: https://api.gateio.ws/api/v4/futures/usdt/... Every contract under /futures/{settle}/
is a PERPETUAL (the docs section is "Perpetual futures"; dated contracts live under /delivery/ and are
never touched). Contract names are BASE_USDT ("BTC_USDT", "1INCH_USDT", "AAPL_USDT", and a few
non-ASCII ones such as "龙虾_USDT"): base = the name without its "_USDT" suffix, quote = settle = USDT
(the settle currency of this endpoint family; the contract rows carry no separate quote field).
Zero Delta shows these markets as "Gate" with identifiers like "BTC/USDT:USDT".

Base URL: the docs list "Live trading: https://api.gateio.ws/api/v4" and a "Futures live trading
alternative (futures only): https://fx-api.gateio.ws/api/v4". The main host is the default because it
also serves /spot/time (the only documented time endpoint) and fx-api.gateio.ws did not resolve from
the development network. Testnet (docs): https://api-testnet.gateapi.io/api/v4, never a default.

Endpoints (all public, no key, no signature; checked against the official docs at
https://www.gate.com/docs/developers/apiv4/en/ (+ /futures/, /spot/) and live on 2026-09-30):
    GET /spot/time                       {"server_time": <epoch ms>}
    GET /futures/usdt/contracts          contract list, paginated with limit/offset. Live the server
                                         rejects limit > 100 ("invalid limit, must in (0, 100]") and
                                         answers [] past the end; rows come sorted by name.
    GET /futures/usdt/contracts/{name}   one contract: funding_rate, funding_interval,
                                         funding_next_apply, mark_price, index_price (get_funding_info)
    GET /futures/usdt/tickers?contract=  highest_bid, lowest_ask, last, mark_price, volume_24h_quote
    GET /futures/usdt/order_book         snapshot, bids high->low, asks low->high. The docs give no
                                         maximum for limit; live limit=300 is accepted and 1000 answers
                                         400 TOO_BIG "limit 300". Bounded here at 300.
    GET /futures/usdt/funding_rate       settled funding history, returned NEWEST first (re-ordered
                                         here); limit <= 1000 (1001 answers 400).
    GET /futures/usdt/candlesticks       ascending. "Maximum of 2000 points are returned in one query"
                                         (docs); live limit=2000 works, 2001 answers 400. `limit` must not
                                         be combined with from/to (docs), from/to are inclusive (live).

Rate limit (docs "Frequency limit rule"): public endpoints 200 requests / 10 s per endpoint per IP;
answers carry X-Gate-RateLimit-* headers (live: limit 200). Exceeding it gives HTTP 429
(TOO_MANY_REQUESTS), which the shared http policy never retries.

Errors (docs "Error Handling"): "For all abnormal requests, APIv4 will return non-2xx status code" with
a JSON body {"label": ..., "message": ...} (live: HTTP 400 {"label":"CONTRACT_NOT_FOUND"}). The shared
http policy turns any non-200 into ExchangeRequestError. A 200 body carrying "label" is also rejected
here (defensive; not observed).

SIZES AND BASE UNITS (the important Gate-specific part):
- Every size is a count of CONTRACTS. quanto_multiplier is "how many units of the underlying asset the
  face value of one contract represents" (docs), e.g. BTC_USDT 0.0001 BTC, ETH_USDT 0.01 ETH,
  PEPE_USDT 10,000,000 PEPE. contract_multiplier = quanto_multiplier, and every quantity this adapter
  returns (book levels, candle volume, qty_step, min_qty, max_qty) is contracts * multiplier = BASE
  units. Verified live: BTC 1m candle v=83206 contracts -> 8.3206 BTC, and sum (quote volume)
  690981 USDT / ~83040 USDT = 8.32 BTC.
- Fractional contract sizes: since API v4.106.0 size fields are strings and contracts with
  enable_decimal=true accept decimal sizes. Unless the request carries "X-Gate-Size-Decimal: 1" the
  server TRUNCATES sizes to integers (announcement 48788, 2025-12-18; live: ETH_USDT order_size_min
  "0.1" with the header, 0 without; book sizes "564.7" vs 564). The v4.106.0 changelog says all
  official SDKs send this header by default; this adapter sends it on every request.
- qty_step = ONE WHOLE CONTRACT (* multiplier). Integer-only contracts (enable_decimal=false) accept
  only whole contracts (docs). For enable_decimal contracts Gate publishes no size increment (only
  order_size_min, e.g. 0.1), so whole contracts are used: always valid, possibly coarser than the venue
  allows (PEPE_USDT: 10,000,000 PEPE, about 100 USDT). min_qty = max(order_size_min, 1 contract) *
  multiplier so it is always a multiple of qty_step. max_qty = the tighter of order_size_max and
  market_order_size_max ("0" = no separate market cap, docs) * multiplier. min_notional is not
  published: None.
- is_tradable = status == "trading" and not in_delisting. Documented statuses: prelaunch, trading,
  delisting, delisted, circuit_breaker. The undocumented is_pre_market flag (true for 13 pre-IPO style
  contracts on 2026-09-30, all status "trading") is not interpreted.

FUNDING RATE SEMANTICS: rates are FRACTIONS per funding interval ("-0.000027" = -0.0027 %); positive =
longs pay shorts (Gate help center 27569: "When the funding rate is positive, long position holders pay
fees to short position holders"). funding_rate is a running estimate: "The funding rate and premium
index are calculated every 60 seconds ... The funding rate used for final settlement is the result
from the last calculation within the current interval" (help center 27569). Live BTC_USDT it moved
-0.000042 -> -0.000037 -> -0.000027 within 10 minutes while the last settled rate was 0.000022. So
FundingInfo.funding_rate = funding_rate, the estimate for the interval settling at funding_next_apply,
and is_predicted=True. funding_rate_indicative is documented as "Indicative Funding rate in next
period. (deprecated. use funding_rate)" and was equal to funding_rate in every live sample: not used.
Settled rates come from get_funding_history().

FUNDING INTERVAL: contracts.funding_interval, documented "unit in seconds" (live: 28800 / 14400 / 3600
-> 8 / 4 / 1 h; a 14400 contract's settled history is spaced exactly 14400 s). Gate may shorten an
interval when the rate hits its cap (help center 27569), so get_funding_info() reads it from the same
fresh single-contract response as the rate, never from the instrument cache. Missing or 0 -> None in
InstrumentMeta and get_funding_info() raises instead of guessing. funding_next_apply is epoch SECONDS.

TIMESTAMPS: order_book.current ("Response data generation timestamp") is epoch seconds with ms
fraction and becomes OrderBook.source_time. Tickers and single-contract rows carry no data timestamp,
so Ticker.source_time and FundingInfo.source_time are None (received_at is always set).

Candle timeframes: only the documented intervals (1m 5m 15m 30m 1h 4h 8h 1d; each confirmed live).
3m and 2h also answered live but are undocumented and therefore not offered.

Fees: contracts rows carry taker_fee_rate / maker_fee_rate (0.00075 / -0.0001 for every contract live),
which do not match the published VIP0 schedule and are not the account's rate, so they are not used;
get_fee_schedule stays ExchangeNotSupported (the account rate needs the signed /futures/{settle}/fee).
"""

import asyncio
import logging
import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.domain.market import Candle, FundingInfo, InstrumentMeta, OrderBook, Ticker

from ._common import (
    TIMEFRAME_MS,
    PublicMarketDataAdapter,
    book_levels,
    check_positive,
    dec,
    is_closed,
    ms_to_dt,
    opt_dec,
    parsing,
    usd_volume,
)
from .base import ExchangeRequestError, SymbolNotFound

log = logging.getLogger(__name__)

_SETTLE = "USDT"
_FUTURES = "/futures/usdt"
_SIZE_DECIMAL_HEADER = "X-Gate-Size-Decimal"  # "1": sizes as exact decimal strings (see module docstring)
_CONTRACTS_PAGE_SIZE = 100  # live maximum (limit=101 answers 400)
_MAX_CONTRACT_PAGES = 50  # 5000 contracts; 1023 listed on 2026-09-30
_MAX_ORDERBOOK_DEPTH = 300  # live: 300 accepted, 1000 -> 400 TOO_BIG "limit 300"
_MAX_CANDLES_PER_REQUEST = 2000  # documented per-query maximum (live: 2001 -> 400)
_MAX_CANDLES = 5000  # per get_candles() call: at most 4 requests
_MAX_FUNDING_HISTORY = 1000  # live: 1001 -> 400
_TRADING_STATUS = "trading"
_INTERVALS = {  # project timeframe -> documented Gate interval (each confirmed live)
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "4h": "4h", "8h": "8h", "1d": "1d",
}
# Characters that cannot occur in a Gate contract name but could alter a URL path.
_UNSAFE_SYMBOL = re.compile(r"[\s/\\?#%&=.]")


def _seconds_to_dt(value: Any) -> datetime:
    """Epoch SECONDS (int or decimal fraction such as 1790757988.835) -> UTC datetime, exact to the ms."""
    return ms_to_dt(int(dec(value) * 1000))


def _opt_seconds_to_dt(value: Any) -> datetime | None:
    """Epoch seconds -> datetime; None, '' and 0 (venue 'no time') -> None."""
    if value is None or value == "":
        return None
    seconds = dec(value)
    return _seconds_to_dt(seconds) if seconds > 0 else None


def _interval_hours(raw: dict) -> Decimal | None:
    """contracts.funding_interval (documented in SECONDS) -> hours; missing / 0 -> None."""
    seconds = opt_dec(raw.get("funding_interval"))
    return seconds / 3600 if seconds is not None and seconds > 0 else None


class GateUsdtFuturesAdapter(PublicMarketDataAdapter):
    name = "gate_usdt"
    DEFAULT_BASE_URL = "https://api.gateio.ws/api/v4"
    FUTURES_ALT_BASE_URL = "https://fx-api.gateio.ws/api/v4"  # docs: futures only (no /spot/time)
    TESTNET_BASE_URL = "https://api-testnet.gateapi.io/api/v4"  # docs "Access URL"; never a default

    def __init__(self, *, base_url: str = DEFAULT_BASE_URL, **kwargs):
        super().__init__(base_url=base_url, **kwargs)
        # Without this header Gate truncates every futures size to an integer (module docstring).
        # PublicHttp has no per-adapter header option, so it is set on its client directly.
        self._http._http.headers[_SIZE_DECIMAL_HEADER] = "1"

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        body = await self._http.get_json(path, params=params)
        if isinstance(body, dict) and "label" in body:  # documented error shape (normally non-2xx)
            label = str(body.get("label"))[:60]
            message = str(body.get("message") or body.get("detail") or "")[:120]
            if label == "TOO_MANY_REQUESTS":
                raise ExchangeRequestError(f"{self.name}: rate limit ({label}) on {path}; not retried")
            raise ExchangeRequestError(
                f"{self.name}: {path} failed (label={label}{', message=' + message if message else ''})"
            )
        return body

    def _check_symbol(self, symbol: str) -> str:
        if not symbol or _UNSAFE_SYMBOL.search(symbol):
            raise SymbolNotFound(f"{self.name}: not a Gate contract name: {symbol!r}")
        return symbol

    # ---- time -----------------------------------------------------------------------------
    async def get_server_time(self) -> datetime:
        data = await self._get("/spot/time")
        with parsing(self.name, "server time"):
            return ms_to_dt(data["server_time"])

    # ---- instruments ----------------------------------------------------------------------
    async def _load_instruments(self) -> list[InstrumentMeta]:
        rows: list[dict] = []
        page_size = _CONTRACTS_PAGE_SIZE
        for page in range(_MAX_CONTRACT_PAGES):
            batch = await self._get(f"{_FUTURES}/contracts", {"limit": page_size, "offset": page * page_size})
            with parsing(self.name, "contracts"):
                if not isinstance(batch, list):
                    raise TypeError(f"expected a list, got {type(batch).__name__}")
                rows.extend(batch)
            if len(batch) < page_size:
                break
        else:
            raise ExchangeRequestError(f"{self.name}: contracts pagination did not terminate")
        received_at = self._now()
        result: list[InstrumentMeta] = []
        seen: set[str] = set()
        for raw in rows:
            try:
                meta = self._parse_instrument(raw, received_at)
            except (KeyError, IndexError, TypeError, ValueError, ArithmeticError, AttributeError) as exc:
                # One malformed row must not hide every other market; that symbol simply cannot resolve.
                log.warning("%s: skipping unparseable contract %r (%s)", self.name,
                            raw.get("name") if isinstance(raw, dict) else raw, type(exc).__name__)
                continue
            if meta.symbol not in seen:  # a listing added mid-pagination shifts offsets by one row
                seen.add(meta.symbol)
                result.append(meta)
        if rows and not result:
            raise ExchangeRequestError(f"{self.name}: no contracts row could be parsed")
        return result

    def _parse_instrument(self, raw: dict, received_at: datetime) -> InstrumentMeta:
        name = raw["name"]
        suffix = "_" + _SETTLE
        if not name.endswith(suffix) or len(name) == len(suffix):
            raise ValueError(f"unexpected contract name {name!r}")
        multiplier = dec(raw["quanto_multiplier"])  # base units per contract
        if multiplier <= 0:
            raise ValueError(f"quanto_multiplier must be > 0, got {multiplier}")
        min_contracts = max(dec(raw["order_size_min"]), Decimal(1))  # whole-contract step, see docstring
        max_contracts = dec(raw["order_size_max"])
        market_max = opt_dec(raw.get("market_order_size_max"))
        if market_max:  # "0" = market orders limited by order_size_max only (docs)
            max_contracts = min(max_contracts, market_max)
        return InstrumentMeta(
            venue=self.name,
            symbol=name,
            base=name[: -len(suffix)],
            quote=_SETTLE,
            settle=_SETTLE,
            is_linear=raw["type"] == "direct",
            is_perpetual=True,
            is_tradable=raw["status"] == _TRADING_STATUS and raw.get("in_delisting") is not True,
            contract_multiplier=multiplier,
            tick_size=dec(raw["order_price_round"]),
            qty_step=multiplier,  # one whole contract
            min_qty=min_contracts * multiplier,
            max_qty=max_contracts * multiplier,
            min_notional=None,  # not published
            funding_interval_hours=_interval_hours(raw),
            max_leverage=opt_dec(raw.get("leverage_max")),
            underlying_type="EQUITY" if raw.get("contract_type") == "stocks" else None,
            received_at=received_at,
        )

    # ---- funding --------------------------------------------------------------------------
    async def get_funding_info(self, symbol: str) -> FundingInfo:
        """Rate, interval and next funding time from ONE fresh single-contract response."""
        row = await self._get(f"{_FUTURES}/contracts/{self._check_symbol(symbol)}")
        with parsing(self.name, "contract"):
            if row["name"] != symbol:
                raise ExchangeRequestError(f"{self.name}: contract returned {row['name']} for {symbol}")
            interval_hours = _interval_hours(row)
            funding_rate = dec(row["funding_rate"])
            next_time = _opt_seconds_to_dt(row.get("funding_next_apply"))
            mark, index = opt_dec(row.get("mark_price")), opt_dec(row.get("index_price"))
        if interval_hours is None:
            raise ExchangeRequestError(f"{self.name}: no funding interval published for {symbol}")
        return FundingInfo(
            venue=self.name,
            symbol=symbol,
            funding_rate=funding_rate,  # running estimate, see module docstring
            interval_hours=interval_hours,
            next_funding_time=next_time,
            is_predicted=True,
            mark_price=mark,
            index_price=index,
            source_time=None,  # the contract row carries no data timestamp
            received_at=self._now(),
        )

    async def get_funding_history(self, symbol: str, limit: int = 100) -> list[tuple[datetime, Decimal]]:
        """Settled funding, oldest first. limit <= 1000."""
        check_positive("limit", limit, _MAX_FUNDING_HISTORY)
        rows = await self._get(f"{_FUTURES}/funding_rate", {"contract": self._check_symbol(symbol), "limit": limit})
        with parsing(self.name, "funding_rate history"):
            history = [(_seconds_to_dt(r["t"]), dec(r["r"])) for r in rows]
        history.sort(key=lambda item: item[0])  # the venue returns newest first
        return history

    # ---- ticker ---------------------------------------------------------------------------
    async def get_ticker(self, symbol: str) -> Ticker:
        rows = await self._get(f"{_FUTURES}/tickers", {"contract": self._check_symbol(symbol)})
        with parsing(self.name, "tickers"):
            if len(rows) != 1 or rows[0]["contract"] != symbol:
                raise ExchangeRequestError(f"{self.name}: tickers returned no unique row for {symbol}")
            row = rows[0]
            return Ticker(
                venue=self.name,
                symbol=symbol,
                bid=opt_dec(row.get("highest_bid")),
                ask=opt_dec(row.get("lowest_ask")),
                last=opt_dec(row.get("last")),
                mark=opt_dec(row.get("mark_price")),
                # volume_24h_quote: "24-hour trading volume in quote currency" (USDT)
                volume_24h_usd=usd_volume(_SETTLE, opt_dec(row.get("volume_24h_quote"))),
                source_time=None,  # the ticker carries no timestamp
                received_at=self._now(),
            )

    # ---- order book -----------------------------------------------------------------------
    async def get_order_book(self, symbol: str, depth: int = 50) -> OrderBook:
        """Snapshot with up to `depth` (<= 300) levels per side, quantities in BASE units
        (contracts * quanto_multiplier). source_time = `current`, when Gate generated the response."""
        check_positive("depth", depth, _MAX_ORDERBOOK_DEPTH)
        data, meta = await asyncio.gather(
            self._get(f"{_FUTURES}/order_book", {"contract": self._check_symbol(symbol), "limit": depth}),
            self.get_instrument_metadata(symbol),
        )
        multiplier = meta.contract_multiplier
        with parsing(self.name, "order_book"):
            def price(row):
                return row["p"]

            def base_qty(row):
                return dec(row["s"]) * multiplier

            return OrderBook(
                venue=self.name,
                symbol=symbol,
                bids=book_levels(data["bids"], descending=True, depth=depth, price_of=price, qty_of=base_qty),
                asks=book_levels(data["asks"], descending=False, depth=depth, price_of=price, qty_of=base_qty),
                source_time=_opt_seconds_to_dt(data.get("current") or data.get("update")),
                received_at=self._now(),
            )

    # ---- candles --------------------------------------------------------------------------
    async def get_candles(self, symbol: str, timeframe: str = "1m", limit: int = 200,
                          include_open: bool = False) -> list[Candle]:
        """Oldest first. Only CLOSED candles by default (the still-forming candle is dropped);
        include_open=True keeps it. limit <= 5000. volume is BASE units (contracts * multiplier).

        Up to 1999 candles take one request (limit=N+1). Larger requests walk back in from/to windows of
        2000 points (the documented per-query maximum) until `limit` closed candles are collected or
        the venue has no older data; at most 4 requests.
        """
        interval = _INTERVALS.get(timeframe)
        if interval is None:
            raise ValueError(f"{self.name}: unsupported timeframe {timeframe!r}; use one of {sorted(_INTERVALS)}")
        check_positive("limit", limit, _MAX_CANDLES)
        self._check_symbol(symbol)
        now = self._now()
        if limit + 1 <= _MAX_CANDLES_PER_REQUEST:
            rows, meta = await asyncio.gather(
                self._get(f"{_FUTURES}/candlesticks", {"contract": symbol, "interval": interval, "limit": limit + 1}),
                self.get_instrument_metadata(symbol),
            )
            candles = self._parse_candles(symbol, rows, meta.contract_multiplier)
        else:
            meta = await self.get_instrument_metadata(symbol)
            candles = await self._candles_by_window(symbol, interval, timeframe, limit, meta.contract_multiplier, now)
        candles.sort(key=lambda c: c.open_time)
        if not include_open:
            candles = [c for c in candles if is_closed(c.open_time, timeframe, now)]
        return candles[-limit:]

    async def _candles_by_window(self, symbol: str, interval: str, timeframe: str, limit: int,
                                 multiplier: Decimal, now: datetime) -> list[Candle]:
        step = TIMEFRAME_MS[timeframe] // 1000
        per_request = _MAX_CANDLES_PER_REQUEST
        now_s = int(now.timestamp())
        to = now_s - now_s % step  # open time of the still-forming candle (UTC-aligned intervals)
        pages = -(-(limit + 1) // per_request) + 1  # one spare page absorbs local/venue clock skew
        by_open: dict[datetime, Candle] = {}
        for _ in range(pages):
            start = to - (per_request - 1) * step  # from and to are both inclusive (verified live)
            rows = await self._get(
                f"{_FUTURES}/candlesticks", {"contract": symbol, "interval": interval, "from": start, "to": to}
            )
            batch = self._parse_candles(symbol, rows, multiplier)
            for candle in batch:
                by_open[candle.open_time] = candle
            closed = sum(1 for t in by_open if is_closed(t, timeframe, now))
            if not batch or closed >= limit:
                break
            to = start - step
        return list(by_open.values())

    def _parse_candles(self, symbol: str, rows: Any, multiplier: Decimal) -> list[Candle]:
        with parsing(self.name, "candlesticks"):
            return [
                Candle(
                    venue=self.name, symbol=symbol, open_time=_seconds_to_dt(r["t"]),
                    open=dec(r["o"]), high=dec(r["h"]), low=dec(r["l"]), close=dec(r["c"]),
                    volume=dec(r["v"]) * multiplier,  # v is a contract count
                )
                for r in rows
            ]
