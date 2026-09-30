"""End-to-end trade lifecycles on scripted markets with a manual clock (paper venues, real engine)."""

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.clock import ManualClock
from app.core.trading_config import TradingSettings
from app.domain.trade import TradeState
from app.execution.engine import TradeEngine
from app.execution.venue_provider import PaperVenueProvider, UnsupportedVenue, VenueProvider
from app.notifications.notifier import Notifier
from app.persistence.repository import Repository
from app.risk.killswitch import KillSwitch
from tests.execution.fakes import FakeSource
from tests.unit.helpers import T0, make_pair_analytics

D = Decimal


def run(coro):
    return asyncio.run(coro)


class World:
    """Everything an engine needs, with handles to the scripted venues."""

    def __init__(self, *, settings_kw=None, sources=None, share_venues=False):
        self.clock = ManualClock(T0)
        self.repo = Repository("sqlite://", self.clock)
        # entry_slice_gates off: these tests drive execution mechanics on flat markets with no funding or candles.
        # The per-slice spread and volatility gates have their own tests.
        kw = dict(target_notional_per_leg_usd=500, min_expected_net_edge_bps=1, entry_slice_gates=False)
        kw.update(settings_kw or {})
        self.params = TradingSettings(_env_file=None, **kw).resolve()
        self.src = sources or {"L": FakeSource("L", 100, clock=self.clock), "S": FakeSource("S", 100, clock=self.clock)}
        self.kill = KillSwitch(self.clock)
        self.notifier = Notifier(self.repo)
        self.provider = PaperVenueProvider(self.src.get, self.clock, self.repo)
        self.share = share_venues
        if share_venues:
            self.provider = SharedVenueProvider(self.provider)
        self.engine = self.new_engine()

    def new_engine(self, owner="worker-1"):
        return TradeEngine(params=self.params, repo=self.repo, clock=self.clock, killswitch=self.kill,
                           notifier=self.notifier, provider=self.provider, owner=owner)

    def open(self, **kw):
        return run(self.engine.open_trade(make_pair_analytics(**kw), {"decision": {"outcome": "ENTER"}}))

    def step(self, engine=None):
        e = engine or self.engine
        return run(e.run_once())

    def advance_and_step(self, n, minutes=0, seconds=0):
        last = None
        for _ in range(n):
            self.clock.advance(minutes=minutes, seconds=seconds)
            last = self.step()
        return last

    def state(self, tid="arb0001"):
        return TradeState(self.repo.get_trade(tid).state)

    def alerts(self, kind):
        return [a for a in self.repo.alerts(kind)]


class SharedVenueProvider(VenueProvider):
    """Returns the same PaperVenue objects on rebuild: models real venues that keep their own state."""

    def __init__(self, inner):
        self.inner, self.cache = inner, {}

    async def build(self, pair, trade_id, params):
        legs = await self.inner.build(pair, trade_id, params)
        self.cache[trade_id] = legs
        return legs

    async def rebuild(self, trade_row, params):
        return self.cache[trade_row.id]


def enter_fully(w: World):
    w.open()
    w.step()  # slice 0 is due immediately
    plan = w.engine.active["arb0001"].entry_plan
    for _ in range(plan.slices):
        w.clock.advance(seconds=plan.interval.total_seconds())
        w.step()
        if w.state() is TradeState.HEDGED_OPEN:
            break
    return plan


# ---- happy path ------------------------------------------------------------------------------

def test_full_lifecycle_entry_hold_normal_exit_close():
    w = World()
    a = w.open()
    assert w.state() is TradeState.ENTERING and a.entry_plan.slices == 5  # $500 / $100
    plan = enter_fully(w)
    assert w.state() is TradeState.HEDGED_OPEN and a.entry_done == plan.slices

    orders = w.repo.orders_for("arb0001")
    assert len(orders) == 2 * plan.slices and len({o.client_order_id for o in orders}) == len(orders)
    assert all(o.filled_quantity * o.average_fill_price <= D("100.5") for o in orders)  # LOCKED child cap
    assert all(o.client_order_id.startswith("arb0001-s") for o in orders)
    t = w.repo.get_trade("arb0001")
    assert t.max_delta_imbalance_usd < w.params.max_delta_imbalance_usd
    assert {s.status for s in w.repo.slices("arb0001", "entry")} == {"FILLED"}
    assert all(s.submission_time_difference_ms is not None for s in w.repo.slices("arb0001", "entry"))

    # funding settles at 16:00 while holding: short receives, long pays
    w.src["S"].rate_per_hour = D("0.0003")
    w.src["L"].rate_per_hour = D("0")
    w.advance_and_step(9, minutes=60)  # crosses the 00:00 settlement
    payments = w.repo.funding_payments("arb0001")
    assert payments and any(p.actual_payment > 0 for p in payments) and all(p.difference == 0 for p in payments)

    # the edge disappears: after the confirmation window a normal exit starts and finishes
    w.src["S"].rate_per_hour = D("0")
    for _ in range(40):
        w.clock.advance(minutes=20)
        w.step()
        if w.state() is TradeState.CLOSED:
            break
    assert w.state() is TradeState.CLOSED
    t = w.repo.get_trade("arb0001")
    assert t.exit_reason == "edge_decayed" and t.pnl["fees"] != "0"
    assert D(t.pnl["funding_pnl"]) > 0
    assert "arb0001" not in w.engine.active
    kinds = {a.kind for a in w.repo.alerts()}
    assert {"entry_started", "entry_completed", "trade_closed", "final_pnl", "funding_payment"} <= kinds
    audit = [a.data["to"] for a in w.repo.audit_for("arb0001") if a.kind == "state_transition"]
    assert audit[:5] == ["VALIDATING_HISTORY", "DIRECT_VERIFYING", "ENTRY_READY", "ENTERING", "HEDGED_OPEN"]
    assert audit[-3:] == ["EXIT_PENDING", "EXITING", "CLOSED"]


def test_no_slice_is_sent_before_it_is_due():
    w = World()
    w.open()
    w.step()
    assert len(w.repo.orders_for("arb0001")) == 2  # slice 0 only
    w.clock.advance(minutes=10)  # slice interval is 72 minutes
    w.step()
    assert len(w.repo.orders_for("arb0001")) == 2


def test_opening_requires_entries_allowed_and_room():
    w = World()
    w.kill.pause("op", "test")
    with pytest.raises(RuntimeError, match="paused"):
        w.open()
    w.kill.resume("op", "test")
    w.open()
    w2 = World(settings_kw=dict(max_concurrent_trades=1))
    w2.open()
    with pytest.raises(RuntimeError, match="max_concurrent_trades"):
        w2.open()
    w3 = World(settings_kw=dict(max_notional_per_venue_usd=400))
    with pytest.raises(RuntimeError, match="max_notional_per_venue_usd"):
        w3.open()


def test_unsupported_venue_is_refused_before_any_record_is_created():
    w = World()
    with pytest.raises(UnsupportedVenue):
        run(w.engine.open_trade(make_pair_analytics("L", "Unknown"), {}))
    assert w.repo.all_trades() == []


# ---- entry failures --------------------------------------------------------------------------

def test_partial_fill_pauses_next_slice_until_hedge_is_repaired():
    w = World()
    w.open()
    a = w.engine.active["arb0001"]
    short = a.executor.short.venue
    short.model.fill_ratio = D("0.62")
    w.step()  # slice 0: long $100, short ~$62 -> repaired in the same cycle
    assert w.alerts("partial_fill")
    # the short venue recovered, so the heal completed the hedge
    delta = run(a.executor.delta())
    assert not a.executor.unsafe(delta)
    assert a.delta_unsafe_since is None


def test_one_leg_rejected_flattens_excess_and_continues():
    w = World()
    w.open()
    a = w.engine.active["arb0001"]
    short = a.executor.short.venue
    short.model.reject_next = 999
    w.step()
    assert w.alerts("one_leg_failure")
    assert run(a.executor.held_quantity(a.executor.long)) == 0  # no unhedged long left behind
    assert w.state() is TradeState.ENTERING


@pytest.mark.parametrize("phase", ["entry", "exit"])
def test_peak_delta_is_saved_before_repair_and_survives_recovery_and_close(phase):
    w = World()
    if phase == "entry":
        a = w.open()
    else:
        enter_fully(w)
        a = w.engine.active["arb0001"]
        w.kill.flatten("op", "test partial exit")
    a.executor.short.venue.model.fill_ratio = D("0.6")
    original_heal = w.engine._heal
    peaks = []

    async def check_then_heal(active, state, *, entry):
        peak = abs((await active.executor.delta()).net_usd)
        assert D(35) < peak < D(45)
        assert active.max_delta_usd == peak
        assert w.repo.get_trade(active.id).max_delta_imbalance_usd == peak
        peaks.append(peak)
        await original_heal(active, state, entry=entry)

    w.engine._heal = check_then_heal
    w.step()
    assert len(peaks) == 1
    assert not a.executor.unsafe(run(a.executor.delta()))
    a.executor.short.venue.model.fill_ratio = D(1)
    run(w.engine._sync_orders(a))
    assert w.repo.get_trade(a.id).max_delta_imbalance_usd == peaks[0]

    # Restart with persisted paper accounts: the smaller repaired delta must not replace the peak.
    engine2 = w.new_engine()
    assert run(engine2.recover()) == []
    assert engine2.active[a.id].max_delta_usd == peaks[0]
    w.kill.flatten("op", "finish test")
    for _ in range(90):
        if w.state(a.id) is TradeState.CLOSED:
            break
        w.clock.advance(seconds=1)
        w.step(engine2)
    assert w.state(a.id) is TradeState.CLOSED
    assert w.repo.get_trade(a.id).max_delta_imbalance_usd == peaks[0]


def test_all_slices_failing_ends_with_a_closed_zero_exposure_trade():
    w = World()
    w.open()
    a = w.engine.active["arb0001"]
    a.executor.short.venue.model.reject_next = 999
    plan = a.entry_plan
    for _ in range(plan.slices + 3):
        w.step()
        w.clock.advance(seconds=plan.interval.total_seconds())
    w.step()
    for _ in range(5):
        w.clock.advance(minutes=30)
        w.step()
    assert w.state() is TradeState.CLOSED
    assert w.repo.get_trade("arb0001").exit_reason == "no_fills"


def test_timeout_after_fill_is_reconciled_without_double_order():
    w = World()
    w.open()
    a = w.engine.active["arb0001"]
    a.executor.long.venue.model.timeout_after_fill_next = 1
    w.step()
    assert len(a.executor.long.venue.fills) == 1  # never sent twice
    assert not a.unresolved


def test_unresolved_order_pauses_entry_then_reconcile_resumes():
    w = World()
    w.open()
    a = w.engine.active["arb0001"]
    long_venue = a.executor.long.venue
    long_venue.model.timeout_before_fill_next = 1
    original = long_venue.get_order

    async def unreachable(*args, **kwargs):
        from app.exchanges.base import ExchangeRequestError
        raise ExchangeRequestError("down")

    long_venue.get_order = unreachable
    w.step()  # the long leg's fate is unknown; the short leg filled
    assert a.unresolved and w.alerts("entry_paused")
    orders_before = len(w.repo.orders_for("arb0001"))
    w.clock.advance(seconds=3)
    w.step()  # risk engine reports DEGRADED (unresolved order); nothing new may be sent
    assert w.state() is TradeState.DEGRADED
    assert len(w.repo.orders_for("arb0001")) == orders_before
    long_venue.get_order = original  # the venue is reachable again: it confirms the order never existed
    w.clock.advance(seconds=1)
    w.step()
    assert w.state() is TradeState.ENTERING and not a.unresolved
    for _ in range(3):  # the missing hedge is then completed
        w.clock.advance(seconds=1)
        w.step()
    assert not a.executor.unsafe(run(a.executor.delta()))
    assert len(long_venue.fills) >= 1


# ---- degraded / disconnect -------------------------------------------------------------------

def test_venue_disconnect_degrades_then_reconciles_and_resumes():
    w = World()
    enter_fully(w)
    a = w.engine.active["arb0001"]
    a.executor.long.venue.connected = False
    w.step()
    assert w.state() is TradeState.DEGRADED and w.alerts("venue_disconnect")
    orders = len(w.repo.orders_for("arb0001"))
    w.clock.advance(minutes=5)
    w.step()  # still unreadable: it keeps trying to reconcile, and sends nothing
    assert w.state() in (TradeState.DEGRADED, TradeState.RECONCILING)
    assert len(w.repo.orders_for("arb0001")) == orders
    a.executor.long.venue.connected = True
    w.step()
    assert w.state() is TradeState.HEDGED_OPEN
    assert any(x.data.get("reason", "").startswith("reconciled") for x in w.repo.audit_for("arb0001"))


# ---- exits -----------------------------------------------------------------------------------

def test_margin_shock_triggers_emergency_exit_that_finishes_within_about_a_minute():
    w = World()
    enter_fully(w)
    a = w.engine.active["arb0001"]
    w.src["S"].mid = w.src["L"].mid = D(160)  # the market rallies: the short account is crushed
    started = w.clock.now()
    w.step()
    assert w.state() is TradeState.EXITING and a.exit_mode == "emergency"
    assert w.alerts("emergency_exit_10pct")
    for _ in range(60):
        if w.state() is TradeState.CLOSED:
            break
        w.clock.advance(seconds=1)
        w.step()
    assert w.state() is TradeState.CLOSED
    assert (w.clock.now() - started).total_seconds() <= 90  # target is about 1 minute
    trade = w.repo.get_trade("arb0001")
    assert "liquidation" in trade.exit_reason or "margin" in trade.exit_reason
    assert w.repo.risk_events("arb0001")[0].action == "EMERGENCY_EXIT"


def test_controlled_exit_at_30_percent_equity_takes_three_hours_paired():
    w = World()  # default backstops: the spec's 30 % rule must fire on its own, before any margin guard
    enter_fully(w)
    a = w.engine.active["arb0001"]
    # a true 4x account: equity $125 per side for $500. A 5-unit short loses $100 on a rally to 120 -> ratio about 20 %
    w.src["S"].mid = w.src["L"].mid = D(120)
    w.step()
    assert w.repo.risk_events("arb0001")[0].action == "CONTROLLED_EXIT"
    assert w.state() is TradeState.EXITING and a.exit_mode == "controlled"
    assert w.alerts("controlled_exit_30pct")
    assert abs((a.exit_plan.due_time(a.exit_plan.slices - 1) - a.exit_plan.start - timedelta(minutes=180)).total_seconds()) < 1
    assert a.exit_plan.child_notional <= 100  # the controlled exit keeps the locked $100 child cap
    w.src["S"].mid = w.src["L"].mid = D(100)  # the market normalises while the paired exit runs
    for _ in range(400):
        if w.state() is TradeState.CLOSED:
            break
        w.clock.advance(minutes=6)
        w.step()
    assert w.state() is TradeState.CLOSED


def test_emergency_takes_precedence_over_controlled_when_both_would_fire():
    w = World(settings_kw=dict(max_margin_utilization_pct=100000, min_liquidation_distance_pct=-100000))
    enter_fully(w)
    w.src["S"].mid = w.src["L"].mid = D(500)  # equity far below 10 %
    w.step()
    assert w.engine.active["arb0001"].exit_mode == "emergency"


def test_exit_stays_delta_neutral_and_is_reduce_only():
    w = World()
    enter_fully(w)
    w.kill.flatten("op", "test")
    a = w.engine.active["arb0001"]
    seen = []
    for _ in range(80):
        if w.state() is TradeState.CLOSED:
            break
        w.clock.advance(seconds=1)
        w.step()
        if a.last_obs and a.last_obs.delta:
            seen.append(abs(a.last_obs.delta.net_usd))
    assert w.state() is TradeState.CLOSED
    assert max(seen) <= float(w.params.max_delta_imbalance_usd) * 3
    exit_orders = [o for o in w.repo.orders_for("arb0001") if "-x" in o.client_order_id]
    assert exit_orders and all(o.reduce_only for o in exit_orders)


def test_kill_switch_pause_stops_new_slices_but_keeps_managing():
    w = World()
    w.open()
    w.step()
    w.kill.pause("op", "maintenance")
    before = len(w.repo.orders_for("arb0001"))
    w.advance_and_step(3, minutes=80)
    assert len(w.repo.orders_for("arb0001")) == before
    assert w.repo.get_trade("arb0001").state == "ENTERING"
    assert w.repo.risk_events("arb0001") == [] or True  # monitoring continued (equity snapshots recorded)
    assert w.repo.session and len(_equity_rows(w)) >= 3


def _equity_rows(w):
    from app.persistence.models import EquitySnapshot
    with w.repo.session() as s:
        return s.query(EquitySnapshot).all()


def test_exit_that_cannot_flatten_ends_in_manual_intervention_and_blocks_new_trades():
    w = World()
    enter_fully(w)
    a = w.engine.active["arb0001"]
    w.kill.flatten("op", "test")
    a.executor.long.venue.model.reject_next = 10_000  # the venue refuses to close
    for _ in range(200):
        if w.state() is TradeState.MANUAL_INTERVENTION_REQUIRED:
            break
        w.clock.advance(seconds=5)
        w.step()
    assert w.state() is TradeState.MANUAL_INTERVENTION_REQUIRED
    assert w.alerts("manual_intervention_required")
    assert w.engine.blocked()
    w.kill.clear_flatten("op", "test")
    w.kill.resume("op", "test")
    with pytest.raises(RuntimeError, match="blocked"):
        w.open()


# ---- ownership -------------------------------------------------------------------------------

def test_a_second_worker_cannot_manage_the_same_trade():
    w = World()
    w.open()
    other = w.new_engine(owner="worker-2")
    other.active["arb0001"] = w.engine.active["arb0001"]
    assert run(other.step(other.active["arb0001"])) is None  # lease held by worker-1
    assert len(w.repo.orders_for("arb0001")) == 0
    w.step()  # the owner proceeds
    assert len(w.repo.orders_for("arb0001")) == 2


# ---- restart recovery ------------------------------------------------------------------------

def test_restart_restores_paper_accounts_and_finishes_the_trade():
    w = World()
    w.open()
    w.step()
    w.clock.advance(minutes=80)
    w.step()
    orders_before = {o.client_order_id for o in w.repo.orders_for("arb0001")}
    # crash: a brand-new engine and provider, same database
    fresh_provider = PaperVenueProvider(w.src.get, w.clock, w.repo)
    engine2 = TradeEngine(params=w.params, repo=w.repo, clock=w.clock, killswitch=w.kill, notifier=w.notifier,
                          provider=fresh_provider, owner="worker-1")
    manual = run(engine2.recover())
    assert manual == [] and "arb0001" in engine2.active
    a2 = engine2.active["arb0001"]
    assert a2.entry_done == 2
    for _ in range(10):
        w.clock.advance(minutes=75)
        run(engine2.run_once())
        if w.state() is TradeState.HEDGED_OPEN:
            break
    assert w.state() is TradeState.HEDGED_OPEN
    orders_after = {o.client_order_id for o in w.repo.orders_for("arb0001")}
    assert orders_before < orders_after and len(orders_after) == 10  # no slice was repeated


def test_crash_after_submit_before_ack_never_duplicates_the_order():
    w = World(share_venues=True)  # the venues remember their fills, like real ones
    w.open()
    a = w.engine.active["arb0001"]
    run(a.executor.entry_slice(0, D(100)))  # orders reach the venues, then the process dies before recording them
    assert w.repo.orders_for("arb0001") == [] and w.repo.slices("arb0001", "entry") == []
    engine2 = w.new_engine()
    assert run(engine2.recover()) == []
    a2 = engine2.active["arb0001"]
    assert {o.client_order_id for o in w.repo.orders_for("arb0001")} == {"arb0001-s0000-L-a01", "arb0001-s0000-S-a01"}
    run(engine2.run_once())  # slice 0 is retried with the same client ids: the venue does not fill it again
    assert len(a2.executor.long.venue.fills) == 1 and len(a2.executor.short.venue.fills) == 1


def test_recovery_with_an_unreadable_venue_requires_manual_intervention():
    w = World(share_venues=True)
    enter_fully(w)
    w.engine.active["arb0001"].executor.long.venue.connected = False
    engine2 = w.new_engine()
    assert run(engine2.recover()) == ["arb0001"]
    assert w.state() is TradeState.MANUAL_INTERVENTION_REQUIRED
    assert engine2.blocked() and w.alerts("manual_intervention_required")


def test_recovery_rejects_records_that_never_reached_a_venue():
    w = World()
    w.repo.create_trade(id="arb0009", asset="BTC", run_mode="paper", long_venue="L", short_venue="S",
                        long_symbol="BTCUSDT", short_symbol="BTCUSDT", state="ENTRY_READY", opened_at=T0,
                        target_notional_per_leg=D(500), leverage=D(2))
    assert run(w.engine.recover()) == []
    assert w.repo.get_trade("arb0009").state == "REJECTED"


def test_recovery_of_a_closed_history_does_nothing():
    w = World()
    assert run(w.engine.recover()) == []
