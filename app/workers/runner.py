"""Run loops. Two independent tasks so scanning can never starve risk management (section 54):

  engine loop   monitors and drives every open trade; fast during exits, slower while just holding
  scan loop     looks for new opportunities; paused by the kill switch, a manual-intervention block or an outage

Neither loop can die from an exception: failures are logged, alerted (rate limited) and retried.
"""

import asyncio
import logging
import time

from app.core.clock import Clock
from app.core.health import Health
from app.core.trading_config import ResolvedParams, TradingSettings
from app.domain.trade import TradeState
from app.execution.engine import TradeEngine
from app.notifications.notifier import Notifier
from app.persistence.repository import Repository
from app.workers.scanner import Scanner, scan_stale_seconds

log = logging.getLogger(__name__)
ERROR_ALERT_EVERY_SECONDS = 300
# The engine loop reports every 1-15 s; three minutes of silence means it is stuck, not slow.
ENGINE_STALE_SECONDS = 180.0
# Live mode: every venue's readiness check is repeated this often, so a venue the user fixes becomes usable and a venue
# that breaks stops opening trades, without a restart.
LIVE_READINESS_EVERY_SECONDS = 1800

# Trade states that mean "not healthy right now": the engine health turns red and names the trade.
UNHEALTHY_TRADE_STATES = frozenset({TradeState.DEGRADED, TradeState.RECONCILING, TradeState.ERROR,
                                    TradeState.MANUAL_INTERVENTION_REQUIRED})


def _brief(exc: BaseException) -> str:
    """Type and first line of an error, for health and alerts (the senior could not see what "OperationalError" was).

    SQLAlchemy appends the whole statement and its parameters after " [SQL:"; that part is dropped.
    """
    text = str(exc).splitlines()[0] if str(exc) else ""
    text = text.split(" [SQL:")[0].strip()
    return (f"{type(exc).__name__}: {text}" if text else type(exc).__name__)[:160]


class Runner:
    def __init__(self, *, settings: TradingSettings, params: ResolvedParams, repo: Repository, engine: TradeEngine,
                 scanner: Scanner, notifier: Notifier, health: Health, clock: Clock):
        self.settings, self.params, self.repo = settings, params, repo
        self.engine, self.scanner, self.notifier, self.health, self.clock = engine, scanner, notifier, health, clock
        self._stop = asyncio.Event()
        self._last_error_alert = 0.0
        self._readiness_at = 0.0
        self.loop_metrics: dict[str, dict] = {}

    def stop(self) -> None:
        self._stop.set()
        if self.scanner is not None:
            self.scanner.stopping = True  # a sweep in progress ends early instead of holding up the shutdown

    async def startup(self) -> list[str]:
        """Recover unfinished trades before anything else. Returns trades that need a human."""
        provenance = {k: v for k, v in self.params.provenance.items() if v == "paper_default"}
        live = self.params.run_mode == "live"
        await self.notifier.alert(
            "startup", f"mode={self.params.run_mode}, {'LIVE TRADING ENABLED: real orders' if live else 'live disabled'}, "
                       f"assets={self.settings.scan_assets}, {len(provenance)} parameters use paper defaults",
            severity="warning" if live else "info")
        if self._live:
            await self.check_live_venues()  # first: it also measures each venue's clock for the signed requests
        manual = await self.engine.recover()
        self.scanner.restore_watch()
        self.mark_database_health()
        self.health.mark("engine", not manual, "manual intervention needed" if manual else "recovered",
                         stale_after=ENGINE_STALE_SECONDS)
        return manual

    @property
    def _live(self) -> bool:
        from app.execution.venue_provider import LiveVenueProvider
        return isinstance(self.engine.provider, LiveVenueProvider)

    async def check_live_venues(self) -> set[str]:
        """Run every live venue's read-only readiness check. Only venues that pass may open NEW trades; with fewer
        than two, new trades are blocked (open trades are always managed and exited)."""
        provider = self.engine.provider
        names = list(provider.clients)
        results = await asyncio.gather(*(provider.clients[n].readiness() for n in names), return_exceptions=True)
        ready, report = set(), {}
        for name, result in zip(names, results):
            if isinstance(result, BaseException):
                self.health.mark(f"live:{name}", False, _brief(result), stale_after=2 * LIVE_READINESS_EVERY_SECONDS)
                report[name] = {"ok": False, "checks": [{"name": "readiness", "ok": False, "detail": _brief(result)}]}
                continue
            failing = [c for c in result.checks if not c.ok]
            self.health.mark(f"live:{name}", result.ok,
                             "ready for live orders" if result.ok else "; ".join(f"{c.name}: {c.detail}" for c in failing),
                             stale_after=2 * LIVE_READINESS_EVERY_SECONDS)
            report[name] = {"ok": result.ok, "checks": [c.__dict__ for c in result.checks]}
            if result.ok:
                ready.add(name)
        changed = ready != provider.ready
        provider.ready = ready
        self.engine.entry_block = None if len(ready) >= 2 else (
            f"live: {len(ready)} venue(s) passed the readiness check, two are needed (see /health live:*)")
        self.repo.set_kv("live_readiness", {"at": self.clock.now().isoformat(), "venues": report})
        self._readiness_at = time.monotonic()
        if changed or self.engine.entry_block:
            await self.notifier.alert(
                "startup", f"live venues ready: {sorted(ready) or 'none'}; not ready: "
                           f"{sorted(set(names) - ready) or 'none'}", severity="warning" if self.engine.entry_block else "info")
        return ready

    async def _guard(self, name: str, coro) -> bool:
        """Run one iteration. True when it completed; False (health red, alert rate limited) when it raised."""
        started = time.monotonic()
        try:
            await coro
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop must survive
            log.exception("%s iteration failed", name)
            self.health.mark(name, False, _brief(exc), stale_after=self._stale_after(name))
            if time.monotonic() - self._last_error_alert > ERROR_ALERT_EVERY_SECONDS:
                self._last_error_alert = time.monotonic()
                await self.notifier.alert("critical_error", f"{name} failed: {_brief(exc)}", severity="critical")
            return False
        finally:
            ms = (time.monotonic() - started) * 1000
            previous = self.loop_metrics.get(name, {})
            self.loop_metrics[name] = {"iterations": previous.get("iterations", 0) + 1,
                                      "last_duration_ms": round(ms, 1),
                                      "max_duration_ms": round(max(ms, previous.get("max_duration_ms", 0)), 1)}

    def mark_engine_health(self) -> None:
        """Green only when the loop completed AND every active trade is in a normal state.

        Senior review 2026-09-30: the engine was marked green straight after a failed iteration, and trades that were
        DEGRADED or in ERROR never showed. A green engine must mean every active trade is healthy.
        """
        active = self.engine.active.values()
        troubled = [f"{t.id} {self.engine.state_of(t).value}" for t in active
                    if self.engine.state_of(t) in UNHEALTHY_TRADE_STATES]
        troubled += [f"{trade_id} not rebuilt after the restart" for trade_id in self.engine.recovery_pending]
        detail = f"{len(self.engine.active)} active trades"
        if troubled:
            self.health.mark("engine", False, detail + "; not healthy: " + ", ".join(troubled),
                             stale_after=ENGINE_STALE_SECONDS)
        else:
            self.health.mark("engine", True, detail, stale_after=ENGINE_STALE_SECONDS)

    def mark_database_health(self) -> None:
        """The database as the repository sees it right now (it was marked "ok" once at startup and never again)."""
        if self.repo.db_ok:
            self.health.mark("database", True, "ok", stale_after=ENGINE_STALE_SECONDS)
        else:
            self.health.mark("database", False, f"unavailable: {self.repo.backlog_size} writes buffered, trading "
                             "continues from memory", stale_after=ENGINE_STALE_SECONDS)

    def _stale_after(self, name: str) -> float:
        """How long each loop may stay silent before its health turns red (a hung loop must not stay green)."""
        if name == "watch":
            return 10 * self.params.watch_interval_seconds
        if name == "scanner":
            return scan_stale_seconds(self.params)
        return ENGINE_STALE_SECONDS

    async def engine_loop(self) -> None:
        while not self._stop.is_set():
            if await self._guard("engine", self.engine.run_once()):
                self.mark_engine_health()
            self.mark_database_health()
            await self._sleep(self.engine.recommended_delay())

    async def scan_loop(self) -> None:
        database_retries = 0
        while not self._stop.is_set():
            if self._live and time.monotonic() - self._readiness_at > LIVE_READINESS_EVERY_SECONDS:
                await self._guard("live readiness", self.check_live_venues())
            if self.scanner.database_source or (self.engine.kill.entries_allowed and not self.engine.blocked()):
                assets = await self.scanner.resolve_assets(self.settings.scan_assets, self.settings.exclude_assets)
                await self._guard("scanner", self.scanner.scan_all(assets))
            else:  # paused on purpose: say so, rather than let the last sweep's report go stale and red
                self.health.mark("scanner", True, "not scanning: new entries are paused or blocked",
                                 stale_after=scan_stale_seconds(self.params))
            if self.scanner.database_source:
                from app.providers.zerodelta.postgres import next_refresh
                delay = (next_refresh(self.clock.now(), self.scanner.zd_settings.refresh_minute)
                         - self.clock.now()).total_seconds()
                database_retries = database_retries + 1 if (self.scanner._zd_alerted or self.scanner.sweep.get("partial")) else 0
                if 0 < database_retries <= self.scanner.zd_settings.max_retries:
                    delay = min(delay, 60)
                await self._sleep(delay)
            else:
                await self._sleep(self.params.scan_interval_seconds)

    async def watch_loop(self) -> None:
        while not self._stop.is_set():
            if await self._guard("watch", self.scanner.recheck_watch()):
                # without this, one failed re-check left the watch (and overall health) red forever
                self.health.mark("watch", True, f"{len(self.scanner.watch)} selected pairs waiting",
                                 stale_after=self._stale_after("watch"))
            await self._sleep(self.params.watch_interval_seconds)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def run(self) -> None:
        await self.startup()
        await asyncio.gather(self.engine_loop(), self.scan_loop(), self.watch_loop())
