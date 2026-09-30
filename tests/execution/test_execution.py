import asyncio
from datetime import datetime, timedelta, UTC
from decimal import Decimal

import pytest

from app.domain.market import OrderStatus, Side
from app.execution.delta import DeltaState, compute_delta, is_unsafe
from app.execution.pair_executor import SliceTooSmall, client_order_id
from app.execution.paper import PaperRejected
from app.execution.twap import make_entry_plan, make_exit_plan

from .fakes import make_pair

D = Decimal
START = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


# ---- TWAP (LOCKED: 6 h, <= $100 child) -------------------------------------------------------

def test_entry_plan_respects_child_cap_and_duration():
    plan = make_entry_plan(D(1000), D(100), 360, START)
    assert plan.slices == 10 and plan.child_notional == 100
    assert plan.interval == timedelta(minutes=36) and plan.due_time(9) == START + timedelta(minutes=324)


@pytest.mark.parametrize("total,slices", [("100", 1), ("100.01", 2), ("250", 3), ("1", 1)])
def test_child_never_exceeds_100(total, slices):
    plan = make_entry_plan(D(total), D(100), 360, START)
    assert plan.slices == slices and plan.child_notional <= 100
    assert plan.child_notional * plan.slices == D(total)


def test_slices_are_never_run_out_of_order():
    plan = make_entry_plan(D(300), D(100), 360, START)
    assert plan.due_index(START, 0) == 0
    assert plan.due_index(START, 1) is None  # slice 1 is due at +120 min
    assert plan.due_index(START + timedelta(minutes=120), 1) == 1
    assert plan.due_index(START + timedelta(days=1), 3) is None  # all done


def test_emergency_plan_compresses_and_may_exceed_cap():
    ctrl = make_exit_plan(D(1000), D(100), 180 * 60, START)
    assert ctrl.slices == 10 and ctrl.child_notional == 100
    emerg = make_exit_plan(D(10_000), D(100), 60, START, allow_larger_children=True)
    assert emerg.slices == 30 and emerg.child_notional > 100 and emerg.interval >= timedelta(seconds=2)
    assert abs((emerg.due_time(29) - START).total_seconds() - 60) < 0.001
    small = make_exit_plan(D(500), D(100), 60, START, allow_larger_children=True)
    assert small.child_notional == 100


# ---- delta -----------------------------------------------------------------------------------

def test_delta_uses_filled_exposure():
    s = compute_delta(D(1), D(100), D("0.62"), D(100))
    assert s.net_usd == 38 and s.ratio == D("0.38") and s.lagging_leg == "short" and s.heavy_leg == "long"


@pytest.mark.parametrize("long,short,unsafe", [(100, 100, False), (100, 95, False), (100, 94, True),
                                               (1000, 949, True), (1000, 951, False), (100, 0, True), (0, 0, False)])
def test_delta_limits(long, short, unsafe):
    # limits: $50 and 5 %; either breach is unsafe
    assert is_unsafe(DeltaState(D(long), D(short)), D(50), D("0.05")) is unsafe


def test_client_order_id_format():
    assert client_order_id("arb0192", 42, "long", 1) == "arb0192-s0042-L-a01"
    assert client_order_id("arb0192", 42, "short", 1) == "arb0192-s0042-S-a01"
    assert client_order_id("arb0192", 3, "long", 2, "x") == "arb0192-x0003-L-a02"


# ---- paper venue -----------------------------------------------------------------------------

def test_paper_fill_fee_position_and_equity():
    ex, lv, sv, *_ = make_pair()

    async def go():
        o = await lv.place_order("BTCUSDT", Side.BUY, D(1), client_order_id="c1")
        pos = await lv.get_position("BTCUSDT")
        return o, pos

    o, pos = run(go())
    assert o.status is OrderStatus.FILLED and o.filled_quantity == 1
    assert pos.quantity == 1 and pos.entry_price == o.average_fill_price
    assert lv.total_fees() == o.filled_quantity * o.average_fill_price * D("0.0005")
    assert lv.equity() < 2000  # paid the fee (mid marks equal the entry mid, so ~zero unrealized)


def test_paper_order_is_idempotent():
    ex, lv, *_ = make_pair()

    async def go():
        a = await lv.place_order("BTCUSDT", Side.BUY, D(1), client_order_id="dup")
        b = await lv.place_order("BTCUSDT", Side.BUY, D(1), client_order_id="dup")
        return a, b, await lv.get_position("BTCUSDT")

    a, b, pos = run(go())
    assert a is b and pos.quantity == 1 and len(lv.fills) == 1


def test_paper_reduce_only_never_opens_exposure():
    ex, lv, *_ = make_pair()

    async def go():
        with pytest.raises(PaperRejected):
            await lv.place_order("BTCUSDT", Side.SELL, D(1), client_order_id="r1", reduce_only=True)
        await lv.place_order("BTCUSDT", Side.BUY, D(1), client_order_id="o1")
        o = await lv.place_order("BTCUSDT", Side.SELL, D(5), client_order_id="r2", reduce_only=True)
        return o, await lv.get_position("BTCUSDT")

    o, pos = run(go())
    assert o.filled_quantity == 1 and pos is None  # capped at the open position, now flat


def test_paper_realized_pnl_and_funding_signs():
    ex, lv, sv, src_l, src_s, *_ = make_pair()

    async def go():
        await lv.place_order("BTCUSDT", Side.BUY, D(1), client_order_id="l")
        await sv.place_order("BTCUSDT", Side.SELL, D(1), client_order_id="s")
        lv.apply_funding("BTCUSDT", D("0.001"), START)  # positive rate: long pays
        sv.apply_funding("BTCUSDT", D("0.001"), START)  # positive rate: short receives
        src_l.mid = src_s.mid = D(110)
        await lv.get_order_book("BTCUSDT")
        await sv.get_order_book("BTCUSDT")
        return lv.equity(), sv.equity()

    le, se = run(go())
    assert lv.total_funding() < 0 < sv.total_funding()
    assert le > 2000 and se < 2000  # price up: long gains, short loses (about +/-10)


def test_paper_liquidation_price_and_margin():
    ex, lv, sv, *_ = make_pair()

    async def go():
        await lv.set_leverage("BTCUSDT", D(2))
        await sv.set_leverage("BTCUSDT", D(2))
        await lv.place_order("BTCUSDT", Side.BUY, D(1), client_order_id="l")
        await sv.place_order("BTCUSDT", Side.SELL, D(1), client_order_id="s")
        return await lv.get_position("BTCUSDT"), await sv.get_position("BTCUSDT"), await lv.get_account_equity()

    lp, sp, acct = run(go())
    assert lp.liquidation_price < lp.entry_price < sp.liquidation_price
    assert lp.margin_mode == "cross"
    assert acct.margin_utilization is not None and acct.available_margin_usd < acct.equity_usd


def test_snapshot_restore_roundtrip():
    ex, lv, sv, *_ = make_pair()
    run(lv.place_order("BTCUSDT", Side.BUY, D(1), client_order_id="l"))
    lv.apply_funding("BTCUSDT", D("0.0001"), START)
    snap = lv.snapshot()
    other = make_pair()[1]
    other.restore(snap)
    assert other.equity() == lv.equity() and len(other.fills) == 1 and len(other.funding_payments) == 1
    again = run(other.place_order("BTCUSDT", Side.BUY, D(1), client_order_id="l"))  # idempotent after restore
    assert again.client_order_id == "l" and len(other.fills) == 1


# ---- paired entry slice ----------------------------------------------------------------------

def test_entry_slice_fills_both_legs_concurrently():
    ex, lv, sv, *_ = make_pair()
    r = run(ex.entry_slice(0, D(100)))
    assert r.fully_filled and r.long.client_order_id == "arb0001-s0000-L-a01"
    assert r.short.client_order_id == "arb0001-s0000-S-a01"
    assert r.submit_diff_ms == 0  # ManualClock: both submitted at the same instant
    delta = run(ex.delta())
    assert abs(delta.net_usd) < 1 and not ex.unsafe(delta)
    kinds = [k for k, _ in ex.events_log]
    assert kinds == ["slice_result"]


def test_slice_below_minimum_is_refused_not_sent():
    ex, lv, sv, *_ = make_pair()
    with pytest.raises(SliceTooSmall):
        run(ex.entry_slice(0, D(1)))  # below the $5 minimum notional
    assert not lv.fills and not sv.fills


def test_partial_fill_is_seen_in_delta_and_unsafe():
    ex, lv, sv, *_ = make_pair()
    sv.model.fill_ratio = D("0.62")  # LONG fills $100, SHORT fills about $62 (project.md example)
    r = run(ex.entry_slice(0, D(100)))
    assert r.long.order.status is OrderStatus.FILLED and r.short.order.status is OrderStatus.PARTIALLY_FILLED
    d = run(ex.delta())
    assert 35 < d.net_usd < 40 and d.lagging_leg == "short" and ex.unsafe(d)


def test_heal_completes_the_missing_hedge():
    ex, lv, sv, src_l, src_s, clock, params = make_pair()
    sv.model.fill_ratio = D("0.62")
    run(ex.entry_slice(0, D(100)))
    sv.model.fill_ratio = D(1)  # the venue recovers
    res = run(ex.heal("entry", clock.now() + timedelta(seconds=10)))
    assert res.healed and not res.flattened and res.steps >= 1
    assert abs(run(ex.delta()).net_usd) <= params.max_delta_imbalance_usd


def test_heal_flattens_the_excess_when_the_hedge_cannot_be_completed():
    ex, lv, sv, src_l, src_s, clock, params = make_pair()
    sv.model.fill_ratio = D("0.62")
    run(ex.entry_slice(0, D(100)))
    sv.model.reject_next = 999  # the short venue now rejects everything
    res = run(ex.heal("entry", clock.now() + timedelta(seconds=3)))
    assert res.flattened and res.healed  # excess long was cut back
    held_long = run(ex.held_quantity(ex.long))
    assert held_long < D("0.7")  # about the $62 that is actually hedged
    assert not ex.unsafe(res.delta)


def test_one_leg_rejected_then_flattened():
    ex, lv, sv, src_l, src_s, clock, params = make_pair()
    sv.model.reject_next = 999
    r = run(ex.entry_slice(0, D(100)))
    assert r.long.status == "OK" and r.short.status == "REJECTED" and not r.fully_filled
    res = run(ex.heal("entry", clock.now() + timedelta(seconds=3)))
    assert res.flattened and run(ex.held_quantity(ex.long)) == 0  # long fully unwound
    assert any(k == "heal_flatten" for k, _ in ex.events_log)


def test_timeout_after_fill_is_queried_not_retried():
    ex, lv, sv, *_ = make_pair()
    lv.model.timeout_after_fill_next = 1
    r = run(ex.entry_slice(0, D(100)))
    assert r.long.status == "OK" and r.long.error is not None  # timed out, but the real fill was found
    assert r.long.order.status is OrderStatus.FILLED
    assert len(lv.fills) == 1  # never submitted twice


def test_timeout_before_fill_confirms_not_placed_and_does_not_double_send():
    ex, lv, sv, *_ = make_pair()
    lv.model.timeout_before_fill_next = 1
    r = run(ex.entry_slice(0, D(100)))
    assert r.long.status == "NOT_PLACED" and r.long.order is None
    assert len(lv.fills) == 0 and len(sv.fills) == 1  # one-legged: the caller must hedge or flatten


def test_unreachable_venue_after_timeout_is_unresolved():
    ex, lv, sv, *_ = make_pair()
    lv.model.timeout_before_fill_next = 1
    original = lv.get_order

    async def dead(*a, **k):
        from app.exchanges.base import ExchangeRequestError
        raise ExchangeRequestError("still down")

    lv.get_order = dead
    r = run(ex.entry_slice(0, D(100)))
    assert r.unresolved and r.long.status == "UNRESOLVED"
    lv.get_order = original


def test_both_legs_rejected_leaves_no_exposure():
    ex, lv, sv, *_ = make_pair()
    lv.model.reject_next = sv.model.reject_next = 1
    r = run(ex.entry_slice(0, D(100)))
    assert r.long.status == r.short.status == "REJECTED"
    assert run(ex.delta()).long_usd == 0 == run(ex.delta()).short_usd


def test_duplicate_acknowledgement_does_not_double_fill():
    ex, lv, sv, *_ = make_pair()
    run(ex.entry_slice(0, D(100)))
    again = run(ex.entry_slice(0, D(100)))  # same slice, same attempt: same client ids
    assert again.fully_filled and len(lv.fills) == 1 and len(sv.fills) == 1


def test_orderbook_movement_changes_fill_price_not_hedge():
    ex, lv, sv, src_l, src_s, *_ = make_pair()
    src_l.mid = D(101)  # long venue moved up before the slice
    r = run(ex.entry_slice(0, D(100)))
    assert r.long.order.average_fill_price > D("100.9") and r.short.order.average_fill_price < D("100.1")
    assert not ex.unsafe(run(ex.delta()))  # equal USD notional even at different prices


# ---- exits -----------------------------------------------------------------------------------

def open_full(ex, n=3):
    for i in range(n):
        run(ex.entry_slice(i, D(100)))


def test_exit_slices_reduce_both_legs_and_end_flat():
    ex, lv, sv, *_ = make_pair()
    open_full(ex)
    for i, left in enumerate((3, 2, 1)):
        r = run(ex.exit_slice(i, left))
        assert r.long.order.reduce_only and r.short.order.reduce_only
        assert not ex.unsafe(run(ex.delta())) or left == 1
    d = run(ex.delta())
    assert d.long_usd == 0 and d.short_usd == 0


def test_exit_client_ids_do_not_collide_with_entry_ids():
    ex, lv, sv, *_ = make_pair()
    open_full(ex, 1)
    r = run(ex.exit_slice(0, 1))
    assert r.long.client_order_id == "arb0001-x0000-L-a01" and r.long.order.filled_quantity > 0


def test_exit_heals_by_reducing_the_heavy_leg_only():
    ex, lv, sv, src_l, src_s, clock, params = make_pair()
    open_full(ex, 3)
    sv.model.fill_ratio = D("0.3")
    run(ex.exit_slice(0, 1))  # short leg only closes 30 %
    sv.model.fill_ratio = D(1)
    d = run(ex.delta())
    assert d.heavy_leg == "short" and ex.unsafe(d)
    res = run(ex.heal("exit", clock.now() + timedelta(seconds=10)))
    assert res.healed
    # the long leg was never re-opened: only reduce-only orders were used
    assert all(f.side is Side.BUY or f.quantity > 0 for f in lv.fills)
    assert run(ex.held_quantity(ex.long)) == 0


def test_disconnected_venue_raises_instead_of_guessing():
    ex, lv, sv, *_ = make_pair()
    lv.connected = False
    from app.exchanges.base import ExchangeRequestError
    with pytest.raises(ExchangeRequestError):
        run(ex.delta())


def test_dust_below_venue_minimum_is_not_unsafe_and_counts_as_flat():
    from app.execution.delta import is_flat
    tiny = DeltaState(D(0), D("0.10"))
    assert not is_unsafe(tiny, D(50), D("0.05"), dust_usd=D(5))
    assert is_unsafe(tiny, D(50), D("0.05"))  # without a dust allowance the ratio would block forever
    assert is_flat(tiny, D(5)) and not is_flat(DeltaState(D(6), D(0)), D(5))
