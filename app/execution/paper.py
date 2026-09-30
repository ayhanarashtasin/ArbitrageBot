"""PaperVenue: simulated account and order execution on top of REAL market data (project.md section 56).

Market data calls are delegated to a real adapter (or a scripted source in tests). Orders are filled
against the current real order book with slippage, partial fills and fees; funding cash flows are
applied by the engine at the venue's real settlement times. No order ever reaches a real exchange.

Idempotency: placing the same client_order_id twice returns the original order and never fills twice.
The venue can be told to misbehave (reject, time out after filling, fill partially) so the failure
paths in project.md sections 28, 29 and 67 can be exercised.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.core.clock import Clock
from app.domain.market import (
    AccountState,
    Candle,
    FeeSchedule,
    FundingInfo,
    InstrumentMeta,
    OrderBook,
    OrderState,
    OrderStatus,
    PositionState,
    Side,
    Ticker,
)
from app.exchanges.base import ExchangeAdapter, ExchangeError, ExchangeRequestError

D = Decimal
MAINTENANCE_MARGIN_RATE = D("0.005")  # paper assumption used for the simulated liquidation price


class PaperRejected(ExchangeError):
    """The simulated venue rejected the order (nothing was filled)."""


@dataclass
class FillModel:
    """Scripted behaviour for the next orders (per client-order-id or global)."""

    fill_ratio: Decimal = D(1)  # share of the requested quantity that fills (0..1)
    reject_next: int = 0  # reject the next N orders outright
    timeout_after_fill_next: int = 0  # fill, then raise a timeout to the caller (ambiguous outcome)
    timeout_before_fill_next: int = 0  # raise a timeout WITHOUT filling
    liquidity_fraction: Decimal = D(1)  # cap on visible depth an order may consume (0..1)


@dataclass
class _Position:
    quantity: Decimal = D(0)  # signed, base units
    entry_price: Decimal = D(0)
    realized_pnl: Decimal = D(0)


@dataclass
class FundingPayment:
    time: datetime
    symbol: str
    rate: Decimal
    notional: Decimal
    payment: Decimal  # positive = received, negative = paid


@dataclass
class PaperFill:
    client_order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    fee: Decimal
    time: datetime
    reference_price: Decimal  # mid price when the order was submitted (slippage reference)


class PaperVenue(ExchangeAdapter):
    def __init__(
        self,
        source: ExchangeAdapter,
        *,
        clock: Clock,
        initial_equity_usd: Decimal,
        taker_fee: Decimal,
        maker_fee: Decimal = D(0),
        fee_verified: bool = False,
    ):
        self.name = source.name
        self._src = source
        self._clock = clock
        self._balance = initial_equity_usd
        self._taker, self._maker, self._fee_verified = taker_fee, maker_fee, fee_verified
        self._positions: dict[str, _Position] = {}
        self._leverage: dict[str, Decimal] = {}
        self._orders: dict[str, OrderState] = {}
        self.fills: list[PaperFill] = []
        self.funding_payments: list[FundingPayment] = []
        self.model = FillModel()
        self.connected = True  # tests flip this to simulate a disconnect
        self._marks: dict[str, Decimal] = {}

    # ---- market data: delegated to the real source ---------------------------------------------
    def _need_connection(self):
        if not self.connected:
            raise ExchangeRequestError(f"{self.name}: paper venue disconnected")

    async def get_server_time(self):
        return self._clock.now()

    async def list_instruments(self) -> list[InstrumentMeta]:
        self._need_connection()
        return await self._src.list_instruments()

    async def get_funding_info(self, symbol: str) -> FundingInfo:
        self._need_connection()
        return await self._src.get_funding_info(symbol)

    async def get_funding_history(self, symbol: str, limit: int = 100):
        self._need_connection()
        return await self._src.get_funding_history(symbol, limit)

    async def get_ticker(self, symbol: str) -> Ticker:
        self._need_connection()
        return await self._src.get_ticker(symbol)

    async def get_order_book(self, symbol: str, depth: int = 50) -> OrderBook:
        self._need_connection()
        book = await self._src.get_order_book(symbol, depth)
        if book.best_bid and book.best_ask:
            self._marks[symbol] = (book.best_bid + book.best_ask) / 2
        return book

    async def get_candles(self, symbol: str, timeframe: str = "1m", limit: int = 200) -> list[Candle]:
        self._need_connection()
        return await self._src.get_candles(symbol, timeframe, limit)

    async def get_fee_schedule(self, symbol: str) -> FeeSchedule:
        return FeeSchedule(venue=self.name, taker_fraction=self._taker, maker_fraction=self._maker,
                           verified=self._fee_verified)

    # ---- account -------------------------------------------------------------------------------
    def mark_price(self, symbol: str) -> Decimal | None:
        return self._marks.get(symbol)

    def set_mark(self, symbol: str, price: Decimal) -> None:
        self._marks[symbol] = price

    def _unrealized(self) -> Decimal:
        total = D(0)
        for symbol, pos in self._positions.items():
            mark = self._marks.get(symbol)
            if pos.quantity != 0 and mark is not None:
                total += (mark - pos.entry_price) * pos.quantity
        return total

    def _used_margin(self) -> Decimal:
        total = D(0)
        for symbol, pos in self._positions.items():
            mark = self._marks.get(symbol) or pos.entry_price
            lev = self._leverage.get(symbol, D(1))
            total += abs(pos.quantity) * mark / lev
        return total

    def _maintenance_margin(self) -> Decimal:
        total = D(0)
        for symbol, pos in self._positions.items():
            mark = self._marks.get(symbol) or pos.entry_price
            total += abs(pos.quantity) * mark * MAINTENANCE_MARGIN_RATE
        return total

    def equity(self) -> Decimal:
        return self._balance + self._unrealized()

    async def _refresh_marks(self) -> None:
        """Mark open positions to the current market so equity is never one cycle stale."""
        for symbol, pos in self._positions.items():
            if pos.quantity != 0:
                await self.get_order_book(symbol)

    async def get_account_equity(self) -> AccountState:
        self._need_connection()
        await self._refresh_marks()
        eq = self.equity()
        used = self._used_margin()
        # margin_utilization is the venues' MARGIN RATIO: maintenance margin / equity, liquidation at 1 (100 %).
        # Initial-margin use would read 100 % on a true 4x account the moment it is fully entered.
        mm = self._maintenance_margin()
        ratio = (mm / eq) if eq > 0 else (D(1) if mm > 0 else None)
        return AccountState(venue=self.name, equity_usd=eq, available_margin_usd=eq - used,
                            margin_utilization=ratio, received_at=self._clock.now())

    async def get_available_balance(self) -> Decimal:
        return (await self.get_account_equity()).available_margin_usd

    def _position_state(self, symbol: str, pos: _Position) -> PositionState:
        """Liquidation uses cross-margin math on this account: liquidated when equity falls to the
        maintenance margin of the position. (B = cash balance; long: B + (p - e)q = m p q.)"""
        lev = self._leverage.get(symbol, D(1))
        liq = None
        q, e, m = abs(pos.quantity), pos.entry_price, MAINTENANCE_MARGIN_RATE
        if q > 0 and e > 0:
            if pos.quantity > 0:
                liq = max(D(0), (q * e - self._balance) / (q * (1 - m)))
            else:
                liq = (self._balance + e * q) / (q * (1 + m))
        return PositionState(venue=self.name, symbol=symbol, quantity=pos.quantity,
                             entry_price=pos.entry_price or None, mark_price=self._marks.get(symbol),
                             liquidation_price=liq, leverage=lev, margin_mode="cross",
                             received_at=self._clock.now())

    async def get_positions(self) -> list[PositionState]:
        self._need_connection()
        await self._refresh_marks()
        return [self._position_state(s, p) for s, p in self._positions.items() if p.quantity != 0]

    async def get_position(self, symbol: str) -> PositionState | None:
        self._need_connection()
        await self._refresh_marks()
        pos = self._positions.get(symbol)
        return self._position_state(symbol, pos) if pos and pos.quantity != 0 else None

    async def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        self._need_connection()
        self._leverage[symbol] = leverage

    async def get_open_orders(self, symbol: str | None = None) -> list[OrderState]:
        return []  # market IOC orders never rest

    async def get_order(self, order_id: str | None = None, client_order_id: str | None = None) -> OrderState:
        self._need_connection()
        key = client_order_id
        if key is None:
            key = next((k for k, o in self._orders.items() if o.order_id == order_id), None)
        if key is None or key not in self._orders:
            raise ExchangeError(f"{self.name}: unknown order")
        return self._orders[key]

    async def get_fills(self, symbol: str | None = None) -> list[PaperFill]:
        return [f for f in self.fills if symbol is None or f.symbol == symbol]

    # ---- orders --------------------------------------------------------------------------------
    async def place_order(self, symbol: str, side: Side, quantity: Decimal, *, client_order_id: str,
                          price: Decimal | None = None, reduce_only: bool = False) -> OrderState:
        self._need_connection()
        existing = self._orders.get(client_order_id)
        if existing is not None:
            return existing  # idempotent: never fill the same client order twice

        m = self.model
        if m.timeout_before_fill_next > 0:
            m.timeout_before_fill_next -= 1
            raise ExchangeRequestError(f"{self.name}: request timed out")
        if m.reject_next > 0:
            m.reject_next -= 1
            self._orders[client_order_id] = self._state(symbol, client_order_id, side, quantity, D(0), None,
                                                        D(0), reduce_only, OrderStatus.REJECTED)
            raise PaperRejected(f"{self.name}: order rejected")

        pos = self._positions.get(symbol, _Position())
        if reduce_only:
            closing = (side is Side.SELL and pos.quantity > 0) or (side is Side.BUY and pos.quantity < 0)
            if not closing:
                self._orders[client_order_id] = self._state(symbol, client_order_id, side, quantity, D(0), None,
                                                            D(0), True, OrderStatus.REJECTED)
                raise PaperRejected(f"{self.name}: reduce-only order would open exposure")
            quantity = min(quantity, abs(pos.quantity))

        book = await self.get_order_book(symbol)
        levels = book.asks if side is Side.BUY else book.bids
        target = quantity * m.fill_ratio
        remaining, filled, spent = target, D(0), D(0)
        for lv in levels:
            take = min(remaining, lv.quantity * m.liquidity_fraction)
            if take <= 0:
                break
            filled += take
            spent += take * lv.price
            remaining -= take
            if remaining <= 0:
                break
        avg = (spent / filled) if filled > 0 else None
        meta = next((i for i in await self._src.list_instruments() if i.symbol == symbol), None)
        if meta is not None and meta.qty_step > 0 and filled > 0:
            filled = (filled // meta.qty_step) * meta.qty_step  # venues only fill in whole steps
            if filled == 0:
                avg = None
        mid = self._marks.get(symbol) or (avg or D(0))
        fee = filled * (avg or D(0)) * self._taker
        status = (OrderStatus.FILLED if filled >= quantity else
                  OrderStatus.PARTIALLY_FILLED if filled > 0 else OrderStatus.CANCELED)
        if filled > 0:
            self._apply_fill(symbol, side, filled, avg, fee)
            self.fills.append(PaperFill(client_order_id, symbol, side, filled, avg, fee, self._clock.now(), mid))
        order = self._state(symbol, client_order_id, side, quantity, filled, avg, fee, reduce_only, status)
        self._orders[client_order_id] = order

        if m.timeout_after_fill_next > 0:
            m.timeout_after_fill_next -= 1
            raise ExchangeRequestError(f"{self.name}: request timed out")
        return order

    async def cancel_order(self, symbol: str, *, order_id: str | None = None,
                           client_order_id: str | None = None) -> OrderState:
        return await self.get_order(order_id, client_order_id)  # IOC orders are already final

    def _state(self, symbol, cid, side, qty, filled, avg, fee, reduce_only, status) -> OrderState:
        return OrderState(venue=self.name, symbol=symbol, order_id=f"paper-{cid}", client_order_id=cid, side=side,
                          status=status, requested_quantity=qty, filled_quantity=filled, average_fill_price=avg,
                          fee_paid_usd=fee, reduce_only=reduce_only, received_at=self._clock.now())

    def _apply_fill(self, symbol: str, side: Side, qty: Decimal, price: Decimal, fee: Decimal) -> None:
        pos = self._positions.setdefault(symbol, _Position())
        signed = qty if side is Side.BUY else -qty
        self._balance -= fee
        if pos.quantity == 0 or (pos.quantity > 0) == (signed > 0):
            total = abs(pos.quantity) + qty
            pos.entry_price = (pos.entry_price * abs(pos.quantity) + price * qty) / total
            pos.quantity += signed
        else:
            closing = min(qty, abs(pos.quantity))
            pnl = (price - pos.entry_price) * closing * (1 if pos.quantity > 0 else -1)
            pos.realized_pnl += pnl
            self._balance += pnl
            pos.quantity += signed
            if pos.quantity == 0:
                pos.entry_price = D(0)
            elif (pos.quantity > 0) == (signed > 0):  # flipped through zero
                pos.entry_price = price

    # ---- funding -------------------------------------------------------------------------------
    def apply_funding(self, symbol: str, rate: Decimal, when: datetime) -> FundingPayment | None:
        """Settle one funding interval. Positive rate: longs pay shorts (section 12)."""
        pos = self._positions.get(symbol)
        mark = self._marks.get(symbol)
        if pos is None or pos.quantity == 0 or mark is None:
            return None
        notional = pos.quantity * mark  # signed
        payment = -notional * rate  # long (notional > 0) pays when rate > 0
        self._balance += payment
        record = FundingPayment(when, symbol, rate, abs(notional), payment)
        self.funding_payments.append(record)
        return record

    # ---- introspection for accounting and recovery ---------------------------------------------
    def realized_pnl(self, symbol: str) -> Decimal:
        pos = self._positions.get(symbol)
        return pos.realized_pnl if pos else D(0)

    def total_fees(self, symbol: str | None = None) -> Decimal:
        return sum((f.fee for f in self.fills if symbol is None or f.symbol == symbol), D(0))

    def total_funding(self, symbol: str | None = None) -> Decimal:
        return sum((p.payment for p in self.funding_payments if symbol is None or p.symbol == symbol), D(0))

    def snapshot(self) -> dict:
        """JSON-safe state for restart recovery."""
        return {
            "balance": str(self._balance),
            "positions": {s: {"q": str(p.quantity), "e": str(p.entry_price), "r": str(p.realized_pnl)}
                          for s, p in self._positions.items()},
            "leverage": {s: str(v) for s, v in self._leverage.items()},
            "marks": {s: str(v) for s, v in self._marks.items()},
            "orders": {k: o.model_dump(mode="json") for k, o in self._orders.items()},
            "fills": [{"cid": f.client_order_id, "symbol": f.symbol, "side": f.side.value, "q": str(f.quantity),
                       "p": str(f.price), "fee": str(f.fee), "t": f.time.isoformat(),
                       "ref": str(f.reference_price)} for f in self.fills],
            "funding": [{"t": p.time.isoformat(), "symbol": p.symbol, "rate": str(p.rate),
                         "notional": str(p.notional), "payment": str(p.payment)} for p in self.funding_payments],
        }

    def restore(self, data: dict) -> None:
        self._balance = D(data["balance"])
        self._positions = {s: _Position(D(v["q"]), D(v["e"]), D(v["r"])) for s, v in data["positions"].items()}
        self._leverage = {s: D(v) for s, v in data["leverage"].items()}
        self._marks = {s: D(v) for s, v in data["marks"].items()}
        self._orders = {k: OrderState.model_validate(v) for k, v in data["orders"].items()}
        self.fills = [PaperFill(f["cid"], f["symbol"], Side(f["side"]), D(f["q"]), D(f["p"]), D(f["fee"]),
                                datetime.fromisoformat(f["t"]), D(f["ref"])) for f in data["fills"]]
        self.funding_payments = [FundingPayment(datetime.fromisoformat(p["t"]), p["symbol"], D(p["rate"]),
                                                D(p["notional"]), D(p["payment"])) for p in data["funding"]]
