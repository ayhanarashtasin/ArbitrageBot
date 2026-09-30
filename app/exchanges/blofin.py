"""BloFin perpetual-swap PUBLIC market-data adapter. name = "blofin_swap".

Covers every LINEAR perpetual on https://openapi.blofin.com: USDT-margined (BTC-USDT) and
USDC-margined (BTC-USDC) swaps. Quote and settle come from each instrument's own quoteCurrency /
settleCurrency (there is no separate margin-currency field), never from the instId string. Inverse
coin-margined swaps (BTC-USD, quote USD / settle BTC) are ignored.

Endpoints (all public, no key, no signature; verified against https://docs.blofin.com/ and live
2026-09-30):
    GET /api/v1/market/instruments           instruments (unfiltered: all 515 in one response, no paging)
    GET /api/v1/market/funding-rate          current funding + fundingInterval/fundingIntervalUnit
                                             (without instId: every instrument in one response)
    GET /api/v1/market/funding-rate-history  settled funding, max 100 rows per call (newest first)
    GET /api/v1/market/mark-price            mark, index, ts ("data return time")
    GET /api/v1/market/tickers               last, best bid/ask, 24h base/contract volume, ts
    GET /api/v1/market/books                 book snapshot, size <= 100 levels per side (documented)
    GET /api/v1/market/candles               candles, max 1440 rows per call (documented; verified live:
                                             limit=1440 -> 1440 rows, limit=1441 silently capped at 1440),
                                             newest first, paged backwards with `after` (exclusive)

BloFin reports application errors as HTTP 200 with code != "0" (e.g. code=152002 "Parameter instId
error." for an unknown instId, verified live), so every response envelope is checked. Rate limit is
500 requests/min and 1500/5 min per IP; a breach answers HTTP 429 (code 429) and suspends the IP for
5 min / 1 h, HTTP 403 means a firewall rule was hit (five-minute ban). Neither is ever retried.

Conventions verified live:
- SIZES ARE CONTRACTS. contractValue is base units per contract (BTC-USDT 0.001 BTC, BTC-USDC 0.0001
  BTC, SHIB-USDT 1,000,000 SHIB). contract_multiplier = contractValue; qty_step = lotSize x
  contractValue; min_qty = minSize x contractValue; max_qty = min(maxLimitSize, maxMarketSize) x
  contractValue. Book sizes are contracts and are converted to BASE units here.
- Candle volume: index 6 volCurrency is documented and verified as BASE currency (BTC-USDT 1m
  vol=221 contracts, volCurrency=0.2218 BTC; SHIB-USDT 1H vol=3423, volCurrency=3423300000 SHIB).
  vol (contracts) is truncated to whole contracts, so volCurrency is used, not vol x contractValue.
- Candles carry confirm ("1" closed, "0" forming). 1m responses omit the forming minute; 1H and most
  other bars include it with confirm="0". All bars are UTC-aligned (1D opens 00:00 UTC).
- Funding rate is a FRACTION per funding interval. Positive = long pays short.
- min_notional is not published (None).

FUNDING RATE SEMANTICS (get_funding_info): funding-rate.fundingRate is documented as "Current funding
rate" and fundingTime as the settlement time. Measured live, fundingTime is the NEXT settlement
(16:00 UTC while funding-rate-history's newest row is the 08:00 UTC settlement) and fundingRate moves
between polls (ETH-USDT 0.00016508 -> 0.00016505 within 3 minutes) and differs from the last settled
rate: it is the running estimate for the interval that settles at fundingTime, so is_predicted=True.
Settled rates ("Actual funding rate") come from get_funding_history().

FUNDING INTERVAL: from funding-rate fundingInterval + fundingIntervalUnit (documented units: minute,
hour; live: 219 x 8 h, 294 x 4 h, 2 x 1 h). InstrumentMeta.funding_interval_hours comes from the
unfiltered funding-rate call made with the instrument load (cached with it). get_funding_info() uses
the interval returned in its own per-symbol call and falls back to the cached value only when that
row omits it. No default is assumed: without an interval get_funding_info() raises.

SERVER TIME: BloFin documents no server-time endpoint. get_server_time() returns mark-price's ts for
BTC-USDT, documented as "Data return time"; measured live it advances with every request (three calls
~300 ms apart -> ts deltas 303 / 298 ms), so it is stamped when the response is produced.

24H VOLUME: tickers only publishes 24h volume in base currency (volCurrency24h) and contracts (vol24h);
no quote-currency figure. volume_24h_usd is therefore the sum of the documented volCurrencyQuote over
the 24 most recent CLOSED 1H candles (window ends at the last full hour, so it lags by < 1 h), for
USD-like quotes only. Live check: 200.4M USDT vs 196.4M from volCurrency24h x last.

Ticker source_time is tickers.ts ("ticker data generation time"); observed live up to ~16 s behind
mark-price ts for BTC-USDT while bid/ask still matched the book, so it can look older than it is.

Not provided: fees (https://blofin.com/fees, checked 2026-09-30, lists the general / VIP0 futures rate
maker 0.0002 / taker 0.0006, i.e. 2 / 6 bps, plus temporary per-symbol promotions; the account's real
tier needs a signed call, so get_fee_schedule stays ExchangeNotSupported). A scheduled delisting
(instruments offTime) is not reflected in is_tradable: state "live" is tradable until BloFin changes it.
"""

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.domain.market import Candle, FundingInfo, InstrumentMeta, OrderBook, Ticker

from ._common import (
    PublicMarketDataAdapter,
    book_levels,
    check_positive,
    dec,
    is_closed,
    ms_to_dt,
    opt_dec,
    opt_ms_to_dt,
    parsing,
    usd_volume,
)
from .base import ExchangeRequestError

log = logging.getLogger(__name__)

_MARKET = "/api/v1/market"
_SWAP = "SWAP"
_LINEAR = "linear"
_LIVE = "live"
_TIME_REFERENCE = "BTC-USDT"  # most liquid swap; its mark-price ts is the server clock
_MAX_BOOK_DEPTH = 100  # documented maximum for books `size`
_MAX_FUNDING_HISTORY = 100  # documented per-call maximum
_CANDLES_PER_CALL = 1440  # documented maximum for candles `limit` (verified live)
_MAX_CANDLES = 5000  # bounded: at most ceil(5001 / 1440) + 1 = 5 requests per get_candles call
_VOLUME_HOURS = 24
_RATE_LIMIT_CODES = frozenset({"429"})  # documented "Too Many Requests"
_UNITS_PER_HOUR = {"hour": 1, "minute": 60}  # documented fundingIntervalUnit values (divide: exact)
_BARS = {  # project timeframe -> BloFin bar (each verified live, UTC-aligned)
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1H", "2h": "2H",
    "4h": "4H", "6h": "6H", "8h": "8H", "12h": "12H", "1d": "1D",
}


def _interval_hours(row: dict) -> Decimal | None:
    """funding-rate fundingInterval + fundingIntervalUnit -> hours; None when absent or unknown."""
    value, unit = row.get("fundingInterval"), row.get("fundingIntervalUnit")
    if value in (None, "") or unit not in _UNITS_PER_HOUR:
        if value not in (None, ""):
            log.warning("blofin_swap: unknown fundingIntervalUnit %r for %r", unit, row.get("instId"))
        return None
    hours = dec(value) / _UNITS_PER_HOUR[unit]
    return hours if hours > 0 else None


class BlofinSwapAdapter(PublicMarketDataAdapter):
    name = "blofin_swap"
    DEFAULT_BASE_URL = "https://openapi.blofin.com"
    DEMO_BASE_URL = "https://demo-trading-openapi.blofin.com"  # docs "Demo Trading"; never a default

    def __init__(self, *, base_url: str = DEFAULT_BASE_URL, **kwargs):
        super().__init__(base_url=base_url, **kwargs)

    async def _call(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET /api/v1/market/<endpoint> and return the envelope, raising on code != "0"."""
        path = f"{_MARKET}/{endpoint}"
        body = await self._http.get_json(path, params=params)
        with parsing(self.name, path):
            code = str(body["code"])
            if code == "0":
                return body
            message = str(body.get("msg", ""))[:120]
        if code in _RATE_LIMIT_CODES:
            raise ExchangeRequestError(f"{self.name}: rate limit (code={code}) on {path}; not retried")
        raise ExchangeRequestError(f"{self.name}: {path} failed (code={code}, msg={message})")

    def _one_row(self, body: dict, symbol: str, what: str) -> dict:
        rows = body["data"]
        if len(rows) != 1 or rows[0]["instId"] != symbol:
            raise ExchangeRequestError(f"{self.name}: {what} returned no unique row for {symbol}")
        return rows[0]

    # ---- time -----------------------------------------------------------------------------
    async def get_server_time(self) -> datetime:
        """mark-price ts ("data return time") of BTC-USDT: BloFin has no time endpoint."""
        body = await self._call("mark-price", {"instId": _TIME_REFERENCE})
        with parsing(self.name, "mark-price"):
            return ms_to_dt(self._one_row(body, _TIME_REFERENCE, "mark-price")["ts"])

    # ---- instruments ----------------------------------------------------------------------
    async def _load_instruments(self) -> list[InstrumentMeta]:
        inst_body, funding_body = await asyncio.gather(
            self._call("instruments", {}), self._call("funding-rate", {})
        )
        received_at = self._now()
        with parsing(self.name, "funding-rate"):
            intervals = {row["instId"]: _interval_hours(row) for row in funding_body["data"]}
        with parsing(self.name, "instruments"):
            swaps = [r for r in inst_body["data"]
                     if r.get("instType") == _SWAP and r.get("contractType") == _LINEAR]
        result: list[InstrumentMeta] = []
        for raw in swaps:
            try:
                result.append(self._parse_instrument(raw, intervals.get(raw["instId"]), received_at))
            except (KeyError, IndexError, TypeError, ValueError, ArithmeticError) as exc:
                # One malformed row must not hide every other market; that symbol simply cannot resolve.
                log.warning("%s: skipping unparseable instrument %r (%s)", self.name, raw.get("instId"),
                            type(exc).__name__)
        if swaps and not result:
            raise ExchangeRequestError(f"{self.name}: no instruments row could be parsed")
        return result

    def _parse_instrument(self, raw: dict, interval_hours: Decimal | None, received_at: datetime) -> InstrumentMeta:
        contract_value = dec(raw["contractValue"])
        if contract_value <= 0:
            raise ValueError(f"contractValue must be positive, got {contract_value}")
        caps = [v for v in (opt_dec(raw.get("maxLimitSize")), opt_dec(raw.get("maxMarketSize"))) if v is not None]
        return InstrumentMeta(
            venue=self.name,
            symbol=raw["instId"],
            base=raw["baseCurrency"],
            quote=raw["quoteCurrency"],
            settle=raw["settleCurrency"],
            is_linear=True,
            is_perpetual=True,
            is_tradable=raw["state"] == _LIVE,
            contract_multiplier=contract_value,
            tick_size=dec(raw["tickSize"]),
            qty_step=dec(raw["lotSize"]) * contract_value,
            min_qty=dec(raw["minSize"]) * contract_value,
            # tighter of the limit and market caps: a quantity <= max_qty is accepted for both
            max_qty=min(caps) * contract_value if caps else None,
            min_notional=None,  # not published
            funding_interval_hours=interval_hours,
            max_leverage=opt_dec(raw.get("maxLeverage")),
            received_at=received_at,
        )

    # ---- funding --------------------------------------------------------------------------
    async def get_funding_info(self, symbol: str) -> FundingInfo:
        funding_body, mark_body = await asyncio.gather(
            self._call("funding-rate", {"instId": symbol}), self._call("mark-price", {"instId": symbol})
        )
        with parsing(self.name, "funding-rate"):
            row = self._one_row(funding_body, symbol, "funding-rate")
            funding_rate = dec(row["fundingRate"])
            next_time = opt_ms_to_dt(row["fundingTime"])
            interval_hours = _interval_hours(row)
        with parsing(self.name, "mark-price"):
            mark_row = self._one_row(mark_body, symbol, "mark-price")
            mark, index = opt_dec(mark_row.get("markPrice")), opt_dec(mark_row.get("indexPrice"))
            source_time = opt_ms_to_dt(mark_row.get("ts"))
        if interval_hours is None:
            interval_hours = (await self.get_instrument_metadata(symbol)).funding_interval_hours
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
            source_time=source_time,  # funding-rate carries no ts; mark-price's, fetched alongside
            received_at=self._now(),
        )

    async def get_funding_history(self, symbol: str, limit: int = 100) -> list[tuple[datetime, Decimal]]:
        """Settled funding, oldest first. limit <= 100 (documented per-call maximum)."""
        check_positive("limit", limit, _MAX_FUNDING_HISTORY)
        body = await self._call("funding-rate-history", {"instId": symbol, "limit": limit})
        with parsing(self.name, "funding-rate-history"):
            history = []
            for r in body["data"]:
                if r["instId"] != symbol:
                    raise ExchangeRequestError(f"{self.name}: funding-rate-history returned {r['instId']}")
                history.append((ms_to_dt(r["fundingTime"]), dec(r["fundingRate"])))
        history.sort(key=lambda item: item[0])  # the venue returns newest first
        return history

    # ---- ticker ---------------------------------------------------------------------------
    async def _quote_volume_24h(self, symbol: str) -> Decimal | None:
        """Sum of volCurrencyQuote over the 24 most recent closed 1H candles (None if none closed)."""
        body = await self._call("candles", {"instId": symbol, "bar": _BARS["1h"], "limit": _VOLUME_HOURS + 1})
        with parsing(self.name, "candles"):
            closed = [(int(r[0]), dec(r[7])) for r in body["data"] if r[8] == "1"]
        if not closed:
            return None
        newest = max(ts for ts, _ in closed)
        window_start = newest - (_VOLUME_HOURS - 1) * 3_600_000  # guards against gaps in the series
        return sum((vol for ts, vol in closed if ts >= window_start), Decimal(0))

    async def get_ticker(self, symbol: str) -> Ticker:
        """last/bid/ask from tickers, mark from mark-price, 24h quote volume from 1H candles."""
        ticker_body, mark_body, quote_volume, meta = await asyncio.gather(
            self._call("tickers", {"instId": symbol}),
            self._call("mark-price", {"instId": symbol}),
            self._quote_volume_24h(symbol),
            self.get_instrument_metadata(symbol),
        )
        with parsing(self.name, "tickers"):
            row = self._one_row(ticker_body, symbol, "tickers")
            mark_row = self._one_row(mark_body, symbol, "mark-price")
            return Ticker(
                venue=self.name,
                symbol=symbol,
                bid=opt_dec(row["bidPrice"]),
                ask=opt_dec(row["askPrice"]),
                last=opt_dec(row["last"]),
                mark=opt_dec(mark_row["markPrice"]),
                volume_24h_usd=usd_volume(meta.quote, quote_volume),
                source_time=opt_ms_to_dt(row["ts"]),
                received_at=self._now(),
            )

    # ---- order book -----------------------------------------------------------------------
    async def get_order_book(self, symbol: str, depth: int = 50) -> OrderBook:
        """Snapshot with up to `depth` (<= 100) levels per side, sizes converted from contracts to
        BASE units. source_time is the book's ts ("order book generation time")."""
        check_positive("depth", depth, _MAX_BOOK_DEPTH)
        body, meta = await asyncio.gather(
            self._call("books", {"instId": symbol, "size": depth}), self.get_instrument_metadata(symbol)
        )
        multiplier = meta.contract_multiplier

        def base_qty(row):
            return dec(row[1]) * multiplier

        with parsing(self.name, "books"):
            (book,) = body["data"]
            return OrderBook(
                venue=self.name,
                symbol=symbol,
                bids=book_levels(book["bids"], descending=True, depth=depth, qty_of=base_qty),
                asks=book_levels(book["asks"], descending=False, depth=depth, qty_of=base_qty),
                source_time=opt_ms_to_dt(book["ts"]),
                received_at=self._now(),
            )

    # ---- candles --------------------------------------------------------------------------
    async def get_candles(self, symbol: str, timeframe: str = "1m", limit: int = 200,
                          include_open: bool = False) -> list[Candle]:
        """Oldest first. Only CLOSED candles by default (venue confirm="1" AND the period has ended by
        the local clock); include_open=True keeps the forming one. volume is BASE-currency volume.

        limit <= 5000. BloFin returns at most 1440 rows per call, so larger requests are paged
        backwards with the `after` cursor (exclusive: rows strictly older than the given ts) until
        `limit` candles are collected, the venue returns an empty page, or the page bound is reached.
        """
        bar = _BARS.get(timeframe)
        if bar is None:
            raise ValueError(f"{self.name}: unsupported timeframe {timeframe!r}; use one of {sorted(_BARS)}")
        check_positive("limit", limit, _MAX_CANDLES)
        now = self._now()
        collected: dict[datetime, Candle] = {}
        cursor: int | None = None
        max_pages = -(-(limit + 1) // _CANDLES_PER_CALL) + 1  # +1: slack for the forming candle / gaps
        for _ in range(max_pages):
            want = min(limit - len(collected) + 1, _CANDLES_PER_CALL)
            params: dict[str, Any] = {"instId": symbol, "bar": bar, "limit": want}
            if cursor is not None:
                params["after"] = cursor
            body = await self._call("candles", params)
            with parsing(self.name, "candles"):
                page = [(int(r[0]), self._parse_candle(symbol, r), r[8] == "1") for r in body["data"]]
            if not page:
                break  # history exhausted
            for _ts, candle, confirmed in page:
                if include_open or (confirmed and is_closed(candle.open_time, timeframe, now)):
                    collected[candle.open_time] = candle
            oldest = min(ts for ts, _, _ in page)
            if cursor is not None and oldest >= cursor:
                raise ExchangeRequestError(f"{self.name}: candles pagination did not advance for {symbol}")
            cursor = oldest
            if len(collected) >= limit:
                break
        candles = sorted(collected.values(), key=lambda c: c.open_time)  # the venue returns newest first
        return candles[-limit:]

    def _parse_candle(self, symbol: str, r: list) -> Candle:
        # [ts, open, high, low, close, vol (contracts), volCurrency (base), volCurrencyQuote, confirm]
        return Candle(
            venue=self.name, symbol=symbol, open_time=ms_to_dt(r[0]),
            open=dec(r[1]), high=dec(r[2]), low=dec(r[3]), close=dec(r[4]), volume=dec(r[6]),
        )
