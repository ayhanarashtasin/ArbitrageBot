"""Delta neutrality from ACTUAL filled exposure (project.md section 27, LOCKED).

    net_delta_usd = long_filled_exposure_usd - short_filled_exposure_usd
    delta_ratio   = abs(net_delta_usd) / max(long_exposure_usd, short_exposure_usd)

Requested quantities are never used. Both limits are checked; breaching either is unsafe.
"""

from dataclasses import dataclass
from decimal import Decimal

D = Decimal


@dataclass(frozen=True)
class DeltaState:
    long_usd: Decimal
    short_usd: Decimal

    @property
    def net_usd(self) -> Decimal:
        return self.long_usd - self.short_usd

    @property
    def ratio(self) -> Decimal:
        biggest = max(self.long_usd, self.short_usd)
        return abs(self.net_usd) / biggest if biggest > 0 else D(0)

    @property
    def lagging_leg(self) -> str | None:
        if self.net_usd == 0:
            return None
        return "short" if self.net_usd > 0 else "long"

    @property
    def heavy_leg(self) -> str | None:
        lag = self.lagging_leg
        return None if lag is None else ("long" if lag == "short" else "short")


def compute_delta(long_qty: Decimal, long_mark: Decimal, short_qty: Decimal, short_mark: Decimal) -> DeltaState:
    """Quantities are unsigned base-asset amounts actually held; marks are current prices."""
    return DeltaState(abs(long_qty) * long_mark, abs(short_qty) * short_mark)


def is_unsafe(state: DeltaState, max_usd: Decimal, max_ratio: Decimal, dust_usd: Decimal = D(0)) -> bool:
    """Either limit breached is unsafe, except an imbalance no larger than `dust_usd`.

    `dust_usd` is the smallest amount a venue can trade (its minimum notional). An imbalance below it
    cannot be corrected, so treating it as unsafe would block trading forever over a few cents.
    """
    net = abs(state.net_usd)
    if net <= dust_usd:
        return False
    return net > max_usd or state.ratio > max_ratio


def is_flat(state: DeltaState, dust_usd: Decimal = D(0)) -> bool:
    """No meaningful exposure left on either leg."""
    return max(state.long_usd, state.short_usd) <= dust_usd
