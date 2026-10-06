"""Settling a paper trade whose venue is gone for good (app.tools.settle_paper_trade)."""

from decimal import Decimal

import pytest

from app.core.trading_config import TradingSettings
from app.domain.trade import TradeState
from app.tools.settle_paper_trade import SettleError, settle
from tests.execution.test_engine import World, enter_fully, run

D = Decimal


async def same_name(venue: str) -> str:
    return venue  # the test venues "L" and "S" are their own adapters


def stranded():
    """A hedged paper trade whose short venue stopped answering: the engine holds it DEGRADED."""
    w = World()
    enter_fully(w)
    w.advance_and_step(3, minutes=1)  # equity snapshots that value both legs
    w.engine.active["arb0001"].executor.short.venue.connected = False
    w.src["L"].mid = D(104)  # moves after the short venue went dark are not the trade's to book
    w.advance_and_step(2, minutes=1)
    assert w.state() in (TradeState.DEGRADED, TradeState.RECONCILING)
    return w


def do_settle(w, **kw):
    settings = TradingSettings(_env_file=None, target_notional_per_leg_usd=500)
    return run(settle(w.repo, settings, "arb0001", "venue_excluded", clock=w.clock, name_for=same_name, **kw))


def test_a_dry_run_prices_both_legs_at_their_last_joint_moment_and_writes_nothing():
    w = stranded()
    orders = len(w.repo.orders_for("arb0001"))
    plan = do_settle(w)
    assert plan["applied"] is False and plan["path"][-1] == "CLOSED"
    assert D(plan["prices"]["long"]) == D(100) and D(plan["prices"]["short"]) == D(100)  # not the later 104
    assert {o["leg"] for o in plan["closing_orders"]} == {"long", "short"}
    assert len(w.repo.orders_for("arb0001")) == orders and w.state() is not TradeState.CLOSED


def test_apply_closes_both_legs_flat_books_the_pnl_and_cannot_run_twice():
    w = stranded()
    plan = do_settle(w, apply=True)
    row = w.repo.get_trade("arb0001")
    assert row.state == "CLOSED" and row.exit_reason == "venue_excluded" and row.owner is None
    assert row.pnl == plan["pnl"] and D(row.pnl["unrealized_residual"]) == 0
    fees = sum(o.fee_usd for o in w.repo.orders_for("arb0001"))
    assert D(row.pnl["fees"]) == fees  # entry and settlement orders are all in the ledger
    for role in ("long", "short"):
        positions = w.repo.load_paper_account(f"arb0001:{role}")["positions"]
        assert all(D(p["q"]) == 0 for p in positions.values())
    assert row.context["settlement"]["method"].startswith("last equity snapshot")
    assert w.alerts("trade_closed") and w.alerts("final_pnl")
    with pytest.raises(SettleError, match="nothing open"):
        do_settle(w)


def test_operator_prices_are_used_when_given():
    w = stranded()
    plan = do_settle(w, prices={"long": D(101), "short": D(102)})
    assert plan["method"] == "prices given by the operator"
    assert [o["price"] for o in plan["closing_orders"]] == ["101", "102"]


def test_a_live_trade_is_refused():
    w = stranded()
    w.repo.update_trade("arb0001", run_mode="live")
    with pytest.raises(SettleError, match="closed on the venues"):
        do_settle(w)


def test_a_leg_that_changed_after_the_joint_snapshot_needs_operator_prices():
    w = stranded()
    w.engine._persist_accounts(w.engine.active["arb0001"])
    acc = w.repo.load_paper_account("arb0001:long")
    acc["funding"].append({"t": w.clock.now().isoformat(), "symbol": "X", "rate": "0", "notional": "0",
                           "payment": "0.01"})
    w.repo.save_paper_account("arb0001:long", acc)
    with pytest.raises(SettleError, match="--long-price"):
        do_settle(w)
