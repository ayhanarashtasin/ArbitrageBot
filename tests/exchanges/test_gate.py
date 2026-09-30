import copy
import itertools
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from app.exchanges import gate
from app.exchanges.base import ExchangeNotSupported, ExchangeRequestError, SymbolNotFound
from app.exchanges.gate import GateUsdtFuturesAdapter

from .helpers import Harness, by_path, load

V = "gate"
P = "/api/v4/futures/usdt"  # request.url.path includes the base URL's /api/v4 prefix
OFFLINE = httpx.MockTransport(lambda request: httpx.Response(500))  # never reached; avoids TLS setup cost
CONTRACTS = load(V, "contracts.json")
CONTRACT_BTC = load(V, "contract_btc_usdt.json")
TICKERS = load(V, "tickers_btc_usdt.json")
BOOK_BTC = load(V, "order_book_btc_usdt.json")
BOOK_ETH = load(V, "order_book_eth_usdt.json")
KLINE_1M = load(V, "candlesticks_btc_usdt_1m.json")
KLINE_1H = load(V, "candlesticks_btc_usdt_1h.json")
HISTORY = load(V, "funding_rate_btc_usdt.json")


def utc(seconds) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def contracts_route(rows):
    """Serve `rows` with Gate's limit/offset paging (the fixture list is shorter than one live page)."""
    def route(request):
        limit, offset = int(request.url.params["limit"]), int(request.url.params["offset"])
        return rows[offset:offset + limit]
    return route


def routes(**overrides):
    base = {
        "/api/v4/spot/time": load(V, "server_time.json"),
        f"{P}/contracts": contracts_route(CONTRACTS),
        f"{P}/contracts/BTC_USDT": CONTRACT_BTC,
        f"{P}/tickers": TICKERS,
        f"{P}/order_book": BOOK_BTC,
        f"{P}/funding_rate": HISTORY,
        f"{P}/candlesticks": KLINE_1M,
    }
    base.update(overrides)
    return by_path(base)


def run(action, h=None, handler=None, **kw):
    h = h or Harness()
    return h.run(GateUsdtFuturesAdapter, handler or routes(), action, **kw)


def instruments(rows=None):
    handler = routes(**{f"{P}/contracts": contracts_route(rows)}) if rows is not None else None
    return {i.symbol: i for i in run(lambda a: a.list_instruments(), handler=handler)}


def raw(name, rows=CONTRACTS):
    return next(r for r in rows if r["name"] == name)


def with_contract(name, **changes):
    rows = copy.deepcopy(CONTRACTS)
    raw(name, rows).update(changes)
    return rows


# ---- instruments -------------------------------------------------------------------------------
def test_instrument_parsing_btc_contracts_become_base_units():
    btc = instruments()["BTC_USDT"]
    assert (btc.venue, btc.base, btc.quote, btc.settle) == ("gate_usdt", "BTC", "USDT", "USDT")
    assert btc.is_linear and btc.is_perpetual and btc.is_tradable
    assert btc.contract_multiplier == Decimal("0.0001")  # quanto_multiplier: BTC per contract
    assert btc.tick_size == Decimal("0.1")  # order_price_round
    assert btc.qty_step == Decimal("0.0001")  # one contract
    assert btc.min_qty == Decimal("0.0001")  # order_size_min "1" contract
    # min(order_size_max 12,000,000, market_order_size_max 10,000,000) contracts * 0.0001 BTC
    assert btc.max_qty == Decimal("1000")
    assert btc.min_notional is None
    assert btc.funding_interval_hours == Decimal(8)  # funding_interval 28800 seconds
    assert btc.max_leverage == Decimal("200")


def test_decimal_size_contracts_use_whole_contract_step():
    inst = instruments()
    eth, pepe, sol = inst["ETH_USDT"], inst["PEPE_USDT"], inst["SOL_USDT"]
    assert raw("ETH_USDT")["enable_decimal"] is True and raw("ETH_USDT")["order_size_min"] == "0.1"
    assert eth.contract_multiplier == Decimal("0.01")
    assert eth.qty_step == Decimal("0.01") and eth.min_qty == Decimal("0.01")  # 1 contract, not 0.1
    assert eth.max_qty == Decimal("8000000") * Decimal("0.01")
    assert pepe.contract_multiplier == Decimal("10000000")  # one contract = 10M PEPE
    assert pepe.qty_step == pepe.min_qty == Decimal("10000000")
    assert pepe.tick_size == Decimal("0.000000001")
    assert sol.qty_step == sol.min_qty == Decimal(1)
    for meta in inst.values():
        assert meta.min_qty % meta.qty_step == 0  # min_qty is always a whole number of steps


def test_min_qty_follows_order_size_min_above_one_contract():
    inst = instruments(with_contract("BTC_USDT", order_size_min="5"))
    assert inst["BTC_USDT"].min_qty == Decimal("0.0005") and inst["BTC_USDT"].qty_step == Decimal("0.0001")


def test_market_size_max_zero_means_no_separate_cap_and_other_intervals():
    inst = instruments()
    gp = inst["GP_USDT"]
    assert raw("GP_USDT")["market_order_size_max"] == "0"
    assert gp.max_qty == Decimal(raw("GP_USDT")["order_size_max"]) * 100  # quanto_multiplier 100
    assert gp.funding_interval_hours == Decimal(1)  # funding_interval 3600
    assert inst["龙虾_USDT"].funding_interval_hours == Decimal(4)  # funding_interval 14400


def test_base_comes_from_the_contract_name():
    inst = instruments()
    assert list(inst) == [r["name"] for r in CONTRACTS]  # venue order kept
    assert (inst["AAPL_USDT"].base, raw("AAPL_USDT")["contract_type"]) == ("AAPL", "stocks")
    assert inst["龙虾_USDT"].base == "龙虾"
    assert inst["PEPE_USDT"].base == "PEPE"
    assert all(i.quote == i.settle == "USDT" for i in inst.values())


@pytest.mark.parametrize("status", ["prelaunch", "delisting", "delisted", "circuit_breaker"])
def test_non_trading_status_is_not_tradable(status):
    assert instruments(with_contract("SOL_USDT", status=status))["SOL_USDT"].is_tradable is False


def test_in_delisting_is_not_tradable():
    assert instruments(with_contract("SOL_USDT", in_delisting=True))["SOL_USDT"].is_tradable is False


def test_missing_funding_interval_is_none():
    assert instruments(with_contract("SOL_USDT", funding_interval=0))["SOL_USDT"].funding_interval_hours is None
    rows = copy.deepcopy(CONTRACTS)
    del raw("SOL_USDT", rows)["funding_interval"]
    assert instruments(rows)["SOL_USDT"].funding_interval_hours is None


def test_one_malformed_instrument_is_skipped_not_fatal():
    broken = copy.deepcopy(CONTRACTS)
    del raw("BTC_USDT", broken)["quanto_multiplier"]
    raw("ETH_USDT", broken)["quanto_multiplier"] = "0"
    raw("SOL_USDT", broken)["name"] = "SOL_USDC"  # not this settle family
    inst = instruments(broken)
    assert not {"BTC_USDT", "ETH_USDT", "SOL_USDT", "SOL_USDC"} & set(inst) and "PEPE_USDT" in inst
    everything_broken = [dict(r, order_price_round="") for r in CONTRACTS]
    with pytest.raises(ExchangeRequestError, match="no contracts row could be parsed"):
        instruments(everything_broken)


def test_contracts_pagination_uses_limit_and_offset(monkeypatch):
    monkeypatch.setattr(gate, "_CONTRACTS_PAGE_SIZE", 3)
    h = Harness()
    inst = run(lambda a: a.list_instruments(), h)
    assert [i.symbol for i in inst] == [r["name"] for r in CONTRACTS]  # 7 rows: pages of 3, 3, 1
    assert h.paths() == [f"{P}/contracts"] * 3
    assert [h.query(i) for i in range(3)] == [{"limit": "3", "offset": str(o)} for o in (0, 3, 6)]


def test_contracts_pagination_stops_on_empty_page_and_dedupes(monkeypatch):
    monkeypatch.setattr(gate, "_CONTRACTS_PAGE_SIZE", 7)  # the fixture exactly fills one page
    h = Harness()
    assert len(run(lambda a: a.list_instruments(), h)) == len(CONTRACTS)
    assert [h.query(i)["offset"] for i in range(len(h.calls))] == ["0", "7"]  # second page is []
    shifted = CONTRACTS[:4] + CONTRACTS[3:]  # a listing added mid-pagination repeats one row
    monkeypatch.setattr(gate, "_CONTRACTS_PAGE_SIZE", 4)
    assert list(instruments(shifted)) == [r["name"] for r in CONTRACTS]


def test_pagination_that_never_ends_is_bounded(monkeypatch):
    monkeypatch.setattr(gate, "_CONTRACTS_PAGE_SIZE", 1)
    counter = itertools.count()
    endless = by_path({f"{P}/contracts": lambda r: [dict(CONTRACTS[0], name=f"X{next(counter)}_USDT")]})
    h = Harness()
    with pytest.raises(ExchangeRequestError, match="pagination did not terminate"):
        run(lambda a: a.list_instruments(), h, handler=endless)
    assert len(h.calls) == 50


def test_contracts_that_is_not_a_list_is_an_error():
    with pytest.raises(ExchangeRequestError, match="unexpected contracts response"):
        run(lambda a: a.list_instruments(), handler=routes(**{f"{P}/contracts": {"oops": 1}}))


# ---- resolve_symbol ----------------------------------------------------------------------------
def test_resolve_symbol_success():
    async def go(a):
        return [await a.resolve_symbol(b, "USDT", "USDT") for b in ("BTC", "eth", "SOL", "PEPE", "龙虾")]
    assert [m.symbol for m in run(go)] == ["BTC_USDT", "ETH_USDT", "SOL_USDT", "PEPE_USDT", "龙虾_USDT"]


def test_resolve_symbol_not_found_not_tradable_and_ambiguous():
    with pytest.raises(SymbolNotFound, match="no perpetual DOGE/USDT:USDT"):
        run(lambda a: a.resolve_symbol("DOGE", "USDT", "USDT"))
    with pytest.raises(SymbolNotFound, match="no perpetual BTC/USDC:USDC"):
        run(lambda a: a.resolve_symbol("BTC", "USDC", "USDC"))  # this adapter only serves USDT settle
    prelaunch = with_contract("SOL_USDT", status="prelaunch")
    with pytest.raises(SymbolNotFound, match="not tradable"):
        run(lambda a: a.resolve_symbol("SOL", "USDT", "USDT"), handler=routes(**{f"{P}/contracts": contracts_route(prelaunch)}))
    dup = copy.deepcopy(CONTRACTS)
    dup.append(dict(raw("BTC_USDT", dup), name="btc_USDT"))  # synthetic: base "btc" matches case-insensitively
    with pytest.raises(SymbolNotFound, match="ambiguous"):
        run(lambda a: a.resolve_symbol("BTC", "USDT", "USDT"), handler=routes(**{f"{P}/contracts": contracts_route(dup)}))


def test_instruments_cached_for_ten_minutes():
    h = Harness()

    async def go(a):
        await a.resolve_symbol("BTC", "USDT", "USDT")
        await a.resolve_symbol("ETH", "USDT", "USDT")
        h.monotonic = 599.0
        await a.list_instruments()
        assert h.paths().count(f"{P}/contracts") == 1
        h.monotonic = 600.5
        await a.list_instruments()
    run(go, h)
    assert h.paths().count(f"{P}/contracts") == 2


# ---- funding -----------------------------------------------------------------------------------
def test_funding_info_parsing():
    h = Harness()
    f = run(lambda a: a.get_funding_info("BTC_USDT"), h)
    assert h.paths() == [f"{P}/contracts/BTC_USDT"]  # one fresh call, no instrument list needed
    assert f.venue == "gate_usdt"
    assert f.funding_rate == Decimal(CONTRACT_BTC["funding_rate"]) == Decimal("-0.000027")  # fraction per interval
    assert f.interval_hours == 8  # funding_interval 28800 s
    assert f.next_funding_time == utc(CONTRACT_BTC["funding_next_apply"]) == datetime(2026, 9, 30, 16, tzinfo=UTC)
    assert f.is_predicted is True
    assert f.mark_price == Decimal(CONTRACT_BTC["mark_price"]) and f.index_price == Decimal(CONTRACT_BTC["index_price"])
    assert f.source_time is None and f.received_at is not None
    assert f.rate_per_hour == Decimal("-0.000027") / 8


def test_funding_interval_comes_from_the_fresh_contract_row():
    changed = dict(CONTRACT_BTC, funding_interval=3600)  # Gate may shorten an interval at the rate cap
    f = run(lambda a: a.get_funding_info("BTC_USDT"), handler=routes(**{f"{P}/contracts/BTC_USDT": changed}))
    assert f.interval_hours == 1


@pytest.mark.parametrize("interval", [0, None])
def test_funding_without_interval_is_an_error(interval):
    payload = dict(CONTRACT_BTC, funding_interval=interval)
    with pytest.raises(ExchangeRequestError, match="no funding interval"):
        run(lambda a: a.get_funding_info("BTC_USDT"), handler=routes(**{f"{P}/contracts/BTC_USDT": payload}))


@pytest.mark.parametrize("value", [0, ""])
def test_missing_next_funding_time_is_none(value):
    payload = dict(CONTRACT_BTC, funding_next_apply=value)
    f = run(lambda a: a.get_funding_info("BTC_USDT"), handler=routes(**{f"{P}/contracts/BTC_USDT": payload}))
    assert f.next_funding_time is None


def test_funding_info_symbol_checks():
    with pytest.raises(ExchangeRequestError, match="returned BTC_USDT for ETH_USDT"):
        run(lambda a: a.get_funding_info("ETH_USDT"), handler=routes(**{f"{P}/contracts/ETH_USDT": CONTRACT_BTC}))
    h = Harness()
    for bad in ("../spot/time", "BTC_USDT?x=1", "", "BTC USDT"):
        with pytest.raises(SymbolNotFound):
            run(lambda a, bad=bad: a.get_funding_info(bad), h)
    assert h.calls == []  # rejected before any request


def test_funding_info_non_ascii_contract_is_percent_encoded():
    h = Harness()
    payload = dict(CONTRACT_BTC, name="龙虾_USDT", funding_interval=14400)
    f = run(lambda a: a.get_funding_info("龙虾_USDT"), h, handler=routes(**{f"{P}/contracts/龙虾_USDT": payload}))
    assert f.interval_hours == 4
    assert h.calls[0].url.raw_path == b"/api/v4/futures/usdt/contracts/%E9%BE%99%E8%99%BE_USDT"


def test_funding_history_is_reordered_oldest_first():
    assert HISTORY[0]["t"] > HISTORY[-1]["t"]  # venue: newest first
    h = Harness()
    hist = run(lambda a: a.get_funding_history("BTC_USDT", 10), h)
    assert h.query(0) == {"contract": "BTC_USDT", "limit": "10"}
    assert [t for t, _ in hist] == sorted(t for t, _ in hist) and len(hist) == len(HISTORY)
    assert hist[-1] == (utc(HISTORY[0]["t"]), Decimal(HISTORY[0]["r"]))
    assert hist[0][0].tzinfo is UTC
    spacing = {b[0] - a[0] for a, b in itertools.pairwise(hist)}
    assert spacing == {timedelta(hours=8)}  # matches BTC_USDT funding_interval 28800 s


def test_funding_history_limit_bounds():
    for bad in (0, 1001):
        with pytest.raises(ValueError):
            run(lambda a, bad=bad: a.get_funding_history("BTC_USDT", bad))


# ---- ticker ------------------------------------------------------------------------------------
def test_ticker_parsing():
    row = TICKERS[0]
    h = Harness()
    t = run(lambda a: a.get_ticker("BTC_USDT"), h)
    assert h.paths() == [f"{P}/tickers"] and h.query(0) == {"contract": "BTC_USDT"}
    keys = ("highest_bid", "lowest_ask", "last", "mark_price")
    assert (t.bid, t.ask, t.last, t.mark) == tuple(Decimal(row[k]) for k in keys)
    assert t.volume_24h_usd == Decimal(row["volume_24h_quote"])  # USDT, not contracts or base
    assert t.volume_24h_usd != Decimal(row["volume_24h"])
    assert t.source_time is None


def test_ticker_empty_prices_are_none():
    payload = [dict(TICKERS[0], highest_bid="", lowest_ask="")]
    t = run(lambda a: a.get_ticker("BTC_USDT"), handler=routes(**{f"{P}/tickers": payload}))
    assert t.bid is None and t.ask is None and t.last is not None


def test_ticker_symbol_must_match_exactly_one_row():
    with pytest.raises(ExchangeRequestError, match="no unique row"):
        run(lambda a: a.get_ticker("ETH_USDT"))  # fixture row is BTC_USDT
    with pytest.raises(ExchangeRequestError, match="no unique row"):
        run(lambda a: a.get_ticker("BTC_USDT"), handler=routes(**{f"{P}/tickers": []}))


# ---- order book --------------------------------------------------------------------------------
def test_order_book_quantities_are_base_units_and_ordered():
    h = Harness()
    book = run(lambda a: a.get_order_book("BTC_USDT", depth=20), h)
    assert h.query(h.paths().index(f"{P}/order_book")) == {"contract": "BTC_USDT", "limit": "20"}
    assert len(book.bids) == len(book.asks) == 20
    assert [lvl.price for lvl in book.bids] == sorted((lvl.price for lvl in book.bids), reverse=True)
    assert [lvl.price for lvl in book.asks] == sorted(lvl.price for lvl in book.asks)
    assert book.best_bid < book.best_ask
    top = BOOK_BTC["bids"][0]
    assert book.bids[0].price == Decimal(top["p"])
    assert book.bids[0].quantity == Decimal(top["s"]) * Decimal("0.0001")  # contracts -> BTC
    assert book.source_time == datetime(2026, 9, 30, 8, 46, 28, 835000, tzinfo=UTC)  # `current`, ms kept
    assert book.source_time == utc(BOOK_BTC["current"]).replace(microsecond=835000)
    assert book.age_ms(now=book.source_time + timedelta(seconds=2)) == 2000


def test_order_book_decimal_contract_sizes():
    book = run(lambda a: a.get_order_book("ETH_USDT", depth=5), handler=routes(**{f"{P}/order_book": BOOK_ETH}))
    tiny = next(lvl for lvl in BOOK_ETH["asks"] if lvl["s"] == "0.1")  # a fractional contract
    assert Decimal(tiny["p"]) * 0 == 0
    level = next(lvl for lvl in book.asks if lvl.price == Decimal(tiny["p"]))
    assert level.quantity == Decimal("0.001")  # 0.1 contract * 0.01 ETH
    assert book.bids[0].quantity == Decimal(BOOK_ETH["bids"][0]["s"]) * Decimal("0.01")


def test_order_book_sorts_trims_and_drops_zero_sizes():
    shuffled = dict(BOOK_BTC, bids=list(reversed(BOOK_BTC["bids"])) + [{"p": "1", "s": "0"}],
                    asks=list(reversed(BOOK_BTC["asks"])))
    book = run(lambda a: a.get_order_book("BTC_USDT", 5), handler=routes(**{f"{P}/order_book": shuffled}))
    assert len(book.bids) == len(book.asks) == 5
    assert book.bids[0].price == max(Decimal(r["p"]) for r in BOOK_BTC["bids"])
    assert book.asks[0].price == min(Decimal(r["p"]) for r in BOOK_BTC["asks"])
    assert all(lvl.quantity > 0 for lvl in book.bids)


def test_order_book_bounds_and_unknown_symbol():
    for bad in (0, 301):
        with pytest.raises(ValueError):
            run(lambda a, bad=bad: a.get_order_book("BTC_USDT", bad))
    with pytest.raises(SymbolNotFound, match="unknown symbol DOGE_USDT"):
        run(lambda a: a.get_order_book("DOGE_USDT"))  # not in the contract list: no multiplier to apply


# ---- candles -----------------------------------------------------------------------------------
def test_candles_oldest_first_forming_dropped_and_volume_in_base():
    assert [r["t"] for r in KLINE_1M] == sorted(r["t"] for r in KLINE_1M)  # venue: ascending
    newest_open = utc(KLINE_1M[-1]["t"])
    h = Harness()
    candles = run(lambda a: a.get_candles("BTC_USDT", "1m", limit=5), h, now=newest_open + timedelta(seconds=20))
    kline_calls = [i for i, p in enumerate(h.paths()) if p == f"{P}/candlesticks"]
    assert len(kline_calls) == 1
    assert h.query(kline_calls[0]) == {"contract": "BTC_USDT", "interval": "1m", "limit": "6"}
    assert [c.open_time for c in candles] == sorted(c.open_time for c in candles)
    assert len(candles) == 5
    assert candles[-1].open_time == utc(KLINE_1M[-2]["t"])  # the forming candle is dropped
    row = KLINE_1M[-2]
    last = candles[-1]
    assert (last.open, last.high, last.low, last.close) == tuple(Decimal(row[k]) for k in "ohlc")
    assert last.volume == Decimal(row["v"]) * Decimal("0.0001")  # contracts -> BTC
    # sanity: quote volume / base volume lands inside the candle's range
    assert last.low <= Decimal(row["sum"]) / last.volume <= last.high


def test_candles_include_open_and_bad_arguments():
    newest_open = utc(KLINE_1M[-1]["t"])
    assert len(run(lambda a: a.get_candles("BTC_USDT", "1m", 6, include_open=True),
                   now=newest_open + timedelta(seconds=20))) == 6
    assert len(run(lambda a: a.get_candles("BTC_USDT", "1m", 12), now=newest_open + timedelta(seconds=60))) == 12
    for undocumented in ("3m", "2h", "12h"):
        with pytest.raises(ValueError, match="unsupported timeframe"):
            run(lambda a, tf=undocumented: a.get_candles("BTC_USDT", tf))
    for bad in (0, 5001):
        with pytest.raises(ValueError):
            run(lambda a, bad=bad: a.get_candles("BTC_USDT", "1m", limit=bad))


def test_hourly_candles():
    newest_open = utc(KLINE_1H[-1]["t"])
    h = Harness()
    candles = run(lambda a: a.get_candles("BTC_USDT", "1h", limit=340), h,
                  handler=routes(**{f"{P}/candlesticks": KLINE_1H}), now=newest_open + timedelta(minutes=46))
    kline_call = h.paths().index(f"{P}/candlesticks")
    assert h.query(kline_call) == {"contract": "BTC_USDT", "interval": "1h", "limit": "341"}  # one request
    assert len(candles) == len(KLINE_1H) - 1  # only what the venue returned, minus the forming hour
    assert all(b.open_time - a.open_time == timedelta(hours=1) for a, b in itertools.pairwise(candles))
    assert candles[-1].volume == Decimal(KLINE_1H[-2]["v"]) * Decimal("0.0001")


def _window_route(rows):
    """Serve the recorded candles that fall inside the request's inclusive from/to window."""
    def route(request):
        params = request.url.params
        assert "limit" not in params  # documented: limit conflicts with from/to
        start, end = int(params["from"]), int(params["to"])
        return [r for r in rows if start <= r["t"] <= end]
    return route


def test_candles_paginate_with_from_to_windows_when_over_the_per_request_cap(monkeypatch):
    monkeypatch.setattr(gate, "_MAX_CANDLES_PER_REQUEST", 5)
    newest_open = KLINE_1M[-1]["t"]
    now = utc(newest_open) + timedelta(seconds=20)
    h = Harness()
    candles = run(lambda a: a.get_candles("BTC_USDT", "1m", limit=8), h,
                  handler=routes(**{f"{P}/candlesticks": _window_route(KLINE_1M)}), now=now)
    windows = [h.query(i) for i, p in enumerate(h.paths()) if p == f"{P}/candlesticks"]
    assert [(int(w["from"]), int(w["to"])) for w in windows] == [
        (newest_open - 4 * 60, newest_open), (newest_open - 9 * 60, newest_open - 5 * 60)]
    assert all(w["contract"] == "BTC_USDT" and w["interval"] == "1m" for w in windows)
    assert [c.open_time for c in candles] == [utc(r["t"]) for r in KLINE_1M[-9:-1]]  # 8 closed, oldest first


def test_candle_pagination_stops_when_history_runs_out(monkeypatch):
    monkeypatch.setattr(gate, "_MAX_CANDLES_PER_REQUEST", 5)
    now = utc(KLINE_1M[-1]["t"]) + timedelta(seconds=20)
    h = Harness()
    candles = run(lambda a: a.get_candles("BTC_USDT", "1m", limit=15), h,
                  handler=routes(**{f"{P}/candlesticks": _window_route(KLINE_1M)}), now=now)
    assert len(candles) == len(KLINE_1M) - 1  # all closed candles that exist
    assert h.paths().count(f"{P}/candlesticks") == 4  # 5 + 5 + 2 rows, then an empty window
    assert len(h.calls) <= 1 + 5  # contracts + at most ceil(16 / 5) + 1 windows


def test_candle_pagination_is_bounded(monkeypatch):
    monkeypatch.setattr(gate, "_MAX_CANDLES_PER_REQUEST", 5)
    h = Harness()
    same = by_path({f"{P}/contracts": contracts_route(CONTRACTS), f"{P}/candlesticks": KLINE_1M[:1]})
    run(lambda a: a.get_candles("BTC_USDT", "1m", limit=20), h, handler=same)
    assert h.paths().count(f"{P}/candlesticks") == 6  # ceil(21 / 5) + 1 spare, never more


# ---- time --------------------------------------------------------------------------------------
def test_server_time_is_epoch_milliseconds():
    payload = load(V, "server_time.json")
    h = Harness()
    t = run(lambda a: a.get_server_time(), h)
    assert h.paths() == ["/api/v4/spot/time"]
    assert t == datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=payload["server_time"])


# ---- Gate errors -------------------------------------------------------------------------------
def test_api_error_status_raises_and_is_not_retried():
    h = Harness()
    not_found = httpx.Response(400, json={"label": "CONTRACT_NOT_FOUND"})  # recorded live for NOPE_USDT
    with pytest.raises(ExchangeRequestError, match=r"gate_usdt: HTTP 400 on GET /futures/usdt/contracts/NOPE_USDT"):
        run(lambda a: a.get_funding_info("NOPE_USDT"), h,
            handler=routes(**{f"{P}/contracts/NOPE_USDT": lambda r: not_found}))
    assert len(h.calls) == 1 and h.sleeps == []


def test_error_label_in_a_200_body_is_an_error():
    body = {"label": "INVALID_PARAM_VALUE", "message": "Invalid request parameter `limit` value: 2001"}
    with pytest.raises(ExchangeRequestError, match=r"label=INVALID_PARAM_VALUE, message=Invalid request"):
        run(lambda a: a.get_order_book("BTC_USDT"), handler=routes(**{f"{P}/order_book": body}))
    with pytest.raises(ExchangeRequestError, match="rate limit"):
        run(lambda a: a.get_server_time(), handler=routes(**{"/api/v4/spot/time": {"label": "TOO_MANY_REQUESTS"}}))


def test_http_429_is_not_retried():
    h = Harness()
    limited = httpx.Response(429, json={"label": "TOO_MANY_REQUESTS", "message": "Request Rate limit Exceeded"})
    with pytest.raises(ExchangeRequestError, match="rate limit exceeded.*not retried"):
        run(lambda a: a.get_ticker("BTC_USDT"), h, handler=lambda r: limited)
    assert len(h.calls) == 1 and h.sleeps == []


def test_malformed_payloads_raise_exchange_request_error():
    with pytest.raises(ExchangeRequestError, match="unexpected funding_rate history response"):
        run(lambda a: a.get_funding_history("BTC_USDT"), handler=routes(**{f"{P}/funding_rate": [{"t": 1}]}))
    with pytest.raises(ExchangeRequestError, match="unexpected candlesticks response"):
        run(lambda a: a.get_candles("BTC_USDT"), handler=routes(**{f"{P}/candlesticks": [{"t": "x"}]}))
    with pytest.raises(ExchangeRequestError, match="unexpected order_book response"):
        run(lambda a: a.get_order_book("BTC_USDT"), handler=routes(**{f"{P}/order_book": {"bids": []}}))


# ---- request hygiene ---------------------------------------------------------------------------
def test_only_public_get_requests_with_size_decimal_header_and_no_credentials():
    h = Harness()

    async def go(a):
        await a.get_funding_info("BTC_USDT")
        await a.get_ticker("BTC_USDT")
        await a.get_order_book("BTC_USDT")
        await a.get_candles("BTC_USDT")
        await a.get_funding_history("BTC_USDT")
        await a.get_server_time()
    run(go, h)
    assert len(h.calls) == 7  # the 6 calls above + one contracts page
    for request in h.calls:
        assert request.method == "GET"
        assert request.url.path.startswith(P + "/") or request.url.path == "/api/v4/spot/time"
        assert request.headers["X-Gate-Size-Decimal"] == "1"  # otherwise Gate truncates sizes
        assert not {"key", "sign", "timestamp", "authorization"} & {k.lower() for k in request.headers}
    assert h.calls[0].url.host == "api.gateio.ws"


def test_account_and_order_methods_are_not_supported():
    async def go(a):
        for call in (a.get_account_equity(), a.get_available_balance(), a.get_positions(),
                     a.get_position("BTC_USDT"), a.get_open_orders(), a.get_order(order_id="1"), a.get_fills(),
                     a.get_fee_schedule("BTC_USDT"), a.set_leverage("BTC_USDT", Decimal(2)),
                     a.place_order("BTC_USDT", "BUY", Decimal(1), client_order_id="x"),
                     a.cancel_order("BTC_USDT", order_id="1"), a.connect_private_streams(),
                     a.connect_market_streams()):
            with pytest.raises(ExchangeNotSupported):
                await call
    h = Harness()
    run(go, h)
    assert h.calls == []


def test_default_alternative_and_testnet_urls():
    assert GateUsdtFuturesAdapter.name == "gate_usdt"
    assert GateUsdtFuturesAdapter(transport=OFFLINE).base_url == "https://api.gateio.ws/api/v4"
    assert GateUsdtFuturesAdapter.FUTURES_ALT_BASE_URL == "https://fx-api.gateio.ws/api/v4"
    testnet = GateUsdtFuturesAdapter(base_url=GateUsdtFuturesAdapter.TESTNET_BASE_URL, transport=OFFLINE)
    assert testnet.base_url == "https://api-testnet.gateapi.io/api/v4"
