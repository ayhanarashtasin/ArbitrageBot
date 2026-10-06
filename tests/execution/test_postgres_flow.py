"""Summary-first scanner, frozen evidence, outages and dropped-pick assessment."""

from datetime import timedelta
from decimal import Decimal

from app.persistence.models import Selection, ZeroDeltaSummaryBatch
from app.providers.zerodelta.exceptions import ZeroDeltaError
from app.providers.zerodelta.provider import ZeroDeltaProvider
from tests.execution.test_scanner import Rig, run
from tests.providers.test_zerodelta_postgres import END, Database, settings, summary, history


def rig():
    r = Rig(now=END + timedelta(hours=1, minutes=5), params_kw={"zero_delta_max_age_seconds": 4500})
    db = Database()
    p = ZeroDeltaProvider(settings(research_pair_limit=4), postgres_client=db, now=r.clock.now)
    r.scanner.zd_settings = p._settings
    r.scanner.zd = r.zd = p
    r.scanner._source_for = {"Binance USDT": r.src["L"], "Bybit USDT": r.src["S"]}.get
    r.engine.provider._source_for = r.scanner._source_for
    # Real venue histories use settlement timestamps and fractions, not normalized percentages.
    async def long_history(*args): return [(END - timedelta(hours=4), Decimal(0))]
    async def short_history(*args): return [(END - timedelta(hours=4), Decimal("0.0024"))]
    r.src["L"].get_funding_history = long_history
    r.src["S"].get_funding_history = short_history
    return r, db


def test_scanner_only_fetches_shortlisted_markets_and_freezes_evidence():
    r, db = rig()
    # Unqualified single-leg APR can be huge, but an ineligible leg must never enter research.
    db.rows += [summary("SMALL", apr=9000, volume=4), summary("SMALL", "bybit_usdt", apr=-9000, volume=4)]
    run(r.scanner.scan_all(["ALL"]))
    assert {q[0] for q in db.requests} == {"BTC"}
    assert r.scanner.last_selection["tradable"][0]["score_apr"] == 262.8
    assert r.scanner.last_selection["rule_check"]["status"] == "prospective_only"
    assert r.repo.all_trades()[0].state == "ENTERING"
    with r.repo.session() as s:
        assert s.query(ZeroDeltaSummaryBatch).count() == 1
        pick = s.query(Selection).first()
        assert len(pick.detail["funding_evidence"]["Bybit USDT"]) == 337
    run(r.scanner.scan_all(["ALL"]))
    with r.repo.session() as s:
        assert s.query(ZeroDeltaSummaryBatch).count() == 1  # immutable idempotent same-hour batch
    assert len(r.repo.all_trades()) == 1


def test_selection_api_summarizes_large_evidence_without_mutating_the_frozen_pick():
    from app.api.dashboard import selection
    r, _ = rig()
    run(r.scanner.scan_all(["ALL"]))
    response = selection(r)
    row = response["tradable"][0]
    assert "funding_evidence" not in row
    assert row["evidence_samples"]["Bybit USDT"] == 337
    assert r.scanner.last_selection["tradable"][0]["funding_evidence"]


def test_database_outage_clears_watch_but_open_trade_remains_managed():
    r, db = rig()
    run(r.scanner.scan_all(["ALL"]))
    db.error = ZeroDeltaError("database request timed out")
    run(r.scanner.scan_all(["ALL"]))
    assert r.scanner.watch == [] and r.scanner.sweep["aborted"] == "outage"
    run(r.engine.run_once())
    assert r.repo.orders_for("arb0001")  # engine needs only direct venue data


def test_hourly_watch_uses_actual_source_time_not_the_hour_bucket_start():
    r, _ = rig()
    r.kill.pause("test", "keep the candidate waiting")
    run(r.scanner.scan_all(["ALL"]))
    assert r.scanner.watch
    r.kill.resume("test", "admit on the next watch")
    r.clock.advance(minutes=40)
    assert run(r.scanner.recheck_watch()) == 1  # still current until the next hourly refresh


def test_due_pick_is_fetched_even_if_coin_disappeared_from_summary():
    r, db = rig()
    run(r.scanner.scan_all(["ALL"]))
    r.clock.advance(days=8)
    boundary = r.clock.now().replace(minute=0, second=0, microsecond=0)
    db.rows = [summary("ETH"), summary("ETH", "bybit_usdt", apr=0)]  # no current edge, BTC no longer listed
    db.data[("BTC", "binance_usdt")] = history(end=boundary)
    db.data[("BTC", "bybit_usdt")] = history("bybit_usdt", rate=.0024, end=boundary)
    db.requests = []
    run(r.scanner.scan_all(["ALL"]))
    assert {q[0] for q in db.requests} == {"BTC"}
    assert r.repo.selection_track_record()
    assert all(row["kind"] == "out_of_sample_hourly_estimate" for row in r.repo.selection_track_record())


def test_permanently_incompatible_pick_is_replaced_within_the_same_batch():
    from app.strategy.verification import Check, Decision, Outcome
    r, db = rig()
    r.scanner.params = r.scanner.params.model_copy(update={"selection_count": 1})
    db.rows += [summary("ETH"), summary("ETH", "bybit_usdt", apr=657)]
    db.data[("ETH", "binance_usdt")] = history(coin="ETH")
    db.data[("ETH", "bybit_usdt")] = history("bybit_usdt", rate=.006, coin="ETH")
    verify = r.scanner.verify_pair
    async def incompatible(pair, zd_age_seconds=0):
        if pair.asset == "ETH":
            return Decision(Outcome.REJECT, [Check("legs_track_same_underlying", False, "10%", "<= 5%")])
        return await verify(pair, zd_age_seconds)
    r.scanner.verify_pair = incompatible
    run(r.scanner.scan_all(["ALL"]))
    assert r.scanner.last_selection["tradable"][0]["asset"] == "BTC"
    assert [t.asset for t in r.repo.all_trades()] == ["BTC"]


def test_runner_sleeps_to_next_hourly_boundary_after_a_six_minute_scan():
    from app.core.health import Health
    from app.core.trading_config import TradingSettings
    from app.workers.runner import Runner
    r, _ = rig()
    runner = Runner(settings=TradingSettings(_env_file=None), params=r.params, repo=r.repo, engine=r.engine,
        scanner=r.scanner, notifier=r.notifier, health=Health(r.clock), clock=r.clock)
    delays = []
    async def scan(assets): r.clock.advance(minutes=6)
    async def sleep(seconds):
        delays.append(seconds)
        runner.stop()
    r.scanner.scan_all = scan
    runner._sleep = sleep
    run(runner.scan_loop())
    assert delays == [54 * 60]


def test_an_outage_found_inside_the_sweep_names_the_real_error_not_the_task_group():
    """Live 2026-10-03: the alert read "unhandled errors in a TaskGroup (1 sub-exception)" and hid the database error."""
    r, db = rig()

    async def failing(*args, **kwargs):
        raise ZeroDeltaError("Zero Delta database request failed (connection)")

    db.history = failing
    db.symbols = failing
    run(r.scanner.scan_all(["ALL"]))
    assert r.scanner.sweep["aborted"] == "outage"
    message = r.repo.alerts("zero_delta_outage")[0].message
    assert "request failed (connection)" in message and "TaskGroup" not in message


def test_one_market_timeout_preserves_good_candidates_and_reports_partial_health():
    from app.core.health import Health
    from app.providers.zerodelta.exceptions import ZeroDeltaTimeoutError
    r, db = rig()
    r.scanner.health = Health(r.clock)
    db.rows += [summary("ETH"), summary("ETH", "bybit_usdt", apr=300)]
    db.data[("ETH", "binance_usdt")] = history(coin="ETH")
    db.data[("ETH", "bybit_usdt")] = history("bybit_usdt", coin="ETH", rate=.003)
    original = db.history
    async def timeout(coin, exchange, *args):
        if coin == "ETH" and exchange == "bybit_usdt":
            raise ZeroDeltaTimeoutError("Zero Delta database query timed out")
        return await original(coin, exchange, *args)
    db.history = timeout
    run(r.scanner.scan_all(["ALL"]))
    assert [t.asset for t in r.repo.all_trades()] == ["BTC"]
    assert r.scanner.sweep["aborted"] is None and r.scanner.sweep["partial"] is True
    assert r.scanner.sweep["market_query_timeouts"] == 1 and r.scanner.sweep["assets_failed"] == 1
    eth = next(p for p in r.scanner.discovery["screening"] if p["asset"] == "ETH")
    assert eth["history_status"] == "insufficient_or_invalid"
    assert any("QUERY_TIMEOUT" in v["reasons"] for v in eth["validation"])
    assert not r.scanner.health.all_ok  # a partial scan must never present itself as fully healthy
    db.history = original
    run(r.scanner.scan_all(["ALL"]))
    assert not r.scanner.sweep["partial"]
    health = r.scanner.health.snapshot()
    assert health["scanner"]["ok"] and health["zero_delta"]["ok"]


def test_history_gaps_keep_specific_reasons_in_the_scan_and_state_api():
    from tests.unit.test_dashboard import make
    r, db = rig()
    del db.data[("BTC", "bybit_usdt")][250]
    run(r.scanner.scan_all(["ALL"]))
    candidate = r.scanner.discovery["screening"][0]
    short = next(m for m in candidate["validation"] if m["venue"] == "Bybit USDT")
    assert "GAPS_EXCEED_LIMIT" in short["reasons"] and short["coverage"]["missing_intervals"] == 1
    assert r.repo.all_trades() == []
    c, _ = make(r)
    assert c.get("/state").json()["last_scans"]["BTC"]["market_validation"]


def test_partial_scans_have_at_most_two_immediate_retry_cycles():
    from app.core.health import Health
    from app.core.trading_config import TradingSettings
    from app.workers.runner import Runner
    r, _ = rig()
    runner = Runner(settings=TradingSettings(_env_file=None), params=r.params, repo=r.repo, engine=r.engine,
        scanner=r.scanner, notifier=r.notifier, health=Health(r.clock), clock=r.clock)
    delays = []
    async def scan(assets): r.scanner.sweep = {"partial": True}
    async def sleep(seconds):
        delays.append(seconds)
        if len(delays) == 3:
            runner.stop()
    r.scanner.scan_all, runner._sleep = scan, sleep
    run(runner.scan_loop())
    assert delays == [60, 60, 3600]


def test_a_sweep_that_is_stopping_fetches_no_coin_and_opens_no_trade():
    r, db = rig()
    r.scanner.stopping = True
    run(r.scanner.scan_all(["ALL"]))
    assert db.requests == [] and r.repo.all_trades() == []
    assert r.scanner.progress["running"] is False
    assert r.scanner._zd_alerted is None and not r.repo.alerts("zero_delta_outage")  # stopping is not an outage


def test_stopping_the_runner_in_the_middle_of_a_sweep_ends_it_before_any_trade_is_opened():
    """Live 2026-10-04: a stop during a sweep waited 20 s for it, timed out and failed the shutdown."""
    from app.core.health import Health
    from app.core.trading_config import TradingSettings
    from app.workers.runner import Runner
    r, _ = rig()
    runner = Runner(settings=TradingSettings(_env_file=None), params=r.params, repo=r.repo, engine=r.engine,
        scanner=r.scanner, notifier=r.notifier, health=Health(r.clock), clock=r.clock)
    discover = r.scanner._discover

    async def stop_while_fetching(asset):
        runner.stop()
        return await discover(asset)

    r.scanner._discover = stop_while_fetching
    run(r.scanner.scan_all(["ALL"]))
    assert r.scanner.stopping and r.scanner.progress["running"] is False
    assert r.repo.all_trades() == [] and r.scanner.watch == []
    assert r.scanner._zd_alerted is None
