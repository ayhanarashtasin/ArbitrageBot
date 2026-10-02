"""Live mode end to end: the real engine and pair executor driving LiveVenue legs.

The venues are simulated behind the signed-client interface (SimPrivate): orders walk the real book of a FakeSource
with fees, exactly like paper mode, but everything the bot knows comes back through PrivateClient calls (place, read
back, fills, position, account), as it would from a real exchange.
"""

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.clock import ManualClock
from app.core.trading_config import TradingSettings
from app.domain.market import Side
from app.domain.trade import TradeState
from app.exchanges.base import ExchangeRequestError
from app.exchanges.private.base import (
    AccountSnapshot,
    FundingIncome,
    OrderNotFound,
    OrderRejected,
    PrivateClient,
    PrivateFill,
    Readiness,
)
from app.execution.engine import TradeEngine
from app.execution.live import LiveVenue
from app.execution.paper import PaperRejected, PaperVenue
from app.execution.venue_provider import LiveVenueProvider
from app.notifications.notifier import Notifier
from app.persistence.repository import Repository
from app.risk.killswitch import KillSwitch
from tests.execution.fakes import FakeSource
from tests.unit.helpers import T0, make_pair_analytics

D = Decimal


def run(coro):
    return asyncio.run(coro)


class SimPrivate(PrivateClient):
    """One simulated exchange account, seen only through the signed-client interface."""

    not_found_grace_seconds = 2.0

    def __init__(self, name: str, source: FakeSource, clock, equity=Decimal(5000)):
        self.name = name
        self.paper = PaperVenue(source, clock=clock, initial_equity_usd=equity, taker_fee=D("0.0005"))
        self.clock = clock
        self.funding: list[FundingIncome] = []
        self.timeout_after_place = 0  # the order is placed, then the reply is lost
        self.position_offset = D(0)  # a position the fills do not explain (a units bug, a hidden fill)
        self.placed: list[str] = []

    async def readiness(self) -> Readiness:
        r = Readiness(self.name)
        r.add("simulated", True, "ok")
        return r

    async def account(self) -> AccountSnapshot:
        eq = self.paper.equity()
        return AccountSnapshot(eq, eq - self.paper._used_margin(), self.paper._maintenance_margin())

    async def position(self, meta):
        pos = await self.paper.get_position(meta.symbol)
        if self.position_offset and pos is not None:
            return pos.model_copy(update={"quantity": pos.quantity + self.position_offset})
        return pos

    async def open_orders(self, meta):
        return []

    async def prepare_symbol(self, meta, leverage):
        await self.paper.set_leverage(meta.symbol, leverage)

    async def place_market_order(self, meta, side, quantity, *, client_order_id, reduce_only):
        if client_order_id in self.paper._orders:
            raise OrderRejected(f"{self.name}: duplicate client order id {client_order_id}")
        try:
            order = await self.paper.place_order(meta.symbol, side, quantity, client_order_id=client_order_id,
                                                 reduce_only=reduce_only)
        except PaperRejected as exc:
            raise OrderRejected(str(exc)) from exc
        self.placed.append(client_order_id)
        if self.timeout_after_place:
            self.timeout_after_place -= 1
            raise ExchangeRequestError(f"{self.name}: request timed out")
        return order.order_id

    async def get_order(self, meta, client_order_id):
        order = self.paper._orders.get(client_order_id)
        if order is None:
            raise OrderNotFound(f"{self.name}: no order {client_order_id}")
        return order.model_copy(update={"fee_paid_usd": D(0)})  # real venues: the fee comes from the fills

    async def fills(self, meta, *, order_id=None, since=None):
        out = []
        for i, f in enumerate(self.paper.fills):
            oid = f"paper-{f.client_order_id}"
            if order_id and oid != order_id:
                continue
            if since and f.time < since:
                continue
            out.append(PrivateFill(f"{self.name}-t{i}", oid, None, f.symbol, f.side, f.quantity, f.price, f.fee,
                                   "USDT", f.time))
        return out

    async def cancel_order(self, meta, client_order_id):
        return None

    async def fee_schedule(self, meta):
        raise NotImplementedError

    async def funding_income(self, meta, since):
        return [p for p in self.funding if p.time >= since]


class LiveWorld:
    def __init__(self, *, settings_kw=None):
        self.clock = ManualClock(T0)
        self.repo = Repository("sqlite://", self.clock)
        kw = {"target_notional_per_leg_usd": 500, "min_expected_net_edge_bps": 1, "entry_slice_gates": False}
        kw.update(settings_kw or {})
        self.params = TradingSettings(_env_file=None, **kw).resolve()
        self.src = {"L": FakeSource("L", 100, clock=self.clock),
                    "S": FakeSource("S", 100, clock=self.clock, rate_per_hour="0.0001")}
        self.venue = {n: SimPrivate(n, s, self.clock) for n, s in self.src.items()}
        self.kill = KillSwitch(self.clock)
        self.notifier = Notifier(self.repo)
        self.provider = self.new_provider()
        self.engine = self.new_engine()

    def new_provider(self):
        p = LiveVenueProvider(self.src.get, self.venue, self.clock, self.repo)
        p.ready = set(self.venue)
        return p

    def new_engine(self):
        return TradeEngine(params=self.params, repo=self.repo, clock=self.clock, killswitch=self.kill,
                           notifier=self.notifier, provider=self.provider)

    def open(self):
        return run(self.engine.open_trade(make_pair_analytics("L", "S"), {"decision": {"outcome": "ENTER"}}))

    def step(self):
        return run(self.engine.run_once())

    def advance_and_step(self, n=1, **kw):
        for _ in range(n):
            self.clock.advance(**kw)
            self.step()

    def state(self, tid="arb0001"):
        return TradeState(self.repo.get_trade(tid).state)

    def venue_qty(self, name):
        pos = run(self.venue[name].paper.get_position("BTCUSDT"))
        return pos.quantity if pos else D(0)

    def restart(self):
        """A new process on the same database and the same (real, stateful) venues."""
        self.provider = self.new_provider()
        self.engine = self.new_engine()
        return run(self.engine.recover())


def enter_fully(w: LiveWorld):
    w.open()
    w.step()
    plan = w.engine.active["arb0001"].entry_plan
    for _ in range(plan.slices + 1):
        w.advance_and_step(seconds=plan.interval.total_seconds())
        if w.state() is TradeState.HEDGED_OPEN:
            break
    return plan


def test_a_live_trade_enters_holds_and_exits_with_the_ledger_matching_the_venues():
    w = LiveWorld()
    plan = enter_fully(w)
    a = w.engine.active["arb0001"]
    assert isinstance(a.legs[0].venue, LiveVenue) and a.baseline_long == D(500) / 4  # budget = notional / leverage
    assert w.state() is TradeState.HEDGED_OPEN
    assert w.venue_qty("L") == -w.venue_qty("S") > 0  # equal quantity, opposite sides, on the venues themselves
    orders = w.repo.orders_for("arb0001")
    assert len(orders) == 2 * plan.slices and all(o.fee_usd > 0 for o in orders)  # fees from the venue's fills

    w.src["S"].rate_per_hour = D(0)  # the edge goes: edge decay exits normally
    for _ in range(60):
        w.advance_and_step(minutes=10)
        if w.state() is TradeState.CLOSED:
            break
    assert w.state() is TradeState.CLOSED
    assert w.venue_qty("L") == 0 and w.venue_qty("S") == 0
    t = w.repo.get_trade("arb0001")
    # The engine's final P&L equals what the two venue accounts actually gained or lost.
    venue_change = sum(v.paper.equity() for v in w.venue.values()) - 2 * D(5000)
    assert abs(D(t.pnl["total_realized_pnl"]) - venue_change) < D("0.000001")


def test_after_a_crash_an_order_that_reached_the_venue_is_adopted_never_sent_again():
    w = LiveWorld()
    w.open()
    w.step()  # slice 0
    a = w.engine.active["arb0001"]
    # The process sends slice 1 on both legs and dies before recording anything.
    for name, side in (("L", Side.BUY), ("S", Side.SELL)):
        role = "long" if name == "L" else "short"
        cid = f"arb0001-s0001-{'L' if role == 'long' else 'S'}-a01"
        run(w.venue[name].place_market_order(a.legs[0].meta, side, a.entry_plan.qty_for(1), client_order_id=cid,
                                             reduce_only=False))
    held = w.venue_qty("L")
    assert w.restart() == []
    assert w.state() is TradeState.ENTERING
    w.advance_and_step(seconds=a.entry_plan.interval.total_seconds())
    assert w.venue_qty("L") == held  # slice 1 was found on the venue and adopted: nothing was sent twice
    assert w.venue["L"].placed.count("arb0001-s0001-L-a01") == 1
    assert len(w.repo.slices("arb0001", "entry")) == 2  # and it is now on record
    w.advance_and_step(seconds=a.entry_plan.interval.total_seconds())
    assert w.venue_qty("L") == held + a.entry_plan.qty_for(2)  # the next slice continues the plan


def test_a_lost_reply_is_reconciled_by_client_order_id_without_a_second_order():
    w = LiveWorld()
    w.open()
    w.venue["L"].timeout_after_place = 1
    w.step()
    assert w.venue["L"].placed == ["arb0001-s0000-L-a01"]  # sent once; the executor asked the venue what happened
    assert w.venue_qty("L") == -w.venue_qty("S") > 0


def test_a_position_the_fills_cannot_explain_stops_the_trade_in_degraded():
    w = LiveWorld()
    enter_fully(w)
    w.venue["S"].position_offset = D("-0.5")  # e.g. a units mistake or a fill the API never shows
    w.advance_and_step(seconds=15)
    assert w.state() in (TradeState.DEGRADED, TradeState.RECONCILING)
    w.venue["S"].position_offset = D(0)  # venue and ledger agree again: the trade resumes
    for _ in range(5):
        w.advance_and_step(seconds=15)
    assert w.state() is TradeState.HEDGED_OPEN


def test_live_funding_is_read_from_the_venue_and_counted_once_across_a_restart():
    w = LiveWorld()
    enter_fully(w)
    settle = w.clock.now() + timedelta(minutes=1)
    w.venue["S"].funding.append(FundingIncome(settle, "BTCUSDT", D("0.42"), "USDT", "f-1"))
    w.advance_and_step(minutes=11)
    payments = w.repo.funding_payments("arb0001")
    assert [p.actual_payment for p in payments] == [D("0.42")]
    assert w.restart() == []
    w.advance_and_step(minutes=11)
    assert len(w.repo.funding_payments("arb0001")) == 1  # the restart did not pay it again
    a = w.engine.active["arb0001"]
    assert sum(f.actual for f in a.funding_ledger) == D("0.42")


def test_a_symbol_already_held_on_the_account_is_never_opened():
    w = LiveWorld()
    run(w.venue["S"].paper.place_order("BTCUSDT", Side.SELL, D("0.1"), client_order_id="manual-1"))
    with pytest.raises(Exception, match="existing BTCUSDT position"):
        w.open()
    assert w.repo.all_trades() == []


def test_a_venue_symbol_belongs_to_one_open_trade_even_before_it_has_filled():
    w = LiveWorld()
    w.open()  # no fills yet, so the venue holds no position
    with pytest.raises(Exception, match="already used by open trade arb0001"):
        w.open()


def test_a_venue_that_failed_its_readiness_check_cannot_open_a_trade():
    w = LiveWorld()
    w.provider.ready = {"L"}
    with pytest.raises(Exception, match="not enabled for live trading"):
        w.open()


def test_a_fee_paid_in_the_venue_token_is_valued_in_usd():
    from app.domain.market import InstrumentMeta
    from tests.unit.helpers import deep_book, meta

    class Source(FakeSource):
        async def list_instruments(self):
            bnb = InstrumentMeta(**{**meta(venue=self.name).model_dump(), "symbol": "BNBUSDT", "base": "BNB"})
            return [meta(venue=self.name), bnb]

        async def get_order_book(self, symbol, depth=50):
            return deep_book(mid=600 if symbol == "BNBUSDT" else 100, venue=self.name, now=self._now())

    clock = ManualClock(T0)
    src = Source("L", 100, clock=clock)
    leg = LiveVenue(src, SimPrivate("L", src, clock), meta(venue="L"), clock=clock, budget_usd=D(100),
                    account_key="live:x:long", opened_at=T0)
    run(leg._price_fee_asset("BNB"))
    fill = PrivateFill("t1", "o1", None, "BTCUSDT", Side.BUY, D(1), D(100), D("0.0001"), "BNB", T0)
    assert leg._fee_usd(fill) == D("0.0001") * D(600)


def test_readiness_decides_which_venues_may_open_trades_and_blocks_entries_below_two():
    from app.core.health import Health
    from app.workers.runner import Runner
    from tests.execution.test_scanner import Rig

    w = LiveWorld()
    w.provider.ready = set()
    runner = Runner(settings=None, params=w.params, repo=w.repo, engine=w.engine, scanner=Rig().scanner,
                    notifier=w.notifier, health=Health(w.clock), clock=w.clock)

    async def failing():
        r = Readiness("S")
        r.add("one-way mode", False, "HEDGE mode is on")
        return r

    w.venue["S"].readiness = failing
    assert run(runner.check_live_venues()) == {"L"}
    assert w.engine.blocked() and "two are needed" in w.engine.blocked()
    snap = runner.health.snapshot()
    assert snap["live:L"]["ok"] and not snap["live:S"]["ok"] and "HEDGE" in snap["live:S"]["detail"]
    del w.venue["S"].readiness  # fixed by the user: the next check enables it
    assert run(runner.check_live_venues()) == {"L", "S"} and w.engine.blocked() is None
