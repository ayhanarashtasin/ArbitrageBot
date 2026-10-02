"""Signed venue clients: signatures, request shapes, response parsing and the error policy (no network).

Signatures are checked against each venue's documented recipe (Binance also against its published test vector).
"""

import asyncio
import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

import app.exchanges.private.base as private_base
from app.domain.market import InstrumentMeta, OrderStatus, Side
from app.exchanges.base import ExchangeRequestError
from app.exchanges.private.base import (
    AuthError,
    OrderNotFound,
    OrderRejected,
    to_contracts,
)
from app.exchanges.private.binance import BinancePrivate
from app.exchanges.private.bitget import BitgetPrivate
from app.exchanges.private.blofin import BlofinPrivate, venue_client_id
from app.exchanges.private.bybit import BybitPrivate
from app.exchanges.private.credentials import VenueCredentials
from app.exchanges.private.mexc import MexcPrivate

D = Decimal
CREDS = VenueCredentials("the-key", "the-secret", "the-passphrase")
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
CID = "arb0007-s0003-L-a01"


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(private_base, "READ_BACKOFF_SECONDS", 0)


def meta(symbol="BTCUSDT", venue="v", multiplier="1", step="0.001"):
    return InstrumentMeta(venue=venue, symbol=symbol, base="BTC", quote="USDT", settle="USDT", is_linear=True,
                          is_perpetual=True, is_tradable=True, contract_multiplier=D(multiplier), tick_size=D("0.1"),
                          qty_step=D(step), min_qty=D(step), max_qty=None, min_notional=None,
                          funding_interval_hours=D(8), max_leverage=D(50), received_at=NOW)


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        status, payload = item if isinstance(item, tuple) else (200, item)
        return httpx.Response(status, json=payload)

    def query(self, i=-1) -> dict:
        return {k: v[0] for k, v in parse_qs(urlsplit(str(self.requests[i].url)).query).items()}

    def body(self, i=-1) -> dict:
        return json.loads(self.requests[i].content)


def client(cls, rec):
    c = cls(CREDS, transport=httpx.MockTransport(rec))
    c.offset_ms = 0
    return c


def go(coro):
    return asyncio.run(coro)


# ---- shared helpers -----------------------------------------------------------------------------

def test_quantities_convert_to_whole_contracts_and_refuse_partial_lots():
    m = meta(multiplier="0.0001", step="0.0001")
    assert to_contracts(m, D("0.0123")) == D(123)
    with pytest.raises(OrderRejected):
        to_contracts(meta(step="0.001"), D("0.0015"))


def test_blofin_client_ids_drop_hyphens_and_stay_unique():
    assert venue_client_id(CID) == "arb0007s0003La01"
    assert venue_client_id("arb0007-s0003-S-a01") != venue_client_id(CID)


# ---- Binance --------------------------------------------------------------------------------------

def test_binance_signature_matches_the_published_example(monkeypatch):
    c = BinancePrivate(VenueCredentials("dbefbc809e3e83c283a984c3a1459732ea7db1360ca80c5c2c8867408d28cc83",
                                        "2b5eb11e18796d12d88f13dc27dbbd02c2cc51ff7059765ed9821957d82bb4d9"))
    monkeypatch.setattr(c, "now_ms", lambda: 1591702613943)
    headers, query = c._sign("POST", "/fapi/v1/order",
                             "symbol=BTCUSDT&side=BUY&type=LIMIT&quantity=1&price=9000&timeInForce=GTC", "")
    assert query.endswith("&signature=3c661234138461fcc7a7d8746c6558c9842d4e10870d2ecbedf7777cad694af9")
    assert headers == {"X-MBX-APIKEY": "dbefbc809e3e83c283a984c3a1459732ea7db1360ca80c5c2c8867408d28cc83"}


def test_binance_places_a_market_order_and_reads_it_back():
    rec = Recorder({"orderId": 42, "status": "FILLED"},
                   {"symbol": "BTCUSDT", "orderId": 42, "clientOrderId": CID, "side": "BUY", "status": "FILLED",
                    "origQty": "0.010", "executedQty": "0.010", "avgPrice": "60000.5", "reduceOnly": False})
    c = client(BinancePrivate, rec)
    assert go(c.place_market_order(meta(), Side.BUY, D("0.010"), client_order_id=CID, reduce_only=True)) == "42"
    q = rec.query(0)
    assert rec.requests[0].method == "POST" and rec.requests[0].url.path == "/fapi/v1/order"
    assert q["type"] == "MARKET" and q["quantity"] == "0.01" and q["newClientOrderId"] == CID
    assert q["reduceOnly"] == "true" and "signature" in q and "timestamp" in q
    o = go(c.get_order(meta(), CID))
    assert o.status is OrderStatus.FILLED and o.filled_quantity == D("0.010") and o.average_fill_price == D("60000.5")


def test_binance_error_policy():
    c = client(BinancePrivate, Recorder((400, {"code": -2013, "msg": "Order does not exist."})))
    with pytest.raises(OrderNotFound):
        go(c.get_order(meta(), CID))
    c = client(BinancePrivate, Recorder((400, {"code": -2019, "msg": "Margin is insufficient."})))
    with pytest.raises(OrderRejected):
        go(c.place_market_order(meta(), Side.BUY, D("0.01"), client_order_id=CID, reduce_only=False))
    c = client(BinancePrivate, Recorder((401, {"code": -2015, "msg": "Invalid API-key, IP, or permissions."})))
    with pytest.raises(AuthError):
        go(c.account())
    rec = Recorder((503, {"code": -1007, "msg": "Unknown error"}))
    c = client(BinancePrivate, rec)
    with pytest.raises(ExchangeRequestError):  # execution status unknown: never "rejected", never retried
        go(c.place_market_order(meta(), Side.BUY, D("0.01"), client_order_id=CID, reduce_only=False))
    assert len(rec.requests) == 1
    rec = Recorder((503, {}), (200, {"totalMarginBalance": "100", "availableBalance": "80", "totalMaintMargin": "1"}))
    assert go(client(BinancePrivate, rec).account()).available_usd == D(80)  # a read is retried
    assert len(rec.requests) == 2


def test_binance_margin_type_already_isolated_is_fine():
    rec = Recorder((400, {"code": -4046, "msg": "No need to change margin type."}), {"leverage": 4})
    go(client(BinancePrivate, rec).prepare_symbol(meta(), D(4)))
    assert [r.url.path for r in rec.requests] == ["/fapi/v1/marginType", "/fapi/v1/leverage"]
    assert rec.query(0)["marginType"] == "ISOLATED" and rec.query(1)["leverage"] == "4"


def test_binance_position_and_fills_are_parsed():
    rec = Recorder([{"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "-0.020", "entryPrice": "60000",
                     "markPrice": "60100", "liquidationPrice": "74000", "isolatedWallet": "300"}])
    p = go(client(BinancePrivate, rec).position(meta()))
    assert p.quantity == D("-0.020") and p.liquidation_price == D(74000) and p.margin_mode == "isolated"
    rec = Recorder([{"id": 7, "orderId": 42, "symbol": "BTCUSDT", "side": "SELL", "qty": "0.02", "price": "60000",
                     "commission": "0.48", "commissionAsset": "USDT", "time": 1790000000000}])
    f = go(client(BinancePrivate, rec).fills(meta(), order_id="42"))[0]
    assert (f.trade_id, f.order_id, f.side, f.quantity, f.fee, f.fee_asset) == ("7", "42", Side.SELL, D("0.02"),
                                                                              D("0.48"), "USDT")


# ---- Bybit ----------------------------------------------------------------------------------------

def test_bybit_signature_follows_the_documented_recipe(monkeypatch):
    c = BybitPrivate(CREDS)
    monkeypatch.setattr(c, "now_ms", lambda: 1658384314791)
    headers, _ = c._sign("GET", "/v5/order/realtime", "category=linear&symbol=BTCUSDT", "")
    expected = hmac.new(b"the-secret", b"1658384314791the-key5000category=linear&symbol=BTCUSDT",
                        hashlib.sha256).hexdigest()
    assert headers["X-BAPI-SIGN"] == expected and headers["X-BAPI-TIMESTAMP"] == "1658384314791"
    headers, _ = c._sign("POST", "/v5/order/create", "", '{"category":"linear"}')
    assert headers["X-BAPI-SIGN"] == hmac.new(b"the-secret", headers["X-BAPI-TIMESTAMP"].encode() +
                                              b'the-key5000{"category":"linear"}', hashlib.sha256).hexdigest()


def test_bybit_order_round_trip_and_partial_fill_mapping():
    rec = Recorder({"retCode": 0, "result": {"orderId": "abc", "orderLinkId": CID}},
                   {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "orderId": "abc", "orderLinkId": CID,
                                                      "side": "Sell", "orderStatus": "Cancelled", "qty": "0.010",
                                                      "cumExecQty": "0.004", "avgPrice": "60000"}]}})
    c = client(BybitPrivate, rec)
    assert go(c.place_market_order(meta(), Side.SELL, D("0.010"), client_order_id=CID, reduce_only=False)) == "abc"
    b = rec.body(0)
    assert b == {"category": "linear", "symbol": "BTCUSDT", "side": "Sell", "orderType": "Market", "qty": "0.01",
                 "positionIdx": 0, "orderLinkId": CID, "reduceOnly": False}
    o = go(c.get_order(meta(), CID))
    assert o.status is OrderStatus.CANCELED and o.filled_quantity == D("0.004")  # IOC remainder cancelled


def test_bybit_unknown_order_and_errors():
    empty = {"retCode": 0, "result": {"list": []}}
    rec = Recorder(empty, empty)
    with pytest.raises(OrderNotFound):
        go(client(BybitPrivate, rec).get_order(meta(), CID))
    assert [r.url.path for r in rec.requests] == ["/v5/order/realtime", "/v5/order/history"]
    with pytest.raises(OrderRejected):
        go(client(BybitPrivate, Recorder({"retCode": 110007, "retMsg": "insufficient"})).place_market_order(
            meta(), Side.BUY, D("0.01"), client_order_id=CID, reduce_only=False))
    with pytest.raises(AuthError):
        go(client(BybitPrivate, Recorder({"retCode": 10010, "retMsg": "Unmatched IP"})).account())


def test_bybit_funding_is_read_from_funding_executions_with_the_sign_flipped():
    rec = Recorder({"retCode": 0, "result": {"list": [
        {"execId": "f1", "symbol": "BTCUSDT", "execType": "Funding", "execFee": "-0.25", "feeCurrency": "USDT",
         "execTime": "1790000000000", "orderId": "", "side": "Sell", "execQty": "0.01", "execPrice": "60000"}]}})
    rows = go(client(BybitPrivate, rec).funding_income(meta(), NOW))
    assert rows[0].amount == D("0.25") and rec.query()["execType"] == "Funding"


# ---- Bitget ---------------------------------------------------------------------------------------

def test_bitget_signature_follows_the_documented_recipe(monkeypatch):
    c = BitgetPrivate(CREDS)
    monkeypatch.setattr(c, "now_ms", lambda: 16273667805456)
    headers, _ = c._sign("GET", "/api/v2/mix/market/depth", "limit=20&symbol=BTCUSDT", "")
    prehash = b"16273667805456GET/api/v2/mix/market/depth?limit=20&symbol=BTCUSDT"
    assert headers["ACCESS-SIGN"] == base64.b64encode(hmac.new(b"the-secret", prehash, hashlib.sha256).digest()).decode()
    assert headers["ACCESS-PASSPHRASE"] == "the-passphrase"


def test_bitget_order_fills_and_not_found():
    rec = Recorder({"code": "00000", "data": {"orderId": "9", "clientOid": CID}})
    c = client(BitgetPrivate, rec)
    go(c.place_market_order(meta(), Side.SELL, D("0.02"), client_order_id=CID, reduce_only=True))
    assert rec.body() == {"symbol": "BTCUSDT", "productType": "USDT-FUTURES", "marginMode": "isolated",
                          "marginCoin": "USDT", "size": "0.02", "side": "sell", "orderType": "market",
                          "clientOid": CID, "reduceOnly": "YES"}
    rec = Recorder({"code": "00000", "data": {"fillList": [
        {"tradeId": "t1", "orderId": "9", "side": "sell", "baseVolume": "0.02", "price": "60000",
         "feeDetail": [{"totalFee": "-0.72", "feeCoin": "USDT"}], "cTime": "1790000000000"}], "endId": "t1"}})
    f = go(client(BitgetPrivate, rec).fills(meta(), order_id="9"))[0]
    assert f.fee == D("0.72") and f.quantity == D("0.02")  # a negative totalFee is a fee paid
    with pytest.raises(OrderNotFound):
        go(client(BitgetPrivate, Recorder({"code": "40109", "msg": "not found"})).get_order(meta(), CID))
    with pytest.raises(OrderRejected):
        go(client(BitgetPrivate, Recorder({"code": "45034", "msg": "clientOid duplicate"})).place_market_order(
            meta(), Side.BUY, D("0.01"), client_order_id=CID, reduce_only=False))


def test_bitget_readiness_reports_a_unified_account_plainly():
    rec = Recorder({"code": "00000", "data": {"serverTime": "1790000000000"}}, (401, {"code": "40014", "msg": "perm"}))
    r = go(client(BitgetPrivate, rec).readiness())
    assert not r.ok and "Classic" in r.checks[-1].detail


# ---- MEXC -----------------------------------------------------------------------------------------

def test_mexc_signature_follows_the_documented_recipe(monkeypatch):
    c = MexcPrivate(CREDS)
    monkeypatch.setattr(c, "now_ms", lambda: 1611111111111)
    headers, _ = c._sign("GET", "/api/v1/private/order/list/open_orders", "page_num=1&page_size=100", "")
    assert headers["Signature"] == hmac.new(b"the-secret", b"the-key1611111111111page_num=1&page_size=100",
                                            hashlib.sha256).hexdigest()
    headers, _ = c._sign("POST", "/api/v1/private/order/create", "", '{"symbol":"BTC_USDT"}')
    assert headers["Signature"] == hmac.new(b"the-secret", b'the-key1611111111111{"symbol":"BTC_USDT"}',
                                            hashlib.sha256).hexdigest()


def test_mexc_orders_are_in_contracts_with_one_way_side_codes():
    m = meta("BTC_USDT", multiplier="0.0001", step="0.0001")
    rec = Recorder({"success": True, "code": 0, "data": {"orderId": "777", "ts": 1}})
    c = client(MexcPrivate, rec)
    c._leverage["BTC_USDT"] = 4
    assert go(c.place_market_order(m, Side.SELL, D("0.0123"), client_order_id=CID, reduce_only=False)) == "777"
    assert rec.body() == {"symbol": "BTC_USDT", "vol": 123, "side": 3, "type": 5, "openType": 1, "externalOid": CID,
                          "positionMode": 2, "leverage": 4}
    go(c.place_market_order(m, Side.BUY, D("0.0123"), client_order_id=CID, reduce_only=True))
    assert rec.body()["side"] == 2 and rec.body()["reduceOnly"] is True and "leverage" not in rec.body()
    rec = Recorder({"success": True, "code": 0, "data": {"orderId": "777", "symbol": "BTC_USDT", "externalOid": CID,
                                                         "side": 3, "state": 3, "vol": 123, "dealVol": 123,
                                                         "dealAvgPrice": 60000}})
    o = go(client(MexcPrivate, rec).get_order(m, CID))
    assert o.status is OrderStatus.FILLED and o.filled_quantity == D("0.0123") and o.side is Side.SELL
    assert rec.requests[0].url.path == f"/api/v1/private/order/external/BTC_USDT/{CID}"
    with pytest.raises(OrderNotFound):
        go(client(MexcPrivate, Recorder({"success": False, "code": 2040, "message": "no"})).get_order(m, CID))


def test_mexc_position_is_converted_to_base_units():
    m = meta("BTC_USDT", multiplier="0.0001", step="0.0001")
    rec = Recorder({"success": True, "code": 0, "data": [{"symbol": "BTC_USDT", "state": 1, "holdVol": 5,
                                                         "positionType": 2, "holdAvgPrice": 109777.5,
                                                         "liquidatePrice": 160000, "leverage": 4, "openType": 1}]})
    p = go(client(MexcPrivate, rec).position(m))
    assert p.quantity == D("-0.0005") and p.margin_mode == "isolated"


# ---- BloFin ---------------------------------------------------------------------------------------

def test_blofin_signature_is_base64_of_the_hex_digest(monkeypatch):
    import uuid as uuid_mod
    c = BlofinPrivate(CREDS)
    monkeypatch.setattr(c, "now_ms", lambda: 1597026383085)
    monkeypatch.setattr(uuid_mod, "uuid4", lambda: type("U", (), {"hex": "nonce123"})())
    headers, _ = c._sign("GET", "/api/v1/account/positions", "instId=BTC-USDT", "")
    prehash = b"/api/v1/account/positions?instId=BTC-USDTGET1597026383085nonce123"
    hex_digest = hmac.new(b"the-secret", prehash, hashlib.sha256).hexdigest()
    assert headers["ACCESS-SIGN"] == base64.b64encode(hex_digest.encode()).decode()
    assert headers["ACCESS-NONCE"] == "nonce123"


def test_blofin_order_in_contracts_with_a_checked_per_order_code():
    m = meta("BTC-USDT", multiplier="0.001", step="0.001")
    rec = Recorder({"code": "0", "data": [{"orderId": "55", "clientOrderId": "x", "code": "0", "msg": ""}]})
    c = client(BlofinPrivate, rec)
    assert go(c.place_market_order(m, Side.BUY, D("0.015"), client_order_id=CID, reduce_only=False)) == "55"
    assert rec.body() == {"instId": "BTC-USDT", "marginMode": "isolated", "positionSide": "net", "side": "buy",
                          "orderType": "market", "size": "15", "reduceOnly": "false",
                          "clientOrderId": "arb0007s0003La01"}
    rec = Recorder({"code": "0", "data": [{"orderId": "", "code": "103003", "msg": "Insufficient USDT margin"}]})
    with pytest.raises(OrderRejected):
        go(client(BlofinPrivate, rec).place_market_order(m, Side.BUY, D("0.015"), client_order_id=CID,
                                                         reduce_only=False))


def test_blofin_finished_orders_are_found_in_history():
    m = meta("BTC-USDT", multiplier="0.001", step="0.001")
    rec = Recorder({"code": "152002", "msg": "Parameter error"},
                   {"code": "0", "data": [{"orderId": "55", "clientOrderId": "arb0007s0003La01", "side": "buy",
                                           "state": "filled", "size": "15", "filledSize": "15",
                                           "averagePrice": "60000", "reduceOnly": "false"}]})
    o = go(client(BlofinPrivate, rec).get_order(m, CID))
    assert o.status is OrderStatus.FILLED and o.filled_quantity == D("0.015")
    assert [r.url.path for r in rec.requests] == ["/api/v1/trade/order-detail", "/api/v1/trade/orders-history"]
