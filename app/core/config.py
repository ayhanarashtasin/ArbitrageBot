"""Settings loaded from environment variables (and an optional local .env file)."""

from decimal import Decimal
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# LOCKED REQUIREMENTS (project.md sections 8 and 9)
LOCKED_MIN_VOLUME_USD = Decimal("5000000")
LOCKED_MIN_LOOKBACK_DAYS = 7


class ZeroDeltaSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ZERO_DELTA_", env_file=".env", env_ignore_empty=True, extra="ignore"
    )

    enabled: bool = True
    base_url: str = "https://www.zero-delta.xyz"
    dash_update_path: str = "/_dash-update-component"
    auth_mode: Literal["session_cookie"] = "session_cookie"
    session_cookie: SecretStr | None = None
    # Account id sent in the Dash auth-session store. No default: it belongs to the user's account.
    user_id: int | None = None

    min_avg_24h_volume_usd: Decimal = LOCKED_MIN_VOLUME_USD
    primary_lookback_days: int = LOCKED_MIN_LOOKBACK_DAYS
    min_lookback_days: int = LOCKED_MIN_LOOKBACK_DAYS

    # NOT specified in project.md. 20s is the value the capture tooling used; confirm or change.
    request_timeout_seconds: float = 20.0
    # Retry policy: NOT specified in project.md. Implementation defaults, confirm or change.
    max_retries: int = 2  # read requests only: timeouts, transport errors, 5xx
    retry_backoff_seconds: float = 0.5  # delay doubles each retry
    # How many missing hourly points are tolerated inside the 7-day window.
    # project.md asks for "acceptable continuity" without a number, so the default is strict.
    max_missing_intervals: int = Field(default=0, ge=0)

    # Display-name -> CEX|DEX|HIP3, e.g. {"Arcus": "DEX"}. JSON in the environment.
    venue_types: dict[str, str] = Field(default_factory=dict)

    @field_validator("min_avg_24h_volume_usd")
    @classmethod
    def _volume_not_below_lock(cls, v: Decimal) -> Decimal:
        if v < LOCKED_MIN_VOLUME_USD:
            raise ValueError("min_avg_24h_volume_usd cannot be below the locked $5,000,000")
        return v

    @field_validator("primary_lookback_days", "min_lookback_days")
    @classmethod
    def _days_not_below_lock(cls, v: int) -> int:
        if v < LOCKED_MIN_LOOKBACK_DAYS:
            raise ValueError("lookback days cannot be below the locked 7 complete days")
        return v
