"""Venue-independent market data models. Every venue adapter returns exactly these types.

Conventions (project.md sections 12, 39, 44, 45):
- All numbers are Decimal. All timestamps are timezone-aware UTC.
- Funding rates are FRACTIONS per funding interval (0.0001 = 0.01 %), NOT percent.
  Zero Delta models use percent; convert at the boundary (divide by 100).
- Positive funding = LONG pays SHORT.
- Quantities are always in BASE asset units. Adapters convert contract counts using the
  contract multiplier, so notional = price * quantity on every venue.
- Every datum carries the venue's own timestamp when it has one, and the local receive time.
"""

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class BookLevel(_Model):
    price: Decimal
    quantity: Decimal  # base asset units


class OrderBook(_Model):
    venue: str
    symbol: str
    bids: tuple[BookLevel, ...]  # best (highest) first
    asks: tuple[BookLevel, ...]  # best (lowest) first
    source_time: datetime | None
    received_at: datetime

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    def age_ms(self, now: datetime | None = None) -> float:
        """Age of the data. Uses the venue timestamp when present, otherwise the receive time."""
        now = now or utcnow()
        reference = self.source_time or self.received_at
        return (now - reference).total_seconds() * 1000


class FundingInfo(_Model):
    venue: str
    symbol: str
    funding_rate: Decimal  # fraction per interval; the rate that applies at next_funding_time
    interval_hours: Decimal
    next_funding_time: datetime | None
    is_predicted: bool  # True when the venue only offers an estimate for the current interval
    mark_price: Decimal | None
    index_price: Decimal | None
    source_time: datetime | None
    received_at: datetime

    @property
    def rate_per_hour(self) -> Decimal:
        return self.funding_rate / self.interval_hours


class Ticker(_Model):
    venue: str
    symbol: str
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    mark: Decimal | None
    volume_24h_usd: Decimal | None
    source_time: datetime | None
    received_at: datetime


class Candle(_Model):
    venue: str
    symbol: str
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class InstrumentMeta(_Model):
    """Dynamic instrument metadata (section 17). Never hardcode; always fetch."""

    venue: str
    symbol: str  # the venue's own symbol / coin name, used for every API call
    base: str
    quote: str
    settle: str
    is_linear: bool
    is_perpetual: bool
    is_tradable: bool
    contract_multiplier: Decimal  # base asset units per 1 venue contract (1 when quantities are already base units)
    tick_size: Decimal
    qty_step: Decimal  # in base asset units
    min_qty: Decimal  # base asset units
    max_qty: Decimal | None
    min_notional: Decimal | None
    funding_interval_hours: Decimal | None
    max_leverage: Decimal | None
    received_at: datetime
    # What the contract tracks, as the venue labels it (Binance underlyingType: COIN, EQUITY, HK_EQUITY, KR_EQUITY,
    # CN_EQUITY, COMMODITY, INDEX, PREMARKET; the Trade.xyz HIP-3 dex: TRADFI). None = the venue does not say.
    underlying_type: str | None = None


class FeeSchedule(_Model):
    venue: str
    taker_fraction: Decimal  # 0.0005 = 5 bps
    maker_fraction: Decimal
    verified: bool  # False = configured assumption, True = read from the venue's account API


class AccountState(_Model):
    venue: str
    equity_usd: Decimal
    available_margin_usd: Decimal
    margin_utilization: Decimal | None  # margin ratio: maintenance margin / equity, 1 = liquidation
    received_at: datetime


class PositionState(_Model):
    venue: str
    symbol: str
    quantity: Decimal  # signed, base asset units: > 0 long, < 0 short
    entry_price: Decimal | None
    mark_price: Decimal | None
    liquidation_price: Decimal | None
    leverage: Decimal | None
    margin_mode: str | None
    received_at: datetime


class OrderStatus(StrEnum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"  # outcome ambiguous: query the venue, never assume


class OrderState(_Model):
    venue: str
    symbol: str
    order_id: str | None
    client_order_id: str
    side: Side
    status: OrderStatus
    requested_quantity: Decimal
    filled_quantity: Decimal
    average_fill_price: Decimal | None
    fee_paid_usd: Decimal
    reduce_only: bool
    received_at: datetime
