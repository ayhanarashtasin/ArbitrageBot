"""Shared builders for unit tests."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.domain.market import BookLevel, Candle, FundingInfo, InstrumentMeta, OrderBook

D = Decimal
T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _levels(rows):
    return tuple(BookLevel(price=D(str(p)), quantity=D(str(q))) for p, q in rows)


def book(bids, asks, *, venue="v", symbol="BTCUSDT", age_ms=0, now=T0):
    t = now - timedelta(milliseconds=age_ms)
    return OrderBook(venue=venue, symbol=symbol, bids=_levels(bids), asks=_levels(asks),
                     source_time=t, received_at=t)


def deep_book(mid=100, spread=D("0.02"), size=100, levels=20, **kw):
    mid = D(str(mid))
    bids = [(mid - spread / 2 - i * D("0.01"), size) for i in range(levels)]
    asks = [(mid + spread / 2 + i * D("0.01"), size) for i in range(levels)]
    return book(bids, asks, **kw)


def funding(rate_per_hour, *, venue="v", interval=8, now=T0):
    interval = D(interval)
    return FundingInfo(venue=venue, symbol="BTCUSDT", funding_rate=D(str(rate_per_hour)) * interval,
                       interval_hours=interval, next_funding_time=now + timedelta(hours=1),
                       is_predicted=False, mark_price=D(100), index_price=D(100),
                       source_time=now, received_at=now)


def meta(*, venue="v", tradable=True, min_notional="5", linear=True, perpetual=True, qty_step="0.001",
         min_qty="0.001", underlying_type=None):
    return InstrumentMeta(venue=venue, symbol="BTCUSDT", base="BTC", quote="USDT", settle="USDT",
                          is_linear=linear, is_perpetual=perpetual, is_tradable=tradable,
                          contract_multiplier=D(1), tick_size=D("0.01"), qty_step=D(qty_step),
                          min_qty=D(min_qty), max_qty=None, underlying_type=underlying_type,
                          min_notional=None if min_notional is None else D(min_notional),
                          funding_interval_hours=D(8), max_leverage=D(20), received_at=T0)


def candles(closes, *, venue="v", start=T0):
    return [Candle(venue=venue, symbol="BTCUSDT", open_time=start + timedelta(minutes=i),
                   open=D(str(c)), high=D(str(c)), low=D(str(c)), close=D(str(c)), volume=D(1))
            for i, c in enumerate(closes)]


def make_pair_analytics(long_name="L", short_name="S", *, long_pct=0.0, short_pct=0.03, mean_pct=0.03, asset="BTC"):
    """A PairAnalytics as the scanner would hand to the engine. Rates are ZD percent per hour."""
    from app.providers.zerodelta.models import HistoryCoverage
    from app.providers.zerodelta.venues import VenueType
    from app.strategy.analytics import EdgeStats
    from app.strategy.pairs import PairAnalytics, PairLeg

    def leg(name, pct):
        return PairLeg(venue_name=name, venue_type=VenueType.CEX, venue_type_verified=True,
                       market_identifier=f"{asset}/USDT:USDT", settle_asset="USDT",
                       avg_24h_volume_usd=D(50_000_000), funding_interval_hours=D(8),
                       current_funding_per_hour=D(str(pct)))

    stats = EdgeStats(mean=D(str(mean_pct)), median=D(str(mean_pct)), minimum=D(0), maximum=D(1), stddev=D(0),
                      positive_edge_ratio=D(1), direction_consistency_ratio=D(1), sign_flip_count=0,
                      longest_positive_edge_streak=169, latest=D(str(mean_pct)))
    cov = HistoryCoverage(requested_start=None, requested_end=None, first_timestamp=None, last_timestamp=None,
                          coverage_seconds=168 * 3600, observation_count=169, expected_observation_count_if_known=169,
                          missing_intervals=0, duplicate_count=0, out_of_order_count=0, status="OK")
    return PairAnalytics(asset=asset, long_leg=leg(long_name, long_pct), short_leg=leg(short_name, short_pct),
                         pair_type="CEX<->CEX", status="OK", settlement_mismatch=False, coverage=cov,
                         current_funding_edge=D(str((short_pct - long_pct) / 100)), stats=stats)
