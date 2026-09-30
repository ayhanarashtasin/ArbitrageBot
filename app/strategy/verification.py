"""Direct venue verification and the deterministic entry decision (project.md sections 15, 18-22, 43).

The decision is assembled from independent checks. Each check records what was measured and the
limit it was compared with, so a trade can always be explained afterwards (section 43).
No AI and no Zero Delta signal can approve an entry on its own: every check here reads direct venue
data, and a disagreement with Zero Delta blocks the trade (ZERO_DELTA_EXCHANGE_MISMATCH).
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from app.core.trading_config import ResolvedParams
from app.domain.market import FundingInfo, InstrumentMeta, OrderBook, Side, utcnow
from app.market_data.volatility import VolScore, entry_allowed
from app.strategy.executable import (
    VwapResult,
    leg_price_gap_pct,
    common_lot,
    depth_notional,
    equal_quantity,
    entry_spread,
    expected_net_edge_bps,
    spread_rule,
    vwap_for_notional,
)
from app.strategy.pairs import PairAnalytics

D = Decimal


class Outcome(StrEnum):
    ENTER = "ENTER"
    WAIT = "WAIT"  # temporary: try again (volatility, stale data)
    REJECT = "REJECT"
    MISMATCH = "EXCHANGE_MISMATCH"


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    value: str  # what was measured
    limit: str  # what it was compared with
    # "reject", "wait" or "mismatch": the outcome if this check fails. "info" is shown but never blocks.
    blocking: str = "reject"


@dataclass
class Decision:
    outcome: Outcome
    checks: list[Check] = field(default_factory=list)
    metrics: dict[str, str] = field(default_factory=dict)

    @property
    def reasons(self) -> list[str]:
        return [c.name for c in self.checks if not c.passed]


@dataclass(frozen=True)
class LegData:
    """Everything read directly from one venue for one leg."""

    meta: InstrumentMeta
    funding: FundingInfo
    book: OrderBook
    taker_fee: Decimal  # fraction
    fee_verified: bool
    settled_rate_per_hour: Decimal | None = None  # the venue's LAST SETTLED rate (fraction / h); None = not read


def _fmt(x: Decimal | None, places: int = 8) -> str:
    return "n/a" if x is None else f"{x:.{places}f}"


def zero_delta_mismatch(
    pair: PairAnalytics, long_funding: FundingInfo, short_funding: FundingInfo, tolerance_per_hour: Decimal,
    settled: tuple[Decimal | None, Decimal | None] = (None, None),
) -> list[Check]:
    """Compare Zero Delta's current funding (percent per interval) with the venue's own (fraction).

    Zero Delta shows the LAST SETTLED rate (live 2026-09-30: Binance ONEUSDT, Zero Delta -0.034797 %/h against the
    10:00 settlement -0.034536 %/h), while the venues' current rate is the running estimate for the NEXT settlement
    (-0.023171 %/h at the same moment). Comparing only with the estimate flagged every coin with fast-moving funding,
    i.e. the best opportunities, as a mismatch. The rate therefore agrees when it matches EITHER the venue's estimate
    OR its last settled rate. A wrong symbol, interval or sign still matches neither.
    """
    checks: list[Check] = []
    for label, leg, direct, last in (("long", pair.long_leg, long_funding, settled[0]),
                                     ("short", pair.short_leg, short_funding, settled[1])):
        interval_ok = leg.funding_interval_hours == direct.interval_hours
        checks.append(Check(
            f"{label}_funding_interval_matches", interval_ok,
            f"zerodelta={leg.funding_interval_hours}h direct={direct.interval_hours}h", "equal", "mismatch"))
        zd_per_hour = leg.current_funding_per_hour / 100  # percent -> fraction
        diff = abs(zd_per_hour - direct.rate_per_hour)
        diff_settled = None if last is None else abs(zd_per_hour - last)
        best = diff if diff_settled is None else min(diff, diff_settled)
        checks.append(Check(
            f"{label}_funding_rate_matches", best <= tolerance_per_hour,
            f"|zd-estimate|={_fmt(diff)} |zd-settled|={_fmt(diff_settled)} per hour", f"<= {tolerance_per_hour}",
            "mismatch"))
    return checks


def evaluate_entry(
    pair: PairAnalytics,
    long: LegData,
    short: LegData,
    vol: VolScore | None,
    params: ResolvedParams,
    *,
    child_notional: Decimal | None = None,
    now: datetime | None = None,
    zero_delta_age_seconds: float | None = None,
    earnings_block: str | None = None,
) -> Decision:
    """Run every gate for opening `pair` and return one auditable Decision."""
    now = now or utcnow()
    child = min(child_notional or params.twap_max_child_notional_usd, params.twap_max_child_notional_usd)
    checks: list[Check] = []
    metrics: dict[str, str] = {}

    def add(name, passed, value, limit, blocking="reject"):
        checks.append(Check(name, bool(passed), str(value), str(limit), blocking))

    # 0. Stocks: no entry within the earnings blackout (task spec). Crypto has no such condition.
    add("earnings_blackout", earnings_block is None, earnings_block or "clear", "no earnings within the window")

    # 0b. Zero Delta freshness and provider/direct agreement
    if zero_delta_age_seconds is not None:
        add("zero_delta_data_fresh", zero_delta_age_seconds <= params.zero_delta_max_age_seconds,
            f"{zero_delta_age_seconds:.0f}s", f"<= {params.zero_delta_max_age_seconds}s", "wait")
    checks += zero_delta_mismatch(pair, long.funding, short.funding, params.zero_delta_mismatch_tolerance_per_hour,
                                  (long.settled_rate_per_hour, short.settled_rate_per_hour))

    # 1. Tradability and metadata
    for label, leg in (("long", long), ("short", short)):
        add(f"{label}_tradable", leg.meta.is_tradable, leg.meta.is_tradable, "True")
        add(f"{label}_linear_perpetual", leg.meta.is_linear and leg.meta.is_perpetual,
            f"linear={leg.meta.is_linear} perpetual={leg.meta.is_perpetual}", "both True")
        add(f"{label}_child_meets_min_notional",
            leg.meta.min_notional is None or child >= leg.meta.min_notional,
            child, f">= {leg.meta.min_notional}")

    # 2. Fresh books (never trade from stale data)
    for label, leg in (("long", long), ("short", short)):
        age = leg.book.age_ms(now)
        add(f"{label}_book_fresh", age <= params.max_market_data_age_ms,
            f"{age:.0f}ms", f"<= {params.max_market_data_age_ms}ms", "wait")

    # 2b. Both legs must be the SAME underlying: a ticker shared by two assets is not a hedge
    gap = leg_price_gap_pct(long.book, short.book)
    add("legs_track_same_underlying", gap is not None and gap <= params.max_leg_price_gap_pct,
        "n/a" if gap is None else f"{gap:.3f}%", f"<= {params.max_leg_price_gap_pct}%")

    # 3. Executable VWAP for one child slice: long buys asks, short sells bids
    lv: VwapResult = vwap_for_notional(long.book, Side.BUY, child)
    sv: VwapResult = vwap_for_notional(short.book, Side.SELL, child)
    add("long_book_depth_sufficient", lv.sufficient_depth, f"{lv.filled_notional}", f">= {child}")
    add("short_book_depth_sufficient", sv.sufficient_depth, f"{sv.filled_notional}", f">= {child}")
    for label, leg, res, side in (("long", long, lv, Side.BUY), ("short", short, sv, Side.SELL)):
        depth = depth_notional(leg.book, side, 20)
        need = child * params.min_required_book_depth_multiplier
        add(f"{label}_depth_multiplier", depth >= need, f"{depth:.2f}", f">= {need}")
        if res.slippage_bps is not None:
            add(f"{label}_slippage", res.slippage_bps <= params.max_allowed_slippage_bps,
                f"{res.slippage_bps:.3f}bps", f"<= {params.max_allowed_slippage_bps}bps")

    if lv.vwap is None or sv.vwap is None:
        return Decision(Outcome.REJECT, checks, metrics)

    # 3b. Delta neutrality (LOCKED): both legs must be able to hold the SAME quantity within the child cap
    lot = common_lot(long.meta.qty_step, short.meta.qty_step)
    q = equal_quantity(child, lv.vwap, sv.vwap, lot)
    fits = (q > 0 and q >= long.meta.min_qty and q >= short.meta.min_qty
            and (long.meta.min_notional is None or q * lv.vwap >= long.meta.min_notional)
            and (short.meta.min_notional is None or q * sv.vwap >= short.meta.min_notional))
    add("legs_can_be_sized_equally", fits, f"{q} per slice (lot {lot})",
        f"> 0, >= both venues' minimums, <= ${child} on each leg")

    # 4. Spread rule (LOCKED) on real executable prices
    spread, neg_cost = entry_spread(lv.vwap, sv.vwap)
    horizon_h = D(params.funding_screening_horizon_minutes) / 60
    long_h, short_h = long.funding.rate_per_hour, short.funding.rate_per_hour
    passes, expected_2h = spread_rule(neg_cost, short_h, long_h, horizon_h)
    add("net_funding_positive_over_2h", expected_2h > 0, _fmt(expected_2h), "> 0")
    add("negative_spread_within_2h_funding", passes, f"cost={_fmt(neg_cost)}", f"<= {_fmt(expected_2h)}")

    # Live orders may only be sized on fees read from the venue's account API (section 20). Paper may assume.
    if params.run_mode == "live":
        add("fees_verified", long.fee_verified and short.fee_verified,
            f"long={long.fee_verified} short={short.fee_verified}", "both True")

    # 5. Net edge after fees and slippage (fees are configured assumptions until verified: Phase 8)
    net_h = short_h - long_h
    conservative_h = min(net_h, pair.stats.mean / 100) if pair.stats else net_h  # ZD stats are percent per hour
    net_edge = expected_net_edge_bps(
        net_funding_per_hour=conservative_h,
        hold_hours=params.expected_hold_hours,
        entry_fee_fraction_each_leg=(long.taker_fee, short.taker_fee),
        exit_fee_fraction_each_leg=(long.taker_fee, short.taker_fee),
        negative_spread_cost=neg_cost,
        slippage_bps_entry=(lv.slippage_bps or D(0), sv.slippage_bps or D(0)),
        slippage_bps_exit_estimate=(lv.slippage_bps or D(0), sv.slippage_bps or D(0)),
    )
    add("expected_net_edge", net_edge >= params.min_expected_net_edge_bps,
        f"{net_edge:.2f}bps", f">= {params.min_expected_net_edge_bps}bps")

    # 6. Volatility filter (LOCKED, 45): a failure means WAIT, not reject
    if vol is None or not vol.available:
        add("volatility_score_available", False, vol.reason if vol else "not computed", "available", "wait")
    else:
        add("low_vol_score", entry_allowed(vol, params.low_vol_max_percentile),
            f"{vol.low_vol_score:.1f} (rv {vol.underlying_rv_percentile:.1f}, basis {vol.basis_vol_percentile:.1f})",
            f"<= {params.low_vol_max_percentile}", "wait")

    metrics.update({
        "p_long_buy": str(lv.vwap), "p_short_sell": str(sv.vwap),
        "entry_spread_pct": _fmt(spread * 100), "negative_spread_cost_pct": _fmt(neg_cost * 100),
        "expected_net_funding_2h": _fmt(expected_2h), "net_funding_per_hour": _fmt(net_h),
        "expected_net_edge_bps": f"{net_edge:.4f}",
        "long_taker_fee": str(long.taker_fee), "short_taker_fee": str(short.taker_fee),
        "fees_verified": str(long.fee_verified and short.fee_verified),
        "long_slippage_bps": _fmt(lv.slippage_bps, 4), "short_slippage_bps": _fmt(sv.slippage_bps, 4),
    })
    if vol and vol.available:
        metrics["low_vol_score"] = f"{vol.low_vol_score:.2f}"

    failed = [c for c in checks if not c.passed and c.blocking != "info"]
    if not failed:
        return Decision(Outcome.ENTER, checks, metrics)
    if any(c.blocking == "mismatch" for c in failed):
        return Decision(Outcome.MISMATCH, checks, metrics)
    if any(c.blocking == "reject" for c in failed):
        return Decision(Outcome.REJECT, checks, metrics)
    return Decision(Outcome.WAIT, checks, metrics)
