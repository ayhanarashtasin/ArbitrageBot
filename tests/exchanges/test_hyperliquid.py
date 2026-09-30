import copy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from app.exchanges.base import ExchangeNotSupported, ExchangeRequestError, SymbolNotFound
from app.exchanges.hyperliquid import HyperliquidAdapter, next_funding_time, price_tick

from .helpers import Harness, by_type, load, ms

V = "hyperliquid"
OFFLINE = httpx.MockTransport(lambda request: httpx.Response(500))  # never reached; avoids TLS setup cost
SPOT_META = load(V, "spot_meta.json")
MAC = {"": load(V, "meta_and_asset_ctxs.json"), "xyz": load(V, "meta_and_asset_ctxs_xyz.json"),
       "km": load(V, "meta_and_asset_ctxs_km.json")}
BOOKS = {"BTC": load(V, "l2book_btc.json"), "xyz:TSLA": load(V, "l2book_xyz_tsla.json")}
HISTORY = {"BTC": load(V, "funding_history_btc.json"), "xyz:TSLA": load(V, "funding_history_xyz_tsla.json")}
CANDLES = {"BTC": load(V, "candles_btc_1m.json"), "xyz:TSLA": load(V, "candles_xyz_tsla_1m.json")}


def routes(**overrides):
    base = {
        "exchangeStatus": load(V, "exchange_status.json"),
        "metaAndAssetCtxs": lambda body: MAC[body.get("dex", "")],
        "spotMeta": SPOT_META,
        "l2Book": lambda body: BOOKS[body["coin"]],
        "fundingHistory": lambda body: HISTORY[body["coin"]],
        "candleSnapshot": lambda body: CANDLES[body["req"]["coin"]],
    }
    base.update(overrides)
    return by_type(base)


def run(action, h=None, handler=None, dex=None, **kw):
    h = h or Harness()
    kwargs = {"dex": dex} if dex else {}
    return h.run(HyperliquidAdapter, handler or routes(), action, **kwargs, **kw)


def instruments(dex=None):
    return {i.symbol: i for i in run(lambda a: a.list_instruments(), dex=dex)}


def ctx(symbol, dex=""):
    meta, ctxs = MAC[dex]
    for asset, c in zip(meta["universe"], ctxs, strict=True):
        if asset["name"] == symbol:
            return asset, c
    raise KeyError(symbol)


# ---- price tick (docs "Tick and lot size" examples) --------------------------------------------
@pytest.mark.parametrize("price,sz,tick", [
    ("1234.5", 1, "0.1"),       # docs: 1234.5 is valid, 1234.56 exceeds 5 significant figures
    ("0.001234", 0, "0.000001"),  # docs: 0.001234 valid, 0.0012345 exceeds 6 decimals
    ("0.01234", 1, "0.00001"),  # docs: szDecimals=1 -> 0.01234 valid, 0.012345 not
    ("83144", 5, "1"),          # 5 sig figs leave whole dollars for BTC
    ("353.68", 3, "0.01"),
    ("4156.4", 4, "0.1"),
    ("250000", 2, "1"),         # integer prices are always allowed
])
def test_price_tick_follows_the_documented_rule(price, sz, tick):
    assert price_tick(Decimal(price), sz) == Decimal(tick)


def test_price_tick_matches_recorded_book_price_spacing():
    for coin, sz in (("BTC", 5), ("xyz:TSLA", 3)):
        book = BOOKS[coin]
        tick = price_tick(Decimal(book["levels"][0][0]["px"]), sz)
        prices = [Decimal(lvl["px"]) for side in book["levels"] for lvl in side]
        assert all(p % tick == 0 for p in prices), coin


# ---- instruments -------------------------------------------------------------------------------
def test_instrument_parsing_core_dex():
    inst = instruments()
    btc = inst["BTC"]
    asset, c = ctx("BTC")
    assert (btc.venue, btc.symbol, btc.base, btc.quote, btc.settle) == ("hyperliquid", "BTC", "BTC", "USDC", "USDC")
    assert btc.is_linear and btc.is_perpetual and btc.is_tradable
    assert btc.contract_multiplier == 1
    assert btc.qty_step == btc.min_qty == Decimal("0.00001")  # szDecimals 5
    assert btc.tick_size == price_tick(Decimal(c["markPx"]), 5) == 1
    assert btc.max_qty is None
    assert btc.min_notional == Decimal(10)
    assert btc.funding_interval_hours == 1  # hourly funding
    assert btc.max_leverage == Decimal(asset["maxLeverage"]) == 40
    assert inst["kPEPE"].qty_step == 1 and inst["kPEPE"].tick_size == Decimal("0.000001")
    assert inst["MATIC"].is_tradable is False  # isDelisted


def test_collateral_token_name_comes_from_spot_meta_not_a_constant():
    inst = instruments(dex="km")
    assert {i.quote for i in inst.values()} == {i.settle for i in inst.values()} == {"USDH"}  # collateralToken 360
    assert all(not i.is_tradable for i in inst.values())  # every km asset is delisted


def test_list_instruments_rejects_misaligned_universe():
    meta, ctxs = MAC[""]
    broken = [meta, ctxs[:-1]]
    with pytest.raises(ExchangeRequestError, match="length mismatch"):
        run(lambda a: a.list_instruments(), handler=routes(metaAndAssetCtxs=broken))


def test_unknown_collateral_token_is_an_error():
    spot = copy.deepcopy(SPOT_META)
    spot["tokens"] = [t for t in spot["tokens"] if t["index"] != 0]
    with pytest.raises(ExchangeRequestError, match="unexpected spotMeta"):
        run(lambda a: a.list_instruments(), handler=routes(spotMeta=spot))


def test_one_malformed_asset_is_skipped():
    meta, ctxs = copy.deepcopy(MAC[""])
    del meta["universe"][0]["szDecimals"]
    inst = {i.symbol: i for i in run(lambda a: a.list_instruments(), handler=routes(metaAndAssetCtxs=[meta, ctxs]))}
    assert "BTC" not in inst and "ETH" in inst


# ---- resolve_symbol ----------------------------------------------------------------------------
def test_resolve_symbol_success_not_found_not_tradable_ambiguous():
    async def go(a):
        return (await a.resolve_symbol("BTC", "USDC", "USDC"), await a.resolve_symbol("eth", "usdc", "usdc"),
                await a.resolve_symbol("SOL", "USDC", "USDC"))
    assert [m.symbol for m in run(go)] == ["BTC", "ETH", "SOL"]
    with pytest.raises(SymbolNotFound, match="no perpetual BTC/USDT:USDT"):
        run(lambda a: a.resolve_symbol("BTC", "USDT", "USDT"))  # collateral is USDC, not USDT
    with pytest.raises(SymbolNotFound, match="not tradable"):
        run(lambda a: a.resolve_symbol("MATIC", "USDC", "USDC"))
    meta, ctxs = copy.deepcopy(MAC[""])
    meta["universe"].append(dict(meta["universe"][0], name="BTC"))  # two tradable "BTC" entries
    ctxs.append(copy.deepcopy(ctxs[0]))
    with pytest.raises(SymbolNotFound, match="ambiguous"):
        run(lambda a: a.resolve_symbol("BTC", "USDC", "USDC"), handler=routes(metaAndAssetCtxs=[meta, ctxs]))


def test_instruments_cached_for_ten_minutes():
    h = Harness()

    async def go(a):
        await a.resolve_symbol("BTC", "USDC", "USDC")
        await a.resolve_symbol("ETH", "USDC", "USDC")
        h.monotonic = 599.0
        await a.list_instruments()
        assert [b["type"] for b in map(h.body, range(len(h.calls)))].count("metaAndAssetCtxs") == 1
        h.monotonic = 601.0
        await a.list_instruments()
    run(go, h)
    assert [h.body(i)["type"] for i in range(len(h.calls))].count("metaAndAssetCtxs") == 2


# ---- HIP-3 dex ---------------------------------------------------------------------------------
def test_hip3_dex_symbols_carry_the_dex_namespace_and_route():
    h = Harness()
    inst = {i.symbol: i for i in run(lambda a: a.list_instruments(), h, dex="xyz")}
    assert set(inst) == {"xyz:XYZ100", "xyz:TSLA", "xyz:GOLD"}
    tsla = inst["xyz:TSLA"]
    assert (tsla.venue, tsla.base, tsla.quote, tsla.settle) == ("hyperliquid:xyz", "TSLA", "USDC", "USDC")
    assert tsla.tick_size == Decimal("0.01") and tsla.qty_step == Decimal("0.001")
    assert tsla.funding_interval_hours == 1
    bodies = [h.body(i) for i in range(len(h.calls))]
    meta_bodies = [b for b in bodies if b["type"] == "metaAndAssetCtxs"]
    assert meta_bodies == [{"type": "metaAndAssetCtxs", "dex": "xyz"}]


def test_core_dex_never_sends_a_dex_parameter():
    h = Harness()
    run(lambda a: a.list_instruments(), h)
    assert {"type": "metaAndAssetCtxs"} in [h.body(i) for i in range(len(h.calls))]
    assert all("dex" not in h.body(i) for i in range(len(h.calls)))


def test_hip3_resolve_and_market_data_use_the_prefixed_coin_without_dex_param():
    h = Harness()

    async def go(a):
        meta = await a.resolve_symbol("TSLA", "USDC", "USDC")
        book = await a.get_order_book(meta.symbol)
        hist = await a.get_funding_history(meta.symbol, 5)
        candles = await a.get_candles(meta.symbol, "1m", 3)
        return meta, book, hist, candles
    after_last_candle = datetime.fromtimestamp(CANDLES["xyz:TSLA"][-1]["t"] / 1000, UTC) + timedelta(minutes=2)
    meta, book, hist, candles = run(go, h, dex="xyz", now=after_last_candle)
    assert meta.symbol == "xyz:TSLA"
    assert book.symbol == "xyz:TSLA" and book.venue == "hyperliquid:xyz"
    assert hist and candles
    for i in range(len(h.calls)):
        body = h.body(i)
        if body["type"] in ("l2Book", "fundingHistory", "candleSnapshot"):
            coin = body["coin"] if "coin" in body else body["req"]["coin"]
            assert coin == "xyz:TSLA" and "dex" not in body


def test_dex_argument_validation_and_adapter_naming():
    assert HyperliquidAdapter(transport=OFFLINE).name == "hyperliquid"
    assert HyperliquidAdapter(transport=OFFLINE).dex is None
    assert HyperliquidAdapter(dex="", transport=OFFLINE).name == "hyperliquid"
    assert HyperliquidAdapter(dex="xyz", transport=OFFLINE).name == "hyperliquid:xyz"
    for bad in ("xyz:TSLA", "x y"):
        with pytest.raises(ValueError, match="invalid Hyperliquid dex"):
            HyperliquidAdapter(dex=bad, transport=OFFLINE)


def test_asset_outside_the_requested_dex_namespace_is_skipped():
    with pytest.raises(ExchangeRequestError, match="no universe entry could be parsed"):
        # the server answers with xyz assets for a "km" adapter: never treated as km markets
        run(lambda a: a.list_instruments(), dex="km", handler=routes(metaAndAssetCtxs=lambda body: MAC["xyz"]))


# ---- funding -----------------------------------------------------------------------------------
def test_funding_info_parsing_hourly_and_next_top_of_hour():
    asset, c = ctx("BTC")
    f = run(lambda a: a.get_funding_info("BTC"), now=datetime(2026, 9, 29, 15, 26, 0, tzinfo=UTC))
    assert f.venue == "hyperliquid" and f.symbol == "BTC"
    assert f.funding_rate == Decimal(c["funding"])  # fraction per HOUR as delivered
    assert f.interval_hours == 1 and f.rate_per_hour == f.funding_rate
    assert f.next_funding_time == datetime(2026, 9, 29, 16, 0, 0, tzinfo=UTC)
    assert f.is_predicted is True
    assert f.mark_price == Decimal(c["markPx"]) and f.index_price == Decimal(c["oraclePx"])
    assert f.source_time is None and f.received_at == datetime(2026, 9, 29, 15, 26, tzinfo=UTC)


def test_next_funding_time_boundaries():
    assert next_funding_time(datetime(2026, 9, 29, 15, 0, 0, tzinfo=UTC)) == datetime(2026, 9, 29, 16, 0, tzinfo=UTC)
    assert next_funding_time(datetime(2026, 9, 29, 23, 59, 59, tzinfo=UTC)) == datetime(2026, 9, 30, 0, 0, tzinfo=UTC)
    assert next_funding_time(datetime(2026, 9, 29, 15, 59, 59, 999999, tzinfo=UTC)).minute == 0


def test_recorded_funding_matches_the_documented_formula_for_hourly_units():
    # F8 = P + clamp(0.0001 - P, -0.0005, 0.0005); the API's fundingRate is F8 / 8 (paid every hour).
    for row in HISTORY["BTC"]:
        p = Decimal(row["premium"])
        f8 = p + max(Decimal("-0.0005"), min(Decimal("0.0005"), Decimal("0.0001") - p))
        assert abs(f8 / 8 - Decimal(row["fundingRate"])) < Decimal("1e-9"), row


def test_funding_info_unknown_delisted_and_hip3():
    with pytest.raises(SymbolNotFound):
        run(lambda a: a.get_funding_info("NOPE"))
    with pytest.raises(ExchangeRequestError, match="delisted"):
        run(lambda a: a.get_funding_info("MATIC"))
    f = run(lambda a: a.get_funding_info("xyz:TSLA"), dex="xyz")
    assert f.venue == "hyperliquid:xyz" and f.funding_rate == Decimal(ctx("xyz:TSLA", "xyz")[1]["funding"])


def test_funding_history_oldest_first_and_request_window():
    h = Harness()
    now = datetime.fromtimestamp(HISTORY["BTC"][-1]["time"] / 1000, UTC) + timedelta(minutes=5)
    hist = run(lambda a: a.get_funding_history("BTC", 10), h, now=now)
    body = h.body(0)
    assert body["type"] == "fundingHistory" and body["coin"] == "BTC"
    assert body["startTime"] == ms(now - timedelta(hours=11))
    assert [t for t, _ in hist] == sorted(t for t, _ in hist)
    last = HISTORY["BTC"][-1]
    assert hist[-1] == (datetime.fromtimestamp(last["time"] / 1000, UTC), Decimal(last["fundingRate"]))
    assert all(t.tzinfo is not None and isinstance(r, Decimal) for t, r in hist)


def test_funding_history_trims_to_limit_and_paginates_full_pages():
    template = HISTORY["BTC"][0]
    base_ms = template["time"]
    hour = 3_600_000
    rows = [dict(template, time=base_ms + i * hour) for i in range(510)]  # synthetic rows, real row shape

    def handler(body):
        start = body["startTime"]
        return [r for r in rows if r["time"] >= start][:500]  # venue caps a response at 500 rows
    now = datetime.fromtimestamp((base_ms + 510 * hour) / 1000, UTC)
    h = Harness()
    hist = run(lambda a: a.get_funding_history("BTC", 505), h, now=now, handler=routes(fundingHistory=handler))
    assert len(hist) == 505 and hist[-1][0] == datetime.fromtimestamp(rows[-1]["time"] / 1000, UTC)
    assert len({t for t, _ in hist}) == 505
    assert len(h.calls) == 2  # second request continues after the last returned timestamp
    assert h.body(1)["startTime"] == rows[503]["time"] + 1  # page 1 held rows 4..503
    with pytest.raises(ValueError):
        run(lambda a: a.get_funding_history("BTC", 1001))


# ---- ticker ------------------------------------------------------------------------------------
def test_ticker_uses_book_for_bid_ask_and_ctx_for_mark_and_volume():
    _, c = ctx("BTC")
    book = BOOKS["BTC"]
    t = run(lambda a: a.get_ticker("BTC"))
    assert t.bid == Decimal(book["levels"][0][0]["px"]) and t.ask == Decimal(book["levels"][1][0]["px"])
    assert t.last is None  # no last-trade price on these endpoints
    assert t.mark == Decimal(c["markPx"]) and t.volume_24h_usd == Decimal(c["dayNtlVlm"])
    assert t.source_time == datetime.fromtimestamp(book["time"] / 1000, UTC)


# ---- order book --------------------------------------------------------------------------------
def test_order_book_ordering_base_units_and_timestamp():
    book_json = BOOKS["BTC"]
    h = Harness()
    book = run(lambda a: a.get_order_book("BTC", depth=50), h)
    assert h.body(0) == {"type": "l2Book", "coin": "BTC"}
    assert len(book.bids) == len(book.asks) == 20  # venue maximum
    assert [lvl.price for lvl in book.bids] == sorted((lvl.price for lvl in book.bids), reverse=True)
    assert [lvl.price for lvl in book.asks] == sorted(lvl.price for lvl in book.asks)
    assert book.best_bid < book.best_ask
    assert book.bids[0].quantity == Decimal(book_json["levels"][0][0]["sz"])  # size is in coin units
    assert book.source_time == datetime.fromtimestamp(book_json["time"] / 1000, UTC)
    assert book.age_ms(now=book.source_time + timedelta(milliseconds=400)) == 400


def test_order_book_trims_sorts_and_drops_empty_levels():
    raw = copy.deepcopy(BOOKS["BTC"])
    raw["levels"][0] = list(reversed(raw["levels"][0])) + [{"px": "1.0", "sz": "0.0", "n": 0}]
    raw["levels"][1] = list(reversed(raw["levels"][1]))
    book = run(lambda a: a.get_order_book("BTC", 3), handler=routes(l2Book=raw))
    assert len(book.bids) == len(book.asks) == 3
    assert book.bids[0].price == max(Decimal(lvl["px"]) for lvl in BOOKS["BTC"]["levels"][0])
    assert book.asks[0].price == min(Decimal(lvl["px"]) for lvl in BOOKS["BTC"]["levels"][1])


def test_order_book_null_answer_for_unknown_coin_is_an_error():
    null = httpx.Response(200, content=b"null")
    with pytest.raises(ExchangeRequestError, match="no order book for NOPE"):
        run(lambda a: a.get_order_book("NOPE"), handler=routes(l2Book=lambda body: null))


def test_order_book_coin_echo_is_checked():
    with pytest.raises(ExchangeRequestError, match="returned BTC for ETH"):
        run(lambda a: a.get_order_book("ETH"), handler=routes(l2Book=lambda body: BOOKS["BTC"]))


# ---- candles -----------------------------------------------------------------------------------
def test_candles_oldest_first_and_forming_candle_dropped():
    rows = CANDLES["BTC"]
    last = rows[-1]
    now = datetime.fromtimestamp(last["t"] / 1000, UTC) + timedelta(seconds=20)  # last candle still open
    h = Harness()
    candles = run(lambda a: a.get_candles("BTC", "1m", limit=10), h, now=now)
    body = h.body(0)["req"]
    assert body["coin"] == "BTC" and body["interval"] == "1m" and body["endTime"] == ms(now)
    assert body["startTime"] == ms(now) - 11 * 60_000
    assert [c.open_time for c in candles] == sorted(c.open_time for c in candles)
    assert len(candles) == len(rows) - 1
    assert candles[-1].open_time == datetime.fromtimestamp(rows[-2]["t"] / 1000, UTC)
    first = candles[0]
    assert (first.open, first.high, first.low, first.close, first.volume) == tuple(
        Decimal(rows[0][k]) for k in ("o", "h", "l", "c", "v"))


def test_candles_include_open_all_closed_and_bad_arguments():
    last_open = datetime.fromtimestamp(CANDLES["BTC"][-1]["t"] / 1000, UTC)
    assert len(run(lambda a: a.get_candles("BTC", "1m", 10, include_open=True),
                   now=last_open + timedelta(seconds=20))) == len(CANDLES["BTC"])
    assert len(run(lambda a: a.get_candles("BTC", "1m", 10), now=last_open + timedelta(seconds=60))) \
        == len(CANDLES["BTC"])
    with pytest.raises(ValueError, match="unsupported timeframe"):
        run(lambda a: a.get_candles("BTC", "6h"))  # not an interval on this venue
    with pytest.raises(ValueError):
        run(lambda a: a.get_candles("BTC", "1m", 5000))


# ---- time --------------------------------------------------------------------------------------
def test_server_time_from_exchange_status():
    t = run(lambda a: a.get_server_time())
    assert t == datetime.fromtimestamp(load(V, "exchange_status.json")["time"] / 1000, UTC)


# ---- request hygiene ---------------------------------------------------------------------------
def test_only_read_only_info_queries_without_credentials():
    h = Harness()

    async def go(a):
        await a.get_funding_info("BTC")
        await a.get_ticker("BTC")
        await a.get_order_book("BTC")
        await a.get_candles("BTC")
        await a.get_funding_history("BTC")
        await a.get_server_time()
    run(go, h)
    allowed = {"metaAndAssetCtxs", "spotMeta", "l2Book", "candleSnapshot", "fundingHistory", "exchangeStatus"}
    for i, request in enumerate(h.calls):
        assert request.method == "POST" and request.url.path == "/info" and request.url.host == "api.hyperliquid.xyz"
        assert h.body(i)["type"] in allowed
        assert "signature" not in h.body(i) and "action" not in h.body(i) and "nonce" not in h.body(i)
        assert not {"authorization"} & {k.lower() for k in request.headers}


def test_account_and_order_methods_are_not_supported():
    async def go(a):
        for call in (a.get_account_equity(), a.get_available_balance(), a.get_open_orders(),
                     a.get_fee_schedule("BTC"), a.connect_private_streams()):
            with pytest.raises(ExchangeNotSupported):
                await call
    run(go)


def test_unknown_coin_http_500_is_retried_then_reported():
    h = Harness()
    with pytest.raises(ExchangeRequestError, match="server error \\(HTTP 500\\)"):
        run(lambda a: a.get_funding_history("NOPE"), h, handler=routes(fundingHistory=httpx.Response(500)))
    assert len(h.calls) == 3 and h.sleeps == [0.5, 1.0]


def test_default_and_testnet_urls():
    assert HyperliquidAdapter(transport=OFFLINE).base_url == "https://api.hyperliquid.xyz"
    testnet = HyperliquidAdapter(base_url=HyperliquidAdapter.TESTNET_BASE_URL, transport=OFFLINE)
    assert testnet.base_url == "https://api.hyperliquid-testnet.xyz"
