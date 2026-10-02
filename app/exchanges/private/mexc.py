"""MEXC futures (contract API), signed half: USDT-margined perpetuals, one-way mode, isolated margin, MARKET orders.

Sources (2026-10-02): the current docs at mexc.com/api-docs/futures (order placement reopened 2026-03-31 after the
2022 maintenance; KYC is required for the "Order Placing" permission) and the older reference
mexcdevelop.github.io/apidocs/contract_v1_en for the signing sample.
- Auth headers ApiKey, Request-Time (ms), Signature, Recv-Window (seconds). Signature = lowercase hex HMAC-SHA256 of
  key + timestamp + params, where params = the GET parameters sorted by name and joined with & (no path parameters,
  no nulls), or the raw JSON body for POST.
- Envelope {success, code, data | message}; code 0 = success. 2040 order does not exist, 602 signature, 401/402 key,
  406 IP whitelist, 511/702/704 permission, 200005 KYC, 513 request time, 510/2037 too frequent, 2005 balance.
- POST /api/v1/private/order/create: symbol, vol (CONTRACTS), leverage, side (1 open long, 2 close short,
  3 open short, 4 close long), type 5 (market), openType 1 (isolated), externalOid (<= 32 chars), positionMode 2
  (one-way), reduceOnly (one-way only). In one-way mode: buy = 1, sell = 3, reduce-only buy = 2, reduce-only sell = 4.
  The market-order price field is not documented for type 5 and is not sent (as the ccxt library does).
- GET /api/v1/private/order/external/{symbol}/{externalOid}: state 1 pending, 2 unfilled, 3 filled, 4 canceled,
  5 invalid; dealVol / dealAvgPrice. GET order/deal_details/{orderId}, order/list/order_deals/v3 (fee positive = paid).
- GET position/open_positions (holdVol contracts, positionType 1 long 2 short, state 1 holding), account/asset/USDT,
  position/position_mode (2 = one-way), POST position/change_leverage, account/tiered_fee_rate/v2,
  position/funding_records (funding positive = received), order/list/open_orders, order/cancel_with_external.
Cross-checked against the ccxt library (2026-10-02), which trades this API live: the same signing, no price on market
orders, the same one-way side codes and no reduceOnly flag, order lookups and cancels by venue order id. One deliberate
difference: ccxt sends market orders as type 6 ("convert to the current price", which can rest like a limit order);
the bot uses the documented type 5 (market), so a hedge leg never waits unfilled. The live leg still checks the venue's
position against its own fills after every order.
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
    to_contracts,
)
from app.exchanges.private.credentials import VenueCredentials

D = Decimal
RECV_WINDOW_SECONDS = "20"
AUTH = frozenset({401, 402, 406, 511, 602, 701, 702, 703, 704, 200005})
REQUEST = frozenset({500, 510, 513, 603, 2037})
SIDE_CODE = {(Side.BUY, False): 1, (Side.SELL, False): 3, (Side.BUY, True): 2, (Side.SELL, True): 4}


def _d(value) -> Decimal:
    return D(str(value)) if value not in (None, "") else D(0)


class MexcPrivate(PrivateClient):
    name = "mexc_contract"

    def __init__(self, creds: VenueCredentials, *, base_url: str = "https://api.mexc.com", transport=None):
        self._key, self._secret = creds.key, creds.secret.encode()
        self._http = SignedHttp(self.name, base_url, self._sign, transport=transport)
        self._leverage: dict[str, int] = {}  # symbol -> leverage set by prepare_symbol (orders must repeat it)
        self._order_ids: dict[str, str] = {}  # client order id -> venue order id (lookups and cancels use the id)

    def _sign(self, method: str, path: str, query: str, body: str) -> tuple[dict[str, str], str]:
        ts = str(self.now_ms())
        payload = self._key + ts + (body if method == "POST" else query)
        signature = hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()
        return {"ApiKey": self._key, "Request-Time": ts, "Signature": signature,
                "Recv-Window": RECV_WINDOW_SECONDS}, query

    async def _call(self, method: str, path: str, params: dict[str, Any] | None = None, *, body: Any = None,
                    order: bool = False, lookup: bool = False) -> Any:
        resp = await self._http.request(method, path, params=params, body=body, order=order, sort_params=True)
        data = json_body(self.name, resp, f"{method} {path}")
        if not isinstance(data, dict) or "code" not in data:
            raise ExchangeRequestError(f"{self.name}: unexpected response from {method} {path} (HTTP {resp.status_code})")
        code = int(data["code"])
        if code != 0 or data.get("success") is False:
            message = f"{self.name}: {method} {path} -> {code} {str(data.get('message', ''))[:160]}"
            if lookup and code == 2040:
                raise OrderNotFound(message)
            if code in AUTH:
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
            server = await self._call("GET", "/api/v1/contract/ping")
            after = int(datetime.now(UTC).timestamp() * 1000)
            self.offset_ms = int(server) - (before + after) // 2
            r.add("clock", abs(self.offset_ms) < 2000, f"venue clock offset {self.offset_ms} ms (corrected)")
            mode = await self._call("GET", "/api/v1/private/position/position_mode")
            r.add("key works", True, "signed request accepted")
            r.add("one-way mode", int(mode) == 2,
                  "one-way" if int(mode) == 2 else "HEDGE mode: switch futures to one-way mode with no open positions")
            account = await self.account()
            r.add("USDT balance", account.available_usd > 0, f"available {account.available_usd:.2f} USDT")
            r.add("order permission", True, "not readable through the API: the key needs 'Order Placing', which "
                  "requires KYC")
        except AuthError as exc:
            r.add("key works", False, f"{exc} (KYC, 'Order Placing' permission and this IP are required)")
        except ExchangeError as exc:
            r.add("reachable", False, str(exc))
        return r

    # ---- account ----------------------------------------------------------------------------------------
    async def account(self) -> AccountSnapshot:
        a = await self._call("GET", "/api/v1/private/account/asset/USDT") or {}
        return AccountSnapshot(_d(a.get("equity")), _d(a.get("availableBalance")), None)

    async def position(self, meta: InstrumentMeta) -> PositionState | None:
        rows = await self._call("GET", "/api/v1/private/position/open_positions", {"symbol": meta.symbol}) or []
        qty, row = D(0), None
        for p in rows:
            if p.get("symbol") != meta.symbol or int(p.get("state") or 1) != 1:
                continue
            size = _d(p.get("holdVol")) * meta.contract_multiplier
            if size:
                qty += size if int(p.get("positionType")) == 1 else -size
                row = p
        if row is None or qty == 0:
            return None
        liq = _d(row.get("liquidatePrice"))
        return PositionState(venue=self.name, symbol=meta.symbol, quantity=qty,
                             entry_price=_d(row.get("holdAvgPrice")) or None, mark_price=None,
                             liquidation_price=liq if liq > 0 else None, leverage=_d(row.get("leverage")) or None,
                             margin_mode="isolated" if int(row.get("openType") or 1) == 1 else "cross",
                             received_at=datetime.now(UTC))

    async def open_orders(self, meta: InstrumentMeta) -> list[OrderState]:
        data = await self._call("GET", "/api/v1/private/order/list/open_orders", {"page_num": 1, "page_size": 100})
        rows = data.get("resultList", []) if isinstance(data, dict) else (data or [])
        return [self._order(o, meta) for o in rows if o.get("symbol") == meta.symbol]

    async def prepare_symbol(self, meta: InstrumentMeta, leverage: Decimal) -> None:
        for position_type in (1, 2):  # no position exists yet (preflight): set both directions
            await self._call("POST", "/api/v1/private/position/change_leverage",
                             body={"openType": 1, "leverage": int(leverage), "symbol": meta.symbol,
                                   "positionType": position_type}, order=True)
        self._leverage[meta.symbol] = int(leverage)

    async def _order_leverage(self, meta: InstrumentMeta) -> int:
        """Opening orders must carry the position's leverage (error 2021 otherwise). After a restart prepare_symbol has
        not run, so the open position's own leverage is used."""
        if meta.symbol not in self._leverage:
            pos = await self.position(meta)
            if pos is None or pos.leverage is None:
                raise ExchangeError(f"{self.name}: leverage for {meta.symbol} is unknown; prepare_symbol has not run")
            self._leverage[meta.symbol] = int(pos.leverage)
        return self._leverage[meta.symbol]

    # ---- orders -------------------------------------------------------------------------------------------
    async def place_market_order(self, meta: InstrumentMeta, side: Side, quantity: Decimal, *, client_order_id: str,
                                 reduce_only: bool) -> str:
        contracts = to_contracts(meta, quantity)
        # As ccxt sends it in one-way mode: the closing side codes (2, 4) can only reduce, so no reduceOnly flag.
        body = {"symbol": meta.symbol, "vol": int(contracts), "side": SIDE_CODE[(side, reduce_only)], "type": 5,
                "openType": 1, "externalOid": client_order_id, "positionMode": 2}
        if not reduce_only:
            body["leverage"] = await self._order_leverage(meta)
        data = await self._call("POST", "/api/v1/private/order/create", body=body, order=True)
        order_id = str(data.get("orderId") if isinstance(data, dict) else data)
        self._order_ids[client_order_id] = order_id
        return order_id

    def _order(self, o: dict, meta: InstrumentMeta) -> OrderState:
        state, filled = int(o.get("state") or 0), _d(o.get("dealVol")) * meta.contract_multiplier
        status = {3: OrderStatus.FILLED, 4: OrderStatus.CANCELED, 5: OrderStatus.REJECTED}.get(
            state, OrderStatus.PARTIALLY_FILLED if filled > 0 else OrderStatus.NEW)
        avg = _d(o.get("dealAvgPrice"))
        side = Side.BUY if int(o.get("side") or 1) in (1, 2) else Side.SELL
        return OrderState(venue=self.name, symbol=meta.symbol, order_id=str(o.get("orderId") or ""),
                          client_order_id=o.get("externalOid") or "", side=side, status=status,
                          requested_quantity=_d(o.get("vol")) * meta.contract_multiplier, filled_quantity=filled,
                          average_fill_price=avg if avg > 0 else None, fee_paid_usd=D(0),
                          reduce_only=int(o.get("side") or 1) in (2, 4), received_at=datetime.now(UTC))

    async def get_order(self, meta: InstrumentMeta, client_order_id: str) -> OrderState:
        """By venue order id when it is known (the endpoint ccxt uses), by client id only when it is not (a lost
        reply or a restart): the documented external-id lookup."""
        order_id = self._order_ids.get(client_order_id)
        if order_id:
            data = await self._call("GET", f"/api/v1/private/order/get/{order_id}", lookup=True)
        else:
            data = await self._call("GET", f"/api/v1/private/order/external/{meta.symbol}/{client_order_id}",
                                    lookup=True)
        if not data:
            raise OrderNotFound(f"{self.name}: no order {client_order_id}")
        state = self._order(data, meta)
        if state.order_id:
            self._order_ids[client_order_id] = state.order_id
        return state if state.client_order_id else state.model_copy(update={"client_order_id": client_order_id})

    def _fill(self, f: dict, meta: InstrumentMeta) -> PrivateFill:
        side = Side.BUY if int(f.get("side") or 1) in (1, 2) else Side.SELL
        return PrivateFill(str(f["id"]), str(f.get("orderId") or ""), None, meta.symbol, side,
                           _d(f["vol"]) * meta.contract_multiplier, _d(f["price"]), _d(f.get("fee")),
                           f.get("feeCurrency") or "USDT", ms_to_dt(f["timestamp"]))

    async def fills(self, meta: InstrumentMeta, *, order_id: str | None = None,
                    since: datetime | None = None) -> list[PrivateFill]:
        if order_id:
            rows = await self._call("GET", f"/api/v1/private/order/deal_details/{order_id}") or []
            return [self._fill({**f, "orderId": f.get("orderId") or order_id}, meta) for f in rows]
        params: dict[str, Any] = {"symbol": meta.symbol, "page_num": 1, "page_size": 1000}
        if since is not None:
            params["start_time"] = int(max(since, datetime.now(UTC) - timedelta(days=89)).timestamp() * 1000)
        data = await self._call("GET", "/api/v1/private/order/list/order_deals/v3", params)
        rows = data.get("resultList", []) if isinstance(data, dict) else (data or [])
        return [self._fill(f, meta) for f in rows if f.get("symbol", meta.symbol) == meta.symbol]

    async def cancel_order(self, meta: InstrumentMeta, client_order_id: str) -> None:
        """POST order/cancel with a list of venue order ids (the body shape ccxt uses; the external-id cancel's body
        differs between the old and new docs)."""
        try:
            state = await self.get_order(meta, client_order_id)
        except OrderNotFound:
            return
        if state.status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED) or not state.order_id:
            return
        await self._call("POST", "/api/v1/private/order/cancel", body=[state.order_id], order=True, lookup=True)

    async def fee_schedule(self, meta: InstrumentMeta) -> FeeSchedule:
        r = await self._call("GET", "/api/v1/private/account/tiered_fee_rate/v2", {"symbol": meta.symbol}) or {}
        return FeeSchedule(venue=self.name, taker_fraction=_d(r.get("realTakerFee")),
                           maker_fraction=_d(r.get("realMakerFee")), verified="realTakerFee" in r)

    async def funding_income(self, meta: InstrumentMeta, since: datetime) -> list[FundingIncome]:
        data = await self._call("GET", "/api/v1/private/position/funding_records",
                                {"symbol": meta.symbol, "page_num": 1, "page_size": 100})
        rows = data.get("resultList", []) if isinstance(data, dict) else (data or [])
        return [FundingIncome(ms_to_dt(r["settleTime"]), meta.symbol, _d(r.get("funding")), "USDT", str(r["id"]))
                for r in rows if r.get("symbol", meta.symbol) == meta.symbol and ms_to_dt(r["settleTime"]) >= since]
