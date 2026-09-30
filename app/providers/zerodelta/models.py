"""Typed internal models. Money-critical numbers are Decimal; unknown values are None."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .venues import VenueType


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class ParseIssue(_Model):
    """A problem found while parsing. Issues are reported, never swallowed."""

    code: str  # MISSING_FIELD, INVALID_VALUE, LENGTH_MISMATCH, NULL_VALUES, SYMBOL_MISMATCH, NO_HISTORY
    message: str
    venue: str | None = None


class ZeroDeltaMarketSnapshot(_Model):
    provider: str = "zerodelta"
    provider_market_id: str | None = None  # Zero Delta exposes no separate market id
    timestamp: datetime | None = None  # provider timestamp; the venue table carries none
    received_at: datetime
    asset: str
    venue_name: str
    venue_type: VenueType
    dex_name: str | None = None
    market_identifier: str  # Zero Delta "Symbol", e.g. BTC/USD:USD
    display_symbol: str
    # Percent per funding interval, exactly as Zero Delta reports it (-0.0014 means -0.0014 %).
    funding_rate_raw: Decimal
    funding_interval_hours: Decimal
    # Percent per hour: funding_rate_raw / funding_interval_hours.
    normalized_funding_rate_per_hour: Decimal
    funding_apr: Decimal | None = None  # percent
    avg_24h_volume_usd: Decimal  # Zero Delta "24hr Volume (M)" x 1,000,000
    open_interest_usd: Decimal | None = None


class ZeroDeltaFundingObservation(_Model):
    timestamp: datetime  # UTC
    asset: str
    venue_name: str
    venue_type: VenueType
    market_identifier: str
    # The history chart carries APR only; raw rate and interval are not available per point.
    funding_rate_raw: Decimal | None = None
    funding_interval_hours: Decimal | None = None
    funding_apr: Decimal  # percent
    normalized_funding_rate_per_hour: Decimal  # percent per hour = APR / 8760
    price: Decimal | None = None
    basis: Decimal | None = None
    volume: Decimal | None = None


class HistoryCoverage(_Model):
    requested_start: datetime | None
    requested_end: datetime | None
    first_timestamp: datetime | None
    last_timestamp: datetime | None
    coverage_seconds: int
    observation_count: int
    expected_observation_count_if_known: int | None
    missing_intervals: int
    duplicate_count: int
    out_of_order_count: int
    status: Literal["OK", "DATA_INSUFFICIENT"]
    reasons: tuple[str, ...] = ()

    @property
    def coverage_hours(self) -> float:
        return self.coverage_seconds / 3600


class MarketEligibility(_Model):
    snapshot: ZeroDeltaMarketSnapshot
    coverage: HistoryCoverage | None
    volume_ok: bool
    history_ok: bool
    venue_type_known: bool
    venue_type_verified: bool = False  # True only when the user supplied the type
    reasons: tuple[str, ...] = ()

    @property
    def eligible(self) -> bool:
        """Locked filters only: volume strictly above $5M and 7 complete days of history."""
        return self.volume_ok and self.history_ok


class AssetAnalysis(_Model):
    asset: str
    received_at: datetime
    markets: tuple[MarketEligibility, ...]
    histories: dict[str, tuple[ZeroDeltaFundingObservation, ...]]  # keyed by venue display name
    issues: tuple[ParseIssue, ...] = ()


class AssetInfo(_Model):
    coin: str
    avg_volume_m: Decimal | None = None  # Zero Delta's coin-table "avg_volume" column, in millions


class HealthStatus(_Model):
    ok: bool
    auth_ok: bool
    latency_ms: float | None = None
    error: str | None = None
