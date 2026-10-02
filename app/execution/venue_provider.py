"""Builds the two execution legs of a trade.

PaperVenueProvider wraps REAL market-data adapters in dedicated simulated accounts (one per trade and leg,
so equity baselines are clean, section 32). LiveVenueProvider builds real legs (LiveVenue) on the venues that have API
credentials and passed their readiness check.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal

from app.core.clock import Clock
from app.core.trading_config import PAPER_FEES_BPS, DEFAULT_PAPER_FEE_BPS, ResolvedParams
from app.domain.market import FeeSchedule
from app.exchanges.base import ExchangeAdapter, ExchangeError
from app.execution.pair_executor import Leg
from app.execution.paper import PaperVenue
from app.persistence.repository import Repository
from app.strategy.instruments import parse_market_identifier
from app.strategy.pairs import PairAnalytics

D = Decimal
BPS = D(10_000)


class UnsupportedVenue(ExchangeError):
    """No executable adapter exists for this Zero Delta venue."""


class VenueProvider(ABC):
    @abstractmethod
    async def build(self, pair: PairAnalytics, trade_id: str, params: ResolvedParams) -> tuple[Leg, Leg]: ...

    @abstractmethod
    async def rebuild(self, trade_row, params: ResolvedParams) -> tuple[Leg, Leg]:
        """Recreate the legs of an unfinished trade after a restart."""


SourceLookup = Callable[[str], ExchangeAdapter | None]


class PaperVenueProvider(VenueProvider):
    def __init__(self, source_for_venue: SourceLookup, clock: Clock, repo: Repository):
        self._source_for = source_for_venue
        self._clock = clock
        self._repo = repo

    def _source(self, zd_venue: str) -> ExchangeAdapter:
        src = self._source_for(zd_venue)
        if src is None:
            raise UnsupportedVenue(f"no executable adapter for venue {zd_venue!r}")
        return src

    def _paper(self, source: ExchangeAdapter, trade_id: str, role: str, params: ResolvedParams) -> PaperVenue:
        taker_bps, maker_bps = PAPER_FEES_BPS.get(source.name, DEFAULT_PAPER_FEE_BPS)
        margin = params.target_notional_per_leg_usd / params.target_leverage
        venue = PaperVenue(source, clock=self._clock, initial_equity_usd=margin * params.paper_account_equity_multiple,
                           taker_fee=taker_bps / BPS, maker_fee=maker_bps / BPS, fee_verified=False)
        venue.paper_key = f"{trade_id}:{role}"  # type: ignore[attr-defined]
        return venue

    async def build(self, pair: PairAnalytics, trade_id: str, params: ResolvedParams) -> tuple[Leg, Leg]:
        legs = []
        for role, leg in (("long", pair.long_leg), ("short", pair.short_leg)):
            source = self._source(leg.venue_name)
            inst = parse_market_identifier(leg.market_identifier)
            if inst is None:
                raise UnsupportedVenue(f"unrecognised market identifier {leg.market_identifier!r}")
            meta = await source.resolve_symbol(inst.base, inst.quote, inst.settle)  # from the venue's own list
            legs.append(Leg(role, self._paper(source, trade_id, role, params), meta))
        return legs[0], legs[1]

    async def rebuild(self, trade_row, params: ResolvedParams) -> tuple[Leg, Leg]:
        legs = []
        for role, venue_name, symbol in (("long", trade_row.long_venue, trade_row.long_symbol),
                                         ("short", trade_row.short_venue, trade_row.short_symbol)):
            source = self._source(venue_name)
            meta = await source.get_instrument_metadata(symbol)
            venue = self._paper(source, trade_row.id, role, params)
            state = self._repo.load_paper_account(venue.paper_key)  # type: ignore[attr-defined]
            if state is not None:
                venue.restore(state)
            legs.append(Leg(role, venue, meta))
        return legs[0], legs[1]


class LiveVenueProvider(VenueProvider):
    """Real venues: public market data plus the signed client of each venue that has credentials (phase 8).

    `ready` holds the venues whose readiness check passed at startup. Only those can open NEW legs; an existing trade
    is always rebuilt (it must be managed and closed even if its venue failed a check since).
    """

    def __init__(self, source_for_venue: SourceLookup, clients: dict, clock: Clock, repo: Repository):
        self._source_for = source_for_venue
        self.clients = clients  # public adapter name -> PrivateClient
        self.ready: set[str] = set()
        self._clock = clock
        self._repo = repo
        self._fees: dict[tuple[str, str], tuple[datetime, FeeSchedule | None]] = {}

    async def fee_schedule(self, venue: str, meta) -> "FeeSchedule | None":
        """The account's real fee tier for a symbol (cached one hour). None when the venue has no client or the read
        fails: the caller then keeps the labelled standard-tier assumption."""
        client = self.clients.get(venue)
        if client is None:
            return None
        key, now = (venue, meta.symbol), self._clock.now()
        cached = self._fees.get(key)
        if cached is not None and now - cached[0] < timedelta(hours=1):
            return cached[1]
        try:
            schedule = await client.fee_schedule(meta)
        except Exception:  # noqa: BLE001 - a fee lookup must never stop a sweep
            schedule = None
        self._fees[key] = (now, schedule)
        return schedule

    def client_for(self, zd_venue: str):
        source = self._source_for(zd_venue)
        return None if source is None else self.clients.get(source.name)

    def tradable_venue(self, zd_venue: str) -> bool:
        source = self._source_for(zd_venue)
        return source is not None and source.name in self.ready

    def _leg(self, source, client, meta, trade_id: str, role: str, params: ResolvedParams, opened_at):
        from app.execution.live import LiveVenue
        budget = params.target_notional_per_leg_usd / params.target_leverage
        return LiveVenue(source, client, meta, clock=self._clock, budget_usd=budget,
                         account_key=f"live:{trade_id}:{role}", opened_at=opened_at)

    async def build(self, pair: PairAnalytics, trade_id: str, params: ResolvedParams) -> tuple[Leg, Leg]:
        from app.execution.live import preflight
        legs = []
        now = self._clock.now()
        for role, leg in (("long", pair.long_leg), ("short", pair.short_leg)):
            source = self._source_for(leg.venue_name)
            if source is None or source.name not in self.ready:
                raise UnsupportedVenue(f"{leg.venue_name} is not enabled for live trading (no credentials or its "
                                       "readiness check failed)")
            client = self.clients[source.name]
            inst = parse_market_identifier(leg.market_identifier)
            if inst is None:
                raise UnsupportedVenue(f"unrecognised market identifier {leg.market_identifier!r}")
            meta = await source.resolve_symbol(inst.base, inst.quote, inst.settle)
            # A leg's ledger is "every execution on this symbol" (see LiveVenue.resync), so a venue symbol can belong
            # to one open trade only. The venue position alone cannot show this: the other trade may not have filled.
            for t in self._repo.unfinished_trades():
                if (leg.venue_name, meta.symbol) in ((t.long_venue, t.long_symbol), (t.short_venue, t.short_symbol)):
                    raise UnsupportedVenue(f"{leg.venue_name} {meta.symbol} is already used by open trade {t.id}")
            await preflight(client, meta, params.target_notional_per_leg_usd / params.target_leverage)
            legs.append(Leg(role, self._leg(source, client, meta, trade_id, role, params, now), meta))
        return legs[0], legs[1]

    async def rebuild(self, trade_row, params: ResolvedParams) -> tuple[Leg, Leg]:
        from app.persistence.models import as_utc
        legs = []
        for role, venue_name, symbol in (("long", trade_row.long_venue, trade_row.long_symbol),
                                         ("short", trade_row.short_venue, trade_row.short_symbol)):
            source = self._source(venue_name)
            client = self.clients.get(source.name)
            if client is None:
                raise UnsupportedVenue(f"{venue_name}: no credentials to manage the open live trade {trade_row.id}")
            meta = await source.get_instrument_metadata(symbol)
            venue = self._leg(source, client, meta, trade_row.id, role, params, as_utc(trade_row.opened_at))
            state = self._repo.load_paper_account(venue.paper_key)
            if state is not None:
                venue.restore(state)
            venue.restored = True  # even without a saved ledger: look every order up on the venue before sending it
            await venue.resync()  # every execution on the symbol since the trade opened, including unrecorded ones
            legs.append(Leg(role, venue, meta))
        return legs[0], legs[1]

    def _source(self, zd_venue: str) -> ExchangeAdapter:
        src = self._source_for(zd_venue)
        if src is None:
            raise UnsupportedVenue(f"no executable adapter for venue {zd_venue!r}")
        return src
