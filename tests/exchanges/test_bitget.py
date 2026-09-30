import asyncio
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

from app.exchanges.base import ExchangeNotSupported, ExchangeRequestError
from app.exchanges.bitget import BitgetUsdtAdapter
from tests.unit.helpers import T0

D = Decimal
MS = int(T0.timestamp() * 1000)
CONTRACT = {"symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT", "supportMarginCoins": ["USDT"],
            "sizeMultiplier": "0.0001", "minTradeNum": "0.0001", "minTradeUSDT": "5", "priceEndStep": "1",
            "pricePlace": "1", "symbolType": "perpetual", "symbolStatus": "normal", "fundInterval": "8",
            "maxLever": "150", "maxMarketOrderQty": "220", "isRwa": "NO"}


def exercise(action, handler):
    async def go():
        adapter = BitgetUsdtAdapter(transport=httpx.MockTransport(handler), now=lambda: T0, max_retries=0)
        try:
            return await action(adapter)
        finally:
            await adapter.close()
    return asyncio.run(go())


def response(data):
    return httpx.Response(200, json={"code": "00000", "msg": "success", "data": data})


def test_contract_metadata_uses_base_quantity_dynamic_ticks_and_filters_delivery():
    async def go(a):
        rows = await a.list_instruments()
        assert len(rows) == 2
        m = rows[0]
        assert m.qty_step == D("0.0001") and m.tick_size == D("0.1") and m.contract_multiplier == 1
        assert m.min_notional == 5 and m.funding_interval_hours == 8 and m.max_qty == 220
        assert rows[1].underlying_type == "TRADFI" and not rows[1].is_tradable
    exercise(go, lambda r: response([CONTRACT, {**CONTRACT, "symbol": "TSLAUSDT", "baseCoin": "TSLA",
                         "isRwa": "YES", "symbolStatus": "limit_open"}, {**CONTRACT, "symbolType": "delivery"}]))


def test_current_funding_uses_live_interval_and_next_settlement():
    result = exercise(lambda a: a.get_funding_info("BTCUSDT"), lambda r: response([
        {"symbol": "BTCUSDT", "fundingRate": "-0.0001", "fundingRateInterval": "4", "nextUpdate": str(MS)}]))
    assert result.rate_per_hour == D("-0.000025") and result.next_funding_time == T0 and result.is_predicted


def test_book_base_units_sorted_and_all_requests_are_public():
    def handler(r):
        assert r.method == "GET" and not any("ACCESS" in h.upper() for h in r.headers)
        assert r.url.params["precision"] == "scale0" and r.url.params["limit"] == "15"
        return response({"bids": [[99, "0.1"], [100, "0.2"]], "asks": [[102, "0.3"], [101, "0.4"]], "ts": str(MS)})
    book = exercise(lambda a: a.get_order_book("BTCUSDT", 10), handler)
    assert book.best_bid == 100 and book.best_ask == 101 and book.bids[0].quantity == D("0.2")
    assert book.source_time == T0


def test_candle_pagination_removes_open_bars_and_duplicate_boundary():
    calls = []
    def candle(ms):
        return [str(ms), "100", "101", "99", "100", "0.01", "1"]
    def handler(r):
        calls.append(r)
        assert r.url.params["granularity"] == "1m"
        end = int(r.url.params["endTime"])
        if len(calls) == 1:
            return response([candle(MS), candle(MS - 60000), candle(MS - 120000)])
        assert end == MS - 120001
        return response([candle(MS - 120000), candle(MS - 180000)])
    bars = exercise(lambda a: a.get_candles("BTCUSDT", "1m", 3), handler)
    assert len(calls) == 2 and len(bars) == 3 and bars[-1].open_time == T0 - timedelta(minutes=1)
    assert bars[0].volume == D("0.01")


@pytest.mark.parametrize("data", [[], [{"symbol": "ETHUSDT", "fundingRate": ".001"}],
    [{"symbol": "BTCUSDT", "fundingRate": ".001", "fundingRateInterval": "0", "nextUpdate": str(MS)}]])
def test_missing_or_invalid_funding_never_guesses_an_interval(data):
    with pytest.raises(ExchangeRequestError):
        exercise(lambda a: a.get_funding_info("BTCUSDT"), lambda r: response(data))


def test_error_envelope_fails_and_private_orders_are_unsupported():
    with pytest.raises(ExchangeRequestError, match="40034"):
        exercise(lambda a: a.list_instruments(), lambda r: httpx.Response(200, json={"code": "40034", "msg": "bad"}))
    async def go(a):
        with pytest.raises(ExchangeNotSupported):
            await a.get_account_equity()
    exercise(go, lambda r: response([]))
