"""Normal exit policy (project.md section 36). The spec leaves it undefined, so this is a labelled design choice.

`edge_decay` (paper default; live mode requires NORMAL_EXIT_POLICY to be set explicitly):
  exit when the current net funding per hour (short - long, from the venues themselves) has been at or below
  the floor for `confirm_minutes` without recovering, or when the trade has been open `max_hold_hours`.
Any other policy name is rejected at startup (fail closed) so a typo can never disable exits.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.core.trading_config import ResolvedParams

D = Decimal
KNOWN_POLICIES = ("edge_decay",)


class UnknownExitPolicy(ValueError):
    pass


@dataclass(frozen=True)
class ExitSignal:
    reason: str  # "edge_decayed" | "max_hold_reached"


class EdgeDecayMonitor:
    def __init__(self, params: ResolvedParams):
        if params.normal_exit_policy not in KNOWN_POLICIES:
            raise UnknownExitPolicy(f"NORMAL_EXIT_POLICY {params.normal_exit_policy!r} is not one of {KNOWN_POLICIES}")
        # the floor is configured in basis points per hour of leg notional; funding is a fraction per hour
        self.floor = params.normal_exit_edge_floor_bps_per_hour / D(10_000)
        self.confirm = timedelta(minutes=params.normal_exit_confirm_minutes)
        self.max_hold = timedelta(hours=params.max_hold_hours)
        self.below_since: datetime | None = None

    def update(self, now: datetime, opened_at: datetime, net_funding_per_hour: Decimal) -> ExitSignal | None:
        if now - opened_at >= self.max_hold:
            return ExitSignal("max_hold_reached")
        if net_funding_per_hour <= self.floor:
            if self.below_since is None:
                self.below_since = now
            if now - self.below_since >= self.confirm:
                return ExitSignal("edge_decayed")
        else:
            self.below_since = None  # recovered: the confirmation window restarts
        return None
