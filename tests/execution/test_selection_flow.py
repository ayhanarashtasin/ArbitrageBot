"""The plan end to end: identify all pairs -> select the best -> simulate 7 days -> verify and trade only the selected."""

from decimal import Decimal

import pytest

from app.core.clock import ManualClock
from app.persistence.models import Selection, StrategyDecision
from tests.execution.fakes import FakeSource
from tests.execution.test_scanner import NOW, FakeZD, Rig, run
from tests.providers.helpers import hours, response, row, trace

D = Decimal

# APR of each venue's funding (percent). The edge of a pair is the difference, so:
#   A-X 400, B-X 300, A-C 200, C-X 200, A-B 100, B-C 100.  X has NO adapter, so it can be selected but never traded.
APR = {"A": 0.0, "B": 100.0, "C": 200.0, "X": 400.0}


def payload(venues=("A", "B", "C", "X"), asset="BTC"):
    rows, traces = [], []
    for v in venues:
        per_interval_pct = APR[v] / 8760 * 8  # Zero Delta shows percent per 8 h interval
        rows.append(row(v, rate=per_interval_pct, interval=8, symbol=f"{asset}/USDT:USDT"))
        traces.append(trace(v, hours(168), APR[v]))
    return response(rows, traces)


class PerAssetZD(FakeZD):
    """Answers each asset with venues whose markets are for that asset."""

    async def fetch_asset(self, asset, days=None, keep_days=None):
        self.payload = payload(asset=asset)
        return await super().fetch_asset(asset, days, keep_days)


def make_rig(**params_kw):
    clock = ManualClock(NOW)
    zd = PerAssetZD(clock, payload())
    sources = {v: FakeSource(v, 100, clock=None, rate_per_hour=str(D(str(APR[v])) / 8760 / 100)) for v in "ABC"}
    rig = Rig(zd=zd, sources=sources, params_kw={"selection_one_per_asset": False, **params_kw})
    zd.clock = rig.clock
    for s in sources.values():
        s.clock = rig.clock
    return rig


def test_best_pairs_are_selected_with_their_exchanges_and_the_untradable_one_is_flagged():
    r = make_rig(selection_count=3)
    r.scan()
    sel = r.scanner.last_selection
    assert sel["count"] == 3 and sel["metric"] == "mean" and sel["pairs_considered"] == 6
    overall = [(p["long"], p["short"]) for p in sel["overall"]]
    assert overall == [("A", "X"), ("B", "X"), ("A", "C")]  # best three in the whole market, 400 / 300 / 200
    assert [p["rank"] for p in sel["overall"]] == [1, 2, 3]
    top = sel["overall"][0]
    assert top["tradable"] is False and "no adapter yet for X" in top["why_not"]
    assert top["score_apr"] == pytest.approx(400.0)
    assert all(k in top for k in ("long_type", "short_type", "steady", "flips", "min_volume_usd"))
    assert [(p["long"], p["short"]) for p in sel["tradable"]] == [("A", "C"), ("A", "B"), ("B", "C")]
    assert all(p["tradable"] for p in sel["tradable"])


def test_only_the_selected_tradable_pairs_are_checked_and_the_best_one_is_traded():
    r = make_rig(selection_count=2)
    rep = r.scan()
    assert rep.pairs_found == 6 and rep.executable_pairs == 3
    assert rep.verified == 1  # A-C ranks first and opens; the same-asset guard stops a second trade this scan
    trade = r.repo.get_trade("arb0001")
    assert (trade.long_venue, trade.short_venue) == ("A", "C") and rep.opened == ["arb0001"]
    with r.repo.session() as s:
        decided = {(d.long_venue, d.short_venue) for d in s.query(StrategyDecision)}
    assert decided == {("A", "C"), ("A", "C")} and ("B", "C") not in decided  # nothing outside the selection was checked


def test_selection_count_limits_how_many_pairs_are_looked_at():
    r = make_rig(selection_count=1, max_concurrent_trades=1)
    r.kill.pause("op", "just look")  # verification runs, but nothing is opened
    rep = r.scan()
    assert len(r.scanner.last_selection["tradable"]) == 1 and rep.verified == 1
    assert r.scanner.last_selection["tradable"][0]["short"] == "C"


def test_each_selected_pair_is_simulated_over_the_week_with_costs():
    r = make_rig(selection_count=3)
    r.scan()
    for group in ("overall", "tradable"):
        for p in r.scanner.last_selection[group]:
            s = p["simulation"]
            assert s is not None and s["fees_assumed"] is True and s["slices"] == 5  # $500 in slices of $100
            assert s["hours"] == 168 and s["notional_usd"] == 500
            assert s["net"] == pytest.approx(s["funding_income"] - s["entry_fees"] - s["exit_fees"] - s["slippage_cost"])
            assert len(s["curve"]) > 100 and s["curve"][-1]["net"] == pytest.approx(s["net"])
    ac = next(p for p in r.scanner.last_selection["tradable"] if (p["long"], p["short"]) == ("A", "C"))["simulation"]
    # 200 % APR before the entry ramp; the ramp costs about 3 hours of exposure out of 168
    assert 190 < ac["gross_apr_pct"] < 200 and ac["net_apr_pct"] < ac["gross_apr_pct"]
    assert ac["breakeven_hours"] is not None and ac["breakeven_hours"] < 60  # $500 x 0.00023/h = $0.11/h vs $0.9 costs
    assert ac["positive_hours_ratio"] == 1.0
    bx = next(p for p in r.scanner.last_selection["overall"] if p["short"] == "X")["simulation"]
    assert bx["net"] > ac["net"] * 0.9  # the untradable best pair is simulated too, so its value is visible


def test_selection_is_saved_to_the_database_for_every_scan():
    r = make_rig(selection_count=3)
    r.scan()
    with r.repo.session() as s:
        rows = s.query(Selection).all()
    assert len(rows) == 6 and {x.kind for x in rows} == {"overall", "tradable"}
    first = next(x for x in rows if x.kind == "overall" and x.rank == 1)
    assert (first.long_venue, first.short_venue, first.tradable) == ("A", "X", False)
    assert first.detail["simulation"]["net"] is not None and first.why_not


def test_selection_ranks_across_assets_not_per_asset():
    r = make_rig(selection_count=4)
    r.kill.pause("op", "only look, the fake venues cannot open ETH")
    run(r.scanner.scan_all(["BTC", "ETH"]))
    overall = r.scanner.last_selection["overall"]
    assert {p["asset"] for p in overall} == {"BTC", "ETH"} and len(overall) == 4
    assert [p["score_apr"] for p in overall] == sorted((p["score_apr"] for p in overall), reverse=True)
    assert r.scanner.last_selection["pairs_considered"] == 12


def test_median_metric_can_be_chosen():
    r = make_rig(selection_metric="median", selection_count=1)
    r.scan()
    assert r.scanner.last_selection["metric"] == "median"


def test_no_selection_when_zero_delta_is_down():
    from app.providers.zerodelta import ZeroDeltaAuthError
    clock = ManualClock(NOW)
    r = Rig(zd=FakeZD(clock, error=ZeroDeltaAuthError(401)))
    r.scan()
    assert r.scanner.last_selection == {} and r.repo.all_trades() == []


def test_invalid_selection_settings_are_refused():
    from app.core.trading_config import TradingSettings
    with pytest.raises(ValueError):
        TradingSettings(_env_file=None, selection_metric="best")
    with pytest.raises(ValueError):
        TradingSettings(_env_file=None, selection_count=0)
    with pytest.raises(ValueError):
        TradingSettings(_env_file=None, selection_count=11)
