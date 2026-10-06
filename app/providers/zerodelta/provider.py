"""ZeroDeltaProvider: the only interface strategy code may use for Zero Delta data.

Zero Delta is a discovery and research source. Nothing here places orders, and a Zero Delta
opportunity alone must never trigger one (project.md section 4).
"""

import asyncio
import hashlib
import json
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.core.config import ZeroDeltaSettings

from .callbacks import HISTORY_WINDOW, asset_payload, coin_table_payload
from .client import ZeroDeltaClient
from .exceptions import (
    ZeroDeltaAccessError,
    ZeroDeltaAuthError,
    ZeroDeltaConfigError,
    ZeroDeltaError,
    ZeroDeltaParseError,
    ZeroDeltaTimeoutError,
)
from .history import analyze_series
from .models import (
    AssetAnalysis,
    AssetInfo,
    HealthStatus,
    MarketEligibility,
    ParseIssue,
    SummaryBatch,
    SummaryMarket,
    ZeroDeltaFundingObservation,
    ZeroDeltaMarketSnapshot,
)
from .normalizer import apr_to_per_hour
from .parser import parse_dash_response
from .venues import VenueRegistry, VenueType, database_venue_name, venue_key

MAX_HOURLY_DAYS = int(HISTORY_WINDOW.rstrip("d"))  # the hourly chart holds about 14 days


class ZeroDeltaProvider:
    def __init__(self, settings: ZeroDeltaSettings, client: ZeroDeltaClient | None = None, *,
                 postgres_client=None, now=None):
        self._settings = settings
        self._client = client
        self._registry = VenueRegistry(settings.venue_types)
        self._postgres = postgres_client
        self._now = now or (lambda: datetime.now(UTC))
        self.summary_batch: SummaryBatch | None = None
        self._history_cache: dict[tuple, list[dict]] = {}

    @property
    def source(self) -> str:
        return self._settings.source

    def database_client(self):
        if self._postgres is None:
            from .postgres import PostgresClient
            self._postgres = PostgresClient(self._settings)
        return self._postgres

    def _database_contract(self):
        missing = self._settings.missing_database_contract()
        if missing:
            raise ZeroDeltaConfigError("Database semantics need explicit configuration: " + ", ".join(missing))
        if not self._settings.db_datetime_utc:
            raise ZeroDeltaConfigError("Non-UTC upstream datetimes require a verified timezone conversion")

    async def refresh_summary(self) -> SummaryBatch:
        """One small summary read. The batch boundary is NOT an asserted upstream refresh timestamp."""
        from .postgres import hour, number
        self._database_contract()
        raw = await self.database_client().summary()
        received = self._now()
        multiplier = Decimal(1_000_000) if self._settings.db_volume_unit == "millions" else Decimal(1)
        markets, issues, seen = [], [], set()
        counts = Counter((str(r.get("coin", "")).strip().upper(), str(r.get("exchange", "")).strip()) for r in raw)
        for row in raw:
            try:
                coin, exchange = str(row["coin"]).strip().upper(), str(row["exchange"]).strip()
                if not coin or not exchange or row["coin"] is None or row["exchange"] is None:
                    raise ZeroDeltaParseError("missing market identity")
                key = (coin, exchange)
                if key in seen:
                    raise ZeroDeltaParseError("duplicate coin/exchange summary: market identity is ambiguous")
                seen.add(key)
                volume = number(row["avg_volume"]) * multiplier
                if volume < 0:
                    raise ZeroDeltaParseError("negative average volume")
                oi = number(row.get("avg_open_interest"), nullable=True)
                windows = {n: number(row.get(n), nullable=True) for n in
                           ("avg_apr", "avg_apr_24h", "avg_apr_3d", "avg_apr_7d", "avg_apr_30d", "avg_apr_90d")}
                markets.append(SummaryMarket(coin=coin, exchange=exchange,
                    venue_name=database_venue_name(exchange, self._settings.db_venue_names), avg_volume_usd=volume,
                    avg_open_interest_usd=None if oi is None else oi * multiplier,
                    avg_apr_7d=windows["avg_apr_7d"], apr_windows=windows))
            except (KeyError, ZeroDeltaParseError) as exc:
                issues.append(ParseIssue(code="INVALID_SUMMARY", message=str(exc)))
        # Ambiguous duplicates invalidate ALL entries for that key, not merely the later row.
        duplicates = {k for k, count in counts.items() if count > 1}
        markets = [m for m in markets if (m.coin, m.exchange) not in duplicates]
        if not markets:
            raise ZeroDeltaParseError("database summary contains no valid markets")
        # Upstream samples arrive around HH:59. At next HH:05 the preceding hour is complete;
        # asking for the still-forming hour would reject every otherwise-current market.
        boundary = hour(received) - timedelta(hours=1)
        digest = hashlib.sha256(json.dumps([m.model_dump(mode="json") for m in markets],
                                          sort_keys=True).encode()).hexdigest()
        self.summary_batch = SummaryBatch(id=boundary.strftime("%Y%m%d%H") + "-" + digest,
            as_of=boundary, fetched_at=received, markets=tuple(markets), issues=tuple(issues))
        # ponytail: bounded in-memory market cache; a restart re-fetches indexed 14-day windows.
        wanted = {(m.coin, m.exchange) for m in markets}
        self._history_cache = {k: v for k, v in self._history_cache.items() if k[:2] in wanted}
        return self.summary_batch

    async def _database_asset(self, asset: str, days: int, keep_days: int, exchanges=None, symbols=None):
        from app.strategy.instruments import ContractKind, parse_market_identifier

        from .postgres import epoch, hour, missing_hours, number, utc
        self._database_contract()
        batch = self.summary_batch or await self.refresh_summary()
        end, received = batch.as_of, self._now()
        start = end - timedelta(days=keep_days)
        query_end = min(received, end + timedelta(hours=1) - timedelta(microseconds=1))
        selected = [m for m in batch.markets if m.coin == asset.upper()
                    and (exchanges is None or m.exchange in exchanges)]
        # A saved pick may have disappeared from the current summary. Its exact symbol still needs assessment.
        for exchange in (exchanges or ()):
            if not any(m.exchange == exchange for m in selected) and symbols and exchange in symbols:
                selected.append(SummaryMarket(coin=asset.upper(), exchange=exchange,
                    venue_name=database_venue_name(exchange, self._settings.db_venue_names), avg_volume_usd=Decimal(0)))
        histories, issues, markets = {}, [], []
        for summary in selected:
            name = summary.venue_name
            try:
                symbol = (symbols or {}).get(summary.exchange)
                if symbol is None:
                    rows = await self.database_client().symbols(asset.upper(), summary.exchange,
                                                               query_end - timedelta(days=1), query_end)
                    if len(rows) >= 3:
                        raise ZeroDeltaParseError("market symbol lookup reached its ambiguity budget")
                    matches = [r["symbol"] for r in rows if (i := parse_market_identifier(r["symbol"]))
                               and i.base == asset.upper() and i.kind is ContractKind.LINEAR]
                    if len(matches) != 1:
                        raise ZeroDeltaParseError("no unique compatible linear market symbol")
                    symbol = matches[0]
                instrument = parse_market_identifier(symbol)
                if not instrument or instrument.base != asset.upper() or instrument.kind is not ContractKind.LINEAR:
                    raise ZeroDeltaParseError("incompatible market symbol")
                key = (asset.upper(), summary.exchange, symbol)
                old = self._history_cache.get(key, [])
                # Refresh the last hour too: a later snapshot/correction must not be hidden by the cache.
                fetch_start = max(start, hour(utc(old[-1]["datetime"])) - timedelta(hours=1)) if old else start
                if old:
                    gaps = missing_hours((r["datetime"] for r in old), start, end)
                    if gaps:
                        # Re-read from the first gap so real upstream backfills become visible without a restart.
                        fetch_start = min(fetch_start, gaps[0])
                fresh = await self.database_client().history(asset.upper(), summary.exchange, symbol,
                                                            fetch_start, query_end)
                raw = [r for r in old if start <= utc(r["datetime"]) < fetch_start] + fresh
                self._history_cache[key] = raw
                while len(self._history_cache) > 2 * self._settings.research_pair_limit + 20:
                    self._history_cache.pop(next(iter(self._history_cache)))
                points, by_source = {}, {}
                interval_divisor = {"hours": Decimal(1), "minutes": Decimal(60), "seconds": Decimal(3600)}[
                    self._settings.db_interval_unit]
                for row in raw:
                    if row["exchange"] != summary.exchange or row["symbol"] != symbol:
                        raise ZeroDeltaParseError("history market identity mismatch")
                    ts = utc(row["datetime"])
                    if not start <= ts <= query_end:
                        raise ZeroDeltaParseError("history row outside requested window")
                    stamped = epoch(row["timestamp"], self._settings.db_timestamp_unit)
                    if stamped is None or abs((stamped - ts).total_seconds()) > 1:
                        raise ZeroDeltaParseError("epoch and datetime disagree: verify units/timezone")
                    interval = number(row["interval"]) / interval_divisor
                    if interval <= 0:
                        raise ZeroDeltaParseError("funding interval must be positive")
                    rate = number(row["fundingrate"]) * (100 if self._settings.db_funding_unit == "fraction" else 1)
                    price = number(row.get("markprice"), nullable=True)
                    value = (rate, interval, price)
                    if ts in by_source and by_source[ts] != value:
                        raise ZeroDeltaParseError("conflicting observations at the same source timestamp")
                    by_source[ts] = value
                    at = hour(ts)
                    if at not in points or ts > points[at][0]:
                        points[at] = (ts, row, rate, interval, price)
                if not points:
                    raise ZeroDeltaParseError("no market history in requested window")
                ordered = sorted(points.items())
                observations = tuple(ZeroDeltaFundingObservation(timestamp=at, source_time=ts,
                    asset=asset.upper(), venue_name=name, venue_type=self._registry.classify(name),
                    market_identifier=symbol, funding_rate_raw=rate, funding_interval_hours=interval,
                    normalized_funding_rate_per_hour=rate / interval, funding_apr=rate / interval * 8760,
                    funding_kind=self._settings.db_funding_kind, price=price)
                    for at, (ts, row, rate, interval, price) in ordered)
                last = ordered[-1][1]
                ts, row, rate, interval, _ = last
                coverage, _ = analyze_series([(o.timestamp, o.normalized_funding_rate_per_hour) for o in observations],
                    primary_days=days, min_days=self._settings.min_lookback_days,
                    max_missing_intervals=self._settings.max_missing_intervals, requested_end=end)
                snap = ZeroDeltaMarketSnapshot(timestamp=ts, received_at=received, source="postgres",
                    exchange_key=summary.exchange,
                    funding_kind=self._settings.db_funding_kind,
                    next_funding_time=epoch(row.get("nextfundingtime"), self._settings.db_timestamp_unit),
                    asset=asset.upper(), venue_name=name, venue_type=self._registry.classify(name),
                    market_identifier=symbol, display_symbol=symbol, funding_rate_raw=rate,
                    funding_interval_hours=interval, normalized_funding_rate_per_hour=rate / interval,
                    funding_apr=rate / interval * 8760, avg_24h_volume_usd=summary.avg_volume_usd,
                    open_interest_usd=summary.avg_open_interest_usd)
                volume_ok = summary.avg_volume_usd > self._settings.min_avg_24h_volume_usd
                reasons = coverage.reasons + (() if volume_ok else ("VOLUME_NOT_ABOVE_MIN",))
                histories[name] = observations
                markets.append(MarketEligibility(snapshot=snap, coverage=coverage, volume_ok=volume_ok,
                    history_ok=coverage.status == "OK", venue_type_known=snap.venue_type is not VenueType.UNKNOWN,
                    venue_type_verified=self._registry.is_verified(name), reasons=reasons))
            except ZeroDeltaParseError as exc:
                issues.append(ParseIssue(code="INVALID_HISTORY", venue=name, message=str(exc)))
            except ZeroDeltaTimeoutError as exc:
                # An isolated bounded query failure invalidates this market, not every other market in the batch.
                issues.append(ParseIssue(code="QUERY_TIMEOUT", venue=name, message=str(exc)))
        return AssetAnalysis(asset=asset.upper(), received_at=received, markets=tuple(markets), histories=histories,
                             issues=tuple(issues), source="postgres", batch_id=batch.id, analysis_end=end)

    def _get_client(self) -> ZeroDeltaClient:
        if self._client is None:
            self._client = ZeroDeltaClient(self._settings)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        if self._postgres is not None:
            await self._postgres.aclose()

    # ---- pure analysis (also used offline with fixtures) -------------------------------------

    def analyze_response(
        self,
        payload: Any,
        asset: str,
        *,
        received_at: datetime | None = None,
        days: int | None = None,
        keep_days: int | None = None,
    ) -> AssetAnalysis:
        """Turn one raw Dash response into snapshots, coverage, eligibility and windowed history.

        Eligibility and coverage always use the trailing `days` (7 by default). `keep_days` (up to the 14 days the
        chart holds) only lengthens the history that is kept, so an older week is available to test the selection
        on data it has not seen. It never changes which markets are eligible.
        """
        days = days or self._settings.primary_lookback_days
        keep_days = max(days, keep_days or days)
        if keep_days > MAX_HOURLY_DAYS:
            raise ValueError(f"hourly history covers at most {MAX_HOURLY_DAYS} days, asked for {keep_days}")
        received_at = received_at or datetime.now(UTC)
        parsed = parse_dash_response(payload, asset, received_at, self._registry)
        issues: list[ParseIssue] = list(parsed.issues)

        series_by_key = {venue_key(name): (name, pts) for name, pts in parsed.series.items()}
        snapshot_keys = {venue_key(s.venue_name) for s in parsed.snapshots}
        for key, (name, _) in series_by_key.items():
            if key not in snapshot_keys:
                issues.append(ParseIssue(
                    code="SYMBOL_MISMATCH", venue=name,
                    message="history series has no matching venue row; ignored",
                ))

        markets: list[MarketEligibility] = []
        histories: dict[str, tuple[ZeroDeltaFundingObservation, ...]] = {}
        threshold = self._settings.min_avg_24h_volume_usd
        for snap in parsed.snapshots:
            reasons: list[str] = []
            volume_ok = snap.avg_24h_volume_usd > threshold  # strictly greater than
            if not volume_ok:
                reasons.append("VOLUME_NOT_ABOVE_MIN")

            coverage = None
            history_ok = False
            match = series_by_key.get(venue_key(snap.venue_name))
            if match is None:
                reasons.append("NO_HISTORY")
                issues.append(ParseIssue(
                    code="NO_HISTORY", venue=snap.venue_name, message="venue row has no history series"
                ))
            else:
                coverage, window = analyze_series(
                    match[1],
                    primary_days=days,
                    min_days=self._settings.min_lookback_days,
                    max_missing_intervals=self._settings.max_missing_intervals,
                )
                history_ok = coverage.status == "OK"
                reasons.extend(coverage.reasons)
                if keep_days > days:
                    _, window = analyze_series(match[1], primary_days=keep_days, min_days=self._settings.min_lookback_days,
                                               max_missing_intervals=self._settings.max_missing_intervals)
                histories[snap.venue_name] = tuple(
                    ZeroDeltaFundingObservation(
                        timestamp=ts,
                        asset=snap.asset,
                        venue_name=snap.venue_name,
                        venue_type=snap.venue_type,
                        market_identifier=snap.market_identifier,
                        funding_apr=apr,
                        normalized_funding_rate_per_hour=apr_to_per_hour(apr),
                    )
                    for ts, apr in window
                )
            if snap.venue_type is VenueType.UNKNOWN:
                reasons.append("VENUE_TYPE_UNKNOWN")  # informational, not a locked filter

            markets.append(MarketEligibility(
                snapshot=snap, coverage=coverage, volume_ok=volume_ok, history_ok=history_ok,
                venue_type_known=snap.venue_type is not VenueType.UNKNOWN,
                venue_type_verified=self._registry.is_verified(snap.venue_name),
                reasons=tuple(reasons),
            ))

        return AssetAnalysis(
            asset=asset.upper(), received_at=received_at, markets=tuple(markets),
            histories=histories, issues=tuple(issues),
        )

    # ---- required public interface -----------------------------------------------------------

    async def health_check(self) -> HealthStatus:
        started = time.monotonic()
        try:
            if self.source == "postgres":
                await self.refresh_summary()
            else:
                await self._get_client().post_dash(coin_table_payload(self._settings))
        except (ZeroDeltaAuthError, ZeroDeltaAccessError) as exc:
            return HealthStatus(ok=False, auth_ok=False, error=str(exc))
        except ZeroDeltaError as exc:
            return HealthStatus(ok=False, auth_ok=True, error=str(exc))
        return HealthStatus(ok=True, auth_ok=True, latency_ms=(time.monotonic() - started) * 1000)

    async def fetch_asset(self, asset: str, days: int | None = None, keep_days: int | None = None, *,
                          exchanges=None, symbols=None) -> AssetAnalysis:
        if self.source == "postgres":
            days = days or self._settings.primary_lookback_days
            keep_days = max(days, keep_days or days)
            if not self._settings.min_lookback_days <= days <= keep_days <= MAX_HOURLY_DAYS:
                raise ValueError("database analysis requires 7-14 days")
            return await self._database_asset(asset, days, keep_days, exchanges, symbols)
        payload = await self._get_client().post_dash(asset_payload(asset, self._settings))
        # Parsing 14 days of hourly history for ~30 venues takes 60-120 ms for a big coin: done off the event loop.
        return await asyncio.to_thread(self.analyze_response, payload, asset, days=days, keep_days=keep_days)

    async def list_assets(self) -> list[AssetInfo]:
        """Every coin Zero Delta lists (its coin table), most liquid first."""
        if self.source == "postgres":
            batch = self.summary_batch or await self.refresh_summary()
            volumes: dict[str, Decimal] = {}
            for m in batch.markets:
                volumes[m.coin] = volumes.get(m.coin, Decimal(0)) + m.avg_volume_usd
            return [AssetInfo(coin=c, avg_volume_m=v / 1_000_000)
                    for c, v in sorted(volumes.items(), key=lambda item: (-item[1], item[0]))]
        payload = await self._get_client().post_dash(coin_table_payload(self._settings))
        try:
            rows = payload["response"]["coin-table"]["rowData"]
        except (KeyError, TypeError) as exc:
            raise ZeroDeltaParseError("Response has no coin table") from exc
        out: list[AssetInfo] = []
        for row in rows:
            coin = str(row.get("coin") or "").strip().upper()
            if not coin:
                continue
            vol = row.get("avg_volume")
            out.append(AssetInfo(coin=coin, avg_volume_m=None if vol in (None, "") else Decimal(str(vol))))
        out.sort(key=lambda a: (-(a.avg_volume_m or Decimal(0)), a.coin))
        return out

    async def get_current_markets(self, asset: str) -> list[ZeroDeltaMarketSnapshot]:
        analysis = await self.fetch_asset(asset)
        return [m.snapshot for m in analysis.markets]

    async def get_hourly_history(
        self, asset: str, days: int = 7
    ) -> dict[str, tuple[ZeroDeltaFundingObservation, ...]]:
        """Windowed hourly history per venue display name (at most 14 days are available)."""
        return (await self.fetch_asset(asset, days=days)).histories

    async def get_eligible_markets(self, asset: str) -> list[MarketEligibility]:
        """Markets passing both locked filters: volume > $5M and 7 complete days of history."""
        analysis = await self.fetch_asset(asset)
        return [m for m in analysis.markets if m.eligible]

    async def get_candidate_pairs(self, asset: str):
        """Compatible pairs of eligible markets with seven-day funding-edge analytics (Phase 2)."""
        # Local import: strategy builds on provider models.
        from app.strategy.pairs import generate_pairs

        return generate_pairs(await self.fetch_asset(asset), self._settings)
