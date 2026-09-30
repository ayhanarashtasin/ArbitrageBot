"""ZeroDeltaProvider: the only interface strategy code may use for Zero Delta data.

Zero Delta is a discovery and research source. Nothing here places orders, and a Zero Delta
opportunity alone must never trigger one (project.md section 4).
"""

import time
from decimal import Decimal
from datetime import UTC, datetime
from typing import Any

from app.core.config import ZeroDeltaSettings

from .callbacks import HISTORY_WINDOW, asset_payload, coin_table_payload
from .client import ZeroDeltaClient
from .exceptions import ZeroDeltaAuthError, ZeroDeltaError, ZeroDeltaParseError
from .history import analyze_series
from .models import (
    AssetAnalysis,
    AssetInfo,
    HealthStatus,
    MarketEligibility,
    ParseIssue,
    ZeroDeltaFundingObservation,
    ZeroDeltaMarketSnapshot,
)
from .normalizer import apr_to_per_hour
from .parser import parse_dash_response
from .venues import VenueRegistry, VenueType, venue_key

MAX_HOURLY_DAYS = int(HISTORY_WINDOW.rstrip("d"))  # the hourly chart holds about 14 days


class ZeroDeltaProvider:
    def __init__(self, settings: ZeroDeltaSettings, client: ZeroDeltaClient | None = None):
        self._settings = settings
        self._client = client
        self._registry = VenueRegistry(settings.venue_types)

    def _get_client(self) -> ZeroDeltaClient:
        if self._client is None:
            self._client = ZeroDeltaClient(self._settings)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

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
            await self._get_client().post_dash(coin_table_payload(self._settings))
        except ZeroDeltaAuthError as exc:
            return HealthStatus(ok=False, auth_ok=False, error=str(exc))
        except ZeroDeltaError as exc:
            return HealthStatus(ok=False, auth_ok=True, error=str(exc))
        return HealthStatus(ok=True, auth_ok=True, latency_ms=(time.monotonic() - started) * 1000)

    async def fetch_asset(self, asset: str, days: int | None = None, keep_days: int | None = None) -> AssetAnalysis:
        payload = await self._get_client().post_dash(asset_payload(asset, self._settings))
        return self.analyze_response(payload, asset, days=days, keep_days=keep_days)

    async def list_assets(self) -> list[AssetInfo]:
        """Every coin Zero Delta lists (its coin table), most liquid first."""
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
        from app.strategy.pairs import generate_pairs  # local import: strategy builds on provider models

        return generate_pairs(await self.fetch_asset(asset), self._settings)
