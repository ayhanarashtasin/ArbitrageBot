"""Builds the two execution legs of a trade.

PaperVenueProvider wraps REAL market-data adapters in dedicated simulated accounts (one per trade and leg,
so equity baselines are clean, section 32). A live provider (Phase 8) will return real adapters instead.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from decimal import Decimal

from app.core.clock import Clock
from app.core.trading_config import PAPER_FEES_BPS, DEFAULT_PAPER_FEE_BPS, ResolvedParams
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
