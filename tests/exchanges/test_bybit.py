import copy
import itertools
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from app.exchanges.base import ExchangeNotSupported, ExchangeRequestError, SymbolNotFound
from app.exchanges.bybit import BybitLinearAdapter

from .helpers import Harness, by_path, load

V = "bybit"
OFFLINE = httpx.MockTransport(lambda request: httpx.Response(500))  # never reached; avoids TLS setup cost
INSTRUMENTS = load(V, "instruments_info.json")
TICKERS = load(V, "tickers_btcusdt.json")
ORDERBOOK = load(V, "orderbook_btcusdt.json")
KLINE = load(V, "kline_btcusdt_1m.json")
HISTORY = load(V, "funding_history_btcusdt.json")


def routes(**overrides):
    base = {
        "/v5/market/time": load(V, "server_time.json"),
        "/v5/market/instruments-info": INSTRUMENTS,
        "/v5/market/tickers": TICKERS,
        "/v5/market/orderbook": ORDERBOOK,
        "/v5/market/funding/history": HISTORY,
        "/v5/market/kline": KLINE,
    }
    base.update(overrides)
    return by_path(base)


def run(action, h=None, handler=None, **kw):
    h = h or Harness()
    return h.run(BybitLinearAdapter, handler or routes(), action, **kw)


def instruments():
    return {i.symbol: i for i in run(lambda a: a.list_instruments())}


def envelope(result, code=0, msg="OK", time=1790696000000):
    return {"retCode": code, "retMsg": msg, "result": result, "retExtInfo": {}, "time": time}


# ---- instruments -------------------------------------------------------------------------------
def test_instrument_parsing_btcusdt():
    btc = instruments()["BTCUSDT"]
    assert (btc.venue, btc.base, btc.quote, btc.settle) == ("bybit_linear", "BTC", "USDT", "USDT")
    assert btc.is_linear and btc.is_perpetual and btc.is_tradable
    assert btc.contract_multiplier == 1
    assert btc.tick_size == Decimal("0.10")
    assert btc.qty_step == Decimal("0.001") and btc.min_qty == Decimal("0.001")
    assert btc.max_qty == Decimal("150.000")  # min(maxOrderQty 1500, maxMktOrderQty 150)
    assert btc.min_notional == Decimal("5")
    assert btc.funding_interval_hours == Decimal(8)  # fundingInterval 480 minutes
    assert btc.max_leverage == Decimal("150.00")


def test_one_malformed_instrument_is_skipped_not_fatal():
    broken = copy.deepcopy(INSTRUMENTS)
    del next(r for r in broken["result"]["list"] if r["symbol"] == "BTCUSDT")["lotSizeFilter"]
    inst = {i.symbol: i for i in run(lambda a: a.list_instruments(),
                                     handler=routes(**{"/v5/market/instruments-info": broken}))}
    assert "BTCUSDT" not in inst and "ETHUSDT" in inst
    everything_broken = copy.deepcopy(INSTRUMENTS)
    for r in everything_broken["result"]["list"]:
        r.pop("lotSizeFilter", None)
    with pytest.raises(ExchangeRequestError, match="no instruments-info row could be parsed"):
        run(lambda a: a.list_instruments(), handler=routes(**{"/v5/market/instruments-info": everything_broken}))


def test_usdc_perps_and_venue_own_names():
    inst = instruments()
    assert (inst["BTCPERP"].quote, inst["BTCPERP"].settle, inst["BTCPERP"].base) == ("USDC", "USDC", "BTC")
    assert inst["1000PEPEUSDT"].base == "1000PEPE"
    assert "BTCUSDT-25DEC26" not in inst  # LinearFutures are ignored


def test_funding_interval_minutes_are_converted_to_hours():
    inst = instruments()
    four_hour = next(r for r in INSTRUMENTS["result"]["list"] if r["fundingInterval"] == 240)
    assert four_hour["fundingInterval"] == 240
    assert inst[four_hour["symbol"]].funding_interval_hours == Decimal(4)


def test_pagination_follows_cursor_and_ignores_inverse_and_dated_contracts():
    pages = [load(V, f"instruments_page{i}.json") for i in (1, 2, 3)]
    cursor1, cursor2 = (pages[0]["result"]["nextPageCursor"], pages[1]["result"]["nextPageCursor"])
    by_cursor = {"": pages[0], cursor1: pages[1], cursor2: pages[2]}
    h = Harness()
    handler = by_path({"/v5/market/instruments-info": lambda r: by_cursor[r.url.params.get("cursor", "")]})
    inst = run(lambda a: a.list_instruments(), h, handler=handler)
    assert [i.symbol for i in inst] == ["BTCPERP", "BTCUSDT"]  # BTCUSD (inverse) / BTCUSDH27 / dated futures dropped
    assert len(h.calls) == 3
    assert "cursor" not in h.query(0)
    assert h.query(1)["cursor"] == pages[0]["result"]["nextPageCursor"]
    assert h.query(2)["cursor"] == pages[1]["result"]["nextPageCursor"]
    assert all(h.query(i)["category"] == "linear" and h.query(i)["limit"] == "1000" for i in range(3))


def test_pagination_that_never_ends_is_bounded():
    counter = itertools.count()
    handler = by_path({"/v5/market/instruments-info": lambda r: envelope(
        {"category": "linear", "list": [], "nextPageCursor": f"c{next(counter)}"})})
    h = Harness()
    with pytest.raises(ExchangeRequestError, match="pagination did not terminate"):
        run(lambda a: a.list_instruments(), h, handler=handler)
    assert len(h.calls) == 50


# ---- resolve_symbol ----------------------------------------------------------------------------
def test_resolve_symbol_success():
    async def go(a):
        return [await a.resolve_symbol(b, q, q) for b, q in (("BTC", "USDT"), ("ETH", "USDC"), ("SOL", "USDT"))]
    assert [m.symbol for m in run(go)] == ["BTCUSDT", "ETHPERP", "SOLUSDT"]


def test_resolve_symbol_not_found_not_tradable_and_ambiguous():
    with pytest.raises(SymbolNotFound, match="no perpetual DOGE/USDT:USDT"):
        run(lambda a: a.resolve_symbol("DOGE", "USDT", "USDT"))
    pre_launch = copy.deepcopy(INSTRUMENTS)
    next(r for r in pre_launch["result"]["list"] if r["symbol"] == "SOLUSDT")["status"] = "PreLaunch"
    with pytest.raises(SymbolNotFound, match="not tradable"):
        run(lambda a: a.resolve_symbol("SOL", "USDT", "USDT"),
            handler=routes(**{"/v5/market/instruments-info": pre_launch}))
    dup = copy.deepcopy(INSTRUMENTS)
    clone = copy.deepcopy(next(r for r in dup["result"]["list"] if r["symbol"] == "BTCUSDT"))
    clone["symbol"] = "BTCUSDT2"  # synthetic duplicate, only to exercise the ambiguity branch
    dup["result"]["list"].append(clone)
    with pytest.raises(SymbolNotFound, match="ambiguous"):
        run(lambda a: a.resolve_symbol("BTC", "USDT", "USDT"), handler=routes(**{"/v5/market/instruments-info": dup}))


def test_instruments_cached_for_ten_minutes():
    h = Harness()

    async def go(a):
        await a.resolve_symbol("BTC", "USDT", "USDT")
        await a.resolve_symbol("ETH", "USDT", "USDT")
        h.monotonic = 599.0
        await a.list_instruments()
        assert h.paths().count("/v5/market/instruments-info") == 1
        h.monotonic = 600.5
        await a.list_instruments()
    run(go, h)
    assert h.paths().count("/v5/market/instruments-info") == 2


# ---- funding -----------------------------------------------------------------------------------
def test_funding_info_parsing():
    row = TICKERS["result"]["list"][0]
    h = Harness()
    f = run(lambda a: a.get_funding_info("BTCUSDT"), h)
    assert h.query(0) == {"category": "linear", "symbol": "BTCUSDT"}
    assert f.venue == "bybit_linear"
    assert f.funding_rate == Decimal(row["fundingRate"])  # fraction per interval
    assert f.interval_hours == Decimal(row["fundingIntervalHour"]) == 8
    assert f.next_funding_time == datetime.fromtimestamp(int(row["nextFundingTime"]) / 1000, UTC)
    assert f.is_predicted is True
    assert f.mark_price == Decimal(row["markPrice"]) and f.index_price == Decimal(row["indexPrice"])
    assert f.source_time == datetime.fromtimestamp(TICKERS["time"] / 1000, UTC)
    assert len(h.calls) == 1  # interval came with the ticker, no instrument lookup needed


def test_funding_interval_prefers_ticker_then_falls_back_to_instruments():
    def with_row(**changes):
        payload = copy.deepcopy(TICKERS)
        payload["result"]["list"][0].update(changes)
        return routes(**{"/v5/market/tickers": payload})
    assert run(lambda a: a.get_funding_info("BTCUSDT"), handler=with_row(fundingIntervalHour="4")).interval_hours == 4
    h = Harness()
    f = run(lambda a: a.get_funding_info("BTCUSDT"), h, handler=with_row(fundingIntervalHour=""))
    assert f.interval_hours == 8  # from instruments-info fundingInterval 480 / 60
    assert "/v5/market/instruments-info" in h.paths()


def test_funding_without_any_interval_is_an_error():
    payload = copy.deepcopy(TICKERS)
    payload["result"]["list"][0]["fundingIntervalHour"] = ""
    instruments_no_interval = copy.deepcopy(INSTRUMENTS)
    for r in instruments_no_interval["result"]["list"]:
        r["fundingInterval"] = 0
    with pytest.raises(ExchangeRequestError, match="no funding interval"):
        run(lambda a: a.get_funding_info("BTCUSDT"), handler=routes(**{
            "/v5/market/tickers": payload, "/v5/market/instruments-info": instruments_no_interval}))


@pytest.mark.parametrize("raw", ["0", ""])
def test_missing_next_funding_time_is_none(raw):
    payload = copy.deepcopy(TICKERS)
    payload["result"]["list"][0]["nextFundingTime"] = raw
    f = run(lambda a: a.get_funding_info("BTCUSDT"), handler=routes(**{"/v5/market/tickers": payload}))
    assert f.next_funding_time is None


def test_funding_history_is_reordered_oldest_first():
    rows = HISTORY["result"]["list"]
    assert int(rows[0]["fundingRateTimestamp"]) > int(rows[-1]["fundingRateTimestamp"])  # venue: newest first
    h = Harness()
    hist = run(lambda a: a.get_funding_history("BTCUSDT", 10), h)
    assert h.query(0) == {"category": "linear", "symbol": "BTCUSDT", "limit": "10"}
    assert [t for t, _ in hist] == sorted(t for t, _ in hist) and len(hist) == len(rows)
    newest = rows[0]
    assert hist[-1] == (datetime.fromtimestamp(int(newest["fundingRateTimestamp"]) / 1000, UTC),
                        Decimal(newest["fundingRate"]))


def test_funding_history_limit_bounds():
    for bad in (0, 201):
        with pytest.raises(ValueError):
            run(lambda a, bad=bad: a.get_funding_history("BTCUSDT", bad))


# ---- ticker ------------------------------------------------------------------------------------
def test_ticker_parsing():
    row = TICKERS["result"]["list"][0]
    t = run(lambda a: a.get_ticker("BTCUSDT"))
    keys = ("bid1Price", "ask1Price", "lastPrice", "markPrice")
    assert (t.bid, t.ask, t.last, t.mark) == tuple(Decimal(row[k]) for k in keys)
    assert t.volume_24h_usd == Decimal(row["turnover24h"])  # USDT-quoted turnover
    assert t.source_time == datetime.fromtimestamp(TICKERS["time"] / 1000, UTC)


def test_ticker_symbol_must_match_exactly_one_row():
    with pytest.raises(ExchangeRequestError, match="no unique row"):
        run(lambda a: a.get_ticker("ETHUSDT"))  # fixture row is BTCUSDT
    empty = envelope({"category": "linear", "list": []})
    with pytest.raises(ExchangeRequestError, match="no unique row"):
        run(lambda a: a.get_funding_info("BTCUSDT"), handler=routes(**{"/v5/market/tickers": empty}))


# ---- order book --------------------------------------------------------------------------------
def test_order_book_ordering_units_and_timestamps():
    result = ORDERBOOK["result"]
    h = Harness()
    book = run(lambda a: a.get_order_book("BTCUSDT", depth=20), h)
    assert h.query(0) == {"category": "linear", "symbol": "BTCUSDT", "limit": "20"}
    assert len(book.bids) == len(book.asks) == 20
    assert [lvl.price for lvl in book.bids] == sorted((lvl.price for lvl in book.bids), reverse=True)
    assert [lvl.price for lvl in book.asks] == sorted(lvl.price for lvl in book.asks)
    assert book.bids[0].quantity == Decimal(result["b"][0][1])  # base coin, multiplier 1
    assert book.source_time == datetime.fromtimestamp(result["ts"] / 1000, UTC)
    assert book.age_ms(now=book.source_time + timedelta(seconds=2)) == 2000


def test_order_book_sorts_and_trims():
    result = dict(ORDERBOOK["result"], b=list(reversed(ORDERBOOK["result"]["b"])),
                  a=list(reversed(ORDERBOOK["result"]["a"])))
    book = run(lambda a: a.get_order_book("BTCUSDT", 5),
               handler=routes(**{"/v5/market/orderbook": envelope(result)}))
    assert len(book.bids) == len(book.asks) == 5
    assert book.bids[0].price == max(Decimal(p) for p, _ in ORDERBOOK["result"]["b"])
    assert book.asks[0].price == min(Decimal(p) for p, _ in ORDERBOOK["result"]["a"])
    with pytest.raises(ExchangeRequestError, match="returned BTCUSDT for ETHUSDT"):
        run(lambda a: a.get_order_book("ETHUSDT"))
    with pytest.raises(ValueError):
        run(lambda a: a.get_order_book("BTCUSDT", 1001))


# ---- candles -----------------------------------------------------------------------------------
def test_candles_reordered_oldest_first_and_forming_candle_dropped():
    rows = KLINE["result"]["list"]
    assert int(rows[0][0]) > int(rows[-1][0])  # venue: newest first
    newest_open = datetime.fromtimestamp(int(rows[0][0]) / 1000, UTC)
    h = Harness()
    candles = run(lambda a: a.get_candles("BTCUSDT", "1m", limit=5), h, now=newest_open + timedelta(seconds=20))
    assert h.query(0) == {"category": "linear", "symbol": "BTCUSDT", "interval": "1", "limit": "6"}
    assert [c.open_time for c in candles] == sorted(c.open_time for c in candles)
    assert len(candles) == 5
    assert candles[-1].open_time == datetime.fromtimestamp(int(rows[1][0]) / 1000, UTC)
    oldest = candles[0]
    expected = tuple(Decimal(x) for x in rows[-1][1:6])
    assert (oldest.open, oldest.high, oldest.low, oldest.close, oldest.volume) == expected


def test_candles_include_open_and_bad_arguments():
    rows = KLINE["result"]["list"]
    newest_open = datetime.fromtimestamp(int(rows[0][0]) / 1000, UTC)
    assert len(run(lambda a: a.get_candles("BTCUSDT", "1m", 6, include_open=True),
                   now=newest_open + timedelta(seconds=20))) == 6
    assert len(run(lambda a: a.get_candles("BTCUSDT", "1m", 6), now=newest_open + timedelta(seconds=60))) == 6
    with pytest.raises(ValueError, match="unsupported timeframe"):
        run(lambda a: a.get_candles("BTCUSDT", "8h"))  # Bybit has no 8h interval
    with pytest.raises(ValueError):
        run(lambda a: a.get_candles("BTCUSDT", "1m", limit=1000))


# ---- time --------------------------------------------------------------------------------------
def test_server_time_uses_nanosecond_precision():
    payload = load(V, "server_time.json")
    t = run(lambda a: a.get_server_time())
    nanos = int(payload["result"]["timeNano"])
    assert t == datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=nanos // 1000)


# ---- Bybit error envelope ----------------------------------------------------------------------
def test_nonzero_retcode_on_http_200_is_an_error_and_not_retried():
    h = Harness()
    bad = envelope({}, code=10001, msg="params error: symbol invalid")
    with pytest.raises(ExchangeRequestError, match=r"retCode=10001.*symbol invalid"):
        run(lambda a: a.get_ticker("NOPEUSDT"), h, handler=routes(**{"/v5/market/tickers": bad}))
    assert h.paths().count("/v5/market/tickers") == 1 and h.sleeps == []  # an API error is never retried


@pytest.mark.parametrize("code", [10006, 10018])
def test_rate_limit_retcodes_are_not_retried(code):
    h = Harness()
    limited = envelope({}, code=code, msg="Too many visits!")
    with pytest.raises(ExchangeRequestError, match="rate limit"):
        run(lambda a: a.get_funding_info("BTCUSDT"), h, handler=routes(**{"/v5/market/tickers": limited}))
    assert len(h.calls) == 1 and h.sleeps == []


def test_http_403_message_mentions_region_and_rate_limit():
    with pytest.raises(ExchangeRequestError, match="403.*region"):
        run(lambda a: a.get_server_time(), handler=lambda r: httpx.Response(403, text="forbidden"))


# ---- request hygiene ---------------------------------------------------------------------------
def test_only_public_get_requests_without_credentials():
    h = Harness()

    async def go(a):
        await a.get_funding_info("BTCUSDT")
        await a.get_ticker("BTCUSDT")
        await a.get_order_book("BTCUSDT")
        await a.get_candles("BTCUSDT")
        await a.get_funding_history("BTCUSDT")
        await a.get_server_time()
    run(go, h)
    for request in h.calls:
        assert request.method == "GET" and request.url.path.startswith("/v5/market/")
        assert not {"x-bapi-api-key", "x-bapi-sign", "authorization"} & {k.lower() for k in request.headers}
    assert h.calls[0].url.host == "api.bybit.com"


def test_account_and_order_methods_are_not_supported():
    async def go(a):
        for call in (a.get_account_equity(), a.get_position("BTCUSDT"), a.get_fills(), a.get_fee_schedule("BTCUSDT"),
                     a.cancel_order("BTCUSDT", order_id="1"), a.connect_private_streams()):
            with pytest.raises(ExchangeNotSupported):
                await call
    run(go)


def test_default_and_testnet_urls():
    assert BybitLinearAdapter(transport=OFFLINE).base_url == "https://api.bybit.com"
    testnet = BybitLinearAdapter(base_url=BybitLinearAdapter.TESTNET_BASE_URL, transport=OFFLINE)
    assert testnet.base_url == "https://api-testnet.bybit.com"
