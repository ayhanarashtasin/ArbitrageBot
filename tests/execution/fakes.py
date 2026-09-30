"""A scripted market data source and a ready-made paper pair for execution and risk tests."""

from datetime import timedelta
from decimal import Decimal

from app.core.clock import ManualClock
from app.core.trading_config import TradingSettings
from app.domain.market import Candle, FundingInfo, InstrumentMeta, OrderBook, Ticker
from app.exchanges.base import ExchangeAdapter
from app.execution.pair_executor import Leg, PairExecutor
from app.execution.paper import PaperVenue
from tests.unit.helpers import T0, deep_book, funding, meta

D = Decimal


class FakeSource(ExchangeAdapter):
    """Market data you control: set .mid, .book or .funding_rate between steps."""

    def __init__(self, name: str, mid=100, *, size=1000, rate_per_hour="0", clock=None):
        self.name = name
        self.clock = clock
        self.mid = D(str(mid))
        self.size = size
        self.rate_per_hour = D(str(rate_per_hour))
        self.book_override: OrderBook | None = None
        self.candle_closes: list | None = None  # set to give the volatility filter something to read
        self.now = T0
        self._meta = meta(venue=name)

    def _now(self):
        return self.clock.now() if self.clock is not None else self.now

    async def get_server_time(self):
        return self._now()

    async def list_instruments(self) -> list[InstrumentMeta]:
        return [self._meta]

    async def get_funding_info(self, symbol: str) -> FundingInfo:
        now = self._now()
        boundary = now.replace(hour=(now.hour // 8) * 8, minute=0, second=0, microsecond=0) + timedelta(hours=8)
        info = funding(self.rate_per_hour, venue=self.name, now=now)
        return info.model_copy(update={"next_funding_time": boundary})

    async def get_funding_history(self, symbol: str, limit: int = 100):
        return []

    async def get_ticker(self, symbol: str) -> Ticker:
        raise NotImplementedError

    async def get_order_book(self, symbol: str, depth: int = 50) -> OrderBook:
        return self.book_override or deep_book(mid=self.mid, size=self.size, venue=self.name, now=self._now())

    async def get_candles(self, symbol: str, timeframe: str = "1m", limit: int = 200) -> list[Candle]:
        if self.candle_closes is None:
            return []
        from datetime import timedelta as td
        closes = self.candle_closes[-limit:]
        end = self._now().replace(second=0, microsecond=0)
        start = end - td(minutes=len(closes))
        return [Candle(venue=self.name, symbol=symbol, open_time=start + td(minutes=i), open=D(str(c)),
                       high=D(str(c)), low=D(str(c)), close=D(str(c)), volume=D(1)) for i, c in enumerate(closes)]


def make_pair(*, equity=D(2000), mid=100, fee="0.0005", params=None, size=1000):
    clock = ManualClock(T0)
    params = params or TradingSettings(_env_file=None).resolve()
    src_l, src_s = FakeSource("venue_long", mid, size=size), FakeSource("venue_short", mid, size=size)
    long_v = PaperVenue(src_l, clock=clock, initial_equity_usd=equity, taker_fee=D(fee))
    short_v = PaperVenue(src_s, clock=clock, initial_equity_usd=equity, taker_fee=D(fee))
    ex = PairExecutor("arb0001", Leg("long", long_v, src_l._meta), Leg("short", short_v, src_s._meta), clock, params,
                      events=lambda kind, data: ex_events.append((kind, data)))
    ex_events: list = []
    ex.events_log = ex_events
    return ex, long_v, short_v, src_l, src_s, clock, params
