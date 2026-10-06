"""Close an open PAPER trade that the bot can no longer manage, at prices both venues showed at the same moment.

    python -m app.tools.settle_paper_trade arb0004 --reason venue_excluded            (dry run: prints the plan)
    python -m app.tools.settle_paper_trade arb0004 --reason venue_excluded --apply

For a paper trade on a venue that is gone for good (it refuses this machine's region and is now in EXCLUDE_VENUES):
the engine would keep it in DEGRADED/RECONCILING for ever, because it can never read that venue again.

The bot must be stopped: the tool takes the same one-bot lock. Each leg is closed in full at its mark from the latest
equity snapshot in which BOTH legs were valued (the last moment the bot saw both venues), or at --long-price and
--short-price. Fees are the leg's paper taker rate, no spread is charged, and funding after that moment is not booked;
the trade's context records all of this. A live trade is refused: real positions are closed on the venues.
"""

import argparse
import asyncio
import json
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal

from app.accounting.pnl import FundingRecord, OrderRecord, compute_pnl
from app.core.clock import Clock
from app.core.instance_lock import AlreadyRunning, InstanceLock
from app.core.trading_config import TradingSettings
from app.domain.market import BookLevel, OrderBook, Side
from app.domain.trade import EXPOSED, TradeState, allowed_next
from app.exchanges.registry import binding_for
from app.execution.venue_provider import PaperVenueProvider
from app.notifications.notifier import Notifier
from app.persistence import models as m
from app.persistence.models import as_utc
from app.persistence.repository import Repository

D = Decimal
S = TradeState
_DEEP = D(10) ** 12  # book depth: the whole position fills at the one price


class SettleError(RuntimeError):
    pass


class FixedPriceSource:
    """Market data for a paper leg that is being settled: one price, with depth for any quantity."""

    def __init__(self, name: str, price: Decimal, clock: Clock):
        self.name, self.price, self._clock = name, price, clock

    async def list_instruments(self):
        return []  # no step rounding: the position is closed exactly

    async def get_order_book(self, symbol: str, depth: int = 50) -> OrderBook:
        level = (BookLevel(price=self.price, quantity=_DEEP),)
        now = self._clock.now()
        return OrderBook(venue=self.name, symbol=symbol, bids=level, asks=level, source_time=now, received_at=now)


def _close_path(state: TradeState) -> list[TradeState]:
    """The shortest allowed path from `state` to CLOSED through the exit states."""
    via = {S.RECONCILING, S.EXIT_PENDING, S.EXITING}
    queue, seen = deque([(state, [])]), {state}
    while queue:
        cur, path = queue.popleft()
        for nxt in allowed_next(cur):
            if nxt is S.CLOSED and cur is S.EXITING:
                return [*path, nxt]
            if nxt in via and nxt not in seen:
                seen.add(nxt)
                queue.append((nxt, [*path, nxt]))
    raise SettleError(f"no path from {state.value} to CLOSED")


def last_joint_prices(repo: Repository, trade_id: str, accounts: dict[str, dict], symbols: dict[str, str]
                      ) -> tuple[datetime, dict[str, Decimal]]:
    """Each leg's mark at the last equity snapshot that valued BOTH legs.

    equity = balance + (mark - entry) * quantity, so mark = entry + (equity - balance) / quantity. That holds only if
    nothing moved the balance since the snapshot, so a fill or funding payment after it is refused."""
    with repo.session() as s:
        snap = (s.query(m.EquitySnapshot).filter(m.EquitySnapshot.trade_id == trade_id,
                                                  m.EquitySnapshot.long_equity.isnot(None),
                                                  m.EquitySnapshot.short_equity.isnot(None))
                .order_by(m.EquitySnapshot.id.desc()).first())
        if snap is None:
            raise SettleError("no equity snapshot values both legs: give --long-price and --short-price")
        at = as_utc(snap.recorded_at)
        equity = {"long": D(str(snap.long_equity)), "short": D(str(snap.short_equity))}
    marks = {}
    for role, acc in accounts.items():
        later = [e["t"] for e in (*acc["fills"], *acc["funding"]) if datetime.fromisoformat(e["t"]) > at]
        if later:
            raise SettleError(f"{role} leg changed after the last joint snapshot ({at.isoformat()}): "
                              "give --long-price and --short-price")
        pos = acc["positions"].get(symbols[role])
        if pos is None or D(pos["q"]) == 0:
            raise SettleError(f"{role} leg holds no position: nothing to value")
        marks[role] = D(pos["e"]) + (equity[role] - D(acc["balance"])) / D(pos["q"])
        if marks[role] <= 0:
            raise SettleError(f"{role} leg: derived mark {marks[role]} is not a price")
    return at, marks


async def adapter_name(zd_venue: str) -> str:
    """The public adapter's name for a Zero Delta venue (the paper fee table and funding records use it)."""
    binding = binding_for(zd_venue)
    if binding is None:
        raise SettleError(f"{zd_venue}: no adapter, so its paper fee rate is unknown")
    adapter = binding.factory()  # only for its name; creating it sends no request
    await adapter.close()
    return adapter.name


async def settle(repo: Repository, settings: TradingSettings, trade_id: str, reason: str, *,
                 prices: dict[str, Decimal] | None = None, apply: bool = False, clock: Clock | None = None,
                 name_for: Callable[[str], Awaitable[str]] = adapter_name) -> dict:
    clock = clock or Clock()
    params = settings.resolve()
    row = repo.get_trade(trade_id)
    state = TradeState(row.state)
    if row.run_mode != "paper":
        raise SettleError(f"{trade_id} is a {row.run_mode} trade: real positions must be closed on the venues")
    if state not in EXPOSED:
        raise SettleError(f"{trade_id} is {state.value}: there is nothing open to settle")
    path = _close_path(state)
    roles = {"long": (row.long_venue, row.long_symbol), "short": (row.short_venue, row.short_symbol)}
    names = {role: await name_for(venue) for role, (venue, _) in roles.items()}
    keys = {role: f"{trade_id}:{role}" for role in roles}
    accounts = {role: repo.load_paper_account(key) for role, key in keys.items()}
    if any(a is None for a in accounts.values()):
        raise SettleError(f"{trade_id}: a paper account is missing, so its position is unknown")
    if prices:
        at, method = clock.now(), "prices given by the operator"
    else:
        at, prices = last_joint_prices(repo, trade_id, accounts, {r: sym for r, (_, sym) in roles.items()})
        method = "last equity snapshot that valued both legs"

    provider = PaperVenueProvider(lambda venue: None, clock, repo)
    venues, closing = {}, []
    for role, (venue_name, symbol) in roles.items():
        venue = provider._paper(FixedPriceSource(names[role], prices[role], clock), trade_id, role, params)
        venue.restore(accounts[role])
        venues[role] = venue
        pos = await venue.get_position(symbol)
        if pos is None:
            continue
        side = Side.SELL if pos.quantity > 0 else Side.BUY
        order = await venue.place_order(symbol, side, abs(pos.quantity), reduce_only=True,
                                        client_order_id=f"{trade_id}-settle-{role[0].upper()}")
        closing.append((role, order, venue.fills[-1]))

    with repo.session() as s:
        refs = {f.client_order_id: f.reference_price for f in s.query(m.Fill).filter(m.Fill.trade_id == trade_id)}
    ledger = [OrderRecord(o.leg, o.side, o.filled_quantity, o.average_fill_price, o.fee_usd, refs.get(o.client_order_id))
              for o in repo.orders_for(trade_id) if o.filled_quantity > 0 and o.average_fill_price]
    ledger += [OrderRecord(role, o.side.value, o.filled_quantity, o.average_fill_price, o.fee_paid_usd, f.reference_price)
               for role, o, f in closing]
    by_name = {names[r]: r for r in roles}
    funding = [FundingRecord(by_name.get(f.venue, "long"), f.expected_payment, f.actual_payment)
               for f in repo.funding_payments(trade_id)]
    pnl = compute_pnl(ledger, funding)
    left = [r for r, v in venues.items() if await v.get_position(roles[r][1]) is not None]
    if left or not pnl.complete:
        raise SettleError(f"the legs did not close flat: {left or pnl.unpriced_legs}")
    plan = {"trade": trade_id, "asset": row.asset, "state": state.value, "path": [p.value for p in path],
            "priced_at": at.isoformat(), "method": method, "prices": {r: str(p) for r, p in prices.items()},
            "closing_orders": [{"leg": r, "venue": o.venue, "side": o.side.value, "quantity": str(o.filled_quantity),
                                "price": str(o.average_fill_price), "fee": str(o.fee_paid_usd)} for r, o, _ in closing],
            "pnl": pnl.as_dict(), "applied": False}
    if not apply:
        return plan

    for role, order, fill in closing:
        repo.upsert_order(trade_id, role, order, True)
        repo.add_fill(trade_id, order.client_order_id, fill.quantity, fill.price, fill.fee, fill.reference_price,
                      fill.time)
    for role, venue in venues.items():
        repo.save_paper_account(keys[role], venue.snapshot())
    long_eq, short_eq = venues["long"].equity(), venues["short"].equity()
    repo.add_equity_snapshot(trade_id, long_equity=long_eq, short_equity=short_eq,
                             long_ratio=long_eq / row.baseline_long_equity,
                             short_ratio=short_eq / row.baseline_short_equity, net_delta_usd=D(0))
    note = (f"settled by app.tools.settle_paper_trade: {reason}; each leg closed at its price of {at.isoformat()} "
            f"({method}); taker fee, no spread, no funding after that moment")
    repo.merge_context(trade_id, exit={"mode": "settled", "reason": reason, "priced_at": at.isoformat()},
                       settlement={"method": method, "prices": plan["prices"], "note": note})
    for new in path:
        repo.set_state(trade_id, new, note if new is S.CLOSED else reason)
    repo.update_trade(trade_id, exit_reason=reason, pnl=pnl.as_dict())
    if row.owner:
        repo.release_lease(trade_id, row.owner)
    repo.audit("paper_trade_settled", plan, trade_id)
    notifier = Notifier(repo)
    await notifier.alert("trade_closed", f"closed: {reason} (settled at the {at:%Y-%m-%d %H:%M} UTC prices)",
                         trade_id=trade_id)
    await notifier.alert("final_pnl", f"total {pnl.total:+.4f} USD (funding {pnl.funding_net:+.4f}, "
                         f"hedge {pnl.hedge_trading:+.4f}, fees {pnl.fees:.4f}, slippage est. "
                         f"{pnl.slippage_estimate:.4f})", trade_id=trade_id)
    plan["applied"] = True
    return plan


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("trade_id")
    parser.add_argument("--reason", required=True, help="why it is settled, e.g. venue_excluded")
    parser.add_argument("--long-price", type=Decimal)
    parser.add_argument("--short-price", type=Decimal)
    parser.add_argument("--apply", action="store_true", help="write it; without this only the plan is printed")
    args = parser.parse_args(argv)
    if (args.long_price is None) != (args.short_price is None):
        parser.error("give both --long-price and --short-price, or neither")
    prices = {"long": args.long_price, "short": args.short_price} if args.long_price is not None else None
    lock = InstanceLock()
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        print(f"stop the bot first: {exc}")
        return 1
    try:
        settings = TradingSettings()
        repo = Repository(settings.database_url, Clock())
        plan = asyncio.run(settle(repo, settings, args.trade_id, args.reason, prices=prices, apply=args.apply))
    except (SettleError, KeyError) as exc:
        print(f"not settled: {exc}")
        return 1
    finally:
        lock.release()
    print(json.dumps(plan, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
