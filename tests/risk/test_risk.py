from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.clock import ManualClock
from app.core.trading_config import TradingSettings
from app.execution.delta import DeltaState
from app.risk.engine import RiskAction, RiskInputs, evaluate
from app.risk.exit_policy import EdgeDecayMonitor, UnknownExitPolicy
from app.risk.killswitch import KillSwitch
from tests.unit.helpers import T0

D = Decimal
P = TradingSettings(_env_file=None).resolve()


def inputs(long_eq=1000, short_eq=1000, **kw):
    return RiskInputs(now=T0, baseline_long_equity=D(1000), baseline_short_equity=D(1000),
                      long_equity=None if long_eq is None else D(str(long_eq)),
                      short_equity=None if short_eq is None else D(str(short_eq)), **kw)


def action(**kw):
    return evaluate(inputs(**kw), P)


# ---- equity thresholds (LOCKED) --------------------------------------------------------------

@pytest.mark.parametrize("pct,expected", [
    (100, RiskAction.CONTINUE), (31, RiskAction.CONTINUE), (30.01, RiskAction.CONTINUE),
    (30, RiskAction.CONTROLLED_EXIT), (29, RiskAction.CONTROLLED_EXIT),
    (10, RiskAction.CONTROLLED_EXIT),  # emergency is strictly below 10 %
    (9.99, RiskAction.EMERGENCY_EXIT), (0, RiskAction.EMERGENCY_EXIT),
])
def test_equity_boundaries_on_either_side(pct, expected):
    eq = D(str(pct)) * 10
    assert evaluate(inputs(long_eq=eq), P).action is expected
    assert evaluate(inputs(short_eq=eq), P).action is expected


def test_the_worse_side_decides():
    d = evaluate(inputs(long_eq=250, short_eq=50), P)
    assert d.action is RiskAction.EMERGENCY_EXIT
    assert d.long_equity_ratio == D("0.25") and d.short_equity_ratio == D("0.05")


def test_emergency_is_checked_before_controlled():
    d = action(long_eq=290, short_eq=90)
    assert d.action is RiskAction.EMERGENCY_EXIT and d.priority == 2
    assert "equity_at_or_below_controlled_ratio" in d.reasons  # still reported


# ---- liquidation / margin override -----------------------------------------------------------

def test_liquidation_distance_forces_emergency_before_equity_trigger():
    # paper default 1 %: a backstop behind the spec's equity exits (at 4x liquidation is about 24.5 % away)
    d = action(long_liquidation_distance_pct=D("0.9"))
    assert d.action is RiskAction.EMERGENCY_EXIT and d.priority == 1
    assert action(long_liquidation_distance_pct=D(1)).action is RiskAction.CONTINUE  # limit is inclusive-safe
    assert action(long_liquidation_distance_pct=D("24.5")).action is RiskAction.CONTINUE  # a fresh 4x position
    assert action(short_liquidation_distance_pct=D("0.5")).action is RiskAction.EMERGENCY_EXIT


def test_margin_utilization_forces_emergency():
    # margin ratio = maintenance margin / equity; the venues liquidate at 100 %, the backstop is 80 %
    assert action(short_margin_utilization_pct=D(81)).action is RiskAction.EMERGENCY_EXIT
    assert action(short_margin_utilization_pct=D(80)).action is RiskAction.CONTINUE
    assert action(short_margin_utilization_pct=D(2)).action is RiskAction.CONTINUE  # a fresh 4x position


# ---- delta -----------------------------------------------------------------------------------

def test_moderate_imbalance_heals_severe_flattens():
    bad = DeltaState(D(100), D(40))
    d = action(delta=bad, delta_unsafe_since=T0 - timedelta(milliseconds=5000))
    assert d.action is RiskAction.HEAL_DELTA and d.priority == 5
    d = action(delta=bad, delta_unsafe_since=T0 - timedelta(milliseconds=P.delta_hedge_timeout_ms + 1))
    assert d.action is RiskAction.EMERGENCY_EXIT and d.priority == 3 and "severe_unresolved_delta_imbalance" in d.reasons
    assert action(delta=bad, delta_unsafe_since=T0 - timedelta(milliseconds=P.delta_hedge_timeout_ms)).action \
        is RiskAction.HEAL_DELTA  # exactly at the timeout is not yet "beyond"


def test_safe_delta_and_dust_do_not_trigger():
    assert action(delta=DeltaState(D(100), D(98))).action is RiskAction.CONTINUE
    assert action(delta=DeltaState(D(0), D("0.1")), delta_dust_usd=D(5)).action is RiskAction.CONTINUE


# ---- failures and stale data -----------------------------------------------------------------

def test_unreachable_venue_or_unknown_state_is_degraded_not_assumed():
    assert action(venues_reachable={"long": False, "short": True}).action is RiskAction.DEGRADED
    assert action(unresolved_orders=True).action is RiskAction.DEGRADED
    assert action(long_eq=None).action is RiskAction.DEGRADED  # equity unreadable: never assume it is fine


def test_stale_market_data_pauses_slices():
    d = action(data_age_ms={"long": P.max_market_data_age_ms + 1, "short": 10})
    assert d.action is RiskAction.PAUSE_SLICES
    assert action(data_age_ms={"long": P.max_market_data_age_ms, "short": 10}).action is RiskAction.CONTINUE


def test_failure_outranks_controlled_exit_per_section_37():
    d = action(long_eq=200, venues_reachable={"long": False, "short": True})
    assert d.action is RiskAction.DEGRADED and d.priority == 4
    assert "equity_at_or_below_controlled_ratio" in d.reasons


def test_emergency_outranks_degraded():
    d = action(long_eq=50, venues_reachable={"long": True, "short": False})
    assert d.action is RiskAction.EMERGENCY_EXIT


def test_normal_exit_is_lowest_priority():
    assert action(normal_exit_signal=True).action is RiskAction.NORMAL_EXIT
    assert action(normal_exit_signal=True, long_eq=250).action is RiskAction.CONTROLLED_EXIT


def test_zero_baseline_is_not_divided():
    d = evaluate(RiskInputs(now=T0, baseline_long_equity=D(0), baseline_short_equity=D(1000),
                            long_equity=D(10), short_equity=D(1000)), P)
    assert d.long_equity_ratio is None  # invalid baseline is never turned into a ratio


# ---- normal exit policy ----------------------------------------------------------------------

def test_edge_decay_needs_confirmation_and_resets_on_recovery():
    m = EdgeDecayMonitor(P)
    t = T0
    assert m.update(t, T0, D("0.0001")) is None
    assert m.update(t + timedelta(minutes=1), T0, D(0)) is None  # decay starts
    assert m.update(t + timedelta(minutes=30), T0, D("-0.0001")) is None
    assert m.update(t + timedelta(minutes=40), T0, D("0.0002")) is None  # recovered: window resets
    assert m.below_since is None
    assert m.update(t + timedelta(minutes=50), T0, D(0)) is None
    sig = m.update(t + timedelta(minutes=50 + P.normal_exit_confirm_minutes), T0, D(0))
    assert sig is not None and sig.reason == "edge_decayed"


def test_max_hold_forces_exit_even_with_good_edge():
    m = EdgeDecayMonitor(P)
    sig = m.update(T0 + timedelta(hours=P.max_hold_hours), T0, D("0.001"))
    assert sig is not None and sig.reason == "max_hold_reached"
    assert m.update(T0 + timedelta(hours=P.max_hold_hours - 1), T0, D("0.001")) is None


def test_unknown_policy_fails_closed():
    params = P.model_copy(update={"normal_exit_policy": "hold_forever"})
    with pytest.raises(UnknownExitPolicy):
        EdgeDecayMonitor(params)


# ---- kill switch -----------------------------------------------------------------------------

def test_kill_switch_states_and_audit():
    log = []
    ks = KillSwitch(ManualClock(T0), audit=lambda k, d: log.append((k, d)))
    assert ks.entries_allowed
    ks.pause("alice", "maintenance")
    assert not ks.entries_allowed and not ks.emergency_flatten
    ks.resume("alice", "done")
    assert ks.entries_allowed
    ks.flatten("bob", "venue incident")
    assert ks.emergency_flatten and not ks.entries_allowed
    with pytest.raises(RuntimeError):
        ks.resume("bob", "oops")  # a latched flatten cannot be quietly undone by resume
    ks.clear_flatten("bob", "all clear")
    ks.resume("bob", "back")
    assert ks.entries_allowed
    assert [e["action"] for e in ks.history] == ["PAUSE_NEW_ENTRIES", "RESUME_ENTRIES", "EMERGENCY_FLATTEN",
                                                 "CLEAR_EMERGENCY_FLATTEN", "RESUME_ENTRIES"]
    assert len(log) == 5 and all(d["actor"] and d["reason"] for _, d in log)


def test_moderate_imbalance_yields_to_an_equity_exit_but_outranks_normal_exit():
    bad = DeltaState(D(100), D(40))
    assert action(delta=bad, long_eq=250).action is RiskAction.CONTROLLED_EXIT  # the exit repairs delta itself
    assert action(delta=bad, normal_exit_signal=True).action is RiskAction.HEAL_DELTA  # repair before a normal exit
