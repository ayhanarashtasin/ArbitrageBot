"""Trading settings, paper-mode defaults and the live safety gate (project.md sections 0.5, 58, 71).

Three kinds of values:
  LOCKED               fixed by project.md. The environment cannot loosen them.
  REQUIRED_BEFORE_LIVE project.md leaves them blank. In paper/testnet mode the bot uses the clearly
                       labelled PAPER_DEFAULTS below so it can run end to end; live mode refuses to
                       start until every one of them is set explicitly in the environment.
  Other                ordinary operational settings.

PAPER_DEFAULTS are the implementer's suggestions for a small, cautious paper run. They are not
facts about any venue and not recommendations for live capital. Every resolved value records where
it came from (`explicit` or `paper_default`) so a report can never pass a default off as a decision.
"""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

D = Decimal

# ---- LOCKED (project.md sections 8, 9, 19, 22, 24, 25, 33, 34) ---------------------------------
LOCKED_MAX_LEVERAGE = D(4)
LOCKED_TWAP_ENTRY_MINUTES = 360
LOCKED_TWAP_MAX_CHILD_USD = D(100)
LOCKED_LOW_VOL_MAX_PERCENTILE = D(45)
LOCKED_SPREAD_HORIZON_MINUTES = 120
LOCKED_CONTROLLED_EXIT_RATIO = D("0.30")
LOCKED_CONTROLLED_EXIT_MINUTES = 180
LOCKED_EMERGENCY_EXIT_RATIO = D("0.10")
LOCKED_EMERGENCY_EXIT_SECONDS = 60

LIVE_CONFIRMATION_PHRASE = "I_UNDERSTAND_LIVE_ORDERS_USE_REAL_FUNDS"

REQUIRED_BEFORE_LIVE = (
    "target_notional_per_leg_usd", "target_leverage",
    "rv_window_minutes", "basis_vol_window_minutes", "vol_percentile_lookback_days", "vol_min_observations",
    "min_expected_net_edge_bps", "max_allowed_slippage_bps", "min_required_book_depth_multiplier",
    "max_market_data_age_ms",
    "max_delta_imbalance_usd", "max_delta_imbalance_ratio", "delta_hedge_timeout_ms",
    "min_liquidation_distance_pct", "max_margin_utilization_pct",
    "max_concurrent_trades", "max_total_gross_notional_usd", "max_notional_per_venue_usd",
    "max_notional_per_asset_usd",
    "normal_exit_policy",
)

# Paper-only suggestions. Each has a one-line reason so a reviewer can challenge it.
PAPER_DEFAULTS: dict[str, tuple[object, str]] = {
    "target_notional_per_leg_usd": (D(1000), "10 child slices of $100 per leg over the 6 h TWAP"),
    "target_leverage": (D(4), "the task spec: leverage 4x (each side's account holds notional / 4, see paper_account_equity_multiple)"),
    "rv_window_minutes": (60, "one hour of 1-minute returns"),
    "basis_vol_window_minutes": (60, "matches the realised-volatility window"),
    "vol_percentile_lookback_days": (1, "1 day requested; one REST call returns at most 999 one-minute candles (Bybit's cap, applied to every venue), so the window is about 16.6 h"),
    "vol_min_observations": (500, "rolling-window samples needed before a percentile is trusted"),
    "min_expected_net_edge_bps": (D(10), "of one leg's notional, after fees, slippage and basis cost"),
    "max_allowed_slippage_bps": (D(5), "per child slice, per leg"),
    "min_required_book_depth_multiplier": (D(5), "visible depth on the consumed side must be 5x the child size"),
    "max_market_data_age_ms": (3000, "REST polling: reject books older than 3 s"),
    "max_delta_imbalance_usd": (D(50), "half of one child slice"),
    "max_delta_imbalance_ratio": (D("0.05"), "5 % of the larger leg"),
    "delta_hedge_timeout_ms": (10_000, "time allowed to complete a missing hedge before flattening"),
    # Both are BACKSTOPS behind the spec's equity exits. At 4x the 30 % equity exit fires near a 17.5 % adverse move,
    # the 10 % exit near 22.5 % and liquidation sits near 24.5 %, so anything tighter would pre-empt the spec's rules.
    "min_liquidation_distance_pct": (D(1), "backstop: mark within 1 % of the liquidation price (the 10 % exit normally fires first)"),
    "max_margin_utilization_pct": (D(80), "backstop: maintenance margin / equity (the venues' margin ratio; 100 % = liquidation)"),
    "max_concurrent_trades": (2, "small book while everything is unproven"),
    "max_total_gross_notional_usd": (D(10_000), "sum of both legs of all open trades"),
    "max_notional_per_venue_usd": (D(5_000), "any single venue"),
    "max_notional_per_asset_usd": (D(5_000), "any single underlying"),
    "normal_exit_policy": ("edge_decay", "exit when the net funding edge decays; see the exit fields"),
}

# Not in the REQUIRED_BEFORE_LIVE list of the spec but needed by the design (also paper suggestions).
DESIGN_DEFAULTS: dict[str, tuple[object, str]] = {
    "expected_hold_hours": (D(72), "horizon used to turn hourly funding into expected income"),
    "normal_exit_edge_floor_bps_per_hour": (D(0), "exit when net funding per hour is at or below this"),
    "normal_exit_confirm_minutes": (60, "the decay must persist this long before exiting"),
    "max_hold_hours": (168, "hard cap on holding time under edge_decay"),
    # Live 2026-10-02: the per-slice spread and volatility gates held AERO at 6 of 10 slices for 44 h after its funding
    # edge had gone to zero, and nothing ended the entry. After this long the entry keeps what it has and the normal
    # exit rules (edge decay, max hold) decide whether the reduced position stays open.
    "max_entry_hours": (12, "an entry still unfinished after this long (twice the locked 6 h TWAP) stops and keeps what it holds"),
    "normal_exit_duration_minutes": (120, "paired TWAP reduction time for a normal exit"),
    # One sweep cycle is about 7 min of sweeping plus the 15 min pause: a shorter limit would leave the one-minute watch
    # blocked on "Zero Delta data stale" for part of every cycle while the direct venue data is perfectly fresh.
    "zero_delta_max_age_seconds": (1800, "Zero Delta data older than this blocks new entries (one sweep cycle + margin)"),
    "zero_delta_history_max_lag_hours": (D(3), "hourly chart data whose newest point is older than this is stale"),
    "selection_count": (3, "how many best pairs are selected each scan (1 to 3 recommended)"),
    "selection_metric": ("mean", "pairs are ranked by their 7-day average funding edge; 'median' is the alternative"),
    "simulation_slippage_bps": (D(2), "assumed slippage per fill in the 7-day simulation (each leg, entry and exit)"),
    # Zero Delta groups markets by ticker, and a ticker can name two different assets (live 2026-09-30: "BB" is
    # BounceBit at $0.0096 on Binance and BlackBerry stock at $8.67 on Trade.xyz). Two legs whose prices differ by
    # more than this are not a hedge of one underlying; a real cross-venue basis is far below 1 %.
    "max_leg_price_gap_pct": (D(5), "both legs must price the same underlying: mid prices within 5 % of each other"),
    "scan_interval_seconds": (900, "pause between full sweeps of every coin (a sweep itself takes several minutes)"),
    "sweep_concurrency": (2, "coins fetched from Zero Delta at the same time: gentle on its servers"),
    "sweep_delay_seconds": (D("0.3"), "pause after each coin request, to stay well inside Zero Delta's rate limits"),
    "paper_account_equity_multiple": (D(1), "simulated equity per side = notional / leverage x this; 1 = a true 4x account"),
    "selection_one_per_asset": (True, "each selected pair is a different coin, so three picks are three opportunities"),
    "selection_require_live_edge": (True, "a pick's current funding edge must still be positive, not only its 7-day average"),
    "earnings_blackout_days": (3, "the task spec: no stock trade within 2-3 days of earnings; 3 is the cautious end"),
    "entry_slice_gates": (True, "the 2 h spread rule and the low-vol score are re-checked before EVERY TWAP slice"),
    "watch_interval_seconds": (60, "selected pairs that are waiting for low volatility are re-checked every minute"),
    "shadow_paper_enabled": (True, "selected pairs also get rule-only forward paper trials when the net-edge or portfolio gate blocks"),
    "zero_delta_mismatch_tolerance_per_hour": (D("0.00002"), "fraction/h (0.002 %/h, about 17 % APR) before ZERO_DELTA_EXCHANGE_MISMATCH"),
}

# Taker/maker fees are NOT public API data. These are unverified paper assumptions
# (standard public tier, section 20 forbids treating them as verified).
PAPER_FEES_BPS: dict[str, tuple[D, D]] = {
    "binance_usdt_m": (D(5), D(2)),
    "bybit_linear": (D("5.5"), D(2)),
    "hyperliquid": (D("4.5"), D("1.5")),
    "okx_swap": (D(5), D(2)),  # Lv1 USDT swaps, okx.com/help/how-to-calculate-the-contract-transaction-fee
    "blofin_swap": (D(6), D(2)),  # lowest tier, blofin.com/fees (checked 2026-09-30)
    # MEXC's API schedule (effective 2026-06-01) overrides its web/app rates: the bot trades through the API.
    "mexc_contract": (D(8), D(6)),
    # Gate: its public contract rows carry taker 0.075 %; the VIP0 page was not confirmed, so the higher figure is used.
    "gate_usdt": (D("7.5"), D(2)),
    "bitget_usdt": (D(6), D(2)),  # standard USDT-M futures tier; not confirmed from an account
}
DEFAULT_PAPER_FEE_BPS = (D(6), D(2))  # for any venue not listed above


class ResolvedParams(BaseModel):
    """Every parameter the engine reads, with no missing values."""

    model_config = ConfigDict(frozen=True)

    run_mode: Literal["paper", "testnet", "live"]
    # locked
    max_allowed_leverage: Decimal
    twap_entry_duration_minutes: int
    twap_max_child_notional_usd: Decimal
    low_vol_max_percentile: Decimal
    funding_screening_horizon_minutes: int
    controlled_exit_equity_ratio: Decimal
    controlled_exit_duration_minutes: int
    emergency_exit_equity_ratio: Decimal
    emergency_exit_target_seconds: int
    # required before live
    target_notional_per_leg_usd: Decimal
    target_leverage: Decimal
    rv_window_minutes: int
    basis_vol_window_minutes: int
    vol_percentile_lookback_days: int
    vol_min_observations: int
    min_expected_net_edge_bps: Decimal
    max_allowed_slippage_bps: Decimal
    min_required_book_depth_multiplier: Decimal
    max_market_data_age_ms: int
    max_delta_imbalance_usd: Decimal
    max_delta_imbalance_ratio: Decimal
    delta_hedge_timeout_ms: int
    min_liquidation_distance_pct: Decimal
    max_margin_utilization_pct: Decimal
    max_concurrent_trades: int
    max_total_gross_notional_usd: Decimal
    max_notional_per_venue_usd: Decimal
    max_notional_per_asset_usd: Decimal
    normal_exit_policy: str
    # design parameters
    expected_hold_hours: Decimal
    normal_exit_edge_floor_bps_per_hour: Decimal
    normal_exit_confirm_minutes: int
    max_hold_hours: int
    max_entry_hours: int
    normal_exit_duration_minutes: int
    zero_delta_max_age_seconds: int
    zero_delta_mismatch_tolerance_per_hour: Decimal
    zero_delta_history_max_lag_hours: Decimal
    selection_count: int
    selection_metric: str
    simulation_slippage_bps: Decimal
    max_leg_price_gap_pct: Decimal
    scan_interval_seconds: int
    sweep_concurrency: int
    sweep_delay_seconds: Decimal
    paper_account_equity_multiple: Decimal
    selection_one_per_asset: bool
    selection_require_live_edge: bool
    earnings_blackout_days: int
    entry_slice_gates: bool
    watch_interval_seconds: int
    shadow_paper_enabled: bool
    # provenance: field name -> "explicit" | "paper_default" | "locked"
    provenance: dict[str, str] = Field(default_factory=dict)


class TradingSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    run_mode: Literal["paper", "testnet", "live"] = "paper"
    live_trading_enabled: bool = False
    live_trading_confirmation: str = ""
    # "ALL" = every coin Zero Delta lists (the default); or a comma separated list such as BTC,ETH,SOL
    scan_assets: Annotated[list[str], NoDecode] = ["ALL"]
    # coins never scanned, e.g. equity or commodity tickers that project.md section 2 puts out of scope
    exclude_assets: Annotated[list[str], NoDecode] = []
    database_url: str = "sqlite:///data/arbitrage.db"
    admin_api_token: str | None = None
    telegram_enabled: bool = False
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    # locked (cannot be loosened)
    max_allowed_leverage: Decimal = LOCKED_MAX_LEVERAGE
    twap_entry_duration_minutes: int = LOCKED_TWAP_ENTRY_MINUTES
    twap_max_child_notional_usd: Decimal = LOCKED_TWAP_MAX_CHILD_USD
    low_vol_max_percentile: Decimal = LOCKED_LOW_VOL_MAX_PERCENTILE
    funding_screening_horizon_minutes: int = LOCKED_SPREAD_HORIZON_MINUTES
    controlled_exit_equity_ratio: Decimal = LOCKED_CONTROLLED_EXIT_RATIO
    controlled_exit_duration_minutes: int = LOCKED_CONTROLLED_EXIT_MINUTES
    emergency_exit_equity_ratio: Decimal = LOCKED_EMERGENCY_EXIT_RATIO
    emergency_exit_target_seconds: int = LOCKED_EMERGENCY_EXIT_SECONDS

    # required before live (None = not set by the user)
    target_notional_per_leg_usd: Decimal | None = None
    target_leverage: Decimal | None = None
    rv_window_minutes: int | None = None
    basis_vol_window_minutes: int | None = None
    vol_percentile_lookback_days: int | None = None
    vol_min_observations: int | None = None
    min_expected_net_edge_bps: Decimal | None = None
    max_allowed_slippage_bps: Decimal | None = None
    min_required_book_depth_multiplier: Decimal | None = None
    max_market_data_age_ms: int | None = None
    max_delta_imbalance_usd: Decimal | None = None
    max_delta_imbalance_ratio: Decimal | None = None
    delta_hedge_timeout_ms: int | None = None
    min_liquidation_distance_pct: Decimal | None = None
    max_margin_utilization_pct: Decimal | None = None
    max_concurrent_trades: int | None = None
    max_total_gross_notional_usd: Decimal | None = None
    max_notional_per_venue_usd: Decimal | None = None
    max_notional_per_asset_usd: Decimal | None = None
    normal_exit_policy: str | None = None

    # design parameters (defaults come from DESIGN_DEFAULTS)
    expected_hold_hours: Decimal | None = None
    normal_exit_edge_floor_bps_per_hour: Decimal | None = None
    normal_exit_confirm_minutes: int | None = None
    max_hold_hours: int | None = None
    max_entry_hours: int | None = None
    normal_exit_duration_minutes: int | None = None
    zero_delta_max_age_seconds: int | None = None
    zero_delta_mismatch_tolerance_per_hour: Decimal | None = None
    zero_delta_history_max_lag_hours: Decimal | None = None
    selection_count: int | None = None
    selection_metric: str | None = None
    simulation_slippage_bps: Decimal | None = None
    max_leg_price_gap_pct: Decimal | None = None
    scan_interval_seconds: int | None = None
    sweep_concurrency: int | None = None
    sweep_delay_seconds: Decimal | None = None
    paper_account_equity_multiple: Decimal | None = None
    selection_one_per_asset: bool | None = None
    selection_require_live_edge: bool | None = None
    earnings_blackout_days: int | None = None
    entry_slice_gates: bool | None = None
    watch_interval_seconds: int | None = None
    shadow_paper_enabled: bool | None = None

    # Stock earnings calendar: automatic (Nasdaq's public calendar) plus an optional CSV of symbol,date rows that
    # adds dates the automatic source cannot know (for example Korean or Hong Kong listed stocks).
    earnings_auto_fetch: bool = True
    earnings_calendar_file: str | None = None

    # ---- locked-value guards -------------------------------------------------------------------
    @field_validator("max_allowed_leverage")
    @classmethod
    def _lev(cls, v: Decimal) -> Decimal:
        if v > LOCKED_MAX_LEVERAGE:
            raise ValueError("MAX_ALLOWED_LEVERAGE is locked at 4")
        return v

    @field_validator("twap_max_child_notional_usd")
    @classmethod
    def _child(cls, v: Decimal) -> Decimal:
        if v > LOCKED_TWAP_MAX_CHILD_USD or v <= 0:
            raise ValueError("TWAP_MAX_CHILD_NOTIONAL_USD is locked at a maximum of 100")
        return v

    @field_validator("low_vol_max_percentile")
    @classmethod
    def _vol(cls, v: Decimal) -> Decimal:
        if v > LOCKED_LOW_VOL_MAX_PERCENTILE:
            raise ValueError("LOW_VOL_MAX_PERCENTILE is locked at 45 (it may only be lowered)")
        return v

    @field_validator("scan_assets", "exclude_assets", mode="before")
    @classmethod
    def _assets(cls, v):
        if isinstance(v, str):
            v = v.split(",")
        return [a.strip().upper() for a in v if a.strip()]

    @field_validator("selection_metric")
    @classmethod
    def _metric(cls, v):
        if v is not None and v not in ("mean", "median"):
            raise ValueError("SELECTION_METRIC must be 'mean' or 'median'")
        return v

    @field_validator("selection_count")
    @classmethod
    def _count(cls, v):
        if v is not None and not 1 <= v <= 10:
            raise ValueError("SELECTION_COUNT must be between 1 and 10")
        return v

    def model_post_init(self, __context) -> None:
        exact = {
            "twap_entry_duration_minutes": LOCKED_TWAP_ENTRY_MINUTES,
            "funding_screening_horizon_minutes": LOCKED_SPREAD_HORIZON_MINUTES,
            "controlled_exit_equity_ratio": LOCKED_CONTROLLED_EXIT_RATIO,
            "controlled_exit_duration_minutes": LOCKED_CONTROLLED_EXIT_MINUTES,
            "emergency_exit_equity_ratio": LOCKED_EMERGENCY_EXIT_RATIO,
            "emergency_exit_target_seconds": LOCKED_EMERGENCY_EXIT_SECONDS,
        }
        for name, locked in exact.items():
            if getattr(self, name) != locked:
                raise ValueError(f"{name.upper()} is a LOCKED REQUIREMENT ({locked}) and cannot be changed")

    # ---- resolution ----------------------------------------------------------------------------
    def missing_required(self) -> list[str]:
        return [n.upper() for n in REQUIRED_BEFORE_LIVE if getattr(self, n) is None]

    def resolve(self) -> ResolvedParams:
        """Fill blanks from paper defaults. Live mode never fills: it raises if anything is missing."""
        if self.run_mode == "live":
            missing = self.missing_required()
            if missing:
                raise LiveGateError("live mode needs explicit values for: " + ", ".join(missing))
        data: dict[str, object] = {"run_mode": self.run_mode}
        provenance: dict[str, str] = {}
        for name in ResolvedParams.model_fields:
            if name in ("run_mode", "provenance"):
                continue
            value = getattr(self, name)
            if name in PAPER_DEFAULTS or name in DESIGN_DEFAULTS:
                if value is None:
                    value = (PAPER_DEFAULTS.get(name) or DESIGN_DEFAULTS[name])[0]
                    provenance[name] = "paper_default"
                else:
                    provenance[name] = "explicit"
            else:
                provenance[name] = "locked"
            data[name] = value
        params = ResolvedParams(**data, provenance=provenance)
        for name in ("target_notional_per_leg_usd", "target_leverage", "paper_account_equity_multiple",
                     "watch_interval_seconds", "scan_interval_seconds", "max_hold_hours", "max_entry_hours"):
            if getattr(params, name) <= 0:
                raise ValueError(f"{name.upper()} must be positive")
        if params.max_entry_hours * 60 < params.twap_entry_duration_minutes:
            raise ValueError("MAX_ENTRY_HOURS cannot be shorter than the locked 6 h entry TWAP")
        if not 2 <= params.earnings_blackout_days <= 3:
            raise ValueError("EARNINGS_BLACKOUT_DAYS must be 2 or 3")
        if params.target_leverage / params.paper_account_equity_multiple > params.max_allowed_leverage:
            raise ValueError("paper account effective leverage cannot exceed MAX_ALLOWED_LEVERAGE (4)")
        if params.target_leverage > params.max_allowed_leverage:
            raise ValueError("TARGET_LEVERAGE cannot exceed MAX_ALLOWED_LEVERAGE (4)")
        return params


class LiveGateError(RuntimeError):
    """Live mode is not allowed to start."""


def check_live_gate(settings: TradingSettings) -> None:
    """Raise LiveGateError unless live orders are explicitly and completely enabled.

    Paper and testnet always pass. Presence of API keys never enables live (section 0.5).
    """
    if settings.run_mode != "live":
        return
    problems = []
    if not settings.live_trading_enabled:
        problems.append("LIVE_TRADING_ENABLED is not true")
    if settings.live_trading_confirmation != LIVE_CONFIRMATION_PHRASE:
        problems.append("LIVE_TRADING_CONFIRMATION does not match the required phrase")
    missing = settings.missing_required()
    if missing:
        problems.append("missing REQUIRED_BEFORE_LIVE values: " + ", ".join(missing))
    if problems:
        raise LiveGateError("; ".join(problems))
