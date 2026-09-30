"""Hyperliquid PUBLIC market-data adapter. name = "hyperliquid" (core perps) or "hyperliquid:<dex>".

Core perps are the first perp dex. HIP-3 builder-deployed markets are selected with the optional
`dex` constructor argument (for example dex="xyz" for the dex that lists xyz:TSLA); the adapter
name then becomes "hyperliquid:xyz" so the exact market identity is never collapsed into a generic
"Hyperliquid" record (project.md section 7).

All calls are POST https://api.hyperliquid.xyz/info with a JSON body (read-only queries; no key, no
signature). Verified against the docs and live 2026-09-29:
    exchangeStatus       {"type":"exchangeStatus"} -> {"specialStatuses":null,"time":<ms>}. Named in
                         the rate-limit doc (weight 2); the response shape is documented by third-party
                         mirrors and confirmed live. Used for get_server_time (no other time source).
    metaAndAssetCtxs     {"type":"metaAndAssetCtxs"[,"dex":<dex>]} -> [meta, ctxs], ctxs aligned to
                         meta.universe. universe: name, szDecimals, maxLeverage, isDelisted; ctx:
                         funding, markPx, oraclePx, dayNtlVlm ... Weight 20.
    spotMeta             token table, used only to turn meta.collateralToken into a name (USDC for
                         the core dex and xyz, USDH for the dead "km" dex). Weight 20.
    l2Book               {"type":"l2Book","coin":<symbol>} up to 20 levels per side, weight 2.
    fundingHistory       {"type":"fundingHistory","coin":<symbol>,"startTime":<ms>} ascending, max 500
                         rows per response.
    candleSnapshot       {"type":"candleSnapshot","req":{coin,interval,startTime,endTime}} ascending,
                         only the latest 5000 candles exist.

HIP-3 route (verified live for dexes xyz, km, mkts): `meta`/`metaAndAssetCtxs` take the documented
"dex" parameter; every other request addresses a market by its dex-prefixed coin name ("xyz:TSLA")
and takes NO dex parameter. universe names already carry the prefix, so symbol == "xyz:TSLA" and
InstrumentMeta.base == "TSLA". predictedFundings is documented as first-dex-only and is not used.

Conventions:
- Sizes are BASE units (szDecimals decimals): contract_multiplier is 1. "kPEPE"-style assets are
  the venue's own base unit (1000 PEPE): price * size is still the notional.
- quote == settle == the collateral token (from spotMeta), not a hard-coded "USDC".

FUNDING RATE UNITS AND INTERVAL: funding is settled every hour (docs: "The funding rate on Hyperliquid
is paid every hour"; fundingHistory rows sit on the hour for core and HIP-3 markets alike), so
interval_hours = 1. The API's fundingRate is the rate charged per HOUR: the docs compute an 8-hour rate
F8 = P + clamp(0.0001 - P, -0.0005, 0.0005) and pay 1/8 of it each hour, and the API value is that
already-divided number. Checked on real data: BTC premium -0.0004146009 -> F8 = 0.0000854 -> /8 =
0.0000106749 = the reported fundingRate. So the API value is used as-is as "fraction per 1 hour".

HIP-3 dexes may scale funding with a deployer multiplier (perpDexs.assetToFundingMultiplier: 0.5 for
the xyz assets). The funding values the API returns already include it (recorded xyz:TSLA rows equal
exactly 0.5 x the core formula while the interest clamp is active), so no adjustment is applied here.

is_predicted=True: metaAndAssetCtxs.funding is the running estimate for the current hour (it differs
from the last settled fundingHistory row). next_funding_time is the next top of the UTC hour computed
from the documented hourly cadence; predictedFundings' own nextFundingTime for "HlPerp" was observed
to be the start of the CURRENT hour (already in the past), so it is not used.

TICK SIZE: Hyperliquid has no fixed tick; the docs' rule is "up to 5 significant figures and no more
than 6 - szDecimals decimals; integer prices are always allowed". InstrumentMeta.tick_size is the
effective tick at the mark price at fetch time; because it depends on price, recompute it with
price_tick() before quoting an order. min_notional = 10 is the documented "Order must have minimum
value of $10" rule (error-response table); it is not part of the meta payload. max_qty is None (the
docs give max order VALUE by leverage tier, not a size).

Not obtainable from public endpoints, therefore not provided: fees (get_fee_schedule stays
ExchangeNotSupported); Ticker.last (neither metaAndAssetCtxs nor l2Book carries a last trade price).
"""

import asyncio
import logging
from datetime import datetime, timedelta
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
    parsing,
    usd_volume,
)
from .base import ExchangeRequestError, SymbolNotFound

log = logging.getLogger(__name__)

MAX_DECIMALS_PERP = 6  # docs "Tick and lot size": MAX_DECIMALS is 6 for perpetuals
PRICE_SIG_FIGS = 5
MIN_ORDER_VALUE = Decimal(10)  # docs error responses: "Order must have minimum value of $10."
FUNDING_INTERVAL_HOURS = Decimal(1)  # docs: funding is paid every hour
_MAX_BOOK_LEVELS = 20  # docs: "Returns at most 20 levels per side"
_MAX_CANDLES = 5000  # docs: only the most recent 5000 candles are available
_FUNDING_PAGE = 500  # docs: time-range responses return at most 500 rows
_MAX_FUNDING_HISTORY = 1000
_MAX_FUNDING_PAGES = 3
_INTERVALS = {  # project timeframe -> candleSnapshot interval (docs list; no 6h on this venue)
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "2h": "2h",
    "4h": "4h", "8h": "8h", "12h": "12h", "1d": "1d",
}


def price_tick(price: Decimal, sz_decimals: int) -> Decimal:
    """Effective price increment at `price` per the documented rule (see module docstring).

    Examples (szDecimals in brackets): BTC 83144 [5] -> 1, xyz:TSLA 353.68 [3] -> 0.01,
    xyz:GOLD 4156.4 [4] -> 0.1.
    """
    decimal_tick = Decimal(1).scaleb(-(MAX_DECIMALS_PERP - sz_decimals))
    if price <= 0:
        return decimal_tick
    sig_fig_tick = Decimal(1).scaleb(price.adjusted() - (PRICE_SIG_FIGS - 1))
    return min(Decimal(1), max(decimal_tick, sig_fig_tick))


def next_funding_time(now: datetime) -> datetime:
    """Next top of the UTC hour strictly after `now`."""
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


class HyperliquidAdapter(PublicMarketDataAdapter):
    DEFAULT_BASE_URL = "https://api.hyperliquid.xyz"
    TESTNET_BASE_URL = "https://api.hyperliquid-testnet.xyz"  # docs API overview; never a default

    def __init__(self, *, dex: str | None = None, underlying_type: str | None = None,
                 base_url: str = DEFAULT_BASE_URL, **kwargs):
        dex = (dex or "").strip()
        if ":" in dex or any(ch.isspace() for ch in dex):
            raise ValueError(f"invalid Hyperliquid dex name {dex!r}")
        self.dex = dex or None
        self.name = f"hyperliquid:{self.dex}" if self.dex else "hyperliquid"
        # Hyperliquid publishes no asset class. The registry states it for dexes whose whole listing is one class
        # (the Trade.xyz dex lists stocks, indices and commodities only: TRADFI).
        self.underlying_type = underlying_type
        super().__init__(base_url=base_url, **kwargs)

    # ---- requests -------------------------------------------------------------------------
    async def _info(self, body: dict[str, Any]) -> Any:
        return await self._http.post_json("/info", body)

    def _meta_request(self) -> dict[str, Any]:
        body: dict[str, Any] = {"type": "metaAndAssetCtxs"}
        if self.dex:
            body["dex"] = self.dex
        return body

    async def _meta_and_ctxs(self) -> tuple[list[dict], list[dict], Any]:
        """(universe, ctxs, collateralToken) with ctxs verified to be aligned with the universe."""
        data = await self._info(self._meta_request())
        with parsing(self.name, "metaAndAssetCtxs"):
            meta, ctxs = data[0], data[1]
            universe = meta["universe"]
            if len(universe) != len(ctxs):
                raise ExchangeRequestError(
                    f"{self.name}: metaAndAssetCtxs universe/ctx length mismatch ({len(universe)} vs {len(ctxs)})"
                )
            return universe, ctxs, meta["collateralToken"]

    async def _ctx_for(self, symbol: str) -> tuple[dict, dict]:
        universe, ctxs, _ = await self._meta_and_ctxs()
        with parsing(self.name, "metaAndAssetCtxs"):
            for asset, ctx in zip(universe, ctxs, strict=True):
                if asset["name"] == symbol:
                    return asset, ctx
        raise SymbolNotFound(f"{self.name}: unknown symbol {symbol}")

    # ---- time -----------------------------------------------------------------------------
    async def get_server_time(self) -> datetime:
        data = await self._info({"type": "exchangeStatus"})
        with parsing(self.name, "exchangeStatus"):
            return ms_to_dt(data["time"])

    # ---- instruments ----------------------------------------------------------------------
    async def _load_instruments(self) -> list[InstrumentMeta]:
        (universe, ctxs, collateral_index), spot = await asyncio.gather(
            self._meta_and_ctxs(), self._info({"type": "spotMeta"})
        )
        received_at = self._now()
        with parsing(self.name, "spotMeta"):
            collateral = next((t["name"] for t in spot["tokens"] if t["index"] == collateral_index), None)
            if collateral is None:
                raise ValueError(f"collateralToken {collateral_index} is not in the spotMeta token list")
        result: list[InstrumentMeta] = []
        for asset, ctx in zip(universe, ctxs, strict=True):
            try:
                result.append(self._parse_instrument(asset, ctx, collateral, received_at))
            except (KeyError, IndexError, TypeError, ValueError, ArithmeticError) as exc:
                log.warning("%s: skipping unparseable asset %r (%s)", self.name, asset.get("name"),
                            type(exc).__name__)
        if universe and not result:
            raise ExchangeRequestError(f"{self.name}: no universe entry could be parsed")
        return result

    def _parse_instrument(self, asset: dict, ctx: dict, collateral: str, received_at: datetime) -> InstrumentMeta:
        symbol = asset["name"]
        if self.dex and not symbol.startswith(f"{self.dex}:"):
            raise ValueError(f"{symbol} is not in dex namespace {self.dex}")
        base = symbol.split(":", 1)[1] if ":" in symbol else symbol
        sz_decimals = int(asset["szDecimals"])
        qty_step = Decimal(1).scaleb(-sz_decimals)
        mark = dec(ctx["markPx"])
        return InstrumentMeta(
            venue=self.name,
            symbol=symbol,
            base=base,
            quote=collateral,
            settle=collateral,
            is_linear=True,
            is_perpetual=True,
            is_tradable=not asset.get("isDelisted", False),
            contract_multiplier=Decimal(1),
            tick_size=price_tick(mark, sz_decimals),
            qty_step=qty_step,
            min_qty=qty_step,
            max_qty=None,
            min_notional=MIN_ORDER_VALUE,
            funding_interval_hours=FUNDING_INTERVAL_HOURS,
            max_leverage=dec(asset["maxLeverage"]),
            received_at=received_at,
            underlying_type=self.underlying_type,
        )

    # ---- funding --------------------------------------------------------------------------
    async def get_funding_info(self, symbol: str) -> FundingInfo:
        asset, ctx = await self._ctx_for(symbol)
        if asset.get("isDelisted"):
            raise ExchangeRequestError(f"{self.name}: {symbol} is delisted")
        received_at = self._now()
        with parsing(self.name, "metaAndAssetCtxs"):
            return FundingInfo(
                venue=self.name,
                symbol=symbol,
                funding_rate=dec(ctx["funding"]),  # per HOUR, running estimate, see module docstring
                interval_hours=FUNDING_INTERVAL_HOURS,
                next_funding_time=next_funding_time(received_at),
                is_predicted=True,
                mark_price=dec(ctx["markPx"]),
                index_price=dec(ctx["oraclePx"]),
                source_time=None,  # the endpoint carries no timestamp
                received_at=received_at,
            )

    async def get_funding_history(self, symbol: str, limit: int = 100) -> list[tuple[datetime, Decimal]]:
        """Settled hourly funding, oldest first; the newest `limit` rows (limit <= 1000)."""
        check_positive("limit", limit, _MAX_FUNDING_HISTORY)
        start_ms = int((self._now() - timedelta(hours=limit + 1)).timestamp() * 1000)
        rows: dict[int, Decimal] = {}
        for _ in range(_MAX_FUNDING_PAGES):
            page = await self._info({"type": "fundingHistory", "coin": symbol, "startTime": start_ms})
            with parsing(self.name, "fundingHistory"):
                if not isinstance(page, list):
                    raise ValueError("expected a list")
                for row in page:
                    rows[int(row["time"])] = dec(row["fundingRate"])
                if len(page) < _FUNDING_PAGE:
                    break
                start_ms = int(page[-1]["time"]) + 1
        history = [(ms_to_dt(t), rate) for t, rate in sorted(rows.items())]
        return history[-limit:]

    # ---- ticker ---------------------------------------------------------------------------
    async def get_ticker(self, symbol: str) -> Ticker:
        (asset, ctx), book, meta = await asyncio.gather(
            self._ctx_for(symbol), self.get_order_book(symbol, depth=1), self.get_instrument_metadata(symbol)
        )
        with parsing(self.name, "metaAndAssetCtxs"):
            return Ticker(
                venue=self.name,
                symbol=symbol,
                bid=book.best_bid,
                ask=book.best_ask,
                last=None,  # no last-trade price in metaAndAssetCtxs or l2Book
                mark=dec(ctx["markPx"]),
                volume_24h_usd=usd_volume(meta.quote, dec(ctx["dayNtlVlm"])),
                source_time=book.source_time,
                received_at=self._now(),
            )

    # ---- order book -----------------------------------------------------------------------
    async def get_order_book(self, symbol: str, depth: int = 50) -> OrderBook:
        """Up to min(depth, 20) levels per side: the venue returns at most 20. source_time is the
        snapshot `time` from the venue."""
        check_positive("depth", depth)
        data = await self._info({"type": "l2Book", "coin": symbol})
        if data is None:  # unknown coin answers HTTP 200 with a JSON null
            raise ExchangeRequestError(f"{self.name}: no order book for {symbol} (unknown coin?)")
        limit = min(depth, _MAX_BOOK_LEVELS)
        with parsing(self.name, "l2Book"):
            if data["coin"] != symbol:
                raise ExchangeRequestError(f"{self.name}: l2Book returned {data['coin']} for {symbol}")
            bids, asks = data["levels"]
            return OrderBook(
                venue=self.name,
                symbol=symbol,
                bids=book_levels(bids, descending=True, depth=limit,
                                 price_of=lambda r: r["px"], qty_of=lambda r: r["sz"]),
                asks=book_levels(asks, descending=False, depth=limit,
                                 price_of=lambda r: r["px"], qty_of=lambda r: r["sz"]),
                source_time=ms_to_dt(data["time"]),
                received_at=self._now(),
            )

    # ---- candles --------------------------------------------------------------------------
    async def get_candles(self, symbol: str, timeframe: str = "1m", limit: int = 200,
                          include_open: bool = False) -> list[Candle]:
        """Oldest first. Only CLOSED candles by default (the still-forming candle is dropped);
        include_open=True keeps it. limit <= 4999. volume is base-unit volume."""
        interval = _INTERVALS.get(timeframe)
        if interval is None:
            raise ValueError(f"{self.name}: unsupported timeframe {timeframe!r}; use one of {sorted(_INTERVALS)}")
        check_positive("limit", limit, _MAX_CANDLES - 1)
        now = self._now()
        end_ms = int(now.timestamp() * 1000)
        start_ms = end_ms - (limit + 1) * TIMEFRAME_MS[timeframe]
        data = await self._info({
            "type": "candleSnapshot",
            "req": {"coin": symbol, "interval": interval, "startTime": start_ms, "endTime": end_ms},
        })
        with parsing(self.name, "candleSnapshot"):
            candles = [
                Candle(
                    venue=self.name, symbol=symbol, open_time=ms_to_dt(r["t"]),
                    open=dec(r["o"]), high=dec(r["h"]), low=dec(r["l"]), close=dec(r["c"]), volume=dec(r["v"]),
                )
                for r in data
            ]
        candles.sort(key=lambda c: c.open_time)
        if not include_open:
            candles = [c for c in candles if is_closed(c.open_time, timeframe, now)]
        return candles[-limit:]
