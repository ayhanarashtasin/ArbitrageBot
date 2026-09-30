from decimal import Decimal

import pytest

from app.domain.market import Side
from app.strategy.executable import (
    depth_notional,
    entry_spread,
    expected_net_edge_bps,
    spread_rule,
    vwap_for_notional,
)

from .helpers import book

D = Decimal


def test_buy_walks_asks_and_sell_walks_bids():
    b = book(bids=[(99, 1), (98, 1)], asks=[(101, 1), (102, 1)])
    buy = vwap_for_notional(b, Side.BUY, D(101))
    assert buy.vwap == D(101) and buy.best_price == D(101) and buy.slippage_bps == 0 and buy.levels_used == 1
    sell = vwap_for_notional(b, Side.SELL, D(99))
    assert sell.vwap == D(99) and sell.slippage_bps == 0


def test_vwap_across_two_levels():
    b = book(bids=[(100, 1)], asks=[(100, 1), (102, 5)])
    r = vwap_for_notional(b, Side.BUY, D(201))  # 1 unit at 100, then 101/102 units at 102
    assert r.sufficient_depth and r.levels_used == 2
    expected = D(201) / (D(1) + D(101) / D(102))
    assert r.vwap == expected
    assert r.slippage_bps == (expected - 100) / 100 * 10000


def test_sell_slippage_is_adverse_direction():
    b = book(bids=[(100, 1), (98, 5)], asks=[(101, 1)])
    r = vwap_for_notional(b, Side.SELL, D(198))
    assert r.vwap < 100 and r.slippage_bps > 0


def test_insufficient_depth_has_no_vwap():
    b = book(bids=[(100, 1)], asks=[(100, 1)])
    r = vwap_for_notional(b, Side.BUY, D(500))
    assert not r.sufficient_depth and r.vwap is None and r.filled_notional == 100


def test_empty_book_side():
    assert vwap_for_notional(book(bids=[], asks=[]), Side.BUY, D(10)).vwap is None


def test_depth_notional():
    b = book(bids=[(10, 2), (9, 2)], asks=[(11, 1), (12, 1)])
    assert depth_notional(b, Side.BUY) == 23  # asks: 11 + 12
    assert depth_notional(b, Side.SELL) == 38
    assert depth_notional(b, Side.SELL, levels=1) == 20


def test_entry_spread_signs():
    spread, cost = entry_spread(D(100), D("100.2"))
    assert spread > 0 and cost == 0
    spread, cost = entry_spread(D(100), D("99.8"))
    ref = (D(100) + D("99.8")) / 2
    assert spread == D("-0.2") / ref and cost == -spread


@pytest.mark.parametrize("cost,ok", [("0.0001", True), ("0.0002", True), ("0.00020001", False)])
def test_two_hour_rule_boundaries(cost, ok):
    # net funding per hour = 0.0001 - 0.0, so two hours = 0.0002
    passes, expected = spread_rule(D(cost), D("0.0001"), D(0), D(2))
    assert expected == D("0.0002") and passes is ok


@pytest.mark.parametrize("short,long", [("0.0001", "0.0001"), ("0.0001", "0.0002"), ("-0.0001", "0")])
def test_non_positive_expected_funding_rejects(short, long):
    passes, expected = spread_rule(D(0), D(short), D(long), D(2))
    assert expected <= 0 and passes is False


def test_net_edge_arithmetic():
    edge = expected_net_edge_bps(
        net_funding_per_hour=D("0.00005"), hold_hours=D(72),  # income 0.0036 = 36 bps
        entry_fee_fraction_each_leg=(D("0.0005"), D("0.0005")),  # 10 bps
        exit_fee_fraction_each_leg=(D("0.0005"), D("0.0005")),  # 10 bps
        negative_spread_cost=D("0.0001"),  # 1 bp
        slippage_bps_entry=(D(1), D(1)), slippage_bps_exit_estimate=(D(1), D(1)),  # 4 bps
    )
    assert edge == D(36) - D(20) - D(1) - D(4)
