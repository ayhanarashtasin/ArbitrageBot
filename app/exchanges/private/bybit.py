"""Bybit V5 (Unified Trading Account), signed half: linear USDT perpetuals, one-way mode, MARKET orders.

Verified against the official docs (bybit-exchange.github.io/docs/v5, 2026-10-02):
- Auth headers X-BAPI-API-KEY, X-BAPI-TIMESTAMP (ms), X-BAPI-RECV-WINDOW (5000), X-BAPI-SIGN = lowercase hex
  HMAC-SHA256 of timestamp + key + recv_window + (query string for GET | JSON body for POST); X-BAPI-SIGN-TYPE 2.
- Envelope {retCode, retMsg, result}; retCode 0 = success. 110001 = order does not exist, 110043 = leverage not
  modified, 110025 = position mode not modified, 10002 timestamp, 10006/10018 rate limit, 10004 sign,
  10003/10005/10010 key / permission / IP.
- POST /v5/order/create: category linear, side Buy|Sell, orderType Market, qty (base coin), positionIdx 0 (one-way),
  orderLinkId (<= 36 chars: letters, digits, - and _), reduceOnly. Asynchronous: only orderId comes back, so the order
  is read back. A market order that cannot fill completely ends "Cancelled" WITH an executed quantity.
- GET /v5/order/realtime (recent orders) then /v5/order/history; GET /v5/execution/list (execType Trade or Funding;
  Funding rows: execFee positive = paid); GET /v5/position/list; GET /v5/account/wallet-balance accountType=UNIFIED;
  POST /v5/position/set-leverage; POST /v5/position/switch-mode (per symbol, mode 0 = one-way); GET /v5/account/info
  (marginMode, account-wide on UTA 2.0); GET /v5/account/fee-rate; GET /v5/user/query-api.
- Margin mode is ACCOUNT-wide on UTA 2.0: the bot reads it and never changes it (it would affect other positions).
  ISOLATED_MARGIN and REGULAR_MARGIN (cross) are accepted, PORTFOLIO_MARGIN is not.
- Fees can be charged in MNT (fee deduction setting): the live leg values such fees at the MNT price.
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
RECV_WINDOW = "5000"
AUTH_CODES = frozenset({10003, 10004, 10005, 10007, 10009, 10010, 33004})
REQUEST_CODES = frozenset({-1, 10000, 10002, 10006, 10016, 10018, 10429, 110079})
STATUS = {"New": OrderStatus.NEW, "Untriggered": OrderStatus.NEW, "PartiallyFilled": OrderStatus.PARTIALLY_FILLED,
          "Filled": OrderStatus.FILLED, "Cancelled": OrderStatus.CANCELED, "PartiallyFilledCanceled": OrderStatus.CANCELED,
          "Deactivated": OrderStatus.CANCELED, "Rejected": OrderStatus.REJECTED}
OPEN = frozenset({"New", "Untriggered", "PartiallyFilled"})
WINDOW = timedelta(days=7) - timedelta(minutes=5)  # execution list and history: 7-day windows


def _d(value) -> Decimal:
    return D(str(value)) if value not in (None, "") else D(0)


class BybitPrivate(PrivateClient):
    name = "bybit_linear"

    def __init__(self, creds: VenueCredentials, *, base_url: str = "https://api.bybit.com", transport=None):
        self._key, self._secret = creds.key, creds.secret.encode()
        self._http = SignedHttp(self.name, base_url, self._sign, transport=transport)

    def _sign(self, method: str, path: str, query: str, body: str) -> tuple[dict[str, str], str]:
        ts = str(self.now_ms())
        payload = ts + self._key + RECV_WINDOW + (query if method == "GET" else body)
        signature = hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()
        return {"X-BAPI-API-KEY": self._key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": RECV_WINDOW,
                "X-BAPI-SIGN": signature, "X-BAPI-SIGN-TYPE": "2"}, query

    async def _call(self, method: str, path: str, params: dict[str, Any] | None = None, *, body: dict | None = None,
                    order: bool = False, ok_codes: frozenset[int] = frozenset()) -> Any:
        resp = await self._http.request(method, path, params=params, body=body, order=order)
        if resp.status_code == 403:
            raise ExchangeRequestError(f"{self.name}: HTTP 403 on {method} {path} (IP rate limit or blocked region)")
        data = json_body(self.name, resp, f"{method} {path}")
        code = data.get("retCode") if isinstance(data, dict) else None
        if code is None:
            raise ExchangeRequestError(f"{self.name}: unexpected response from {method} {path} (HTTP {resp.status_code})")
        if code != 0 and code not in ok_codes:
            message = f"{self.name}: {method} {path} -> {code} {str(data.get('retMsg', ''))[:160]}"
            if code == 110001:
                raise OrderNotFound(message)
            if code in AUTH_CODES:
                raise AuthError(message)
            if code in REQUEST_CODES:
                raise ExchangeRequestError(message)
            raise OrderRejected(message) if order else ExchangeError(message)
        return data.get("result") or {}

    async def close(self) -> None:
        await self._http.aclose()

    # ---- checks ---------------------------------------------------------------------------------------
    async def readiness(self) -> Readiness:
        r = Readiness(self.name)
        try:
            before = int(datetime.now(UTC).timestamp() * 1000)
            server = await self._call("GET", "/v5/market/time")
            after = int(datetime.now(UTC).timestamp() * 1000)
            self.offset_ms = int(server["timeNano"]) // 1_000_000 - (before + after) // 2
            r.add("clock", abs(self.offset_ms) < 2000, f"venue clock offset {self.offset_ms} ms (corrected)")
            key = await self._call("GET", "/v5/user/query-api")
            r.add("key works", True, "signed request accepted")
            r.add("read-write key", str(key.get("readOnly")) == "0", f"readOnly={key.get('readOnly')}")
            perms = key.get("permissions") or {}
            trade = bool(perms.get("ContractTrade")) or bool(perms.get("Derivatives"))
            r.add("derivatives trading permission", trade, f"permissions={ {k: v for k, v in perms.items() if v} }")
            r.add("no withdrawal permission", not (perms.get("Wallet") and "Withdraw" in str(perms.get("Wallet"))),
                  "withdrawals must stay disabled on a trading key")
            info = await self._call("GET", "/v5/account/info")
            mode = info.get("marginMode")
            r.add("margin mode", mode in ("ISOLATED_MARGIN", "REGULAR_MARGIN"),
                  f"{mode} (isolated or cross; portfolio margin is not supported)")
            account = await self.account()
            r.add("USDT balance", account.available_usd > 0, f"available {account.available_usd:.2f} USDT")
        except AuthError as exc:
            r.add("key works", False, f"{exc} (Unified Trading key with Contract/Derivatives trade, bound to this IP)")
        except ExchangeError as exc:
            r.add("reachable", False, str(exc))
        return r

    # ---- account ----------------------------------------------------------------------------------------
    async def account(self) -> AccountSnapshot:
        res = await self._call("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": "USDT"})
        rows = res.get("list") or []
        coins = [c for a in rows for c in (a.get("coin") or []) if c.get("coin") == "USDT"]
        if not coins:
            return AccountSnapshot(D(0), D(0), None)
        c = coins[0]
        available = (_d(c.get("walletBalance")) - _d(c.get("totalPositionIM")) - _d(c.get("totalOrderIM"))
                     - _d(c.get("locked")))  # the documented formula (availableToWithdraw is deprecated)
        return AccountSnapshot(_d(c.get("equity")), max(available, D(0)), _d(c.get("totalPositionMM")))

    async def position(self, meta: InstrumentMeta) -> PositionState | None:
        res = await self._call("GET", "/v5/position/list", {"category": "linear", "symbol": meta.symbol})
        qty, row = D(0), None
        for p in res.get("list") or []:
            if p.get("symbol") != meta.symbol:
                continue
            if int(p.get("positionIdx") or 0) != 0:
                raise ExchangeError(f"{self.name}: {meta.symbol} is in hedge mode; the bot needs one-way mode")
            size = _d(p.get("size"))
            if size and p.get("side") in ("Buy", "Sell"):
                qty += size if p["side"] == "Buy" else -size
                row = p
        if row is None or qty == 0:
            return None
        liq = _d(row.get("liqPrice"))
        return PositionState(venue=self.name, symbol=meta.symbol, quantity=qty, entry_price=_d(row.get("avgPrice")) or None,
                             mark_price=_d(row.get("markPrice")) or None, liquidation_price=liq if liq > 0 else None,
                             leverage=_d(row.get("leverage")) or None, margin_mode=None, received_at=datetime.now(UTC))

    async def open_orders(self, meta: InstrumentMeta) -> list[OrderState]:
        res = await self._call("GET", "/v5/order/realtime", {"category": "linear", "symbol": meta.symbol, "openOnly": 0})
        return [self._order(o) for o in res.get("list") or [] if o.get("orderStatus") in OPEN]

    async def prepare_symbol(self, meta: InstrumentMeta, leverage: Decimal) -> None:
        await self._call("POST", "/v5/position/switch-mode", body={"category": "linear", "symbol": meta.symbol,
                                                                   "mode": 0}, order=True, ok_codes=frozenset({110025}))
        lev = plain(D(int(leverage)))
        await self._call("POST", "/v5/position/set-leverage", body={"category": "linear", "symbol": meta.symbol,
                                                                    "buyLeverage": lev, "sellLeverage": lev},
                         order=True, ok_codes=frozenset({110043}))

    # ---- orders -------------------------------------------------------------------------------------------
    async def place_market_order(self, meta: InstrumentMeta, side: Side, quantity: Decimal, *, client_order_id: str,
                                 reduce_only: bool) -> str:
        body = {"category": "linear", "symbol": meta.symbol, "side": "Buy" if side is Side.BUY else "Sell",
                "orderType": "Market", "qty": plain(quantity), "positionIdx": 0, "orderLinkId": client_order_id,
                "reduceOnly": reduce_only}
        res = await self._call("POST", "/v5/order/create", body=body, order=True)
        return str(res["orderId"])

    def _order(self, o: dict) -> OrderState:
        avg = _d(o.get("avgPrice"))
        return OrderState(venue=self.name, symbol=o["symbol"], order_id=str(o["orderId"]),
                          client_order_id=o.get("orderLinkId") or "", side=Side.BUY if o["side"] == "Buy" else Side.SELL,
                          status=STATUS.get(o.get("orderStatus"), OrderStatus.UNKNOWN), requested_quantity=_d(o.get("qty")),
                          filled_quantity=_d(o.get("cumExecQty")), average_fill_price=avg if avg > 0 else None,
                          fee_paid_usd=D(0), reduce_only=bool(o.get("reduceOnly")), received_at=datetime.now(UTC))

    async def get_order(self, meta: InstrumentMeta, client_order_id: str) -> OrderState:
        params = {"category": "linear", "symbol": meta.symbol, "orderLinkId": client_order_id}
        for path in ("/v5/order/realtime", "/v5/order/history"):  # history: after a restart of Bybit's side
            rows = (await self._call("GET", path, params)).get("list") or []
            if rows:
                return self._order(rows[0])
        raise OrderNotFound(f"{self.name}: no order {client_order_id} in recent orders or history")

    async def _executions(self, meta: InstrumentMeta, exec_type: str, *, order_id: str | None = None,
                          since: datetime | None = None) -> list[dict]:
        params: dict[str, Any] = {"category": "linear", "symbol": meta.symbol, "execType": exec_type, "limit": 100}
        if order_id:
            params = {"category": "linear", "orderId": order_id, "limit": 100}
        elif since is not None:
            params["startTime"] = int(max(since, datetime.now(UTC) - WINDOW).timestamp() * 1000)
        out: list[dict] = []
        for _ in range(20):  # cursor pages
            res = await self._call("GET", "/v5/execution/list", params)
            out.extend(res.get("list") or [])
            cursor = res.get("nextPageCursor")
            if not cursor:
                break
            params = {**params, "cursor": cursor}
        return out

    async def fills(self, meta: InstrumentMeta, *, order_id: str | None = None,
                    since: datetime | None = None) -> list[PrivateFill]:
        rows = await self._executions(meta, "Trade", order_id=order_id, since=since)
        return [PrivateFill(str(e["execId"]), str(e["orderId"]), e.get("orderLinkId") or None, e["symbol"],
                            Side.BUY if e["side"] == "Buy" else Side.SELL, _d(e["execQty"]), _d(e["execPrice"]),
                            _d(e.get("execFee")), e.get("feeCurrency") or "USDT", ms_to_dt(e["execTime"]))
                for e in rows if e.get("execType", "Trade") == "Trade" and e.get("symbol") == meta.symbol]

    async def cancel_order(self, meta: InstrumentMeta, client_order_id: str) -> None:
        await self._call("POST", "/v5/order/cancel", body={"category": "linear", "symbol": meta.symbol,
                                                           "orderLinkId": client_order_id},
                         order=True, ok_codes=frozenset({110001}))

    async def fee_schedule(self, meta: InstrumentMeta) -> FeeSchedule:
        res = await self._call("GET", "/v5/account/fee-rate", {"category": "linear", "symbol": meta.symbol})
        row = (res.get("list") or [{}])[0]
        return FeeSchedule(venue=self.name, taker_fraction=_d(row.get("takerFeeRate")),
                           maker_fraction=_d(row.get("makerFeeRate")), verified=bool(row))

    async def funding_income(self, meta: InstrumentMeta, since: datetime) -> list[FundingIncome]:
        rows = await self._executions(meta, "Funding", since=since)
        return [FundingIncome(ms_to_dt(e["execTime"]), e["symbol"], -_d(e.get("execFee")), e.get("feeCurrency") or "USDT",
                              str(e["execId"]))
                for e in rows if e.get("execType") == "Funding" and e.get("symbol") == meta.symbol]
