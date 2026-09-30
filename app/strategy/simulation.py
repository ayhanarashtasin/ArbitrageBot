"""Step 3 of the plan: run a selected pair through the bot's own rules on real history.

A replay of the pair's real hourly funding (Zero Delta) through the way the bot trades:
  - the position is built in equal slices across the 6-hour TWAP (at most $100 per slice), so funding is only earned
    on the part already open at each settlement;
  - each slice pays entry fees on both legs, plus an assumed slippage; closing pays them again;
  - with `rules` (the bot's settings) the trade can also END early, exactly as the engine would end it:
      each side is its own account holding notional / leverage x equity multiple (a true 4x account by default),
      marked to hourly prices of the underlying: either side at or below 30 % of its starting equity starts the
      3-hour controlled exit, either side below 10 % closes everything at once (the 1-minute emergency exit);
      the normal exit fires when the net funding edge stays at or below the floor for the confirmation time, or at
      the maximum holding time;
    without `rules` the position is simply held to the end of the window;
  - `start`/`end` pick the window, so a pair chosen on one week can be replayed on the FOLLOWING week (walk-forward),
    which is the only replay that says anything about the future.

Approximations, stated plainly: funding accrues hourly from Zero Delta's normalised rate (venues really settle every
1 to 8 hours); both legs are valued at the same underlying price (the basis between venues is ignored, so the hedge's
price P&L is zero and only the equity of each separate account moves); a slice fills at the hour's closing price;
the volatility wait and the 2 h spread rule need order books and 1-minute data that no history holds, so they are
applied only in the forward (paper) simulation; fees are configured assumptions and slippage is flat.
The percent-per-hour edge is used exactly as stored.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

D = Decimal
HOURS_PER_YEAR = D(8760)
BPS = D(10_000)
MAX_CURVE_POINTS = 240
H = timedelta(hours=1)


@dataclass(frozen=True)
class Bar:
    """One hour of the underlying's price (the candle that opens at the key time)."""

    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True)
class SimRules:
    leverage: Decimal
    equity_multiple: Decimal
    controlled_ratio: Decimal
    controlled_exit_hours: Decimal
    emergency_ratio: Decimal
    normal_exit_floor_per_hour: Decimal  # fraction per hour of leg notional
    normal_exit_confirm_minutes: int
    normal_exit_hours: Decimal
    max_hold_hours: int

    @classmethod
    def from_params(cls, p) -> "SimRules":
        return cls(leverage=p.target_leverage, equity_multiple=p.paper_account_equity_multiple,
                   controlled_ratio=p.controlled_exit_equity_ratio,
                   controlled_exit_hours=D(p.controlled_exit_duration_minutes) / 60,
                   emergency_ratio=p.emergency_exit_equity_ratio,
                   normal_exit_floor_per_hour=p.normal_exit_edge_floor_bps_per_hour / BPS,
                   normal_exit_confirm_minutes=p.normal_exit_confirm_minutes,
                   normal_exit_hours=D(p.normal_exit_duration_minutes) / 60, max_hold_hours=p.max_hold_hours)


@dataclass
class Simulation:
    hours: float  # span of the history that was replayed
    notional_usd: Decimal  # per leg
    slices: int
    entry_hours: Decimal
    funding_income: Decimal
    entry_fees: Decimal
    exit_fees: Decimal
    slippage_cost: Decimal
    net: Decimal
    gross_apr_pct: Decimal  # funding only, on one leg's notional, over the replayed window
    net_apr_pct: Decimal  # after every cost, same base
    breakeven_hours: float | None  # hours until funding has paid for the whole round trip; None = never in the window
    positive_hours_ratio: Decimal
    max_drawdown: Decimal
    curve: list[tuple[datetime, Decimal]] = field(default_factory=list)  # cumulative net; last point includes the exit
    exit_reason: str = "window_end"  # window_end | edge_decayed | max_hold | controlled_exit_30pct | emergency_exit_10pct
    exit_at: datetime | None = None
    held_hours: float = 0.0
    rules_applied: bool = False
    price_checked: bool = False  # hourly prices were available, so the 30 % / 10 % equity exits could fire
    leverage: Decimal | None = None
    equity_per_side_usd: Decimal | None = None
    min_long_equity_ratio: Decimal | None = None
    min_short_equity_ratio: Decimal | None = None
    max_adverse_move_pct: Decimal | None = None
    start: datetime | None = None
    end: datetime | None = None

    def as_dict(self) -> dict:
        step = max(1, -(-len(self.curve) // MAX_CURVE_POINTS))  # ceiling: never more than MAX_CURVE_POINTS
        curve = self.curve[::step]
        if self.curve and curve[-1] != self.curve[-1]:
            curve.append(self.curve[-1])

        def f(x):
            return None if x is None else float(x)

        return {
            "hours": self.hours, "notional_usd": float(self.notional_usd), "slices": self.slices,
            "entry_hours": float(self.entry_hours), "funding_income": float(self.funding_income),
            "entry_fees": float(self.entry_fees), "exit_fees": float(self.exit_fees),
            "slippage_cost": float(self.slippage_cost), "net": float(self.net),
            "gross_apr_pct": float(self.gross_apr_pct), "net_apr_pct": float(self.net_apr_pct),
            "breakeven_hours": self.breakeven_hours, "positive_hours_ratio": float(self.positive_hours_ratio),
            "max_drawdown": float(self.max_drawdown), "exit_reason": self.exit_reason,
            "exit_at": self.exit_at.isoformat() if self.exit_at else None, "held_hours": self.held_hours,
            "rules_applied": self.rules_applied, "price_checked": self.price_checked, "leverage": f(self.leverage),
            "equity_per_side_usd": f(self.equity_per_side_usd),
            "min_long_equity_ratio": f(self.min_long_equity_ratio),
            "min_short_equity_ratio": f(self.min_short_equity_ratio),
            "max_adverse_move_pct": f(self.max_adverse_move_pct),
            "start": self.start.isoformat() if self.start else None, "end": self.end.isoformat() if self.end else None,
            "curve": [{"at": t.isoformat(), "net": float(v)} for t, v in curve],
        }


def simulate_pair(
    long_rates: dict[datetime, Decimal],
    short_rates: dict[datetime, Decimal],
    *,
    notional_usd: Decimal,
    child_cap_usd: Decimal,
    entry_minutes: int,
    long_fee: Decimal,
    short_fee: Decimal,
    slippage_bps: Decimal,
    rules: SimRules | None = None,
    prices: dict[datetime, Bar] | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> Simulation:
    """Rates are Zero Delta's normalised funding in PERCENT per hour; fees are fractions (0.0005 = 5 bps).

    The hedge earns short - long, so the caller passes the legs already oriented (long = the cheaper side).
    `prices` maps an hour's open time to that hour's bar of the underlying.
    """
    times = sorted(t for t in long_rates.keys() & short_rates.keys()
                   if (start is None or t >= start) and (end is None or t <= end))
    if len(times) < 2:
        raise ValueError("need at least two shared hourly points to simulate")
    if (start is not None and times[0] != start) or (end is not None and times[-1] != end):
        raise ValueError("requested simulation window is not fully covered")
    if any(b - a != H for a, b in zip(times, times[1:])):
        raise ValueError("missing hourly funding; refusing to fill gaps")
    if notional_usd <= 0 or child_cap_usd <= 0 or entry_minutes <= 0:
        raise ValueError("notional, child cap and entry duration must be positive")
    n = max(1, int(-(-notional_usd // child_cap_usd)))  # ceil
    entry_hours = D(entry_minutes) / 60
    interval = entry_hours / n
    per_slice = notional_usd / n
    slip = slippage_bps / BPS
    leg_cost = {"long": long_fee + slip, "short": short_fee + slip}
    round_trip_rate = leg_cost["long"] + leg_cost["short"]  # both legs, one fill each
    total_costs = notional_usd * round_trip_rate * 2
    prices = prices or {}
    use_prices = rules is not None and all(t - H in prices for t in times)
    if not use_prices:
        prices = {}  # a partial price series cannot masquerade as an equity-risk simulation
    equity0 = notional_usd / rules.leverage * rules.equity_multiple if rules else None
    confirm = timedelta(minutes=rules.normal_exit_confirm_minutes) if rules else None

    def px(t: datetime) -> Decimal | None:
        """Price at time t: the close of the hour that ends at t (or the open of the one that starts there)."""
        bar = prices.get(t - H)
        return bar.close if bar else None  # never read the close of a candle that has not ended yet

    t0 = times[0]
    income, entry_paid, slices_done = D(0), D(0), 0
    fund = {"long": D(0), "short": D(0)}
    fees = {"long": D(0), "short": D(0)}
    qty, basis_usd, realized = D(0), D(0), D(0)  # base quantity per leg, USD paid for it, long-leg realised P&L
    exit_mode = exit_reason = None
    exit_start: datetime | None = None
    exit_hours = D(0)
    exit_from = D(0)  # exposure fraction when the current exit plan began
    exit_share = D(0)  # exposure fraction when the FIRST exit began: what the exit closes in total
    qty_at_exit = D(0)
    closed_at: datetime | None = None
    below_since: datetime | None = None
    positive, hours_seen = 0, 0
    min_ratio = {"long": D(1), "short": D(1)}
    max_move = D(0)
    curve: list[tuple[datetime, Decimal]] = []
    peak = worst = D(0)
    breakeven = None

    def exposure(t: datetime) -> Decimal:
        if exit_start is None:
            return D(slices_done) / n
        if exit_hours <= 0:
            return D(0)
        done = D((t - exit_start).total_seconds()) / 3600 / exit_hours
        return max(D(0), exit_from * (1 - done))

    def begin_exit(t: datetime, mode: str, reason: str, hours: Decimal) -> None:
        """Start (or, for an emergency during a slower exit, replace) the exit plan from the exposure held now."""
        nonlocal exit_mode, exit_reason, exit_start, exit_hours, exit_from, exit_share, qty_at_exit
        current = exposure(t)
        if exit_start is None:
            exit_share = current
        exit_from, qty_at_exit = current, qty
        exit_mode, exit_reason, exit_start, exit_hours = mode, reason, t, hours

    def open_at(t: datetime, elapsed: Decimal) -> None:
        nonlocal slices_done, entry_paid, qty, basis_usd
        if exit_start is not None:
            return
        while slices_done < min(n, int(elapsed // interval) + 1):
            slices_done += 1
            for leg in fees:
                fees[leg] += per_slice * leg_cost[leg]
            entry_paid += per_slice * round_trip_rate
            p = px(t)
            if p:
                qty += per_slice / p
                basis_usd += per_slice

    for k, t in enumerate(times):
        elapsed = D((t - t0).total_seconds()) / 3600
        if k == 0 or rules is None:
            open_at(t, elapsed)
        if k > 0:  # funding for the hour that just ended, paid on the position held at its end
            prev = times[k - 1]
            span_h = D((t - prev).total_seconds()) / 3600
            x = exposure(t)
            value = qty * px(t) if use_prices else x * notional_usd
            fund["long"] -= value * long_rates[prev] / 100 * span_h
            fund["short"] += value * short_rates[prev] / 100 * span_h
            income = fund["long"] + fund["short"]
            edge = (short_rates[prev] - long_rates[prev]) / 100
            positive += 1 if edge > 0 else 0
            hours_seen += 1
            if use_prices and qty > 0 and exit_mode != "emergency":
                bar = prices.get(prev)
                if bar is not None:
                    avg = basis_usd / qty
                    long_eq = equity0 + fund["long"] - fees["long"] + realized + qty * (bar.low - avg)
                    short_eq = equity0 + fund["short"] - fees["short"] - realized - qty * (bar.high - avg)
                    ratios = {"long": long_eq / equity0, "short": short_eq / equity0}
                    for leg in ratios:
                        min_ratio[leg] = min(min_ratio[leg], ratios[leg])
                    max_move = max(max_move, (avg - bar.low) / avg * 100, (bar.high - avg) / avg * 100)
                    lowest = min(ratios.values())
                    if lowest < rules.emergency_ratio:
                        begin_exit(t, "emergency", "emergency_exit_10pct", D(0))
                    elif exit_mode is None and lowest <= rules.controlled_ratio:
                        begin_exit(t, "controlled", "controlled_exit_30pct", rules.controlled_exit_hours)
            if exit_start is not None and qty > 0:
                x = exposure(t)
                target = qty_at_exit * (x / exit_from) if exit_from > 0 else D(0)
                sold = qty - target
                if sold > 0:
                    avg = basis_usd / qty
                    p = px(t)
                    if p:
                        realized += (p - avg) * sold
                    for leg in fees:
                        fees[leg] += sold * avg * leg_cost[leg]
                    basis_usd, qty = avg * target, target
            if rules is not None:
                # No fill at time t can benefit from (or suffer) an intrabar move BEFORE time t.
                open_at(t, elapsed)
        if rules is not None and exit_start is None and slices_done >= n and elapsed >= entry_hours:
            now_edge = (short_rates[t] - long_rates[t]) / 100
            if now_edge <= rules.normal_exit_floor_per_hour:
                below_since = below_since or t
                if t - below_since >= confirm:
                    begin_exit(t, "normal", "edge_decayed", rules.normal_exit_hours)
            else:
                below_since = None
            if exit_start is None and elapsed >= rules.max_hold_hours:
                begin_exit(t, "normal", "max_hold", rules.normal_exit_hours)
        net = income - entry_paid
        if exit_start is not None and exposure(t) <= 0:  # the exit finished: pay its costs and stop
            closed_at = t
            curve.append((t, net))
            break
        curve.append((t, net))
        peak = max(peak, net)
        worst = max(worst, peak - net)
        if breakeven is None and income >= total_costs:
            breakeven = float(elapsed)

    last = closed_at or times[-1]
    closed_share = exit_share if exit_start is not None else D(slices_done) / n
    exit_cost = closed_share * notional_usd * round_trip_rate
    final = income - entry_paid - exit_cost
    curve.append((last, final))
    peak = max(peak, max(c for _, c in curve[:-1]))
    worst = max(worst, peak - final)
    hours = (times[-1] - t0).total_seconds() / 3600
    years = D(hours) / HOURS_PER_YEAR
    traded = D(slices_done) * per_slice + closed_share * notional_usd  # USD traded per leg, entry plus exit
    return Simulation(
        hours=hours, notional_usd=notional_usd, slices=n, entry_hours=entry_hours, funding_income=income,
        entry_fees=D(slices_done) * per_slice * (long_fee + short_fee),
        exit_fees=closed_share * notional_usd * (long_fee + short_fee),
        slippage_cost=traded * slip * 2, net=final,
        gross_apr_pct=income / notional_usd / years * 100 if years else D(0),
        net_apr_pct=final / notional_usd / years * 100 if years else D(0),
        breakeven_hours=breakeven, positive_hours_ratio=D(positive) / D(max(1, hours_seen)),
        max_drawdown=worst, curve=curve, exit_reason=exit_reason or "window_end", exit_at=closed_at,
        held_hours=(last - t0).total_seconds() / 3600, rules_applied=rules is not None, price_checked=use_prices,
        leverage=rules.leverage if rules else None, equity_per_side_usd=equity0,
        min_long_equity_ratio=min_ratio["long"] if use_prices else None,
        min_short_equity_ratio=min_ratio["short"] if use_prices else None,
        max_adverse_move_pct=max_move if use_prices else None, start=t0, end=times[-1])
