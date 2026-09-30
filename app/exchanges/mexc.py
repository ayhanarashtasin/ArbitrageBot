"""MEXC futures (contract API) PUBLIC market-data adapter, LINEAR perpetuals only. name = "mexc_contract".

Serves the USDT-margined (BTC_USDT), USDC-margined (BTC_USDC) and USD1-margined (BTC_USD1) perpetuals
of the MEXC contract API. Base / quote / settle come from each contract's own baseCoin / quoteCoin /
settleCoin, never from the symbol string. Inverse (coin-margined) contracts such as BTC_USD (quote USD,
settle BTC, contractSize in USD) are ignored: their size cannot be expressed as base units per contract.

BASE URL: https://api.mexc.com. MEXC moved the futures REST API there from https://contract.mexc.com
("Futures API Access Domain Update", 2026-01-14: the old domain is officially discontinued since
2026-01-19, https://www.mexc.com/announcements/article/futures-api-access-domain-update-17827791532974;
new docs https://www.mexc.com/api-docs/futures/integration-guide). Paths and payloads are unchanged:
the endpoint reference used is https://mexcdevelop.github.io/apidocs/contract_v1_en/. Both hosts
answered identically live on 2026-09-30; the old one is kept only as LEGACY_BASE_URL.

Endpoints (all public, no key, no signature; verified against the docs and live 2026-09-30; the rate
limits are the documented per-endpoint limits):
    GET /api/v1/contract/ping                      server time (epoch ms)            20 req / 2 s
    GET /api/v1/contract/detail                    every contract in ONE response    1 req / 5 s (!)
                                                   (1189 rows live, ~2.3 MB; cached 10 minutes)
    GET /api/v1/contract/funding_rate/{symbol}     fundingRate, collectCycle,        20 req / 2 s
                                                   nextSettleTime
    GET /api/v1/contract/funding_rate/history      settled funding, NEWEST first,    20 req / 2 s
                                                   page_size <= 1000 (documented; live page_size=1001
                                                   is clamped to 1000); only page_num=1 is used
    GET /api/v1/contract/ticker?symbol=            bid1/ask1/lastPrice/fairPrice/    20 req / 2 s
                                                   indexPrice/amount24/timestamp
    GET /api/v1/contract/depth/{symbol}?limit=     book snapshot; live limit=1000    20 req / 2 s
                                                   -> 1000 levels per side (no limit -> 1500)
    GET /api/v1/contract/kline/{symbol}            candles, OLDEST first, start/end  20 req / 2 s
                                                   in SECONDS, at most 2000 rows per request
                                                   (documented; live a 3000-minute window returned
                                                   the 2000 rows closest to `end`)

MEXC reports application errors as HTTP 200 with {"success": false, "code": ..., "message": ...}
(live: code 1001 "Contract does not exist" for an unknown symbol, 600 "Parameter error" for a bad
kline interval), so every envelope is checked. Code 510 "Excessive frequency of requests" is the
documented rate-limit code and is never retried (HTTP 429/418/403 are refused by PublicHttp).

UNITS (verified live on BTC_USDT: contractSize 0.0001 BTC, volUnit 1, minVol 1):
- Every MEXC volume is in CONTRACTS. contract_multiplier = contractSize (base units per contract,
  docs "contract value"); qty_step = volUnit * contractSize, min_qty = minVol * contractSize,
  max_qty = maxVol * contractSize; order-book quantities and candle volume are multiplied by
  contractSize. Live check: kline vol 179073 contracts * 0.0001 = 17.9073 BTC, and its `amount`
  1488717.28 USDT / 17.9073 = 83134, the candle's price. PEPE_USDT has contractSize 10,000,000 PEPE.
- Depth rows are [price, contracts, order_count] (docs).
- Ticker amount24 is the 24h turnover in the QUOTE coin (live: 2.46e9 USDT = volume24 294,475,431
  contracts * 0.0001 BTC * ~83,600); volume24 is contracts. volume_24h_usd = amount24, converted only
  for USD-like quotes.
- tick_size = priceUnit. min_notional is None (MEXC publishes none).
- Prices come as JSON numbers; PublicHttp parses them straight into Decimal (no float rounding).

FUNDING: fundingRate is a FRACTION per funding interval (docs example -0.000489 with max 0.001; live
BTC_USDT -0.00001 with bounds +/-0.0018). Positive = long pays short (MEXC help centre, "MEXC Futures
Funding Rate"). mark_price is the ticker's fairPrice (MEXC's name for the mark price), index_price its
indexPrice.

FUNDING RATE SEMANTICS (get_funding_info): funding_rate/{symbol}.fundingRate is the rate for the
settlement at nextSettleTime and it is a running estimate: live, XDP_USDT moved -0.001016 ->
-0.001067 within four minutes while its last settled rate (history) was -0.000532. So is_predicted
is True. Settled rates come from get_funding_history().

FUNDING INTERVAL: collectCycle ("charge cycle", documented int) is the interval in HOURS (live: 8 for
BTC_USDT / BTC_USDC with 8 h spacing in the history, 4 for XDP_USDT with 4 h spacing). It is only
published per symbol by funding_rate/{symbol}; contract/detail has no interval field (none of the
1189 live rows carries one), so InstrumentMeta.funding_interval_hours is None and get_funding_info()
reads collectCycle from the same call as the rate (never stale). No default is assumed: without a
positive collectCycle get_funding_info() raises instead of guessing.

INSTRUMENT FILTERS (undocumented fields, observed live 2026-09-30, fail-closed):
- futureType == 1 marks a perpetual (all 1189 live rows, displayNameEn "... PERPETUAL"). A row
  without it is not treated as a perpetual.
- is_tradable = state == 0 (docs: 0 enabled, 1 delivery, 2 completed, 3 offline, 4 pause) AND
  apiAllowed is true (docs: "whether support api"; 40 live contracts are false, e.g. EACC_USDT;
  MEXC's API trading excludes Innovation Zone pairs) AND no deliveryTime is published. deliveryTime
  (with automaticDelivery=1) was set on exactly the 26 perpetuals named in MEXC's delisting notices
  of 2026-09-30 .. 2026-10-04 (https://www.mexc.com/announcements/delistings/futures-19): a scheduled
  delisting.
- Equity perps use MEXC's own baseCoin, which is NOT the ticker: TESLA_USDT has baseCoin "TESLA"
  (baseCoinName "TSLA"), NVIDIA_USDT "NVIDIA", AAPLSTOCK_USDT "AAPLSTOCK". resolve_symbol matches
  baseCoin only; nothing is guessed from display names.

Not obtainable from public endpoints, therefore not provided: account fee tiers (get_fee_schedule
stays ExchangeNotSupported). contract/detail does publish per-contract takerFeeRate / makerFeeRate,
but they are the web/app rates: MEXC's API futures trading has its own schedule that takes precedence
("Updates to API Futures Trading Fees (Jun 1, 2026)": maker 0.06 %, taker 0.08 %).
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
    opt_ms_to_dt,
    parsing,
    usd_volume,
)
from .base import ExchangeRequestError, SymbolNotFound

log = logging.getLogger(__name__)

_PREFIX = "/api/v1/contract"
_PERPETUAL_FUTURE_TYPE = 1  # undocumented; every live row is 1 (see module docstring)
_ENABLED_STATE = 0
_MAX_ORDERBOOK_DEPTH = 1000  # verified live: limit=1000 returns 1000 levels per side
_KLINE_PAGE_ROWS = 2000  # documented per-request maximum (verified live)
_MAX_KLINES = 5000  # get_candles limit bound
_MAX_KLINE_PAGES = 5  # hard bound on kline requests per get_candles call
_MAX_FUNDING_HISTORY = 1000  # documented page_size maximum (verified live)
_RATE_LIMIT_CODES = frozenset({510})  # documented "Excessive frequency of requests"
_INTERVALS = {  # project timeframe -> MEXC interval (each verified live)
    "1m": "Min1", "5m": "Min5", "15m": "Min15", "30m": "Min30", "1h": "Min60",
    "4h": "Hour4", "8h": "Hour8", "1d": "Day1",
}
_SYMBOL_RE = re.compile(r"[A-Za-z0-9_]+")  # every live symbol matches; guards the URL path


class MexcContractAdapter(PublicMarketDataAdapter):
    name = "mexc_contract"
    DEFAULT_BASE_URL = "https://api.mexc.com"
    LEGACY_BASE_URL = "https://contract.mexc.com"  # officially discontinued 2026-01-19; never a default

    def __init__(self, *, base_url: str = DEFAULT_BASE_URL, **kwargs):
        super().__init__(base_url=base_url, **kwargs)

    async def _call(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET an endpoint and return its `data`, raising on success != true or code != 0."""
        body = await self._http.get_json(path, params=params)
        with parsing(self.name, path):
            success = body["success"]
            code = int(body.get("code", 0))  # the documented ping example carries no code
            if success is True and code == 0:
                return body["data"]
            message = str(body.get("message", ""))[:120]
        if code in _RATE_LIMIT_CODES:
            raise ExchangeRequestError(f"{self.name}: rate limit (code={code}) on {path}; not retried")
        raise ExchangeRequestError(f"{self.name}: {path} failed (code={code}, message={message})")

    def _path_symbol(self, symbol: str) -> str:
        if not isinstance(symbol, str) or not _SYMBOL_RE.fullmatch(symbol):
            raise SymbolNotFound(f"{self.name}: invalid contract symbol {symbol!r}")
        return symbol

    # ---- time -----------------------------------------------------------------------------
    async def get_server_time(self) -> datetime:
        data = await self._call(f"{_PREFIX}/ping")
        with parsing(self.name, "server time"):
            return ms_to_dt(data)

    # ---- instruments ----------------------------------------------------------------------
    async def _load_instruments(self) -> list[InstrumentMeta]:
        data = await self._call(f"{_PREFIX}/detail")
        received_at = self._now()
        with parsing(self.name, "contract detail"):
            linear = [
                r for r in data
                if r.get("futureType") == _PERPETUAL_FUTURE_TYPE and r.get("quoteCoin") == r.get("settleCoin")
            ]
        result: list[InstrumentMeta] = []
        for raw in linear:
            try:
                result.append(self._parse_instrument(raw, received_at))
            except (KeyError, IndexError, TypeError, ValueError, ArithmeticError) as exc:
                # One malformed row must not hide every other market; that symbol simply cannot resolve.
                log.warning("%s: skipping unparseable contract %r (%s)", self.name, raw.get("symbol"),
                            type(exc).__name__)
        if linear and not result:
            raise ExchangeRequestError(f"{self.name}: no contract detail row could be parsed")
        return result

    def _parse_instrument(self, raw: dict, received_at: datetime) -> InstrumentMeta:
        size, tick, vol_unit = dec(raw["contractSize"]), dec(raw["priceUnit"]), dec(raw["volUnit"])
        if size <= 0 or tick <= 0 or vol_unit <= 0:
            raise ValueError("contractSize, priceUnit and volUnit must be positive")
        max_vol = opt_dec(raw.get("maxVol"))
        scheduled_delivery = opt_ms_to_dt(raw.get("deliveryTime")) is not None
        return InstrumentMeta(
            venue=self.name,
            symbol=raw["symbol"],
            base=raw["baseCoin"],
            quote=raw["quoteCoin"],
            settle=raw["settleCoin"],
            is_linear=True,
            is_perpetual=True,
            is_tradable=raw["state"] == _ENABLED_STATE and raw["apiAllowed"] is True and not scheduled_delivery,
            contract_multiplier=size,
            tick_size=tick,
            qty_step=vol_unit * size,
            min_qty=dec(raw["minVol"]) * size,
            max_qty=max_vol * size if max_vol else None,
            min_notional=None,  # not published
            funding_interval_hours=None,  # only per symbol via funding_rate/{symbol}.collectCycle
            max_leverage=opt_dec(raw.get("maxLeverage")),
            received_at=received_at,
        )

    async def _contract_size(self, symbol: str) -> Decimal:
        return (await self.get_instrument_metadata(symbol)).contract_multiplier

    # ---- ticker row (shared by funding + ticker) -----------------------------------------
    async def _ticker_row(self, symbol: str) -> dict:
        data = await self._call(f"{_PREFIX}/ticker", {"symbol": self._path_symbol(symbol)})
        with parsing(self.name, "ticker"):
            if not isinstance(data, dict) or data["symbol"] != symbol:
                raise ExchangeRequestError(f"{self.name}: ticker returned no row for {symbol}")
            return data

    # ---- funding --------------------------------------------------------------------------
    async def get_funding_info(self, symbol: str) -> FundingInfo:
        path = f"{_PREFIX}/funding_rate/{self._path_symbol(symbol)}"
        funding, ticker = await asyncio.gather(self._call(path), self._ticker_row(symbol))
        with parsing(self.name, "funding_rate"):
            if funding["symbol"] != symbol:
                raise ExchangeRequestError(f"{self.name}: funding_rate returned {funding['symbol']} for {symbol}")
            interval_hours = opt_dec(funding.get("collectCycle"))
            if not interval_hours or interval_hours <= 0:
                raise ExchangeRequestError(f"{self.name}: no funding interval published for {symbol}")
            return FundingInfo(
                venue=self.name,
                symbol=symbol,
                funding_rate=dec(funding["fundingRate"]),  # running estimate, see module docstring
                interval_hours=interval_hours,
                next_funding_time=opt_ms_to_dt(funding["nextSettleTime"]),
                is_predicted=True,
                mark_price=opt_dec(ticker["fairPrice"]),
                index_price=opt_dec(ticker["indexPrice"]),
                source_time=opt_ms_to_dt(funding["timestamp"]),
                received_at=self._now(),
            )

    async def get_funding_history(self, symbol: str, limit: int = 100) -> list[tuple[datetime, Decimal]]:
        """Settled funding, oldest first. limit <= 1000 (documented page_size maximum, one page)."""
        check_positive("limit", limit, _MAX_FUNDING_HISTORY)
        data = await self._call(
            f"{_PREFIX}/funding_rate/history",
            {"symbol": self._path_symbol(symbol), "page_num": 1, "page_size": limit},
        )
        with parsing(self.name, "funding history"):
            rows = data["resultList"]
            if any(r["symbol"] != symbol for r in rows):
                raise ExchangeRequestError(f"{self.name}: funding history returned another symbol for {symbol}")
            history = [(ms_to_dt(r["settleTime"]), dec(r["fundingRate"])) for r in rows]
        history.sort(key=lambda item: item[0])  # the venue returns newest first
        return history[-limit:]

    # ---- ticker ---------------------------------------------------------------------------
    async def get_ticker(self, symbol: str) -> Ticker:
        row, meta = await asyncio.gather(self._ticker_row(symbol), self.get_instrument_metadata(symbol))
        with parsing(self.name, "ticker"):
            return Ticker(
                venue=self.name,
                symbol=symbol,
                bid=opt_dec(row["bid1"]),
                ask=opt_dec(row["ask1"]),
                last=opt_dec(row["lastPrice"]),
                mark=opt_dec(row["fairPrice"]),
                # amount24 is the 24h turnover in the quote coin; converted only for USD-like quotes
                volume_24h_usd=usd_volume(meta.quote, opt_dec(row["amount24"])),
                source_time=opt_ms_to_dt(row["timestamp"]),
                received_at=self._now(),
            )

    # ---- order book -----------------------------------------------------------------------
    async def get_order_book(self, symbol: str, depth: int = 50) -> OrderBook:
        """Snapshot with up to `depth` (<= 1000) levels per side, quantities in BASE units
        (contracts * contractSize). source_time is the snapshot's documented `timestamp`."""
        check_positive("depth", depth, _MAX_ORDERBOOK_DEPTH)
        path = f"{_PREFIX}/depth/{self._path_symbol(symbol)}"
        data, size = await asyncio.gather(self._call(path, {"limit": depth}), self._contract_size(symbol))

        def base_qty(row):  # [price, contracts, order_count]
            return dec(row[1]) * size

        with parsing(self.name, "depth"):
            return OrderBook(
                venue=self.name,
                symbol=symbol,
                bids=book_levels(data["bids"], descending=True, depth=depth, qty_of=base_qty),
                asks=book_levels(data["asks"], descending=False, depth=depth, qty_of=base_qty),
                source_time=opt_ms_to_dt(data["timestamp"]),
                received_at=self._now(),
            )

    # ---- candles --------------------------------------------------------------------------
    async def get_candles(self, symbol: str, timeframe: str = "1m", limit: int = 200,
                          include_open: bool = False) -> list[Candle]:
        """Oldest first. Only CLOSED candles by default (the still-forming candle is dropped);
        include_open=True keeps it. limit <= 5000. volume is BASE units (vol contracts * contractSize).

        MEXC caps a kline request at 2000 rows and takes start/end in seconds, so the window is
        walked backwards from now: each request asks for at most 2000 aligned rows ending just before
        the oldest candle already received, until `limit` candles are collected, the venue returns
        nothing older (history start or a gap wider than one window), or 5 requests were made.
        """
        interval = _INTERVALS.get(timeframe)
        if interval is None:
            raise ValueError(f"{self.name}: unsupported timeframe {timeframe!r}; use one of {sorted(_INTERVALS)}")
        check_positive("limit", limit, _MAX_KLINES)
        path = f"{_PREFIX}/kline/{self._path_symbol(symbol)}"
        size = await self._contract_size(symbol)
        step = TIMEFRAME_MS[timeframe] // 1000
        now = self._now()
        end = int(now.timestamp())
        by_open: dict[datetime, Candle] = {}
        for _ in range(_MAX_KLINE_PAGES):
            closed = sum(1 for t in by_open if is_closed(t, timeframe, now))
            if closed >= limit:
                break
            rows = min(limit - closed + 1, _KLINE_PAGE_ROWS)  # +1 for the still-forming candle
            last_open = end - end % step
            data = await self._call(path, {"interval": interval, "start": last_open - (rows - 1) * step, "end": end})
            page = self._parse_klines(data, symbol, size)
            oldest = min(by_open) if by_open else None
            fresh = [c for c in page if c.open_time not in by_open]
            by_open.update((c.open_time, c) for c in fresh)
            if not fresh or (oldest is not None and min(c.open_time for c in fresh) >= oldest):
                break  # nothing older is available
            end = int(min(by_open).timestamp()) - 1
        candles = sorted(by_open.values(), key=lambda c: c.open_time)
        if not include_open:
            candles = [c for c in candles if is_closed(c.open_time, timeframe, now)]
        return candles[-limit:]

    def _parse_klines(self, data: dict, symbol: str, size: Decimal) -> list[Candle]:
        with parsing(self.name, "kline"):
            columns = zip(data["time"], data["open"], data["high"], data["low"], data["close"], data["vol"],
                          strict=True)
            return [
                Candle(
                    venue=self.name, symbol=symbol, open_time=ms_to_dt(int(t) * 1000),
                    open=dec(o), high=dec(h), low=dec(lo), close=dec(c), volume=dec(v) * size,
                )
                for t, o, h, lo, c, v in columns
            ]
