"""Executable-price math from real order books (project.md sections 18, 19, 21). Pure Decimal, no I/O.

Never uses the last traded price. A buy walks the ASK side, a sell walks the BID side.
"""

import math
from dataclasses import dataclass
from decimal import Decimal

from app.domain.market import OrderBook, Side

D = Decimal
BPS = D(10_000)


@dataclass(frozen=True)
class VwapResult:
    side: Side
    requested_notional: Decimal
    vwap: Decimal | None  # None when the book cannot absorb the size
    filled_notional: Decimal
    filled_quantity: Decimal
    best_price: Decimal | None
    slippage_bps: Decimal | None  # adverse move of the VWAP against the best price
    levels_used: int
    sufficient_depth: bool


def vwap_for_notional(book: OrderBook, side: Side, notional: Decimal) -> VwapResult:
    """VWAP to trade `notional` USD by walking the book. Partial levels are used pro rata."""
    levels = book.asks if side is Side.BUY else book.bids
    best = levels[0].price if levels else None
    remaining, qty, spent, used = notional, D(0), D(0), 0
    for level in levels:
        level_notional = level.price * level.quantity
        take = min(remaining, level_notional)
        if take <= 0:
            break
        qty += take / level.price
        spent += take
        remaining -= take
        used += 1
        if remaining <= 0:
            break
    ok = remaining <= 0 and qty > 0
    vwap = spent / qty if qty > 0 else None
    slip = None
    if ok and best:
        move = (vwap - best) if side is Side.BUY else (best - vwap)
        slip = move / best * BPS
    return VwapResult(side, notional, vwap if ok else None, spent, qty, best, slip, used, ok)


def depth_notional(book: OrderBook, side: Side, levels: int | None = None) -> Decimal:
    """Visible notional on the side that a trade of `side` would consume."""
    book_side = book.asks if side is Side.BUY else book.bids
    return sum((lv.price * lv.quantity for lv in book_side[:levels]), D(0))


def entry_spread(p_long_buy: Decimal, p_short_sell: Decimal) -> tuple[Decimal, Decimal]:
    """(entry_spread_pct, negative_spread_cost_pct) as fractions (section 18).

    entry_spread = (P_short_sell - P_long_buy) / P_ref, with P_ref the mean of the two prices.
    A negative spread means selling the short leg yields less than buying the long leg costs.
    """
    p_ref = (p_long_buy + p_short_sell) / 2
    spread = (p_short_sell - p_long_buy) / p_ref
    return spread, max(D(0), -spread)


def spread_rule(
    negative_spread_cost: Decimal,
    current_net_funding_per_hour: Decimal,
    median_net_funding_per_hour: Decimal | None,
    horizon_hours: Decimal,
) -> tuple[bool, Decimal]:
    """LOCKED: adverse spread must be covered by at most 8 h of 7-day median net funding.

    Funding rates are fractions per hour. Current funding must remain positive; the historical median
    determines how much adverse spread is acceptable. Missing/non-positive median history fails closed.
    """
    expected = D(0) if median_net_funding_per_hour is None else median_net_funding_per_hour * horizon_hours
    if current_net_funding_per_hour <= 0 or median_net_funding_per_hour is None or median_net_funding_per_hour <= 0:
        return False, expected
    return negative_spread_cost <= expected, expected


def expected_net_edge_bps(
    *,
    net_funding_per_hour: Decimal,
    hold_hours: Decimal,
    entry_fee_fraction_each_leg: tuple[Decimal, Decimal],
    exit_fee_fraction_each_leg: tuple[Decimal, Decimal],
    negative_spread_cost: Decimal,
    slippage_bps_entry: tuple[Decimal, Decimal],
    slippage_bps_exit_estimate: tuple[Decimal, Decimal],
    other_costs_fraction: Decimal = D(0),
) -> Decimal:
    """Section 20, in basis points of ONE leg's notional (both legs are sized equal).

    Expected Net Edge = funding income - entry fees - exit fees - negative entry basis
                        - slippage - other explicit costs.
    """
    income = net_funding_per_hour * hold_hours
    fees = sum(entry_fee_fraction_each_leg) + sum(exit_fee_fraction_each_leg)
    slippage = (sum(slippage_bps_entry) + sum(slippage_bps_exit_estimate)) / BPS
    return (income - fees - negative_spread_cost - slippage - other_costs_fraction) * BPS


def common_lot(step_a: Decimal, step_b: Decimal) -> Decimal:
    """Smallest quantity that is a whole number of lots on BOTH venues (the least common multiple of the steps).

    Delta neutrality (LOCKED): both legs of every slice get the SAME base quantity, so it must be tradable on each
    venue. Sizing each leg to its own step instead leaves an imbalance of up to one step per slice
    (0.001 BTC against 0.0012 BTC = 17 % of a $100 slice at $83k).
    """
    if step_a <= 0 or step_b <= 0:
        return max(step_a, step_b)
    places = max(-step_a.as_tuple().exponent, -step_b.as_tuple().exponent, 0)
    scale = D(10) ** places
    a, b = int(step_a * scale), int(step_b * scale)
    return D(a * b // math.gcd(a, b)) / scale


def equal_quantity(notional: Decimal, price_a: Decimal, price_b: Decimal, lot: Decimal) -> Decimal:
    """One base quantity for both legs, a whole number of `lot`, worth at most `notional` on EITHER venue."""
    ref = max(price_a, price_b)
    if ref <= 0 or lot <= 0:
        return D(0)
    return (notional / ref // lot) * lot


def leg_price_gap_pct(long_book: OrderBook, short_book: OrderBook) -> Decimal | None:
    """Percent gap between the two legs' mid prices, relative to their mean. None when a book side is empty.

    Two venues quoting the same underlying differ by the basis (normally well under 1 %). A large gap means the two
    markets are different assets that share a ticker, so an "equal quantity" hedge between them would not be one.
    """
    mids = []
    for book in (long_book, short_book):
        if book.best_bid is None or book.best_ask is None:
            return None
        mids.append((book.best_bid + book.best_ask) / 2)
    ref = (mids[0] + mids[1]) / 2
    return abs(mids[0] - mids[1]) / ref * 100 if ref > 0 else None
