"""Play a whole paper trade on a scripted market, in seconds, and print what the bot does.

    python -m app.tools.paper_demo                 # a normal trade: entry, funding, edge decays, exit
    python -m app.tools.paper_demo --scenario stoploss  # a 20 % rally: the short account hits 30 %, 3 h controlled exit
    python -m app.tools.paper_demo --scenario crash   # a 50 % rally wipes out the short account: 1-minute emergency exit
    python -m app.tools.paper_demo --scenario partial # one venue fills only 60 %: the hedge is repaired

Nothing is real here: prices, books and funding come from a script, time is simulated, and the database is in
memory. It exists so you can watch the engine make its decisions without waiting hours. Live data is used by
the real bot (uvicorn app.main:app) and by `python -m app.tools.zerodelta_opportunities`.
"""

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.clock import ManualClock
from app.core.trading_config import TradingSettings
from app.domain.market import BookLevel, Candle, FundingInfo, InstrumentMeta, OrderBook, Ticker
from app.exchanges.base import ExchangeAdapter
from app.execution.engine import TradeEngine
from app.execution.venue_provider import PaperVenueProvider
from app.notifications.notifier import Notifier
from app.persistence.repository import Repository
from app.risk.killswitch import KillSwitch

D = Decimal
START = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)


class ScriptedSource(ExchangeAdapter):
    def __init__(self, name: str, clock: ManualClock):
        self.name, self.clock = name, clock
        self.mid, self.funding_per_hour = D(100), D(0)
        self._meta = InstrumentMeta(
            venue=name, symbol="BTCUSDT", base="BTC", quote="USDT", settle="USDT", is_linear=True,
            is_perpetual=True, is_tradable=True, contract_multiplier=D(1), tick_size=D("0.01"), qty_step=D("0.001"),
            min_qty=D("0.001"), max_qty=None, min_notional=D(5), funding_interval_hours=D(8), max_leverage=D(20),
            received_at=START)

    async def get_server_time(self):
        return self.clock.now()

    async def list_instruments(self) -> list[InstrumentMeta]:
        return [self._meta]

    async def get_funding_info(self, symbol: str) -> FundingInfo:
        now = self.clock.now()
        nxt = now.replace(hour=(now.hour // 8) * 8, minute=0, second=0, microsecond=0) + timedelta(hours=8)
        return FundingInfo(venue=self.name, symbol=symbol, funding_rate=self.funding_per_hour * 8,
                           interval_hours=D(8), next_funding_time=nxt, is_predicted=False, mark_price=self.mid,
                           index_price=self.mid, source_time=now, received_at=now)

    async def get_funding_history(self, symbol: str, limit: int = 100):
        return []

    async def get_ticker(self, symbol: str) -> Ticker:
        raise NotImplementedError

    async def get_order_book(self, symbol: str, depth: int = 50) -> OrderBook:
        now = self.clock.now()
        bids = tuple(BookLevel(price=self.mid - D("0.01") * (i + 1), quantity=D(50)) for i in range(20))
        asks = tuple(BookLevel(price=self.mid + D("0.01") * (i + 1), quantity=D(50)) for i in range(20))
        return OrderBook(venue=self.name, symbol=symbol, bids=bids, asks=asks, source_time=now, received_at=now)

    async def get_candles(self, symbol: str, timeframe: str = "1m", limit: int = 200) -> list[Candle]:
        """A calm market: flat closed 1-minute candles, so the low-vol score is 0 and entry slices may go."""
        end = self.clock.now().replace(second=0, microsecond=0)
        return [Candle(venue=self.name, symbol=symbol, open_time=end - timedelta(minutes=limit - i), open=self.mid,
                       high=self.mid, low=self.mid, close=self.mid, volume=D(1)) for i in range(limit)]


def demo_pair():
    """A candidate that Zero Delta and the venues agree on: long Venue A, short Venue B."""
    from app.providers.zerodelta.models import HistoryCoverage
    from app.providers.zerodelta.venues import VenueType
    from app.strategy.analytics import EdgeStats
    from app.strategy.pairs import PairAnalytics, PairLeg

    def leg(name, pct):
        return PairLeg(venue_name=name, venue_type=VenueType.CEX, venue_type_verified=True,
                       market_identifier="BTC/USDT:USDT", settle_asset="USDT", avg_24h_volume_usd=D(50_000_000),
                       funding_interval_hours=D(8), current_funding_per_hour=D(str(pct)))

    stats = EdgeStats(mean=D("0.03"), median=D("0.03"), minimum=D(0), maximum=D(1), stddev=D(0),
                      positive_edge_ratio=D(1), direction_consistency_ratio=D(1), sign_flip_count=0,
                      longest_positive_edge_streak=169, latest=D("0.03"))
    cov = HistoryCoverage(requested_start=None, requested_end=None, first_timestamp=None, last_timestamp=None,
                          coverage_seconds=168 * 3600, observation_count=169, expected_observation_count_if_known=169,
                          missing_intervals=0, duplicate_count=0, out_of_order_count=0, status="OK")
    return PairAnalytics(asset="BTC", long_leg=leg("Venue A", 0.0), short_leg=leg("Venue B", 0.03),
                         pair_type="CEX<->CEX", status="OK", settlement_mismatch=False, coverage=cov,
                         current_funding_edge=D("0.0003"), stats=stats)


async def main(scenario: str) -> None:
    clock = ManualClock(START)
    repo = Repository("sqlite://", clock)
    src = {"Venue A": ScriptedSource("venue_a", clock), "Venue B": ScriptedSource("venue_b", clock)}
    src["Venue A"].funding_per_hour, src["Venue B"].funding_per_hour = D(0), D("0.0003")
    params = TradingSettings(_env_file=None, target_notional_per_leg_usd=500, min_expected_net_edge_bps=1).resolve()
    notifier = Notifier(repo)
    engine = TradeEngine(params=params, repo=repo, clock=clock, killswitch=KillSwitch(clock), notifier=notifier,
                         provider=PaperVenueProvider(src.get, clock, repo))

    print(f"scenario: {scenario} | paper mode | ${params.target_notional_per_leg_usd} per leg, "
          f"{params.target_leverage}x leverage, 6 h TWAP, ${params.twap_max_child_notional_usd} child slices\n")
    pair = demo_pair()
    trade = await engine.open_trade(pair, {"decision": {"outcome": "ENTER"}})
    if scenario == "partial":
        trade.executor.short.venue.model.fill_ratio = D("0.6")

    def stamp() -> str:
        return f"{(clock.now() - START).total_seconds() / 3600:5.2f}h"

    last_state, printed_alerts = None, 0
    for tick in range(4000):
        if scenario == "partial" and tick == 1:
            trade.executor.short.venue.model.fill_ratio = D(1)
        if scenario == "crash" and clock.now() >= START + timedelta(hours=7):
            for s in src.values():
                s.mid = D(150)
        if scenario == "stoploss" and clock.now() >= START + timedelta(hours=7):
            for s in src.values():
                s.mid = D(120)
        if scenario == "normal" and clock.now() >= START + timedelta(hours=14):
            src["Venue B"].funding_per_hour = D(0)  # the edge disappears
        await engine.run_once()
        state = trade.state
        if state is not last_state:
            print(f"[{stamp()}] state -> {state.value}")
            last_state = state
        for a in repo.alerts()[printed_alerts:]:
            if a.kind not in ("funding_payment",):
                print(f"[{stamp()}]   alert {a.kind}: {a.message[:100]}")
        printed_alerts = len(repo.alerts())
        if state.value == "CLOSED":
            break
        emergency = trade.exit_mode == "emergency" and state.value == "EXITING"
        clock.advance(seconds=1 if emergency else (60 if state.value == "ENTERING" else 300))

    t = repo.get_trade(trade.id)
    print(f"\nexit reason: {t.exit_reason}")
    print(f"orders: {len(repo.orders_for(trade.id))} | max delta imbalance seen: ${t.max_delta_imbalance_usd:.2f}")
    print("P&L (separate lines, section 46):")
    for k, v in (t.pnl or {}).items():
        print(f"  {k:42} {Decimal(v):>12.4f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", choices=["normal", "stoploss", "crash", "partial"], default="normal")
    asyncio.run(main(ap.parse_args().scenario))
