"""BlofinSwapAdapter against recorded REAL public responses (tests/fixtures/exchanges/blofin, captured
2026-09-30 ~08:36-08:40 UTC from https://openapi.blofin.com; trimmed only: fewer instruments, 20 book
levels, 30 one-minute / 25 one-hour candles). No network."""

import copy
import itertools
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from app.domain.market import Side
from app.exchanges import blofin
from app.exchanges.base import (
    ExchangeNotSupported,
    ExchangeRequestError,
    SymbolNotFound,
)
from app.exchanges.blofin import BlofinSwapAdapter

from .helpers import Harness, by_path, load

V = "blofin"
M = "/api/v1/market"
OFFLINE = httpx.MockTransport(lambda request: httpx.Response(500))  # never reached; avoids TLS setup cost
INSTRUMENTS = load(V, "instruments.json")
FUNDING_ALL = load(V, "funding_rates.json")
FUNDING_BTC = load(V, "funding_rate_btcusdt.json")
HISTORY = load(V, "funding_rate_history_btcusdt.json")
MARK = load(V, "mark_price_btcusdt.json")
TICKERS = load(V, "tickers_btcusdt.json")
BOOKS = load(V, "books_btcusdt.json")
CANDLES_1M = load(V, "candles_btcusdt_1m.json")
CANDLES_1H = load(V, "candles_btcusdt_1h.json")
ERROR = load(V, "error_instid.json")


def envelope(data, code="0", msg="success"):
    return {"code": code, "msg": msg, "data": data}


def dt(ms_value) -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=int(ms_value))


def funding_rate(request):
    inst = request.url.params.get("instId")
    if inst is None:
        return FUNDING_ALL
    return FUNDING_BTC if inst == "BTC-USDT" else envelope([r for r in FUNDING_ALL["data"] if r["instId"] == inst])


def candle_venue(cap: int = 1440, ignore_after: bool = False):
    """Emulates GET /market/candles over the recorded rows: newest first, `after` exclusive, capped."""
    def handle(request):
        p = request.url.params
        rows = CANDLES_1H["data"] if p["bar"] == "1H" else CANDLES_1M["data"]
        if "after" in p and not ignore_after:
            rows = [r for r in rows if int(r[0]) < int(p["after"])]
        return envelope(rows[: min(int(p["limit"]), cap)])
    return handle


def routes(**overrides):
    base = {
        f"{M}/instruments": INSTRUMENTS,
        f"{M}/funding-rate": funding_rate,
        f"{M}/funding-rate-history": HISTORY,
        f"{M}/mark-price": MARK,
        f"{M}/tickers": TICKERS,
        f"{M}/books": BOOKS,
        f"{M}/candles": candle_venue(),
    }
    base.update({f"{M}/{k.replace('_', '-')}": v for k, v in overrides.items()})
    return by_path(base)


def run(action, h=None, handler=None, **kw):
    h = h or Harness()
    return h.run(BlofinSwapAdapter, handler or routes(), action, **kw)


def instruments(**overrides):
    return {i.symbol: i for i in run(lambda a: a.list_instruments(), handler=routes(**overrides))}


def patched_instruments(symbol, **changes):
    payload = copy.deepcopy(INSTRUMENTS)
    next(r for r in payload["data"] if r["instId"] == symbol).update(changes)
    return payload


# ---- instruments -------------------------------------------------------------------------------
def test_instrument_parsing_converts_contracts_to_base_units():
    btc = instruments()["BTC-USDT"]
    assert (btc.venue, btc.base, btc.quote, btc.settle) == ("blofin_swap", "BTC", "USDT", "USDT")
    assert btc.is_linear and btc.is_perpetual and btc.is_tradable
    assert btc.contract_multiplier == Decimal("0.001")  # contractValue: 0.001 BTC per contract
    assert btc.qty_step == Decimal("0.0001")  # lotSize 0.1 contracts x 0.001
    assert btc.min_qty == Decimal("0.0001")  # minSize 0.1 contracts x 0.001
    assert btc.max_qty == Decimal(100)  # min(maxLimitSize 1e6, maxMarketSize 1e5) contracts x 0.001
    assert btc.tick_size == Decimal("0.1")
    assert btc.min_notional is None  # not published
    assert btc.funding_interval_hours == Decimal(8)
    assert btc.max_leverage == Decimal(150)


def test_usdc_perps_odd_contract_sizes_and_exclusions():
    inst = instruments()
    usdc = inst["BTC-USDC"]
    assert (usdc.base, usdc.quote, usdc.settle) == ("BTC", "USDC", "USDC")
    assert usdc.contract_multiplier == Decimal("0.0001") and usdc.qty_step == Decimal("0.0001")
    assert usdc.max_qty == Decimal(25)  # maxMarketSize 250000 contracts x 0.0001
    shib = inst["SHIB-USDT"]  # one contract = 1,000,000 SHIB
    assert shib.contract_multiplier == Decimal(1_000_000)
    assert shib.qty_step == Decimal(100_000) and shib.min_qty == Decimal(100_000)
    assert inst["TSLA-USDT"].base == "TSLA"  # equity perp, venue's own base
    assert inst["ONE-USDT"].funding_interval_hours == Decimal(1)
    assert inst["RARE-USDT"].is_tradable  # scheduled delisting (offTime) but state is still live
    assert "BTC-USD" not in inst  # inverse coin-margined swap is ignored
    assert len(inst) == len(INSTRUMENTS["data"]) - 1


def test_funding_interval_units_and_missing_interval():
    payload = copy.deepcopy(FUNDING_ALL)
    rows = {r["instId"]: r for r in payload["data"]}
    rows["ETH-USDT"].update(fundingInterval="480", fundingIntervalUnit="minute")
    rows["SOL-USDT"].update(fundingIntervalUnit="day")  # undocumented unit: never guessed
    payload["data"].remove(rows["ONE-USDT"])

    def funding(request):
        return payload if "instId" not in request.url.params else funding_rate(request)
    inst = instruments(funding_rate=funding)
    assert inst["ETH-USDT"].funding_interval_hours == Decimal(8)
    assert inst["SOL-USDT"].funding_interval_hours is None
    assert inst["ONE-USDT"].funding_interval_hours is None


def test_one_malformed_instrument_is_skipped_not_fatal():
    broken = copy.deepcopy(INSTRUMENTS)
    del next(r for r in broken["data"] if r["instId"] == "BTC-USDT")["contractValue"]
    inst = instruments(instruments=broken)
    assert "BTC-USDT" not in inst and "ETH-USDT" in inst
    zero = instruments(instruments=patched_instruments("ETH-USDT", contractValue="0"))
    assert "ETH-USDT" not in zero  # a zero multiplier would silently zero every quantity
    everything_broken = copy.deepcopy(INSTRUMENTS)
    for r in everything_broken["data"]:
        r.pop("lotSize")
    with pytest.raises(ExchangeRequestError, match="no instruments row could be parsed"):
        instruments(instruments=everything_broken)


def test_instrument_load_calls_both_endpoints_unfiltered():
    h = Harness()
    run(lambda a: a.list_instruments(), h)
    assert sorted(h.paths()) == [f"{M}/funding-rate", f"{M}/instruments"]
    assert h.query(0) == {} and h.query(1) == {}


# ---- resolve_symbol ----------------------------------------------------------------------------
def test_resolve_symbol_usdt_and_usdc():
    async def go(a):
        return [await a.resolve_symbol(b, q, q) for b, q in (("BTC", "USDT"), ("BTC", "USDC"), ("ETH", "USDC"),
                                                                 ("shib", "usdt"))]
    assert [m.symbol for m in run(go)] == ["BTC-USDT", "BTC-USDC", "ETH-USDC", "SHIB-USDT"]


def test_resolve_symbol_not_found_not_tradable_and_ambiguous():
    with pytest.raises(SymbolNotFound, match="no perpetual DOGE/USDT:USDT"):
        run(lambda a: a.resolve_symbol("DOGE", "USDT", "USDT"))
    with pytest.raises(SymbolNotFound, match="no perpetual BTC/USD:BTC"):  # inverse swap never resolves
        run(lambda a: a.resolve_symbol("BTC", "USD", "BTC"))
    with pytest.raises(SymbolNotFound, match="not tradable"):
        run(lambda a: a.resolve_symbol("SOL", "USDT", "USDT"),
            handler=routes(instruments=patched_instruments("SOL-USDT", state="suspend")))
    dup = copy.deepcopy(INSTRUMENTS)
    clone = copy.deepcopy(next(r for r in dup["data"] if r["instId"] == "BTC-USDT"))
    clone["instId"] = "BTC-USDT2"  # synthetic duplicate, only to exercise the ambiguity branch
    dup["data"].append(clone)
    with pytest.raises(SymbolNotFound, match="ambiguous"):
        run(lambda a: a.resolve_symbol("BTC", "USDT", "USDT"), handler=routes(instruments=dup))


def test_instruments_cached_for_ten_minutes():
    h = Harness()

    async def go(a):
        await a.resolve_symbol("BTC", "USDT", "USDT")
        await a.resolve_symbol("BTC", "USDC", "USDC")
        h.monotonic = 599.0
        await a.list_instruments()
        assert h.paths().count(f"{M}/instruments") == 1 and h.paths().count(f"{M}/funding-rate") == 1
        h.monotonic = 600.5
        await a.list_instruments()
    run(go, h)
    assert h.paths().count(f"{M}/instruments") == 2 and h.paths().count(f"{M}/funding-rate") == 2


# ---- funding -----------------------------------------------------------------------------------
def test_funding_info_parsing():
    row, mark = FUNDING_BTC["data"][0], MARK["data"][0]
    h = Harness()
    f = run(lambda a: a.get_funding_info("BTC-USDT"), h)
    assert sorted(h.paths()) == [f"{M}/funding-rate", f"{M}/mark-price"]
    assert all(h.query(i) == {"instId": "BTC-USDT"} for i in range(2))
    assert f.venue == "blofin_swap"
    assert f.funding_rate == Decimal("-0.00004548") == Decimal(row["fundingRate"])  # fraction, not percent
    assert f.interval_hours == Decimal(8)
    assert f.next_funding_time == datetime(2026, 9, 30, 16, 0, tzinfo=UTC) == dt(row["fundingTime"])
    assert f.is_predicted is True
    assert f.mark_price == Decimal(mark["markPrice"]) and f.index_price == Decimal(mark["indexPrice"])
    assert f.source_time == dt(mark["ts"])
    assert f.rate_per_hour == Decimal("-0.00004548") / 8


def test_funding_interval_prefers_row_then_falls_back_to_instruments():
    def with_row(**changes):
        payload = copy.deepcopy(FUNDING_BTC)
        payload["data"][0].update(changes)
        return routes(funding_rate=lambda r: payload if "instId" in r.url.params else FUNDING_ALL)
    four = run(lambda a: a.get_funding_info("BTC-USDT"), handler=with_row(fundingInterval="4"))
    assert four.interval_hours == 4
    minutes = run(lambda a: a.get_funding_info("BTC-USDT"),
                  handler=with_row(fundingInterval="60", fundingIntervalUnit="minute"))
    assert minutes.interval_hours == 1
    h = Harness()
    f = run(lambda a: a.get_funding_info("BTC-USDT"), h, handler=with_row(fundingInterval=""))
    assert f.interval_hours == 8  # from the unfiltered funding-rate loaded with the instruments
    assert f"{M}/instruments" in h.paths()


def test_funding_without_any_interval_is_an_error():
    per_symbol = copy.deepcopy(FUNDING_BTC)
    per_symbol["data"][0].pop("fundingInterval")
    unfiltered = copy.deepcopy(FUNDING_ALL)
    for r in unfiltered["data"]:
        r.pop("fundingInterval")
    handler = routes(funding_rate=lambda r: per_symbol if "instId" in r.url.params else unfiltered)
    with pytest.raises(ExchangeRequestError, match="no funding interval"):
        run(lambda a: a.get_funding_info("BTC-USDT"), handler=handler)


def test_funding_info_rows_must_match_the_symbol():
    with pytest.raises(ExchangeRequestError, match="mark-price returned no unique row for ETH-USDT"):
        run(lambda a: a.get_funding_info("ETH-USDT"))  # mark-price fixture is BTC-USDT
    with pytest.raises(ExchangeRequestError, match="funding-rate returned no unique row"):
        run(lambda a: a.get_funding_info("BTC-USDT"), handler=routes(funding_rate=envelope([])))


def test_funding_history_is_reordered_oldest_first():
    rows = HISTORY["data"]
    assert int(rows[0]["fundingTime"]) > int(rows[-1]["fundingTime"])  # venue: newest first
    h = Harness()
    hist = run(lambda a: a.get_funding_history("BTC-USDT", 10), h)
    assert h.query(0) == {"instId": "BTC-USDT", "limit": "10"}
    assert [t for t, _ in hist] == sorted(t for t, _ in hist) and len(hist) == len(rows) == 10
    assert hist[-1] == (dt(rows[0]["fundingTime"]), Decimal(rows[0]["fundingRate"]))
    assert hist[-1] == (datetime(2026, 9, 30, 8, 0, tzinfo=UTC), Decimal("-0.000098"))
    assert all(b - a == timedelta(hours=8) for (a, _), (b, _) in itertools.pairwise(hist))


def test_funding_history_limit_bounds_and_symbol_check():
    for bad in (0, 101):
        with pytest.raises(ValueError):
            run(lambda a, bad=bad: a.get_funding_history("BTC-USDT", bad))
    with pytest.raises(ExchangeRequestError, match="returned BTC-USDT"):
        run(lambda a: a.get_funding_history("ETH-USDT"))


# ---- ticker ------------------------------------------------------------------------------------
def test_ticker_parsing_and_24h_quote_volume_from_closed_hourly_candles():
    row = TICKERS["data"][0]
    h = Harness()
    t = run(lambda a: a.get_ticker("BTC-USDT"), h)
    assert (t.bid, t.ask, t.last) == (Decimal(row["bidPrice"]), Decimal(row["askPrice"]), Decimal(row["last"]))
    assert t.mark == Decimal(MARK["data"][0]["markPrice"])
    closed = [r for r in CANDLES_1H["data"] if r[8] == "1"]
    assert len(closed) == 24 and CANDLES_1H["data"][0][8] == "0"  # the forming hour is excluded
    assert t.volume_24h_usd == sum(Decimal(r[7]) for r in closed) == Decimal("200441925.55844")
    assert t.source_time == dt(row["ts"])
    candle_call = h.calls[h.paths().index(f"{M}/candles")]
    assert dict(candle_call.url.params) == {"instId": "BTC-USDT", "bar": "1H", "limit": "25"}


def test_ticker_volume_window_is_24_hours_and_none_without_closed_candles():
    gappy = copy.deepcopy(CANDLES_1H)
    del gappy["data"][5]  # a missing hour inside the series ...
    older = list(gappy["data"][-1])
    older[0] = str(int(older[0]) - 3_600_000)  # ... so the venue's 25th row is 24 h before the newest closed
    gappy["data"].append(older)  # synthetic row, only to exercise the window guard
    t = run(lambda a: a.get_ticker("BTC-USDT"), handler=routes(candles=gappy))
    newest_closed = int(gappy["data"][1][0])
    assert int(older[0]) == newest_closed - 24 * 3_600_000
    window = [r for r in gappy["data"] if r[8] == "1" and int(r[0]) > newest_closed - 24 * 3_600_000]
    assert len(window) == 23 and older not in window
    assert t.volume_24h_usd == sum(Decimal(r[7]) for r in window)
    only_forming = envelope(CANDLES_1H["data"][:1])
    assert run(lambda a: a.get_ticker("BTC-USDT"), handler=routes(candles=only_forming)).volume_24h_usd is None


def test_ticker_symbol_must_match_exactly_one_row():
    with pytest.raises(ExchangeRequestError, match="no unique row"):
        run(lambda a: a.get_ticker("ETH-USDT"))  # fixture rows are BTC-USDT
    with pytest.raises(ExchangeRequestError, match="tickers returned no unique row"):
        run(lambda a: a.get_ticker("BTC-USDT"), handler=routes(tickers=envelope([])))


# ---- order book --------------------------------------------------------------------------------
def test_order_book_converts_contracts_to_base_units_and_orders_levels():
    book_raw = BOOKS["data"][0]
    h = Harness()
    book = run(lambda a: a.get_order_book("BTC-USDT", depth=20), h)
    books_call = h.calls[h.paths().index(f"{M}/books")]
    assert dict(books_call.url.params) == {"instId": "BTC-USDT", "size": "20"}
    assert len(book.bids) == len(book.asks) == 20
    assert [lvl.price for lvl in book.bids] == sorted((lvl.price for lvl in book.bids), reverse=True)
    assert [lvl.price for lvl in book.asks] == sorted(lvl.price for lvl in book.asks)
    assert book_raw["asks"][0] == ["83012.9", "7993"] and book_raw["bids"][0] == ["83012.8", "235"]
    assert book.asks[0].quantity == Decimal("7.993")  # 7993 contracts x 0.001 BTC
    assert book.bids[0].quantity == Decimal("0.235")
    assert book.best_bid == Decimal("83012.8") and book.best_ask == Decimal("83012.9")
    assert book.source_time == dt(book_raw["ts"])
    assert book.age_ms(now=book.source_time + timedelta(seconds=2)) == 2000


def test_order_book_uses_each_instruments_own_multiplier():
    book = run(lambda a: a.get_order_book("SHIB-USDT", 5))  # same rows, SHIB multiplier 1,000,000
    assert book.asks[0].quantity == Decimal(7993) * 1_000_000


def test_order_book_sorts_trims_and_drops_empty_levels():
    raw = copy.deepcopy(BOOKS["data"][0])
    raw["bids"].reverse()
    raw["asks"].reverse()
    raw["asks"].append(["83000", "0"])  # zero-size level must never become the best ask
    book = run(lambda a: a.get_order_book("BTC-USDT", 5), handler=routes(books=envelope([raw])))
    assert len(book.bids) == len(book.asks) == 5
    assert book.bids[0].price == max(Decimal(p) for p, _ in BOOKS["data"][0]["bids"])
    assert book.asks[0].price == min(Decimal(p) for p, _ in BOOKS["data"][0]["asks"])
    for bad in (0, 101):
        with pytest.raises(ValueError):
            run(lambda a, bad=bad: a.get_order_book("BTC-USDT", bad))


# ---- candles -----------------------------------------------------------------------------------
CAPTURE_1M = dt(CANDLES_1M["data"][0][0]) + timedelta(seconds=108)  # when the 1m rows were recorded
CAPTURE_1H = dt(CANDLES_1H["data"][0][0]) + timedelta(minutes=40)


def test_candles_oldest_first_base_volume_and_query():
    rows = CANDLES_1M["data"]
    assert int(rows[0][0]) > int(rows[-1][0])  # venue: newest first
    h = Harness()
    candles = run(lambda a: a.get_candles("BTC-USDT", "1m", limit=5), h, now=CAPTURE_1M)
    assert h.query(0) == {"instId": "BTC-USDT", "bar": "1m", "limit": "6"}
    assert len(h.calls) == 1
    assert [c.open_time for c in candles] == sorted(c.open_time for c in candles)
    assert [c.open_time for c in candles] == [dt(r[0]) for r in reversed(rows[:5])]
    newest = candles[-1]
    assert (newest.open, newest.high, newest.low, newest.close) == tuple(Decimal(x) for x in rows[0][1:5])
    assert newest.volume == Decimal("0.4008") == Decimal(rows[0][6])  # volCurrency (BTC), not vol=400 contracts


def test_candles_closed_only_uses_confirm_flag_and_local_clock():
    rows = CANDLES_1H["data"]
    assert rows[0][8] == "0"  # the venue marks the forming hour
    h = Harness()
    candles = run(lambda a: a.get_candles("BTC-USDT", "1h", limit=24), h, now=CAPTURE_1H)
    assert h.query(0) == {"instId": "BTC-USDT", "bar": "1H", "limit": "25"}
    assert len(candles) == 24 and candles[-1].open_time == dt(rows[1][0])
    with_open = run(lambda a: a.get_candles("BTC-USDT", "1h", limit=25, include_open=True), now=CAPTURE_1H)
    assert len(with_open) == 25 and with_open[-1].open_time == dt(rows[0][0])
    # confirm="1" but the local clock says the minute is still running (clock skew): not closed yet
    newest_open = dt(CANDLES_1M["data"][0][0])
    early = run(lambda a: a.get_candles("BTC-USDT", "1m", limit=5), now=newest_open + timedelta(seconds=20))
    assert early[-1].open_time == dt(CANDLES_1M["data"][1][0])


def test_candles_paginate_with_after_cursor(monkeypatch):
    monkeypatch.setattr(blofin, "_CANDLES_PER_CALL", 5)
    rows = CANDLES_1M["data"]
    h = Harness()
    candles = run(lambda a: a.get_candles("BTC-USDT", "1m", limit=12), h,
                  handler=routes(candles=candle_venue(cap=5)), now=CAPTURE_1M)
    assert len(h.calls) == 3
    assert "after" not in h.query(0)
    assert h.query(1)["after"] == rows[4][0] and h.query(2)["after"] == rows[9][0]  # oldest of previous page
    assert [h.query(i)["limit"] for i in range(3)] == ["5", "5", "3"]  # capped, then only what is missing
    assert len(candles) == 12
    assert [c.open_time for c in candles] == [dt(r[0]) for r in reversed(rows[:12])]
    assert all(b.open_time - a.open_time == timedelta(minutes=1) for a, b in itertools.pairwise(candles))


def test_candles_pagination_stops_when_history_is_exhausted(monkeypatch):
    monkeypatch.setattr(blofin, "_CANDLES_PER_CALL", 5)
    h = Harness()
    candles = run(lambda a: a.get_candles("BTC-USDT", "1m", limit=40), h,
                  handler=routes(candles=candle_venue(cap=5)), now=CAPTURE_1M)
    assert len(candles) == 30  # every recorded row, oldest first
    assert len(h.calls) == 7  # six full pages, then one empty page
    assert candles[0].open_time == dt(CANDLES_1M["data"][-1][0])


def test_candles_pagination_that_does_not_advance_is_an_error(monkeypatch):
    monkeypatch.setattr(blofin, "_CANDLES_PER_CALL", 5)
    with pytest.raises(ExchangeRequestError, match="did not advance"):
        run(lambda a: a.get_candles("BTC-USDT", "1m", limit=12),
            handler=routes(candles=candle_venue(cap=5, ignore_after=True)), now=CAPTURE_1M)


def test_candles_large_request_uses_documented_page_size():
    h = Harness()
    run(lambda a: a.get_candles("BTC-USDT", "1m", limit=1000), h, now=CAPTURE_1M)
    assert h.query(0)["limit"] == "1001"  # one call covers 1000 closed candles (+1 for a forming one)
    h = Harness()
    run(lambda a: a.get_candles("BTC-USDT", "1m", limit=2000), h, now=CAPTURE_1M)
    assert h.query(0)["limit"] == "1440"  # documented maximum per call


def test_candles_bad_arguments():
    with pytest.raises(ValueError, match="unsupported timeframe"):
        run(lambda a: a.get_candles("BTC-USDT", "1w"))
    for bad in (0, 5001):
        with pytest.raises(ValueError):
            run(lambda a, bad=bad: a.get_candles("BTC-USDT", "1m", limit=bad))


# ---- time --------------------------------------------------------------------------------------
def test_server_time_is_mark_price_data_return_time():
    h = Harness()
    t = run(lambda a: a.get_server_time(), h)
    assert h.paths() == [f"{M}/mark-price"] and h.query(0) == {"instId": "BTC-USDT"}
    assert t == dt(MARK["data"][0]["ts"])


# ---- BloFin error envelope ---------------------------------------------------------------------
def test_nonzero_code_on_http_200_is_an_error_and_not_retried():
    assert ERROR == {"code": "152002", "msg": "Parameter instId error."}  # recorded for instId=NOPE-USDT
    h = Harness()
    with pytest.raises(ExchangeRequestError, match=r"code=152002.*instId error"):
        run(lambda a: a.get_candles("NOPE-USDT"), h, handler=routes(candles=ERROR))
    assert len(h.calls) == 1 and h.sleeps == []  # an API error is never retried


def test_rate_limit_code_and_http_429_are_not_retried():
    h = Harness()
    limited = {"code": "429", "msg": "Too Many Requests"}
    with pytest.raises(ExchangeRequestError, match="rate limit"):
        run(lambda a: a.get_funding_history("BTC-USDT"), h, handler=routes(funding_rate_history=limited))
    assert len(h.calls) == 1 and h.sleeps == []
    h = Harness()
    with pytest.raises(ExchangeRequestError, match="rate limit exceeded.*429.*not retried"):
        run(lambda a: a.get_server_time(), h, handler=lambda r: httpx.Response(429, json=limited))
    assert len(h.calls) == 1 and h.sleeps == []


def test_http_403_firewall_is_not_retried():
    h = Harness()
    with pytest.raises(ExchangeRequestError, match="403"):
        run(lambda a: a.get_server_time(), h, handler=lambda r: httpx.Response(403, text="forbidden"))
    assert len(h.calls) == 1


def test_malformed_envelope_is_a_request_error():
    with pytest.raises(ExchangeRequestError, match="unexpected"):
        run(lambda a: a.get_server_time(), handler=routes(mark_price={"msg": "no code"}))
    with pytest.raises(ExchangeRequestError, match="unexpected candles"):
        run(lambda a: a.get_candles("BTC-USDT"), handler=routes(candles=envelope([["1790757360000", "1"]])))


# ---- request hygiene ---------------------------------------------------------------------------
def test_only_public_get_requests_without_credentials():
    h = Harness()

    async def go(a):
        await a.get_funding_info("BTC-USDT")
        await a.get_ticker("BTC-USDT")
        await a.get_order_book("BTC-USDT")
        await a.get_candles("BTC-USDT")
        await a.get_funding_history("BTC-USDT")
        await a.get_server_time()
    run(go, h, now=CAPTURE_1M)
    for request in h.calls:
        assert request.method == "GET" and request.url.path.startswith(f"{M}/")
        headers = {k.lower() for k in request.headers}
        assert not {"access-key", "access-sign", "access-timestamp", "access-nonce", "access-passphrase",
                    "authorization"} & headers
    assert h.calls[0].url.host == "openapi.blofin.com"


def test_account_and_order_methods_are_not_supported():
    async def go(a):
        for call in (a.get_account_equity(), a.get_available_balance(), a.get_positions(),
                     a.get_position("BTC-USDT"), a.get_open_orders(), a.get_order(order_id="1"), a.get_fills(),
                     a.get_fee_schedule("BTC-USDT"), a.set_leverage("BTC-USDT", Decimal(2)),
                     a.cancel_order("BTC-USDT", order_id="1"), a.connect_private_streams()):
            with pytest.raises(ExchangeNotSupported):
                await call
        with pytest.raises(ExchangeNotSupported):
            await a.place_order("BTC-USDT", Side.BUY, Decimal(1), client_order_id="x")
    h = Harness()
    run(go, h)
    assert h.calls == []


def test_default_and_demo_urls():
    assert BlofinSwapAdapter(transport=OFFLINE).base_url == "https://openapi.blofin.com"
    demo = BlofinSwapAdapter(base_url=BlofinSwapAdapter.DEMO_BASE_URL, transport=OFFLINE)
    assert demo.base_url == "https://demo-trading-openapi.blofin.com"
    assert BlofinSwapAdapter.name == "blofin_swap"
