"""TWAP scheduling (project.md sections 25, 33, 34). Pure functions, no I/O.

Entry (LOCKED): 6 hours, at most $100 of notional per leg per slice.
A plan splits `total_notional` into equal slices no larger than the child cap and spaces them evenly.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

D = Decimal


@dataclass(frozen=True)
class TwapPlan:
    total_notional: Decimal  # per leg
    child_notional: Decimal  # per leg, per slice (<= cap)
    slices: int
    interval: timedelta
    start: datetime
    # Base quantity of each slice, the SAME on both legs (make_lot_entry_plan). None = size from child_notional.
    slice_qty: tuple[Decimal, ...] | None = None
    end_at: datetime | None = None  # exit plans put the last reduction exactly at the requested deadline

    def qty_for(self, index: int) -> Decimal | None:
        return self.slice_qty[index] if self.slice_qty and index < len(self.slice_qty) else None

    def due_time(self, index: int) -> datetime:
        if self.end_at is not None and self.slices > 1:
            return self.start + (self.end_at - self.start) * index / (self.slices - 1)
        return self.start + self.interval * index

    def due_index(self, now: datetime, completed: int) -> int | None:
        """Index of the next slice if it is due, else None. Slices are never run out of order or doubled."""
        if completed >= self.slices:
            return None
        return completed if now >= self.due_time(completed) else None

    @property
    def end(self) -> datetime:
        return self.end_at or self.start + self.interval * self.slices


def make_entry_plan(total_notional: Decimal, child_cap: Decimal, duration_minutes: int, start: datetime) -> TwapPlan:
    if total_notional <= 0 or child_cap <= 0:
        raise ValueError("notional and child cap must be positive")
    slices = max(1, math.ceil(total_notional / child_cap))
    child = total_notional / slices
    assert child <= child_cap  # LOCKED: never above the cap
    return TwapPlan(total_notional, child, slices, timedelta(minutes=duration_minutes) / slices, start)


def make_lot_entry_plan(total_notional: Decimal, child_cap: Decimal, duration_minutes: int, start: datetime, *,
                        lot: Decimal, price: Decimal) -> TwapPlan:
    """Entry plan in whole lots that BOTH venues accept, so every slice is the same quantity on each leg.

    `lot` is the common lot of the two venues and `price` the higher of the two current prices. The target is
    rounded DOWN to whole lots (never above it) and each slice holds at most as many lots as fit in the $100 child
    cap (LOCKED). The slice count stays the usual ceil(target / cap) unless coarse lots would then leave the position
    more than 1 % short of the target (BTC: one 0.001 lot is $83, so ten slices would build only $830 of $1000); then
    more slices are used. The lots are spread as evenly as possible.
    """
    if total_notional <= 0 or child_cap <= 0 or lot <= 0 or price <= 0:
        raise ValueError("notional, child cap, lot and price must be positive")
    unit = lot * price
    max_lots = int(child_cap // unit)
    if max_lots < 1:
        raise ValueError(f"one lot ({lot}) is worth {unit:.2f}, more than the {child_cap} child cap")
    total_lots = int(total_notional // unit)
    if total_lots < 1:
        raise ValueError(f"the target {total_notional} is smaller than one lot ({unit:.2f})")
    slices = max(1, math.ceil(total_notional / child_cap))
    if slices * max_lots < total_lots * D("0.99"):
        slices = math.ceil(total_lots / max_lots)
    total_lots = min(total_lots, slices * max_lots)
    base, extra = divmod(total_lots, slices)
    qty = tuple(D(base + (1 if i < extra else 0)) * lot for i in range(slices))
    total = D(total_lots) * unit
    return TwapPlan(total, total / slices, slices, timedelta(minutes=duration_minutes) / slices, start, qty)


MIN_EMERGENCY_SPACING_SECONDS = D(2)


def make_exit_plan(position_notional: Decimal, child_cap: Decimal, duration_seconds: int, start: datetime,
                   *, allow_larger_children: bool = False) -> TwapPlan:
    """Paired reduction plan.

    Controlled exit (3 h) keeps the $100 child cap. The emergency exit (about 1 minute) would need too many
    slices for a large position, so with `allow_larger_children` the slice count is limited to one every
    2 seconds and the child grows accordingly. This is the only place the $100 cap can be exceeded.
    """
    slices = max(1, math.ceil(position_notional / child_cap))
    if allow_larger_children:
        slices = max(1, min(slices, int(D(duration_seconds) / MIN_EMERGENCY_SPACING_SECONDS)))
    child = position_notional / slices
    # The first reduction is immediate and the LAST is at the requested duration, rather than one interval early.
    return TwapPlan(position_notional, child, slices, timedelta(seconds=duration_seconds) / max(1, slices - 1), start,
                    end_at=start + timedelta(seconds=duration_seconds))
