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
from app.execution.engine import TradeEngine
from app.notifications.notifier import Notifier
from app.persistence.repository import Repository
from app.workers.scanner import Scanner

log = logging.getLogger(__name__)
ERROR_ALERT_EVERY_SECONDS = 300


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

    async def _guard(self, name: str, coro) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop must survive
            log.exception("%s iteration failed", name)
            self.health.mark(name, False, type(exc).__name__)
            if time.monotonic() - self._last_error_alert > ERROR_ALERT_EVERY_SECONDS:
                self._last_error_alert = time.monotonic()
                await self.notifier.alert("critical_error", f"{name} failed: {type(exc).__name__}", severity="critical")

    async def engine_loop(self) -> None:
        while not self._stop.is_set():
            await self._guard("engine", self.engine.run_once())
            self.health.mark("engine", True, f"{len(self.engine.active)} active trades")
            await self._sleep(self.engine.recommended_delay())

    async def scan_loop(self) -> None:
        while not self._stop.is_set():
            if self.engine.kill.entries_allowed and not self.engine.blocked():
                assets = await self.scanner.resolve_assets(self.settings.scan_assets, self.settings.exclude_assets)
                await self._guard("scanner", self.scanner.scan_all(assets))
            await self._sleep(self.params.scan_interval_seconds)

    async def watch_loop(self) -> None:
        while not self._stop.is_set():
            await self._guard("watch", self.scanner.recheck_watch())
            await self._sleep(self.params.watch_interval_seconds)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def run(self) -> None:
        await self.startup()
        await asyncio.gather(self.engine_loop(), self.scan_loop(), self.watch_loop())
