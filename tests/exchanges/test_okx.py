"""OKX swap adapter tests. Fixtures are real public responses captured 2026-09-30 ~08:36 UTC, only trimmed
(fewer instruments / rows / levels). Rows marked "synthetic" are built in the test to reach a branch
the live data cannot show (no USDC-settled swap exists on OKX today, and candle pagination needs more
rows than a fixture should hold); they follow the response shapes and cursor semantics verified live.
"""

import copy
import itertools
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from app.exchanges.base import (
    ExchangeNotSupported,
    ExchangeRequestError,
    SymbolNotFound,
)
from app.exchanges.okx import OkxSwapAdapter

from .helpers import Harness, by_path, load

V = "okx"
OFFLINE = httpx.MockTransport(lambda request: httpx.Response(500))  # never reached; avoids TLS setup cost
INSTRUMENTS = load(V, "instruments_swap.json")
FUNDING_ANY = load(V, "funding_rate_any.json")
FUNDING = load(V, "funding_rate_btcusdt.json")
HISTORY = load(V, "funding_rate_history_btcusdt.json")
TICKER = load(V, "ticker_btcusdt.json")
MARK = load(V, "mark_price_btcusdt.json")
INDEX = load(V, "index_tickers_btcusdt.json")
BOOKS = load(V, "books_btcusdt.json")
CANDLES_1M = load(V, "candles_btcusdt_1m.json")
CANDLES_1H = load(V, "candles_btcusdt_1h.json")

INSTRUMENTS_PATH = "/api/v5/public/instruments"
FUNDING_PATH = "/api/v5/public/funding-rate"
CANDLES_PATH = "/api/v5/market/candles"
HISTORY_CANDLES_PATH = "/api/v5/market/history-candles"


def dt(ms) -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=int(ms))


def funding_route(request: httpx.Request):
    """/public/funding-rate serves both instId=ANY (instrument load) and a single instId."""
    return FUNDING_ANY if request.url.params["instId"] == "ANY" else FUNDING


def candles_route(request: httpx.Request):
    """The captured candle rows, honouring `after` (exclusive) and `limit` like the venue does."""
    params = request.url.params
    rows = (CANDLES_1H if params.get("bar") == "1H" else CANDLES_1M)["data"]
    if "after" in params:
        rows = [r for r in rows if int(r[0]) < int(params["after"])]
    return envelope(rows[:int(params.get("limit", 100))])


def routes(**overrides):
    base = {
        "/api/v5/public/time": load(V, "server_time.json"),
        INSTRUMENTS_PATH: INSTRUMENTS,
        FUNDING_PATH: funding_route,
        "/api/v5/public/funding-rate-history": HISTORY,
        "/api/v5/public/mark-price": MARK,
        "/api/v5/market/index-tickers": INDEX,
        "/api/v5/market/ticker": TICKER,
        "/api/v5/market/books": BOOKS,
        CANDLES_PATH: candles_route,
        HISTORY_CANDLES_PATH: candles_route,
    }
    base.update(overrides)
    return by_path(base)


def run(action, h=None, handler=None, **kw):
    h = h or Harness()
    return h.run(OkxSwapAdapter, handler or routes(), action, **kw)


def instruments(handler=None):
    return {i.symbol: i for i in run(lambda a: a.list_instruments(), handler=handler)}


def envelope(data, code="0", msg=""):
    return {"code": code, "data": data, "msg": msg}


def raw_instrument(inst_id, payload=INSTRUMENTS):
    return next(r for r in payload["data"] if r["instId"] == inst_id)


def with_instruments(mutate):
    payload = copy.deepcopy(INSTRUMENTS)
    mutate(payload)
    return routes(**{INSTRUMENTS_PATH: payload})


# ---- instruments -------------------------------------------------------------------------------
def test_instrument_parsing_converts_contracts_to_base_units():
    raw = raw_instrument("BTC-USDT-SWAP")
    assert (raw["ctVal"], raw["lotSz"], raw["minSz"], raw["ctValCcy"]) == ("0.01", "0.01", "0.01", "BTC")
    btc = instruments()["BTC-USDT-SWAP"]
    assert (btc.venue, btc.base, btc.quote, btc.settle) == ("okx_swap", "BTC", "USDT", "USDT")
    assert btc.is_linear and btc.is_perpetual and btc.is_tradable
    assert btc.contract_multiplier == Decimal("0.01")  # BTC per contract
    assert btc.qty_step == Decimal("0.0001") and btc.min_qty == Decimal("0.0001")  # 0.01 contract * 0.01 BTC
    assert btc.max_qty == Decimal(350)  # min(maxLmtSz 100000000, maxMktSz 35000) contracts * 0.01 BTC
    assert btc.tick_size == Decimal("0.1")
    assert btc.min_notional is None
    assert btc.funding_interval_hours == Decimal(8)  # nextFundingTime - fundingTime from instId=ANY
    assert btc.max_leverage == Decimal(100)


def test_other_contract_values_are_converted_too():
    inst = instruments()
    eth, pepe, tsla = inst["ETH-USDT-SWAP"], inst["PEPE-USDT-SWAP"], inst["TSLA-USDT-SWAP"]
    assert (eth.contract_multiplier, eth.qty_step, eth.min_qty) == (Decimal("0.1"), Decimal("0.001"), Decimal("0.001"))
    # PEPE: one contract is 10,000,000 PEPE, lotSz 0.1 contract -> 1,000,000 PEPE
    assert pepe.base == "PEPE" and pepe.contract_multiplier == Decimal(10000000)
    assert pepe.qty_step == Decimal(1000000) and pepe.min_qty == Decimal(1000000)
    assert tsla.base == "TSLA" and tsla.contract_multiplier == 1 and tsla.qty_step == Decimal("0.01")


def test_only_linear_swaps_are_listed():
    inst = instruments()
    assert raw_instrument("BTC-USD-SWAP")["ctType"] == "inverse"
    assert "BTC-USD-SWAP" not in inst
    assert set(inst) == {r["instId"] for r in INSTRUMENTS["data"] if r["ctType"] == "linear"}
    assert all(i.is_linear and i.quote == i.settle == "USDT" for i in inst.values())


def test_base_quote_settle_come_from_instrument_fields():
    usdc = instruments()["USDC-USDT-SWAP"]  # the one live SWAP with "USDC" in its id: base USDC, USDT-settled
    assert (usdc.base, usdc.quote, usdc.settle) == ("USDC", "USDT", "USDT")


def test_funding_interval_per_symbol_from_funding_rate_any():
    tia = next(r for r in FUNDING_ANY["data"] if r["instId"] == "TIA-USDT-SWAP")
    assert int(tia["nextFundingTime"]) - int(tia["fundingTime"]) == 4 * 3_600_000
    inst = instruments()
    assert inst["TIA-USDT-SWAP"].funding_interval_hours == Decimal(4)
    assert inst["ETH-USDT-SWAP"].funding_interval_hours == Decimal(8)


def test_instrument_load_requests():
    h = Harness()
    run(lambda a: a.list_instruments(), h)
    queries = {r.url.path: dict(r.url.params) for r in h.calls}
    assert queries == {INSTRUMENTS_PATH: {"instType": "SWAP"}, FUNDING_PATH: {"instId": "ANY"}}


def test_missing_funding_times_leave_interval_unknown():
    payload = copy.deepcopy(FUNDING_ANY)
    next(r for r in payload["data"] if r["instId"] == "ETH-USDT-SWAP")["nextFundingTime"] = ""
    payload["data"] = [r for r in payload["data"] if r["instId"] != "TSLA-USDT-SWAP"]
    inst = instruments(routes(**{FUNDING_PATH: payload}))
    assert inst["ETH-USDT-SWAP"].funding_interval_hours is None
    assert inst["TSLA-USDT-SWAP"].funding_interval_hours is None
    assert inst["BTC-USDT-SWAP"].funding_interval_hours == 8


def test_state_other_than_live_is_not_tradable():
    handler = with_instruments(lambda p: raw_instrument("ETH-USDT-SWAP", p).update(state="suspend"))
    assert instruments(handler)["ETH-USDT-SWAP"].is_tradable is False
    with pytest.raises(SymbolNotFound, match="not tradable"):
        run(lambda a: a.resolve_symbol("ETH", "USDT", "USDT"), handler=handler)


def test_pre_market_swaps_follow_state():
    raw = raw_instrument("ANTHROPIC-USDT-SWAP")
    assert (raw["ruleType"], raw["state"]) == ("pre_market", "live")
    assert instruments()["ANTHROPIC-USDT-SWAP"].is_tradable is True


def test_one_malformed_instrument_is_skipped_not_fatal():
    handler = with_instruments(lambda p: raw_instrument("BTC-USDT-SWAP", p).pop("lotSz"))
    inst = instruments(handler)
    assert "BTC-USDT-SWAP" not in inst and "ETH-USDT-SWAP" in inst

    def break_all(p):
        for r in p["data"]:
            r.pop("tickSz")
    with pytest.raises(ExchangeRequestError, match="no instruments row could be parsed"):
        run(lambda a: a.list_instruments(), handler=with_instruments(break_all))


@pytest.mark.parametrize("field,value", [("instFamily", "BTC-USD"), ("ctValCcy", "ETH"), ("ctMult", "10"),
                                         ("ctVal", "0"), ("ctValCcy", "")])
def test_uninterpretable_instrument_rows_are_skipped(field, value):
    handler = with_instruments(lambda p: raw_instrument("BTC-USDT-SWAP", p).update({field: value}))
    inst = instruments(handler)
    assert "BTC-USDT-SWAP" not in inst and "ETH-USDT-SWAP" in inst


def test_instruments_cached_for_ten_minutes():
    h = Harness()

    async def go(a):
        await a.resolve_symbol("BTC", "USDT", "USDT")
        await a.resolve_symbol("ETH", "USDT", "USDT")
        h.monotonic = 599.0
        await a.list_instruments()
        assert h.paths().count(INSTRUMENTS_PATH) == 1
        h.monotonic = 600.5
        await a.list_instruments()
    run(go, h)
    assert h.paths().count(INSTRUMENTS_PATH) == 2


# ---- resolve_symbol ----------------------------------------------------------------------------
def test_resolve_symbol_usdt():
    async def go(a):
        return [await a.resolve_symbol(b, "USDT", "USDT") for b in ("BTC", "ETH", "USDC", "TSLA")]
    assert [m.symbol for m in run(go)] == ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "USDC-USDT-SWAP", "TSLA-USDT-SWAP"]


def test_resolve_symbol_usdc_is_not_listed_today():
    with pytest.raises(SymbolNotFound, match="no perpetual BTC/USDC:USDC"):
        run(lambda a: a.resolve_symbol("BTC", "USDC", "USDC"))
    with pytest.raises(SymbolNotFound):
        run(lambda a: a.resolve_symbol("BTC", "USD", "BTC"))  # the inverse BTC-USD-SWAP is never offered


def test_resolve_symbol_usdc_when_listed():
    def add_usdc(p):  # synthetic: OKX delisted its USDC-margined swaps on 2025-12-11
        clone = copy.deepcopy(raw_instrument("BTC-USDT-SWAP", p))
        clone.update(instId="BTC-USDC-SWAP", instFamily="BTC-USDC", uly="BTC-USDC", settleCcy="USDC")
        p["data"].append(clone)
    meta = run(lambda a: a.resolve_symbol("BTC", "USDC", "USDC"), handler=with_instruments(add_usdc))
    assert (meta.symbol, meta.base, meta.quote, meta.settle) == ("BTC-USDC-SWAP", "BTC", "USDC", "USDC")
    usdt = run(lambda a: a.resolve_symbol("BTC", "USDT", "USDT"), handler=with_instruments(add_usdc))
    assert usdt.symbol == "BTC-USDT-SWAP"


# ---- funding -----------------------------------------------------------------------------------
def test_funding_info_parsing():
    row = FUNDING["data"][0]
    h = Harness()
    f = run(lambda a: a.get_funding_info("BTC-USDT-SWAP"), h)
    assert f.venue == "okx_swap" and f.symbol == "BTC-USDT-SWAP"
    assert f.funding_rate == Decimal(row["fundingRate"]) == Decimal("0.0000248456077781")  # fraction
    assert f.interval_hours == 8
    # fundingTime is the upcoming settlement the predicted rate applies to (nextFundingTime is the one after)
    assert f.next_funding_time == dt(row["fundingTime"]) == datetime(2026, 9, 30, 16, 0, tzinfo=UTC)
    assert f.next_funding_time < dt(row["nextFundingTime"])
    assert f.is_predicted is True
    assert f.mark_price == Decimal(MARK["data"][0]["markPx"])
    assert f.index_price == Decimal(INDEX["data"][0]["idxPx"])
    assert f.source_time == dt(row["ts"])
    assert f.rate_per_hour == f.funding_rate / 8
    queries = {(r.url.path, tuple(sorted(r.url.params.items()))) for r in h.calls}
    assert (FUNDING_PATH, (("instId", "BTC-USDT-SWAP"),)) in queries
    assert ("/api/v5/public/mark-price", (("instId", "BTC-USDT-SWAP"), ("instType", "SWAP"))) in queries
    assert ("/api/v5/market/index-tickers", (("instId", "BTC-USDT"),)) in queries  # the swap's uly


def test_funding_interval_prefers_own_row_then_instrument_cache():
    def single(**changes):
        row = dict(FUNDING["data"][0], **changes)
        return routes(**{FUNDING_PATH: lambda r: FUNDING_ANY if r.url.params["instId"] == "ANY" else envelope([row])})
    four_hours = str(int(FUNDING["data"][0]["fundingTime"]) + 4 * 3_600_000)
    assert run(lambda a: a.get_funding_info("BTC-USDT-SWAP"),
               handler=single(nextFundingTime=four_hours)).interval_hours == 4
    assert run(lambda a: a.get_funding_info("BTC-USDT-SWAP"),
               handler=single(nextFundingTime="")).interval_hours == 8  # cached instId=ANY value


def test_funding_without_any_interval_is_an_error():
    single = dict(FUNDING["data"][0], nextFundingTime="")
    any_rows = copy.deepcopy(FUNDING_ANY)
    for r in any_rows["data"]:
        r["nextFundingTime"] = r["fundingTime"]
    handler = routes(**{FUNDING_PATH: lambda r: any_rows if r.url.params["instId"] == "ANY" else envelope([single])})
    with pytest.raises(ExchangeRequestError, match="no funding interval"):
        run(lambda a: a.get_funding_info("BTC-USDT-SWAP"), handler=handler)


def test_funding_info_index_is_optional_and_symbol_checked():
    f = run(lambda a: a.get_funding_info("BTC-USDT-SWAP"),
            handler=routes(**{"/api/v5/market/index-tickers": envelope([])}))
    assert f.index_price is None and f.mark_price is not None
    with pytest.raises(ExchangeRequestError, match="returned no unique row"):
        run(lambda a: a.get_funding_info("ETH-USDT-SWAP"))  # fixture rows are BTC-USDT-SWAP / BTC-USDT
    with pytest.raises(SymbolNotFound):
        run(lambda a: a.get_funding_info("BTC-USD-SWAP"))  # inverse: not a supported instrument


def test_funding_history_is_reordered_oldest_first_and_uses_realized_rate():
    rows = HISTORY["data"]
    assert int(rows[0]["fundingTime"]) > int(rows[-1]["fundingTime"])  # venue: newest first
    h = Harness()
    hist = run(lambda a: a.get_funding_history("BTC-USDT-SWAP", 10), h)
    assert h.query(0) == {"instId": "BTC-USDT-SWAP", "limit": "10"}
    assert [t for t, _ in hist] == sorted(t for t, _ in hist) and len(hist) == len(rows)
    assert hist[-1] == (dt(rows[0]["fundingTime"]), Decimal(rows[0]["realizedRate"]))
    assert hist[-1][1] == Decimal(FUNDING["data"][0]["settFundingRate"])  # last settled rate agrees
    # realizedRate ("Actual funding rate"), not fundingRate ("Predicted"): synthetic divergence
    diverged = copy.deepcopy(HISTORY)
    diverged["data"][0]["fundingRate"] = "0.5"
    hist = run(lambda a: a.get_funding_history("BTC-USDT-SWAP", 10),
               handler=routes(**{"/api/v5/public/funding-rate-history": diverged}))
    assert hist[-1][1] == Decimal(rows[0]["realizedRate"])


def test_funding_history_limit_bounds_and_symbol_check():
    for bad in (0, 401):
        with pytest.raises(ValueError):
            run(lambda a, bad=bad: a.get_funding_history("BTC-USDT-SWAP", bad))
    with pytest.raises(ExchangeRequestError, match="another symbol"):
        run(lambda a: a.get_funding_history("ETH-USDT-SWAP"))


# ---- ticker ------------------------------------------------------------------------------------
def test_ticker_parsing():
    row = TICKER["data"][0]
    h = Harness()
    t = run(lambda a: a.get_ticker("BTC-USDT-SWAP"), h)
    assert (t.bid, t.ask, t.last) == (Decimal(row["bidPx"]), Decimal(row["askPx"]), Decimal(row["last"]))
    assert t.mark == Decimal(MARK["data"][0]["markPx"])
    # volCcy24h is BTC (= vol24h contracts * ctVal 0.01); valued at the last price for a USDT quote
    assert Decimal(row["volCcy24h"]) == Decimal(row["vol24h"]) * Decimal("0.01")
    assert t.volume_24h_usd == Decimal(row["volCcy24h"]) * Decimal(row["last"])
    assert t.source_time == dt(row["ts"])
    ticker_call = next(r for r in h.calls if r.url.path == "/api/v5/market/ticker")
    assert dict(ticker_call.url.params) == {"instId": "BTC-USDT-SWAP"}


def test_ticker_empty_book_side_and_symbol_check():
    payload = copy.deepcopy(TICKER)
    payload["data"][0].update(bidPx="", askPx="")
    t = run(lambda a: a.get_ticker("BTC-USDT-SWAP"), handler=routes(**{"/api/v5/market/ticker": payload}))
    assert t.bid is None and t.ask is None
    with pytest.raises(ExchangeRequestError, match="no unique row"):
        run(lambda a: a.get_ticker("ETH-USDT-SWAP"))


# ---- order book --------------------------------------------------------------------------------
def test_order_book_converts_contracts_to_base_and_orders_levels():
    raw = BOOKS["data"][0]
    h = Harness()
    book = run(lambda a: a.get_order_book("BTC-USDT-SWAP", depth=20), h)
    books_call = next(r for r in h.calls if r.url.path == "/api/v5/market/books")
    assert dict(books_call.url.params) == {"instId": "BTC-USDT-SWAP", "sz": "20"}
    assert len(book.bids) == len(book.asks) == 20
    assert [lvl.price for lvl in book.bids] == sorted((lvl.price for lvl in book.bids), reverse=True)
    assert [lvl.price for lvl in book.asks] == sorted(lvl.price for lvl in book.asks)
    assert book.bids[0].price == Decimal(raw["bids"][0][0])
    assert book.bids[0].quantity == Decimal(raw["bids"][0][1]) * Decimal("0.01")  # contracts -> BTC
    assert book.asks[0].quantity == Decimal(raw["asks"][0][1]) * Decimal("0.01")
    assert book.best_bid < book.best_ask
    assert book.source_time == dt(raw["ts"])


def test_order_book_uses_each_symbols_contract_value():
    book = run(lambda a: a.get_order_book("ETH-USDT-SWAP", depth=5))  # same levels, ETH ctVal 0.1
    assert book.bids[0].quantity == Decimal(BOOKS["data"][0]["bids"][0][1]) * Decimal("0.1")


def test_order_book_sorts_trims_and_drops_empty_levels():
    raw = copy.deepcopy(BOOKS["data"][0])
    raw["bids"] = list(reversed(raw["bids"])) + [["1", "0", "0", "0"]]
    raw["asks"] = list(reversed(raw["asks"]))
    book = run(lambda a: a.get_order_book("BTC-USDT-SWAP", 5),
               handler=routes(**{"/api/v5/market/books": envelope([raw])}))
    assert len(book.bids) == len(book.asks) == 5
    assert book.bids[0].price == max(Decimal(p) for p, *_ in BOOKS["data"][0]["bids"])
    assert book.asks[0].price == min(Decimal(p) for p, *_ in BOOKS["data"][0]["asks"])
    assert all(lvl.quantity > 0 for lvl in book.bids)
    for bad in (0, 401):
        with pytest.raises(ValueError):
            run(lambda a, bad=bad: a.get_order_book("BTC-USDT-SWAP", bad))


# ---- candles -----------------------------------------------------------------------------------
def test_candles_1m_oldest_first_and_forming_candle_dropped():
    rows = CANDLES_1M["data"]
    assert int(rows[0][0]) > int(rows[-1][0]) and rows[0][8] == "0"  # venue: newest first, newest forming
    newest_open = dt(rows[0][0])
    h = Harness()
    candles = run(lambda a: a.get_candles("BTC-USDT-SWAP", "1m", limit=5), h, now=newest_open + timedelta(seconds=20))
    assert h.paths() == [CANDLES_PATH]
    assert h.query(0) == {"instId": "BTC-USDT-SWAP", "bar": "1m", "limit": "6"}
    assert [c.open_time for c in candles] == [dt(r[0]) for r in reversed(rows[1:])]
    oldest = candles[0]
    expected = tuple(Decimal(x) for x in (*rows[-1][1:5], rows[-1][6]))
    assert (oldest.open, oldest.high, oldest.low, oldest.close, oldest.volume) == expected
    assert oldest.volume == Decimal(rows[-1][5]) * Decimal("0.01")  # volCcy = contracts * ctVal (BTC)


def test_candles_include_open_and_confirm_flag():
    rows = CANDLES_1M["data"]
    newest_open = dt(rows[0][0])
    kept = run(lambda a: a.get_candles("BTC-USDT-SWAP", "1m", 6, include_open=True),
               now=newest_open + timedelta(seconds=20))
    assert len(kept) == 6 and kept[-1].open_time == newest_open
    # interval elapsed on the local clock but OKX has not confirmed it yet: still not closed
    late = run(lambda a: a.get_candles("BTC-USDT-SWAP", "1m", 6), now=newest_open + timedelta(minutes=5))
    assert newest_open not in [c.open_time for c in late] and len(late) == 5
    # confirmed but the local clock says the interval has not elapsed: not closed either
    early = run(lambda a: a.get_candles("BTC-USDT-SWAP", "1m", 6), now=dt(rows[1][0]) + timedelta(seconds=30))
    assert [c.open_time for c in early][-1] == dt(rows[2][0])


def test_candles_1h_maps_to_okx_bar():
    rows = CANDLES_1H["data"]
    h = Harness()
    candles = run(lambda a: a.get_candles("BTC-USDT-SWAP", "1h", limit=1), h, now=dt(rows[0][0]) + timedelta(minutes=38))
    assert h.query(0) == {"instId": "BTC-USDT-SWAP", "bar": "1H", "limit": "2"}
    assert [c.open_time for c in candles] == [dt(rows[1][0])]


def test_candles_bad_arguments():
    with pytest.raises(ValueError, match="unsupported timeframe"):
        run(lambda a: a.get_candles("BTC-USDT-SWAP", "8h"))  # OKX has no 8h bar
    for bad in (0, 1501):
        with pytest.raises(ValueError):
            run(lambda a, bad=bad: a.get_candles("BTC-USDT-SWAP", "1m", limit=bad))


class CandleVenue:
    """Synthetic OKX candle server following the live-verified semantics: newest first, at most 300 rows
    per call, `after` exclusive, /market/candles limited to the latest 1,440 rows (empty page beyond),
    /market/history-candles serving all `total` rows. The newest row is the forming one."""

    def __init__(self, newest_ms: int, step_ms: int, total: int, window: int = 1440):
        self.stamps = [newest_ms - i * step_ms for i in range(total)]
        self.window = window
        self.template = CANDLES_1M["data"][1]

    def __call__(self, request: httpx.Request):
        params = request.url.params
        stamps = self.stamps[:self.window] if request.url.path == CANDLES_PATH else self.stamps
        if "after" in params:
            stamps = [t for t in stamps if t < int(params["after"])]
        stamps = stamps[:min(int(params.get("limit", 100)), 300)]
        rows = [[str(t), *self.template[1:8], "0" if t == self.stamps[0] else "1"] for t in stamps]
        return envelope(rows)

    def handler(self):
        return by_path({CANDLES_PATH: self, HISTORY_CANDLES_PATH: self})


NEWEST_1M = 1790757480000  # forming candle of the captured 1m fixture


def test_candles_paginate_1000_one_minute_candles():
    venue = CandleVenue(NEWEST_1M, 60_000, total=5000)
    h = Harness()
    candles = run(lambda a: a.get_candles("BTC-USDT-SWAP", "1m", limit=1000), h, handler=venue.handler(),
                  now=dt(NEWEST_1M) + timedelta(seconds=20))
    assert h.paths() == [CANDLES_PATH] * 4
    assert [h.query(i)["limit"] for i in range(4)] == ["300", "300", "300", "101"]
    assert "after" not in h.query(0)
    assert [int(h.query(i)["after"]) for i in (1, 2, 3)] == [NEWEST_1M - k * 300 * 60_000 + 60_000 for k in (1, 2, 3)]
    assert len(candles) == 1000
    assert candles[-1].open_time == dt(NEWEST_1M - 60_000)  # newest CLOSED candle
    assert candles[0].open_time == dt(NEWEST_1M - 1000 * 60_000)
    assert all(b.open_time - a.open_time == timedelta(minutes=1) for a, b in itertools.pairwise(candles))


def test_candles_paginate_340_one_hour_candles():
    newest = 1790755200000  # 08:00 UTC, the forming hour of the 1h fixture
    venue = CandleVenue(newest, 3_600_000, total=2000)
    h = Harness()
    candles = run(lambda a: a.get_candles("BTC-USDT-SWAP", "1h", limit=340), h, handler=venue.handler(),
                  now=dt(newest) + timedelta(minutes=38))
    assert h.paths() == [CANDLES_PATH] * 2
    assert [(h.query(i)["bar"], h.query(i)["limit"]) for i in range(2)] == [("1H", "300"), ("1H", "41")]
    assert len(candles) == 340 and candles[-1].open_time == dt(newest - 3_600_000)
    assert all(b.open_time - a.open_time == timedelta(hours=1) for a, b in itertools.pairwise(candles))


def test_candles_continue_on_history_endpoint_past_the_1440_row_window():
    venue = CandleVenue(NEWEST_1M, 60_000, total=5000)
    h = Harness()
    candles = run(lambda a: a.get_candles("BTC-USDT-SWAP", "1m", limit=1500), h, handler=venue.handler(),
                  now=dt(NEWEST_1M) + timedelta(seconds=20))
    # 4 x 300 + 240 rows fill the window, an empty page hands over, history-candles serves the rest
    assert h.paths() == [CANDLES_PATH] * 6 + [HISTORY_CANDLES_PATH]
    assert h.query(6)["after"] == h.query(5)["after"] == str(NEWEST_1M - 1439 * 60_000)
    assert h.query(6)["limit"] == "61"
    assert len(candles) == 1500 and candles[0].open_time == dt(NEWEST_1M - 1500 * 60_000)
    assert all(b.open_time - a.open_time == timedelta(minutes=1) for a, b in itertools.pairwise(candles))


def test_candles_stop_when_history_runs_out():
    venue = CandleVenue(NEWEST_1M, 60_000, total=50)  # a young listing: 50 rows exist in total
    h = Harness()
    candles = run(lambda a: a.get_candles("BTC-USDT-SWAP", "1m", limit=1000), h, handler=venue.handler(),
                  now=dt(NEWEST_1M) + timedelta(seconds=20))
    assert len(candles) == 49  # every closed candle there is
    assert h.paths() == [CANDLES_PATH, CANDLES_PATH, HISTORY_CANDLES_PATH]


def test_candle_pagination_that_does_not_move_back_is_an_error():
    handler = by_path({CANDLES_PATH: lambda r: envelope(CANDLES_1M["data"])})  # ignores `after`
    h = Harness()
    with pytest.raises(ExchangeRequestError, match="did not move back"):
        run(lambda a: a.get_candles("BTC-USDT-SWAP", "1m", limit=100), h, handler=handler)
    assert len(h.calls) == 2


# ---- time --------------------------------------------------------------------------------------
def test_server_time():
    payload = load(V, "server_time.json")
    assert run(lambda a: a.get_server_time()) == dt(payload["data"][0]["ts"])


# ---- OKX error envelope ------------------------------------------------------------------------
def test_nonzero_code_on_http_200_is_an_error_and_not_retried():
    h = Harness()
    bad = load(V, "error_instrument_not_found.json")  # real answer for instId=NOPE-USDT-SWAP
    with pytest.raises(ExchangeRequestError, match=r"code=51001.*doesn't exist"):
        run(lambda a: a.get_ticker("BTC-USDT-SWAP"), h, handler=routes(**{"/api/v5/market/ticker": bad}))
    assert h.paths().count("/api/v5/market/ticker") == 1 and h.sleeps == []


def test_error_envelope_without_data_key():
    bad = load(V, "error_parameter_sz.json")  # real answer for sz=401: no "data" key at all
    assert "data" not in bad
    with pytest.raises(ExchangeRequestError, match=r"code=51000.*sz error"):
        run(lambda a: a.get_order_book("BTC-USDT-SWAP", 20), handler=routes(**{"/api/v5/market/books": bad}))


def test_malformed_envelope_is_a_request_error():
    with pytest.raises(ExchangeRequestError, match="unexpected"):
        run(lambda a: a.get_server_time(), handler=routes(**{"/api/v5/public/time": {"data": []}}))
    with pytest.raises(ExchangeRequestError, match="unexpected"):
        run(lambda a: a.get_server_time(), handler=routes(**{"/api/v5/public/time": envelope({"ts": "1"})}))


def test_rate_limit_code_is_not_retried():
    h = Harness()
    limited = envelope([], code="50011", msg="Rate limit reached.")
    with pytest.raises(ExchangeRequestError, match="rate limit"):
        run(lambda a: a.get_funding_history("BTC-USDT-SWAP"), h,
            handler=routes(**{"/api/v5/public/funding-rate-history": limited}))
    assert len(h.calls) == 1 and h.sleeps == []


def test_http_429_is_not_retried():
    h = Harness()
    with pytest.raises(ExchangeRequestError, match="429"):
        run(lambda a: a.get_server_time(), h,
            handler=lambda r: httpx.Response(429, json={"code": "50011", "msg": "Too Many Requests"}))
    assert len(h.calls) == 1 and h.sleeps == []


# ---- request hygiene ---------------------------------------------------------------------------
def test_only_public_get_requests_without_credentials():
    h = Harness()

    async def go(a):
        await a.get_funding_info("BTC-USDT-SWAP")
        await a.get_ticker("BTC-USDT-SWAP")
        await a.get_order_book("BTC-USDT-SWAP")
        await a.get_candles("BTC-USDT-SWAP")
        await a.get_funding_history("BTC-USDT-SWAP")
        await a.get_server_time()
    run(go, h)
    for request in h.calls:
        assert request.method == "GET"
        assert request.url.path.startswith(("/api/v5/public/", "/api/v5/market/"))
        headers = {k.lower() for k in request.headers}
        assert not {"ok-access-key", "ok-access-sign", "ok-access-passphrase", "authorization"} & headers
    assert h.calls[0].url.host == "www.okx.com"


def test_account_and_order_methods_are_not_supported():
    async def go(a):
        for call in (a.get_account_equity(), a.get_available_balance(), a.get_positions(),
                     a.get_position("BTC-USDT-SWAP"), a.get_open_orders(), a.get_order(order_id="1"),
                     a.get_fills(), a.get_fee_schedule("BTC-USDT-SWAP"),
                     a.set_leverage("BTC-USDT-SWAP", Decimal(2)),
                     a.place_order("BTC-USDT-SWAP", "BUY", Decimal(1), client_order_id="x"),
                     a.cancel_order("BTC-USDT-SWAP", order_id="1"), a.connect_private_streams()):
            with pytest.raises(ExchangeNotSupported):
                await call
    run(go)


def test_default_url():
    assert OkxSwapAdapter(transport=OFFLINE).base_url == "https://www.okx.com"
    assert OkxSwapAdapter.name == "okx_swap"
