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
from app.workers.scanner import Scanner

log = logging.getLogger(__name__)
ERROR_ALERT_EVERY_SECONDS = 300
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

    def stop(self) -> None:
        self._stop.set()

    async def startup(self) -> list[str]:
        """Recover unfinished trades before anything else. Returns trades that need a human."""
        provenance = {k: v for k, v in self.params.provenance.items() if v == "paper_default"}
        await self.notifier.alert(
            "startup", f"mode={self.params.run_mode}, live disabled, assets={self.settings.scan_assets}, "
                       f"{len(provenance)} parameters use paper defaults")
        manual = await self.engine.recover()
        self.scanner.restore_watch()
        self.health.mark("database", True, "ok")
        self.health.mark("engine", not manual, "manual intervention needed" if manual else "recovered")
        return manual

    async def _guard(self, name: str, coro) -> bool:
        """Run one iteration. True when it completed; False (health red, alert rate limited) when it raised."""
        try:
            await coro
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop must survive
            log.exception("%s iteration failed", name)
            self.health.mark(name, False, _brief(exc))
            if time.monotonic() - self._last_error_alert > ERROR_ALERT_EVERY_SECONDS:
                self._last_error_alert = time.monotonic()
                await self.notifier.alert("critical_error", f"{name} failed: {_brief(exc)}", severity="critical")
            return False

    def mark_engine_health(self) -> None:
        """Green only when the loop completed AND every active trade is in a normal state.

        Senior review 2026-09-30: the engine was marked green straight after a failed iteration, and trades that were
        DEGRADED or in ERROR never showed. A green engine must mean every active trade is healthy.
        """
        active = self.engine.active.values()
        troubled = [f"{t.id} {self.engine.state_of(t).value}" for t in active
                    if self.engine.state_of(t) in UNHEALTHY_TRADE_STATES]
        detail = f"{len(self.engine.active)} active trades"
        if troubled:
            self.health.mark("engine", False, detail + "; not healthy: " + ", ".join(troubled))
        else:
            self.health.mark("engine", True, detail)

    async def engine_loop(self) -> None:
        while not self._stop.is_set():
            if await self._guard("engine", self.engine.run_once()):
                self.mark_engine_health()
            await self._sleep(self.engine.recommended_delay())

    async def scan_loop(self) -> None:
        while not self._stop.is_set():
            if self.engine.kill.entries_allowed and not self.engine.blocked():
                assets = await self.scanner.resolve_assets(self.settings.scan_assets, self.settings.exclude_assets)
                await self._guard("scanner", self.scanner.scan_all(assets))
            await self._sleep(self.params.scan_interval_seconds)

    async def watch_loop(self) -> None:
        while not self._stop.is_set():
            if await self._guard("watch", self.scanner.recheck_watch()):
                # without this, one failed re-check left the watch (and overall health) red forever
                self.health.mark("watch", True, f"{len(self.scanner.watch)} selected pairs waiting")
            await self._sleep(self.params.watch_interval_seconds)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def run(self) -> None:
        await self.startup()
        await asyncio.gather(self.engine_loop(), self.scan_loop(), self.watch_loop())
