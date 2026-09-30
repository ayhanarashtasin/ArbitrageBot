from decimal import Decimal


from app.core.trading_config import TradingSettings
from app.market_data.volatility import VolScore
from app.providers.zerodelta.models import HistoryCoverage
from app.providers.zerodelta.venues import VenueType
from app.strategy.analytics import EdgeStats
from app.strategy.pairs import PairAnalytics, PairLeg
from app.strategy.verification import LegData, Outcome, evaluate_entry

from .helpers import T0, book, deep_book, funding, meta

D = Decimal
PARAMS = TradingSettings(_env_file=None).resolve()


def leg(name, zd_pct_per_hour):
    return PairLeg(venue_name=name, venue_type=VenueType.CEX, venue_type_verified=True,
                   market_identifier="BTC/USDT:USDT", settle_asset="USDT", avg_24h_volume_usd=D(50_000_000),
                   funding_interval_hours=D(8), current_funding_per_hour=D(str(zd_pct_per_hour)))


def make_pair(long_pct=0.0, short_pct=0.03, mean_pct=0.03):
    stats = EdgeStats(mean=D(str(mean_pct)), median=D(str(mean_pct)), minimum=D(0), maximum=D(1), stddev=D(0),
                      positive_edge_ratio=D(1), direction_consistency_ratio=D(1), sign_flip_count=0,
                      longest_positive_edge_streak=169, latest=D(str(mean_pct)))
    cov = HistoryCoverage(requested_start=None, requested_end=None, first_timestamp=None, last_timestamp=None,
                          coverage_seconds=168 * 3600, observation_count=169,
                          expected_observation_count_if_known=169, missing_intervals=0, duplicate_count=0,
                          out_of_order_count=0, status="OK")
    return PairAnalytics(asset="BTC", long_leg=leg("L", long_pct), short_leg=leg("S", short_pct),
                         pair_type="CEX<->CEX", status="OK", settlement_mismatch=False, coverage=cov,
                         current_funding_edge=D(str((short_pct - long_pct) / 100)), stats=stats)


def data(rate_per_hour, book_=None, *, fee="0.0005", **meta_kw):
    return LegData(meta=meta(**meta_kw), funding=funding(rate_per_hour), book=book_ or deep_book(now=T0),
                   taker_fee=D(fee), fee_verified=False)


CALM = VolScore(True, D(20), D(30), D(30), 1000, 1000)


def run(long=None, short=None, vol=CALM, pair=None, **kw):
    return evaluate_entry(pair or make_pair(), long or data("0"), short or data("0.0003"), vol, PARAMS, now=T0, **kw)


def failed(decision):
    return set(decision.reasons)


def test_happy_path_enters_and_records_metrics():
    d = run()
    assert d.outcome is Outcome.ENTER and d.reasons == []
    assert D(d.metrics["expected_net_edge_bps"]) > PARAMS.min_expected_net_edge_bps
    assert d.metrics["fees_verified"] == "False"
    assert "p_long_buy" in d.metrics and "low_vol_score" in d.metrics


def test_volatility_above_45_waits_not_rejects():
    d = run(vol=VolScore(True, D(46), D(10), D(46), 1000, 1000))
    assert d.outcome is Outcome.WAIT and failed(d) == {"low_vol_score"}


def test_volatility_exactly_45_passes():
    assert run(vol=VolScore(True, D(45), D(10), D(45), 1000, 1000)).outcome is Outcome.ENTER


def test_missing_volatility_data_waits():
    assert run(vol=None).outcome is Outcome.WAIT
    assert run(vol=VolScore(False, None, None, None, 0, 0, "few candles")).outcome is Outcome.WAIT


def test_stale_book_waits():
    stale = deep_book(now=T0, age_ms=5000)
    d = run(long=data("0", stale))
    assert d.outcome is Outcome.WAIT and "long_book_fresh" in failed(d)


def test_book_at_the_age_limit_is_fresh():
    at_limit = deep_book(now=T0, age_ms=PARAMS.max_market_data_age_ms)
    assert run(short=data("0.0003", at_limit)).outcome is Outcome.ENTER


def test_zero_delta_funding_mismatch_blocks_trade():
    d = run(pair=make_pair(short_pct=0.30, mean_pct=0.03))  # ZD says 0.30 %/h, venue says 0.03 %/h
    assert d.outcome is Outcome.MISMATCH and "short_funding_rate_matches" in failed(d)


def test_zero_delta_interval_mismatch_blocks_trade():
    pair = make_pair()
    pair = pair.model_copy(update={"long_leg": pair.long_leg.model_copy(update={"funding_interval_hours": D(1)})})
    d = run(pair=pair)
    assert d.outcome is Outcome.MISMATCH and "long_funding_interval_matches" in failed(d)


def test_mismatch_outranks_reject_and_wait():
    d = run(pair=make_pair(short_pct=0.30), vol=None, long=data("0", deep_book(now=T0, age_ms=9999)))
    assert d.outcome is Outcome.MISMATCH


def test_reject_outranks_wait():
    d = run(vol=None, long=data("0", deep_book(now=T0, size=0.001)))  # thin book AND no vol data
    assert d.outcome is Outcome.REJECT


def test_negative_spread_larger_than_two_hours_of_funding_rejects():
    wide_short = book(bids=[(99.5 - i * 0.01, 100) for i in range(20)], asks=[(99.6 + i * 0.01, 100) for i in range(20)])
    d = run(short=data("0.0003", wide_short))
    assert d.outcome is Outcome.REJECT and "negative_spread_within_2h_funding" in failed(d)


def test_non_positive_net_funding_rejects():
    d = run(long=data("0.0003"), short=data("0.0003"), pair=make_pair(long_pct=0.03, short_pct=0.03))
    assert d.outcome is Outcome.REJECT and "net_funding_positive_over_2h" in failed(d)


def test_insufficient_depth_rejects():
    d = run(long=data("0", deep_book(now=T0, size="0.01")))  # about $20 of visible depth for a $100 slice
    assert d.outcome is Outcome.REJECT and "long_book_depth_sufficient" in failed(d)


def test_depth_multiplier_enforced():
    # $100 needs 5x = $500 visible in 20 levels: 20 levels of 0.2 units at 100 is only $400
    d = run(long=data("0", deep_book(now=T0, size="0.2")))
    assert d.outcome is Outcome.REJECT and "long_depth_multiplier" in failed(d)


def test_slippage_limit_enforced():
    steep = book(bids=[(99.99, 100)], asks=[(100.0 + i * 0.05, 0.2) for i in range(40)])  # walks up the book
    d = run(long=data("0", steep))
    assert d.outcome is Outcome.REJECT and ({"long_slippage", "long_depth_multiplier"} & failed(d))


def test_thin_edge_after_fees_rejects_once_the_threshold_is_set():
    tight = deep_book(now=T0, spread=D("0.004"))  # keeps the 2h spread rule satisfied
    explicit = TradingSettings(_env_file=None, min_expected_net_edge_bps=10).resolve()
    d = evaluate_entry(make_pair(short_pct=0.003, mean_pct=0.003), data("0", tight), data("0.00003", tight), CALM,
                       explicit, now=T0)
    assert d.outcome is Outcome.REJECT and failed(d) == {"expected_net_edge"}


def test_thin_edge_keeps_the_standard_gate_in_paper_and_live():
    tight = deep_book(now=T0, spread=D("0.004"))
    d = run(long=data("0", tight), short=data("0.00003", tight), pair=make_pair(short_pct=0.003, mean_pct=0.003))
    check = next(c for c in d.checks if c.name == "expected_net_edge")
    assert d.outcome is Outcome.REJECT and not check.passed and check.blocking == "reject"
    live_like = PARAMS.model_copy(update={"run_mode": "live"})
    d = evaluate_entry(make_pair(short_pct=0.003, mean_pct=0.003), data("0", tight), data("0.00003", tight), CALM,
                       live_like, now=T0)
    assert d.outcome is Outcome.REJECT and "expected_net_edge" in failed(d)


def test_earnings_blackout_rejects_and_names_the_date():
    d = run(earnings_block="TSLA reports earnings on 2026-10-28 (within 3 days)")
    assert d.outcome is Outcome.REJECT and failed(d) == {"earnings_blackout"}
    assert "2026-10-28" in next(c.value for c in d.checks if c.name == "earnings_blackout")


def test_legs_that_cannot_hold_the_same_quantity_reject():
    # one lot of 0.01 at price 100 is $1 on the long venue; the short venue only trades 3 units (3 x $100 > $100 cap)
    d = run(short=data("0.0003", qty_step="3", min_qty="3"))
    assert d.outcome is Outcome.REJECT and "legs_can_be_sized_equally" in failed(d)
    ok = next(c for c in run().checks if c.name == "legs_can_be_sized_equally")
    assert ok.passed and ok.value.startswith("0.999")


def test_untradable_or_inverse_or_tiny_child_rejects():
    assert "long_tradable" in failed(run(long=data("0", tradable=False)))
    assert "short_linear_perpetual" in failed(run(short=data("0.0003", linear=False)))
    assert "long_child_meets_min_notional" in failed(run(long=data("0", min_notional="500")))


def test_child_size_is_capped_at_the_locked_100():
    d = run(child_notional=D(5000))
    vwap_notional_check = [c for c in d.checks if c.name == "long_book_depth_sufficient"][0]
    assert vwap_notional_check.value.startswith("100")


def test_zero_delta_stale_data_waits():
    d = run(zero_delta_age_seconds=PARAMS.zero_delta_max_age_seconds + 1)
    assert d.outcome is Outcome.WAIT and "zero_delta_data_fresh" in failed(d)
    assert run(zero_delta_age_seconds=PARAMS.zero_delta_max_age_seconds).outcome is Outcome.ENTER


def test_conservative_edge_uses_the_lower_of_current_and_seven_day_mean():
    # current net 0.0003/h (72 h = 216 bps) but the 7-day mean is only 0.00005/h (36 bps): expect the lower
    d = run(pair=make_pair(short_pct=0.03, mean_pct=0.005))
    assert d.outcome is Outcome.ENTER
    assert D(d.metrics["expected_net_edge_bps"]) < D(100)


def test_zero_delta_is_cross_checked_against_the_settled_rate_as_well_as_the_estimate():
    """Live ONEUSDT 2026-09-30: Zero Delta -0.034797 %/h = the 10:00 settlement (-0.034536 %/h), while Binance's
    running estimate for the next settlement was -0.023171 %/h. That is agreement, not a mismatch."""
    from app.strategy.verification import zero_delta_mismatch
    pair = make_pair(long_pct=-0.034797, short_pct=0.03)
    estimate = funding("-0.00023171", interval=1)
    short = funding("0.0003", interval=8)
    only_estimate = zero_delta_mismatch(pair, estimate, short, PARAMS.zero_delta_mismatch_tolerance_per_hour)
    assert not next(c for c in only_estimate if c.name == "long_funding_rate_matches").passed
    both = zero_delta_mismatch(pair, estimate, short, PARAMS.zero_delta_mismatch_tolerance_per_hour,
                               (D("-0.00034536"), None))
    assert next(c for c in both if c.name == "long_funding_rate_matches").passed
    wrong = zero_delta_mismatch(pair, funding("0.0004", interval=1), short, PARAMS.zero_delta_mismatch_tolerance_per_hour,
                                (D("0.0004"), None))  # a sign or symbol error matches neither
    assert not next(c for c in wrong if c.name == "long_funding_rate_matches").passed
