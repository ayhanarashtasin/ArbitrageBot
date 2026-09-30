"""Paired execution: one hedge slice = a long leg and a short leg sent together (project.md sections 26-30).

Rules enforced here:
- both legs are submitted concurrently (asyncio.gather) and the submit-time gap is recorded;
- client order ids are deterministic:  <tradeId>-s<slice>-<L|S>-a<attempt>  (section 30);
- a timed-out order is NEVER assumed failed: its real state is queried by client id first (sections 30, 53);
- fills, not requests, drive delta (section 27);
- a missing hedge is completed or the excess is flattened (sections 28, 29). No blind retries.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.core.clock import Clock
from app.core.trading_config import ResolvedParams
from app.domain.market import InstrumentMeta, OrderBook, OrderState, OrderStatus, Side
from app.exchanges.base import ExchangeAdapter, ExchangeError, ExchangeRequestError
from app.execution.delta import DeltaState, compute_delta, is_flat, is_unsafe
from app.strategy.executable import common_lot, equal_quantity, vwap_for_notional

D = Decimal
EventSink = Callable[[str, dict], None]


class SliceTooSmall(Exception):
    """The child order would fall below the venue's minimum quantity or notional."""


@dataclass
class Leg:
    role: str  # "long" or "short"
    venue: ExchangeAdapter  # a PaperVenue in paper mode
    meta: InstrumentMeta

    @property
    def entry_side(self) -> Side:
        return Side.BUY if self.role == "long" else Side.SELL

    @property
    def exit_side(self) -> Side:
        return Side.SELL if self.role == "long" else Side.BUY


@dataclass
class Submission:
    leg: str
    client_order_id: str
    quantity: Decimal
    side: Side
    submitted_at: datetime
    order: OrderState | None
    status: str  # OK | REJECTED | NOT_PLACED | UNRESOLVED | SKIPPED
    error: str | None = None

    @property
    def filled_quantity(self) -> Decimal:
        return self.order.filled_quantity if self.order else D(0)

    @property
    def filled_usd(self) -> Decimal:
        if not self.order or not self.order.average_fill_price:
            return D(0)
        return self.order.filled_quantity * self.order.average_fill_price


@dataclass
class SliceResult:
    index: int
    phase: str  # entry | exit
    long: Submission
    short: Submission
    submit_diff_ms: float
    reference: dict[str, Decimal] = field(default_factory=dict)  # best prices used for sizing

    @property
    def unresolved(self) -> bool:
        return "UNRESOLVED" in (self.long.status, self.short.status)

    @property
    def fully_filled(self) -> bool:
        return all(s.order is not None and s.order.status is OrderStatus.FILLED for s in (self.long, self.short))


@dataclass
class HealResult:
    healed: bool
    steps: int
    flattened: bool
    delta: DeltaState


def client_order_id(trade_id: str, slice_index: int, leg: str, attempt: int, phase: str = "s") -> str:
    """tradeId-sliceId-leg-attempt. phase: s = entry slice, x = exit slice, h = healing order."""
    return f"{trade_id}-{phase}{slice_index:04d}-{'L' if leg == 'long' else 'S'}-a{attempt:02d}"


def round_down(qty: Decimal, step: Decimal) -> Decimal:
    return (qty // step) * step if step > 0 else qty


class PairExecutor:
    def __init__(self, trade_id: str, long: Leg, short: Leg, clock: Clock, params: ResolvedParams,
                 events: EventSink | None = None, heal_seq_start: int = 0):
        self.trade_id = trade_id
        self._heal_seq = heal_seq_start  # every healing order gets a fresh id: a reused id would be a no-op
        self.max_delta_usd = D(0)
        self.long, self.short = long, short
        self.clock, self.params = clock, params
        self._emit = events or (lambda kind, data: None)

    # ---- data ----------------------------------------------------------------------------------
    async def books(self) -> tuple[OrderBook, OrderBook]:
        return tuple(await asyncio.gather(  # type: ignore[return-value]
            self.long.venue.get_order_book(self.long.meta.symbol),
            self.short.venue.get_order_book(self.short.meta.symbol)))

    @staticmethod
    def _price(book: OrderBook, side: Side) -> Decimal:
        price = book.best_ask if side is Side.BUY else book.best_bid
        if price is None:
            raise ExchangeError("empty order book side")
        return price

    async def held_quantity(self, leg: Leg) -> Decimal:
        pos = await leg.venue.get_position(leg.meta.symbol)
        return abs(pos.quantity) if pos else D(0)

    async def delta(self) -> DeltaState:
        lq, sq = await asyncio.gather(self.held_quantity(self.long), self.held_quantity(self.short))
        lb, sb = await self.books()
        lmark = (lb.best_bid + lb.best_ask) / 2
        smark = (sb.best_bid + sb.best_ask) / 2
        state = compute_delta(lq, lmark, sq, smark)
        self.max_delta_usd = max(self.max_delta_usd, abs(state.net_usd))
        return state

    @property
    def lot(self) -> Decimal:
        """Smallest quantity both venues accept: every entry and exit slice is a multiple of it on BOTH legs."""
        return common_lot(self.long.meta.qty_step, self.short.meta.qty_step)

    def _check_min(self, qty: Decimal, prices: dict[str, Decimal]) -> None:
        for leg, price in ((self.long, prices["long"]), (self.short, prices["short"])):
            if qty <= 0 or qty < leg.meta.min_qty or (leg.meta.min_notional and qty * price < leg.meta.min_notional):
                raise SliceTooSmall(f"{leg.role}: {qty} at {price} is below the venue minimum")

    @property
    def dust_usd(self) -> Decimal:
        """Smallest tradable amount on either venue: anything below it cannot be corrected or closed."""
        return max(self.long.meta.min_notional or D(0), self.short.meta.min_notional or D(0))

    def unsafe(self, state: DeltaState) -> bool:
        return is_unsafe(state, self.params.max_delta_imbalance_usd, self.params.max_delta_imbalance_ratio,
                         self.dust_usd)

    def flat(self, state: DeltaState) -> bool:
        return is_flat(state, self.dust_usd)

    # ---- order submission ----------------------------------------------------------------------
    async def _query_final(self, leg: Leg, cid: str) -> tuple[OrderState | None, str]:
        """Ask the venue what really happened to `cid`. Returns (order, status)."""
        for _ in range(3):
            try:
                return await leg.venue.get_order(client_order_id=cid), "OK"
            except ExchangeRequestError:
                await self.clock.sleep(0.5)  # could not even ask: try again, then report UNRESOLVED
            except ExchangeError:
                return None, "NOT_PLACED"  # the venue has no such order: confirmed absent
        return None, "UNRESOLVED"

    async def _submit(self, leg: Leg, side: Side, qty: Decimal, cid: str, reduce_only: bool) -> Submission:
        submitted_at = self.clock.now()
        if qty <= 0:
            return Submission(leg.role, cid, qty, side, submitted_at, None, "SKIPPED")
        try:
            order = await leg.venue.place_order(leg.meta.symbol, side, qty, client_order_id=cid,
                                                reduce_only=reduce_only)
            return Submission(leg.role, cid, qty, side, submitted_at, order, "OK")
        except ExchangeRequestError as exc:  # timeout or transport failure: outcome unknown
            order, status = await self._query_final(leg, cid)
            return Submission(leg.role, cid, qty, side, submitted_at, order, status, str(exc))
        except ExchangeError as exc:  # explicit rejection
            return Submission(leg.role, cid, qty, side, submitted_at, None, "REJECTED", str(exc))

    def _size(self, leg: Leg, notional: Decimal, price: Decimal) -> Decimal:
        qty = round_down(notional / price, leg.meta.qty_step)
        if qty < leg.meta.min_qty or (leg.meta.min_notional and qty * price < leg.meta.min_notional):
            raise SliceTooSmall(f"{leg.role}: {qty} at {price} is below the venue minimum")
        return qty

    async def _pair(self, index: int, phase: str, long_args, short_args, reference) -> SliceResult:
        long_sub, short_sub = await asyncio.gather(self._submit(*long_args), self._submit(*short_args))
        diff = abs((long_sub.submitted_at - short_sub.submitted_at).total_seconds()) * 1000
        result = SliceResult(index, phase, long_sub, short_sub, diff, reference)
        self._emit("slice_result", {
            "index": index, "phase": phase, "submit_diff_ms": diff,
            "long_submit": long_sub.submitted_at.isoformat(), "short_submit": short_sub.submitted_at.isoformat(),
            "long": {"cid": long_sub.client_order_id, "status": long_sub.status,
                     "filled_qty": str(long_sub.filled_quantity), "filled_usd": str(long_sub.filled_usd)},
            "short": {"cid": short_sub.client_order_id, "status": short_sub.status,
                      "filled_qty": str(short_sub.filled_quantity), "filled_usd": str(short_sub.filled_usd)},
        })
        return result

    # ---- slices --------------------------------------------------------------------------------
    async def entry_slice(self, index: int, notional: Decimal, attempt: int = 1,
                          quantity: Decimal | None = None) -> SliceResult:
        """Open one hedge slice: buy the long leg and sell the short leg at the same time, the SAME quantity on each.

        `quantity` is the planned slice size (whole common lots). It is cut back if prices rose so far that it would
        now be worth more than the $100 child cap (LOCKED) on either venue.
        """
        lb, sb = await self.books()
        lp, sp = self._price(lb, Side.BUY), self._price(sb, Side.SELL)
        cap = equal_quantity(self.params.twap_max_child_notional_usd, lp, sp, self.lot)
        # The cap is on executable NOTIONAL, not the best quote. Walking deeper levels costs more.
        lv = vwap_for_notional(lb, Side.BUY, self.params.twap_max_child_notional_usd)
        sv = vwap_for_notional(sb, Side.SELL, self.params.twap_max_child_notional_usd)
        if lv.sufficient_depth and sv.sufficient_depth:
            cap = min(cap, round_down(min(lv.filled_quantity, sv.filled_quantity), self.lot))
        qty = equal_quantity(notional, lp, sp, self.lot) if quantity is None else min(quantity, cap)
        self._check_min(qty, {"long": lp, "short": sp})
        lq = sq = qty
        return await self._pair(
            index, "entry",
            (self.long, Side.BUY, lq, client_order_id(self.trade_id, index, "long", attempt), False),
            (self.short, Side.SELL, sq, client_order_id(self.trade_id, index, "short", attempt), False),
            {"long_price": lp, "short_price": sp})

    async def exit_slice(self, index: int, slices_left: int, attempt: int = 1) -> SliceResult:
        """Reduce both legs by the SAME quantity with reduce-only orders; the last slice closes whatever is left.

        The quantity is a whole number of common lots, taken from the smaller leg, so an exit never opens an
        imbalance. If a leg holds more (after a repair), the difference goes in the last slice or to the healer.
        """
        lheld, sheld = await asyncio.gather(self.held_quantity(self.long), self.held_quantity(self.short))
        lb, sb = await self.books()
        if slices_left <= 1:
            lq, sq = lheld, sheld
        else:
            smaller, lot = min(lheld, sheld), self.lot
            q = round_down(smaller / slices_left, lot)
            if q <= 0 and smaller >= lot:
                q = lot  # slices smaller than one lot: close one lot now, the plan finishes early
            lq = sq = min(q, smaller)
        if lq <= 0 and sq <= 0:
            raise SliceTooSmall("nothing left to close")
        long_args = (self.long, Side.SELL, lq, client_order_id(self.trade_id, index, "long", attempt, "x"), True)
        short_args = (self.short, Side.BUY, sq, client_order_id(self.trade_id, index, "short", attempt, "x"), True)
        return await self._pair(index, "exit", long_args, short_args,
                                {"long_price": self._price(lb, Side.SELL), "short_price": self._price(sb, Side.BUY)})

    # ---- imbalance repair ----------------------------------------------------------------------
    async def heal(self, mode: str, deadline: datetime, start_attempt: int = 0) -> HealResult:
        """Bring delta back inside the limits.

        mode "entry": complete the missing hedge on the lagging leg until `deadline`, then flatten
                      the excess on the heavy leg (reduce-only).
        mode "exit":  reduce the heavy leg (reduce-only), because an exit must never open exposure.
        """
        steps = 0
        while True:
            state = await self.delta()
            if not self.unsafe(state):
                return HealResult(True, steps, False, state)
            if self.clock.now() >= deadline:
                break
            steps += 1
            if mode == "entry":
                await self._correct_lagging(state)
            else:
                await self._reduce_heavy(state)
            await self.clock.sleep(1)

        for _ in range(3):  # deadline passed: flatten the excess
            state = await self.delta()
            if not self.unsafe(state):
                break
            steps += 1
            await self._reduce_heavy(state)
            await self.clock.sleep(1)
        state = await self.delta()
        self._emit("heal_flatten", {"healed": not self.unsafe(state), "net_usd": str(state.net_usd)})
        return HealResult(not self.unsafe(state), steps, True, state)

    def _leg(self, role: str) -> Leg:
        return self.long if role == "long" else self.short

    async def _correct_lagging(self, state: DeltaState) -> None:
        leg = self._leg(state.lagging_leg)
        book = await leg.venue.get_order_book(leg.meta.symbol)
        price = self._price(book, leg.entry_side)
        try:
            qty = self._size(leg, abs(state.net_usd), price)
        except SliceTooSmall:
            return
        self._heal_seq += 1
        cid = client_order_id(self.trade_id, self._heal_seq, leg.role, 1, "h")
        sub = await self._submit(leg, leg.entry_side, qty, cid, False)
        self._emit("heal_order", {"leg": leg.role, "cid": cid, "status": sub.status, "filled": str(sub.filled_quantity)})

    async def _reduce_heavy(self, state: DeltaState) -> None:
        leg = self._leg(state.heavy_leg)
        book = await leg.venue.get_order_book(leg.meta.symbol)
        price = self._price(book, leg.exit_side)
        held = await self.held_quantity(leg)
        try:
            qty = min(held, self._size(leg, abs(state.net_usd), price))
        except SliceTooSmall:
            return
        self._heal_seq += 1
        cid = client_order_id(self.trade_id, self._heal_seq, leg.role, 1, "h")
        sub = await self._submit(leg, leg.exit_side, qty, cid, True)
        self._emit("heal_order", {"leg": leg.role, "cid": cid, "status": sub.status, "filled": str(sub.filled_quantity)})
