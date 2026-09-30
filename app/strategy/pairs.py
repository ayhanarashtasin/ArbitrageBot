"""Candidate pair generation and seven-day analytics (project.md sections 13 and 14).

Pairs are built only from markets that already passed the locked volume and history filters.
A pair is generated when both legs are the requested underlying and both are LINEAR contracts.
No ranking formula exists in project.md, so the output is unranked; consumers may sort by any single
metric for display.

Not computed because Zero Delta's chart has no such series: basis metrics, provider score/rank.
These fields stay None and are never invented.
"""

from datetime import datetime
from decimal import Decimal
from itertools import combinations
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.core.config import ZeroDeltaSettings
from app.providers.zerodelta.history import analyze_series
from app.providers.zerodelta.models import (
    AssetAnalysis,
    HistoryCoverage,
    MarketEligibility,
    ZeroDeltaFundingObservation,
)
from app.providers.zerodelta.venues import VenueType

from .analytics import EdgeStats, compute_edge_stats
from .instruments import ContractKind, parse_market_identifier


class PairLeg(BaseModel):
    model_config = ConfigDict(frozen=True)

    venue_name: str
    venue_type: VenueType
    venue_type_verified: bool
    market_identifier: str
    settle_asset: str
    avg_24h_volume_usd: Decimal
    funding_interval_hours: Decimal
    current_funding_per_hour: Decimal  # percent per hour, from the current venue table


class PairAnalytics(BaseModel):
    model_config = ConfigDict(frozen=True)

    asset: str
    long_leg: PairLeg
    short_leg: PairLeg
    pair_type: str  # e.g. "CEX<->DEX", from the two venue types (UNKNOWN when unresolved)
    status: Literal["OK", "DATA_INSUFFICIENT"]
    reasons: tuple[str, ...] = ()
    # Both legs settle in different assets (e.g. USDT vs USDC): a collateral question for Phase 3.
    settlement_mismatch: bool
    contract_multiplier_verified: bool = False  # always False until direct venue metadata (Phase 3)
    coverage: HistoryCoverage
    # Edge = short funding/h - long funding/h, percent per hour.
    current_funding_edge: Decimal  # from the current venue table
    stats: EdgeStats | None  # None when DATA_INSUFFICIENT
    current_basis: Decimal | None = None
    mean_basis_7d: Decimal | None = None
    median_basis_7d: Decimal | None = None
    basis_stddev_7d: Decimal | None = None
    provider_score: Decimal | None = None
    provider_rank: int | None = None
    window_end: datetime | None = None


def _leg(m: MarketEligibility) -> PairLeg:
    s = m.snapshot
    inst = parse_market_identifier(s.market_identifier)
    return PairLeg(
        venue_name=s.venue_name,
        venue_type=s.venue_type,
        venue_type_verified=m.venue_type_verified,
        market_identifier=s.market_identifier,
        settle_asset=inst.settle if inst else "",
        avg_24h_volume_usd=s.avg_24h_volume_usd,
        funding_interval_hours=s.funding_interval_hours,
        current_funding_per_hour=s.normalized_funding_rate_per_hour,
    )


def _compatible(m: MarketEligibility, asset: str) -> bool:
    inst = parse_market_identifier(m.snapshot.market_identifier)
    return inst is not None and inst.base == asset.upper() and inst.kind is ContractKind.LINEAR


def _series(obs: tuple[ZeroDeltaFundingObservation, ...]) -> dict[datetime, Decimal]:
    return {o.timestamp: o.normalized_funding_rate_per_hour for o in obs}


def generate_pairs(analysis: AssetAnalysis, settings: ZeroDeltaSettings, *,
                   as_of: datetime | None = None) -> list[PairAnalytics]:
    """All compatible pairs of eligible markets, each oriented so the mean edge is >= 0.

    `as_of` rebuilds the pairs as they looked at that moment: only history up to it is used (so the 7-day window
    ends there) and each leg's "current" funding is its hourly value at that moment instead of today's snapshot.
    The walk-forward check uses it to run the selection a week ago and then see what the following week paid.
    Volume and eligibility are today's (Zero Delta gives no past volume).
    """
    eligible = [m for m in analysis.markets if m.eligible and _compatible(m, analysis.asset)]
    eligible.sort(key=lambda m: m.snapshot.venue_name.lower())
    out: list[PairAnalytics] = []

    def series(m: MarketEligibility) -> dict[datetime, Decimal]:
        full = _series(analysis.histories[m.snapshot.venue_name])
        return full if as_of is None else {t: v for t, v in full.items() if t <= as_of}

    for a, b in combinations(eligible, 2):
        sa, sb = series(a), series(b)
        if as_of is not None and not (sa and sb):
            continue
        common = sorted(sa.keys() & sb.keys())
        diff = [(ts, sb[ts] - sa[ts]) for ts in common]  # b short, a long
        coverage, window = analyze_series(
            diff,
            primary_days=settings.primary_lookback_days,
            min_days=settings.min_lookback_days,
            max_missing_intervals=settings.max_missing_intervals,
        )

        mean_ab = sum((v for _, v in window), Decimal(0)) / len(window) if window else Decimal(0)
        # Orient so the hedge earns on average: short the venue with the higher mean funding.
        long_m, short_m, sign = (a, b, 1) if mean_ab >= 0 else (b, a, -1)
        edges = [sign * v for _, v in window]
        stats = compute_edge_stats(edges) if coverage.status == "OK" else None

        long_leg, short_leg = _leg(long_m), _leg(short_m)
        if as_of is not None and window:
            at = window[-1][0]
            long_s, short_s = (sa, sb) if sign == 1 else (sb, sa)
            long_leg = long_leg.model_copy(update={"current_funding_per_hour": long_s[at]})
            short_leg = short_leg.model_copy(update={"current_funding_per_hour": short_s[at]})
        out.append(PairAnalytics(
            asset=analysis.asset,
            long_leg=long_leg,
            short_leg=short_leg,
            pair_type="<->".join(sorted((long_leg.venue_type.value, short_leg.venue_type.value))),
            status=coverage.status,
            reasons=coverage.reasons,
            settlement_mismatch=long_leg.settle_asset != short_leg.settle_asset,
            coverage=coverage,
            current_funding_edge=short_leg.current_funding_per_hour - long_leg.current_funding_per_hour,
            stats=stats,
            window_end=coverage.last_timestamp,
        ))
    return out
