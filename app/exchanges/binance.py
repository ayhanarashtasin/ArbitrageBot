"""Binance USDⓈ-M futures (fapi) PUBLIC market-data adapter. name = "binance_usdt_m".

Covers every PERPETUAL and TRADIFI_PERPETUAL contract on https://fapi.binance.com, i.e. USDT-margined
perpetuals and the USDC-margined perpetuals that live on the same host. Quote and settle asset come
from each instrument's own quoteAsset / marginAsset, never from the symbol string. Delivery contracts
(CURRENT_QUARTER / NEXT_QUARTER) are ignored: this bot trades perpetuals only.

Endpoints (all public, no key, no signature; verified against the docs and live 2026-09-29):
    GET /fapi/v1/time             server time
    GET /fapi/v1/exchangeInfo     instruments, PRICE_FILTER / LOT_SIZE / MARKET_LOT_SIZE / MIN_NOTIONAL
    GET /fapi/v1/fundingInfo      fundingIntervalHours per symbol (weight 0, shares 500/5min/IP with
                                  fundingRate)
    GET /fapi/v1/premiumIndex     mark, index, current funding rate, next funding time
    GET /fapi/v1/fundingRate      settled funding history (ascending)
    GET /fapi/v1/depth            order book snapshot
    GET /fapi/v1/ticker/24hr      last price and 24h quoteVolume
    GET /fapi/v1/ticker/bookTicker best bid/ask (24hr has none)
    GET /fapi/v1/klines           candles

Conventions verified live:
- Quantities are already in BASE asset units (BTCUSDT stepSize 0.001 = 0.001 BTC). contract_multiplier
  is therefore 1. "Base" is the venue's own base asset: 1000PEPEUSDT has base "1000PEPE", and one
  unit of quantity is one 1000PEPE, so price * quantity is still the notional.
- Funding rate is a FRACTION per funding interval (0.0001 = 0.01 %). Positive = long pays short.

FUNDING RATE SEMANTICS (get_funding_info): premiumIndex.lastFundingRate is named "last" and documented
as "the Latest funding rate", but measured live it is NOT the last settled rate. Over a few minutes it
moves tick by tick (0.00001679 -> 0.00001628 for BTCUSDT) while the last settled rate from
/fapi/v1/fundingRate stayed at 0.00006798, and HL's predictedFundings for Binance tracks it. It is the
running estimate of the rate that will be charged at nextFundingTime. So FundingInfo.funding_rate is
that estimate and is_predicted=True. Settled rates come from get_funding_history(), never from here.

FUNDING INTERVAL: taken per symbol from /fapi/v1/fundingInfo (fundingIntervalHours, 8 for BTC/ETH/SOL,
4 or 1 for some alts; the interval matches the real settlement spacing in the funding history). No
default is assumed: a symbol missing from fundingInfo has funding_interval_hours None, and
get_funding_info() then raises instead of guessing. The interval comes from the instrument cache
(TTL 10 minutes) so a change made by Binance shows up within one TTL.

Not obtainable from public endpoints, therefore None: max_leverage (needs the signed
leverageBracket endpoint). Fees are not exposed publicly either (get_fee_schedule stays
ExchangeNotSupported).
"""

import asyncio
import logging
from datetime import datetime
from decimal import Decimal

from app.domain.market import Candle, FundingInfo, InstrumentMeta, OrderBook, Ticker

from ._common import (
    PublicMarketDataAdapter,
    book_levels,
    check_positive,
    dec,
    is_closed,
    ms_to_dt,
    opt_ms_to_dt,
    parsing,
    usd_volume,
)
from .base import ExchangeRequestError

log = logging.getLogger(__name__)

_PERPETUAL_TYPES = frozenset({"PERPETUAL", "TRADIFI_PERPETUAL"})
_DEPTH_LIMITS = (5, 10, 20, 50, 100, 500, 1000)  # documented valid values for /fapi/v1/depth
_MAX_KLINES = 1500  # verified live: limit=1500 returns 1500 rows, 1501 is rejected (-1130)
_MAX_FUNDING_HISTORY = 1000  # documented maximum for /fapi/v1/fundingRate
_INTERVALS = {  # project timeframe -> Binance interval (each verified live)
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "2h": "2h",
    "4h": "4h", "6h": "6h", "8h": "8h", "12h": "12h", "1d": "1d",
}


class BinanceUsdmAdapter(PublicMarketDataAdapter):
    name = "binance_usdt_m"
    DEFAULT_BASE_URL = "https://fapi.binance.com"
    TESTNET_BASE_URL = "https://demo-fapi.binance.com"  # docs "General Info"; never a default

    def __init__(self, *, base_url: str = DEFAULT_BASE_URL, **kwargs):
        super().__init__(base_url=base_url, **kwargs)

    # ---- time -----------------------------------------------------------------------------
    async def get_server_time(self) -> datetime:
        data = await self._http.get_json("/fapi/v1/time")
        with parsing(self.name, "server time"):
            return ms_to_dt(data["serverTime"])

    # ---- instruments ----------------------------------------------------------------------
    async def _load_instruments(self) -> list[InstrumentMeta]:
        info, funding_info = await asyncio.gather(
            self._http.get_json("/fapi/v1/exchangeInfo"),
            self._http.get_json("/fapi/v1/fundingInfo"),
        )
        received_at = self._now()
        with parsing(self.name, "fundingInfo"):
            intervals = {row["symbol"]: dec(row["fundingIntervalHours"]) for row in funding_info}
        with parsing(self.name, "exchangeInfo"):
            raw_symbols = [s for s in info["symbols"] if s.get("contractType") in _PERPETUAL_TYPES]
        result: list[InstrumentMeta] = []
        for raw in raw_symbols:
            try:
                result.append(self._parse_instrument(raw, intervals.get(raw["symbol"]), received_at))
            except (KeyError, IndexError, TypeError, ValueError, ArithmeticError) as exc:
                # One malformed row must not hide every other market; that symbol simply cannot resolve.
                log.warning("%s: skipping unparseable instrument %r (%s)", self.name, raw.get("symbol"),
                            type(exc).__name__)
        if raw_symbols and not result:
            raise ExchangeRequestError(f"{self.name}: no exchangeInfo symbol could be parsed")
        return result

    def _parse_instrument(self, raw: dict, interval_hours: Decimal | None, received_at: datetime) -> InstrumentMeta:
        filters = {f["filterType"]: f for f in raw["filters"]}
        lot, market_lot = filters["LOT_SIZE"], filters.get("MARKET_LOT_SIZE", filters["LOT_SIZE"])
        quote, settle = raw["quoteAsset"], raw["marginAsset"]
        return InstrumentMeta(
            venue=self.name,
            symbol=raw["symbol"],
            base=raw["baseAsset"],
            quote=quote,
            settle=settle,
            is_linear=quote == settle,
            is_perpetual=True,
            is_tradable=raw["status"] == "TRADING",
            contract_multiplier=Decimal(1),
            tick_size=dec(filters["PRICE_FILTER"]["tickSize"]),
            qty_step=dec(lot["stepSize"]),
            # A quantity inside [min_qty, max_qty] is accepted for limit AND market orders:
            # the tighter of the two filters wins on each side.
            min_qty=max(dec(lot["minQty"]), dec(market_lot["minQty"])),
            max_qty=min(dec(lot["maxQty"]), dec(market_lot["maxQty"])),
            min_notional=dec(filters["MIN_NOTIONAL"]["notional"]) if "MIN_NOTIONAL" in filters else None,
            funding_interval_hours=interval_hours,
            max_leverage=None,  # only in the signed leverageBracket endpoint
            received_at=received_at,
            underlying_type=raw.get("underlyingType") or None,  # COIN, EQUITY, HK_EQUITY, COMMODITY, ... (verified live)
        )

    # ---- funding --------------------------------------------------------------------------
    async def get_funding_info(self, symbol: str) -> FundingInfo:
        data, meta = await asyncio.gather(
            self._http.get_json("/fapi/v1/premiumIndex", params={"symbol": symbol}),
            self.get_instrument_metadata(symbol),
        )
        if meta.funding_interval_hours is None:
            raise ExchangeRequestError(f"{self.name}: no funding interval published for {symbol}")
        with parsing(self.name, "premiumIndex"):
            if data["symbol"] != symbol:
                raise ExchangeRequestError(f"{self.name}: premiumIndex returned {data['symbol']} for {symbol}")
            return FundingInfo(
                venue=self.name,
                symbol=symbol,
                funding_rate=dec(data["lastFundingRate"]),  # running estimate, see module docstring
                interval_hours=meta.funding_interval_hours,
                next_funding_time=opt_ms_to_dt(data["nextFundingTime"]),
                is_predicted=True,
                mark_price=dec(data["markPrice"]),
                index_price=dec(data["indexPrice"]),
                source_time=opt_ms_to_dt(data["time"]),
                received_at=self._now(),
            )

    async def get_funding_history(self, symbol: str, limit: int = 100) -> list[tuple[datetime, Decimal]]:
        """Settled funding, oldest first. limit <= 1000 (documented per-call maximum)."""
        check_positive("limit", limit, _MAX_FUNDING_HISTORY)
        rows = await self._http.get_json("/fapi/v1/fundingRate", params={"symbol": symbol, "limit": limit})
        with parsing(self.name, "fundingRate history"):
            history = [(ms_to_dt(r["fundingTime"]), dec(r["fundingRate"])) for r in rows]
        history.sort(key=lambda item: item[0])
        return history

    # ---- ticker ---------------------------------------------------------------------------
    async def get_ticker(self, symbol: str) -> Ticker:
        """Last + 24h volume from ticker/24hr, bid/ask from bookTicker, mark from premiumIndex."""
        stats, book, premium, meta = await asyncio.gather(
            self._http.get_json("/fapi/v1/ticker/24hr", params={"symbol": symbol}),
            self._http.get_json("/fapi/v1/ticker/bookTicker", params={"symbol": symbol}),
            self._http.get_json("/fapi/v1/premiumIndex", params={"symbol": symbol}),
            self.get_instrument_metadata(symbol),
        )
        with parsing(self.name, "ticker"):
            return Ticker(
                venue=self.name,
                symbol=symbol,
                bid=dec(book["bidPrice"]),
                ask=dec(book["askPrice"]),
                last=dec(stats["lastPrice"]),
                mark=dec(premium["markPrice"]),
                # quoteVolume is denominated in the quote asset; converted only for USD-like quotes
                volume_24h_usd=usd_volume(meta.quote, dec(stats["quoteVolume"])),
                source_time=opt_ms_to_dt(book.get("time") or stats["closeTime"]),
                received_at=self._now(),
            )

    # ---- order book -----------------------------------------------------------------------
    async def get_order_book(self, symbol: str, depth: int = 50) -> OrderBook:
        """Snapshot with up to `depth` levels per side (rounded up to a valid Binance limit, then trimmed).

        source_time is the snapshot's message output time E (falls back to the last-transaction
        time T): E is when this snapshot was produced, which is what freshness checks need; T only
        moves when the book changes, so an illiquid but perfectly current book would look stale.
        """
        check_positive("depth", depth, _DEPTH_LIMITS[-1])
        limit = next(v for v in _DEPTH_LIMITS if v >= depth)
        data = await self._http.get_json("/fapi/v1/depth", params={"symbol": symbol, "limit": limit})
        with parsing(self.name, "depth"):
            return OrderBook(
                venue=self.name,
                symbol=symbol,
                bids=book_levels(data["bids"], descending=True, depth=depth),
                asks=book_levels(data["asks"], descending=False, depth=depth),
                source_time=opt_ms_to_dt(data.get("E") or data["T"]),
                received_at=self._now(),
            )

    # ---- candles --------------------------------------------------------------------------
    async def get_candles(self, symbol: str, timeframe: str = "1m", limit: int = 200,
                          include_open: bool = False) -> list[Candle]:
        """Oldest first. By default only CLOSED candles are returned (the still-forming last candle
        is dropped); pass include_open=True to keep it. limit <= 1499. volume is base-asset volume."""
        interval = _INTERVALS.get(timeframe)
        if interval is None:
            raise ValueError(f"{self.name}: unsupported timeframe {timeframe!r}; use one of {sorted(_INTERVALS)}")
        check_positive("limit", limit, _MAX_KLINES - 1)
        rows = await self._http.get_json(
            "/fapi/v1/klines", params={"symbol": symbol, "interval": interval, "limit": limit + 1}
        )
        now = self._now()
        with parsing(self.name, "klines"):
            candles = [
                Candle(
                    venue=self.name, symbol=symbol, open_time=ms_to_dt(r[0]),
                    open=dec(r[1]), high=dec(r[2]), low=dec(r[3]), close=dec(r[4]), volume=dec(r[5]),
                )
                for r in rows
            ]
        candles.sort(key=lambda c: c.open_time)
        if not include_open:
            candles = [c for c in candles if is_closed(c.open_time, timeframe, now)]
        return candles[-limit:]
