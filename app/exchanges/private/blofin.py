"""BloFin perpetual swaps, signed half: USDT-margined, one-way ("net") mode, isolated margin, MARKET orders.

Verified against the official docs (docs.blofin.com, 2026-10-02):
- Auth headers ACCESS-KEY, ACCESS-SIGN, ACCESS-TIMESTAMP (ms), ACCESS-NONCE (unique, e.g. a UUID), ACCESS-PASSPHRASE.
  prehash = requestPath (with its query string) + METHOD + timestamp + nonce + body; the signature is the HEX digest
  of HMAC-SHA256, taken as a UTF-8 string, then Base64 ("string2bytes, not hex2bytes").
- Envelope {code "0", msg, data}; a placed order also carries data[0].code. 152409/401 signature, 152401/152402 key,
  152404 permission, 152406 IP, 152408 passphrase, 152405/152410 timestamp, 152407 nonce, 102002 duplicate client id,
  103003 insufficient margin, 102022 reduce-only without position, 429 rate limit.
- POST /api/v1/trade/order: instId, marginMode isolated, positionSide net, side buy|sell, orderType market,
  size (CONTRACTS = base / contractValue), reduceOnly "true"|"false", clientOrderId (letters and digits only, <= 32:
  the bot's hyphens are removed).
- GET /api/v1/trade/order-detail (instId + clientOrderId; whether finished orders are returned is not documented, so
  orders-history is searched as well), GET /api/v1/trade/fills-history (by orderId; no client id), GET
  /api/v1/account/positions (net mode: signed "positions"), GET /api/v1/account/balance (productType USDT-FUTURES),
  POST /api/v1/account/set-leverage, GET margin-mode / position-mode (account-wide: checked, never changed), GET
  /api/v1/account/funding-fees (7-day window; positive = received), POST /api/v1/trade/cancel-order (needs orderId),
  GET /api/v1/user/query-apikey (readOnly 0 = read-write).
- The account fee rate is not published by the API: fees use the configured standard tier (labelled unverified).
Cross-checked against the ccxt library (2026-10-02), which trades this API live: the same signing (Base64 of the hex
digest), order size in contracts, positions in contracts (signed in net mode, times contractValue), fees positive when
paid and charged in the settlement coin. The live leg still checks the venue's position against its own fills after
every order.
"""

import base64
import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.core.trading_config import PAPER_FEES_BPS
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
    to_contracts,
)
from app.exchanges.private.credentials import VenueCredentials

D = Decimal
AUTH = frozenset({"401", "152401", "152402", "152404", "152406", "152408", "152409", "152411"})
REQUEST = frozenset({"429", "152405", "152407", "152410"})
STATUS = {"live": OrderStatus.NEW, "partially_filled": OrderStatus.PARTIALLY_FILLED, "filled": OrderStatus.FILLED,
          "canceled": OrderStatus.CANCELED, "partially_canceled": OrderStatus.CANCELED}
FUNDING_WINDOW = timedelta(days=7) - timedelta(minutes=5)


def _d(value) -> Decimal:
    return D(str(value)) if value not in (None, "") else D(0)


def venue_client_id(client_order_id: str) -> str:
    """BloFin accepts letters and digits only (<= 32). The bot's ids differ in fixed-width parts, so they stay unique."""
    vid = "".join(ch for ch in client_order_id if ch.isalnum())
    if len(vid) > 32:
        raise OrderRejected(f"blofin_swap: client order id {client_order_id!r} is longer than 32 characters")
    return vid


class BlofinPrivate(PrivateClient):
    name = "blofin_swap"

    def __init__(self, creds: VenueCredentials, *, base_url: str = "https://openapi.blofin.com", transport=None):
        self._key, self._secret, self._passphrase = creds.key, creds.secret.encode(), creds.passphrase or ""
        self._http = SignedHttp(self.name, base_url, self._sign, transport=transport)
        self._order_ids: dict[str, str] = {}  # venue client id -> order id (fills and cancel need the order id)

    def _sign(self, method: str, path: str, query: str, body: str) -> tuple[dict[str, str], str]:
        ts, nonce = str(self.now_ms()), uuid.uuid4().hex
        request_path = path + (f"?{query}" if query else "")
        prehash = request_path + method.upper() + ts + nonce + body
        hex_digest = hmac.new(self._secret, prehash.encode(), hashlib.sha256).hexdigest()
        signature = base64.b64encode(hex_digest.encode()).decode()
        return {"ACCESS-KEY": self._key, "ACCESS-SIGN": signature, "ACCESS-TIMESTAMP": ts, "ACCESS-NONCE": nonce,
                "ACCESS-PASSPHRASE": self._passphrase}, query

    async def _call(self, method: str, path: str, params: dict[str, Any] | None = None, *, body: dict | None = None,
                    order: bool = False, lookup: bool = False) -> Any:
        resp = await self._http.request(method, path, params=params, body=body, order=order)
        data = json_body(self.name, resp, f"{method} {path}")
        code = str(data.get("code")) if isinstance(data, dict) else None
        if code is None:
            raise ExchangeRequestError(f"{self.name}: unexpected response from {method} {path} (HTTP {resp.status_code})")
        if code != "0":
            message = f"{self.name}: {method} {path} -> {code} {str(data.get('msg', ''))[:160]}"
            if code in AUTH:
                raise AuthError(message)
            if code in REQUEST:
                raise ExchangeRequestError(message)
            if lookup:
                raise OrderNotFound(message)  # the "order does not exist" code for queries is not documented
            raise OrderRejected(message) if order else ExchangeError(message)
        return data.get("data")

    async def close(self) -> None:
        await self._http.aclose()

    # ---- checks ---------------------------------------------------------------------------------------
    async def readiness(self) -> Readiness:
        r = Readiness(self.name)
        try:
            key = await self._call("GET", "/api/v1/user/query-apikey") or {}
            r.add("key works", True, "signed request accepted")
            r.add("read-write key", str(key.get("readOnly")) == "0", f"readOnly={key.get('readOnly')}")
            r.add("IP bound", bool(key.get("ips")), "bound to an IP" if key.get("ips") else
                  "not bound to an IP (keys without an IP expire after 90 days)")
            pos_mode = (await self._call("GET", "/api/v1/account/position-mode") or {}).get("positionMode")
            r.add("one-way mode", pos_mode == "net_mode",
                  "net mode" if pos_mode == "net_mode" else f"{pos_mode}: switch to one-way (net) mode")
            margin = (await self._call("GET", "/api/v1/account/margin-mode") or {}).get("marginMode")
            r.add("isolated margin", margin == "isolated",
                  "isolated" if margin == "isolated" else f"{margin}: switch futures margin mode to isolated")
            account = await self.account()
            r.add("USDT balance", account.available_usd > 0, f"available {account.available_usd:.2f} USDT")
        except AuthError as exc:
            r.add("key works", False, f"{exc} (the key needs TRADE permission, the right passphrase and this IP)")
        except ExchangeError as exc:
            r.add("reachable", False, str(exc))
        return r

    # ---- account ----------------------------------------------------------------------------------------
    async def account(self) -> AccountSnapshot:
        data = await self._call("GET", "/api/v1/account/balance", {"productType": "USDT-FUTURES"}) or {}
        usdt = next((d for d in data.get("details") or [] if d.get("currency") == "USDT"), None)
        if usdt is None:
            return AccountSnapshot(D(0), D(0), None)
        return AccountSnapshot(_d(usdt.get("equity")), _d(usdt.get("available")), None)

    async def position(self, meta: InstrumentMeta) -> PositionState | None:
        rows = await self._call("GET", "/api/v1/account/positions", {"instId": meta.symbol}) or []
        qty, row = D(0), None
        for p in rows:
            if p.get("instId") != meta.symbol:
                continue
            if p.get("positionSide") not in (None, "", "net"):
                raise ExchangeError(f"{self.name}: {meta.symbol} has a long/short-mode position; the bot needs net mode")
            size = _d(p.get("positions")) * meta.contract_multiplier
            if size:
                qty += size
                row = p
        if row is None or qty == 0:
            return None
        liq = _d(row.get("liquidationPrice"))
        return PositionState(venue=self.name, symbol=meta.symbol, quantity=qty,
                             entry_price=_d(row.get("averagePrice")) or None, mark_price=_d(row.get("markPrice")) or None,
                             liquidation_price=liq if liq > 0 else None, leverage=_d(row.get("leverage")) or None,
                             margin_mode=row.get("marginMode"), received_at=datetime.now(UTC))

    async def open_orders(self, meta: InstrumentMeta) -> list[OrderState]:
        rows = await self._call("GET", "/api/v1/trade/orders-pending", {"instId": meta.symbol, "limit": 100}) or []
        return [self._order(o, meta) for o in rows]

    async def prepare_symbol(self, meta: InstrumentMeta, leverage: Decimal) -> None:
        await self._call("POST", "/api/v1/account/set-leverage",
                         body={"instId": meta.symbol, "leverage": str(int(leverage)), "marginMode": "isolated"},
                         order=True)

    # ---- orders -------------------------------------------------------------------------------------------
    async def place_market_order(self, meta: InstrumentMeta, side: Side, quantity: Decimal, *, client_order_id: str,
                                 reduce_only: bool) -> str:
        vid = venue_client_id(client_order_id)
        body = {"instId": meta.symbol, "marginMode": "isolated", "positionSide": "net",
                "side": "buy" if side is Side.BUY else "sell", "orderType": "market",
                "size": plain(to_contracts(meta, quantity)), "reduceOnly": "true" if reduce_only else "false",
                "clientOrderId": vid}
        data = await self._call("POST", "/api/v1/trade/order", body=body, order=True)
        item = (data or [{}])[0] if isinstance(data, list) else (data or {})
        if str(item.get("code", "0")) != "0":
            raise OrderRejected(f"{self.name}: order refused: {item.get('code')} {str(item.get('msg', ''))[:160]}")
        order_id = str(item.get("orderId") or "")
        if order_id:
            self._order_ids[vid] = order_id
        return order_id

    def _order(self, o: dict, meta: InstrumentMeta) -> OrderState:
        avg, cv = _d(o.get("averagePrice")), meta.contract_multiplier
        vid = o.get("clientOrderId") or ""
        if o.get("orderId") and vid:
            self._order_ids[vid] = str(o["orderId"])
        return OrderState(venue=self.name, symbol=meta.symbol, order_id=str(o.get("orderId") or ""), client_order_id=vid,
                          side=Side.BUY if o.get("side") == "buy" else Side.SELL,
                          status=STATUS.get(str(o.get("state")), OrderStatus.UNKNOWN),
                          requested_quantity=_d(o.get("size")) * cv, filled_quantity=_d(o.get("filledSize")) * cv,
                          average_fill_price=avg if avg > 0 else None, fee_paid_usd=D(0),
                          reduce_only=str(o.get("reduceOnly")).lower() == "true", received_at=datetime.now(UTC))

    async def get_order(self, meta: InstrumentMeta, client_order_id: str) -> OrderState:
        vid = venue_client_id(client_order_id)
        try:
            data = await self._call("GET", "/api/v1/trade/order-detail", {"instId": meta.symbol, "clientOrderId": vid},
                                    lookup=True)
            if data:
                return self._order(data[0] if isinstance(data, list) else data, meta)
        except OrderNotFound:
            pass
        # finished orders: search the recent history (it has no client id filter)
        rows = await self._call("GET", "/api/v1/trade/orders-history", {"instId": meta.symbol, "limit": 100}) or []
        for o in rows:
            if o.get("clientOrderId") == vid:
                return self._order(o, meta)
        raise OrderNotFound(f"{self.name}: no order {client_order_id} in open orders or the last 100 finished orders")

    async def fills(self, meta: InstrumentMeta, *, order_id: str | None = None,
                    since: datetime | None = None) -> list[PrivateFill]:
        params: dict[str, Any] = {"instId": meta.symbol, "limit": 100}
        if order_id:
            params["orderId"] = order_id
        elif since is not None:
            params["begin"] = int(since.timestamp() * 1000)
        out, seen = [], set()
        for _ in range(20):  # pages, newest first, by tradeId
            rows = await self._call("GET", "/api/v1/trade/fills-history", params) or []
            for f in rows:
                if f["tradeId"] in seen:
                    continue
                seen.add(f["tradeId"])
                out.append(PrivateFill(str(f["tradeId"]), str(f["orderId"]), None, meta.symbol,
                                       Side.BUY if f.get("side") == "buy" else Side.SELL,
                                       _d(f["fillSize"]) * meta.contract_multiplier, _d(f["fillPrice"]),
                                       abs(_d(f.get("fee"))), "USDT", ms_to_dt(f["ts"])))
            if len(rows) < 100:
                break
            params = {**params, "after": rows[-1]["tradeId"]}
        return out

    async def cancel_order(self, meta: InstrumentMeta, client_order_id: str) -> None:
        vid = venue_client_id(client_order_id)
        order_id = self._order_ids.get(vid) or (await self.get_order(meta, client_order_id)).order_id
        try:
            await self._call("POST", "/api/v1/trade/cancel-order",
                             body={"instId": meta.symbol, "orderId": order_id, "clientOrderId": vid}, order=True)
        except OrderRejected:
            return  # already filled or canceled (102068)

    async def fee_schedule(self, meta: InstrumentMeta) -> FeeSchedule:
        taker, maker = PAPER_FEES_BPS["blofin_swap"]
        return FeeSchedule(venue=self.name, taker_fraction=taker / 10_000, maker_fraction=maker / 10_000, verified=False)

    async def funding_income(self, meta: InstrumentMeta, since: datetime) -> list[FundingIncome]:
        now = datetime.now(UTC)
        begin = max(since, now - FUNDING_WINDOW)
        rows = await self._call("GET", "/api/v1/account/funding-fees",
                                {"instId": meta.symbol, "begin": int(begin.timestamp() * 1000),
                                 "end": int(now.timestamp() * 1000), "limit": 1000}) or []
        return [FundingIncome(ms_to_dt(r["fundingTime"]), meta.symbol, _d(r.get("fundingFee")), r.get("currency") or "USDT",
                              str(r["billId"])) for r in rows if r.get("instId", meta.symbol) == meta.symbol]
