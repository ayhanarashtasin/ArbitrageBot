from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.clock import ManualClock
from app.domain.market import OrderState, OrderStatus, Side
from app.domain.trade import InvalidTransition, TradeState, allowed_next, check_transition
from app.persistence.models import as_utc
from app.persistence.repository import Repository
from tests.unit.helpers import T0

D = Decimal


def repo():
    clock = ManualClock(T0)
    return Repository("sqlite://", clock), clock


def new_trade(r, tid="arb0001", state="ENTERING"):
    return r.create_trade(id=tid, asset="BTC", run_mode="paper", long_venue="A", short_venue="B",
                          long_symbol="BTCUSDT", short_symbol="BTC", state=state, opened_at=T0,
                          target_notional_per_leg=D(1000), leverage=D(2), context={"zd": {"mean": D("0.0123")}})


# ---- state machine ---------------------------------------------------------------------------

def test_happy_path_transitions():
    path = [TradeState.DISCOVERED, TradeState.VALIDATING_HISTORY, TradeState.DIRECT_VERIFYING,
            TradeState.WAITING_FOR_LOW_VOL, TradeState.ENTRY_READY, TradeState.ENTERING, TradeState.HEDGED_OPEN,
            TradeState.EXIT_PENDING, TradeState.EXITING, TradeState.CLOSED]
    for a, b in zip(path, path[1:]):
        check_transition(a, b)


@pytest.mark.parametrize("a,b", [
    (TradeState.DISCOVERED, TradeState.ENTERING), (TradeState.ENTRY_READY, TradeState.HEDGED_OPEN),
    (TradeState.CLOSED, TradeState.ENTERING), (TradeState.REJECTED, TradeState.ENTRY_READY),
    (TradeState.HEDGED_OPEN, TradeState.ENTERING), (TradeState.EMERGENCY_EXIT, TradeState.HEDGED_OPEN),
    (TradeState.EXITING, TradeState.HEDGED_OPEN),
])
def test_illegal_transitions_are_refused(a, b):
    with pytest.raises(InvalidTransition):
        check_transition(a, b)


def test_exits_and_failures_reachable_from_holding():
    nxt = allowed_next(TradeState.HEDGED_OPEN)
    assert {TradeState.CONTROLLED_EXIT, TradeState.EMERGENCY_EXIT, TradeState.EXIT_PENDING,
            TradeState.DEGRADED, TradeState.RECONCILING, TradeState.MANUAL_INTERVENTION_REQUIRED} <= nxt
    assert allowed_next(TradeState.CLOSED) == frozenset()
    assert TradeState.DEGRADED in allowed_next(TradeState.ENTERING)
    assert TradeState.HEDGED_OPEN in allowed_next(TradeState.RECONCILING)  # resume once consistent


def test_emergency_can_be_reached_from_every_exposed_state():
    from app.domain.trade import EXPOSED
    for s in EXPOSED - {TradeState.EMERGENCY_EXIT}:
        assert TradeState.EMERGENCY_EXIT in allowed_next(s) or TradeState.EXITING in allowed_next(s)


# ---- repository ------------------------------------------------------------------------------

def test_trade_roundtrip_keeps_exact_decimals_and_json():
    r, _ = repo()
    new_trade(r)
    t = r.get_trade("arb0001")
    assert t.target_notional_per_leg == D(1000) and isinstance(t.leverage, Decimal)
    assert t.context["zd"]["mean"] == "0.0123"  # Decimal became an exact string, not a float
    r.update_trade("arb0001", baseline_long_equity=D("1234.56789012345678"))
    assert r.get_trade("arb0001").baseline_long_equity == D("1234.56789012345678")


def test_set_state_validates_and_audits():
    r, clock = repo()
    new_trade(r, state="HEDGED_OPEN")
    r.set_state("arb0001", TradeState.CONTROLLED_EXIT, "equity 29%")
    with pytest.raises(InvalidTransition):
        r.set_state("arb0001", TradeState.ENTERING)
    assert r.get_trade("arb0001").state == "CONTROLLED_EXIT"  # unchanged by the refused move
    clock.advance(hours=1)
    r.set_state("arb0001", TradeState.EXITING, "reducing")
    r.set_state("arb0001", TradeState.CLOSED, "flat")
    t = r.get_trade("arb0001")
    assert t.state == "CLOSED" and as_utc(t.closed_at) == T0 + timedelta(hours=1)
    kinds = [(a.kind, a.data.get("to")) for a in r.audit_for("arb0001") if a.kind == "state_transition"]
    assert [k[1] for k in kinds] == ["CONTROLLED_EXIT", "EXITING", "CLOSED"]


def test_unfinished_trades_exclude_terminal_states():
    r, _ = repo()
    new_trade(r, "arb0001", "HEDGED_OPEN")
    new_trade(r, "arb0002", "CLOSED")
    new_trade(r, "arb0003", "REJECTED")
    new_trade(r, "arb0004", "MANUAL_INTERVENTION_REQUIRED")
    assert {t.id for t in r.unfinished_trades()} == {"arb0001", "arb0004"}
    assert r.next_trade_id() == "arb0005"


def test_orders_are_keyed_by_client_id_and_updated_not_duplicated():
    r, _ = repo()
    new_trade(r)
    o = OrderState(venue="A", symbol="BTCUSDT", order_id="1", client_order_id="arb0001-s0000-L-a01", side=Side.BUY,
                   status=OrderStatus.PARTIALLY_FILLED, requested_quantity=D(1), filled_quantity=D("0.4"),
                   average_fill_price=D(100), fee_paid_usd=D("0.02"), reduce_only=False, received_at=T0)
    r.upsert_order("arb0001", "long", o, False)
    r.upsert_order("arb0001", "long", o.model_copy(update={"status": OrderStatus.FILLED, "filled_quantity": D(1)}), False)
    rows = r.orders_for("arb0001")
    assert len(rows) == 1 and rows[0].status == "FILLED" and rows[0].filled_quantity == 1
    assert r.order_exists("arb0001-s0000-L-a01") and not r.order_exists("nope")


def test_fills_are_not_recorded_twice():
    r, _ = repo()
    new_trade(r)
    for _ in range(2):
        r.add_fill("arb0001", "cid", D(1), D(100), D("0.05"), D(100), T0)
    from app.persistence.models import Fill
    with r.session() as s:
        assert len(s.query(Fill).all()) == 1


def test_lease_gives_one_owner_at_a_time():
    r, clock = repo()
    new_trade(r)
    assert r.acquire_lease("arb0001", "worker-a", 60)
    assert not r.acquire_lease("arb0001", "worker-b", 60)  # a second worker cannot manage the same trade
    assert r.acquire_lease("arb0001", "worker-a", 60)  # the owner may renew
    clock.advance(seconds=61)
    assert r.acquire_lease("arb0001", "worker-b", 60)  # expired lease can be taken over
    r.release_lease("arb0001", "worker-a")  # a non-owner cannot release
    assert not r.acquire_lease("arb0001", "worker-a", 60)
    r.release_lease("arb0001", "worker-b")
    assert r.acquire_lease("arb0001", "worker-a", 60)


def test_paper_account_and_kv_roundtrip():
    r, _ = repo()
    r.save_paper_account("A:BTC", {"balance": "1000"})
    r.save_paper_account("A:BTC", {"balance": "1001"})
    assert r.load_paper_account("A:BTC") == {"balance": "1001"} and r.load_paper_account("x") is None
    r.set_kv("kill", {"pause": True})
    assert r.get_kv("kill") == {"pause": True} and r.get_kv("none") is None


def test_alerts_and_risk_events_and_funding():
    r, _ = repo()
    new_trade(r)
    aid = r.add_alert("partial_fill", "warning", "long 100 short 62", "arb0001")
    r.mark_alert_delivered(aid)
    assert r.alerts("partial_fill")[0].delivered
    r.add_risk_event("arb0001", "HEAL_DELTA", 3, ["delta_imbalance"])
    assert r.risk_events("arb0001")[0].reasons == ["delta_imbalance"]
    r.add_funding_payment("arb0001", "A", "BTCUSDT", T0, D("0.0001"), D(1000), D("-0.1"), D("-0.1"))
    fp = r.funding_payments("arb0001")[0]
    assert fp.difference == 0 and fp.rate == D("0.0001")
