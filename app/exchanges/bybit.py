"""Bybit v5 linear-derivatives PUBLIC market-data adapter. name = "bybit_linear".

category=linear, LinearPerpetual contracts only: USDT perpetuals (BTCUSDT) and USDC perpetuals
(BTCPERP). Quote and settle come from each instrument's quoteCoin / settleCoin, never from the symbol
string. Dated LinearFutures (BTCUSDT-25DEC26) are ignored.

Endpoints (all public; verified against the docs and live 2026-09-29):
    GET /v5/market/time              server time
    GET /v5/market/instruments-info  instruments, PAGINATED via nextPageCursor
    GET /v5/market/tickers           funding, next funding time, bid1/ask1, mark, index, turnover24h
    GET /v5/market/orderbook         book snapshot (linear: up to 1000 levels per side)
    GET /v5/market/funding/history   settled funding (returned NEWEST first; re-ordered here)
    GET /v5/market/kline             candles (returned NEWEST first; re-ordered here)

Bybit reports application errors as HTTP 200 with retCode != 0, so every response is checked. The
documented IP limit answers HTTP 403 (and US / mainland-China source IPs are refused with 403): both
are surfaced as ExchangeRequestError and never retried.

Conventions verified live:
- Linear quantities are BASE coin (BTCUSDT qtyStep 0.001 = 0.001 BTC; kline volume is base coin;
  docs: "base coin for USDT/USDC contracts"). contract_multiplier is 1. A "1000PEPE" base is the
  venue's own base coin: one unit is 1000 PEPE and price * quantity is still the notional.
- Funding rate is a FRACTION per funding interval. Positive = long pays short.

FUNDING RATE SEMANTICS (get_funding_info): the ticker's fundingRate is the running estimate for the
interval that settles at nextFundingTime (it moves between polls and differs from the last settled
rate in /funding/history), so is_predicted=True. Settled rates come from get_funding_history().

FUNDING INTERVAL: instruments-info.fundingInterval is documented in MINUTES (480 -> 8 h, 240 -> 4 h)
and becomes InstrumentMeta.funding_interval_hours. get_funding_info() prefers the ticker's own
fundingIntervalHour (documented, whole hours, fetched in the same call so it is never stale) and only
falls back to the cached instrument value when the ticker omits it.

Not obtainable from public endpoints, therefore not provided: fees (get_fee_schedule stays
ExchangeNotSupported).
"""

import asyncio
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from app.domain.market import Candle, FundingInfo, InstrumentMeta, OrderBook, Ticker

from ._common import (
    EPOCH,
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

_CATEGORY = "linear"
_PERPETUAL_TYPE = "LinearPerpetual"
_MAX_INSTRUMENT_PAGES = 50
_MAX_ORDERBOOK_DEPTH = 1000  # verified live: linear limit=1000 returns 1000 levels
_MAX_KLINES = 999  # endpoint max is 1000 rows; one is reserved for the still-forming candle
_MAX_FUNDING_HISTORY = 200  # documented per-call maximum
_RATE_LIMIT_CODES = frozenset({10006, 10018})  # "Too many visits" / rate-limit family
_INTERVALS = {  # project timeframe -> Bybit interval (each verified live)
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "2h": "120",
    "4h": "240", "6h": "360", "12h": "720", "1d": "D",
}


class BybitLinearAdapter(PublicMarketDataAdapter):
    name = "bybit_linear"
    DEFAULT_BASE_URL = "https://api.bybit.com"
    TESTNET_BASE_URL = "https://api-testnet.bybit.com"  # docs "Integration guide"; never a default

    def __init__(self, *, base_url: str = DEFAULT_BASE_URL, **kwargs):
        super().__init__(base_url=base_url, **kwargs)

    async def _call(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET a v5 endpoint and return the envelope, raising on retCode != 0."""
        body = await self._http.get_json(path, params=params)
        with parsing(self.name, path):
            code = int(body["retCode"])
            if code == 0:
                return body
            message = str(body.get("retMsg", ""))[:120]
        if code in _RATE_LIMIT_CODES:
            raise ExchangeRequestError(f"{self.name}: rate limit (retCode={code}) on {path}; not retried")
        raise ExchangeRequestError(f"{self.name}: {path} failed (retCode={code}, retMsg={message})")

    # ---- time -----------------------------------------------------------------------------
    async def get_server_time(self) -> datetime:
        body = await self._call("/v5/market/time", {})
        with parsing(self.name, "server time"):
            nanos = int(body["result"]["timeNano"])
            return EPOCH + timedelta(microseconds=nanos // 1000)

    # ---- instruments ----------------------------------------------------------------------
    async def _load_instruments(self) -> list[InstrumentMeta]:
        rows: list[dict] = []
        cursor = ""
        for _ in range(_MAX_INSTRUMENT_PAGES):
            params: dict[str, Any] = {"category": _CATEGORY, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            body = await self._call("/v5/market/instruments-info", params)
            with parsing(self.name, "instruments-info"):
                rows.extend(body["result"]["list"])
                cursor = body["result"].get("nextPageCursor") or ""
            if not cursor:
                break
        else:
            raise ExchangeRequestError(f"{self.name}: instruments-info pagination did not terminate")
        received_at = self._now()
        with parsing(self.name, "instruments-info"):
            perpetuals = [r for r in rows if r.get("contractType") == _PERPETUAL_TYPE]
        result: list[InstrumentMeta] = []
        for raw in perpetuals:
            try:
                result.append(self._parse_instrument(raw, received_at))
            except (KeyError, IndexError, TypeError, ValueError, ArithmeticError) as exc:
                # One malformed row must not hide every other market; that symbol simply cannot resolve.
                log.warning("%s: skipping unparseable instrument %r (%s)", self.name, raw.get("symbol"),
                            type(exc).__name__)
        if perpetuals and not result:
            raise ExchangeRequestError(f"{self.name}: no instruments-info row could be parsed")
        return result

    def _parse_instrument(self, raw: dict, received_at: datetime) -> InstrumentMeta:
        lot = raw["lotSizeFilter"]
        minutes = dec(raw["fundingInterval"]) if raw.get("fundingInterval") not in (None, "") else Decimal(0)
        max_order, max_market = dec(lot["maxOrderQty"]), opt_dec(lot.get("maxMktOrderQty"))
        return InstrumentMeta(
            venue=self.name,
            symbol=raw["symbol"],
            base=raw["baseCoin"],
            quote=raw["quoteCoin"],
            settle=raw["settleCoin"],
            is_linear=True,
            is_perpetual=True,
            is_tradable=raw["status"] == "Trading",
            contract_multiplier=Decimal(1),
            tick_size=dec(raw["priceFilter"]["tickSize"]),
            qty_step=dec(lot["qtyStep"]),
            min_qty=dec(lot["minOrderQty"]),
            # tighter of the limit and market caps: a quantity <= max_qty is accepted for both
            max_qty=min(max_order, max_market) if max_market is not None else max_order,
            min_notional=opt_dec(lot.get("minNotionalValue")),
            funding_interval_hours=minutes / 60 if minutes > 0 else None,
            max_leverage=opt_dec(raw.get("leverageFilter", {}).get("maxLeverage")),
            received_at=received_at,
        )

    # ---- tickers (shared by funding + ticker) ---------------------------------------------
    async def _ticker_row(self, symbol: str) -> tuple[dict, datetime | None]:
        body = await self._call("/v5/market/tickers", {"category": _CATEGORY, "symbol": symbol})
        with parsing(self.name, "tickers"):
            rows = body["result"]["list"]
            if len(rows) != 1 or rows[0]["symbol"] != symbol:
                raise ExchangeRequestError(f"{self.name}: tickers returned no unique row for {symbol}")
            return rows[0], opt_ms_to_dt(body["time"])

    # ---- funding --------------------------------------------------------------------------
    async def get_funding_info(self, symbol: str) -> FundingInfo:
        row, source_time = await self._ticker_row(symbol)
        with parsing(self.name, "tickers"):
            interval_hours = opt_dec(row.get("fundingIntervalHour"))
            funding_rate = dec(row["fundingRate"])
            next_time = opt_ms_to_dt(row["nextFundingTime"])
            mark, index = opt_dec(row.get("markPrice")), opt_dec(row.get("indexPrice"))
        if not interval_hours:
            interval_hours = (await self.get_instrument_metadata(symbol)).funding_interval_hours
        if not interval_hours:
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
            source_time=source_time,
            received_at=self._now(),
        )

    async def get_funding_history(self, symbol: str, limit: int = 100) -> list[tuple[datetime, Decimal]]:
        """Settled funding, oldest first. limit <= 200 (documented per-call maximum)."""
        check_positive("limit", limit, _MAX_FUNDING_HISTORY)
        body = await self._call(
            "/v5/market/funding/history", {"category": _CATEGORY, "symbol": symbol, "limit": limit}
        )
        with parsing(self.name, "funding history"):
            history = [
                (ms_to_dt(r["fundingRateTimestamp"]), dec(r["fundingRate"])) for r in body["result"]["list"]
            ]
        history.sort(key=lambda item: item[0])  # the venue returns newest first
        return history

    # ---- ticker ---------------------------------------------------------------------------
    async def get_ticker(self, symbol: str) -> Ticker:
        (row, source_time), meta = await asyncio.gather(
            self._ticker_row(symbol), self.get_instrument_metadata(symbol)
        )
        with parsing(self.name, "tickers"):
            return Ticker(
                venue=self.name,
                symbol=symbol,
                bid=opt_dec(row["bid1Price"]),
                ask=opt_dec(row["ask1Price"]),
                last=opt_dec(row["lastPrice"]),
                mark=opt_dec(row["markPrice"]),
                # turnover24h is in the quote coin; converted only for USD-like quotes
                volume_24h_usd=usd_volume(meta.quote, opt_dec(row["turnover24h"])),
                source_time=source_time,
                received_at=self._now(),
            )

    # ---- order book -----------------------------------------------------------------------
    async def get_order_book(self, symbol: str, depth: int = 50) -> OrderBook:
        """Snapshot with up to `depth` (<= 1000) levels per side. source_time is `ts`, the time the
        venue generated this snapshot (cts, the matching-engine time, only moves when the book does)."""
        check_positive("depth", depth, _MAX_ORDERBOOK_DEPTH)
        body = await self._call(
            "/v5/market/orderbook", {"category": _CATEGORY, "symbol": symbol, "limit": depth}
        )
        with parsing(self.name, "orderbook"):
            result = body["result"]
            if result["s"] != symbol:
                raise ExchangeRequestError(f"{self.name}: orderbook returned {result['s']} for {symbol}")
            return OrderBook(
                venue=self.name,
                symbol=symbol,
                bids=book_levels(result["b"], descending=True, depth=depth),
                asks=book_levels(result["a"], descending=False, depth=depth),
                source_time=opt_ms_to_dt(result.get("ts") or body["time"]),
                received_at=self._now(),
            )

    # ---- candles --------------------------------------------------------------------------
    async def get_candles(self, symbol: str, timeframe: str = "1m", limit: int = 200,
                          include_open: bool = False) -> list[Candle]:
        """Oldest first. Only CLOSED candles by default (the still-forming candle is dropped);
        include_open=True keeps it. limit <= 999. volume is base-coin volume."""
        interval = _INTERVALS.get(timeframe)
        if interval is None:
            raise ValueError(f"{self.name}: unsupported timeframe {timeframe!r}; use one of {sorted(_INTERVALS)}")
        check_positive("limit", limit, _MAX_KLINES)
        body = await self._call(
            "/v5/market/kline",
            {"category": _CATEGORY, "symbol": symbol, "interval": interval, "limit": limit + 1},
        )
        now = self._now()
        with parsing(self.name, "kline"):
            candles = [
                Candle(
                    venue=self.name, symbol=symbol, open_time=ms_to_dt(r[0]),
                    open=dec(r[1]), high=dec(r[2]), low=dec(r[3]), close=dec(r[4]), volume=dec(r[5]),
                )
                for r in body["result"]["list"]
            ]
        candles.sort(key=lambda c: c.open_time)  # the venue returns newest first
        if not include_open:
            candles = [c for c in candles if is_closed(c.open_time, timeframe, now)]
        return candles[-limit:]
