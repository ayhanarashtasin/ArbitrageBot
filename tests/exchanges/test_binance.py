import copy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from app.exchanges.base import ExchangeNotSupported, ExchangeRequestError, SymbolNotFound
from app.exchanges.binance import BinanceUsdmAdapter

from .helpers import NOW, Harness, by_path, load

V = "binance"
OFFLINE = httpx.MockTransport(lambda request: httpx.Response(500))  # never reached; avoids TLS setup cost
EXCHANGE_INFO = load(V, "exchange_info.json")
FUNDING_INFO = load(V, "funding_info.json")
PREMIUM = load(V, "premium_index_btcusdt.json")
DEPTH = load(V, "depth_btcusdt.json")
KLINES = load(V, "klines_btcusdt_1m.json")


def routes(**overrides):
    base = {
        "/fapi/v1/time": load(V, "server_time.json"),
        "/fapi/v1/exchangeInfo": EXCHANGE_INFO,
        "/fapi/v1/fundingInfo": FUNDING_INFO,
        "/fapi/v1/premiumIndex": PREMIUM,
        "/fapi/v1/fundingRate": load(V, "funding_rate_btcusdt.json"),
        "/fapi/v1/depth": DEPTH,
        "/fapi/v1/ticker/24hr": load(V, "ticker_24hr_btcusdt.json"),
        "/fapi/v1/ticker/bookTicker": load(V, "book_ticker_btcusdt.json"),
        "/fapi/v1/klines": KLINES,
    }
    base.update(overrides)
    return by_path(base)


def run(action, h=None, handler=None, **kw):
    h = h or Harness()
    return h.run(BinanceUsdmAdapter, handler or routes(), action, **kw)


def instruments():
    return {i.symbol: i for i in run(lambda a: a.list_instruments())}


# ---- instruments -------------------------------------------------------------------------------
def test_instrument_parsing_btcusdt():
    btc = instruments()["BTCUSDT"]
    assert (btc.venue, btc.base, btc.quote, btc.settle) == ("binance_usdt_m", "BTC", "USDT", "USDT")
    assert btc.is_linear and btc.is_perpetual and btc.is_tradable
    assert btc.contract_multiplier == 1
    assert btc.tick_size == Decimal("0.10")
    assert btc.qty_step == Decimal("0.001")
    assert btc.min_qty == Decimal("0.001")
    assert btc.max_qty == Decimal("120")  # min(LOT_SIZE 1000, MARKET_LOT_SIZE 120)
    assert btc.min_notional == Decimal("50")
    assert btc.funding_interval_hours == Decimal(8)
    assert btc.max_leverage is None  # not public
    assert btc.received_at == NOW


def test_instruments_only_perpetuals_and_use_venue_quote_and_settle():
    inst = instruments()
    assert "BTCUSDT_261225" not in inst  # CURRENT_QUARTER is ignored
    assert inst["BTCUSDC"].quote == inst["BTCUSDC"].settle == "USDC"
    assert (inst["ETHBTC"].quote, inst["ETHBTC"].settle) == ("BTC", "BTC")
    assert inst["TSLAUSDT"].is_perpetual  # TRADIFI_PERPETUAL is a perpetual
    assert inst["1000PEPEUSDT"].base == "1000PEPE" and inst["1000PEPEUSDT"].qty_step == 1
    assert inst["OMGUSDT"].is_tradable is False  # status SETTLING


def test_funding_interval_comes_from_funding_info_per_symbol():
    inst = instruments()
    assert inst["BTCUSDT"].funding_interval_hours == 8
    assert inst["LPTUSDT"].funding_interval_hours == 4
    assert inst["ONEUSDT"].funding_interval_hours == 1


def test_symbol_missing_from_funding_info_has_no_interval_and_no_default_is_assumed():
    trimmed = [f for f in FUNDING_INFO if f["symbol"] != "BTCUSDT"]
    handler = routes(**{"/fapi/v1/fundingInfo": trimmed})
    assert {i.symbol: i for i in run(lambda a: a.list_instruments(), handler=handler)}["BTCUSDT"] \
        .funding_interval_hours is None
    with pytest.raises(ExchangeRequestError, match="no funding interval"):
        run(lambda a: a.get_funding_info("BTCUSDT"), handler=handler)


def test_one_malformed_instrument_is_skipped_not_fatal():
    info = copy.deepcopy(EXCHANGE_INFO)
    info["symbols"][0]["filters"] = []  # BTCUSDT loses PRICE_FILTER and LOT_SIZE
    inst = {i.symbol: i for i in run(lambda a: a.list_instruments(), handler=routes(**{"/fapi/v1/exchangeInfo": info}))}
    assert "BTCUSDT" not in inst and "ETHUSDT" in inst


def test_exchange_info_without_symbols_is_a_request_error():
    with pytest.raises(ExchangeRequestError, match="unexpected exchangeInfo"):
        run(lambda a: a.list_instruments(), handler=routes(**{"/fapi/v1/exchangeInfo": {"timezone": "UTC"}}))


# ---- resolve_symbol ----------------------------------------------------------------------------
def test_resolve_symbol_success_for_each_settle_asset():
    async def go(a):
        return (await a.resolve_symbol("BTC", "USDT", "USDT"), await a.resolve_symbol("eth", "usdc", "usdc"),
                await a.resolve_symbol("SOL", "USDT", "USDT"))
    btc, eth, sol = run(go)
    assert (btc.symbol, eth.symbol, sol.symbol) == ("BTCUSDT", "ETHUSDC", "SOLUSDT")


def test_resolve_symbol_not_found_and_not_tradable():
    with pytest.raises(SymbolNotFound, match="no perpetual DOGE/USDT:USDT"):
        run(lambda a: a.resolve_symbol("DOGE", "USDT", "USDT"))
    with pytest.raises(SymbolNotFound, match="not tradable"):
        run(lambda a: a.resolve_symbol("OMG", "USDT", "USDT"))
    with pytest.raises(SymbolNotFound):  # BTC/USDC exists but a BTC/USDC:USDT market does not
        run(lambda a: a.resolve_symbol("BTC", "USDC", "USDT"))


def test_resolve_symbol_ambiguous_when_two_tradable_perps_share_identity():
    info = copy.deepcopy(EXCHANGE_INFO)
    clone = copy.deepcopy(next(s for s in info["symbols"] if s["symbol"] == "BTCUSDT"))
    clone["symbol"] = "BTCUSDT_DUP"  # synthetic duplicate, only to exercise the ambiguity branch
    info["symbols"].append(clone)
    with pytest.raises(SymbolNotFound, match="ambiguous"):
        run(lambda a: a.resolve_symbol("BTC", "USDT", "USDT"), handler=routes(**{"/fapi/v1/exchangeInfo": info}))


def test_instruments_cached_for_ten_minutes():
    h = Harness()

    async def go(a):
        await a.resolve_symbol("BTC", "USDT", "USDT")
        await a.resolve_symbol("ETH", "USDT", "USDT")
        h.monotonic = 599.0
        await a.list_instruments()
        assert h.paths().count("/fapi/v1/exchangeInfo") == 1
        h.monotonic = 601.0
        await a.list_instruments()
    run(go, h)
    assert h.paths().count("/fapi/v1/exchangeInfo") == 2 and h.paths().count("/fapi/v1/fundingInfo") == 2


def test_instrument_ttl_is_injectable():
    h = Harness()

    async def go(a):
        await a.list_instruments()
        h.monotonic = 6.0
        await a.list_instruments()
    run(go, h, instrument_ttl_seconds=5.0)
    assert h.paths().count("/fapi/v1/exchangeInfo") == 2


# ---- funding -----------------------------------------------------------------------------------
def test_funding_info_parsing():
    f = run(lambda a: a.get_funding_info("BTCUSDT"))
    assert f.venue == "binance_usdt_m" and f.symbol == "BTCUSDT"
    assert f.funding_rate == Decimal(PREMIUM["lastFundingRate"])  # a fraction, not percent
    assert f.interval_hours == 8
    assert f.rate_per_hour == f.funding_rate / 8
    assert f.next_funding_time == datetime.fromtimestamp(PREMIUM["nextFundingTime"] / 1000, UTC)
    assert f.next_funding_time.tzinfo is not None
    assert f.is_predicted is True  # lastFundingRate is the running estimate, not the settled rate
    assert f.mark_price == Decimal(PREMIUM["markPrice"]) and f.index_price == Decimal(PREMIUM["indexPrice"])
    assert f.source_time == datetime.fromtimestamp(PREMIUM["time"] / 1000, UTC)


def test_funding_info_uses_the_symbols_own_interval():
    premium = dict(PREMIUM, symbol="LPTUSDT")
    f = run(lambda a: a.get_funding_info("LPTUSDT"), handler=routes(**{"/fapi/v1/premiumIndex": premium}))
    assert f.interval_hours == 4


def test_funding_info_unknown_symbol_and_wrong_symbol_echo():
    with pytest.raises(SymbolNotFound):
        run(lambda a: a.get_funding_info("NOPEUSDT"))
    with pytest.raises(ExchangeRequestError, match="returned BTCUSDT for ETHUSDT"):
        run(lambda a: a.get_funding_info("ETHUSDT"))


def test_funding_info_empty_rate_is_request_error_not_keyerror():
    premium = dict(PREMIUM, lastFundingRate="")
    with pytest.raises(ExchangeRequestError, match="unexpected premiumIndex"):
        run(lambda a: a.get_funding_info("BTCUSDT"), handler=routes(**{"/fapi/v1/premiumIndex": premium}))


def test_funding_history_oldest_first_utc_and_decimal():
    rows = load(V, "funding_rate_btcusdt.json")
    h = Harness()
    hist = run(lambda a: a.get_funding_history("BTCUSDT", limit=10), h)
    assert h.query(0) == {"symbol": "BTCUSDT", "limit": "10"}
    assert [t for t, _ in hist] == sorted(t for t, _ in hist)
    assert hist[-1] == (datetime.fromtimestamp(rows[-1]["fundingTime"] / 1000, UTC), Decimal(rows[-1]["fundingRate"]))
    assert all(t.tzinfo is not None and isinstance(r, Decimal) for t, r in hist)


def test_funding_history_is_reordered_when_venue_returns_newest_first():
    rows = list(reversed(load(V, "funding_rate_btcusdt.json")))
    hist = run(lambda a: a.get_funding_history("BTCUSDT", 10), handler=routes(**{"/fapi/v1/fundingRate": rows}))
    assert hist == sorted(hist)


def test_funding_history_limit_bounds():
    for bad in (0, 1001):
        with pytest.raises(ValueError):
            run(lambda a, bad=bad: a.get_funding_history("BTCUSDT", bad))


# ---- ticker ------------------------------------------------------------------------------------
def test_ticker_parsing_and_usd_volume():
    stats, book = load(V, "ticker_24hr_btcusdt.json"), load(V, "book_ticker_btcusdt.json")
    t = run(lambda a: a.get_ticker("BTCUSDT"))
    assert (t.bid, t.ask) == (Decimal(book["bidPrice"]), Decimal(book["askPrice"]))
    assert t.last == Decimal(stats["lastPrice"]) and t.mark == Decimal(PREMIUM["markPrice"])
    assert t.volume_24h_usd == Decimal(stats["quoteVolume"])
    assert t.source_time == datetime.fromtimestamp(book["time"] / 1000, UTC)


def test_ticker_volume_is_none_for_a_non_usd_quote():
    t = run(lambda a: a.get_ticker("ETHBTC"))  # quote BTC: quoteVolume is in BTC, no rate assumed
    assert t.volume_24h_usd is None


# ---- order book --------------------------------------------------------------------------------
def test_order_book_ordering_base_units_and_timestamps():
    h = Harness()
    book = run(lambda a: a.get_order_book("BTCUSDT", depth=20), h)
    assert h.query(0) == {"symbol": "BTCUSDT", "limit": "20"}
    assert len(book.bids) == len(book.asks) == 20
    assert [lvl.price for lvl in book.bids] == sorted((lvl.price for lvl in book.bids), reverse=True)
    assert [lvl.price for lvl in book.asks] == sorted(lvl.price for lvl in book.asks)
    assert book.best_bid < book.best_ask
    assert book.bids[0].quantity == Decimal(DEPTH["bids"][0][1])  # base units untouched (multiplier 1)
    assert book.source_time == datetime.fromtimestamp(DEPTH["E"] / 1000, UTC)
    assert book.age_ms(now=book.source_time + timedelta(milliseconds=250)) == 250


def test_order_book_sorts_unsorted_input_and_drops_empty_levels():
    payload = dict(DEPTH, bids=list(reversed(DEPTH["bids"])) + [["1.0", "0"]], asks=list(reversed(DEPTH["asks"])))
    book = run(lambda a: a.get_order_book("BTCUSDT", 20), handler=routes(**{"/fapi/v1/depth": payload}))
    assert book.bids[0].price == max(Decimal(p) for p, _ in DEPTH["bids"])
    assert book.asks[0].price == min(Decimal(p) for p, _ in DEPTH["asks"])
    assert all(lvl.quantity > 0 for lvl in book.bids + book.asks)


def test_order_book_depth_maps_to_a_valid_limit_and_is_trimmed():
    h = Harness()
    book = run(lambda a: a.get_order_book("BTCUSDT", depth=7), h)  # 7 is not a valid Binance limit
    assert h.query(0)["limit"] == "10"
    assert len(book.bids) == len(book.asks) == 7
    with pytest.raises(ValueError):
        run(lambda a: a.get_order_book("BTCUSDT", depth=1001))


# ---- candles -----------------------------------------------------------------------------------
def test_candles_oldest_first_and_forming_candle_dropped():
    last_open = KLINES[-1][0]
    now = datetime.fromtimestamp(last_open / 1000, UTC) + timedelta(seconds=30)  # last candle still forming
    h = Harness()
    candles = run(lambda a: a.get_candles("BTCUSDT", "1m", limit=5), h, now=now)
    assert h.query(0) == {"symbol": "BTCUSDT", "interval": "1m", "limit": "6"}
    assert [c.open_time for c in candles] == sorted(c.open_time for c in candles)
    assert len(candles) == 5 and candles[-1].open_time == datetime.fromtimestamp(KLINES[-2][0] / 1000, UTC)
    first = candles[0]
    assert (first.open, first.high, first.low, first.close, first.volume) == tuple(Decimal(x) for x in KLINES[0][1:6])
    assert first.venue == "binance_usdt_m" and first.open_time.tzinfo is not None


def test_candles_include_open_and_all_closed_cases():
    last_open = datetime.fromtimestamp(KLINES[-1][0] / 1000, UTC)
    kept = run(lambda a: a.get_candles("BTCUSDT", "1m", 6, include_open=True), now=last_open + timedelta(seconds=30))
    assert len(kept) == 6
    closed = run(lambda a: a.get_candles("BTCUSDT", "1m", 6), now=last_open + timedelta(seconds=60))
    assert len(closed) == 6  # exactly closed at now == open + 1m


def test_candles_reject_bad_arguments():
    with pytest.raises(ValueError, match="unsupported timeframe"):
        run(lambda a: a.get_candles("BTCUSDT", "7m"))
    with pytest.raises(ValueError):
        run(lambda a: a.get_candles("BTCUSDT", "1m", limit=1500))


# ---- time --------------------------------------------------------------------------------------
def test_server_time():
    t = run(lambda a: a.get_server_time())
    assert t == datetime.fromtimestamp(load(V, "server_time.json")["serverTime"] / 1000, UTC)


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
        assert request.method == "GET" and request.url.path.startswith("/fapi/v1/")
        assert "signature" not in request.url.params and "timestamp" not in request.url.params
        assert not {"x-mbx-apikey", "authorization"} & {k.lower() for k in request.headers}
    assert h.calls[0].url.host == "fapi.binance.com"


def test_account_and_order_methods_are_not_supported():
    async def go(a):
        for call in (a.get_account_equity(), a.get_positions(), a.get_open_orders(), a.get_fee_schedule("BTCUSDT"),
                     a.place_order("BTCUSDT", "BUY", Decimal(1), client_order_id="x"),
                     a.set_leverage("BTCUSDT", Decimal(2)), a.connect_private_streams()):
            with pytest.raises(ExchangeNotSupported):
                await call
    run(go)


def test_testnet_url_is_an_explicit_constructor_choice_only():
    assert BinanceUsdmAdapter.DEFAULT_BASE_URL == "https://fapi.binance.com"
    assert BinanceUsdmAdapter(transport=OFFLINE).base_url == "https://fapi.binance.com"
    testnet = BinanceUsdmAdapter(base_url=BinanceUsdmAdapter.TESTNET_BASE_URL, transport=OFFLINE)
    assert testnet.base_url == "https://demo-fapi.binance.com"


# ---- Binance specific error handling -----------------------------------------------------------
def test_418_ip_ban_is_not_retried_and_says_so():
    h = Harness()
    with pytest.raises(ExchangeRequestError, match="418.*banned|banned.*418"):
        run(lambda a: a.get_server_time(), h, handler=lambda r: httpx.Response(418, headers={"Retry-After": "120"}))
    assert len(h.calls) == 1 and h.sleeps == []


def test_error_body_code_and_message_are_reported_without_retry():
    h = Harness()
    handler = lambda r: httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})  # noqa: E731
    with pytest.raises(ExchangeRequestError, match=r"HTTP 400.*code=-1121.*Invalid symbol"):
        run(lambda a: a.get_funding_history("NOPE"), h, handler=handler)
    assert len(h.calls) == 1
