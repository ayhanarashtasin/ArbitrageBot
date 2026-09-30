"""OKX v5 perpetual-swap (SWAP) PUBLIC market-data adapter, LINEAR contracts only. name = "okx_swap".

instType=SWAP rows with ctType "linear" are served. Inverse swaps (BTC-USD-SWAP, ctType "inverse",
contract value in USD, margined in the coin) and X-Perps (instType FUTURES, e.g.
XAU-USD_UM_XPERP-310502) are ignored. Base / quote / settle come from each instrument's own fields,
never from the instId string:
    base   = ctValCcy   (docs: "for linear contracts, ctVal is in the base currency")
    settle = settleCcy
    quote  = settleCcy  (docs, ctType: "linear: margin, P&L, and settlement in the quote currency";
                         baseCcy / quoteCcy are documented SPOT/MARGIN-only and are "" for SWAP)
A row whose instFamily is not exactly "<ctValCcy>-<settleCcy>" is skipped as uninterpretable (all 478
live linear rows match, 2026-09-30).

USDC: verified live 2026-09-30, every linear SWAP settles in USDT and instId=BTC-USDC-SWAP answers
51001 "doesn't exist". OKX delisted its USDC-margined perpetuals (BTCUSDC, ETHUSDC) on 2025-12-11
(https://www.okx.com/help/okx-to-delist-usdc-margined-perpetual-futures). A USDC-settled linear
swap, should OKX list one again, is parsed the same way (quote = settle = USDC); none exists today.

Endpoints (all public, no key, no signature; verified against https://www.okx.com/docs-v5/en/ and live
on 2026-09-30; limits are the documented per-endpoint limits):
    GET /api/v5/public/time                  server time                       10 req / 2 s / IP
    GET /api/v5/public/instruments           instType=SWAP, one unpaginated     20 req / 2 s / IP+instType
                                             response (493 rows live)
    GET /api/v5/public/funding-rate          instId=<swap> or instId=ANY (all   10 req / 2 s / IP+instId
                                             SWAP + X-Perps rows in one call)
    GET /api/v5/public/funding-rate-history  settled funding, NEWEST first,     10 req / 2 s / IP+instId
                                             limit <= 400
    GET /api/v5/public/mark-price            markPx                            10 req / 2 s / IP+instId
    GET /api/v5/market/index-tickers         idxPx, instId = the swap's uly     20 req / 2 s / IP
    GET /api/v5/market/ticker                bid/ask/last, 24h volume           20 req / 2 s / IP
    GET /api/v5/market/books                 sz <= 400 levels per side          40 req / 2 s / IP
                                             (live: sz=400 -> 400, sz=401 -> 51000)
    GET /api/v5/market/candles               NEWEST first, limit <= 300 per     40 req / 2 s / IP
                                             call (live: limit=301 returns 300), only the latest
                                             1,440 rows are reachable (live: an `after` beyond them
                                             returns an empty page)
    GET /api/v5/market/history-candles       same row format, older history,    20 req / 2 s / IP
                                             limit <= 300 per call

OKX reports application errors as HTTP 200 with code != "0" (live: 51001 unknown instId, 51000
parameter error, the latter WITHOUT a "data" key), so every envelope is checked. Rate limiting is code
50011 (HTTP 200 or 429); 50013 "Systems are busy" comes with HTTP 429; neither is retried (HTTP 429 is
refused by PublicHttp, the codes by _call).

UNITS (all verified live on BTC-USDT-SWAP: ctVal 0.01 BTC, ctMult 1):
- lotSz, minSz, maxLmtSz, maxMktSz, book sizes and candle `vol` are CONTRACTS. They are converted to
  BASE units with ctVal: qty_step = lotSz * ctVal, min_qty = minSz * ctVal, book quantity = sz * ctVal,
  contract_multiplier = ctVal. A row with ctMult != 1 is skipped (the documented linear notional,
  sz * ctVal * markPx, has no ctMult term, so such a row could not be interpreted safely).
- Candle volume is `volCcy`, documented "the number of base currency" for derivatives (live: equals
  vol * ctVal on all 300 rows checked).
- Ticker volCcy24h is BASE currency for derivatives (live: 60927.2882 = vol24h 6092728.82 * 0.01).
  OKX's ticker has no quote-volume field, so volume_24h_usd = volCcy24h * last, converted only for
  USD-like quotes. That is an approximation (24h base volume valued at the current last price).
- Funding rates are FRACTIONS per funding interval. Positive = long pays short (documented).

FUNDING RATE SEMANTICS (get_funding_info): /public/funding-rate `fundingRate` is documented as the
"Predicted funding rate for the upcoming settlement period ... This is a forecast - the final settled
rate may differ" and it settles at `fundingTime` (live: fundingTime is the NEXT settlement, e.g. 16:00
UTC when polled at 08:36 UTC, and nextFundingTime is the settlement after that). So funding_rate is
that running estimate, is_predicted=True and next_funding_time = fundingTime. `settFundingRate` is the
last settled rate; settled history comes from get_funding_history(), which returns `realizedRate`
(documented "Actual funding rate"; `fundingRate` in the history rows is documented "Predicted").

FUNDING INTERVAL: the docs say the interval is "typically 8 hours but may be adjusted; use the
difference between fundingTime and nextFundingTime to determine the actual interval". That is what is
used, per symbol: for InstrumentMeta.funding_interval_hours from one funding-rate?instId=ANY call made
with each instrument load (live: 282 swaps at 8 h, 211 at 4 h), and in get_funding_info() from the
symbol's own funding-rate row (same call, never stale), falling back to the cached instrument value.
No default is assumed: without a usable pair of times the interval is None and get_funding_info()
raises instead of guessing.

TRADABILITY: is_tradable is state == "live". The live list also contains ruleType "pre_market" swaps
(pre-IPO names such as ANTHROPIC-USDT-SWAP) whose state is "live"; they are reported tradable like any
other live swap.

Not obtainable from public endpoints, therefore not provided: fees (get_fee_schedule stays
ExchangeNotSupported; the public fee page lists 0.02 % maker / 0.05 % taker for regular Lv1 users on
USDT-margined perpetuals, https://www.okx.com/help/how-to-calculate-the-contract-transaction-fee).
min_notional is None (OKX publishes no minimum notional for swaps; minSz is the only floor).
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

_INST_TYPE = "SWAP"
_LINEAR = "linear"
_LIVE = "live"
_CANDLES = "/api/v5/market/candles"
_HISTORY_CANDLES = "/api/v5/market/history-candles"
_CANDLE_PAGE = 300  # documented per-call maximum of both candle endpoints (verified live)
_MAX_CANDLES = 1500  # per get_candles() call: at most 6 full pages
# full pages for limit + 1 rows, plus one short page at the 1,440-row edge of /market/candles and the
# empty page that hands over to /market/history-candles
_MAX_CANDLE_PAGES = -(-(_MAX_CANDLES + 1) // _CANDLE_PAGE) + 2
_MAX_ORDERBOOK_DEPTH = 400  # documented and verified live (401 -> code 51000)
_MAX_FUNDING_HISTORY = 400  # documented per-call maximum
_MS_PER_HOUR = Decimal(3_600_000)
_RATE_LIMIT_CODES = frozenset({"50011", "50013", "50061"})  # rate limit / systems busy / order rate
_BARS = {  # project timeframe -> OKX bar (each verified live; lowercase "1h" is rejected with 51000)
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "2h": "2H", "4h": "4H",
    # 6H/12H/1D open on UTC+8 boundaries; the *utc bars open on UTC boundaries like the other venues
    "6h": "6Hutc", "12h": "12Hutc", "1d": "1Dutc",
}


def _interval_hours(row: dict) -> Decimal | None:
    """nextFundingTime - fundingTime in hours (the documented way to read the interval), or None."""
    try:
        current, following = int(row["fundingTime"]), int(row["nextFundingTime"])
    except (KeyError, TypeError, ValueError):
        return None
    if current <= 0 or following <= current:
        return None
    return Decimal(following - current) / _MS_PER_HOUR


class OkxSwapAdapter(PublicMarketDataAdapter):
    name = "okx_swap"
    DEFAULT_BASE_URL = "https://www.okx.com"

    def __init__(self, *, base_url: str = DEFAULT_BASE_URL, **kwargs):
        super().__init__(base_url=base_url, **kwargs)
        self._index_of: dict[str, str] = {}  # instId -> index id (the swap's uly), set by each load

    async def _call(self, path: str, params: dict[str, Any]) -> list:
        """GET a v5 endpoint and return its `data` list, raising on code != "0"."""
        body = await self._http.get_json(path, params=params)
        with parsing(self.name, path):
            code = str(body["code"])
            if code == "0":
                data = body["data"]
                if not isinstance(data, list):
                    raise TypeError("data is not a list")
                return data
            message = str(body.get("msg", ""))[:120]
        if code in _RATE_LIMIT_CODES:
            raise ExchangeRequestError(f"{self.name}: rate limit (code={code}) on {path}; not retried")
        raise ExchangeRequestError(f"{self.name}: {path} failed (code={code}, msg={message})")

    def _single(self, rows: list, symbol: str, what: str) -> dict:
        if len(rows) != 1 or rows[0]["instId"] != symbol:
            raise ExchangeRequestError(f"{self.name}: {what} returned no unique row for {symbol}")
        return rows[0]

    # ---- time -----------------------------------------------------------------------------
    async def get_server_time(self) -> datetime:
        rows = await self._call("/api/v5/public/time", {})
        with parsing(self.name, "server time"):
            return ms_to_dt(rows[0]["ts"])

    # ---- instruments ----------------------------------------------------------------------
    async def _load_instruments(self) -> list[InstrumentMeta]:
        rows, funding_rows = await asyncio.gather(
            self._call("/api/v5/public/instruments", {"instType": _INST_TYPE}),
            self._call("/api/v5/public/funding-rate", {"instId": "ANY"}),
        )
        received_at = self._now()
        with parsing(self.name, "funding-rate"):
            intervals = {r["instId"]: _interval_hours(r) for r in funding_rows if r.get("instType") == _INST_TYPE}
        with parsing(self.name, "instruments"):
            linear = [r for r in rows if r.get("instType") == _INST_TYPE and r.get("ctType") == _LINEAR]
        result: list[InstrumentMeta] = []
        index_of: dict[str, str] = {}
        for raw in linear:
            try:
                meta = self._parse_instrument(raw, intervals.get(raw["instId"]), received_at)
                index_of[meta.symbol] = raw.get("uly") or raw["instFamily"]
            except (KeyError, IndexError, TypeError, ValueError, ArithmeticError) as exc:
                # One malformed row must not hide every other market; that symbol simply cannot resolve.
                log.warning("%s: skipping unparseable instrument %r (%s: %s)", self.name, raw.get("instId"),
                            type(exc).__name__, exc)
                continue
            result.append(meta)
        if linear and not result:
            raise ExchangeRequestError(f"{self.name}: no instruments row could be parsed")
        self._index_of = index_of
        return result

    def _parse_instrument(self, raw: dict, interval_hours: Decimal | None, received_at: datetime) -> InstrumentMeta:
        base, settle = raw["ctValCcy"], raw["settleCcy"]
        if not base or not settle or raw["instFamily"] != f"{base}-{settle}":
            raise ValueError(f"instFamily {raw.get('instFamily')!r} does not match ctValCcy/settleCcy")
        if raw.get("ctMult") not in (None, "") and dec(raw["ctMult"]) != 1:
            raise ValueError(f"ctMult {raw['ctMult']} != 1")
        ct_val = dec(raw["ctVal"])  # base units per contract
        if ct_val <= 0:
            raise ValueError(f"ctVal {ct_val} is not positive")
        caps = [c for c in (opt_dec(raw.get("maxLmtSz")), opt_dec(raw.get("maxMktSz"))) if c is not None]
        return InstrumentMeta(
            venue=self.name,
            symbol=raw["instId"],
            base=base,
            quote=settle,  # linear: settled in the quote currency (see module docstring)
            settle=settle,
            is_linear=True,
            is_perpetual=True,
            is_tradable=raw["state"] == _LIVE,
            contract_multiplier=ct_val,
            tick_size=dec(raw["tickSz"]),
            qty_step=dec(raw["lotSz"]) * ct_val,
            min_qty=dec(raw["minSz"]) * ct_val,
            # tighter of the limit and market caps: a quantity <= max_qty is accepted for both
            max_qty=min(caps) * ct_val if caps else None,
            min_notional=None,
            funding_interval_hours=interval_hours,
            max_leverage=opt_dec(raw.get("lever")),
            received_at=received_at,
        )

    # ---- mark / index ---------------------------------------------------------------------
    async def _mark_price(self, symbol: str) -> Decimal:
        rows = await self._call("/api/v5/public/mark-price", {"instType": _INST_TYPE, "instId": symbol})
        with parsing(self.name, "mark-price"):
            return dec(self._single(rows, symbol, "mark-price")["markPx"])

    async def _index_price(self, index_id: str | None) -> Decimal | None:
        if not index_id:
            return None
        rows = await self._call("/api/v5/market/index-tickers", {"instId": index_id})
        with parsing(self.name, "index-tickers"):
            if not rows:
                return None
            return dec(self._single(rows, index_id, "index-tickers")["idxPx"])

    # ---- funding --------------------------------------------------------------------------
    async def get_funding_info(self, symbol: str) -> FundingInfo:
        meta = await self.get_instrument_metadata(symbol)  # linear swaps only; loads the index map
        rows, mark, index = await asyncio.gather(
            self._call("/api/v5/public/funding-rate", {"instId": symbol}),
            self._mark_price(symbol),
            self._index_price(self._index_of.get(symbol)),
        )
        with parsing(self.name, "funding-rate"):
            row = self._single(rows, symbol, "funding-rate")
            funding_rate = dec(row["fundingRate"])
            next_time = opt_ms_to_dt(row["fundingTime"])  # the settlement fundingRate applies to
            source_time = opt_ms_to_dt(row.get("ts"))
        interval_hours = _interval_hours(row) or meta.funding_interval_hours
        if not interval_hours:
            raise ExchangeRequestError(f"{self.name}: no funding interval published for {symbol}")
        return FundingInfo(
            venue=self.name,
            symbol=symbol,
            funding_rate=funding_rate,  # predicted rate for the current period, see module docstring
            interval_hours=interval_hours,
            next_funding_time=next_time,
            is_predicted=True,
            mark_price=mark,
            index_price=index,
            source_time=source_time,
            received_at=self._now(),
        )

    async def get_funding_history(self, symbol: str, limit: int = 100) -> list[tuple[datetime, Decimal]]:
        """Settled funding (realizedRate), oldest first. limit <= 400 (documented per-call maximum)."""
        check_positive("limit", limit, _MAX_FUNDING_HISTORY)
        rows = await self._call("/api/v5/public/funding-rate-history", {"instId": symbol, "limit": limit})
        with parsing(self.name, "funding-rate-history"):
            if any(r["instId"] != symbol for r in rows):
                raise ExchangeRequestError(f"{self.name}: funding-rate-history returned rows for another symbol")
            history = [(ms_to_dt(r["fundingTime"]), dec(r["realizedRate"])) for r in rows]
        history.sort(key=lambda item: item[0])  # the venue returns newest first
        return history

    # ---- ticker ---------------------------------------------------------------------------
    async def get_ticker(self, symbol: str) -> Ticker:
        """bid/ask/last/24h volume from /market/ticker, mark from /public/mark-price."""
        rows, mark, meta = await asyncio.gather(
            self._call("/api/v5/market/ticker", {"instId": symbol}),
            self._mark_price(symbol),
            self.get_instrument_metadata(symbol),
        )
        with parsing(self.name, "ticker"):
            row = self._single(rows, symbol, "ticker")
            last, base_volume = opt_dec(row["last"]), opt_dec(row["volCcy24h"])
            # volCcy24h is BASE currency for derivatives: valued at the last price, see module docstring
            quote_volume = base_volume * last if base_volume is not None and last is not None else None
            return Ticker(
                venue=self.name,
                symbol=symbol,
                bid=opt_dec(row["bidPx"]),
                ask=opt_dec(row["askPx"]),
                last=last,
                mark=mark,
                volume_24h_usd=usd_volume(meta.quote, quote_volume),
                source_time=opt_ms_to_dt(row["ts"]),
                received_at=self._now(),
            )

    # ---- order book -----------------------------------------------------------------------
    async def get_order_book(self, symbol: str, depth: int = 50) -> OrderBook:
        """Snapshot with up to `depth` (<= 400) levels per side, quantities converted from contracts to
        BASE units (sz * ctVal). source_time is `ts`, the documented order book generation time."""
        check_positive("depth", depth, _MAX_ORDERBOOK_DEPTH)
        rows, meta = await asyncio.gather(
            self._call("/api/v5/market/books", {"instId": symbol, "sz": depth}),
            self.get_instrument_metadata(symbol),
        )
        ct_val = meta.contract_multiplier

        def qty_of(row):
            return dec(row[1]) * ct_val

        with parsing(self.name, "books"):
            if len(rows) != 1:
                raise ExchangeRequestError(f"{self.name}: books returned {len(rows)} snapshots for {symbol}")
            book = rows[0]
            return OrderBook(
                venue=self.name,
                symbol=symbol,
                bids=book_levels(book["bids"], descending=True, depth=depth, qty_of=qty_of),
                asks=book_levels(book["asks"], descending=False, depth=depth, qty_of=qty_of),
                source_time=opt_ms_to_dt(book["ts"]),
                received_at=self._now(),
            )

    # ---- candles --------------------------------------------------------------------------
    async def get_candles(self, symbol: str, timeframe: str = "1m", limit: int = 200,
                          include_open: bool = False) -> list[Candle]:
        """Oldest first. Only CLOSED candles by default; include_open=True keeps the forming one.
        limit <= 1500. volume is base-currency volume (volCcy).

        OKX returns at most 300 rows per call, newest first, so older rows are paged with the
        documented `after` cursor (exclusive: rows strictly older than the given open time).
        /market/candles only reaches the latest 1,440 rows; when it answers an empty page the walk
        continues on /market/history-candles with the same cursor, and stops when that runs out.
        A candle counts as closed when its interval has elapsed AND OKX marks it confirm == "1".
        """
        bar = _BARS.get(timeframe)
        if bar is None:
            raise ValueError(f"{self.name}: unsupported timeframe {timeframe!r}; use one of {sorted(_BARS)}")
        check_positive("limit", limit, _MAX_CANDLES)
        wanted = limit + 1  # one spare row: only the newest candle can still be forming
        rows: dict[int, list] = {}
        endpoint, cursor = _CANDLES, None
        for _ in range(_MAX_CANDLE_PAGES):
            size = min(_CANDLE_PAGE, wanted - len(rows))
            params: dict[str, Any] = {"instId": symbol, "bar": bar, "limit": size}
            if cursor is not None:
                params["after"] = cursor
            page = await self._call(endpoint, params)
            if not page:
                if endpoint == _CANDLES and cursor is not None:
                    endpoint = _HISTORY_CANDLES  # past the 1,440-row window of /market/candles
                    continue
                break  # no (older) history
            with parsing(self.name, "candles"):
                stamps = [int(r[0]) for r in page]
            oldest = min(stamps)
            if cursor is not None and oldest >= cursor:
                raise ExchangeRequestError(f"{self.name}: candle pagination did not move back in time")
            rows.update(zip(stamps, page, strict=True))
            cursor = oldest
            if len(rows) >= wanted:
                break
        else:
            raise ExchangeRequestError(
                f"{self.name}: candle pagination did not finish within {_MAX_CANDLE_PAGES} requests")
        now = self._now()
        with parsing(self.name, "candles"):
            parsed = [
                (Candle(
                    venue=self.name, symbol=symbol, open_time=ms_to_dt(r[0]),
                    open=dec(r[1]), high=dec(r[2]), low=dec(r[3]), close=dec(r[4]), volume=dec(r[6]),
                ), r[8] == "1")
                for _, r in sorted(rows.items())  # oldest first
            ]
        if not include_open:
            parsed = [(c, confirmed) for c, confirmed in parsed
                      if confirmed and is_closed(c.open_time, timeframe, now)]
        return [c for c, _ in parsed][-limit:]
