"""Chaos tests (project.md section 68). Each one breaks something and checks the bot stays safe."""

import contextlib
from decimal import Decimal

import pytest
from sqlalchemy.exc import OperationalError

from app.domain.trade import TradeState
from app.providers.zerodelta import ZeroDeltaParseError
from tests.execution.test_engine import World, enter_fully, run
from tests.execution.test_scanner import FakeZD, Rig

D = Decimal


@contextlib.contextmanager
def database_down(repo):
    """Every database access fails until the block ends."""
    real = repo.session

    @contextlib.contextmanager
    def broken():
        raise OperationalError("SELECT 1", None, Exception("connection refused"))
        yield  # pragma: no cover

    repo.session = broken
    try:
        yield
    finally:
        repo.session = real


# ---- Zero Delta ------------------------------------------------------------------------------

def test_zero_delta_malformed_response_is_an_outage_not_a_crash():
    r = Rig(zd=FakeZD(None, error=ZeroDeltaParseError("Zero Delta returned malformed JSON")))
    rep = r.scan()
    assert not rep.zero_delta_ok and r.alerts("zero_delta_outage") and r.repo.all_trades() == []


# ---- database outage -------------------------------------------------------------------------

def test_database_outage_does_not_stop_an_emergency_exit():
    w = World()
    enter_fully(w)
    a = w.engine.active["arb0001"]
    with database_down(w.repo):
        w.kill.flatten("op", "test")
        for _ in range(90):
            w.clock.advance(seconds=1)
            w.step()  # must not raise, must keep closing
            if a.state is TradeState.CLOSED:
                break
        assert a.state is TradeState.CLOSED  # the exit completed with the database unreachable
        assert not w.repo.db_ok and w.repo.backlog_size > 0
    assert run(a.executor.delta()).long_usd == 0 and run(a.executor.delta()).short_usd == 0  # really flat on the venues
    assert w.engine.compute_trade_pnl(a).fees > 0  # P&L is known without the database


def test_buffered_writes_are_replayed_in_order_when_the_database_returns():
    w = World()
    enter_fully(w)
    a = w.engine.active["arb0001"]
    with database_down(w.repo):
        w.kill.flatten("op", "test")
        for _ in range(90):
            w.clock.advance(seconds=1)
            w.step()
            if a.state is TradeState.CLOSED:
                break
        backlog = w.repo.backlog_size
    assert backlog > 0
    assert w.repo.flush_backlog()
    assert w.repo.backlog_size == 0 and w.repo.db_ok
    t = w.repo.get_trade("arb0001")
    assert t.state == "CLOSED" and t.pnl is not None and t.exit_reason
    orders = w.repo.orders_for("arb0001")
    assert any("-x" in o.client_order_id for o in orders)  # exit orders that were placed during the outage
    audit = [x.data["to"] for x in w.repo.audit_for("arb0001") if x.kind == "state_transition"]
    assert audit[-2:] == ["EXITING", "CLOSED"]


def test_database_outage_while_holding_keeps_monitoring_and_exit_rules_working():
    w = World(settings_kw=dict(max_margin_utilization_pct=100000, min_liquidation_distance_pct=-100000))
    enter_fully(w)
    a = w.engine.active["arb0001"]
    with database_down(w.repo):
        w.src["S"].mid = w.src["L"].mid = D(500)  # equity collapses during the outage
        w.step()
        assert a.state is TradeState.EXITING and a.exit_mode == "emergency"


def test_new_trades_are_refused_while_the_database_is_down():
    r = Rig()
    with database_down(r.repo):
        with pytest.raises(OperationalError):
            r.scan()  # discovery cannot be recorded, so nothing new is started


# ---- venue failures --------------------------------------------------------------------------

def test_both_venues_down_then_up_recovers_without_new_orders_in_between():
    w = World()
    enter_fully(w)
    a = w.engine.active["arb0001"]
    n = len(w.repo.orders_for("arb0001"))
    a.executor.long.venue.connected = a.executor.short.venue.connected = False
    for _ in range(5):
        w.clock.advance(minutes=1)
        w.step()
    assert a.state in (TradeState.DEGRADED, TradeState.RECONCILING) and len(w.repo.orders_for("arb0001")) == n
    a.executor.long.venue.connected = a.executor.short.venue.connected = True
    w.clock.advance(minutes=1)
    w.step()
    assert a.state is TradeState.HEDGED_OPEN


def test_exit_survives_the_venue_dropping_mid_exit():
    w = World()
    enter_fully(w)
    a = w.engine.active["arb0001"]
    w.kill.flatten("op", "test")
    w.clock.advance(seconds=1)
    w.step()
    a.executor.long.venue.connected = False
    for _ in range(5):
        w.clock.advance(seconds=2)
        w.step()  # cannot trade, must not crash or duplicate anything
    assert a.state is not TradeState.CLOSED
    a.executor.long.venue.connected = True
    for _ in range(90):
        w.clock.advance(seconds=2)
        w.step()
        if a.state is TradeState.CLOSED:
            break
    assert a.state is TradeState.CLOSED and run(a.executor.delta()).long_usd == 0


def test_crash_during_exit_resumes_the_exit_after_restart():
    w = World(share_venues=True)
    enter_fully(w)
    a = w.engine.active["arb0001"]
    w.kill.flatten("op", "test")
    w.clock.advance(seconds=1)
    w.step()
    w.clock.advance(seconds=2)
    w.step()  # some exit slices done, then the process dies
    assert w.repo.get_trade("arb0001").state == "EXITING"
    engine2 = w.new_engine()
    assert run(engine2.recover()) == []
    for _ in range(120):
        w.clock.advance(seconds=2)
        run(engine2.run_once())
        if w.repo.get_trade("arb0001").state == "CLOSED":
            break
    assert w.repo.get_trade("arb0001").state == "CLOSED"
    x_orders = [o.client_order_id for o in w.repo.orders_for("arb0001") if "-x" in o.client_order_id]
    assert len(x_orders) == len(set(x_orders))  # no exit order id was reused


def test_crash_during_entry_resumes_the_entry_after_restart():
    w = World()
    w.open()
    w.step()
    w.clock.advance(minutes=75)
    w.step()
    engine2 = TradeEngineFactory(w)
    assert run(engine2.recover()) == []
    for _ in range(12):
        w.clock.advance(minutes=75)
        run(engine2.run_once())
        if w.repo.get_trade("arb0001").state == "HEDGED_OPEN":
            break
    assert w.repo.get_trade("arb0001").state == "HEDGED_OPEN"
    ids = [o.client_order_id for o in w.repo.orders_for("arb0001")]
    assert len(ids) == len(set(ids)) == 10


def TradeEngineFactory(w):
    from app.execution.engine import TradeEngine
    from app.execution.venue_provider import PaperVenueProvider
    provider = PaperVenueProvider(w.src.get, w.clock, w.repo)
    return TradeEngine(params=w.params, repo=w.repo, clock=w.clock, killswitch=w.kill, notifier=w.notifier,
                       provider=provider, owner="worker-1")


# ---- market events ---------------------------------------------------------------------------

def test_funding_sign_flip_ends_the_trade_via_the_normal_exit():
    w = World()
    enter_fully(w)
    w.src["S"].rate_per_hour = D("-0.0005")  # the edge flips negative
    w.src["L"].rate_per_hour = D("0.0005")
    for _ in range(60):
        w.clock.advance(minutes=20)
        w.step()
        if w.repo.get_trade("arb0001").state == "CLOSED":
            break
    assert w.repo.get_trade("arb0001").exit_reason == "edge_decayed"


def test_basis_widening_makes_delta_unsafe_and_it_is_repaired_without_opening_exposure():
    w = World()
    enter_fully(w)
    a = w.engine.active["arb0001"]
    w.src["L"].mid = D(130)  # long venue rallies alone: exposures diverge by about 30 %
    w.step()
    delta = run(a.executor.delta())
    assert not a.executor.unsafe(delta) or a.state in (TradeState.HEDGED_OPEN, TradeState.EXITING)
    held_long_before = run(a.executor.held_quantity(a.executor.long))
    w.step()
    assert run(a.executor.held_quantity(a.executor.long)) <= held_long_before  # repair only ever reduces


def test_margin_warning_and_liquidation_warning_force_a_protected_exit():
    w = World()
    enter_fully(w)
    a = w.engine.active["arb0001"]
    w.src["S"].mid = w.src["L"].mid = D(140)  # a 40 % rally: the short account nears its limits
    w.step()
    assert a.state is TradeState.EXITING and a.exit_mode == "emergency"
    assert w.repo.risk_events("arb0001")[0].action == "EMERGENCY_EXIT"


def test_a_step_that_raises_puts_the_trade_in_error_instead_of_crashing_the_worker():
    w = World()
    enter_fully(w)
    a = w.engine.active["arb0001"]

    async def boom(*args, **kwargs):
        raise ZeroDivisionError("bug")

    real_delta = a.executor.delta
    a.executor.delta = boom
    w.kill.flatten("op", "test")  # forces the exit path, which needs delta
    w.step()  # must not raise
    assert a.state is TradeState.ERROR and len(w.alerts("critical_error")) == 1
    w.clock.advance(seconds=1)
    w.step()
    assert len(w.alerts("critical_error")) == 1  # one alert per incident
    a.executor.delta = real_delta  # the bug is gone: the trade must not be abandoned in ERROR
    for _ in range(120):
        w.clock.advance(seconds=2)
        w.step()
        if a.state is TradeState.CLOSED:
            break
    assert a.state is TradeState.CLOSED and run(a.executor.delta()).long_usd == 0


def test_engine_loop_survives_a_failing_iteration():
    from app.workers.runner import Runner
    w = World()
    scanner = Rig().scanner
    runner = Runner(settings=None, params=w.params, repo=w.repo, engine=w.engine, scanner=scanner,
                    notifier=w.notifier, health=__import__("app.core.health", fromlist=["Health"]).Health(w.clock),
                    clock=w.clock)

    async def go():
        async def failing():
            raise RuntimeError("boom")
        await runner._guard("engine", failing())  # must swallow and record
        return runner.health.snapshot()["engine"]

    snap = run(go())
    assert snap["ok"] is False and snap["detail"] == "RuntimeError: boom"  # the cause is visible, not just its type


def test_a_coins_discovery_rows_are_one_write_and_are_buffered_during_an_outage():
    """Live 2026-10-02: ~4,200 discovery rows per sweep were committed one by one on the event loop."""
    from app.persistence import models as m
    w = World()
    now = w.clock.now()
    rows = [(m.ZeroDeltaProviderEvent, {"kind": "OK", "message": f"row {i}", "recorded_at": now}) for i in range(3)]
    commits = {"n": 0}
    real = w.repo.session

    @contextlib.contextmanager
    def counting():
        commits["n"] += 1
        with real() as s:
            yield s

    w.repo.session = counting
    w.repo.record_many(rows)
    w.repo.session = real
    assert commits["n"] == 1
    with database_down(w.repo):
        w.repo.record_many(rows)  # never raises: buffered like every other scanner write
    assert w.repo.backlog_size == 1 and not w.repo.db_ok
    w.repo.flush_backlog()
    with w.repo.session() as s:
        assert s.query(m.ZeroDeltaProviderEvent).count() == 6
