"""Binance USDⓈ-M futures, signed half: one-way mode, isolated margin, MARKET orders (USDT-margined only).

Verified against the official docs (developers.binance.com/docs/derivatives/usds-margined-futures, 2026-10-02):
- Auth: header X-MBX-APIKEY; HMAC-SHA256 hex of the full query string (incl. recvWindow and timestamp in ms),
  appended as the last parameter `signature`. Every parameter is sent in the query string, also for POST/DELETE.
- POST /fapi/v1/order type=MARKET, quantity (base units, MARKET_LOT_SIZE), newClientOrderId
  (^[\\.A-Z\\:/a-z0-9_-]{1,36}$), reduceOnly "true", positionSide defaults to BOTH in one-way mode. The response has
  no fill price: the order is read back (GET /fapi/v1/order) and its trades (GET /fapi/v1/userTrades).
- GET /fapi/v1/order: status NEW, PARTIALLY_FILLED, FILLED, CANCELED, REJECTED, EXPIRED, EXPIRED_IN_MATCH; -2013 =
  "Order does not exist" (only safe to believe once the request's recvWindow has passed: not_found_grace_seconds).
- GET /fapi/v3/positionRisk (no leverage / marginType in v3), GET /fapi/v3/account (totalMarginBalance,
  availableBalance, totalMaintMargin), GET /fapi/v1/accountConfig (dualSidePosition, multiAssetsMargin, canTrade).
- POST /fapi/v1/marginType ISOLATED (-4046 = already isolated), POST /fapi/v1/leverage.
- GET /fapi/v1/commissionRate, GET /fapi/v1/income?incomeType=FUNDING_FEE (positive income = credited).
- HTTP 503 "Unknown error" / -1007 = execution status UNKNOWN: never treated as a failure (SignedHttp raises
  ExchangeRequestError, and the executor queries the order by client id).
"""

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
RECV_WINDOW_MS = 5000
AUTH_CODES = frozenset({-1002, -1022, -2014, -2015})
REQUEST_CODES = frozenset({-1000, -1001, -1003, -1006, -1007, -1008, -1015, -1021, -5028})  # no definite answer
STATUS = {"NEW": OrderStatus.NEW, "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED, "FILLED": OrderStatus.FILLED,
          "CANCELED": OrderStatus.CANCELED, "EXPIRED": OrderStatus.CANCELED, "EXPIRED_IN_MATCH": OrderStatus.CANCELED,
          "REJECTED": OrderStatus.REJECTED}
MAX_TRADE_WINDOW = timedelta(days=7) - timedelta(minutes=5)  # userTrades and income: at most 7 days per query


class BinancePrivate(PrivateClient):
    name = "binance_usdt_m"
    not_found_grace_seconds = RECV_WINDOW_MS / 1000 + 1

    def __init__(self, creds: VenueCredentials, *, base_url: str = "https://fapi.binance.com", transport=None):
        self._key, self._secret = creds.key, creds.secret.encode()
        self._http = SignedHttp(self.name, base_url, self._sign, transport=transport)

    def _sign(self, method: str, path: str, query: str, body: str) -> tuple[dict[str, str], str]:
        q = (query + "&" if query else "") + f"recvWindow={RECV_WINDOW_MS}&timestamp={self.now_ms()}"
        signature = hmac.new(self._secret, q.encode(), hashlib.sha256).hexdigest()
        return {"X-MBX-APIKEY": self._key}, f"{q}&signature={signature}"

    async def _call(self, method: str, path: str, params: dict[str, Any] | None = None, *, order: bool = False,
                    ok_codes: frozenset[int] = frozenset()) -> Any:
        resp = await self._http.request(method, path, params=params, order=order)
        data = json_body(self.name, resp, f"{method} {path}")
        code = data.get("code") if isinstance(data, dict) else None
        if isinstance(code, int) and code < 0:
            if code in ok_codes:
                return data
            message = f"{self.name}: {method} {path} -> {code} {str(data.get('msg', ''))[:160]}"
            if code == -2013:
                raise OrderNotFound(message)
            if code in AUTH_CODES:
                raise AuthError(message)
            if code in REQUEST_CODES:
                raise ExchangeRequestError(message)
            raise OrderRejected(message) if order else ExchangeError(message)
        if resp.status_code != 200:
            raise ExchangeRequestError(f"{self.name}: HTTP {resp.status_code} on {method} {path}")
        return data

    async def close(self) -> None:
        await self._http.aclose()

    # ---- checks ---------------------------------------------------------------------------------------
    async def readiness(self) -> Readiness:
        r = Readiness(self.name)
        try:
            before = int(datetime.now(UTC).timestamp() * 1000)
            server = (await self._call("GET", "/fapi/v1/time"))["serverTime"]
            after = int(datetime.now(UTC).timestamp() * 1000)
            self.offset_ms = int(server) - (before + after) // 2
            r.add("clock", abs(self.offset_ms) < 2000, f"venue clock offset {self.offset_ms} ms (corrected)")
            cfg = await self._call("GET", "/fapi/v1/accountConfig")
            r.add("key works", True, "signed request accepted")
            r.add("trading allowed", bool(cfg.get("canTrade")), f"canTrade={cfg.get('canTrade')}")
            r.add("one-way mode", cfg.get("dualSidePosition") is False,
                  "one-way" if cfg.get("dualSidePosition") is False else
                  "HEDGE mode is on: switch to One-way (Futures > Preferences > Position Mode) with no open positions")
            r.add("single-asset mode", cfg.get("multiAssetsMargin") is False,
                  "single-asset" if cfg.get("multiAssetsMargin") is False else
                  "Multi-Assets mode is on: isolated margin needs Single-Asset mode")
            account = await self.account()
            r.add("USDT balance", account.available_usd > 0, f"available {account.available_usd:.2f} USDT")
        except AuthError as exc:
            r.add("key works", False, f"{exc} (enable Futures on the key and bind it to this IP)")
        except ExchangeError as exc:
            r.add("reachable", False, str(exc))
        return r

    # ---- account ----------------------------------------------------------------------------------------
    async def account(self) -> AccountSnapshot:
        a = await self._call("GET", "/fapi/v3/account")
        return AccountSnapshot(D(str(a["totalMarginBalance"])), D(str(a["availableBalance"])),
                               D(str(a["totalMaintMargin"])))

    async def position(self, meta: InstrumentMeta) -> PositionState | None:
        rows = await self._call("GET", "/fapi/v3/positionRisk", {"symbol": meta.symbol})
        qty, row = D(0), None
        for p in rows:
            if p.get("symbol") == meta.symbol:
                if p.get("positionSide") not in (None, "BOTH"):
                    raise ExchangeError(f"{self.name}: hedge-mode position on {meta.symbol}; the bot needs one-way mode")
                qty += D(str(p["positionAmt"]))
                row = p
        if row is None or qty == 0:
            return None
        liq = D(str(row.get("liquidationPrice") or 0))
        return PositionState(venue=self.name, symbol=meta.symbol, quantity=qty,
                             entry_price=D(str(row["entryPrice"])) or None, mark_price=D(str(row["markPrice"])) or None,
                             liquidation_price=liq if liq > 0 else None, leverage=None,
                             margin_mode="isolated" if D(str(row.get("isolatedWallet") or 0)) != 0 else "cross",
                             received_at=datetime.now(UTC))

    async def open_orders(self, meta: InstrumentMeta) -> list[OrderState]:
        return [self._order(o) for o in await self._call("GET", "/fapi/v1/openOrders", {"symbol": meta.symbol})]

    async def prepare_symbol(self, meta: InstrumentMeta, leverage: Decimal) -> None:
        await self._call("POST", "/fapi/v1/marginType", {"symbol": meta.symbol, "marginType": "ISOLATED"},
                         order=True, ok_codes=frozenset({-4046}))
        await self._call("POST", "/fapi/v1/leverage", {"symbol": meta.symbol, "leverage": int(leverage)}, order=True)

    # ---- orders -------------------------------------------------------------------------------------------
    async def place_market_order(self, meta: InstrumentMeta, side: Side, quantity: Decimal, *, client_order_id: str,
                                 reduce_only: bool) -> str:
        params = {"symbol": meta.symbol, "side": side.value, "type": "MARKET", "quantity": plain(quantity),
                  "newClientOrderId": client_order_id, "newOrderRespType": "RESULT"}
        if reduce_only:
            params["reduceOnly"] = "true"
        data = await self._call("POST", "/fapi/v1/order", params, order=True)
        return str(data["orderId"])

    def _order(self, o: dict) -> OrderState:
        filled = D(str(o.get("executedQty") or 0))
        avg = D(str(o.get("avgPrice") or 0))
        return OrderState(venue=self.name, symbol=o["symbol"], order_id=str(o["orderId"]),
                          client_order_id=o.get("clientOrderId") or "", side=Side(o["side"]),
                          status=STATUS.get(o["status"], OrderStatus.UNKNOWN), requested_quantity=D(str(o["origQty"])),
                          filled_quantity=filled, average_fill_price=avg if avg > 0 else None, fee_paid_usd=D(0),
                          reduce_only=bool(o.get("reduceOnly")), received_at=datetime.now(UTC))

    async def get_order(self, meta: InstrumentMeta, client_order_id: str) -> OrderState:
        return self._order(await self._call("GET", "/fapi/v1/order",
                                            {"symbol": meta.symbol, "origClientOrderId": client_order_id}))

    async def fills(self, meta: InstrumentMeta, *, order_id: str | None = None,
                    since: datetime | None = None) -> list[PrivateFill]:
        params: dict[str, Any] = {"symbol": meta.symbol, "limit": 1000}
        if order_id:
            params["orderId"] = order_id
        elif since is not None:
            params["startTime"] = int(max(since, datetime.now(UTC) - MAX_TRADE_WINDOW).timestamp() * 1000)
        rows = await self._call("GET", "/fapi/v1/userTrades", params)
        return [PrivateFill(str(t["id"]), str(t["orderId"]), None, t["symbol"], Side(t["side"]), D(str(t["qty"])),
                            D(str(t["price"])), D(str(t["commission"])), t["commissionAsset"], ms_to_dt(t["time"]))
                for t in rows]

    async def cancel_order(self, meta: InstrumentMeta, client_order_id: str) -> None:
        await self._call("DELETE", "/fapi/v1/order", {"symbol": meta.symbol, "origClientOrderId": client_order_id},
                         order=True, ok_codes=frozenset({-2011, -2013}))

    async def fee_schedule(self, meta: InstrumentMeta) -> FeeSchedule:
        r = await self._call("GET", "/fapi/v1/commissionRate", {"symbol": meta.symbol})
        return FeeSchedule(venue=self.name, taker_fraction=D(str(r["takerCommissionRate"])),
                           maker_fraction=D(str(r["makerCommissionRate"])), verified=True)

    async def funding_income(self, meta: InstrumentMeta, since: datetime) -> list[FundingIncome]:
        start = max(since, datetime.now(UTC) - MAX_TRADE_WINDOW)
        rows = await self._call("GET", "/fapi/v1/income", {"symbol": meta.symbol, "incomeType": "FUNDING_FEE",
                                                           "startTime": int(start.timestamp() * 1000), "limit": 1000})
        return [FundingIncome(ms_to_dt(r["time"]), r["symbol"], D(str(r["income"])), r["asset"], str(r["tranId"]))
                for r in rows]
