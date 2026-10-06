"""Wires the bot together. The ONLY place that decides which adapters and providers exist.

Paper mode: real market data, simulated accounts. Live mode (project.md phase 8): real orders on the venues that
have API credentials, only after every live gate is satisfied (LIVE_TRADING_ENABLED, the confirmation phrase, every
REQUIRED_BEFORE_LIVE value) and at least two venues have credentials. At startup each venue's readiness check decides
whether it may open NEW trades; open trades are always managed. Testnet is not implemented and refuses to start.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from app.core.clock import Clock
from app.core.config import ZeroDeltaSettings
from app.core.health import Health
from app.core.trading_config import LiveGateError, ResolvedParams, TradingSettings, check_live_gate
from app.exchanges.base import ExchangeAdapter
from app.exchanges.registry import excluded_venues
from app.execution.engine import TradeEngine
from app.execution.venue_provider import LiveVenueProvider, PaperVenueProvider
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
    private_clients: dict = field(default_factory=dict)  # live mode: signed clients by venue

    async def aclose(self) -> None:
        if self.scanner.earnings is not None:
            await self.scanner.earnings.aclose()
        if self.zd is not None:
            await self.zd.aclose()
        for adapter in self.adapters.values():
            await adapter.close()
        for client in self.private_clients.values():
            await client.close()


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
    credentials=None,
) -> Runtime:
    settings = settings or TradingSettings()
    if settings.run_mode == "testnet":
        raise LiveGateError("RUN_MODE=testnet is not implemented: use paper, or live (check the venues first with "
                            "python -m app.tools.live_check)")
    check_live_gate(settings)  # refuses live mode unless every gate and value is explicit
    params = settings.resolve()
    live = settings.run_mode == "live"
    private_clients: dict = {}
    credential_secrets: list[str] = []
    if live:
        from app.exchanges.private.credentials import LiveCredentials
        from app.exchanges.private.registry import build_private_clients
        credentials = credentials or LiveCredentials()
        private_clients = build_private_clients(credentials)
        if len(private_clients) < 2:
            raise LiveGateError("live mode needs complete API credentials for at least two venues (each trade has a "
                                f"long and a short venue); configured: {sorted(private_clients) or 'none'}")
        credential_secrets = credentials.secret_values()
    zd_settings = zd_settings or ZeroDeltaSettings()
    if zd_settings.source == "postgres":
        if zd_settings.database_url is not None:
            from sqlalchemy.engine import make_url
            from sqlalchemy.exc import ArgumentError
            from app.providers.zerodelta.exceptions import ZeroDeltaConfigError
            try:
                local = make_url(settings.database_url)
                upstream = make_url(zd_settings.database_url.get_secret_value())
            except ArgumentError:
                raise ZeroDeltaConfigError("Database connections must be valid URLs") from None
            if local.drivername.startswith("postgres") and (local.host, local.port or 5432, local.database) == (
                    upstream.host, upstream.port or 5432, upstream.database):
                raise ZeroDeltaConfigError("DATABASE_URL must not point to the upstream Zero Delta database")
        params = params.model_copy(update={"scan_interval_seconds": 3600,
            "zero_delta_max_age_seconds": (60 + zd_settings.refresh_minute + zd_settings.refresh_grace_minutes) * 60,
            "provenance": {**params.provenance, "scan_interval_seconds": "provider_default",
                           "zero_delta_max_age_seconds": "provider_default"}})
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

    secrets = [v.get_secret_value() for v in (zd_settings.session_cookie, zd_settings.database_url) if v is not None]
    if settings.admin_api_token:
        secrets.append(settings.admin_api_token)
    secrets.extend(credential_secrets)  # API keys never appear in an alert
    notifier = Notifier(repo, telegram_enabled=settings.telegram_enabled, bot_token=settings.telegram_bot_token,
                        chat_id=settings.telegram_chat_id, secrets=secrets)

    adapters: dict = {}
    lookup = source_for_venue or default_source_lookup(adapters)
    earnings = EarningsCalendar(blackout_days=params.earnings_blackout_days, now=clock.now,
                               auto_fetch=settings.earnings_auto_fetch, file_path=settings.earnings_calendar_file)
    provider = LiveVenueProvider(lookup, private_clients, clock, repo) if live else PaperVenueProvider(lookup, clock, repo)
    execution_venue = None
    if live:
        from app.exchanges.private.registry import LIVE_ZERODELTA_VENUES

        def execution_venue(zd_venue: str) -> bool:
            return zd_venue in LIVE_ZERODELTA_VENUES and provider.tradable_venue(zd_venue)
    engine = TradeEngine(params=params, repo=repo, clock=clock, killswitch=kill, notifier=notifier, provider=provider,
                         earnings=earnings)

    configured = (zd_settings.database_url is not None if zd_settings.source == "postgres" else
                  zd_settings.session_cookie is not None and zd_settings.user_id is not None)
    if zd is None and zd_settings.enabled and configured:
        zd = ZeroDeltaProvider(zd_settings, now=clock.now)
    elif zd is None:
        log.warning("Zero Delta is not configured: the bot will manage existing trades but find no new ones")

    scanner = Scanner(params=params, zd_settings=zd_settings, zd=zd, source_for_venue=lookup, repo=repo,
                      engine=engine, notifier=notifier, clock=clock, health=health,
                      earnings=earnings, execution_venue=execution_venue,
                      excluded_venues=excluded_venues(settings.exclude_venues))
    runner = Runner(settings=settings, params=params, repo=repo, engine=engine, scanner=scanner, notifier=notifier,
                    health=health, clock=clock)
    return Runtime(settings, params, repo, clock, kill, notifier, health, engine, scanner, runner, zd, adapters,
                   private_clients)
