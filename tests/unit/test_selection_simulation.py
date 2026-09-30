from datetime import timedelta
from decimal import Decimal

import pytest

from app.strategy.selection import rank, score, select, why_not_tradable
from app.strategy.simulation import simulate_pair
from tests.unit.helpers import T0, make_pair_analytics

D = Decimal


def hourly(pct_per_hour, n=169):
    """Constant normalised funding, percent per hour, on n hourly points."""
    return {T0 + timedelta(hours=h): D(str(pct_per_hour)) for h in range(n)}


def sim(long_pct, short_pct, *, points=169, **kw):
    args = dict(notional_usd=D(1000), child_cap_usd=D(100), entry_minutes=360, long_fee=D("0.0005"),
                short_fee=D("0.0005"), slippage_bps=D(0))
    args.update(kw)
    return simulate_pair(hourly(long_pct, points), hourly(short_pct, points), **args)


# ---- selection -------------------------------------------------------------------------------

def pair(name_l, name_s, mean, consistency=1.0, asset="BTC"):
    p = make_pair_analytics(name_l, name_s, mean_pct=mean, asset=asset)
    st = p.stats.model_copy(update={"direction_consistency_ratio": D(str(consistency)), "median": D(str(mean))})
    return p.model_copy(update={"stats": st})


ADAPTERS = {"A": object(), "B": object(), "C": object()}
lookup = ADAPTERS.get


def test_score_is_the_annualised_mean_or_median():
    p = pair("A", "B", "0.01")
    assert score(p, "mean") == D("0.01") * 8760 == D("87.60")
    p2 = p.model_copy(update={"stats": p.stats.model_copy(update={"median": D("0.02")})})
    assert score(p2, "median") == D("175.20") and score(p2, "mean") == D("87.60")


def test_rank_orders_by_edge_then_steadiness_and_drops_unusable_pairs():
    a, b, c = pair("A", "B", "0.03"), pair("A", "C", "0.05", 0.7), pair("B", "C", "0.05", 0.9)
    negative = pair("B", "A", "-0.01")
    bad = pair("A", "C", "0.9").model_copy(update={"status": "DATA_INSUFFICIENT", "stats": None})
    order = rank([a, b, c, negative, bad], "mean")
    assert [(p.long_leg.venue_name, p.short_leg.venue_name) for p in order] == [("B", "C"), ("A", "C"), ("A", "B")]


def test_selection_names_exchanges_and_keeps_only_n():
    pairs = [pair("A", "B", "0.05"), pair("A", "C", "0.04"), pair("B", "C", "0.03"), pair("A", "X", "0.09")]
    overall, tradable = select(pairs, 3, "mean", lookup)
    assert [p.pair.short_leg.venue_name for p in overall] == ["X", "B", "C"] and [p.rank for p in overall] == [1, 2, 3]
    assert overall[0].tradable is False and "no adapter yet for X" in overall[0].why_not
    assert all(p.tradable for p in tradable) and [p.pair.short_leg.venue_name for p in tradable] == ["B", "C", "C"]
    assert [p.rank for p in tradable] == [1, 2, 3]  # ranked among themselves


def test_a_best_pair_that_cannot_be_traded_never_reaches_the_tradable_list():
    _, tradable = select([pair("A", "X", "0.9"), pair("A", "B", "0.01")], 1, "mean", lookup)
    assert len(tradable) == 1 and tradable[0].pair.short_leg.venue_name == "B"


def test_same_exchange_pair_is_not_tradable():
    shared = object()
    assert why_not_tradable(pair("A", "B", "0.1"), {"A": shared, "B": shared}.get) == "both legs are on the same exchange"


def test_count_larger_than_available_and_empty_input():
    assert select([], 3, "mean", lookup) == ([], [])
    overall, tradable = select([pair("A", "B", "0.05")], 3, "mean", lookup)
    assert len(overall) == len(tradable) == 1


# ---- simulation (hand-checked) ---------------------------------------------------------------

def test_no_edge_no_fees_earns_nothing():
    r = sim(0.01, 0.01, long_fee=D(0), short_fee=D(0))
    assert r.funding_income == 0 and r.net == 0


def test_full_exposure_income_is_edge_times_notional_times_hours():
    # 10 slices of $100 over 6 h: exposure ramps 10 %, 20 %... only during the entry. Use a single slice: full from hour 0.
    r = sim(0.00, 0.01, notional_usd=D(100), long_fee=D(0), short_fee=D(0))
    assert r.slices == 1
    # 168 hours x $100 x 0.01 % per hour = $1.68
    assert r.funding_income == D("100") * D("0.0001") * 168 == D("1.68")
    assert r.net == r.funding_income  # no costs
    assert float(r.gross_apr_pct) == pytest.approx(87.6)  # 0.01 %/h x 8760
    assert r.positive_hours_ratio == 1


def test_entry_ramp_earns_less_than_full_exposure():
    ramped = sim(0.00, 0.01, long_fee=D(0), short_fee=D(0))
    full = D(1000) * D("0.0001") * 168
    assert ramped.slices == 10  # $1000 in slices of at most $100
    # 10 slices spread over 6 h open at 0, 36, 72 ... min. Funding is paid on the position held at each hourly settlement:
    # 20 %, 40 %, 60 %, 70 %, 90 %, 100 % at hours 1 to 6, so 0.8 + 0.6 + 0.4 + 0.3 + 0.1 = 2.2 hours of full exposure are missed.
    assert full - ramped.funding_income == D(1000) * D("0.0001") * D("2.2") == D("0.22")


def test_fees_and_slippage_arithmetic():
    r = sim(0.00, 0.01, long_fee=D("0.0005"), short_fee=D("0.0005"), slippage_bps=D(2))
    # each fill costs (5 + 5 + 2 + 2) bps = 14 bps of its notional; entry $1000 and exit $1000
    assert r.entry_fees + r.exit_fees == D(1000) * D("0.0010") * 2
    assert r.slippage_cost == D(1000) * D("0.0004") * 2 * 1  # 2 bps x 2 legs on entry and on exit
    total_costs = D(1000) * D("0.0014") * 2
    assert r.net == r.funding_income - total_costs


def test_breakeven_time_and_none_when_never_paid_back():
    good = sim(0.00, 0.05)  # 0.05 %/h on $1000 = $0.5/h; costs are $2 round trip
    assert good.breakeven_hours is not None and good.breakeven_hours < 20
    poor = sim(0.00, 0.0005)
    assert poor.breakeven_hours is None and poor.net < 0


def test_negative_edge_loses_money_and_reports_no_positive_hours():
    r = sim(0.02, 0.0)
    assert r.funding_income < 0 and r.net < r.funding_income and r.positive_hours_ratio == 0


def test_curve_ends_at_the_final_net_and_drawdown_is_measured():
    r = sim(0.00, 0.001)
    assert r.curve[-1][1] == r.net and r.curve[0][0] == T0
    assert r.max_drawdown >= 0
    assert r.curve[-2][1] - r.curve[-1][1] == pytest.approx(float(D(1000) * D("0.001")), rel=1e-6)  # the exit cost step


def test_as_dict_is_json_ready_and_downsamples_long_curves():
    long = simulate_pair(hourly(0, 2000), hourly(0.01, 2000), notional_usd=D(100), child_cap_usd=D(100),
                         entry_minutes=360, long_fee=D(0), short_fee=D(0), slippage_bps=D(0))
    d = long.as_dict()
    assert len(d["curve"]) <= 245 and d["curve"][-1]["net"] == d["net"]
    import json
    json.dumps(d)


def test_needs_two_shared_points():
    with pytest.raises(ValueError):
        simulate_pair({T0: D(0)}, {T0: D(0)}, notional_usd=D(100), child_cap_usd=D(100), entry_minutes=360,
                      long_fee=D(0), short_fee=D(0), slippage_bps=D(0))
    with pytest.raises(ValueError):
        simulate_pair(hourly(0, 5), {T0 + timedelta(days=9): D(0)}, notional_usd=D(100), child_cap_usd=D(100),
                      entry_minutes=360, long_fee=D(0), short_fee=D(0), slippage_bps=D(0))
