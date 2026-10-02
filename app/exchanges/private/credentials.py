"""API credentials for live trading, read from the environment / .env and never logged.

Set only the venues you trade. A venue without a complete set of credentials is simply not traded live.

    BINANCE_API_KEY, BINANCE_API_SECRET
    BYBIT_API_KEY, BYBIT_API_SECRET
    MEXC_API_KEY, MEXC_API_SECRET
    BLOFIN_API_KEY, BLOFIN_API_SECRET, BLOFIN_API_PASSPHRASE
    BITGET_API_KEY, BITGET_API_SECRET, BITGET_API_PASSPHRASE

Create every key with futures TRADING enabled, WITHDRAWALS DISABLED, and bound to your IP address.
"""

from dataclasses import dataclass
from typing import ClassVar

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class VenueCredentials:
    key: str
    secret: str
    passphrase: str | None = None


class LiveCredentials(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    binance_api_key: SecretStr | None = None
    binance_api_secret: SecretStr | None = None
    bybit_api_key: SecretStr | None = None
    bybit_api_secret: SecretStr | None = None
    mexc_api_key: SecretStr | None = None
    mexc_api_secret: SecretStr | None = None
    blofin_api_key: SecretStr | None = None
    blofin_api_secret: SecretStr | None = None
    blofin_api_passphrase: SecretStr | None = None
    bitget_api_key: SecretStr | None = None
    bitget_api_secret: SecretStr | None = None
    bitget_api_passphrase: SecretStr | None = None

    # venue (public adapter name) -> the env prefix and whether it needs a passphrase
    VENUES: ClassVar[dict[str, tuple[str, bool]]] = {
        "binance_usdt_m": ("binance", False),
        "bybit_linear": ("bybit", False),
        "mexc_contract": ("mexc", False),
        "blofin_swap": ("blofin", True),
        "bitget_usdt": ("bitget", True),
    }

    def for_venue(self, venue: str) -> VenueCredentials | None:
        """Complete credentials for the venue, or None when any required part is missing."""
        spec = self.VENUES.get(venue)
        if spec is None:
            return None
        prefix, needs_passphrase = spec
        key, secret = getattr(self, f"{prefix}_api_key"), getattr(self, f"{prefix}_api_secret")
        passphrase = getattr(self, f"{prefix}_api_passphrase", None)
        if key is None or secret is None or (needs_passphrase and passphrase is None):
            return None
        return VenueCredentials(key.get_secret_value(), secret.get_secret_value(),
                                passphrase.get_secret_value() if passphrase is not None else None)

    def configured(self) -> list[str]:
        return [v for v in self.VENUES if self.for_venue(v) is not None]

    def secret_values(self) -> list[str]:
        """Every configured secret, so alerts can scrub them (the Notifier replaces them with ***)."""
        out = []
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                out.append(value.get_secret_value())
        return out
