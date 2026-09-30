"""TradeEngine: runs hedged trades from entry to close (project.md sections 25-37, 47, 52, 55, 63).

Each call to `step(trade)` is one monitoring-and-execution cycle. It observes both venues, evaluates the risk
engine, and then does exactly one of: repair delta, pause, enter a slice, begin or continue an exit, or hold.
All time comes from the injected Clock, so the whole life of a trade can be simulated deterministically.

Zero Delta is not used anywhere in this file: an outage there can never stop risk management or an exit.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from app.accounting.pnl import FundingRecord, OrderRecord, compute_pnl
from app.core.clock import Clock
from app.core.trading_config import ResolvedParams
from app.domain.market import FundingInfo, Side
from app.domain.trade import EXPOSED, TERMINAL, TradeState, check_transition
from app.exchanges.base import ExchangeError, ExchangeRequestError
from app.execution.delta import DeltaState
from app.execution.pair_executor import Leg, PairExecutor, SliceResult, SliceTooSmall
from app.execution.paper import PaperVenue
from app.execution.twap import TwapPlan, make_exit_plan, make_lot_entry_plan
from app.execution.venue_provider import VenueProvider
from app.market_data.volatility import candles_needed, entry_allowed, low_vol_score
from app.market_data.earnings import CRYPTO, EarningsCalendar, asset_class
from app.notifications.notifier import Notifier
from app.persistence.models import as_utc
from app.persistence.repository import Repository
from app.risk.engine import RiskAction, RiskDecision, RiskInputs, evaluate
from app.risk.exit_policy import EdgeDecayMonitor
from app.risk.killswitch import KillSwitch
from app.strategy.executable import common_lot, leg_price_gap_pct, depth_notional, entry_spread, spread_rule, vwap_for_notional
from app.strategy.pairs import PairAnalytics

log = logging.getLogger(__name__)
D = Decimal
S = TradeState


@dataclass
class Observation:
    long_equity: Decimal | None = None
    short_equity: Decimal | None = None
    delta: DeltaState | None = None
    long_liq_pct: Decimal | None = None
    short_liq_pct: Decimal | None = None
    long_margin_pct: Decimal | None = None
    short_margin_pct: Decimal | None = None
    data_age_ms: dict[str, float] = field(default_factory=dict)
    reachable: dict[str, bool] = field(default_factory=lambda: {"long": True, "short": True})
    funding: dict[str, FundingInfo | None] = field(default_factory=lambda: {"long": None, "short": None})
    marks: dict[str, Decimal] = field(default_factory=dict)  # mid price per leg, for mark-to-market P&L

    @property
    def net_funding_per_hour(self) -> Decimal | None:
        lf, sf = self.funding["long"], self.funding["short"]
        if lf is None or sf is None:
            return None
        return sf.rate_per_hour - lf.rate_per_hour


@dataclass
class ActiveTrade:
    id: str
    asset: str
    executor: PairExecutor
    opened_at: datetime
    entry_plan: TwapPlan
    monitor: EdgeDecayMonitor
    baseline_long: Decimal
    baseline_short: Decimal
    state: TradeState = TradeState.ENTERING  # in-memory truth: the process never depends on the database to know this
    ledger: list[OrderRecord] = field(default_factory=list)  # every fill, for P&L without the database
    funding_ledger: list[FundingRecord] = field(default_factory=list)
    entry_done: int = 0
    exit_plan: TwapPlan | None = None
    exit_mode: str | None = None  # normal | controlled | emergency
    exit_reason: str | None = None
    exit_done: int = 0
    cleanup_attempts: int = 0
    exit_generation: int = 0  # bumps with every new exit plan so client order ids never repeat
    resume_state: TradeState | None = None
    delta_unsafe_since: datetime | None = None
    unresolved: dict[str, str] = field(default_factory=dict)  # client order id -> leg
    synced: set[str] = field(default_factory=set)
    pending_funding: dict[str, tuple[datetime, Decimal]] = field(default_factory=dict)
    last_settled: dict[str, datetime] = field(default_factory=dict)  # leg -> last settlement time paid
    max_delta_usd: Decimal = D(0)
    last_action: RiskAction | None = None
    last_obs: Observation | None = None
    asset_class: str | None = None  # the sweep's stock/crypto classification, for the per-slice earnings check
    last_marks: dict[str, tuple[datetime, Decimal]] = field(default_factory=dict)  # leg -> (seen at, last mid)
    action_changed: bool = True  # the risk action differs from the previous cycle's (alerts fire on changes)
    entry_delay: timedelta = field(default_factory=timedelta)  # how far waiting slices pushed the schedule back
    gate_wait: str | None = None  # why the next entry slice is waiting (spread rule / volatility), None = not waiting
    gate_next_check: datetime | None = None

    @property
    def legs(self) -> tuple[Leg, Leg]:
        return self.executor.long, self.executor.short


MAX_CLEANUP_ATTEMPTS = 5
# A book older than the limit is fetched ONCE more after this pause before it counts as stale. Measured live
# 2026-09-30: MEXC's REST depth sometimes returns a cached snapshot older than the one before it, and a quiet book
# keeps the time of its last change, so one stale-looking reply is common while the next is fresh. A venue whose
# data really is old stays stale after the retry and still blocks, as before.
STALE_BOOK_RETRY_SECONDS = 0.5
# A venue that misses a cycle keeps its last price for the open-trade valuation this long, labelled with its age.
LAST_KNOWN_PRICE_MAX_AGE = timedelta(minutes=5)


class TradeEngine:
    def __init__(self, *, params: ResolvedParams, repo: Repository, clock: Clock, killswitch: KillSwitch,
                 notifier: Notifier, provider: VenueProvider, owner: str = "worker-1",
                 earnings: EarningsCalendar | None = None):
        self.params, self.repo, self.clock = params, repo, clock
        self.kill, self.notifier, self.provider = killswitch, notifier, provider
        self.owner = owner
        self.earnings = earnings
        self.active: dict[str, ActiveTrade] = {}

    # ==== admission ==============================================================================
    def blocked(self) -> str | None:
        flag = self.repo.get_kv("block_new_trades")
        return flag.get("reason") if flag else None

    def portfolio_room(self, pair: PairAnalytics) -> tuple[bool, str]:
        """Section 51 limits. Notional per venue counts one leg; per asset and total count both legs."""
        p = self.params
        target = p.target_notional_per_leg_usd
        exposed = [t for t in self.repo.unfinished_trades() if not (t.context or {}).get("shadow")]
        if len(exposed) >= p.max_concurrent_trades:
            return False, "max_concurrent_trades"
        gross = sum((t.target_notional_per_leg * 2 for t in exposed), D(0)) + target * 2
        if gross > p.max_total_gross_notional_usd:
            return False, "max_total_gross_notional_usd"
        asset_gross = sum((t.target_notional_per_leg * 2 for t in exposed if t.asset == pair.asset), D(0)) + target * 2
        if asset_gross > p.max_notional_per_asset_usd:
            return False, "max_notional_per_asset_usd"
        for name in {pair.long_leg.venue_name, pair.short_leg.venue_name}:
            used = sum((t.target_notional_per_leg for t in exposed if name in (t.long_venue, t.short_venue)), D(0))
            if used + target > p.max_notional_per_venue_usd:
                return False, "max_notional_per_venue_usd"
        return True, ""

    async def open_trade(self, pair: PairAnalytics, decision_context: dict[str, Any], *, shadow: bool = False) -> ActiveTrade:
        """Create a trade whose entry has already been approved by the deterministic validator."""
        if not self.kill.entries_allowed:
            raise RuntimeError("new entries are paused by the kill switch")
        if (reason := self.blocked()):
            raise RuntimeError(f"new trades are blocked: {reason}")
        if shadow and self.params.run_mode != "paper":
            raise RuntimeError("shadow trades are paper-only")
        if shadow:
            trials = [t for t in self.repo.unfinished_trades() if (t.context or {}).get("shadow")]
            if len(trials) >= self.params.selection_count:
                raise RuntimeError("shadow trial capacity reached; waiting for an existing trial to close")
        ok, why = (True, "") if shadow else self.portfolio_room(pair)
        if not ok:
            raise RuntimeError(f"portfolio limit: {why}")

        p, now = self.params, self.clock.now()
        trade_id = self.repo.next_trade_id()
        long_leg, short_leg = await self.provider.build(pair, trade_id, p)
        if p.target_leverage > p.max_allowed_leverage:
            raise RuntimeError("leverage above the locked 4x")
        # Plan in whole lots that BOTH venues accept, before anything is recorded: the same quantity goes on each leg.
        lb, sb = await asyncio.gather(long_leg.venue.get_order_book(long_leg.meta.symbol),
                                      short_leg.venue.get_order_book(short_leg.meta.symbol))
        ref_price = max(x for x in (lb.best_ask, sb.best_bid, lb.best_bid, sb.best_ask) if x)
        try:
            plan = make_lot_entry_plan(p.target_notional_per_leg_usd, p.twap_max_child_notional_usd,
                                       p.twap_entry_duration_minutes, now,
                                       lot=common_lot(long_leg.meta.qty_step, short_leg.meta.qty_step), price=ref_price)
        except ValueError as exc:
            raise RuntimeError(f"cannot size both legs equally: {exc}") from exc
        for leg in (long_leg, short_leg):
            await leg.venue.set_leverage(leg.meta.symbol, p.target_leverage)
        long_eq = (await long_leg.venue.get_account_equity()).equity_usd  # baselines BEFORE entry (section 32)
        short_eq = (await short_leg.venue.get_account_equity()).equity_usd

        self.repo.create_trade(
            id=trade_id, asset=pair.asset, run_mode=p.run_mode, long_venue=pair.long_leg.venue_name,
            short_venue=pair.short_leg.venue_name, long_symbol=long_leg.meta.symbol,
            short_symbol=short_leg.meta.symbol, state=S.DISCOVERED.value, opened_at=now,
            target_notional_per_leg=p.target_notional_per_leg_usd, leverage=p.target_leverage,
            baseline_long_equity=long_eq, baseline_short_equity=short_eq,
            context={"zero_delta": pair.model_dump(mode="json"), "params": p.model_dump(mode="json"),
                      **decision_context, "shadow": shadow})
        for state in (S.VALIDATING_HISTORY, S.DIRECT_VERIFYING, S.ENTRY_READY, S.ENTERING):
            self.repo.set_state(trade_id, state, "entry approved by validator")
        for leg in (long_leg, short_leg):
            self.repo.upsert_leg(trade_id, leg.role, leg.venue.name, leg.meta.symbol)

        executor = PairExecutor(trade_id, long_leg, short_leg, self.clock, p,
                                events=lambda kind, data, tid=trade_id: self.repo.audit(kind, data, tid))
        active = ActiveTrade(trade_id, pair.asset, executor, now, plan, EdgeDecayMonitor(p), long_eq, short_eq,
                             asset_class=decision_context.get("asset_class"),
                             state=S.ENTERING)
        self.active[trade_id] = active
        self.repo.acquire_lease(trade_id, self.owner)
        self.repo.merge_context(trade_id, entry_plan={"slices": plan.slices, "child": plan.child_notional,
                                                      "interval_s": plan.interval.total_seconds(),
                                                      "slice_qty": [str(q) for q in plan.slice_qty or ()]})
        first = plan.qty_for(0)
        await self.notifier.alert(
            "entry_started", f"{pair.asset} long {pair.long_leg.venue_name} / short {pair.short_leg.venue_name}: "
            f"{plan.slices} slices of {first} (about ${plan.child_notional:.2f}) on EACH leg over "
            f"{p.twap_entry_duration_minutes} min", trade_id=trade_id)
        self._persist_accounts(active)
        return active

    # ==== observation ============================================================================
    async def _observe_leg(self, active: ActiveTrade, leg: Leg, obs: Observation) -> tuple[Decimal | None, Decimal | None]:
        role = leg.role
        try:
            acct = await leg.venue.get_account_equity()
            pos = await leg.venue.get_position(leg.meta.symbol)
            book = await self._fresh_book(leg)
            fund = await leg.venue.get_funding_info(leg.meta.symbol)
        except ExchangeError:
            obs.reachable[role] = False
            return None, None
        mark = (book.best_bid + book.best_ask) / 2 if book.best_bid and book.best_ask else None
        if mark is not None:
            obs.marks[role] = mark
        obs.data_age_ms[role] = max(0.0, book.age_ms(self.clock.now()))
        obs.funding[role] = fund
        equity = acct.equity_usd
        margin = None if acct.margin_utilization is None else acct.margin_utilization * 100
        liq_pct = None
        if pos and pos.liquidation_price and mark:
            dist = (mark - pos.liquidation_price) if pos.quantity > 0 else (pos.liquidation_price - mark)
            liq_pct = dist / mark * 100
        if role == "long":
            obs.long_equity, obs.long_liq_pct, obs.long_margin_pct = equity, liq_pct, margin
        else:
            obs.short_equity, obs.short_liq_pct, obs.short_margin_pct = equity, liq_pct, margin
        return (abs(pos.quantity) if pos else D(0)), mark

    async def _fresh_book(self, leg: Leg):
        """The leg's order book; a stale-looking one is fetched once more before it is judged stale."""
        book = await leg.venue.get_order_book(leg.meta.symbol)
        if book.age_ms(self.clock.now()) > self.params.max_market_data_age_ms:
            await self.clock.sleep(STALE_BOOK_RETRY_SECONDS)
            book = await leg.venue.get_order_book(leg.meta.symbol)
        return book

    async def observe(self, active: ActiveTrade) -> Observation:
        obs = Observation()
        (lq, lm), (sq, sm) = await asyncio.gather(self._observe_leg(active, active.legs[0], obs),
                                                  self._observe_leg(active, active.legs[1], obs))
        now = self.clock.now()
        for role, mark in obs.marks.items():  # remembered so a brief venue outage does not erase the valuation
            active.last_marks[role] = (now, mark)
        if lq is not None and sq is not None and lm and sm:
            obs.delta = DeltaState(lq * lm, sq * sm)
            active.max_delta_usd = max(active.max_delta_usd, abs(obs.delta.net_usd))
        active.last_obs = obs
        return obs

    # ==== funding (paper) ========================================================================
    async def _accrue_funding(self, active: ActiveTrade, obs: Observation) -> None:
        """Settle paper funding at each venue's real settlement time, using the rate that applied.

        Each settlement is paid at most once per leg. The observation is read BEFORE this runs, so right after a
        settlement a venue can still report the settlement just paid as its "next" one (live 2026-09-30: Hyperliquid's
        13:00 funding was credited twice on AERO). A settlement at or before the last one paid is therefore ignored.
        """
        now = self.clock.now()
        for leg in active.legs:
            info = obs.funding[leg.role]
            if info is None or info.next_funding_time is None:
                continue
            last_paid = active.last_settled.get(leg.role)
            pending = active.pending_funding.get(leg.role)
            if pending is not None and last_paid is not None and pending[0] <= last_paid:
                active.pending_funding.pop(leg.role, None)  # already paid: never pay the same settlement twice
                pending = None
            if pending is not None and now >= pending[0] and isinstance(leg.venue, PaperVenue):
                settle_time, rate = pending
                pos = await leg.venue.get_position(leg.meta.symbol)
                mark = leg.venue.mark_price(leg.meta.symbol)
                expected = D(0)
                if pos and mark:
                    expected = -pos.quantity * mark * rate
                payment = leg.venue.apply_funding(leg.meta.symbol, rate, settle_time)
                if payment is not None:
                    active.funding_ledger.append(FundingRecord(leg.role, expected, payment.payment))
                    self.repo.add_funding_payment(active.id, leg.venue.name, leg.meta.symbol, settle_time, rate,
                                                  payment.notional, expected, payment.payment)
                    await self.notifier.alert(
                        "funding_payment", f"{leg.role} {leg.venue.name}: {payment.payment:+.6f} USD at rate {rate}",
                        trade_id=active.id)
                active.last_settled[leg.role] = settle_time
                active.pending_funding.pop(leg.role, None)
                pending = None
            last_paid = active.last_settled.get(leg.role)
            if (pending is None or info.next_funding_time == pending[0]) and \
                    (last_paid is None or info.next_funding_time > last_paid):
                active.pending_funding[leg.role] = (info.next_funding_time, info.funding_rate)

    # ==== persistence helpers ====================================================================
    def _persist_accounts(self, active: ActiveTrade) -> None:
        for leg in active.legs:
            if isinstance(leg.venue, PaperVenue):
                self.repo.save_paper_account(leg.venue.paper_key, leg.venue.snapshot())  # type: ignore[attr-defined]

    async def _sync_orders(self, active: ActiveTrade) -> None:
        """Copy real order and fill state from the venues into the database (venues are the truth)."""
        exposures: dict[str, Decimal] = {}
        for leg in active.legs:
            try:
                fills = await leg.venue.get_fills(leg.meta.symbol)
            except ExchangeError:
                continue
            for f in fills:
                if f.client_order_id in active.synced:
                    continue
                order = await leg.venue.get_order(client_order_id=f.client_order_id)
                if order.filled_quantity > 0 and order.average_fill_price:
                    active.ledger.append(OrderRecord(leg.role, order.side.value, order.filled_quantity,
                                                     order.average_fill_price, order.fee_paid_usd, f.reference_price))
                self.repo.upsert_order(active.id, leg.role, order, order.reduce_only)
                self.repo.add_fill(active.id, f.client_order_id, f.quantity, f.price, f.fee, f.reference_price, f.time)
                active.synced.add(f.client_order_id)
            try:
                held = await active.executor.held_quantity(leg)
                book = await leg.venue.get_order_book(leg.meta.symbol)
            except ExchangeError:
                continue
            mark = (book.best_bid + book.best_ask) / 2
            exposures[leg.role] = held * mark
            fees = sum((o.fee for o in active.ledger if o.leg == leg.role), D(0))
            self.repo.upsert_leg(active.id, leg.role, leg.venue.name, leg.meta.symbol,
                                  filled_quantity=held, filled_notional_usd=held * mark, fees_usd=fees)
        # Sync runs immediately after fills, before repair can hide an entry or exit imbalance.
        if len(exposures) == 2:
            active.max_delta_usd = max(active.max_delta_usd, abs(exposures["long"] - exposures["short"]))
        active.max_delta_usd = max(active.max_delta_usd, active.executor.max_delta_usd)
        self.repo.update_trade(active.id, max_delta_imbalance_usd=active.max_delta_usd)

    def _record_slice(self, active: ActiveTrade, phase: str, res: SliceResult, planned: Decimal) -> None:
        status = "FILLED" if res.fully_filled else ("UNRESOLVED" if res.unresolved else "PARTIAL_OR_FAILED")
        self.repo.record_slice(
            active.id, phase, res.index, planned, status, long_submit_time=res.long.submitted_at,
            short_submit_time=res.short.submitted_at, submission_time_difference_ms=res.submit_diff_ms,
            detail={"long": {"cid": res.long.client_order_id, "status": res.long.status,
                             "filled": str(res.long.filled_quantity)},
                    "short": {"cid": res.short.client_order_id, "status": res.short.status,
                              "filled": str(res.short.filled_quantity)}})

    # ==== state helpers ==========================================================================
    def state_of(self, active: ActiveTrade) -> TradeState:
        return active.state

    def _set(self, active: ActiveTrade, new: TradeState, reason: str) -> None:
        """Validate in memory, persist best-effort (buffered during a database outage), then commit."""
        check_transition(active.state, new)
        self.repo.set_state(active.id, new, reason)
        active.state = new

    async def _risk_inputs(self, active: ActiveTrade, obs: Observation, normal_signal: bool) -> RiskInputs:
        return RiskInputs(
            now=self.clock.now(), baseline_long_equity=active.baseline_long, baseline_short_equity=active.baseline_short,
            long_equity=obs.long_equity, short_equity=obs.short_equity,
            long_liquidation_distance_pct=obs.long_liq_pct, short_liquidation_distance_pct=obs.short_liq_pct,
            long_margin_utilization_pct=obs.long_margin_pct, short_margin_utilization_pct=obs.short_margin_pct,
            delta=obs.delta, delta_dust_usd=active.executor.dust_usd, delta_unsafe_since=active.delta_unsafe_since,
            data_age_ms=obs.data_age_ms, venues_reachable=obs.reachable, unresolved_orders=bool(active.unresolved),
            normal_exit_signal=normal_signal)

    # ==== the cycle ==============================================================================
    async def step(self, active: ActiveTrade) -> RiskDecision | None:
        """One cycle. Never raises: unexpected errors put the trade in ERROR and raise an alert."""
        if not self.repo.acquire_lease(active.id, self.owner):
            return None  # another worker owns this trade (section 52)
        try:
            return await self._step(active)
        except Exception as exc:  # noqa: BLE001 - a crash here must degrade the trade, not the process
            log.exception("trade %s step failed", active.id)
            entered = False
            try:
                prev = self.state_of(active)
                if prev in EXPOSED and prev not in (S.ERROR, S.DEGRADED, S.RECONCILING, S.MANUAL_INTERVENTION_REQUIRED):
                    active.resume_state = prev
                    self.repo.merge_context(active.id, resume_state=prev.value)
                    self._set(active, S.ERROR, f"{type(exc).__name__}")
                    entered = True
            except Exception:  # noqa: BLE001
                pass
            if entered:  # one alert per incident, not one per retry
                await self.notifier.alert("critical_error", f"{type(exc).__name__} while managing the trade",
                                          severity="critical", trade_id=active.id)
            return None

    async def _step(self, active: ActiveTrade) -> RiskDecision | None:
        now = self.clock.now()
        state = self.state_of(active)
        if state in TERMINAL:
            self.active.pop(active.id, None)
            return None
        if state is S.MANUAL_INTERVENTION_REQUIRED:
            return None  # a human must clear this; nothing automatic touches it

        obs = await self.observe(active)
        await self._accrue_funding(active, obs)

        # delta bookkeeping for the "severe unresolved imbalance" rule
        unsafe = obs.delta is not None and active.executor.unsafe(obs.delta)
        if unsafe and active.delta_unsafe_since is None:
            active.delta_unsafe_since = now
        elif not unsafe:
            active.delta_unsafe_since = None

        normal_signal = False
        if state is S.HEDGED_OPEN and obs.net_funding_per_hour is not None:
            sig = active.monitor.update(now, active.opened_at, obs.net_funding_per_hour)
            if sig:
                normal_signal, active.exit_reason = True, sig.reason

        decision = evaluate(await self._risk_inputs(active, obs, normal_signal), self.params)
        if self.kill.emergency_flatten and decision.action is not RiskAction.EMERGENCY_EXIT:
            decision = RiskDecision(RiskAction.EMERGENCY_EXIT, 1, ("kill_switch_emergency_flatten",),
                                    decision.long_equity_ratio, decision.short_equity_ratio)
        active.action_changed = decision.action is not active.last_action
        if decision.action is not active.last_action:
            if decision.action is not RiskAction.CONTINUE:
                self.repo.add_risk_event(active.id, decision.action.value, decision.priority, decision.reasons)
            active.last_action = decision.action
        self.repo.add_equity_snapshot(
            active.id, long_equity=obs.long_equity, short_equity=obs.short_equity,
            long_ratio=decision.long_equity_ratio, short_ratio=decision.short_equity_ratio,
            net_delta_usd=None if obs.delta is None else obs.delta.net_usd)

        await self._apply(active, state, decision, obs)
        self._persist_accounts(active)
        return decision

    async def _apply(self, active: ActiveTrade, state: TradeState, decision: RiskDecision, obs: Observation) -> None:
        a = decision.action

        # ---- most urgent first
        if a is RiskAction.EMERGENCY_EXIT:
            if not (state is S.EXITING and active.exit_mode == "emergency"):
                await self._begin_exit(active, "emergency", ",".join(decision.reasons))
            await self._run_exit(active)
            return
        if a is RiskAction.DEGRADED:
            if state in (S.DEGRADED, S.RECONCILING, S.ERROR):
                await self._resume_from_degraded(active, state)  # keep trying to reconcile every cycle
            else:
                await self._enter_degraded(active, state, decision)
            return
        if state in (S.DEGRADED, S.RECONCILING, S.ERROR):
            await self._resume_from_degraded(active, state)
            return
        if a is RiskAction.HEAL_DELTA:
            await self._heal(active, state, entry=(state is S.ENTERING))
            return
        if a is RiskAction.CONTROLLED_EXIT and state in (S.ENTERING, S.HEDGED_OPEN, S.EXIT_PENDING):
            await self._begin_exit(active, "controlled", ",".join(decision.reasons))
        elif a is RiskAction.NORMAL_EXIT and state is S.HEDGED_OPEN:
            await self._begin_exit(active, "normal", active.exit_reason or "normal_exit")
        elif a is RiskAction.PAUSE_SLICES:
            if active.action_changed:  # once when data goes stale, not again every 5 s while it stays stale
                await self.notifier.alert("stale_direct_data",
                                          "market data stale: no new slices (" + ", ".join(decision.reasons) + ")",
                                          severity="warning", trade_id=active.id)
            if state is not S.EXITING:
                return

        state = self.state_of(active)
        if state is S.ENTERING:
            await self._entry_step(active, obs)
        elif state is S.EXITING:
            await self._run_exit(active)

    # ---- entry ------------------------------------------------------------------------------
    async def _slice_gates(self, active: ActiveTrade, obs: Observation, planned_usd: Decimal) -> str | None:
        """The task spec's opening rules, re-checked before EVERY entry slice. None = go, else why it waits.

        1. 2 h spread rule on this slice's executable prices and the venues' current funding (LOCKED).
        2. Low-vol score on 1-minute candles of both legs <= 45 (LOCKED).
        """
        p = self.params
        need = candles_needed(p)
        long_leg, short_leg = active.legs
        if self.earnings is not None:
            cls = asset_class([long_leg.meta.underlying_type, short_leg.meta.underlying_type])
            if cls == CRYPTO and active.asset_class:  # both legs unlabeled: use the sweep's classification
                cls = active.asset_class
            if await self.earnings.blackout(active.asset, cls):
                return "earnings_blackout"
        lc, sc = await asyncio.gather(long_leg.venue.get_candles(long_leg.meta.symbol, "1m", need),
                                      short_leg.venue.get_candles(short_leg.meta.symbol, "1m", need))
        vol = low_vol_score(lc, sc, rv_window=p.rv_window_minutes, basis_window=p.basis_vol_window_minutes,
                            min_observations=p.vol_min_observations)
        if not entry_allowed(vol, p.low_vol_max_percentile):
            return "low_vol_score" if vol.available else "volatility_score_unavailable"
        # Books/funding LAST: a slow candle fetch must not age the data used to approve an order.
        (lb, sb), lf, sf = await asyncio.gather(active.executor.books(),
            long_leg.venue.get_funding_info(long_leg.meta.symbol),
            short_leg.venue.get_funding_info(short_leg.meta.symbol))
        if max(lb.age_ms(self.clock.now()), sb.age_ms(self.clock.now())) > p.max_market_data_age_ms:
            await self.clock.sleep(STALE_BOOK_RETRY_SECONDS)  # one retry: a cached or quiet snapshot is common
            lb, sb = await active.executor.books()
            if max(lb.age_ms(self.clock.now()), sb.age_ms(self.clock.now())) > p.max_market_data_age_ms:
                return "stale_order_book"
        gap = leg_price_gap_pct(lb, sb)
        if gap is None or gap > p.max_leg_price_gap_pct:
            return "legs_price_gap"
        lv, sv = vwap_for_notional(lb, Side.BUY, planned_usd), vwap_for_notional(sb, Side.SELL, planned_usd)
        if lv.vwap is None or sv.vwap is None:
            return "book_too_thin"
        need_depth = planned_usd * p.min_required_book_depth_multiplier
        if depth_notional(lb, Side.BUY, 20) < need_depth or depth_notional(sb, Side.SELL, 20) < need_depth:
            return "book_depth_multiplier"
        if any(v.slippage_bps is not None and v.slippage_bps > p.max_allowed_slippage_bps for v in (lv, sv)):
            return "slippage_too_high"
        _, neg_cost = entry_spread(lv.vwap, sv.vwap)
        ok, _ = spread_rule(neg_cost, sf.rate_per_hour, lf.rate_per_hour, D(p.funding_screening_horizon_minutes) / 60)
        if not ok:
            return "negative_spread_within_2h_funding"
        return None

    async def _entry_step(self, active: ActiveTrade, obs: Observation | None = None) -> None:
        now = self.clock.now()
        if active.unresolved or not self.kill.entries_allowed:
            return  # entry work is paused, risk management continues
        plan = active.entry_plan
        idx = active.entry_done if active.entry_done < plan.slices else None
        scheduled = plan.due_time(idx) + active.entry_delay if idx is not None else None
        if idx is None or now < scheduled:
            if active.entry_done >= plan.slices:
                if now >= plan.end + active.entry_delay:
                    await self._finish_entry(active)
            return
        planned = plan.child_notional
        if self.params.entry_slice_gates and obs is not None:
            if active.gate_next_check is not None and now < active.gate_next_check:
                return  # waiting: re-check on the 1-minute cadence, not every cycle
            try:
                wait = await self._slice_gates(active, obs, planned)
            except (ExchangeError, ValueError) as exc:  # the slice waits; the trade never goes to ERROR over data
                wait = f"gate_data_unavailable: {type(exc).__name__}"
            if wait is not None:
                if active.gate_wait != wait:
                    self.repo.audit("entry_slice_waiting", {"slice": idx, "reason": wait}, active.id)
                    await self.notifier.alert("entry_paused", f"slice {idx} waits: {wait}", trade_id=active.id)
                active.gate_wait = wait
                active.gate_next_check = now + timedelta(seconds=self.params.watch_interval_seconds)
                return
            was_waiting = active.gate_wait is not None
            if was_waiting:
                self.repo.audit("entry_slice_resumed", {"slice": idx, "after": active.gate_wait}, active.id)
            active.gate_wait = active.gate_next_check = None
            late = now - scheduled
            if was_waiting and late > timedelta(seconds=self.params.watch_interval_seconds):
                active.entry_delay += late
                self.repo.merge_context(active.id, entry_delay_s=active.entry_delay.total_seconds())
        try:
            res = await active.executor.entry_slice(idx, planned, quantity=plan.qty_for(idx))
        except SliceTooSmall:
            self.repo.record_slice(active.id, "entry", idx, planned, "SKIPPED_TOO_SMALL")
            active.entry_done += 1
            return
        except ExchangeError as exc:
            await self.notifier.alert("stale_direct_data", f"slice {idx} not sent: {exc}", severity="warning",
                                      trade_id=active.id)
            return  # not counted: it will be retried on the next cycle
        active.entry_done += 1
        self._record_slice(active, "entry", res, planned)
        await self._sync_orders(active)
        for sub in (res.long, res.short):
            if sub.status == "UNRESOLVED":
                active.unresolved[sub.client_order_id] = sub.leg
        if res.unresolved:
            await self.notifier.alert("entry_paused", f"slice {idx}: order state unknown, reconciling",
                                      severity="warning", trade_id=active.id)
            return
        if not res.fully_filled:
            kind = "one_leg_failure" if "REJECTED" in (res.long.status, res.short.status) or \
                "NOT_PLACED" in (res.long.status, res.short.status) else "partial_fill"
            await self.notifier.alert(
                kind, f"slice {idx}: long ${res.long.filled_usd:.2f} short ${res.short.filled_usd:.2f}",
                severity="warning", trade_id=active.id)
        delta = await active.executor.delta()
        if active.executor.unsafe(delta):
            await self.notifier.alert("delta_imbalance", f"net delta ${delta.net_usd:.2f}", severity="warning",
                                      trade_id=active.id)
            await self._heal(active, S.ENTERING, entry=True)
        if active.entry_done >= active.entry_plan.slices and now >= active.entry_plan.end + active.entry_delay:
            await self._finish_entry(active)

    async def _finish_entry(self, active: ActiveTrade) -> None:
        delta = await active.executor.delta()
        if active.executor.flat(delta):
            await self._begin_exit(active, "normal", "no_fills")  # nothing was hedged: close the record
            await self._run_exit(active)
            return
        if active.executor.unsafe(delta):
            return  # stay in ENTERING; the risk engine keeps trying to repair delta
        self._set(active, S.HEDGED_OPEN, "entry complete and delta safe")
        await self.notifier.alert("entry_completed", f"hedged: long ${delta.long_usd:.2f} short ${delta.short_usd:.2f}",
                                  trade_id=active.id)

    async def _heal(self, active: ActiveTrade, state: TradeState, *, entry: bool) -> None:
        deadline = self.clock.now() + timedelta(milliseconds=self.params.delta_hedge_timeout_ms)
        result = await active.executor.heal("entry" if entry else "exit", deadline)
        await self._sync_orders(active)
        if result.healed:
            active.delta_unsafe_since = None
        if result.flattened:
            await self.notifier.alert("one_leg_failure" if not result.healed else "delta_imbalance",
                                      f"excess exposure flattened, net delta ${result.delta.net_usd:.2f}",
                                      severity="warning", trade_id=active.id)

    # ---- exit -------------------------------------------------------------------------------
    async def _begin_exit(self, active: ActiveTrade, mode: str, reason: str) -> None:
        state = self.state_of(active)
        delta = await active.executor.delta()
        notional = max(delta.long_usd, delta.short_usd, active.executor.dust_usd)
        p, now = self.params, self.clock.now()
        if mode == "emergency":
            plan = make_exit_plan(notional, p.twap_max_child_notional_usd, p.emergency_exit_target_seconds, now,
                                  allow_larger_children=True)
            waypoint = S.EMERGENCY_EXIT
        elif mode == "controlled":
            plan = make_exit_plan(notional, p.twap_max_child_notional_usd, p.controlled_exit_duration_minutes * 60, now)
            waypoint = S.CONTROLLED_EXIT
        else:
            plan = make_exit_plan(notional, p.twap_max_child_notional_usd, p.normal_exit_duration_minutes * 60, now)
            waypoint = S.EXIT_PENDING if state is S.HEDGED_OPEN else None
        if waypoint is not None and state is not waypoint:
            self._set(active, waypoint, reason)
        if self.state_of(active) is not S.EXITING:
            self._set(active, S.EXITING, reason)
        active.exit_plan, active.exit_mode, active.exit_done, active.cleanup_attempts = plan, mode, 0, 0
        active.exit_generation += 1
        active.exit_reason = reason
        self.repo.merge_context(active.id, exit={"mode": mode, "reason": reason, "slices": plan.slices,
                                                 "child": plan.child_notional, "generation": active.exit_generation,
                                                 "start": plan.start.isoformat(), "interval_s": plan.interval.total_seconds(),
                                                 "end_at": plan.end.isoformat()},
                                exit_progress=0)
        kind = {"emergency": "emergency_exit_10pct", "controlled": "controlled_exit_30pct"}.get(mode)
        if kind:
            await self.notifier.alert(kind, f"{mode} exit started: {reason}", severity="critical", trade_id=active.id)

    async def _run_exit(self, active: ActiveTrade) -> None:
        if self.state_of(active) is not S.EXITING or active.exit_plan is None:
            return
        plan, now = active.exit_plan, self.clock.now()
        if active.exit_done < plan.slices:
            idx = plan.due_index(now, active.exit_done)
            if idx is None:
                return
            attempt_index, slices_left = idx, plan.slices - idx
        else:
            delta = await active.executor.delta()
            if active.executor.flat(delta):
                await self._close(active)
                return
            active.cleanup_attempts += 1
            if active.cleanup_attempts > MAX_CLEANUP_ATTEMPTS:
                self._set(active, S.MANUAL_INTERVENTION_REQUIRED, "exit could not reach a flat position")
                self.repo.set_kv("block_new_trades", {"reason": f"trade {active.id} needs manual intervention"})
                await self.notifier.alert("manual_intervention_required", "exit did not complete",
                                          severity="critical", trade_id=active.id)
                return
            attempt_index, slices_left = plan.slices + active.cleanup_attempts, 1
        try:
            res = await active.executor.exit_slice(attempt_index, slices_left, attempt=active.exit_generation)
        except SliceTooSmall:
            active.exit_done = max(active.exit_done + 1, plan.slices) if slices_left <= 1 else active.exit_done + 1
            delta = await active.executor.delta()
            if active.executor.flat(delta):
                await self._close(active)
            return
        except ExchangeError as exc:
            await self.notifier.alert("venue_disconnect", f"exit slice not sent: {exc}", severity="warning",
                                      trade_id=active.id)
            return
        if active.exit_done < plan.slices:
            active.exit_done += 1
        self.repo.merge_context(active.id, exit_progress=active.exit_done)
        self._record_slice(active, "exit", res, plan.child_notional)
        await self._sync_orders(active)
        if res.unresolved:
            for sub in (res.long, res.short):
                if sub.status == "UNRESOLVED":
                    active.unresolved[sub.client_order_id] = sub.leg
            return
        delta = await active.executor.delta()
        if active.executor.unsafe(delta) and not active.executor.flat(delta):
            await self._heal(active, S.EXITING, entry=False)
            delta = await active.executor.delta()
        if active.exit_done >= plan.slices and active.executor.flat(delta):
            await self._close(active)

    async def _close(self, active: ActiveTrade) -> None:
        await self._sync_orders(active)
        try:  # one last equity reading AFTER the final orders, so the profit curve ends at the true final value
            lq, sq = await asyncio.gather(*(leg.venue.get_account_equity() for leg in active.legs))
            self.repo.add_equity_snapshot(active.id, long_equity=lq.equity_usd, short_equity=sq.equity_usd,
                                          long_ratio=lq.equity_usd / active.baseline_long,
                                          short_ratio=sq.equity_usd / active.baseline_short, net_delta_usd=D(0))
        except ExchangeError:
            pass
        pnl = self.compute_trade_pnl(active)
        self._set(active, S.CLOSED, active.exit_reason or "closed")
        self.repo.update_trade(active.id, exit_reason=active.exit_reason, pnl=pnl.as_dict(),
                               max_delta_imbalance_usd=active.max_delta_usd)
        self.repo.release_lease(active.id, self.owner)
        self._persist_accounts(active)
        self.active.pop(active.id, None)
        await self.notifier.alert("trade_closed", f"closed: {active.exit_reason}", trade_id=active.id)
        await self.notifier.alert("final_pnl", f"total {pnl.total:+.4f} USD (funding {pnl.funding_net:+.4f}, "
                                  f"hedge {pnl.hedge_trading:+.4f}, fees {pnl.fees:.4f}, "
                                  f"slippage est. {pnl.slippage_estimate:.4f})", trade_id=active.id)

    def live_valuation(self, active: ActiveTrade):
        """(P&L if the open legs were valued now, before closing costs; how it was priced).

        Same ledger as the final P&L, so it converges to it: fees and funding already paid are included.
        How it was priced, "status":
          live               both legs have a price from this cycle
          last_known_prices  a leg's venue did not answer this cycle: its last price seen within
                             LAST_KNOWN_PRICE_MAX_AGE is used, and its age is reported per leg
          unavailable        an open leg has no price recent enough. The total would leave that leg's whole
                             position out (a false loss or gain of its full notional), so callers must not show it.
        """
        now = self.clock.now()
        current = dict(active.last_obs.marks) if active.last_obs else {}
        marks, age = dict(current), {}
        for role, (seen, mark) in active.last_marks.items():
            if role not in marks and now - seen <= LAST_KNOWN_PRICE_MAX_AGE:
                marks[role], age[role] = mark, (now - seen).total_seconds()
        pnl = compute_pnl(list(active.ledger), list(active.funding_ledger), residual_marks=marks)
        stale = {role: secs for role, secs in age.items() if role in pnl.priced_legs}
        status = "unavailable" if not pnl.complete else ("last_known_prices" if stale else "live")
        return pnl, {"status": status, "stale_legs_seconds": stale, "unpriced_legs": list(pnl.unpriced_legs)}

    def live_pnl(self, active: ActiveTrade):
        return self.live_valuation(active)[0]

    def compute_trade_pnl(self, active: ActiveTrade):
        """P&L from the in-memory ledger of fills and funding, so it works while the database is down."""
        return compute_pnl(list(active.ledger), list(active.funding_ledger))

    # ---- degraded / reconciliation ------------------------------------------------------------
    async def _enter_degraded(self, active: ActiveTrade, state: TradeState, decision: RiskDecision) -> None:
        if state not in (S.DEGRADED, S.RECONCILING):
            active.resume_state = state
            self._set(active, S.DEGRADED, ",".join(decision.reasons))
            self.repo.merge_context(active.id, resume_state=state.value)
            await self.notifier.alert("venue_disconnect", f"degraded: {', '.join(decision.reasons)}",
                                      severity="critical", trade_id=active.id)

    async def _resume_from_degraded(self, active: ActiveTrade, state: TradeState) -> None:
        if state in (S.DEGRADED, S.ERROR):
            self._set(active, S.RECONCILING, "reconciling before resuming")
        if not await self.reconcile(active):
            return
        target = active.resume_state or S.HEDGED_OPEN
        if target not in EXPOSED or target in (S.DEGRADED, S.RECONCILING):
            target = S.HEDGED_OPEN
        self._set(active, target, "reconciled: venues and database agree")
        await self.notifier.alert("entry_resumed", f"resumed in {target.value}", trade_id=active.id)

    async def reconcile(self, active: ActiveTrade) -> bool:
        """Query the venues, adopt their state as truth and resolve ambiguous orders (sections 53, 55)."""
        try:
            for cid, role in list(active.unresolved.items()):
                leg = active.executor.long if role == "long" else active.executor.short
                try:
                    await leg.venue.get_order(client_order_id=cid)
                except ExchangeRequestError:
                    return False
                except ExchangeError:
                    pass  # confirmed absent: it never reached the venue
                active.unresolved.pop(cid, None)
            for leg in active.legs:  # prove both venues are readable before trusting anything
                await leg.venue.get_account_equity()
                await leg.venue.get_position(leg.meta.symbol)
                await leg.venue.get_open_orders(leg.meta.symbol)
            await self._sync_orders(active)
        except ExchangeError:
            return False
        return True

    # ==== recovery (section 55) ==================================================================
    async def recover(self) -> list[str]:
        """Rebuild unfinished trades after a restart. Returns ids that need a human."""
        manual: list[str] = []
        for row in self.repo.unfinished_trades():
            state = TradeState(row.state)
            if state not in EXPOSED:
                self.repo.set_state(row.id, S.REJECTED, "process restarted before any order was sent")
                continue
            if state is S.MANUAL_INTERVENTION_REQUIRED:
                manual.append(row.id)
                continue
            try:
                legs = await self.provider.rebuild(row, self.params)
                plan_ctx = (row.context or {}).get("entry_plan", {})
                p = self.params
                heals = sum(1 for o in self.repo.orders_for(row.id) if "-h" in o.client_order_id)
                executor = PairExecutor(row.id, legs[0], legs[1], self.clock, p,
                                        events=lambda kind, data, tid=row.id: self.repo.audit(kind, data, tid),
                                        heal_seq_start=heals + 1000)  # skip past ids used before the crash
                child = D(str(plan_ctx.get("child", p.twap_max_child_notional_usd)))
                slices = int(plan_ctx.get("slices", 1))
                interval = timedelta(seconds=float(plan_ctx.get("interval_s", 60)))
                slice_qty = tuple(D(q) for q in plan_ctx.get("slice_qty") or ()) or None
                plan = TwapPlan(child * slices, child, slices, interval, as_utc(row.opened_at), slice_qty)
                active = ActiveTrade(row.id, row.asset, executor, as_utc(row.opened_at), plan, EdgeDecayMonitor(p),
                                     row.baseline_long_equity, row.baseline_short_equity, state=state,
                                     max_delta_usd=row.max_delta_imbalance_usd)
                active.entry_delay = timedelta(seconds=float((row.context or {}).get("entry_delay_s", 0)))
                active.asset_class = (row.context or {}).get("asset_class")
                self._load_ledgers(active)
                # entry progress = slices whose orders exist; exit progress restarts safely (reduce-only)
                done = self.repo.slices(row.id, "entry")
                active.entry_done = len(done)
                active.synced = {o.client_order_id for o in self.repo.orders_for(row.id)}
                ctx_exit = (row.context or {}).get("exit")
                if state in (S.CONTROLLED_EXIT, S.EMERGENCY_EXIT, S.EXITING, S.EXIT_PENDING) and ctx_exit:
                    active.exit_mode, active.exit_reason = ctx_exit["mode"], ctx_exit["reason"]
                    active.exit_generation = int(ctx_exit.get("generation", 1))
                    now = datetime.fromisoformat(ctx_exit["start"]) if ctx_exit.get("start") else self.clock.now()
                    n = int(ctx_exit["slices"])
                    active.exit_plan = TwapPlan(D(str(ctx_exit["child"])) * n, D(str(ctx_exit["child"])), n,
                                                timedelta(seconds=float(ctx_exit.get("interval_s",
                                                    2 if active.exit_mode == "emergency" else 60))), now,
                                                end_at=datetime.fromisoformat(ctx_exit["end_at"]) if ctx_exit.get("end_at") else None)
                    active.exit_done = int((row.context or {}).get("exit_progress", len(self.repo.slices(row.id, "exit"))))
                if state in (S.DEGRADED, S.RECONCILING, S.ERROR):
                    rs = (row.context or {}).get("resume_state")
                    active.resume_state = TradeState(rs) if rs else S.HEDGED_OPEN
                self.active[row.id] = active
                # venue truth beats the database: adopt it, then require a consistent, safe position
                if not await self.reconcile(active):
                    raise ExchangeError("venue state cannot be read")
                await self.notifier.alert("recovery", f"trade {row.id} restored in {state.value}", trade_id=row.id)
            except Exception as exc:  # noqa: BLE001
                self.active.pop(row.id, None)
                self._set_manual(row.id, f"recovery failed: {type(exc).__name__}: {exc}")
                manual.append(row.id)
                await self.notifier.alert("manual_intervention_required", "recovery could not establish state",
                                          severity="critical", trade_id=row.id)
        if manual:
            self.repo.set_kv("block_new_trades", {"reason": f"manual intervention needed for {', '.join(manual)}"})
        return manual

    def _load_ledgers(self, active: ActiveTrade) -> None:
        from app.persistence.models import Fill
        with self.repo.session() as s:
            refs = {f.client_order_id: f.reference_price for f in s.query(Fill).filter(Fill.trade_id == active.id)}
        for o in self.repo.orders_for(active.id):
            if o.filled_quantity > 0 and o.average_fill_price:
                active.ledger.append(OrderRecord(o.leg, o.side, o.filled_quantity, o.average_fill_price, o.fee_usd,
                                                 refs.get(o.client_order_id)))
        roles = {leg.venue.name: leg.role for leg in active.legs}
        payments = self.repo.funding_payments(active.id)
        active.funding_ledger = [FundingRecord(roles.get(f.venue, "long"), f.expected_payment, f.actual_payment)
                                 for f in payments]
        for f in payments:  # a restart must not pay a settlement that was already paid before it
            role, at = roles.get(f.venue), as_utc(f.timestamp)
            if role and (role not in active.last_settled or at > active.last_settled[role]):
                active.last_settled[role] = at

    def _set_manual(self, trade_id: str, reason: str) -> None:
        try:
            self.repo.set_state(trade_id, S.MANUAL_INTERVENTION_REQUIRED, reason)
        except Exception:  # noqa: BLE001 - already terminal or invalid: leave the record as it is
            pass

    # ==== driving the engine =====================================================================
    async def run_once(self) -> dict[str, RiskDecision | None]:
        return {tid: await self.step(t) for tid, t in list(self.active.items())}

    def recommended_delay(self) -> float:
        """Seconds until the next cycle: fast during an emergency exit, slower while just holding."""
        delay = 15.0
        for t in self.active.values():
            state = self.state_of(t)
            if t.exit_mode == "emergency" and state is S.EXITING:
                return 1.0
            if state in (S.EXITING, S.ENTERING, S.DEGRADED, S.RECONCILING):
                delay = min(delay, 5.0)
        return delay

    def summary(self) -> list[dict[str, Any]]:
        out = []
        for t in self.active.values():
            o = t.last_obs
            out.append({"id": t.id, "asset": t.asset, "state": self.state_of(t).value, "exit_mode": t.exit_mode,
                        "entry_slices": f"{t.entry_done}/{t.entry_plan.slices}",
                        "net_delta_usd": None if not o or not o.delta else str(o.delta.net_usd),
                        "long_equity": None if not o or o.long_equity is None else str(o.long_equity),
                        "short_equity": None if not o or o.short_equity is None else str(o.short_equity)})
        return out
