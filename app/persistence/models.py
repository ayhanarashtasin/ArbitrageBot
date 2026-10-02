"""Database schema (project.md section 42).

- Decimals are stored as exact strings (DecimalString), never as floats (section 44).
- All timestamps are UTC (section 45); SQLite drops the zone, so readers must use `as_utc`.
- The same models run on SQLite (paper / development) and PostgreSQL (DATABASE_URL).
"""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class DecimalString(TypeDecorator):
    impl = String(64)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else str(Decimal(value))

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


def as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


class Base(DeclarativeBase):
    pass


def _ts(index: bool = False) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), index=index)


class Venue(Base):
    __tablename__ = "venues"
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    venue_type: Mapped[str] = mapped_column(String(16))
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class Instrument(Base):
    __tablename__ = "instruments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    venue: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(64))
    meta: Mapped[dict] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = _ts()
    __table_args__ = (UniqueConstraint("venue", "symbol", "fetched_at"),)


class ZeroDeltaMarket(Base):
    __tablename__ = "zerodelta_markets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset: Mapped[str] = mapped_column(String(16), index=True)
    venue_name: Mapped[str] = mapped_column(String(64))
    venue_type: Mapped[str] = mapped_column(String(16))
    market_identifier: Mapped[str] = mapped_column(String(64))
    funding_rate_raw: Mapped[Decimal] = mapped_column(DecimalString)
    funding_interval_hours: Mapped[Decimal] = mapped_column(DecimalString)
    normalized_funding_rate_per_hour: Mapped[Decimal] = mapped_column(DecimalString)
    avg_24h_volume_usd: Mapped[Decimal] = mapped_column(DecimalString)
    eligible: Mapped[bool] = mapped_column(Boolean)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    received_at: Mapped[datetime] = _ts(index=True)  # indexed: old rows are pruned by time after every sweep


class ZeroDeltaObservation(Base):
    """Raw hourly points, stored only for venues of trades that were entered (audit trail)."""

    __tablename__ = "zerodelta_observations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[str] = mapped_column(String(32), index=True)
    venue_name: Mapped[str] = mapped_column(String(64))
    timestamp: Mapped[datetime] = _ts()
    funding_apr: Mapped[Decimal] = mapped_column(DecimalString)
    normalized_funding_rate_per_hour: Mapped[Decimal] = mapped_column(DecimalString)


class ZeroDeltaHistory(Base):
    """Coverage report per venue per scan (section 9)."""

    __tablename__ = "zerodelta_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset: Mapped[str] = mapped_column(String(16), index=True)
    venue_name: Mapped[str] = mapped_column(String(64))
    coverage: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24))
    recorded_at: Mapped[datetime] = _ts(index=True)  # indexed: old rows are pruned by time after every sweep


class ZeroDeltaCandidate(Base):
    __tablename__ = "zerodelta_candidates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset: Mapped[str] = mapped_column(String(16), index=True)
    long_venue: Mapped[str] = mapped_column(String(64))
    short_venue: Mapped[str] = mapped_column(String(64))
    pair_type: Mapped[str] = mapped_column(String(32))
    analytics: Mapped[dict] = mapped_column(JSON)
    recorded_at: Mapped[datetime] = _ts()


class ZeroDeltaProviderEvent(Base):
    __tablename__ = "zerodelta_provider_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(48))  # AUTH_FAILURE, OUTAGE, STALE, MALFORMED, OK
    message: Mapped[str] = mapped_column(Text)
    recorded_at: Mapped[datetime] = _ts(index=True)  # indexed: old rows are pruned by time after every sweep


class Opportunity(Base):
    __tablename__ = "opportunities"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset: Mapped[str] = mapped_column(String(16), index=True)
    long_venue: Mapped[str] = mapped_column(String(64))
    short_venue: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    detail: Mapped[dict] = mapped_column(JSON)
    recorded_at: Mapped[datetime] = _ts()


class StrategyDecision(Base):
    __tablename__ = "strategy_decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset: Mapped[str] = mapped_column(String(16), index=True)
    long_venue: Mapped[str] = mapped_column(String(64))
    short_venue: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(24))
    checks: Mapped[list] = mapped_column(JSON)
    metrics: Mapped[dict] = mapped_column(JSON)
    trade_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    recorded_at: Mapped[datetime] = _ts()


class Selection(Base):
    """The pairs picked in each scan: best in the market and best tradable, with their 7-day simulation."""

    __tablename__ = "selections"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_at: Mapped[datetime] = _ts(index=True)  # indexed: picks due for their week-later score are found by time every sweep
    kind: Mapped[str] = mapped_column(String(12), index=True)  # overall | tradable
    rank: Mapped[int] = mapped_column(Integer)
    asset: Mapped[str] = mapped_column(String(16), index=True)
    long_venue: Mapped[str] = mapped_column(String(64))
    short_venue: Mapped[str] = mapped_column(String(64))
    score_apr: Mapped[Decimal] = mapped_column(DecimalString)
    tradable: Mapped[bool] = mapped_column(Boolean)
    why_not: Mapped[str | None] = mapped_column(String(200), nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class Trade(Base):
    __tablename__ = "trades"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    asset: Mapped[str] = mapped_column(String(16), index=True)
    run_mode: Mapped[str] = mapped_column(String(16))
    long_venue: Mapped[str] = mapped_column(String(64))
    short_venue: Mapped[str] = mapped_column(String(64))
    long_symbol: Mapped[str] = mapped_column(String(64))
    short_symbol: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(40), index=True)
    opened_at: Mapped[datetime] = _ts()
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    target_notional_per_leg: Mapped[Decimal] = mapped_column(DecimalString)
    leverage: Mapped[Decimal] = mapped_column(DecimalString)
    baseline_long_equity: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    baseline_short_equity: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    max_delta_imbalance_usd: Mapped[Decimal] = mapped_column(DecimalString, default=Decimal(0))
    exit_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pnl: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    context: Mapped[dict] = mapped_column(JSON, default=dict)  # zero delta report, params, decision, plans
    owner: Mapped[str | None] = mapped_column(String(64), nullable=True)  # execution owner (section 52)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TradeLeg(Base):
    __tablename__ = "trade_legs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[str] = mapped_column(ForeignKey("trades.id"), index=True)
    role: Mapped[str] = mapped_column(String(8))
    venue: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(64))
    filled_quantity: Mapped[Decimal] = mapped_column(DecimalString, default=Decimal(0))
    filled_notional_usd: Mapped[Decimal] = mapped_column(DecimalString, default=Decimal(0))
    fees_usd: Mapped[Decimal] = mapped_column(DecimalString, default=Decimal(0))
    __table_args__ = (UniqueConstraint("trade_id", "role"),)


class TwapSlice(Base):
    __tablename__ = "twap_slices"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[str] = mapped_column(ForeignKey("trades.id"), index=True)
    phase: Mapped[str] = mapped_column(String(8))  # entry | exit
    slice_index: Mapped[int] = mapped_column(Integer)
    planned_notional: Mapped[Decimal] = mapped_column(DecimalString)
    status: Mapped[str] = mapped_column(String(24))
    long_submit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    short_submit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submission_time_difference_ms: Mapped[float | None] = mapped_column(nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("trade_id", "phase", "slice_index"),)


class Order(Base):
    __tablename__ = "orders"
    client_order_id: Mapped[str] = mapped_column(String(80), primary_key=True)  # idempotency key
    trade_id: Mapped[str] = mapped_column(ForeignKey("trades.id"), index=True)
    venue: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(64))
    leg: Mapped[str] = mapped_column(String(8))
    side: Mapped[str] = mapped_column(String(8))
    requested_quantity: Mapped[Decimal] = mapped_column(DecimalString)
    filled_quantity: Mapped[Decimal] = mapped_column(DecimalString, default=Decimal(0))
    average_fill_price: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    fee_usd: Mapped[Decimal] = mapped_column(DecimalString, default=Decimal(0))
    status: Mapped[str] = mapped_column(String(24))
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = _ts()


class Fill(Base):
    __tablename__ = "fills"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_order_id: Mapped[str] = mapped_column(String(80), index=True)
    trade_id: Mapped[str] = mapped_column(String(32), index=True)
    quantity: Mapped[Decimal] = mapped_column(DecimalString)
    price: Mapped[Decimal] = mapped_column(DecimalString)
    fee_usd: Mapped[Decimal] = mapped_column(DecimalString)
    reference_price: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    filled_at: Mapped[datetime] = _ts()
    __table_args__ = (UniqueConstraint("client_order_id", "filled_at", "quantity"),)


class PositionSnapshot(Base):
    __tablename__ = "positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[str] = mapped_column(String(32), index=True)
    leg: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[Decimal] = mapped_column(DecimalString)
    mark_price: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    exposure_usd: Mapped[Decimal] = mapped_column(DecimalString)
    recorded_at: Mapped[datetime] = _ts()


class FundingPayment(Base):
    __tablename__ = "funding_payments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[str] = mapped_column(String(32), index=True)
    venue: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(64))
    timestamp: Mapped[datetime] = _ts()
    rate: Mapped[Decimal] = mapped_column(DecimalString)
    notional: Mapped[Decimal] = mapped_column(DecimalString)
    expected_payment: Mapped[Decimal] = mapped_column(DecimalString)
    actual_payment: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    difference: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[str] = mapped_column(String(32), index=True)
    long_equity: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    short_equity: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    long_ratio: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    short_ratio: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    net_delta_usd: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    recorded_at: Mapped[datetime] = _ts()


class RiskEvent(Base):
    __tablename__ = "risk_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(24))
    priority: Mapped[int] = mapped_column(Integer)
    reasons: Mapped[list] = mapped_column(JSON)
    recorded_at: Mapped[datetime] = _ts()


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(48), index=True)
    severity: Mapped[str] = mapped_column(String(12))
    message: Mapped[str] = mapped_column(Text)
    trade_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    recorded_at: Mapped[datetime] = _ts()


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(48), index=True)
    data: Mapped[dict] = mapped_column(JSON)
    recorded_at: Mapped[datetime] = _ts()


class PaperAccount(Base):
    """Simulated venue state so paper trading survives a restart (section 55)."""

    __tablename__ = "paper_accounts"
    venue_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    state: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = _ts()


class KeyValue(Base):
    __tablename__ = "kv_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = _ts()
