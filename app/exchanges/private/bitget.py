"""Bitget futures, signed half (Classic account, API v2 "mix"): USDT-M perpetuals, one-way mode, isolated, MARKET orders.

Verified against the official docs (bitget.com/docs/classic/..., 2026-10-02):
- Auth headers ACCESS-KEY, ACCESS-SIGN, ACCESS-TIMESTAMP (ms), ACCESS-PASSPHRASE, locale; ACCESS-SIGN = base64 of
  HMAC-SHA256 over timestamp + METHOD + requestPath + ("?" + queryString if any) + body.
- Envelope {code "00000", msg, data}. 40109/43001/45057 = order not found; 40009 sign; 40006/40012/40014 key /
  passphrase / permission; 40038/40018 IP; 40008/40005 timestamp; 429 rate limit; 45034 duplicate clientOid.
- POST /api/v2/mix/order/place-order: productType USDT-FUTURES, marginMode isolated, marginCoin USDT, size (base coin),
  side buy|sell (one-way mode: no tradeSide), orderType market, clientOid ([0-9a-zA-Z-+_#], < 50), reduceOnly YES|NO.
- GET /api/v2/mix/order/detail (state live|partially_filled|filled|canceled, baseVolume, priceAvg), GET
  /api/v2/mix/order/fills (feeDetail[].totalFee is negative when paid), GET /api/v2/mix/position/single-position,
  GET /api/v2/mix/account/accounts (posMode, assetMode), POST set-margin-mode / set-leverage (per symbol),
  GET /api/v2/common/trade-rate, GET /api/v2/mix/account/bill (businessType contract_settle_fee = funding).
- THE ACCOUNT MUST BE A CLASSIC ACCOUNT: Bitget's Unified (UTA) keys cannot call the v2 endpoints (notice of
  2026-06-17). readiness() reports that plainly; a Classic-mode sub-account is Bitget's own suggestion.
- Position mode applies to the whole product type, so the bot only checks it (one_way_mode) and never changes it.
"""

import base64
import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.domain.market import (
    FeeSchedule,
    InstrumentMeta,
    OrderState,
    OrderStatus,
    PositionState,
    Side,
)
from app.exchanges._common import ms_to_dt
from app.exchanges.base import ExchangeError, ExchangeRequestError
from app.exchanges.private.base import (
    AccountSnapshot,
    AuthError,
    FundingIncome,
    OrderNotFound,
    OrderRejected,
    PrivateClient,
    PrivateFill,
    Readiness,
    SignedHttp,
    json_body,
    plain,
)
from app.exchanges.private.credentials import VenueCredentials

D = Decimal
PRODUCT = "USDT-FUTURES"
NOT_FOUND = frozenset({"40109", "43001", "45057"})
AUTH = frozenset({"40006", "40009", "40011", "40012", "40014", "40018", "40037", "40038"})
REQUEST = frozenset({"40005", "40008", "40010", "429", "45001", "40725", "40808", "40015"})
STATUS = {"live": OrderStatus.NEW, "new": OrderStatus.NEW, "init": OrderStatus.NEW,
          "partially_filled": OrderStatus.PARTIALLY_FILLED, "filled": OrderStatus.FILLED,
          "canceled": OrderStatus.CANCELED, "cancelled": OrderStatus.CANCELED}
FILL_WINDOW = timedelta(days=89)
BILL_WINDOW = timedelta(days=29)


def _d(value) -> Decimal:
    return D(str(value)) if value not in (None, "") else D(0)


class BitgetPrivate(PrivateClient):
    name = "bitget_usdt"

    def __init__(self, creds: VenueCredentials, *, base_url: str = "https://api.bitget.com", transport=None):
        self._key, self._secret, self._passphrase = creds.key, creds.secret.encode(), creds.passphrase or ""
        self._http = SignedHttp(self.name, base_url, self._sign, transport=transport)

    def _sign(self, method: str, path: str, query: str, body: str) -> tuple[dict[str, str], str]:
        ts = str(self.now_ms())
        prehash = ts + method.upper() + path + (f"?{query}" if query else "") + body
        signature = base64.b64encode(hmac.new(self._secret, prehash.encode(), hashlib.sha256).digest()).decode()
        return {"ACCESS-KEY": self._key, "ACCESS-SIGN": signature, "ACCESS-TIMESTAMP": ts,
                "ACCESS-PASSPHRASE": self._passphrase, "locale": "en-US"}, query

    async def _call(self, method: str, path: str, params: dict[str, Any] | None = None, *, body: dict | None = None,
                    order: bool = False, lookup: bool = False) -> Any:
        resp = await self._http.request(method, path, params=params, body=body, order=order)
        data = json_body(self.name, resp, f"{method} {path}")
        code = str(data.get("code")) if isinstance(data, dict) else None
        if code is None:
            raise ExchangeRequestError(f"{self.name}: unexpected response from {method} {path} (HTTP {resp.status_code})")
        if code != "00000":
            message = f"{self.name}: {method} {path} -> {code} {str(data.get('msg', ''))[:160]}"
            if lookup and code in NOT_FOUND:
                raise OrderNotFound(message)
            if code in AUTH or resp.status_code in (401, 403):
                raise AuthError(message)
            if code in REQUEST:
                raise ExchangeRequestError(message)
            raise OrderRejected(message) if order else ExchangeError(message)
        return data.get("data")

    async def close(self) -> None:
        await self._http.aclose()

    # ---- checks ---------------------------------------------------------------------------------------
    async def readiness(self) -> Readiness:
        r = Readiness(self.name)
        try:
            before = int(datetime.now(UTC).timestamp() * 1000)
            server = await self._call("GET", "/api/v2/public/time")
            after = int(datetime.now(UTC).timestamp() * 1000)
            self.offset_ms = int(server["serverTime"]) - (before + after) // 2
            r.add("clock", abs(self.offset_ms) < 2000, f"venue clock offset {self.offset_ms} ms (corrected)")
            rows = await self._call("GET", "/api/v2/mix/account/accounts", {"productType": PRODUCT})
            r.add("key works (Classic account)", True, "signed v2 request accepted")
            usdt = next((a for a in rows or [] if a.get("marginCoin") == "USDT"), None)
            if usdt is None:
                r.add("USDT futures account", False, "no USDT-M futures account found")
                return r
            pos_mode = usdt.get("posMode")
            r.add("one-way mode", pos_mode == "one_way_mode",
                  "one-way" if pos_mode == "one_way_mode" else f"{pos_mode}: switch USDT-M futures to one-way mode")
            asset_mode = usdt.get("assetMode")
            r.add("single-asset mode", asset_mode in (None, "", "single"),
                  f"assetMode={asset_mode} (isolated margin needs single-asset mode)")
            r.add("USDT balance", _d(usdt.get("available")) > 0, f"available {_d(usdt.get('available')):.2f} USDT")
        except AuthError as exc:
            r.add("key works (Classic account)", False,
                  f"{exc} (a Unified/UTA account cannot use the Classic API: use a Classic-mode sub-account; the key needs "
                  "futures read-write and this IP)")
        except ExchangeError as exc:
            r.add("reachable", False, str(exc))
        return r

    # ---- account ----------------------------------------------------------------------------------------
    async def account(self) -> AccountSnapshot:
        rows = await self._call("GET", "/api/v2/mix/account/accounts", {"productType": PRODUCT})
        usdt = next((a for a in rows or [] if a.get("marginCoin") == "USDT"), None)
        if usdt is None:
            return AccountSnapshot(D(0), D(0), None)
        mm = usdt.get("unionMm")
        return AccountSnapshot(_d(usdt.get("accountEquity")), _d(usdt.get("available")),
                               _d(mm) if mm not in (None, "") else None)

    async def position(self, meta: InstrumentMeta) -> PositionState | None:
        rows = await self._call("GET", "/api/v2/mix/position/single-position",
                                {"productType": PRODUCT, "symbol": meta.symbol, "marginCoin": "USDT"})
        qty, row = D(0), None
        for p in rows or []:
            if p.get("symbol", meta.symbol).upper() != meta.symbol.upper():
                continue
            size = _d(p.get("total"))
            if size:
                qty += size if p.get("holdSide") == "long" else -size
                row = p
        if row is None or qty == 0:
            return None
        liq = _d(row.get("liquidationPrice"))
        return PositionState(venue=self.name, symbol=meta.symbol, quantity=qty,
                             entry_price=_d(row.get("openPriceAvg")) or None, mark_price=_d(row.get("markPrice")) or None,
                             liquidation_price=liq if liq > 0 else None, leverage=_d(row.get("leverage")) or None,
                             margin_mode=row.get("marginMode"), received_at=datetime.now(UTC))

    async def open_orders(self, meta: InstrumentMeta) -> list[OrderState]:
        data = await self._call("GET", "/api/v2/mix/order/orders-pending", {"productType": PRODUCT, "symbol": meta.symbol})
        return [self._order(o, meta) for o in (data or {}).get("entrustedList") or []]

    async def prepare_symbol(self, meta: InstrumentMeta, leverage: Decimal) -> None:
        common = {"symbol": meta.symbol, "productType": PRODUCT, "marginCoin": "USDT"}
        current = await self._call("GET", "/api/v2/mix/account/account", common) or {}
        if current.get("marginMode") != "isolated":  # the reply to an unnecessary change is not documented
            await self._call("POST", "/api/v2/mix/account/set-margin-mode", body={**common, "marginMode": "isolated"},
                             order=True)
        await self._call("POST", "/api/v2/mix/account/set-leverage", body={**common, "leverage": str(int(leverage))},
                         order=True)

    # ---- orders -------------------------------------------------------------------------------------------
    async def place_market_order(self, meta: InstrumentMeta, side: Side, quantity: Decimal, *, client_order_id: str,
                                 reduce_only: bool) -> str:
        body = {"symbol": meta.symbol, "productType": PRODUCT, "marginMode": "isolated", "marginCoin": "USDT",
                "size": plain(quantity), "side": "buy" if side is Side.BUY else "sell", "orderType": "market",
                "clientOid": client_order_id, "reduceOnly": "YES" if reduce_only else "NO"}
        data = await self._call("POST", "/api/v2/mix/order/place-order", body=body, order=True)
        return str((data or {}).get("orderId") or "")  # may be empty for a reduce-only order: read back by clientOid

    def _order(self, o: dict, meta: InstrumentMeta) -> OrderState:
        avg = _d(o.get("priceAvg"))
        return OrderState(venue=self.name, symbol=meta.symbol, order_id=str(o.get("orderId") or ""),
                          client_order_id=o.get("clientOid") or "", side=Side.BUY if o.get("side") == "buy" else Side.SELL,
                          status=STATUS.get(str(o.get("state") or o.get("status")), OrderStatus.UNKNOWN),
                          requested_quantity=_d(o.get("size")), filled_quantity=_d(o.get("baseVolume")),
                          average_fill_price=avg if avg > 0 else None, fee_paid_usd=D(0),
                          reduce_only=o.get("reduceOnly") == "YES", received_at=datetime.now(UTC))

    async def get_order(self, meta: InstrumentMeta, client_order_id: str) -> OrderState:
        data = await self._call("GET", "/api/v2/mix/order/detail",
                                {"symbol": meta.symbol, "productType": PRODUCT, "clientOid": client_order_id}, lookup=True)
        if not data:
            raise OrderNotFound(f"{self.name}: no order {client_order_id}")
        return self._order(data, meta)

    async def fills(self, meta: InstrumentMeta, *, order_id: str | None = None,
                    since: datetime | None = None) -> list[PrivateFill]:
        params: dict[str, Any] = {"productType": PRODUCT, "symbol": meta.symbol, "limit": 100}
        if order_id:
            params["orderId"] = order_id
        elif since is not None:
            params["startTime"] = int(max(since, datetime.now(UTC) - FILL_WINDOW).timestamp() * 1000)
        out = []
        for _ in range(20):  # pages, newest first
            data = await self._call("GET", "/api/v2/mix/order/fills", params) or {}
            rows = data.get("fillList") or []
            for f in rows:
                detail = (f.get("feeDetail") or [{}])[0]
                total = _d(detail.get("totalFee"))
                out.append(PrivateFill(str(f["tradeId"]), str(f["orderId"]), None, meta.symbol,
                                       Side.BUY if f.get("side") == "buy" else Side.SELL, _d(f["baseVolume"]),
                                       _d(f["price"]), -total, detail.get("feeCoin") or "USDT", ms_to_dt(f["cTime"])))
            end_id = data.get("endId")
            if len(rows) < 100 or not end_id:
                break
            params = {**params, "idLessThan": end_id}
        return out

    async def cancel_order(self, meta: InstrumentMeta, client_order_id: str) -> None:
        try:
            await self._call("POST", "/api/v2/mix/order/cancel-order",
                             body={"symbol": meta.symbol, "productType": PRODUCT, "marginCoin": "USDT",
                                   "clientOid": client_order_id}, order=True, lookup=True)
        except OrderNotFound:
            return

    async def fee_schedule(self, meta: InstrumentMeta) -> FeeSchedule:
        r = await self._call("GET", "/api/v2/common/trade-rate", {"symbol": meta.symbol, "businessType": "mix"})
        return FeeSchedule(venue=self.name, taker_fraction=_d(r.get("takerFeeRate")),
                           maker_fraction=_d(r.get("makerFeeRate")), verified=True)

    async def funding_income(self, meta: InstrumentMeta, since: datetime) -> list[FundingIncome]:
        start = max(since, datetime.now(UTC) - BILL_WINDOW)
        params: dict[str, Any] = {"productType": PRODUCT, "businessType": "contract_settle_fee", "coin": "USDT",
                                  "startTime": int(start.timestamp() * 1000), "limit": 100}
        out = []
        for _ in range(20):
            data = await self._call("GET", "/api/v2/mix/account/bill", params) or {}
            bills = data.get("bills") or []
            for b in bills:
                if str(b.get("symbol", "")).upper() == meta.symbol.upper():
                    out.append(FundingIncome(ms_to_dt(b["cTime"]), meta.symbol, _d(b.get("amount")), b.get("coin") or "USDT",
                                             str(b["billId"])))
            end_id = data.get("endId")
            if len(bills) < 100 or not end_id:
                break
            params = {**params, "idLessThan": end_id}
        return out
