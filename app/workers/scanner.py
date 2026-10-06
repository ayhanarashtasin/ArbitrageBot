"""Scanner: the plan, end to end (identify -> select -> simulate -> verify and paper-trade -> watch).

IDENTIFY  every coin Zero Delta lists: all possible pairs with their 1-week funding (>= 7 complete days, > $5M volume).
          14 days of hourly history are kept: the trailing 7 rank the pairs, the 7 before them test the selection rule.
SELECT    the best 1-3 pairs across all coins (one pair per coin), ranked on the 7-day mean edge, with the exchange of
          each leg; the pair's edge must still be positive now; stocks inside the earnings blackout are never traded.
SIMULATE  each pick is replayed through the bot's own rules (6 h TWAP, $100 slices, fees, 4x accounts, the 30 % and 10 %
          equity exits, the normal exit). The same selection is also run as it would have been a week ago and replayed
          on the following week (walk-forward): the honest test of whether picking this way makes money.
VERIFY    only the selected tradable pairs go to direct-venue checks (executable prices, 8 h median-funding spread,
          earnings, equal sizing, Zero Delta cross-check). ENTER opens a paper trade: the forward simulation.
WATCH     selected pairs that are waiting (usually for low volatility) are re-checked every minute until they enter or
          the next sweep replaces them.

A Zero Delta failure stops NEW opportunity generation and raises an alert; it never touches open trades,
which the engine manages from the venues alone (sections 6, 38).
"""

import asyncio
import logging
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from dataclasses import replace

from app.core.clock import Clock
from app.core.config import ZeroDeltaSettings
from app.core.trading_config import DEFAULT_PAPER_FEE_BPS, PAPER_FEES_BPS, ResolvedParams
from app.domain.market import InstrumentMeta
from app.exchanges.base import ExchangeAdapter, ExchangeError
from app.exchanges.registry import binding_for, is_excluded
from app.execution.engine import STALE_BOOK_RETRY_SECONDS, TradeEngine
from app.execution.venue_provider import UnsupportedVenue
from app.market_data.earnings import CRYPTO, EarningsCalendar, asset_class
from app.market_data.volatility import VolScore, candles_needed, low_vol_score
from app.notifications.notifier import Notifier
from app.persistence.repository import DB_OUTAGE_ERRORS, Repository
from app.providers.zerodelta import ZeroDeltaAuthError, ZeroDeltaError, ZeroDeltaProvider, ZeroDeltaRateLimitError
from app.providers.zerodelta.models import AssetAnalysis
from app.providers.zerodelta.provider import MAX_HOURLY_DAYS
from app.strategy.instruments import parse_market_identifier
from app.strategy.pairs import PairAnalytics, generate_pairs
from app.strategy.selection import Pick, rank, select, why_not_tradable, screen_summary
from app.providers.zerodelta.exceptions import ZeroDeltaAccessError
from app.strategy.simulation import Bar, SimRules, simulate_pair
from app.strategy.verification import Decision, LegData, Outcome, evaluate_entry

log = logging.getLogger(__name__)
D = Decimal
BPS = D(10_000)
WEEK = timedelta(days=7)
HISTORY_KEEP_DAYS = MAX_HOURLY_DAYS  # 14: the chart's whole hourly window
KEEP_TOP_PER_ASSET = 15  # candidate pairs kept per coin after a sweep: bounds memory across ~450 coins
KEEP_TRADABLE_PER_ASSET = 5  # plus the best tradable ones, which may rank lower than the top 15
MAX_CONSECUTIVE_ERRORS = 5  # this many Zero Delta errors in a row abort the sweep as an outage
HOURLY_BARS = HISTORY_KEEP_DAYS * 24 + 4  # hourly candles fetched per picked coin for the simulation
# Zero Delta venues that list stocks and other TradFi contracts: a coin on one of them is checked for earnings
# unless a venue with a finer label (Binance underlyingType) says otherwise.
TRADFI_VENUE_HINTS = frozenset({"Trade.xyz", "Ondo Perps", "Ligher on RH"})
# Where hourly prices of a coin are read when neither leg of the pair has an adapter (the underlying is the same).
PRICE_SOURCES = ("Binance USDT", "Bybit USDT", "Hyperliquid", "OKX", "Gate", "MEXC USDT", "BloFin USDT", "Bitget")


# A pick that fails a check which cannot change within hours is set aside, so the next best pair takes its slot.
# Live 2026-10-02: ONE (Binance and OKX list two different coins under that ticker, prices 7 % apart) was the top
# tradable pick of every sweep for days and failed legs_track_same_underlying every time, wasting one of three slots.
PERMANENT_CHECKS = frozenset({"legs_track_same_underlying"})
PARK_FOR = timedelta(hours=24)


def scan_stale_seconds(params: ResolvedParams) -> float:
    """A sweep reports once per cycle (several minutes of sweeping plus the pause): two cycles and a margin. Sweep
    health older than this is stale, and a venue-pair entry this old no longer describes a pair the bot uses."""
    return 2 * params.scan_interval_seconds + 1800.0


def _failure_text(exc: BaseException) -> str:
    """What failed, in words. A TaskGroup wraps the real error in an ExceptionGroup whose own text is only
    "unhandled errors in a TaskGroup (1 sub-exception)" (live 2026-10-03: the outage alert hid a database timeout).

    Provider errors are already scrubbed of connection strings and credentials; any other error gives its type only.
    """
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return str(exc) if isinstance(exc, ZeroDeltaError) else type(exc).__name__


@dataclass
class ScanReport:
    asset: str
    zero_delta_ok: bool = True
    pairs_found: int = 0
    executable_pairs: int = 0
    verified: int = 0
    opened: list[str] = field(default_factory=list)
    shadow_opened: list[str] = field(default_factory=list)
    outcomes: dict[str, int] = field(default_factory=dict)
    note: str = ""
    scanned_at: str | None = None  # ISO time of this scan
    zero_delta_eligible_markets: int = 0
    total_markets: int = 0
    top_pairs: list[dict] = field(default_factory=list)  # best pairs by 7-day mean edge, for display only
    market_validation: list[dict] = field(default_factory=list)


@dataclass
class _Kept:
    """What a sweep remembers about one coin: only the histories of its candidate pairs' venues."""

    asset: str
    histories: dict
    newest: datetime | None = None  # last hourly point: the selection week is (newest - 7 d, newest]
    tradfi_hint: bool = False  # listed on a Zero Delta venue that carries stocks
    received_at: datetime | None = None
    source_time: datetime | None = None


class Scanner:
    def __init__(self, *, params: ResolvedParams, zd_settings: ZeroDeltaSettings, zd: ZeroDeltaProvider | None,
                 source_for_venue: Callable[[str], ExchangeAdapter | None], repo: Repository, engine: TradeEngine,
                 notifier: Notifier, clock: Clock, health=None, earnings: EarningsCalendar | None = None,
                 execution_venue: Callable[[str], bool] | None = None,
                 excluded_venues: frozenset[str] = frozenset()):
        self.params, self.zd_settings, self.zd = params, zd_settings, zd
        # Market data: every venue with a public adapter, except EXCLUDE_VENUES. An excluded venue is never selected,
        # verified or read for prices; trades already open on it stay with the engine, which has its own lookup.
        self.excluded_venues = excluded_venues
        self._source_for = source_for_venue if not excluded_venues else (
            lambda venue: None if is_excluded(venue, excluded_venues) else source_for_venue(venue))
        # Live mode: only venues with credentials that passed their readiness check can be TRADED. None = paper mode,
        # where every venue with an adapter can be traded on paper.
        self.execution_venue = execution_venue
        self.repo, self.engine, self.notifier, self.clock = repo, engine, notifier, clock
        self.health = health
        self.earnings = earnings
        self._zd_alerted: str | None = None  # avoid repeating the same outage alert every scan
        self.last_report: dict[str, ScanReport] = {}
        self.last_selection: dict = {}
        self.last_top_pairs: list[dict] = []  # best pairs across every coin, for display
        self.sweep: dict = {}  # totals of the latest sweep
        self.progress: dict = {"running": False, "done": 0, "total": 0}
        self._known_assets: list[str] = []
        self.watch: list[Pick] = []  # selected tradable pairs still waiting to enter
        self._watch_seen: dict[tuple, tuple] = {}  # pair key -> (outcome, reasons) last recorded
        self._found: dict = {}
        self._earnings_block: dict[str, str] = {}
        self._asset_class: dict[str, str] = {}
        self._bars: dict[str, dict[datetime, Bar]] = {}
        self._selected_keys: set[tuple] = set()
        self._parked: dict[tuple, tuple[datetime, str]] = {}  # pair key -> (set aside until, failed checks)
        self._enter_lock = asyncio.Lock()  # the sweep and the watch never open the same pair twice
        self.discovery: dict = {}
        self._db_markets: dict[str, set[str]] = {}
        self._db_symbols: dict[str, dict[str, str]] = {}
        self._db_candidate_assets: set[str] = set()
        self._excluded_assets: set[str] = set()
        self._funding_estimates: dict[tuple[str, str], deque] = {}
        # Set when the bot is shutting down. A sweep then fetches no more coins and opens no trade: a sweep takes
        # minutes, and shutdown must not wait for one that nobody will read.
        self.stopping = False

    @property
    def database_source(self) -> bool:
        return self.zd_settings.source == "postgres"

    # ---- Zero Delta ---------------------------------------------------------------------------
    async def _analysis(self, asset: str) -> tuple[AssetAnalysis | None, str | None, str]:
        """(analysis, failure reason, detail). Reasons: no_zd, auth, rate_limit, error, stale."""
        if self.zd is None:
            return None, "no_zd", "Zero Delta is not configured"
        try:
            kwargs = {"exchanges": sorted(self._db_markets.get(asset, set())),
                      "symbols": self._db_symbols.get(asset)} if self.database_source else {}
            analysis = await self.zd.fetch_asset(asset, keep_days=HISTORY_KEEP_DAYS, **kwargs)
        except ZeroDeltaAccessError as exc:
            return None, "access", str(exc)
        except ZeroDeltaAuthError as exc:
            return None, "auth", str(exc)
        except ZeroDeltaRateLimitError as exc:
            return None, "rate_limit", str(exc)
        except ZeroDeltaError as exc:
            return None, "error", f"{asset}: {type(exc).__name__}: {exc}"
        lag_limit = timedelta(hours=float(self.params.zero_delta_history_max_lag_hours))
        newest = self._newest(analysis)
        if self.database_source:
            # Each market and pair already requires coverage through analysis_end. Retain their precise reasons,
            # including missing tails and query timeouts, rather than throwing away the whole asset analysis.
            return analysis, None, ""
        if newest is None or self.clock.now() - newest > lag_limit:
            return None, "stale", f"{asset}: newest history point is {newest}"
        return analysis, None, ""

    @staticmethod
    def _newest(analysis: AssetAnalysis) -> datetime | None:
        return max((m.coverage.last_timestamp for m in analysis.markets if m.coverage and m.coverage.last_timestamp),
                   default=None)

    async def _zd_failure(self, kind: str, message: str) -> None:
        self.watch = []
        self.repo.add_provider_event(kind.upper(), message)
        if self.health:
            self.health.mark("zero_delta", False, kind, stale_after=scan_stale_seconds(self.params))
        if self._zd_alerted != kind:  # one alert per distinct failure until it recovers
            self._zd_alerted = kind
            await self.notifier.alert(kind, message + " | new opportunities stopped; open trades keep running",
                                      severity="critical")

    def _persist_discovery(self, analysis: AssetAnalysis, pairs: list[PairAnalytics]) -> None:
        now = self.clock.now()
        from app.persistence import models as m
        rows: list[tuple[type, dict]] = []
        for mk in analysis.markets:
            s = mk.snapshot
            rows.append((m.ZeroDeltaMarket, {
                "asset": analysis.asset, "venue_name": s.venue_name, "venue_type": s.venue_type.value,
                "market_identifier": s.market_identifier, "funding_rate_raw": s.funding_rate_raw,
                "funding_interval_hours": s.funding_interval_hours,
                "normalized_funding_rate_per_hour": s.normalized_funding_rate_per_hour,
                "avg_24h_volume_usd": s.avg_24h_volume_usd, "eligible": mk.eligible, "reasons": list(mk.reasons),
                "received_at": analysis.received_at}))
            if mk.coverage is not None:
                rows.append((m.ZeroDeltaHistory, {
                    "asset": analysis.asset, "venue_name": s.venue_name,
                    "coverage": mk.coverage.model_dump(mode="json"), "status": mk.coverage.status,
                    "recorded_at": now}))
        self.repo.record_many(rows)  # one transaction per coin, not one per row

    # ---- direct verification ------------------------------------------------------------------
    def _leg_fee(self, adapter: ExchangeAdapter) -> tuple[Decimal, bool]:
        taker_bps, _ = PAPER_FEES_BPS.get(adapter.name, DEFAULT_PAPER_FEE_BPS)
        return taker_bps / BPS, False  # an assumption: the account's own fee tier needs a signed call (live mode)

    async def _prepare(self, adapter: ExchangeAdapter, leg) -> tuple[InstrumentMeta, list]:
        """Symbol and the slow, retry-prone 1-minute candles."""
        inst = parse_market_identifier(leg.market_identifier)
        if inst is None:
            raise UnsupportedVenue(f"unrecognised market identifier {leg.market_identifier!r}")
        meta: InstrumentMeta = await adapter.resolve_symbol(inst.base, inst.quote, inst.settle)
        return meta, await adapter.get_closed_candles(meta.symbol, "1m", candles_needed(self.params))

    async def _snapshot(self, adapter: ExchangeAdapter, meta: InstrumentMeta) -> LegData:
        funding, book = await asyncio.gather(adapter.get_funding_info(meta.symbol), adapter.get_order_book(meta.symbol))
        key = (adapter.name, meta.symbol)
        estimates = self._funding_estimates.setdefault(key, deque(maxlen=512))
        estimates.append(funding)
        while len(self._funding_estimates) > 64:
            self._funding_estimates.pop(next(iter(self._funding_estimates)))
        fee, verified = self._leg_fee(adapter)
        try:
            schedule = await adapter.get_fee_schedule(meta.symbol)
            fee, verified = schedule.taker_fraction, schedule.verified
        except ExchangeError:
            pass  # public data only: keep the labelled assumption
        live_fees = getattr(self.engine.provider, "fee_schedule", None)
        if live_fees is not None:  # live mode: the account's real tier, read through its signed client
            schedule = await live_fees(adapter.name, meta)
            if schedule is not None and schedule.verified:
                fee, verified = schedule.taker_fraction, True
        settled = None
        history = []
        try:  # the venue's last SETTLED rate: what Zero Delta shows, for the cross-check
            history = await adapter.get_funding_history(meta.symbol, 3)
            if history and funding.interval_hours:
                settled = history[-1][1] / funding.interval_hours
        except (ExchangeError, ValueError) as exc:
            log.info("no settled funding for %s on %s: %s", meta.symbol, adapter.name, exc)
        return LegData(meta=meta, funding=funding, book=book, taker_fee=fee, fee_verified=verified,
                        settled_rate_per_hour=settled, settled_history=tuple(history), predicted_history=tuple(estimates))

    async def verify_pair(self, pair: PairAnalytics, zd_age_seconds: float | None = 0.0) -> Decision | None:
        long_src, short_src = self._source_for(pair.long_leg.venue_name), self._source_for(pair.short_leg.venue_name)
        if long_src is None or short_src is None:
            return None
        try:
            # Phase 1: slow data for both legs. Phase 2: funding and order books for both legs TOGETHER and LAST,
            # so neither book has aged while the other leg was still loading (they are judged for freshness).
            (long_meta, long_c), (short_meta, short_c) = await asyncio.gather(
                self._prepare(long_src, pair.long_leg), self._prepare(short_src, pair.short_leg))
            long_data, short_data = await asyncio.gather(self._snapshot(long_src, long_meta),
                                                         self._snapshot(short_src, short_meta))
            limit = self.params.max_market_data_age_ms
            if max(long_data.book.age_ms(self.clock.now()), short_data.book.age_ms(self.clock.now())) > limit:
                # one retry of both books: a cached or quiet snapshot often looks old while the next reply is fresh
                await self.clock.sleep(STALE_BOOK_RETRY_SECONDS)
                lb, sb = await asyncio.gather(long_src.get_order_book(long_meta.symbol),
                                              short_src.get_order_book(short_meta.symbol))
                long_data, short_data = replace(long_data, book=lb), replace(short_data, book=sb)
        except (ExchangeError, UnsupportedVenue, ValueError) as exc:  # ValueError: an adapter refused an argument;
            # one pair must never abort the verification of the others (a Bybit candle cap once did exactly that)
            log.warning("verification of %s/%s failed: %s", pair.long_leg.venue_name, pair.short_leg.venue_name, exc)
            if self.health:
                self.health.mark(f"venue:{pair.long_leg.venue_name}/{pair.short_leg.venue_name}", False, str(exc),
                                 expire_after=scan_stale_seconds(self.params))
            return None
        if self.health:
            self.health.mark(f"venue:{pair.long_leg.venue_name}/{pair.short_leg.venue_name}", True,
                             "direct venue data fetched successfully", expire_after=scan_stale_seconds(self.params))
        vol: VolScore = low_vol_score(long_c, short_c, rv_window=self.params.rv_window_minutes,
                                      basis_window=self.params.basis_vol_window_minutes,
                                      min_observations=self.params.vol_min_observations)
        block = self._earnings_block.get(pair.asset)
        if self.earnings is not None:
            cls = asset_class([long_meta.underlying_type, short_meta.underlying_type])
            if cls == CRYPTO:
                cls = self._asset_class.get(pair.asset, cls)
            block = await self.earnings.blackout(pair.asset, cls)
        return evaluate_entry(pair, long_data, short_data, vol, self.params, now=self.clock.now(),
                              zero_delta_age_seconds=zd_age_seconds, earnings_block=block)

    # ---- step 1: identify ----------------------------------------------------------------------
    def _tradable(self, pair: PairAnalytics) -> bool:
        return why_not_tradable(pair, self._exec_source) is None

    def _exec_source(self, venue: str) -> ExchangeAdapter | None:
        src = self._source_for(venue)
        if src is None or (self.execution_venue is not None and not self.execution_venue(venue)):
            return None
        return src

    async def resolve_assets(self, configured: list[str], excluded: list[str] | None = None) -> list[str]:
        """The coins to scan. "ALL" means every coin Zero Delta lists, most liquid first."""
        excl = {a.upper() for a in (excluded or [])}
        wanted = [a.upper() for a in configured]
        self._excluded_assets = excl
        if self.database_source:
            return [a for a in wanted if a not in excl]  # scan_all reads ONE summary, including for ALL
        if "ALL" not in wanted:
            return [a for a in wanted if a not in excl]
        names: list[str] = []
        if self.zd is not None:
            try:
                names = [a.coin for a in await self.zd.list_assets()]
                self._known_assets = names
            except ZeroDeltaAuthError as exc:
                await self._zd_failure("zero_delta_auth_failure", str(exc))
                names = self._known_assets
            except ZeroDeltaError as exc:
                await self._zd_failure("zero_delta_outage", f"coin list: {type(exc).__name__}: {exc}")
                names = self._known_assets
        extras = [a for a in wanted if a != "ALL" and a not in names]
        return [a for a in names + extras if a not in excl]

    def _keep(self, pairs: list[PairAnalytics]) -> list[PairAnalytics]:
        """The best candidates of one coin (and its best tradable ones), in rank order, one per venue pair."""
        ranked = rank(pairs, self.params.selection_metric)
        keep, seen = [], set()
        # Filter before truncation: obsolete edges must not crowd a viable lower-ranked pair out of memory.
        viable = [p for p in ranked if not self.params.selection_require_live_edge or p.current_funding_edge > 0]
        for p in viable[:KEEP_TOP_PER_ASSET] + [q for q in viable if self._tradable(q)][:KEEP_TRADABLE_PER_ASSET]:
            key = (p.long_leg.venue_name, p.short_leg.venue_name)
            if key not in seen:
                seen.add(key)
                keep.append(p)
        return keep

    def _candidate_pairs(self, analysis: AssetAnalysis, newest: datetime | None
                         ) -> tuple[list[PairAnalytics], list[PairAnalytics]]:
        """(pairs now, the same coin's pairs one week earlier). Reads only its arguments: safe in a worker thread."""
        pairs = [p for p in generate_pairs(analysis, self.zd_settings) if p.status == "OK" and p.stats]
        earlier = [p for p in generate_pairs(analysis, self.zd_settings, as_of=newest - WEEK)
                   if p.status == "OK" and p.stats] if newest and analysis.source != "postgres" else []
        return pairs, earlier

    async def _discover(self, asset: str):
        """Every possible pair for one coin with its 7-day funding analytics, straight from Zero Delta.

        Returns (report, kept histories, candidate pairs, the same coin's candidates one week earlier, failure).
        Only the best candidates are kept, so a sweep over ~450 coins stays small in memory.
        """
        report = ScanReport(asset, scanned_at=self.clock.now().isoformat())
        analysis, reason, detail = await self._analysis(asset)
        if analysis is None:
            report.zero_delta_ok, report.note = False, detail or "Zero Delta unavailable"
            return report, None, [], [], (reason, detail)
        newest = self._newest(analysis)
        # Pure computation, 0.3-0.4 s per run for BTC or ETH (measured live): in a worker thread, so the event loop
        # that also runs the trades and the API is not held for it (two big coins at once paused it ~2 s per sweep).
        pairs, earlier = await asyncio.to_thread(self._candidate_pairs, analysis, newest)
        self._persist_discovery(analysis, pairs)
        report.market_validation = [{"venue": m.snapshot.venue_name, "exchange": m.snapshot.exchange_key,
            "symbol": m.snapshot.market_identifier, "eligible": m.eligible, "reasons": list(m.reasons),
            "source_time": m.snapshot.timestamp.isoformat() if m.snapshot.timestamp else None,
            "coverage": m.coverage.model_dump(mode="json") if m.coverage else None} for m in analysis.markets]
        report.market_validation += [{"venue": i.venue, "eligible": False, "reasons": [i.code],
                                      "detail": i.message} for i in analysis.issues]
        report.zero_delta_ok = not any(i.code == "QUERY_TIMEOUT" for i in analysis.issues)
        if analysis.source == "postgres":
            for market in report.market_validation:
                if not market["eligible"]:
                    log.info("history excluded %s / %s: %s%s", asset, market["venue"], ", ".join(market["reasons"]),
                             f" ({market['detail']})" if market.get("detail") else "")
        report.pairs_found = len(pairs)
        report.total_markets = len(analysis.markets)
        if analysis.source == "postgres" and analysis.issues:
            report.note = "; ".join(f"{i.venue}: {i.message}" for i in analysis.issues)[:500]
        report.zero_delta_eligible_markets = sum(1 for m in analysis.markets if m.eligible)
        report.executable_pairs = sum(1 for p in pairs if self._tradable(p))
        keep, keep_earlier = self._keep(pairs), self._keep(earlier)
        venues = {v for p in keep + keep_earlier for v in (p.long_leg.venue_name, p.short_leg.venue_name)}
        for saved in getattr(self, "_due_selections", []):
            if saved.asset == asset:
                venues.update((saved.long_venue, saved.short_venue))
        kept = _Kept(asset, {v: analysis.histories[v] for v in venues if v in analysis.histories}, newest,
                     any(m.snapshot.venue_name in TRADFI_VENUE_HINTS for m in analysis.markets), analysis.received_at)
        kept.source_time = min((m.snapshot.timestamp for m in analysis.markets if m.eligible
                                and m.snapshot.timestamp is not None), default=None)
        report.top_pairs = [dict(self._pair_row(p), asset=asset, executable=self._tradable(p))
                            for p in rank(pairs, self.params.selection_metric)[:5]]
        return report, kept, keep, keep_earlier, None

    # ---- stocks: asset class and the earnings blackout -------------------------------------------
    async def _venue_labels(self) -> dict[str, list[str]]:
        """base -> underlying type, from Binance's instrument list (the one venue that labels every contract)."""
        labels = {}
        seen = set()
        for name in PRICE_SOURCES:
            src = self._source_for(name)
            if src is None or id(src) in seen:
                continue
            seen.add(id(src))
            try:
                for i in await src.list_instruments():
                    if i.underlying_type:
                        labels.setdefault(i.base.upper(), []).append(i.underlying_type)
            except ExchangeError as exc:
                log.warning("could not read %s contract labels: %s", name, exc)
        return labels

    async def _classify_and_check_earnings(self, found: dict) -> None:
        labels = await self._venue_labels()
        self._asset_class, self._earnings_block = {}, {}
        for asset, (kept, _, _) in found.items():
            # A venue's own contract label wins. The Zero Delta venue hint only decides when no venue labels the coin:
            # "Ligher on RH" and "Ondo Perps" list BTC, ETH, SOL... next to stocks, so the hint alone once made them
            # "tradfi", and a Nasdaq outage (or Emeren's SOL ticker) would have blocked crypto, which has no condition.
            hints = labels.get(asset.upper()) or (["TRADFI"] if kept.tradfi_hint else [])
            self._asset_class[asset] = asset_class(hints)
        if self.earnings is None:
            return
        for asset, cls in self._asset_class.items():
            if cls != CRYPTO:
                reason = await self.earnings.blackout(asset, cls)
                if reason:
                    self._earnings_block[asset] = reason

    # ---- step 3: simulate ----------------------------------------------------------------------
    def _fee(self, adapter: ExchangeAdapter | None) -> Decimal:
        taker_bps, _ = PAPER_FEES_BPS.get(adapter.name, DEFAULT_PAPER_FEE_BPS) if adapter else DEFAULT_PAPER_FEE_BPS
        return taker_bps / BPS

    async def _load_bars(self, assets: set[str], pairs: dict[str, PairAnalytics]) -> None:
        """Hourly prices of each picked coin, so the simulation can apply the 30 % / 10 % equity exits."""
        async def one(asset: str) -> None:
            pair = pairs[asset]
            names = [pair.long_leg.venue_name, pair.short_leg.venue_name, *PRICE_SOURCES]
            inst = parse_market_identifier(pair.long_leg.market_identifier)
            for name in dict.fromkeys(names):
                src, binding = self._source_for(name), binding_for(name)
                if src is None or binding is None or inst is None:
                    continue
                try:
                    meta = await src.resolve_symbol(inst.base, binding.quote, binding.settle)
                    candles = await src.get_candles(meta.symbol, "1h", HOURLY_BARS)
                except (ExchangeError, ValueError) as exc:
                    log.info("no hourly prices for %s on %s: %s", asset, name, exc)
                    continue
                if candles:
                    self._bars[asset] = {c.open_time: Bar(c.high, c.low, c.close) for c in candles}
                    return
            self._bars[asset] = {}

        await asyncio.gather(*(one(a) for a in assets if a not in self._bars))

    def _simulate(self, pair: PairAnalytics, kept: _Kept, start: datetime | None = None,
                  end: datetime | None = None, params: ResolvedParams | None = None) -> dict | None:
        """Run the pair through the bot's rules between start and end (default: its own selection week)."""
        long_h = kept.histories.get(pair.long_leg.venue_name)
        short_h = kept.histories.get(pair.short_leg.venue_name)
        if not long_h or not short_h:
            return None
        end = end or pair.window_end or kept.newest
        start = start or (end - WEEK if end else None)
        p = params or self.params
        try:
            sim = simulate_pair(
                {o.timestamp: o.normalized_funding_rate_per_hour for o in long_h},
                {o.timestamp: o.normalized_funding_rate_per_hour for o in short_h},
                notional_usd=p.target_notional_per_leg_usd,
                child_cap_usd=p.twap_max_child_notional_usd,
                entry_minutes=p.twap_entry_duration_minutes,
                long_fee=self._fee(self._source_for(pair.long_leg.venue_name)),
                short_fee=self._fee(self._source_for(pair.short_leg.venue_name)),
                slippage_bps=p.simulation_slippage_bps,
                rules=SimRules.from_params(p), prices=self._bars.get(pair.asset),
                start=start, end=end)
        except ValueError:
            return None
        out = sim.as_dict()
        out["fees_assumed"] = True  # standard public fee tiers, not this account's real ones
        out["limitations"] = ["Hourly funding approximation, not actual settlement cashflows",
                              "No historical spread or 1-minute volatility entry gates", "Venue basis ignored"]
        if not sim.price_checked:
            out["limitations"].append("Complete hourly prices unavailable: equity exits were not tested")
        return out

    # ---- step 2: select -------------------------------------------------------------------------
    def _pick_row(self, pick: Pick, simulation: dict | None) -> dict:
        p = pick.pair
        row = {
            "rank": pick.rank, "asset": p.asset, "long": p.long_leg.venue_name, "short": p.short_leg.venue_name,
            "long_type": p.long_leg.venue_type.value, "short_type": p.short_leg.venue_type.value,
            "types_verified": p.long_leg.venue_type_verified and p.short_leg.venue_type_verified,
            "score_apr": float(pick.score_apr), "now_apr": float(p.current_funding_edge * 8760),
            "steady": float(p.stats.positive_edge_ratio), "flips": p.stats.sign_flip_count,
            "min_volume_usd": float(min(p.long_leg.avg_24h_volume_usd, p.short_leg.avg_24h_volume_usd)),
            "asset_class": self._asset_class.get(p.asset, CRYPTO),
            "tradable": pick.tradable, "why_not": pick.why_not, "simulation": simulation,
            "replay_kind": "in_sample_diagnostic",
            "pair": p.model_dump(mode="json"), "params": self.params.model_dump(mode="json"),
            "forward": {"status": "pending" if pick.tradable else "unavailable", "reason": pick.why_not},
            "source": "postgres" if self.database_source else "dash",
            "batch_id": self.discovery.get("batch_id"),
            "spike_diagnostics": {
                "largest_positive_contribution": None if p.stats.largest_positive_contribution is None else float(p.stats.largest_positive_contribution),
                "top_three_positive_contribution": None if p.stats.top_three_positive_contribution is None else float(p.stats.top_three_positive_contribution),
                "mean_without_largest_apr": None if p.stats.mean_without_largest is None else float(p.stats.mean_without_largest * 8760)},
        }
        if self.database_source and p.asset in self._found:
            kept = self._found[p.asset][0]
            row["funding_evidence"] = {v: [o.model_dump(mode="json") for o in kept.histories.get(v, ())]
                                       for v in (p.long_leg.venue_name, p.short_leg.venue_name)}
        return row

    def _select(self, pairs: list[PairAnalytics], *, with_earnings: bool = True) -> tuple[list[Pick], list[Pick]]:
        """with_earnings=False is the walk-forward replay of an earlier week: today's earnings blackout and today's
        set-aside pairs did not exist then, so neither applies."""
        return select(pairs, self.params.selection_count, self.params.selection_metric, self._exec_source,
                      one_per_asset=self.params.selection_one_per_asset,
                      require_live_edge=self.params.selection_require_live_edge,
                      blocked=(lambda p: self._earnings_block.get(p.asset) or self._parked_reason(p))
                      if with_earnings else None,
                      missing_label=("not enabled for live trading:" if self.execution_venue else
                                     "excluded (EXCLUDE_VENUES) or no adapter yet for" if self.excluded_venues else
                                     "no adapter yet for"))

    def _parked_reason(self, pair: PairAnalytics) -> str | None:
        entry = self._parked.get(self._key(pair))
        if entry is None:
            return None
        until, failed = entry
        if self.clock.now() >= until:
            self._parked.pop(self._key(pair), None)
            return None
        return f"set aside until {until:%Y-%m-%d %H:%M} UTC: failed {failed}"

    def _park_if_permanent(self, pair: PairAnalytics, decision: Decision) -> bool:
        """True when the pair failed a check that will not clear within hours: it leaves the watch and the next
        selection, so its slot goes to the next best pair."""
        failed = sorted(c.name for c in decision.checks if not c.passed and c.name in PERMANENT_CHECKS)
        if not failed:
            return False
        self._parked[self._key(pair)] = (self.clock.now() + PARK_FOR, ", ".join(failed))
        self._forward_status(pair, "set_aside", f"set aside for {PARK_FOR.total_seconds() / 3600:.0f} h: "
                             + ", ".join(failed))
        log.info("set aside %s long %s / short %s: %s", pair.asset, pair.long_leg.venue_name,
                 pair.short_leg.venue_name, ", ".join(failed))
        return True

    def _walk_forward(self, found: dict) -> dict:
        """Run the SAME selection a week ago and replay the week that followed: did picking this way make money?

        "predicted" is the replay of the week the pair was picked on (what an in-sample replay promises);
        "realised" is the following week, which the selection could not see.
        """
        if self.database_source:
            return {"status": "prospective_only", "limitations": [
                "Today's shortlist is not a historical market-wide universe; retrospective selection is not reported.",
                "Original hourly summary batches and picks are frozen for week-later assessment."]}
        earlier = [p for _, _, prev in found.values() for p in prev]
        if not earlier:
            return {}
        overall, tradable = self._select(earlier, with_earnings=False)

        def rows(picks: list[Pick]) -> list[dict]:
            out = []
            for k in picks:
                kept = found[k.pair.asset][0]
                as_of = k.pair.window_end
                row = self._pick_row(k, None)
                row["picked_at"] = as_of.isoformat() if as_of else None
                row["predicted"] = self._simulate(k.pair, kept, as_of - WEEK, as_of) if as_of else None
                row["realised"] = self._simulate(k.pair, kept, as_of, kept.newest) if as_of else None
                row.pop("simulation", None)
                out.append(row)
            return out

        check = {"overall": rows(overall), "tradable": rows(tradable)}
        check["limitations"] = ["Today's volume and venue universe are used; historical volume is unavailable",
                                "Hourly funding/underlying-price approximation; no historical books or 1m entry gates",
                                "Historical earnings blackout is not reconstructed"]
        for kind in ("overall", "tradable"):
            done = [r for r in check[kind] if r["predicted"] and r["realised"]]
            check[f"{kind}_summary"] = {
                "picks": len(done),
                "predicted_net": sum(r["predicted"]["net"] for r in done),
                "realised_net": sum(r["realised"]["net"] for r in done),
                "profitable": sum(1 for r in done if r["realised"]["net"] > 0),
            }
        check["as_of"] = next((r["picked_at"] for r in check["overall"] + check["tradable"] if r["picked_at"]), None)
        return check

    async def _select_and_simulate(self, found: dict, total_pairs: int) -> tuple[list[Pick], list[Pick]]:
        """Choose the best pairs across every asset scanned, note their exchanges, and simulate each one."""
        await self._classify_and_check_earnings(found)
        all_pairs = [p for _, pairs, _ in found.values() for p in pairs]
        overall, tradable = self._select(all_pairs)
        earlier = [p for _, _, prev in found.values() for p in prev]
        wf_overall, wf_tradable = self._select(earlier, with_earnings=False) if earlier else ([], [])
        firsts: dict[str, PairAnalytics] = {}
        for k in overall + tradable + wf_overall + wf_tradable:
            firsts.setdefault(k.pair.asset, k.pair)
        self._bars = {}
        await self._load_bars(set(firsts), firsts)

        def rows(picks: list[Pick]) -> list[dict]:
            return [self._pick_row(k, self._simulate(k.pair, found[k.pair.asset][0])) for k in picks]

        rule_check = self._walk_forward(found)
        live_edge_dropped = sum(1 for p in rank(all_pairs, self.params.selection_metric) if p.current_funding_edge <= 0)
        selection = {"at": self.clock.now().isoformat(), "count": self.params.selection_count,
                      "source": "postgres" if self.database_source else "dash",
                      "discovery": dict(self.discovery),
                     "metric": self.params.selection_metric, "assets": list(found), "pairs_considered": total_pairs,
                     "one_per_asset": self.params.selection_one_per_asset,
                     "require_live_edge": self.params.selection_require_live_edge,
                     "dropped_edge_gone": live_edge_dropped,
                     "earnings_blocked": dict(self._earnings_block),
                     "notional_usd": float(self.params.target_notional_per_leg_usd),
                     "leverage": float(self.params.target_leverage),
                     "overall": rows(overall), "tradable": rows(tradable), "rule_check": rule_check}
        self.last_selection = selection
        self.repo.set_kv("last_selection", selection)
        from app.persistence import models as m
        now = self.clock.now()
        for kind in ("overall", "tradable"):
            for row in selection[kind]:
                self.repo.record(m.Selection, run_at=now, kind=kind, rank=row["rank"], asset=row["asset"],
                              long_venue=row["long"], short_venue=row["short"], score_apr=D(str(row["score_apr"])),
                              tradable=row["tradable"], why_not=(row["why_not"] or "")[:200] or None, detail=row)
        self.repo.set_kv("rule_check", {**rule_check, "at": now.isoformat()})
        keys = {(k.pair.asset, k.pair.long_leg.venue_name, k.pair.short_leg.venue_name) for k in tradable}
        if keys and keys != self._selected_keys:
            text = "; ".join(f"#{k.rank} {k.pair.asset} long {k.pair.long_leg.venue_name} / short "
                             f"{k.pair.short_leg.venue_name} {float(k.score_apr):.1f}% APR" for k in tradable)
            await self.notifier.alert("pairs_selected", f"selected to simulate: {text}")
        self._selected_keys = keys
        return overall, tradable

    # ---- step 4: verify live and (paper) trade only what was selected --------------------------
    def _record(self, pair: PairAnalytics, decision: Decision, report: ScanReport | None) -> None:
        if report is not None:
            report.verified += 1
            report.outcomes[decision.outcome.value] = report.outcomes.get(decision.outcome.value, 0) + 1
        self.repo.add_decision(asset=pair.asset, long_venue=pair.long_leg.venue_name,
                               short_venue=pair.short_leg.venue_name, outcome=decision.outcome.value,
                               checks=[c.__dict__ for c in decision.checks], metrics=decision.metrics)

    @staticmethod
    def _key(pair: PairAnalytics) -> tuple:
        return pair.asset, pair.long_leg.venue_name, pair.short_leg.venue_name

    async def _verify_and_enter(self, picks: list[Pick], found: dict, reports: dict[str, ScanReport]) -> None:
        async with self._enter_lock:
            busy = {(t.asset, t.long_venue, t.short_venue) for t in self.repo.unfinished_trades()}
            opened_assets: set[str] = set()
            waiting: list[Pick] = []
            for pick in picks:
                pair = pick.pair
                report = reports[pair.asset]
                if self._key(pair) in busy or pair.asset in opened_assets or any(a == pair.asset for a, _, _ in busy):
                    self._forward_status(pair, "active", "an existing trial already covers this asset")
                    continue
                at = self._provider_time(found[pair.asset][0])
                age = (self.clock.now() - at).total_seconds() if at else None
                decision = await self.verify_pair(pair, zd_age_seconds=age)
                if decision is None:
                    self._forward_status(pair, "waiting", "direct venue data unavailable")
                    waiting.append(pick)
                    continue
                self._record(pair, decision, report)
                self._watch_seen[self._key(pair)] = (decision.outcome, tuple(decision.reasons))
                await self._announce(pair, decision)
                if self._park_if_permanent(pair, decision):
                    continue  # not watched: it cannot pass, and the next sweep gives its slot to another pair
                if await self._admit(pair, decision, report, found[pair.asset][0]):
                    opened_assets.add(pair.asset)  # one new trade per asset per scan
                else:
                    waiting.append(pick)
            self.watch = waiting

    async def recheck_watch(self) -> int:
        """Re-verify the selected pairs that are waiting (every minute, on fresh direct-venue data).

        Returns how many trades were opened. A decision is stored only when its outcome or reasons change, so the
        audit trail shows every transition without a row per minute.
        """
        if not self.watch or not self.engine.kill.entries_allowed or self.engine.blocked():
            return 0
        opened = 0
        async with self._enter_lock:
            busy = {(t.asset, t.long_venue, t.short_venue) for t in self.repo.unfinished_trades()}
            still: list[Pick] = []
            for pick in self.watch:
                pair = pick.pair
                if self._key(pair) in busy or any(a == pair.asset for a, _, _ in busy):
                    continue
                kept = self._found.get(pair.asset, (None,))[0]
                at = self._provider_time(kept) if kept else None
                age = (self.clock.now() - at).total_seconds() if at else None
                decision = await self.verify_pair(pair, zd_age_seconds=age)
                if decision is None:
                    self._forward_status(pair, "waiting", "direct venue data unavailable")
                    still.append(pick)
                    continue
                seen = (decision.outcome, tuple(decision.reasons))
                if self._watch_seen.get(self._key(pair)) != seen:
                    self._watch_seen[self._key(pair)] = seen
                    self._record(pair, decision, None)
                    await self._announce(pair, decision)
                if self._park_if_permanent(pair, decision):
                    continue
                report = self.last_report.get(pair.asset) or ScanReport(pair.asset)
                if kept is not None and await self._admit(pair, decision, report, kept):
                    opened += 1
                    busy.add(self._key(pair))
                    continue
                still.append(pick)
            self.watch = still
        return opened

    async def scan_all(self, assets: list[str]) -> list[ScanReport]:
        """The whole plan over `assets`: identify every pair, select the best, simulate them, verify and enter.

        Coins are fetched a couple at a time with a pause after each request. An auth or rate-limit answer aborts the
        sweep at once (nothing is retried or worked around); an isolated bad coin is skipped, and many errors in a row
        abort it as an outage.
        """
        if self.database_source:
            return await self._scan_postgres(assets)
        started = self.clock.now()
        self._due_selections = self.repo.due_selections()
        self.progress = {"running": True, "done": 0, "total": len(assets), "started_at": started.isoformat(),
                         "finished_at": None}
        sem = asyncio.Semaphore(max(1, self.params.sweep_concurrency))
        delay = float(self.params.sweep_delay_seconds)
        found: dict[str, tuple[_Kept, list[PairAnalytics], list[PairAnalytics]]] = {}
        reports: dict[str, ScanReport] = {}
        problems: Counter = Counter()
        first_detail: dict[str, str] = {}
        state = {"abort": None, "in_a_row": 0}

        async def one(asset: str) -> None:
            async with sem:
                if state["abort"] or self.stopping:
                    return
                report, kept, pairs, earlier, failure = await self._discover(asset)
                await self.clock.sleep(delay)
            reports[asset] = report
            self.progress["done"] += 1
            if failure is None:
                found[asset] = (kept, pairs, earlier)
                state["in_a_row"] = 0
                return
            reason, detail = failure
            problems[reason] += 1
            first_detail.setdefault(reason, detail)
            state["in_a_row"] = state["in_a_row"] + 1 if reason == "error" else state["in_a_row"]
            if reason in ("auth", "rate_limit") or state["in_a_row"] >= MAX_CONSECUTIVE_ERRORS:
                state["abort"] = reason if reason in ("auth", "rate_limit") else "outage"

        await asyncio.gather(*(one(a) for a in assets))
        if self.stopping:  # a half-fetched sweep is neither an outage nor a basis for trades
            self.progress = {**self.progress, "running": False, "finished_at": self.clock.now().isoformat()}
            return [reports[a] for a in assets if a in reports]

        attempted, ok = len(reports), len(found)
        if state["abort"] == "auth":
            await self._zd_failure("zero_delta_auth_failure", first_detail["auth"])
        elif state["abort"] == "rate_limit":
            await self._zd_failure("zero_delta_outage", "rate limited by Zero Delta: " + first_detail["rate_limit"])
        elif state["abort"] == "outage":
            await self._zd_failure("zero_delta_outage", first_detail.get("error", "repeated errors"))
        elif attempted and ok == 0:
            kind = "zero_delta_stale_data" if problems.get("stale") == attempted else "zero_delta_outage"
            await self._zd_failure(kind, first_detail.get("stale") or first_detail.get("error") or "no data")
        elif ok:
            self._zd_alerted = None
            self.repo.add_provider_event("OK", f"sweep: {ok} of {attempted} coins scanned")
            if self.health:
                self.health.mark("zero_delta", True, f"last sweep ok ({ok} coins)",
                                 stale_after=scan_stale_seconds(self.params))

        total_pairs = sum(r.pairs_found for r in reports.values())
        if found:
            self._found = found
            _, tradable = await self._select_and_simulate(found, total_pairs)
            await self._score_saved_picks(found)
            if state["abort"] is None:
                await self._verify_and_enter(tradable, found, reports)
            else:
                self.watch = []
        else:
            self.watch = []  # stale picks must not survive a provider outage
            self._found = {}

        for r in reports.values():
            self.last_report[r.asset] = r
        top = [row for r in reports.values() for row in r.top_pairs]
        self.last_top_pairs = sorted(top, key=lambda row: -row["mean_apr"])[:60]
        finished = self.clock.now()
        check = (self.last_selection or {}).get("rule_check") or {}
        self.sweep = {
            "assets_total": len(assets), "assets_scanned": ok, "assets_failed": attempted - ok,
            "assets_skipped": len(assets) - attempted, "aborted": state["abort"],
            "assets_with_pairs": sum(1 for r in reports.values() if r.pairs_found),
            "pairs_total": total_pairs, "tradable_pairs": sum(r.executable_pairs for r in reports.values()),
            "verified": sum(r.verified for r in reports.values()),
            "passed": sum(r.outcomes.get("ENTER", 0) for r in reports.values()),
            "opened": sum(len(r.opened) for r in reports.values()),
            "shadow_opened": sum(len(r.shadow_opened) for r in reports.values()),
            "watching": len(self.watch),
            "rule_check": {k: check.get(k) for k in ("overall_summary", "tradable_summary", "as_of")},
            "started_at": started.isoformat(), "finished_at": finished.isoformat(),
            "duration_seconds": (finished - started).total_seconds(),
        }
        self.progress = {**self.progress, "running": False, "finished_at": finished.isoformat()}
        if self.health and ok:
            self.health.mark("scanner", True, f"{ok} coins, {total_pairs} pairs, {self.sweep['opened']} opened",
                             stale_after=scan_stale_seconds(self.params))
        try:
            self.repo.prune_discovery()
        except Exception:  # noqa: BLE001 - housekeeping must never break a scan
            log.exception("could not prune old discovery rows")
        return [reports[a] for a in assets if a in reports]

    def _provider_time(self, kept: _Kept) -> datetime | None:
        return (kept.source_time or kept.received_at) if self.database_source else kept.received_at

    async def _scan_postgres(self, assets: list[str]) -> list[ScanReport]:
        """Summary-first discovery; only shortlist markets and due frozen picks reach the raw table."""
        started = self.clock.now()
        self._due_selections = self.repo.due_selections()
        self.progress = {"running": True, "done": 0, "total": 0, "started_at": started.isoformat()}
        found, reports = {}, {}
        try:
            if self.zd is None:
                raise ZeroDeltaError("Zero Delta database is not configured")
            batch = await self.zd.refresh_summary()
            self.repo.save_summary_batch(batch.model_dump(mode="json"))
            pool = screen_summary(batch, None if "ALL" in assets else set(assets), self._excluded_assets,
                self.zd_settings.min_avg_24h_volume_usd, self.zd_settings.research_pair_limit, self._exec_source)
            self._db_markets, self._db_symbols = {}, {}
            self._db_candidate_assets = {p["asset"] for p in pool}
            for p in pool:
                self._db_markets.setdefault(p["asset"], set()).update((p["long_exchange"], p["short_exchange"]))
            for saved in self._due_selections:
                for role in ("long", "short"):
                    leg = (saved.detail.get("pair") or {}).get(role + "_leg") or {}
                    exchange = leg.get("exchange_key")
                    if exchange is None:
                        matches = [m.exchange for m in batch.markets if m.coin == saved.asset
                                   and m.venue_name == getattr(saved, role + "_venue")]
                        exchange = matches[0] if len(matches) == 1 else None
                    if exchange and leg.get("market_identifier"):
                        self._db_markets.setdefault(saved.asset, set()).add(exchange)
                        self._db_symbols.setdefault(saved.asset, {})[exchange] = leg["market_identifier"]
            self.discovery = {"source": "postgres", "batch_id": batch.id, "as_of": batch.as_of.isoformat(),
                "fetched_at": batch.fetched_at.isoformat(), "summary_rows": len(batch.markets),
                "summary_refresh_verified": batch.refresh_verified,
                "scope": "best among history-validated shortlist; summary ranks are provisional",
                "research_pair_limit": self.zd_settings.research_pair_limit, "screening": pool,
                "summary_issues": [i.model_dump(mode="json") for i in batch.issues]}
            self.progress["total"] = len(self._db_markets)
            sem = asyncio.Semaphore(self.zd_settings.db_concurrency)

            async def one(asset):
                async with sem:
                    if self.stopping:
                        return
                    report, kept, pairs, earlier, failure = await self._discover(asset)
                reports[asset] = report
                self.progress["done"] += 1
                if failure is None:
                    found[asset] = (kept, pairs, earlier)
                elif failure[0] in ("error", "access", "auth"):
                    raise ZeroDeltaError(failure[1])

            # All requests finish/cancel before this batch is published or rejected.
            async with asyncio.TaskGroup() as tasks:
                for asset in self._db_markets:
                    tasks.create_task(one(asset))
            if self.stopping:
                return list(reports.values())
            validated = [p for _, pairs, _ in found.values() for p in pairs]
            self.discovery["validated_pairs"] = len(validated)
            timeouts = sum("QUERY_TIMEOUT" in m["reasons"] for r in reports.values() for m in r.market_validation)
            attempted = sum(len(r.market_validation) for r in reports.values())
            self.discovery["market_query_timeouts"] = timeouts
            for screened in pool:
                matches = [p for p in validated if p.asset == screened["asset"] and
                    {p.long_leg.venue_name, p.short_leg.venue_name} == {screened["long"], screened["short"]}]
                screened["history_status"] = "validated" if matches else "insufficient_or_invalid"
                report = reports.get(screened["asset"])
                screened["validation"] = [m for m in report.market_validation
                    if m["venue"] in {screened["long"], screened["short"]}] if report else []
            viable = any(p.asset in self._db_candidate_assets for p in validated)
            if timeouts and (not viable or (timeouts >= MAX_CONSECUTIVE_ERRORS and timeouts * 2 >= attempted)):
                await self._zd_failure("zero_delta_outage", f"{timeouts}/{attempted} selected market history queries timed out")
            elif pool and not viable:
                await self._zd_failure("zero_delta_stale_data", "No screened pair has complete current hourly coverage")
            else:
                self._zd_alerted = None
                if self.health:
                    detail = (f"partial batch: {timeouts} market query timeout(s); validated markets remain usable" if timeouts
                              else "summary read; selected raw-market coverage checked; upstream summary refresh marker unavailable")
                    self.health.mark("zero_delta", not timeouts, detail, stale_after=scan_stale_seconds(self.params))
            self._found = found
            total = sum(r.pairs_found for r in reports.values())
            await self._select_and_simulate(found, total)
            await self._score_saved_picks(found)
            if self._zd_alerted is None:
                # A permanently incompatible pick does not waste a slot until the next hour.
                for attempt in range(len(pool) + 1):
                    if self.stopping:
                        break
                    parked_before = len(self._parked)
                    pairs = [p for _, ps, _ in found.values() for p in ps]
                    _, picks = self._select(pairs)
                    await self._verify_and_enter(picks, found, reports)
                    if len(self._parked) == parked_before:
                        break
                    await self._select_and_simulate(found, total)
            self.last_report = reports
            self.last_top_pairs = sorted([row for r in reports.values() for row in r.top_pairs],
                                         key=lambda r: -r["mean_apr"])[:60]
            self.sweep = {"source": "postgres", "assets_total": len({m.coin for m in batch.markets}),
                "assets_scanned": len(found), "assets_failed": sum(not r.zero_delta_ok for r in reports.values()),
                "assets_skipped": len({m.coin for m in batch.markets}) - len(reports),
                "pairs_total": total, "tradable_pairs": sum(r.executable_pairs for r in reports.values()),
                "assets_with_pairs": sum(bool(r.pairs_found) for r in reports.values()),
                "passed": sum(r.outcomes.get("ENTER", 0) for r in reports.values()),
                "verified": sum(r.verified for r in reports.values()),
                "opened": sum(len(r.opened) for r in reports.values()),
                "shadow_opened": sum(len(r.shadow_opened) for r in reports.values()),
                "watching": len(self.watch), "aborted": self._zd_alerted, "market_query_timeouts": timeouts,
                "partial": bool(timeouts), "discovery": dict(self.discovery),
                "started_at": started.isoformat(), "finished_at": self.clock.now().isoformat(),
                "duration_seconds": (self.clock.now() - started).total_seconds()}
            if self.health:
                self.health.mark("scanner", self._zd_alerted is None and not timeouts,
                    f"{len(pool)} screened; {len(validated)} shortlisted pairs validated"
                    + (f"; partial: {timeouts} market query timeout(s)" if timeouts else ""),
                    stale_after=scan_stale_seconds(self.params))
            try:
                self.repo.prune_discovery()
            except Exception:  # housekeeping must never interrupt the independent trade engine
                log.exception("could not prune old database discovery rows")
        except (ZeroDeltaError, ExceptionGroup) as exc:
            # ExceptionGroup comes from TaskGroup. Provider exceptions are already scrubbed by the transport.
            self._found, self.watch = {}, []
            self.discovery["provider_status"] = "unavailable"
            await self._zd_failure("zero_delta_outage", _failure_text(exc)[:200])
            self.sweep = {"source": "postgres", "aborted": "outage", "opened": 0,
                          "started_at": started.isoformat(), "finished_at": self.clock.now().isoformat()}
            if self.health:
                self.health.mark("scanner", False, "database discovery failed", stale_after=scan_stale_seconds(self.params))
        finally:
            self.progress.update(running=False, finished_at=self.clock.now().isoformat())
        return list(reports.values())

    async def scan_asset(self, asset: str) -> ScanReport:
        reports = await self.scan_all([asset])
        return reports[0] if reports else ScanReport(asset, zero_delta_ok=False, note="not scanned")

    @staticmethod
    def _pair_row(p: PairAnalytics) -> dict:
        """Plain numbers for the dashboard. Edges are shown as APR % (per-hour percent x 8760)."""
        apr = D(8760)
        st = p.stats
        return {"long": p.long_leg.venue_name, "short": p.short_leg.venue_name, "type": p.pair_type,
                "mean_apr": float(st.mean * apr), "median_apr": float(st.median * apr),
                "now_apr": float(p.current_funding_edge * apr), "stddev_apr": float(st.stddev * apr),
                 "positive_ratio": float(st.positive_edge_ratio), "sign_flips": st.sign_flip_count,
                 "largest_positive_contribution": None if st.largest_positive_contribution is None else float(st.largest_positive_contribution),
                 "top_three_positive_contribution": None if st.top_three_positive_contribution is None else float(st.top_three_positive_contribution),
                 "mean_without_largest_apr": None if st.mean_without_largest is None else float(st.mean_without_largest * apr),
                "hours": p.coverage.coverage_hours, "executable": False,
                "min_volume_usd": float(min(p.long_leg.avg_24h_volume_usd, p.short_leg.avg_24h_volume_usd))}

    async def _announce(self, pair: PairAnalytics, decision: Decision) -> None:
        label = f"{pair.asset} long {pair.long_leg.venue_name} / short {pair.short_leg.venue_name}"
        if decision.outcome is Outcome.ENTER:
            await self.notifier.alert("candidate_found", f"{label}: all gates passed, "
                                      f"net edge {decision.metrics.get('expected_net_edge_bps')} bps")
        elif decision.outcome is Outcome.MISMATCH:
            await self.notifier.alert("direct_venue_mismatch",
                                      f"ZERO_DELTA_EXCHANGE_MISMATCH {label}: {', '.join(decision.reasons)}",
                                      severity="warning")
        elif decision.outcome is Outcome.WAIT and "low_vol_score" in decision.reasons:
            await self.notifier.alert("waiting_for_low_volatility", f"{label}: volatility above the limit")
        elif decision.outcome is Outcome.REJECT:
            log.info("candidate rejected %s: %s", label, ", ".join(decision.reasons))

    async def _try_open(self, pair: PairAnalytics, decision: Decision, report: ScanReport,
                        analysis, *, shadow: bool = False) -> bool:
        if not self.engine.kill.entries_allowed or self.engine.blocked():
            return False
        ok, why = (True, "") if shadow else self.engine.portfolio_room(pair)
        if not ok:
            report.note = f"limit reached: {why}"
            return False
        try:
            # asset_class travels with the trade: OKX, BloFin and MEXC do not label stock contracts, so the engine's
            # per-slice earnings check needs the sweep's classification (from the venues that do label them).
            trade = await self.engine.open_trade(pair, {"decision": {
                "outcome": decision.outcome.value, "metrics": decision.metrics,
                "checks": [c.__dict__ for c in decision.checks]},
                "asset_class": self._asset_class.get(pair.asset, CRYPTO)}, shadow=shadow)
        except (RuntimeError, ExchangeError, UnsupportedVenue, *DB_OUTAGE_ERRORS) as exc:
            # a trade that cannot be recorded is not opened (DB_OUTAGE_ERRORS: the database is locked or down)
            report.note = f"not opened: {type(exc).__name__}: {exc}"[:300]
            # A plain RuntimeError is the engine's deliberate refusal (trial capacity, a portfolio limit, the kill
            # switch): routine and repeated every scan, so INFO. Anything else is a failure worth a warning.
            log.log(logging.INFO if type(exc) is RuntimeError else logging.WARNING, "could not open trade: %s", exc)
            return False
        (report.shadow_opened if shadow else report.opened).append(trade.id)
        self._forward_status(pair, "shadow" if shadow else "paper", trade_id=trade.id)
        from app.persistence import models as m  # audit trail: what Zero Delta reported for this trade (section 43)
        for venue in (pair.long_leg.venue_name, pair.short_leg.venue_name):
            for obs in analysis.histories.get(venue, ()):
                self.repo.record(m.ZeroDeltaObservation, trade_id=trade.id, venue_name=venue, timestamp=obs.timestamp,
                              funding_apr=obs.funding_apr,
                              normalized_funding_rate_per_hour=obs.normalized_funding_rate_per_hour)
        self.repo.add_decision(asset=pair.asset, long_venue=pair.long_leg.venue_name,
                                short_venue=pair.short_leg.venue_name, outcome="SHADOW_ENTERED" if shadow else "ENTERED",
                               checks=[], metrics=decision.metrics, trade_id=trade.id)
        return True

    def _forward_status(self, pair, status, reason=None, trade_id=None) -> None:
        value = {"status": status, "reason": reason, "trade_id": trade_id}
        for kind in ("overall", "tradable"):
            for row in self.last_selection.get(kind, []):
                if (row["asset"], row["long"], row["short"]) == self._key(pair):
                    row["forward"] = value
        self.repo.set_kv("last_selection", self.last_selection)

    async def _admit(self, pair, decision, report, kept) -> bool:
        if decision.outcome is Outcome.ENTER and await self._try_open(pair, decision, report, kept):
            return True
        # The research trial drops ONLY the discretionary net-edge/portfolio gates. Market safety and Alex's
        # spread, volatility, earnings, sizing and paired-execution rules still apply through the same engine.
        failed = [c for c in decision.checks if not c.passed and c.blocking != "info" and c.name != "expected_net_edge"]
        if self.params.shadow_paper_enabled and self.params.run_mode == "paper" and not failed:
            trial = Decision(Outcome.ENTER,
                             [replace(c, blocking="info") if c.name == "expected_net_edge" else c for c in decision.checks],
                             decision.metrics)
            if await self._try_open(pair, trial, report, kept, shadow=True):
                return True
        self._forward_status(pair, "waiting", ", ".join(filter(None, [", ".join(decision.reasons), report.note])))
        return False

    async def _score_saved_picks(self, found: dict) -> None:
        """Freeze a week-later result against the ORIGINAL direction and rules, never reselect retrospectively."""
        firsts = {}
        for row in self._due_selections:
            if row.asset in found and row.detail.get("pair"):
                firsts.setdefault(row.asset, PairAnalytics.model_validate(row.detail["pair"]))
        await self._load_bars(set(firsts), firsts)
        from app.persistence.models import as_utc
        for row in self._due_selections:
            start = as_utc(row.run_at).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            end = start + WEEK
            if row.asset not in found or not row.detail.get("pair"):
                if self.clock.now() - start > timedelta(days=HISTORY_KEEP_DAYS):
                    self.repo.score_selection(row.id, {"status": "unavailable", "reason": "asset/history no longer available"})
                continue
            kept = found[row.asset][0]
            if kept.newest is None or kept.newest < end:
                continue
            pair = PairAnalytics.model_validate(row.detail["pair"])
            result = self._simulate(pair, kept, start, end, ResolvedParams.model_validate(row.detail["params"]))
            if result is not None:
                self.repo.score_selection(row.id, {"status": "scored", "scored_at": self.clock.now().isoformat(),
                                                   "result": result, "kind": "out_of_sample_hourly_estimate"})
            elif self.clock.now() - start > timedelta(days=HISTORY_KEEP_DAYS):
                self.repo.score_selection(row.id, {"status": "unavailable", "reason": "history window expired or incomplete"})

    def restore_watch(self) -> None:
        self.watch = []
        self._found = {}
        saved = self.repo.latest_selection()
        self.last_selection = saved
        if self.database_source:
            return  # revalidate a database batch before admitting a restored pick
        for row in saved.get("tradable", []):
            if not row.get("pair"):
                continue
            pair = PairAnalytics.model_validate(row["pair"])
            at = datetime.fromisoformat(saved["at"])
            if (self.clock.now() - at).total_seconds() > self.params.zero_delta_max_age_seconds:
                continue
            self.watch.append(Pick(row["rank"], pair, D(str(row["score_apr"])), True, None))
            self._found[pair.asset] = (_Kept(pair.asset, {}, received_at=at), [], [])
