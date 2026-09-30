"""Risk engine: turns the current state of a hedged trade into ONE action (project.md sections 31-40).

Precedence (section 37), highest first:
  1. imminent liquidation / margin emergency          -> EMERGENCY_EXIT
  2. equity < 10 % of baseline (either side)          -> EMERGENCY_EXIT
  3. severe unresolved delta imbalance                -> EMERGENCY_EXIT
  4. exchange / order-system failure or unknown state -> DEGRADED, stale data -> PAUSE_SLICES
  5. equity <= 30 % of baseline (either side)         -> CONTROLLED_EXIT
     (a MODERATE imbalance, HEAL_DELTA, shares this level but yields to the exit, whose slices repair delta)
  6. normal exit signal                               -> NORMAL_EXIT
  7. otherwise                                        -> CONTINUE

Boundaries (LOCKED): exactly 30 % is a controlled exit; exactly 10 % is NOT an emergency (strictly below).
The engine is deterministic and reads only numbers handed to it. It has no side effects and no AI.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from app.core.trading_config import ResolvedParams
from app.execution.delta import DeltaState, is_unsafe

D = Decimal


class RiskAction(StrEnum):
    CONTINUE = "CONTINUE"
    NORMAL_EXIT = "NORMAL_EXIT"
    CONTROLLED_EXIT = "CONTROLLED_EXIT"
    EMERGENCY_EXIT = "EMERGENCY_EXIT"
    HEAL_DELTA = "HEAL_DELTA"  # pause new slices and repair the imbalance
    PAUSE_SLICES = "PAUSE_SLICES"  # stale data: no new slices, keep monitoring
    DEGRADED = "DEGRADED"  # cannot establish state: reconcile, do not assume


@dataclass(frozen=True)
class RiskInputs:
    now: datetime
    baseline_long_equity: Decimal
    baseline_short_equity: Decimal
    long_equity: Decimal | None  # None = could not be read
    short_equity: Decimal | None
    long_liquidation_distance_pct: Decimal | None = None  # percent distance from mark to liquidation
    short_liquidation_distance_pct: Decimal | None = None
    long_margin_utilization_pct: Decimal | None = None
    short_margin_utilization_pct: Decimal | None = None
    delta: DeltaState | None = None
    delta_dust_usd: Decimal = D(0)
    delta_unsafe_since: datetime | None = None
    data_age_ms: dict[str, float] = field(default_factory=dict)  # {"long": ..., "short": ...}
    venues_reachable: dict[str, bool] = field(default_factory=lambda: {"long": True, "short": True})
    unresolved_orders: bool = False
    normal_exit_signal: bool = False


@dataclass(frozen=True)
class RiskDecision:
    action: RiskAction
    priority: int  # 1 (most urgent) .. 7
    reasons: tuple[str, ...]
    long_equity_ratio: Decimal | None
    short_equity_ratio: Decimal | None

    @property
    def is_exit(self) -> bool:
        return self.action in (RiskAction.NORMAL_EXIT, RiskAction.CONTROLLED_EXIT, RiskAction.EMERGENCY_EXIT)


def equity_ratio(current: Decimal | None, baseline: Decimal) -> Decimal | None:
    if current is None or baseline <= 0:
        return None
    return current / baseline


def evaluate(i: RiskInputs, p: ResolvedParams) -> RiskDecision:
    long_ratio = equity_ratio(i.long_equity, i.baseline_long_equity)
    short_ratio = equity_ratio(i.short_equity, i.baseline_short_equity)
    ratios = [r for r in (long_ratio, short_ratio) if r is not None]
    lowest = min(ratios) if ratios else None

    triggered: list[tuple[int, RiskAction, str]] = []

    # 1. liquidation / margin
    for side, dist, util in (("long", i.long_liquidation_distance_pct, i.long_margin_utilization_pct),
                             ("short", i.short_liquidation_distance_pct, i.short_margin_utilization_pct)):
        if dist is not None and dist < p.min_liquidation_distance_pct:
            triggered.append((1, RiskAction.EMERGENCY_EXIT, f"{side}_liquidation_too_close"))
        if util is not None and util > p.max_margin_utilization_pct:
            triggered.append((1, RiskAction.EMERGENCY_EXIT, f"{side}_margin_utilization_high"))

    # 2. equity below 10 % (strictly)
    if lowest is not None and lowest < p.emergency_exit_equity_ratio:
        triggered.append((2, RiskAction.EMERGENCY_EXIT, "equity_below_emergency_ratio"))

    # 3. delta imbalance
    if i.delta is not None and is_unsafe(i.delta, p.max_delta_imbalance_usd, p.max_delta_imbalance_ratio,
                                         i.delta_dust_usd):
        timeout = timedelta(milliseconds=p.delta_hedge_timeout_ms)
        if i.delta_unsafe_since is not None and i.now - i.delta_unsafe_since > timeout:
            triggered.append((3, RiskAction.EMERGENCY_EXIT, "severe_unresolved_delta_imbalance"))
        else:
            triggered.append((5, RiskAction.HEAL_DELTA, "delta_imbalance"))

    # 4. venue / order-system failure or unknown state
    for side in ("long", "short"):
        if not i.venues_reachable.get(side, True):
            triggered.append((4, RiskAction.DEGRADED, f"{side}_venue_unreachable"))
    if i.unresolved_orders:
        triggered.append((4, RiskAction.DEGRADED, "unresolved_order_state"))
    if i.long_equity is None or i.short_equity is None:
        triggered.append((4, RiskAction.DEGRADED, "equity_unknown"))
    for side, age in i.data_age_ms.items():
        if age > p.max_market_data_age_ms:
            triggered.append((4, RiskAction.PAUSE_SLICES, f"{side}_market_data_stale"))

    # 5. equity at or below 30 %
    if lowest is not None and lowest <= p.controlled_exit_equity_ratio:
        triggered.append((5, RiskAction.CONTROLLED_EXIT, "equity_at_or_below_controlled_ratio"))

    # 6. normal exit
    if i.normal_exit_signal:
        triggered.append((6, RiskAction.NORMAL_EXIT, "normal_exit_signal"))

    if not triggered:
        return RiskDecision(RiskAction.CONTINUE, 7, (), long_ratio, short_ratio)

    top_priority = min(t[0] for t in triggered)
    top = [t for t in triggered if t[0] == top_priority]
    # within one priority level the most severe action wins (levels 4 and 5 mix two actions)
    order = {RiskAction.EMERGENCY_EXIT: 0, RiskAction.DEGRADED: 1, RiskAction.CONTROLLED_EXIT: 2,
             RiskAction.HEAL_DELTA: 3, RiskAction.PAUSE_SLICES: 4, RiskAction.NORMAL_EXIT: 5}
    action = min((t[1] for t in top), key=order.__getitem__)
    return RiskDecision(action, top_priority, tuple(t[2] for t in triggered), long_ratio, short_ratio)
