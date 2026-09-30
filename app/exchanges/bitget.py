"""Bitget classic v2 USDT perpetuals, public reads only.

Verified against https://www.bitget.com/docs/catalog/classic-contract-market/classic-contract-market
and public BTC responses on 2026-09-30. Sizes/volume are BASE units, sizeMultiplier is the quantity step;
priceEndStep * 10^-pricePlace is the price tick. Current funding carries its own interval and nextUpdate.
Candles: max 1000 per request, paginated backwards with endTime; only closed bars returned by default.
RWA instruments have no stock/commodity subtype, so their label is TRADFI and earnings is checked conservatively.
No private endpoints, signing or order implementation. Fees remain labelled paper assumptions.
"""

from decimal import Decimal

from app.domain.market import Candle, FundingInfo, InstrumentMeta, OrderBook, Ticker
from ._common import (PublicMarketDataAdapter, book_levels, check_positive, dec, is_closed,
                      ms_to_dt, opt_dec, opt_ms_to_dt, parsing)
from .base import ExchangeRequestError

D = Decimal
ROOT = "/api/v2/mix/market/"
FRAMES = {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
          "1h": "1H", "4h": "4H", "6h": "6H", "12h": "12H", "1d": "1Dutc"}


class BitgetUsdtAdapter(PublicMarketDataAdapter):
    name = "bitget_usdt"

    def __init__(self, *, base_url="https://api.bitget.com", **kwargs):
        super().__init__(base_url=base_url, **kwargs)

    async def _get(self, endpoint, **params):
        body = await self._http.get_json(ROOT + endpoint, {"productType": "USDT-FUTURES", **params})
        return self._data(body)

    def _data(self, body):
        with parsing(self.name, "envelope"):
            if body["code"] != "00000":
                raise ExchangeRequestError(f"{self.name}: public API error {body['code']}: {str(body.get('msg', ''))[:120]}")
            return body["data"]

    async def get_server_time(self):
        body = await self._http.get_json("/api/v2/public/time")
        with parsing(self.name, "time"):
            return ms_to_dt(self._data(body)["serverTime"])

    async def _load_instruments(self):
        rows = await self._get("contracts")
        out = []
        for r in rows:
            with parsing(self.name, "contract"):
                if r["quoteCoin"] != "USDT" or "USDT" not in r["supportMarginCoins"] or r["symbolType"] != "perpetual":
                    continue
                now_ms = int(self._now().timestamp() * 1000)
                restricted = any(int(r.get(k) or -1) > 0 and int(r[k]) <= now_ms
                                 for k in ("offTime", "limitOpenTime"))
                interval = opt_dec(r.get("fundInterval"))
                out.append(InstrumentMeta(
                    venue=self.name, symbol=r["symbol"], base=r["baseCoin"], quote="USDT", settle="USDT",
                    is_linear=True, is_perpetual=True, is_tradable=r["symbolStatus"] == "normal" and not restricted,
                    contract_multiplier=D(1), tick_size=dec(r["priceEndStep"]) * D(10) ** -int(r["pricePlace"]),
                    qty_step=dec(r["sizeMultiplier"]), min_qty=dec(r["minTradeNum"]),
                    min_notional=opt_dec(r.get("minTradeUSDT")), max_qty=opt_dec(r.get("maxMarketOrderQty")),
                    funding_interval_hours=interval if interval and interval > 0 else None,
                    max_leverage=opt_dec(r.get("maxLever")),
                    underlying_type="TRADFI" if r.get("isRwa") == "YES" else "COIN", received_at=self._now()))
        return out

    async def get_funding_info(self, symbol):
        rows = await self._get("current-fund-rate", symbol=symbol)
        with parsing(self.name, "funding"):
            if len(rows) != 1 or rows[0]["symbol"] != symbol:
                raise ValueError("no unique funding row")
            r = rows[0]
            if dec(r["fundingRateInterval"]) <= 0:
                raise ValueError("funding interval must be positive")
            return FundingInfo(venue=self.name, symbol=symbol, funding_rate=dec(r["fundingRate"]),
                               interval_hours=dec(r["fundingRateInterval"]),
                               next_funding_time=opt_ms_to_dt(r["nextUpdate"]), is_predicted=True,
                               mark_price=None, index_price=None, source_time=None,
                               received_at=self._now())

    async def get_funding_history(self, symbol, limit=100):
        check_positive("limit", limit, 100)
        rows = await self._get("history-fund-rate", symbol=symbol, pageSize=limit, pageNo=1)
        with parsing(self.name, "funding history"):
            return sorted((ms_to_dt(r["fundingTime"]), dec(r["fundingRate"])) for r in rows)

    async def get_ticker(self, symbol):
        rows = await self._get("ticker", symbol=symbol)
        with parsing(self.name, "ticker"):
            if len(rows) != 1 or rows[0]["symbol"] != symbol:
                raise ValueError("no unique ticker row")
            r = rows[0]
            return Ticker(venue=self.name, symbol=symbol, bid=opt_dec(r.get("bidPr")), ask=opt_dec(r.get("askPr")),
                          last=opt_dec(r.get("lastPr")), mark=opt_dec(r.get("markPrice")),
                          volume_24h_usd=opt_dec(r.get("usdtVolume")), source_time=opt_ms_to_dt(r.get("ts")),
                          received_at=self._now())

    async def get_order_book(self, symbol, depth=50):
        check_positive("depth", depth, 200)
        tier = next((n for n in (1, 5, 15, 50) if n >= depth), "max")
        r = await self._get("merge-depth", symbol=symbol, precision="scale0", limit=tier)
        with parsing(self.name, "book"):
            return OrderBook(venue=self.name, symbol=symbol,
                             bids=book_levels(r["bids"], descending=True, depth=depth),
                             asks=book_levels(r["asks"], descending=False, depth=depth),
                             source_time=opt_ms_to_dt(r.get("ts")), received_at=self._now())

    async def get_candles(self, symbol, timeframe="1m", limit=200, include_open=False):
        check_positive("limit", limit, 5000)
        if timeframe not in FRAMES:
            raise ValueError(f"unsupported timeframe {timeframe}")
        end = int(self._now().timestamp() * 1000)
        found = {}
        for _ in range((limit + 999) // 1000 + 1):
            rows = await self._get("candles", symbol=symbol, granularity=FRAMES[timeframe], limit=1000, endTime=end)
            if not rows:
                break
            with parsing(self.name, "candles"):
                for r in rows:
                    t = ms_to_dt(r[0])
                    if include_open or is_closed(t, timeframe, self._now()):
                        found[t] = Candle(venue=self.name, symbol=symbol, open_time=t, open=dec(r[1]), high=dec(r[2]),
                                          low=dec(r[3]), close=dec(r[4]), volume=dec(r[5]))
                older = min(int(r[0]) for r in rows) - 1
            if len(found) >= limit or older >= end:
                break
            end = older
        return [found[t] for t in sorted(found)][-limit:]
