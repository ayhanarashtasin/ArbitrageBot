"""LiveVenue: one trade's leg on a REAL venue (project.md phase 8).

The live counterpart of PaperVenue. Market data comes from the public adapter, orders and positions from the signed
client. The interface the engine and the pair executor use is identical, so every rule tested in paper mode (paired
slices, equal quantities, delta repair, the 30 % / 10 % equity exits, edge decay) runs unchanged.

THE LEG'S EQUITY is the trade's own, exactly as in paper mode:
    equity = budget + realised P&L + unrealised P&L - fees + funding     (all from THIS leg's own fills)
with budget = target notional / leverage, the margin a true 4x account would hold. The venue account may hold other
money; the equity rules never depend on it. The venue's own figures are still used where they are the truth: the
position itself (quantity and liquidation price) and the account's margin ratio.

The venue is the truth for the position. If the venue's position ever differs from this leg's fills, the fills are
re-read; if it still differs (a fill we cannot see, or someone traded the symbol by hand) equity is reported as
unknown, which puts the trade in DEGRADED and keeps it there until venue and ledger agree again.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
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
from app.exchanges.private.base import (
    FundingIncome,
    OrderNotFound,
    PrivateClient,
    PrivateFill,
)

log = logging.getLogger(__name__)
D = Decimal
USD_LIKE = frozenset({"USDT", "USDC", "USD"})
TERMINAL = frozenset({OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED})
# A market order is matched at once; its final state is read back within this budget (0.2 s, 0.4 s, 0.8 s, ... ).
ORDER_SETTLE_SECONDS = 6.0
# Right after a timed-out request a venue can briefly answer "no such order" while it is still processing it: "not
# found" is only believed once it has been repeated for the client's not_found_grace_seconds (one query per second).
POSITION_CACHE_SECONDS = 1.0


@dataclass(frozen=True)
class LiveFill:
    """One ORDER's executions aggregated (the engine records one fill row per order). fee is USD."""

    client_order_id: str
    order_id: str
    symbol: str
    side: Side
    quantity: Decimal  # BASE units
    price: Decimal  # volume-weighted
    fee: Decimal  # USD, positive = paid
    time: datetime
    reference_price: Decimal | None  # mid when the order was sent (slippage reference)


class LiveVenue(ExchangeAdapter):
    def __init__(self, source: ExchangeAdapter, private: PrivateClient, meta: InstrumentMeta, *, clock: Clock,
                 budget_usd: Decimal, account_key: str, opened_at: datetime):
        self.name = source.name
        self._src, self._private, self.meta = source, private, meta
        self._clock = clock
        self.budget = budget_usd
        self.paper_key = account_key  # the engine persists every leg ledger under this key (paper or live)
        self.opened_at = opened_at
        self._orders: dict[str, OrderState] = {}
        self._fills: dict[str, list[PrivateFill]] = {}  # client order id -> venue executions
        self._fill_ids: set[str] = set()
        self._refs: dict[str, Decimal | None] = {}  # client order id -> mid at submission
        self._order_ids: dict[str, str] = {}  # client order id -> venue order id
        self.funding_payments: list[FundingIncome] = []
        self._funding_refs: set[str] = set()
        self._marks: dict[str, Decimal] = {}
        self._fee_prices: dict[str, Decimal] = {}  # fee asset -> USD price when the fee was first seen
        self._position_cache: tuple[datetime, PositionState | None] | None = None
        # True for a leg rebuilt after a restart: an order may have reached the venue just before the crash without
        # being recorded, so every client order id is looked up on the venue before it is sent (never sent twice).
        self.restored = False

    # ---- market data: the public adapter ----------------------------------------------------------
    async def get_server_time(self):
        return await self._src.get_server_time()

    async def list_instruments(self) -> list[InstrumentMeta]:
        return await self._src.list_instruments()

    async def get_funding_info(self, symbol: str) -> FundingInfo:
        return await self._src.get_funding_info(symbol)

    async def get_funding_history(self, symbol: str, limit: int = 100):
        return await self._src.get_funding_history(symbol, limit)

    async def get_ticker(self, symbol: str) -> Ticker:
        return await self._src.get_ticker(symbol)

    async def get_order_book(self, symbol: str, depth: int = 50) -> OrderBook:
        book = await self._src.get_order_book(symbol, depth)
        if book.best_bid and book.best_ask:
            self._marks[symbol] = (book.best_bid + book.best_ask) / 2
        return book

    async def get_candles(self, symbol: str, timeframe: str = "1m", limit: int = 200) -> list[Candle]:
        return await self._src.get_candles(symbol, timeframe, limit)

    async def get_closed_candles(self, symbol: str, timeframe: str = "1m", limit: int = 200) -> list[Candle]:
        return await self._src.get_closed_candles(symbol, timeframe, limit)

    async def get_fee_schedule(self, symbol: str) -> FeeSchedule:
        return await self._private.fee_schedule(self.meta)

    def mark_price(self, symbol: str) -> Decimal | None:
        return self._marks.get(symbol)

    # ---- the leg's own ledger -----------------------------------------------------------------------
    def _replay(self) -> tuple[Decimal, Decimal, Decimal]:
        """(signed quantity, average entry price, realised P&L) from this leg's executions, oldest first."""
        qty, entry, realised = D(0), D(0), D(0)
        executions = sorted((f for fills in self._fills.values() for f in fills), key=lambda f: (f.time, f.trade_id))
        for f in executions:
            signed = f.quantity if f.side is Side.BUY else -f.quantity
            if qty == 0 or (qty > 0) == (signed > 0):
                total = abs(qty) + f.quantity
                entry = (entry * abs(qty) + f.price * f.quantity) / total
                qty += signed
            else:
                closing = min(f.quantity, abs(qty))
                realised += (f.price - entry) * closing * (1 if qty > 0 else -1)
                qty += signed
                if qty == 0:
                    entry = D(0)
                elif (qty > 0) == (signed > 0):  # flipped through zero
                    entry = f.price
        return qty, entry, realised

    def total_fees(self) -> Decimal:
        return sum((self._fee_usd(f) for fills in self._fills.values() for f in fills), D(0))

    def total_funding(self) -> Decimal:
        return sum((p.amount for p in self.funding_payments), D(0))

    def _fee_usd(self, f: PrivateFill) -> Decimal:
        asset = f.fee_asset.upper()
        if asset in USD_LIKE or f.fee == 0:
            return f.fee
        price = self._fee_prices.get(asset)
        if price is None:  # _price_fee_asset runs before any such fill enters the ledger
            raise ExchangeRequestError(f"{self.name}: no USD price for a fee charged in {asset}")
        return f.fee * price

    async def _price_fee_asset(self, asset: str) -> None:
        """A fee paid in the venue's token (BNB on Binance, MNT on Bybit) is valued at that token's price on the same
        venue when it is first seen. Refusing it instead would leave a live position without a usable ledger."""
        asset = asset.upper()
        if asset in USD_LIKE or asset in self._fee_prices:
            return
        try:
            meta = await self._src.resolve_symbol(asset, "USDT", "USDT")
            book = await self._src.get_order_book(meta.symbol, 5)
        except ExchangeError as exc:
            raise ExchangeRequestError(f"{self.name}: cannot value a fee charged in {asset}: {exc}") from exc
        if not (book.best_bid and book.best_ask):
            raise ExchangeRequestError(f"{self.name}: cannot value a fee charged in {asset}: empty book")
        self._fee_prices[asset] = (book.best_bid + book.best_ask) / 2
        log.warning("%s: fees are being charged in %s (valued at %s USD); switch fee payment to USDT to avoid this",
                    self.name, asset, self._fee_prices[asset])

    async def _ingest(self, client_order_id: str, order_id: str | None) -> None:
        """Read this order's executions from the venue into the ledger (idempotent)."""
        if not order_id:
            return
        for f in await self._private.fills(self.meta, order_id=order_id):
            if f.trade_id in self._fill_ids:
                continue
            await self._price_fee_asset(f.fee_asset)
            self._fill_ids.add(f.trade_id)
            self._fills.setdefault(client_order_id, []).append(f)

    async def resync(self) -> None:
        """Re-read EVERY execution on this symbol since the trade opened (after a restart or a ledger/venue mismatch).

        The bot only opens a leg on a symbol the account does not hold (see preflight) and trades each symbol alone, so
        all of them belong to this leg. An execution of an order this leg never recorded (sent just before a crash) is
        kept under "venue-<order id>" so the ledger still matches the venue."""
        by_order = {oid: cid for cid, oid in self._order_ids.items() if oid}
        for f in await self._private.fills(self.meta, since=self.opened_at - timedelta(minutes=1)):
            if f.trade_id in self._fill_ids:
                continue
            await self._price_fee_asset(f.fee_asset)
            cid = by_order.get(f.order_id) or f"venue-{f.order_id}"
            self._fill_ids.add(f.trade_id)
            self._fills.setdefault(cid, []).append(f)

    # ---- account and position -------------------------------------------------------------------
    async def get_position(self, symbol: str) -> PositionState | None:
        now = self._clock.now()
        if self._position_cache and now - self._position_cache[0] < timedelta(seconds=POSITION_CACHE_SECONDS):
            return self._position_cache[1]
        pos = await self._private.position(self.meta)
        self._position_cache = (now, pos)
        return pos

    async def get_positions(self) -> list[PositionState]:
        pos = await self.get_position(self.meta.symbol)
        return [pos] if pos else []

    def _agrees(self, venue_qty: Decimal, ledger_qty: Decimal) -> bool:
        return abs(venue_qty - ledger_qty) < self.meta.qty_step / 2

    async def get_account_equity(self) -> AccountState:
        pos = await self.get_position(self.meta.symbol)
        venue_qty = pos.quantity if pos else D(0)
        qty, entry, realised = self._replay()
        if not self._agrees(venue_qty, qty):
            await self.resync()
            self._position_cache = None
            pos = await self.get_position(self.meta.symbol)
            venue_qty = pos.quantity if pos else D(0)
            qty, entry, realised = self._replay()
            if not self._agrees(venue_qty, qty):
                raise ExchangeRequestError(
                    f"{self.name}: venue position {venue_qty} {self.meta.symbol} does not match this trade's fills "
                    f"({qty}); was the symbol traded outside the bot?")
        unrealised = D(0)
        if qty != 0:
            mark = self._marks.get(self.meta.symbol) or (pos.mark_price if pos else None)
            if mark is None:
                await self.get_order_book(self.meta.symbol)
                mark = self._marks[self.meta.symbol]
            unrealised = qty * (mark - entry)
        equity = self.budget + realised + unrealised - self.total_fees() + self.total_funding()
        account = await self._private.account()
        return AccountState(venue=self.name, equity_usd=equity, available_margin_usd=account.available_usd,
                            margin_utilization=account.margin_ratio, received_at=self._clock.now())

    async def get_available_balance(self) -> Decimal:
        return (await self._private.account()).available_usd

    async def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        await self._private.prepare_symbol(self.meta, leverage)

    # ---- orders ---------------------------------------------------------------------------------
    def _with_fills(self, state: OrderState) -> OrderState:
        """The venue's order state, with filled quantity, average price and fee taken from the ledger's executions
        when the ledger has them (the venue's own fee figure is not always in USD)."""
        fills = self._fills.get(state.client_order_id, [])
        if not fills:
            return state.model_copy(update={"fee_paid_usd": D(0)}) if state.filled_quantity == 0 else state
        qty = sum((f.quantity for f in fills), D(0))
        avg = sum((f.price * f.quantity for f in fills), D(0)) / qty
        fee = sum((self._fee_usd(f) for f in fills), D(0))
        return state.model_copy(update={"filled_quantity": max(qty, state.filled_quantity), "average_fill_price": avg,
                                        "fee_paid_usd": fee})

    def _ledger_complete(self, state: OrderState) -> bool:
        have = sum((f.quantity for f in self._fills.get(state.client_order_id, [])), D(0))
        return state.status in TERMINAL and have >= state.filled_quantity

    async def place_order(self, symbol: str, side: Side, quantity: Decimal, *, client_order_id: str,
                          price: Decimal | None = None, reduce_only: bool = False) -> OrderState:
        if symbol != self.meta.symbol:
            raise ExchangeError(f"{self.name}: this leg trades {self.meta.symbol}, not {symbol}")
        cached = self._orders.get(client_order_id)
        if cached is not None:
            return cached  # idempotent: the same client order id is never sent twice
        if client_order_id in self._order_ids:
            return await self.get_order(client_order_id=client_order_id)
        if self.restored:  # did this order reach the venue before the restart? Then adopt it, never send it again
            try:
                state = await self._private.get_order(self.meta, client_order_id)
            except OrderNotFound:
                pass
            else:
                self._order_ids[client_order_id] = state.order_id or ""
                return await self._settle(client_order_id)
        self._refs[client_order_id] = self._marks.get(symbol)
        self._position_cache = None
        order_id = await self._private.place_market_order(self.meta, side, quantity, client_order_id=client_order_id,
                                                          reduce_only=reduce_only)
        self._order_ids[client_order_id] = order_id
        return await self._settle(client_order_id)

    async def _settle(self, client_order_id: str) -> OrderState:
        """Read a just-sent order back until it is final, then its executions."""
        waited, step = 0.0, 0.2
        state = None
        while True:
            try:
                state = await self._private.get_order(self.meta, client_order_id)
            except OrderNotFound:
                state = None  # not visible yet
            if state is not None and (state.status in TERMINAL or state.filled_quantity >= state.requested_quantity):
                break
            if waited >= ORDER_SETTLE_SECONDS:
                break
            await self._clock.sleep(step)
            waited += step
            step = min(step * 2, 1.6)
        if state is None:
            raise ExchangeRequestError(f"{self.name}: order {client_order_id} was accepted but cannot be read back")
        self._order_ids.setdefault(client_order_id, state.order_id or "")
        await self._ingest(client_order_id, state.order_id)
        state = self._with_fills(state)
        if self._ledger_complete(state):
            self._orders[client_order_id] = state
        return state

    async def get_order(self, order_id: str | None = None, client_order_id: str | None = None) -> OrderState:
        if client_order_id is None:
            client_order_id = next((c for c, o in self._order_ids.items() if o == order_id), None)
            if client_order_id is None:
                raise OrderNotFound(f"{self.name}: unknown order")
        cached = self._orders.get(client_order_id)
        if cached is not None:
            return cached
        if client_order_id.startswith("venue-"):  # found only through its executions (see resync)
            return self._from_fills(client_order_id)
        waited = 0.0
        while True:
            try:
                state = await self._private.get_order(self.meta, client_order_id)
                break
            except OrderNotFound:
                if waited >= self._private.not_found_grace_seconds:
                    raise
                await self._clock.sleep(1.0)
                waited += 1.0
        if state.order_id:
            self._order_ids[client_order_id] = state.order_id
        await self._ingest(client_order_id, state.order_id)
        state = self._with_fills(state)
        if self._ledger_complete(state):
            self._orders[client_order_id] = state
        return state

    def _from_fills(self, client_order_id: str) -> OrderState:
        fills = self._fills.get(client_order_id)
        if not fills:
            raise OrderNotFound(f"{self.name}: unknown order {client_order_id}")
        qty = sum((f.quantity for f in fills), D(0))
        return OrderState(venue=self.name, symbol=self.meta.symbol, order_id=fills[0].order_id,
                          client_order_id=client_order_id, side=fills[0].side, status=OrderStatus.FILLED,
                          requested_quantity=qty, filled_quantity=qty,
                          average_fill_price=sum((f.price * f.quantity for f in fills), D(0)) / qty,
                          fee_paid_usd=sum((self._fee_usd(f) for f in fills), D(0)), reduce_only=False,
                          received_at=self._clock.now())

    async def get_fills(self, symbol: str | None = None) -> list[LiveFill]:
        out = []
        for cid, fills in self._fills.items():
            if not fills:
                continue
            qty = sum((f.quantity for f in fills), D(0))
            out.append(LiveFill(cid, fills[0].order_id, fills[0].symbol, fills[0].side, qty,
                                sum((f.price * f.quantity for f in fills), D(0)) / qty,
                                sum((self._fee_usd(f) for f in fills), D(0)), max(f.time for f in fills),
                                self._refs.get(cid)))
        return out

    async def get_open_orders(self, symbol: str | None = None) -> list[OrderState]:
        return await self._private.open_orders(self.meta)

    async def cancel_order(self, symbol: str, *, order_id: str | None = None,
                           client_order_id: str | None = None) -> OrderState:
        if client_order_id is None:
            raise ExchangeError(f"{self.name}: cancel needs the client order id")
        await self._private.cancel_order(self.meta, client_order_id)
        self._orders.pop(client_order_id, None)
        return await self.get_order(client_order_id=client_order_id)

    # ---- funding ----------------------------------------------------------------------------------
    async def collect_funding(self) -> list[FundingIncome]:
        """New funding settlements on this symbol since the trade opened (each counted once)."""
        new = []
        for p in await self._private.funding_income(self.meta, since=self.opened_at):
            if p.ref in self._funding_refs or p.time < self.opened_at:
                continue
            if p.asset.upper() not in USD_LIKE:
                raise ExchangeRequestError(f"{self.name}: funding paid in {p.asset}")
            self._funding_refs.add(p.ref)
            self.funding_payments.append(p)
            new.append(p)
        return new

    # ---- persistence (restart recovery) ---------------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "live": True, "budget": str(self.budget), "opened_at": self.opened_at.isoformat(),
            "order_ids": dict(self._order_ids), "refs": {k: str(v) if v is not None else None for k, v in self._refs.items()},
            "orders": {k: o.model_dump(mode="json") for k, o in self._orders.items()},
            "fills": {cid: [_fill_json(f) for f in fills] for cid, fills in self._fills.items()},
            "funding": [{"t": p.time.isoformat(), "symbol": p.symbol, "amount": str(p.amount), "asset": p.asset,
                         "ref": p.ref} for p in self.funding_payments],
            "marks": {s: str(v) for s, v in self._marks.items()},
            "fee_prices": {a: str(v) for a, v in self._fee_prices.items()},
        }

    def restore(self, data: dict) -> None:
        self.restored = True
        self.budget = D(data["budget"])
        self.opened_at = datetime.fromisoformat(data["opened_at"])
        self._order_ids = dict(data.get("order_ids", {}))
        self._refs = {k: D(v) if v is not None else None for k, v in data.get("refs", {}).items()}
        self._orders = {k: OrderState.model_validate(v) for k, v in data.get("orders", {}).items()}
        self._fills = {cid: [_fill_from(f) for f in fills] for cid, fills in data.get("fills", {}).items()}
        self._fill_ids = {f.trade_id for fills in self._fills.values() for f in fills}
        self.funding_payments = [FundingIncome(datetime.fromisoformat(p["t"]), p["symbol"], D(p["amount"]),
                                               p["asset"], p["ref"]) for p in data.get("funding", [])]
        self._funding_refs = {p.ref for p in self.funding_payments}
        self._marks = {s: D(v) for s, v in data.get("marks", {}).items()}
        self._fee_prices = {a: D(v) for a, v in data.get("fee_prices", {}).items()}


def _fill_json(f: PrivateFill) -> dict:
    return {"id": f.trade_id, "oid": f.order_id, "cid": f.client_order_id, "symbol": f.symbol, "side": f.side.value,
            "q": str(f.quantity), "p": str(f.price), "fee": str(f.fee), "fee_asset": f.fee_asset,
            "t": f.time.isoformat()}


def _fill_from(d: dict) -> PrivateFill:
    return PrivateFill(d["id"], d["oid"], d.get("cid"), d["symbol"], Side(d["side"]), D(d["q"]), D(d["p"]),
                       D(d["fee"]), d["fee_asset"], datetime.fromisoformat(d["t"]))


async def preflight(private: PrivateClient, meta: InstrumentMeta, budget_usd: Decimal) -> None:
    """Refuse to open a leg unless the venue is clean for this symbol and the account can carry the margin."""
    if meta.settle.upper() not in private.settle_assets:
        raise ExchangeError(f"{private.name}: live trading is limited to {sorted(private.settle_assets)} margined "
                            f"contracts, {meta.symbol} settles in {meta.settle}")
    pos, orders, account = await asyncio.gather(private.position(meta), private.open_orders(meta), private.account())
    if pos is not None and pos.quantity != 0:
        raise ExchangeError(f"{private.name}: an existing {meta.symbol} position ({pos.quantity}) is open on the "
                            "account; the bot only trades symbols it holds alone")
    if orders:
        raise ExchangeError(f"{private.name}: {len(orders)} open {meta.symbol} order(s) on the account")
    need = budget_usd * D("1.2")  # the leg's margin plus room for fees and a first adverse move
    if account.available_usd < need:
        raise ExchangeError(f"{private.name}: available margin {account.available_usd:.2f} USD is below the "
                            f"{need:.2f} USD this leg needs")
