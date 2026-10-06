"""Regression checks for Alex's actual workflow: unknown future, forward fills and exact rule boundaries."""

from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.trading_config import TradingSettings
from app.market_data.earnings import CalendarUnavailable, EarningsCalendar, OTHER_EQUITY, US_EQUITY
from app.risk.engine import RiskAction
from app.strategy.pairs import generate_pairs
from app.strategy.selection import select
from app.strategy.simulation import Bar, SimRules, simulate_pair
from app.workers.scanner import _Kept
from tests.execution.test_engine import World, enter_fully
from tests.execution.test_scanner import Rig, calm_closes, wild_closes, run
from tests.providers.helpers import T0, hours, response, row, trace

D = Decimal


def test_net_edge_rejection_still_gets_a_labelled_shadow_trial_and_real_paper_fills():
    r = Rig(params_kw={"shadow_paper_enabled": True, "min_expected_net_edge_bps": 5000})
    report = r.scan()
    assert report.outcomes == {"REJECT": 1} and report.opened == []
    assert report.shadow_opened == ["arb0001"]
    assert r.repo.get_trade("arb0001").context["shadow"] is True
    run(r.engine.run_once())
    orders = r.repo.orders_for("arb0001")
    assert len(orders) == 2 and all(o.filled_quantity > 0 for o in orders)
    assert orders[0].filled_quantity == orders[1].filled_quantity
    assert r.scan().shadow_opened == []  # no duplicate trial on the next sweep
    restored = r.engine.__class__(params=r.params, repo=r.repo, clock=r.clock, killswitch=r.kill,
        notifier=r.notifier, provider=r.engine.provider)
    assert run(restored.recover()) == []
    assert restored.active["arb0001"].entry_done == 1
    assert restored.active["arb0001"].median_net_funding_per_hour_7d == D("0.0003")


def test_watch_enters_on_the_next_minute_without_another_sweep_and_stops_on_outage():
    r = Rig(closes=wild_closes(), params_kw={"shadow_paper_enabled": True})
    r.scan()
    assert r.scanner.watch and not r.engine.active
    for s in r.src.values():
        s.candle_closes = calm_closes()
    r.clock.advance(seconds=60)
    assert run(r.scanner.recheck_watch()) == 1 and r.zd.calls == 1
    assert run(r.scanner.recheck_watch()) == 0
    from app.providers.zerodelta import ZeroDeltaAuthError
    r.zd.error = ZeroDeltaAuthError(401)
    r.scan()
    assert r.scanner.watch == []


def test_spread_and_volatility_are_rechecked_for_the_second_slice():
    r = Rig()
    r.scan()
    run(r.engine.run_once())
    a = r.engine.active["arb0001"]
    r.clock.advance(seconds=a.entry_plan.interval.total_seconds())
    r.src["S"].rate_per_hour = D(0)
    run(r.engine.run_once())
    assert a.entry_done == 1 and a.gate_wait == "net_funding_positive_now"
    r.src["S"].rate_per_hour = D("0.0003")
    r.src["S"].mid = D("99.9")  # adverse spread exceeds 2 h current funding, but fits 8 h of the 7-day median
    for s in r.src.values():
        s.candle_closes = wild_closes()
    r.clock.advance(seconds=60)
    run(r.engine.run_once())
    assert a.entry_done == 1 and a.gate_wait == "low_vol_score"
    for s in r.src.values():
        s.candle_closes = calm_closes()
    r.clock.advance(seconds=60)
    run(r.engine.run_once())
    assert a.entry_done == 2 and a.gate_wait is None


def test_a_gate_data_error_pauses_the_slice_and_logs_its_detail_once(caplog):
    from app.exchanges.base import ExchangeRequestError
    r = Rig()
    r.scan()
    run(r.engine.run_once())
    a = r.engine.active["arb0001"]
    r.clock.advance(seconds=a.entry_plan.interval.total_seconds())

    async def refused(*args, **kwargs):
        raise ExchangeRequestError("S: rate limit (retCode=10006) on /v5/market/kline; not retried")

    r.src["S"].get_candles = refused
    with caplog.at_level("INFO", logger="app.execution.engine"):
        run(r.engine.run_once())
        r.clock.advance(seconds=60)
        run(r.engine.run_once())
    assert a.entry_done == 1 and a.gate_wait == "gate_data_unavailable: ExchangeRequestError"
    detail = [m for m in caplog.messages if "entry gate data unavailable" in m]
    assert len(detail) == 1 and "retCode=10006" in detail[0]


@pytest.mark.parametrize("price,action", [(120, RiskAction.CONTROLLED_EXIT), (123, RiskAction.EMERGENCY_EXIT)])
def test_true_4x_accounts_fire_equity_rules_before_default_backstops(price, action):
    w = World()
    enter_fully(w)
    a = w.engine.active["arb0001"]
    assert a.baseline_long == a.baseline_short == D(125)
    assert w.step()[a.id].action is RiskAction.CONTINUE
    for s in w.src.values():
        s.mid = D(price)
    decision = w.step()[a.id]
    assert decision.action is action
    assert not any("margin" in why or "liquidation" in why for why in decision.reasons)


def test_btc_coarse_lots_stay_equal_through_entry_and_emergency_exit():
    w = World(settings_kw={"target_notional_per_leg_usd": 1000})
    for name, s in w.src.items():
        s.mid = D(83000)
        step = D("0.001") if name == "L" else D("0.0001")
        s._meta = s._meta.model_copy(update={"qty_step": step, "min_qty": step})
    enter_fully(w)
    a = w.engine.active["arb0001"]
    l, s = run(a.executor.long.venue.get_position("BTCUSDT")), run(a.executor.short.venue.get_position("BTCUSDT"))
    assert l.quantity == -s.quantity == D("0.012")
    assert a.max_delta_usd == 0
    w.kill.flatten("test", "close")
    for _ in range(65):
        w.clock.advance(seconds=1)
        w.step()
        if not w.engine.active:
            break
    assert w.repo.get_trade(a.id).state == "CLOSED"
    assert w.repo.get_trade(a.id).max_delta_imbalance_usd == 0


def two_week_analysis(r):
    data = response([row("L", rate=0), row("S", rate=-0.08)],
                    [trace("L", hours(336), 0), trace("S", hours(336), [100] * 169 + [-100] * 168)])
    return r.zd.inner.analyze_response(data, "BTC", keep_days=14, received_at=r.clock.now())


def test_walk_forward_freezes_week_one_direction_despite_week_two_reversal():
    r = Rig()
    analysis = two_week_analysis(r)
    old = generate_pairs(analysis, r.scanner.zd_settings, as_of=T0 + timedelta(days=7))
    assert old[0].long_leg.venue_name == "L" and old[0].current_funding_edge > 0
    current = generate_pairs(analysis, r.scanner.zd_settings)
    assert current[0].long_leg.venue_name == "S"
    kept = _Kept("BTC", analysis.histories, T0 + timedelta(days=14))
    result = r.scanner._walk_forward({"BTC": (kept, current, old)})["tradable"][0]
    assert result["predicted"]["net"] > 0 and result["realised"]["net"] < 0
    assert result["realised"]["start"] == result["picked_at"]
    assert result["realised"]["exit_reason"] == "edge_decayed"


def test_saved_selection_is_scored_once_a_week_later_with_its_original_rules():
    r = Rig()
    r.scan()
    saved = r.repo.latest_selection()
    original = saved["tradable"][0]["params"]["target_notional_per_leg_usd"]
    r.clock.advance(days=8)
    # Future hourly history, with the ORIGINAL L/S direction losing. Today's ranking would reverse it.
    data = response([row("L", rate=0), row("S", rate=-0.08)],
                    [trace("L", hours(370), 0), trace("S", hours(370), [-100] * 371)])
    analysis = r.zd.inner.analyze_response(data, "BTC", keep_days=14, received_at=r.clock.now())
    r.scanner._due_selections = r.repo.due_selections()
    assert len(r.scanner._due_selections) == 2
    r.scanner.params = r.params.model_copy(update={"target_notional_per_leg_usd": D(2000)})
    run(r.scanner._score_saved_picks({"BTC": (_Kept("BTC", analysis.histories, T0 + timedelta(hours=370)), [], [])}))
    track = r.repo.selection_track_record()
    assert len(track) == 2 and all(x["result"]["net"] < 0 for x in track)
    assert all(x["result"]["notional_usd"] == float(original) for x in track)
    assert r.repo.due_selections() == []


def test_selection_is_one_per_coin_and_drops_current_nonpositive_edges():
    from tests.unit.test_selection_simulation import pair, lookup
    candidates = [pair("A", "B", "0.03"), pair("A", "C", "0.02"), pair("B", "C", "0.01", asset="ETH")]
    overall, _ = select(candidates, 3, "mean", lookup, one_per_asset=True, require_live_edge=True)
    assert [p.pair.asset for p in overall] == ["BTC", "ETH"]
    dead = candidates[0].model_copy(update={"current_funding_edge": D(0)})
    overall, _ = select([dead, candidates[1]], 3, "mean", lookup, one_per_asset=True, require_live_edge=True)
    assert len(overall) == 1 and overall[0].pair.short_leg.venue_name == "C"


def test_historical_replay_refuses_missing_hours_or_incomplete_window_and_partial_prices():
    from tests.unit.test_selection_simulation import hourly, sim
    from tests.unit.helpers import T0
    rates = hourly(0.01)
    del rates[T0 + timedelta(hours=9)]
    args = dict(notional_usd=D(1000), child_cap_usd=D(100), entry_minutes=360,
                long_fee=D(0), short_fee=D(0), slippage_bps=D(0))
    with pytest.raises(ValueError, match="missing hourly"):
        simulate_pair(hourly(0), rates, **args)
    with pytest.raises(ValueError, match="not fully covered"):
        simulate_pair(hourly(0), hourly(0.01), start=T0 - timedelta(hours=1), **args)
    rules = SimRules.from_params(TradingSettings(_env_file=None).resolve())
    assert not sim(0, .01, rules=rules, prices={T0: Bar(D(100), D(100), D(100))}).price_checked


@pytest.mark.parametrize("shock,reason", [(120, "controlled_exit_30pct"), (123, "emergency_exit_10pct")])
def test_hourly_replay_accounts_for_leverage_and_equity_exits(shock, reason):
    from tests.unit.test_selection_simulation import sim
    from tests.unit.helpers import T0
    rules = SimRules.from_params(TradingSettings(_env_file=None).resolve())
    bars = {T0 + timedelta(hours=k): Bar(D(100 if k < 8 else shock), D(100 if k < 8 else shock),
                                        D(100 if k < 8 else shock)) for k in range(-1, 25)}
    result = sim(0, .01, points=25, notional_usd=D(100), long_fee=D(0), short_fee=D(0), rules=rules, prices=bars)
    assert result.price_checked and result.exit_reason == reason and result.held_hours < 24


def test_stock_earnings_blackout_and_calendar_failure_do_not_block_crypto():
    r = Rig()
    async def reports(day):
        return {"TSLA"} if day == r.clock.now().date() + timedelta(days=3) else set()
    calendar = EarningsCalendar(blackout_days=3, now=r.clock.now, fetch_day=reports)
    assert "reports earnings" in run(calendar.blackout("TSLA", US_EQUITY))
    assert run(calendar.blackout("BTC", "crypto")) is None
    async def broken(day):
        raise CalendarUnavailable("offline")
    unknown = EarningsCalendar(blackout_days=3, now=r.clock.now, fetch_day=broken)
    assert "unavailable" in run(unknown.blackout("AAPL", US_EQUITY))
    assert "unknown" in run(unknown.blackout("005930", OTHER_EQUITY))
    assert run(unknown.blackout("BTC", "crypto")) is None


def test_earnings_are_checked_again_before_the_next_entry_slice():
    r = Rig()
    for src in r.src.values():
        src._meta = src._meta.model_copy(update={"underlying_type": "EQUITY"})
    reporting = False
    async def calendar_day(day):
        return {"BTC"} if reporting else set()  # use this rig's synthetic stock symbol
    cal = EarningsCalendar(blackout_days=3, now=r.clock.now, fetch_day=calendar_day)
    r.scanner.earnings = r.engine.earnings = cal
    r.scan()
    run(r.engine.run_once())
    a = r.engine.active["arb0001"]
    reporting = True
    cal._days.clear()
    r.clock.advance(seconds=a.entry_plan.interval.total_seconds())
    run(r.engine.run_once())
    assert a.entry_done == 1 and a.gate_wait == "earnings_blackout"


def test_controlled_exit_recovery_preserves_three_hour_schedule():
    w = World()
    enter_fully(w)
    a = w.engine.active["arb0001"]
    for src in w.src.values():
        src.mid = D(120)
    w.step()
    start = a.exit_plan.start
    final_due = a.exit_plan.due_time(a.exit_plan.slices - 1)
    assert abs((final_due - start).total_seconds() - 3 * 3600) < .01
    restored = w.new_engine()
    assert run(restored.recover()) == []
    b = restored.active[a.id]
    assert b.exit_plan.start == start and b.exit_done == a.exit_done
    assert abs((b.exit_plan.due_time(b.exit_plan.slices - 1) - final_due).total_seconds()) < .01


def test_api_distinguishes_shadow_trials_walk_forward_and_frozen_track_record():
    from tests.unit.test_dashboard import make
    r = Rig(params_kw={"shadow_paper_enabled": True, "min_expected_net_edge_bps": 5000})
    r.scan()
    run(r.engine.run_once())
    client, _ = make(r)
    current = client.get("/api/selection").json()
    assert current["tradable"][0]["forward"]["shadow"] is True
    assert current["tradable"][0]["replay_kind"] == "in_sample_diagnostic"
    assert "track_record" in current and client.get("/api/selection/track-record").json() == []
    assert client.get("/api/trades/arb0001").json()["shadow"] is True
    stats = client.get("/api/performance").json()["summary"]
    assert stats["shadow_trades"] == 1 and stats["standard_net_pnl"] == 0


def test_per_slice_earnings_check_uses_the_sweep_class_when_neither_leg_is_labelled():
    """OKX, BloFin and MEXC do not label stock contracts. A stock traded between two of them must still be stopped
    by the per-slice earnings check, using the class the sweep read from a venue that does label it (Binance)."""
    r = Rig()  # the rig's venues carry no underlying_type, like OKX / BloFin
    reporting = False

    async def calendar_day(day):
        return {"BTC"} if reporting else set()

    async def labels():
        return {"BTC": ["EQUITY"]}  # what Binance's instrument list would say for a US stock

    cal = EarningsCalendar(blackout_days=3, now=r.clock.now, fetch_day=calendar_day)
    r.scanner.earnings = r.engine.earnings = cal
    r.scanner._venue_labels = labels
    r.scan()
    run(r.engine.run_once())
    a = r.engine.active["arb0001"]
    assert a.asset_class == US_EQUITY and r.repo.get_trade("arb0001").context["asset_class"] == US_EQUITY
    reporting = True
    cal._days.clear()
    r.clock.advance(seconds=a.entry_plan.interval.total_seconds())
    run(r.engine.run_once())
    assert a.entry_done == 1 and a.gate_wait == "earnings_blackout"
    restored = r.engine.__class__(params=r.params, repo=r.repo, clock=r.clock, killswitch=r.kill,
                                  notifier=r.notifier, provider=r.engine.provider)
    run(restored.recover())
    assert restored.active["arb0001"].asset_class == US_EQUITY  # survives a restart


def test_a_ticker_shared_by_two_assets_is_never_hedged():
    """Live 2026-09-30: Zero Delta's "BB" is BounceBit ($0.0096, Binance) AND BlackBerry stock ($8.67, Trade.xyz)."""
    from tests.unit.test_verification import data, run as verify
    from tests.unit.helpers import T0 as UT0, deep_book
    d = verify(long=data("0", deep_book(mid="0.0096", spread=D("0.0001"), size=10_000_000, now=UT0)),
               short=data("0.0003", deep_book(mid="8.67", spread=D("0.01"), now=UT0)))
    assert d.outcome.value == "REJECT" and "legs_track_same_underlying" in d.reasons
    assert "legs_track_same_underlying" not in verify().reasons  # a real hedge: same price on both venues
    r = Rig()
    r.scan()
    run(r.engine.run_once())
    a = r.engine.active["arb0001"]
    r.src["S"].mid = D(200)  # the short venue now quotes a different asset
    assert run(r.engine._slice_gates(a, a.last_obs, a.entry_plan.child_notional)) == "legs_price_gap"


def test_venue_labels_beat_venue_hints_so_crypto_is_never_held_to_the_earnings_calendar():
    """Ondo Perps and Lighter-on-Robinhood list BTC, ETH, SOL next to stocks: the hint alone must not make them stocks."""
    r = Rig()

    async def broken(day):
        raise CalendarUnavailable("offline")

    async def labels():
        return {"BTC": ["COIN"], "TSLA": ["EQUITY"]}

    r.scanner.earnings = EarningsCalendar(blackout_days=3, now=r.clock.now, fetch_day=broken)
    r.scanner._venue_labels = labels
    found = {a: (_Kept(a, {}, tradfi_hint=True), [], []) for a in ("BTC", "TSLA", "UNLABELLED")}
    run(r.scanner._classify_and_check_earnings(found))
    assert r.scanner._asset_class == {"BTC": "crypto", "TSLA": US_EQUITY, "UNLABELLED": "tradfi"}
    assert set(r.scanner._earnings_block) == {"TSLA", "UNLABELLED"}  # fail closed for stocks, never for BTC


def test_a_venue_outage_never_shows_the_whole_leg_as_a_loss():
    """Senior review 2026-09-30: during an AERO outage the API showed -$98.96 while the equity said -$0.13, because the
    unreachable leg's position was left unvalued. Now: last-known price (labelled, with its age), then unavailable."""
    from types import SimpleNamespace
    from app.api.dashboard import _pnl_numbers
    w = World()
    enter_fully(w)
    a = w.engine.active["arb0001"]
    rt = SimpleNamespace(engine=w.engine, repo=w.repo)
    trade = w.repo.get_trade(a.id)
    healthy = _pnl_numbers(rt, trade)
    assert healthy["status"] == "live" and abs(healthy["net"]) < 5
    a.executor.long.venue.connected = False  # the long venue stops answering
    w.clock.advance(seconds=15)
    w.step()
    during = _pnl_numbers(rt, trade)
    assert during["status"] == "last_known_prices" and "long" in during["stale_legs_seconds"]
    assert abs(during["net"] - healthy["net"]) < 1  # the small real value, never minus the whole $500 leg
    w.clock.advance(minutes=6)
    w.step()
    later = _pnl_numbers(rt, trade)
    assert later["status"] == "unavailable" and later["net"] is None and "long" in later["note"]
    a.executor.long.venue.connected = True
    w.clock.advance(seconds=15)
    w.step()
    assert _pnl_numbers(rt, trade)["status"] == "live"


def test_engine_health_is_red_after_a_failed_loop_or_while_a_trade_is_degraded():
    """Senior review 2026-09-30: the engine was marked green right after a failed iteration and never showed a
    DEGRADED trade. A green engine must mean the loop ran AND every active trade is healthy."""
    from types import SimpleNamespace
    from app.core.health import Health
    from app.workers.runner import Runner
    w = World()
    enter_fully(w)
    a = w.engine.active["arb0001"]
    health = Health(w.clock)
    calls = {"n": 0}

    seen_before_second_pass = {}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        if calls["n"] == 2:
            seen_before_second_pass.update(health.snapshot()["watch"])

    scanner = SimpleNamespace(watch=[], recheck_watch=flaky)
    runner = Runner(settings=None, params=w.params, repo=w.repo, engine=w.engine, scanner=scanner,
                    notifier=w.notifier, health=health, clock=w.clock)

    async def broken():
        raise RuntimeError("engine iteration failed")

    assert run(runner._guard("engine", broken())) is False
    assert health.snapshot()["engine"]["ok"] is False  # and nothing marks it green again in the same pass
    runner.mark_engine_health()
    assert health.snapshot()["engine"]["ok"] is True
    a.executor.long.venue.connected = False
    w.clock.advance(seconds=15)
    w.step()
    assert w.engine.state_of(a).value == "DEGRADED"
    runner.mark_engine_health()
    snap = health.snapshot()["engine"]
    assert snap["ok"] is False and "arb0001 DEGRADED" in snap["detail"]
    a.executor.long.venue.connected = True
    for _ in range(3):
        w.clock.advance(seconds=15)
        w.step()
    runner.mark_engine_health()
    assert health.snapshot()["engine"]["ok"] is True

    # the real watch loop: its first re-check fails, the second succeeds, and health must end green
    runner.params = w.params.model_copy(update={"watch_interval_seconds": 0})

    async def two_passes():
        import asyncio
        task = asyncio.create_task(runner.watch_loop())
        while calls["n"] < 2:
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.02)
        runner.stop()
        await task

    run(two_passes())
    assert seen_before_second_pass["ok"] is False and seen_before_second_pass["detail"] == "RuntimeError: boom"
    assert calls["n"] >= 2 and health.snapshot()["watch"]["ok"] is True  # green again after the good pass


def test_a_stale_looking_book_is_fetched_once_more_but_really_stale_data_still_blocks():
    """Live 2026-09-30: MEXC's REST depth sometimes returned a cached snapshot OLDER than the previous reply, and a
    quiet book keeps its last-change time. One retry clears that; data that stays old is still refused, and the
    stale-data alert fires once per episode instead of every 5 s (INJ had logged 12)."""
    from tests.unit.helpers import deep_book
    w = World()
    enter_fully(w)
    a = w.engine.active["arb0001"]
    src = w.src["S"]
    calls = {"n": 0, "always_stale": False}

    async def flaky_book(symbol, depth=50):
        calls["n"] += 1
        stale = calls["always_stale"] or calls["n"] % 2 == 1  # every first fetch of a cycle looks 5 s old
        return deep_book(mid=src.mid, size=src.size, venue=src.name, now=w.clock.now(), age_ms=5000 if stale else 0)

    src.get_order_book = flaky_book
    w.clock.advance(seconds=15)
    obs = run(w.engine.observe(a))
    assert obs.data_age_ms["short"] <= w.params.max_market_data_age_ms  # the retry was fresh
    calls["always_stale"] = True
    before = len(w.alerts("stale_direct_data"))
    for _ in range(4):  # a 20 s blip: slices pause at once, but it is not worth a warning
        w.clock.advance(seconds=5)
        decision = w.step()[a.id]
        assert decision.action.value == "PAUSE_SLICES"  # still blocked: the data really is old
    assert len(w.alerts("stale_direct_data")) == before
    for _ in range(6):
        w.clock.advance(seconds=5)
        assert w.step()[a.id].action.value == "PAUSE_SLICES"
    assert len(w.alerts("stale_direct_data")) - before == 1  # one alert for the whole stale episode, after 30 s


def test_a_pick_that_fails_a_permanent_check_is_set_aside_so_its_slot_is_free():
    """Live 2026-10-02: ONE (two different coins under one ticker on Binance and OKX) was the top tradable pick of every
    sweep for days and failed legs_track_same_underlying every time. It is now set aside for 24 h."""
    r = Rig()
    r.src["S"].mid = D(200)  # the short venue lists a different asset under the same ticker
    rep = r.scan()
    assert rep.verified == 1 and rep.outcomes == {"REJECT": 1} and not r.engine.active
    assert r.scanner.watch == []  # not re-verified every minute either
    forward = r.scanner.last_selection["tradable"][0]["forward"]
    assert forward["status"] == "set_aside" and "legs_track_same_underlying" in forward["reason"]

    r.clock.advance(minutes=20)
    rep = r.scan()
    assert rep.verified == 0 and r.scanner.last_selection["tradable"] == []  # its slot goes to the next pair
    market = r.scanner.last_selection["overall"][0]
    assert market["tradable"] is False and market["why_not"].startswith("set aside until")

    key = next(iter(r.scanner._parked))
    r.scanner._parked[key] = (r.clock.now(), r.scanner._parked[key][1])  # the 24 h are over
    r.src["S"].mid = D(100)
    rep = r.scan()
    assert rep.verified == 1 and rep.outcomes == {"ENTER": 1} and key not in r.scanner._parked


def test_the_walk_forward_replay_ignores_todays_set_aside_pairs():
    r = Rig()
    r.src["S"].mid = D(200)
    r.scan()
    pair = r.scanner._found["BTC"][1][0]
    assert r.scanner._select([pair])[1] == []  # today: set aside
    assert len(r.scanner._select([pair], with_earnings=False)[1]) == 1  # last week's replay did not know about it
