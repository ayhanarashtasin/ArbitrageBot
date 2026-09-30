import copy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import httpx
import pytest

from app.exchanges import mexc
from app.exchanges.base import (
    ExchangeNotSupported,
    ExchangeRequestError,
    SymbolNotFound,
)
from app.exchanges.mexc import MexcContractAdapter

from .helpers import Harness, by_path, load

V = "mexc"
P = "/api/v1/contract"
OFFLINE = httpx.MockTransport(lambda request: httpx.Response(500))  # never reached; avoids TLS setup cost
DETAIL = load(V, "contract_detail.json")
FUNDING = load(V, "funding_rate_btcusdt.json")
FUNDING_XDP = load(V, "funding_rate_xdpusdt.json")
HISTORY = load(V, "funding_history_btcusdt.json")
TICKER = load(V, "ticker_btcusdt.json")
DEPTH = load(V, "depth_btcusdt.json")
KLINE_1M = load(V, "kline_btcusdt_1m.json")
KLINE_1H = load(V, "kline_btcusdt_1h.json")
NOT_EXIST = load(V, "error_contract_not_exist.json")


def D(value) -> Decimal:
    """Expected Decimal for a fixture number (the adapter parses the same JSON text into Decimal)."""
    return Decimal(str(value))


def at(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def envelope(data, success=True, code=0, **extra):
    return {"success": success, "code": code, "data": data, **extra}


def kline_server(fixture, cap=2000):
    """Serves the recorded kline columns for start <= time <= end (seconds), newest `cap` rows only,
    like the live endpoint does when a window holds more rows than its per-request maximum."""
    cols = fixture["data"]

    def handle(request):
        start, end = int(request.url.params["start"]), int(request.url.params["end"])
        idx = [i for i, t in enumerate(cols["time"]) if start <= t <= end][-cap:]
        return envelope({k: [v[i] for i in idx] for k, v in cols.items()})
    return handle


def routes(**overrides):
    base = {
        f"{P}/ping": load(V, "server_time.json"),
        f"{P}/detail": DETAIL,
        f"{P}/funding_rate/BTC_USDT": FUNDING,
        f"{P}/funding_rate/XDP_USDT": FUNDING_XDP,
        f"{P}/funding_rate/history": HISTORY,
        f"{P}/ticker": TICKER,
        f"{P}/depth/BTC_USDT": DEPTH,
        f"{P}/kline/BTC_USDT": kline_server(KLINE_1M),
    }
    base.update(overrides)
    return by_path(base)


def run(action, h=None, handler=None, **kw):
    h = h or Harness()
    return h.run(MexcContractAdapter, handler or routes(), action, **kw)


def instruments(handler=None):
    return {i.symbol: i for i in run(lambda a: a.list_instruments(), handler=handler)}


def detail_with(change):
    payload = copy.deepcopy(DETAIL)
    change({r["symbol"]: r for r in payload["data"]})
    return payload


def raw_contract(symbol):
    return next(r for r in DETAIL["data"] if r["symbol"] == symbol)


# ---- instruments -------------------------------------------------------------------------------
def test_instrument_parsing_converts_contracts_to_base_units():
    raw = raw_contract("BTC_USDT")
    assert (raw["contractSize"], raw["volUnit"], raw["minVol"]) == (0.0001, 1, 1)  # 1 contract = 0.0001 BTC
    btc = instruments()["BTC_USDT"]
    assert (btc.venue, btc.base, btc.quote, btc.settle) == ("mexc_contract", "BTC", "USDT", "USDT")
    assert btc.is_linear and btc.is_perpetual and btc.is_tradable
    assert btc.contract_multiplier == Decimal("0.0001")
    assert btc.qty_step == Decimal("0.0001") and btc.min_qty == Decimal("0.0001")  # volUnit/minVol * contractSize
    assert btc.max_qty == D(raw["maxVol"]) * Decimal("0.0001") == Decimal(40)  # 400000 contracts
    assert btc.tick_size == D(raw["priceUnit"]) == Decimal("0.1")
    assert btc.min_notional is None
    assert btc.funding_interval_hours is None  # contract/detail publishes no interval
    assert btc.max_leverage == D(raw["maxLeverage"])


def test_large_contract_size_and_other_margin_coins():
    inst = instruments()
    pepe = inst["PEPE_USDT"]
    assert pepe.base == "PEPE" and pepe.contract_multiplier == Decimal(10_000_000)  # 1 contract = 10M PEPE
    assert pepe.qty_step == pepe.min_qty == Decimal(10_000_000)
    assert pepe.tick_size == D(raw_contract("PEPE_USDT")["priceUnit"])
    eth = inst["ETH_USDT"]
    assert eth.contract_multiplier == Decimal("0.01") and eth.qty_step == Decimal("0.01")
    usdc = inst["BTC_USDC"]
    assert (usdc.base, usdc.quote, usdc.settle, usdc.is_tradable) == ("BTC", "USDC", "USDC", True)


def test_inverse_and_non_perpetual_rows_are_ignored():
    assert raw_contract("BTC_USD")["settleCoin"] == "BTC"  # inverse: quote USD, settle BTC
    assert "BTC_USD" not in instruments()

    def drop_future_type(rows):
        del rows["ETH_USDT"]["futureType"]
        rows["BTC_USDC"]["futureType"] = 2  # synthetic: an unknown future type is not a perpetual
    inst = instruments(routes(**{f"{P}/detail": detail_with(drop_future_type)}))
    assert "ETH_USDT" not in inst and "BTC_USDC" not in inst and "BTC_USDT" in inst


def test_tradability_needs_enabled_state_api_access_and_no_scheduled_delivery():
    inst = instruments()
    assert raw_contract("FLORKBSC_USDT")["deliveryTime"] > 0 and raw_contract("FLORKBSC_USDT")["state"] == 0
    assert inst["FLORKBSC_USDT"].is_tradable is False  # scheduled delisting
    assert raw_contract("EACC_USDT")["apiAllowed"] is False
    assert inst["EACC_USDT"].is_tradable is False
    paused = instruments(routes(**{f"{P}/detail": detail_with(lambda rows: rows["ETH_USDT"].update(state=4))}))
    assert paused["ETH_USDT"].is_tradable is False


def test_one_malformed_contract_is_skipped_not_fatal():
    inst = instruments(routes(**{f"{P}/detail": detail_with(lambda rows: rows["BTC_USDT"].pop("contractSize"))}))
    assert "BTC_USDT" not in inst and "ETH_USDT" in inst
    zero = instruments(routes(**{f"{P}/detail": detail_with(lambda rows: rows["ETH_USDT"].update(contractSize=0))}))
    assert "ETH_USDT" not in zero

    def break_all(rows):
        for r in rows.values():
            r.pop("priceUnit")
    with pytest.raises(ExchangeRequestError, match="no contract detail row could be parsed"):
        instruments(routes(**{f"{P}/detail": detail_with(break_all)}))


def test_equity_perps_keep_mexc_base_coin():
    raw = raw_contract("TESLA_USDT")
    assert raw["baseCoinName"] == "TSLA"
    assert instruments()["TESLA_USDT"].base == "TESLA"
    with pytest.raises(SymbolNotFound, match="no perpetual TSLA/USDT:USDT"):
        run(lambda a: a.resolve_symbol("TSLA", "USDT", "USDT"))  # never guessed from a display name


# ---- resolve_symbol ----------------------------------------------------------------------------
def test_resolve_symbol_usdt_and_usdc():
    async def go(a):
        return [await a.resolve_symbol(b, q, s) for b, q, s in
                (("BTC", "USDT", "USDT"), ("BTC", "USDC", "USDC"), ("eth", "usdt", "usdt"), ("PEPE", "USDT", "USDT"))]
    assert [m.symbol for m in run(go)] == ["BTC_USDT", "BTC_USDC", "ETH_USDT", "PEPE_USDT"]


def test_resolve_symbol_not_found_and_not_tradable():
    with pytest.raises(SymbolNotFound, match="no perpetual ETH/USDC:USDC"):
        run(lambda a: a.resolve_symbol("ETH", "USDC", "USDC"))  # not in the trimmed fixture
    with pytest.raises(SymbolNotFound, match="no perpetual BTC/USD:BTC"):
        run(lambda a: a.resolve_symbol("BTC", "USD", "BTC"))  # inverse contracts are not served
    with pytest.raises(SymbolNotFound, match="not tradable"):
        run(lambda a: a.resolve_symbol("FLORKBSC", "USDT", "USDT"))


def test_instruments_cached_for_ten_minutes():
    h = Harness()

    async def go(a):
        await a.resolve_symbol("BTC", "USDT", "USDT")
        await a.get_ticker("BTC_USDT")
        await a.get_order_book("BTC_USDT")
        h.monotonic = 599.0
        await a.list_instruments()
        assert h.paths().count(f"{P}/detail") == 1  # detail is limited to 1 request / 5 s
        h.monotonic = 600.5
        await a.list_instruments()
    run(go, h)
    assert h.paths().count(f"{P}/detail") == 2


# ---- funding -----------------------------------------------------------------------------------
def test_funding_info_parsing():
    data, tick = FUNDING["data"], TICKER["data"]
    h = Harness()
    f = run(lambda a: a.get_funding_info("BTC_USDT"), h)
    assert sorted(h.paths()) == [f"{P}/funding_rate/BTC_USDT", f"{P}/ticker"]  # no contract/detail needed
    assert h.query(h.paths().index(f"{P}/ticker")) == {"symbol": "BTC_USDT"}
    assert f.venue == "mexc_contract" and f.symbol == "BTC_USDT"
    assert f.funding_rate == D(data["fundingRate"]) == Decimal("-0.00001")  # fraction per interval
    assert f.interval_hours == Decimal(data["collectCycle"]) == 8
    assert f.next_funding_time == datetime.fromtimestamp(data["nextSettleTime"] / 1000, UTC)
    assert f.next_funding_time == datetime(2026, 9, 30, 16, 0, tzinfo=UTC)
    assert f.is_predicted is True
    assert f.mark_price == D(tick["fairPrice"]) and f.index_price == D(tick["indexPrice"])
    assert f.source_time == datetime.fromtimestamp(data["timestamp"] / 1000, UTC)
    assert f.rate_per_hour == f.funding_rate / 8


def test_four_hour_collect_cycle():
    ticker = copy.deepcopy(TICKER)
    ticker["data"]["symbol"] = "XDP_USDT"  # synthetic: only the symbol differs, to pair with the XDP funding row
    f = run(lambda a: a.get_funding_info("XDP_USDT"), handler=routes(**{f"{P}/ticker": ticker}))
    assert f.interval_hours == 4 and f.funding_rate == D(FUNDING_XDP["data"]["fundingRate"])
    assert f.next_funding_time == datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("cycle", [None, 0, "absent"])
def test_funding_without_interval_is_an_error(cycle):
    payload = copy.deepcopy(FUNDING)
    if cycle == "absent":
        del payload["data"]["collectCycle"]
    else:
        payload["data"]["collectCycle"] = cycle
    with pytest.raises(ExchangeRequestError, match="no funding interval"):
        run(lambda a: a.get_funding_info("BTC_USDT"), handler=routes(**{f"{P}/funding_rate/BTC_USDT": payload}))


def test_funding_symbol_mismatch_is_an_error():
    with pytest.raises(ExchangeRequestError, match="ticker returned no row for XDP_USDT"):
        run(lambda a: a.get_funding_info("XDP_USDT"))  # the ticker fixture row is BTC_USDT
    with pytest.raises(ExchangeRequestError, match="funding_rate returned BTC_USDT for XDP_USDT"):
        run(lambda a: a.get_funding_info("XDP_USDT"), handler=routes(**{
            f"{P}/funding_rate/XDP_USDT": FUNDING,
            f"{P}/ticker": envelope(dict(TICKER["data"], symbol="XDP_USDT"))}))


def test_funding_history_is_reordered_oldest_first():
    rows = HISTORY["data"]["resultList"]
    assert rows[0]["settleTime"] > rows[-1]["settleTime"]  # venue: newest first
    h = Harness()
    hist = run(lambda a: a.get_funding_history("BTC_USDT", 10), h)
    assert h.query(0) == {"symbol": "BTC_USDT", "page_num": "1", "page_size": "10"}
    assert [t for t, _ in hist] == sorted(t for t, _ in hist) and len(hist) == len(rows) == 10
    assert all(b - a == timedelta(hours=8) for (a, _), (b, _) in pairwise(hist))
    newest = rows[0]
    assert hist[-1] == (datetime.fromtimestamp(newest["settleTime"] / 1000, UTC), D(newest["fundingRate"]))
    assert hist[0][1] == D(rows[-1]["fundingRate"])


def test_funding_history_limits_and_symbol_check():
    for bad in (0, 1001):
        with pytest.raises(ValueError):
            run(lambda a, bad=bad: a.get_funding_history("BTC_USDT", bad))
    assert len(run(lambda a: a.get_funding_history("BTC_USDT", 3))) == 3  # trimmed even if the venue sends more
    with pytest.raises(ExchangeRequestError, match="another symbol"):
        run(lambda a: a.get_funding_history("ETH_USDT"))


# ---- ticker ------------------------------------------------------------------------------------
def test_ticker_parsing():
    row = TICKER["data"]
    h = Harness()
    t = run(lambda a: a.get_ticker("BTC_USDT"), h)
    assert h.query(h.paths().index(f"{P}/ticker")) == {"symbol": "BTC_USDT"}
    assert (t.bid, t.ask, t.last, t.mark) == tuple(D(row[k]) for k in ("bid1", "ask1", "lastPrice", "fairPrice"))
    assert t.volume_24h_usd == D(row["amount24"])  # quote-coin (USDT) turnover, not volume24 contracts
    assert t.source_time == datetime.fromtimestamp(row["timestamp"] / 1000, UTC)


def test_ticker_needs_a_single_matching_row():
    with pytest.raises(ExchangeRequestError, match="no row for ETH_USDT"):
        run(lambda a: a.get_ticker("ETH_USDT"))
    every = envelope([TICKER["data"]])  # the no-symbol form of the endpoint returns a list
    with pytest.raises(ExchangeRequestError, match="no row for BTC_USDT"):
        run(lambda a: a.get_ticker("BTC_USDT"), handler=routes(**{f"{P}/ticker": every}))


# ---- order book --------------------------------------------------------------------------------
def test_order_book_ordering_units_and_timestamp():
    data = DEPTH["data"]
    assert len(data["bids"][0]) == 3  # [price, contracts, order_count]
    h = Harness()
    book = run(lambda a: a.get_order_book("BTC_USDT", depth=20), h)
    assert h.query(h.paths().index(f"{P}/depth/BTC_USDT")) == {"limit": "20"}
    assert len(book.bids) == len(book.asks) == 20
    assert [lvl.price for lvl in book.bids] == sorted((lvl.price for lvl in book.bids), reverse=True)
    assert [lvl.price for lvl in book.asks] == sorted(lvl.price for lvl in book.asks)
    assert book.best_bid < book.best_ask
    assert book.bids[0].quantity == D(data["bids"][0][1]) * Decimal("0.0001")  # contracts -> BTC
    assert book.asks[0].quantity == D(data["asks"][0][1]) * Decimal("0.0001")
    assert book.source_time == datetime.fromtimestamp(data["timestamp"] / 1000, UTC)
    assert book.age_ms(now=book.source_time + timedelta(seconds=2)) == 2000


def test_order_book_sorts_trims_and_uses_each_contract_size():
    data = dict(DEPTH["data"], bids=list(reversed(DEPTH["data"]["bids"])), asks=list(reversed(DEPTH["data"]["asks"])))
    book = run(lambda a: a.get_order_book("BTC_USDT", 5), handler=routes(**{f"{P}/depth/BTC_USDT": envelope(data)}))
    assert len(book.bids) == len(book.asks) == 5
    assert book.bids[0].price == max(D(r[0]) for r in DEPTH["data"]["bids"])
    assert book.asks[0].price == min(D(r[0]) for r in DEPTH["data"]["asks"])
    # synthetic: the BTC rows served for PEPE_USDT, only to check the 10,000,000 PEPE contract size
    pepe = run(lambda a: a.get_order_book("PEPE_USDT", 3), handler=routes(**{f"{P}/depth/PEPE_USDT": DEPTH}))
    assert pepe.bids[0].quantity == D(DEPTH["data"]["bids"][0][1]) * 10_000_000


def test_order_book_bounds_and_unknown_symbol():
    for bad in (0, 1001):
        with pytest.raises(ValueError):
            run(lambda a, bad=bad: a.get_order_book("BTC_USDT", bad))
    with pytest.raises(SymbolNotFound):
        run(lambda a: a.get_order_book("NOPE_USDT"), handler=routes(**{f"{P}/depth/NOPE_USDT": DEPTH}))


# ---- candles -----------------------------------------------------------------------------------
LAST_1M = KLINE_1M["data"]["time"][-1]  # still forming when recorded


def closed_opens(candles):
    return [int(c.open_time.timestamp()) for c in candles]


def test_candles_oldest_first_closed_only_and_base_volume():
    cols = KLINE_1M["data"]
    assert cols["time"] == sorted(cols["time"])  # venue: oldest first, seconds
    h = Harness()
    candles = run(lambda a: a.get_candles("BTC_USDT", "1m", limit=5), h, now=at(LAST_1M + 20))
    kline_calls = [i for i, p in enumerate(h.paths()) if p == f"{P}/kline/BTC_USDT"]
    assert len(kline_calls) == 1
    assert h.query(kline_calls[0]) == {"interval": "Min1", "start": str(LAST_1M - 300), "end": str(LAST_1M + 20)}
    assert closed_opens(candles) == [LAST_1M - 60 * k for k in range(5, 0, -1)]  # forming candle dropped
    newest, i = candles[-1], cols["time"].index(LAST_1M - 60)
    assert (newest.open, newest.high, newest.low, newest.close) == tuple(
        D(cols[k][i]) for k in ("open", "high", "low", "close"))
    assert newest.volume == D(cols["vol"][i]) * Decimal("0.0001")  # contracts -> BTC
    # sanity: base volume * price ~ the venue's quote amount for that minute
    assert abs(newest.volume * newest.close / D(cols["amount"][i]) - 1) < Decimal("0.01")


def test_candles_include_open_and_after_close():
    assert closed_opens(run(lambda a: a.get_candles("BTC_USDT", "1m", 6, include_open=True),
                            now=at(LAST_1M + 20)))[-1] == LAST_1M
    assert closed_opens(run(lambda a: a.get_candles("BTC_USDT", "1m", 6), now=at(LAST_1M + 60)))[-1] == LAST_1M


def test_one_hour_candles_map_to_min60():
    last_1h = KLINE_1H["data"]["time"][-1]
    h = Harness()
    candles = run(lambda a: a.get_candles("BTC_USDT", "1h", limit=3), h, now=at(last_1h + 1800),
                  handler=routes(**{f"{P}/kline/BTC_USDT": kline_server(KLINE_1H)}))
    q = h.query(h.paths().index(f"{P}/kline/BTC_USDT"))
    assert q["interval"] == "Min60" and int(q["start"]) == last_1h - 3 * 3600
    assert closed_opens(candles) == [last_1h - 3600 * k for k in (3, 2, 1)]
    i = KLINE_1H["data"]["time"].index(last_1h - 3600)
    assert candles[-1].volume == D(KLINE_1H["data"]["vol"][i]) * Decimal("0.0001")


def test_candles_paginate_backwards_in_bounded_windows(monkeypatch):
    monkeypatch.setattr(mexc, "_KLINE_PAGE_ROWS", 8)
    h = Harness()
    candles = run(lambda a: a.get_candles("BTC_USDT", "1m", limit=20), h, now=at(LAST_1M + 20))
    windows = [(int(h.query(i)["start"]), int(h.query(i)["end"])) for i, p in enumerate(h.paths())
               if p == f"{P}/kline/BTC_USDT"]
    assert len(windows) == 3
    assert all((end - end % 60 - start) // 60 + 1 <= 8 for start, end in windows)  # never more rows than a page
    assert all(older[1] < newer[0] for newer, older in pairwise(windows))  # no overlap
    assert closed_opens(candles) == [LAST_1M - 60 * k for k in range(20, 0, -1)]


def test_candles_paginate_when_the_venue_truncates_a_window():
    h = Harness()
    candles = run(lambda a: a.get_candles("BTC_USDT", "1m", limit=20), h, now=at(LAST_1M + 20),
                  handler=routes(**{f"{P}/kline/BTC_USDT": kline_server(KLINE_1M, cap=7)}))
    assert h.paths().count(f"{P}/kline/BTC_USDT") == 3
    assert closed_opens(candles) == [LAST_1M - 60 * k for k in range(20, 0, -1)]  # contiguous, no gaps


def test_candles_stop_at_start_of_history_and_page_budget():
    h = Harness()
    candles = run(lambda a: a.get_candles("BTC_USDT", "1m", limit=100), h, now=at(LAST_1M + 20))
    assert len(candles) == len(KLINE_1M["data"]["time"]) - 1  # all recorded closed candles
    assert h.paths().count(f"{P}/kline/BTC_USDT") == 2  # second window was empty: history exhausted
    h = Harness()
    candles = run(lambda a: a.get_candles("BTC_USDT", "1m", limit=20), h, now=at(LAST_1M + 20),
                  handler=routes(**{f"{P}/kline/BTC_USDT": kline_server(KLINE_1M, cap=1)}))
    assert h.paths().count(f"{P}/kline/BTC_USDT") == 5  # hard bound on requests per call
    assert len(candles) == 4


def test_candles_bad_arguments_and_malformed_columns():
    with pytest.raises(ValueError, match="unsupported timeframe"):
        run(lambda a: a.get_candles("BTC_USDT", "3m"))  # MEXC has no 3-minute interval
    for bad in (0, 5001):
        with pytest.raises(ValueError):
            run(lambda a, bad=bad: a.get_candles("BTC_USDT", "1m", limit=bad))
    ragged = copy.deepcopy(KLINE_1M)
    ragged["data"]["vol"].pop()
    with pytest.raises(ExchangeRequestError, match="unexpected kline"):
        run(lambda a: a.get_candles("BTC_USDT", "1m", 5), now=at(LAST_1M + 20),
            handler=routes(**{f"{P}/kline/BTC_USDT": ragged}))


# ---- time --------------------------------------------------------------------------------------
def test_server_time():
    payload = load(V, "server_time.json")
    assert run(lambda a: a.get_server_time()) == datetime.fromtimestamp(payload["data"] / 1000, UTC)


# ---- MEXC error envelope -----------------------------------------------------------------------
def test_error_body_on_http_200_is_an_error_and_not_retried():
    h = Harness()
    with pytest.raises(ExchangeRequestError, match=r"code=1001, message=Contract does not exist"):
        run(lambda a: a.get_funding_history("NOPE_USDT"), h,
            handler=routes(**{f"{P}/funding_rate/history": NOT_EXIST}))
    assert len(h.calls) == 1 and h.sleeps == []


def test_rate_limit_code_is_not_retried():
    h = Harness()
    limited = {"success": False, "code": 510, "message": "Excessive frequency of requests"}
    with pytest.raises(ExchangeRequestError, match="rate limit.*not retried"):
        run(lambda a: a.get_server_time(), h, handler=routes(**{f"{P}/ping": limited}))
    assert len(h.calls) == 1 and h.sleeps == []


def test_malformed_envelopes_are_errors():
    for bad in ([], {"code": 0, "data": 1}, {"success": True, "code": 0}, envelope(1, code=600)):
        with pytest.raises(ExchangeRequestError):
            run(lambda a: a.get_server_time(), handler=routes(**{f"{P}/ping": bad}))


def test_http_429_is_not_retried():
    h = Harness()
    with pytest.raises(ExchangeRequestError, match="rate limit exceeded"):
        run(lambda a: a.get_server_time(), h, handler=lambda r: httpx.Response(429))
    assert len(h.calls) == 1


def test_symbols_are_validated_before_building_a_path():
    h = Harness()
    for bad in ("../private/account/assets", "BTC_USDT?x=1", "BTC USDT", ""):
        with pytest.raises(SymbolNotFound, match="invalid contract symbol"):
            run(lambda a, bad=bad: a.get_funding_info(bad), h)
    assert h.calls == []


# ---- request hygiene ---------------------------------------------------------------------------
def test_only_public_get_requests_without_credentials():
    h = Harness()

    async def go(a):
        await a.get_funding_info("BTC_USDT")
        await a.get_ticker("BTC_USDT")
        await a.get_order_book("BTC_USDT")
        await a.get_candles("BTC_USDT", limit=5)
        await a.get_funding_history("BTC_USDT")
        await a.get_server_time()
    run(go, h, now=at(LAST_1M + 20))
    for request in h.calls:
        assert request.method == "GET" and request.url.path.startswith(f"{P}/")
        assert "private" not in request.url.path
        assert not {"apikey", "signature", "request-time", "authorization"} & {k.lower() for k in request.headers}
    assert {r.url.host for r in h.calls} == {"api.mexc.com"}


def test_account_and_order_methods_are_not_supported():
    async def go(a):
        for call in (a.get_account_equity(), a.get_available_balance(), a.get_positions(), a.get_position("BTC_USDT"),
                     a.get_open_orders(), a.get_order(order_id="1"), a.get_fills(), a.get_fee_schedule("BTC_USDT"),
                     a.set_leverage("BTC_USDT", Decimal(2)),
                     a.place_order("BTC_USDT", "BUY", Decimal(1), client_order_id="x"),
                     a.cancel_order("BTC_USDT", order_id="1"), a.connect_private_streams(),
                     a.connect_market_streams()):
            with pytest.raises(ExchangeNotSupported):
                await call
    h = Harness()
    run(go, h)
    assert h.calls == []


def test_default_and_legacy_urls():
    assert MexcContractAdapter(transport=OFFLINE).base_url == "https://api.mexc.com"
    assert MexcContractAdapter.LEGACY_BASE_URL == "https://contract.mexc.com"
    assert MexcContractAdapter.name == "mexc_contract"
