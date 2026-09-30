"""Wires the bot together. The ONLY place that decides which adapters and providers exist.

Paper mode is the only mode that can start today: live and testnet need real order placement
(project.md phase 8), which is not implemented, so they refuse to start even if every gate is satisfied.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from app.core.clock import Clock
from app.core.config import ZeroDeltaSettings
from app.core.health import Health
from app.core.trading_config import LiveGateError, ResolvedParams, TradingSettings, check_live_gate
from app.exchanges.base import ExchangeAdapter
from app.execution.engine import TradeEngine
from app.execution.venue_provider import PaperVenueProvider
from app.notifications.notifier import Notifier
from app.persistence.repository import Repository
from app.providers.zerodelta import ZeroDeltaProvider
from app.risk.killswitch import KillSwitch
from app.market_data.earnings import EarningsCalendar
from app.workers.runner import Runner
from app.workers.scanner import Scanner

log = logging.getLogger(__name__)

SourceLookup = Callable[[str], ExchangeAdapter | None]


@dataclass
class Runtime:
    settings: TradingSettings
    params: ResolvedParams
    repo: Repository
    clock: Clock
    kill: KillSwitch
    notifier: Notifier
    health: Health
    engine: TradeEngine
    scanner: Scanner
    runner: Runner
    zd: ZeroDeltaProvider | None
    adapters: dict = field(default_factory=dict)

    async def aclose(self) -> None:
        if self.scanner.earnings is not None:
            await self.scanner.earnings.aclose()
        if self.zd is not None:
            await self.zd.aclose()
        for adapter in self.adapters.values():
            await adapter.close()


def default_source_lookup(cache: dict) -> SourceLookup:
    """Real public market-data adapters, created lazily. One instance per exchange, shared by its venues
    (for example "Binance USDT" and "Binance USDC"), so the instrument cache is shared. Unknown venues give None."""
    from app.exchanges.registry import binding_for

    def lookup(zd_venue_name: str) -> ExchangeAdapter | None:
        binding = binding_for(zd_venue_name)
        if binding is None:
            return None
        if binding.factory not in cache:
            cache[binding.factory] = binding.factory()
        return cache[binding.factory]

    return lookup


def build_runtime(
    settings: TradingSettings | None = None,
    zd_settings: ZeroDeltaSettings | None = None,
    *,
    clock: Clock | None = None,
    source_for_venue: SourceLookup | None = None,
    zd: ZeroDeltaProvider | None = None,
) -> Runtime:
    settings = settings or TradingSettings()
    check_live_gate(settings)  # refuses live mode unless every gate and value is explicit
    if settings.run_mode != "paper":
        raise LiveGateError(
            f"RUN_MODE={settings.run_mode!r} needs real order placement, which is not implemented yet "
            "(project.md phase 8). Only paper mode can start.")
    params = settings.resolve()
    zd_settings = zd_settings or ZeroDeltaSettings()
    clock = clock or Clock()
    repo = Repository(settings.database_url, clock)
    health = Health(clock)

    def audit(kind: str, data: dict) -> None:
        repo.audit(kind, data)
        repo.set_kv("kill_switch", {"pause_new_entries": kill.pause_new_entries,
                                    "emergency_flatten": kill.emergency_flatten})

    kill = KillSwitch(clock, audit=audit)
    saved = repo.get_kv("kill_switch")  # a restart must not silently lift a pause or a flatten
    if saved:
        kill.pause_new_entries = bool(saved.get("pause_new_entries"))
        kill.emergency_flatten = bool(saved.get("emergency_flatten"))

    secrets = [v.get_secret_value() for v in (zd_settings.session_cookie,) if v is not None]
    if settings.admin_api_token:
        secrets.append(settings.admin_api_token)
    notifier = Notifier(repo, telegram_enabled=settings.telegram_enabled, bot_token=settings.telegram_bot_token,
                        chat_id=settings.telegram_chat_id, secrets=secrets)

    adapters: dict = {}
    lookup = source_for_venue or default_source_lookup(adapters)
    earnings = EarningsCalendar(blackout_days=params.earnings_blackout_days, now=clock.now,
                               auto_fetch=settings.earnings_auto_fetch, file_path=settings.earnings_calendar_file)
    provider = PaperVenueProvider(lookup, clock, repo)
    engine = TradeEngine(params=params, repo=repo, clock=clock, killswitch=kill, notifier=notifier, provider=provider,
                         earnings=earnings)

    if zd is None and zd_settings.enabled and zd_settings.session_cookie is not None and zd_settings.user_id is not None:
        zd = ZeroDeltaProvider(zd_settings)
    elif zd is None:
        log.warning("Zero Delta is not configured: the bot will manage existing trades but find no new ones")

    scanner = Scanner(params=params, zd_settings=zd_settings, zd=zd, source_for_venue=lookup, repo=repo,
                      engine=engine, notifier=notifier, clock=clock, health=health,
                      earnings=earnings)
    runner = Runner(settings=settings, params=params, repo=repo, engine=engine, scanner=scanner, notifier=notifier,
                    health=health, clock=clock)
    return Runtime(settings, params, repo, clock, kill, notifier, health, engine, scanner, runner, zd, adapters)
