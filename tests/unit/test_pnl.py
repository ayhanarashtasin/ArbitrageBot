from decimal import Decimal

from app.accounting.pnl import FundingRecord, OrderRecord, compute_pnl

D = Decimal


def order(leg, side, qty, price, fee="0", ref=None):
    return OrderRecord(leg, side, D(str(qty)), D(str(price)), D(str(fee)), None if ref is None else D(str(ref)))


def test_flat_hedge_earns_funding_minus_fees():
    orders = [order("long", "BUY", 10, 100, "0.5"), order("short", "SELL", 10, 100, "0.5"),
              order("long", "SELL", 10, 100, "0.5"), order("short", "BUY", 10, 100, "0.5")]
    pnl = compute_pnl(orders, [FundingRecord("short", D("3.0"), D("3.0")), FundingRecord("long", D("-1.0"), D("-1.0"))])
    assert pnl.hedge_trading == 0 and pnl.fees == 2
    assert pnl.funding_received == 3 and pnl.funding_paid == 1 and pnl.funding_net == 2
    assert pnl.total == 0  # 2 funding - 2 fees


def test_price_move_cancels_between_the_legs():
    # both legs close 5 higher: long gains 50, short loses 50
    orders = [order("long", "BUY", 10, 100), order("short", "SELL", 10, 100),
              order("long", "SELL", 10, 105), order("short", "BUY", 10, 105)]
    pnl = compute_pnl(orders, [])
    assert pnl.long_trading == 50 and pnl.short_trading == -50 and pnl.total == 0


def test_open_residual_is_marked_and_reported():
    orders = [order("long", "BUY", 10, 100), order("short", "SELL", 10, 100)]
    pnl = compute_pnl(orders, [], residual_marks={"long": D(102), "short": D(102)})
    assert pnl.long_trading == -1000 + 1020 and pnl.short_trading == 1000 - 1020
    assert pnl.unrealized_residual == 0  # +1020 (long) and -1020 (short) cancel in the residual line


def test_missing_mark_flags_incomplete_pnl():
    pnl = compute_pnl([order("long", "BUY", 1, 100)], [])
    assert any("no mark price" in n for n in pnl.notes)


def test_slippage_is_an_estimate_not_double_counted():
    orders = [order("long", "BUY", 10, "100.05", ref=100), order("short", "SELL", 10, "99.95", ref=100),
              order("long", "SELL", 10, "100.05"), order("short", "BUY", 10, "99.95")]
    pnl = compute_pnl(orders, [])
    assert pnl.slippage_estimate == D("0.5") + D("0.5")  # 0.05 x 10 on each of two entry orders
    assert pnl.total == pnl.hedge_trading  # slippage is already inside the trading numbers


def test_funding_difference_vs_actual_is_tracked():
    pnl = compute_pnl([], [FundingRecord("short", D("5"), D("4.8")), FundingRecord("long", D("-2"), None)])
    assert pnl.funding_expected_vs_actual_difference == D("-0.2")
    assert pnl.funding_net == D("4.8") - 2  # unreconciled funding falls back to the expected amount
    assert any("not reconciled" in n for n in pnl.notes)


def test_report_has_all_separate_lines():
    keys = compute_pnl([], []).as_dict().keys()
    assert {"funding_pnl", "hedge_trading_pnl", "fees", "slippage_estimate", "total_realized_pnl"} <= set(keys)
