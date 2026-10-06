"""Venue-type classification (CEX / DEX / HIP3), keyed by Zero Delta's display name.

PROVISIONAL: the built-in CEX/DEX lists below are the implementer's assumption, not verified against
Zero Delta or the venues (REQUIRED_BEFORE_LIVE: the user confirms them). A type is only "verified"
when it comes from ZERO_DELTA_VENUE_TYPES / settings.venue_types. Everything unlisted is UNKNOWN.
HIP-3 (builder-deployed Hyperliquid markets) needs the exact market/DEX identifier, so no venue
is preset as HIP3.
"""

import re
from enum import StrEnum


class VenueType(StrEnum):
    CEX = "CEX"
    DEX = "DEX"
    HIP3 = "HIP3"
    UNKNOWN = "UNKNOWN"


def venue_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


_CEX = [
    "Binance USDC", "Binance USDT", "Bitstamp", "BloFin USDC", "BloFin USDT",
    "Bybit USDC", "Bybit USDT", "Coinbase International", "Coinbase US", "Deribit",
    "Gate", "Kraken", "MEXC USDC", "MEXC USDT", "OKX", "OKX xPerps", "Bitget",
]
_DEX = ["Aster", "dYdX", "Extended", "GRVT", "Hyperliquid", "Lighter", "Paradex"]
# Added after web research on 2026-09-29 (sources in the research notes). Still provisional.
_DEX += ["Arcus", "Katana Perps", "Ligher on RH", "Nado", "Pacifica", "Perpl", "Variational", "n1.xyz"]
# Builder-deployed Hyperliquid markets (HIP-3). Provisional: Zero Delta gives no dex/market id, so the
# exact market must be resolved from Hyperliquid itself before any order (project.md section 7).
_HIP3 = ["Trade.xyz", "Markets by Kinetiq"]
# Ondo Perps is deliberately left UNKNOWN: sources disagree on custody.

DEFAULT_VENUE_TYPES: dict[str, VenueType] = {
    **{venue_key(n): VenueType.CEX for n in _CEX},
    **{venue_key(n): VenueType.DEX for n in _DEX},
    **{venue_key(n): VenueType.HIP3 for n in _HIP3},
}


def database_venue_name(exchange: str, overrides: dict[str, str] | None = None) -> str:
    """Use existing display identities; unfamiliar keys remain unsupported, never guessed."""
    if overrides and exchange in overrides:
        return overrides[exchange]
    aliases = {"coinbase_intl": "Coinbase International", "trade_xyz": "Trade.xyz",
               "lighter_rh": "Ligher on RH", "ondoperps": "Ondo Perps", "bitget_usdt": "Bitget"}
    names = {venue_key(n): n for n in [*_CEX, *_DEX, *_HIP3]}
    return aliases.get(exchange, names.get(venue_key(exchange), exchange))


class VenueRegistry:
    def __init__(self, overrides: dict[str, str] | None = None):
        self._types = dict(DEFAULT_VENUE_TYPES)
        self._verified: set[str] = set()
        for name, kind in (overrides or {}).items():
            self._types[venue_key(name)] = VenueType(kind.upper().replace("-", ""))
            self._verified.add(venue_key(name))

    def is_verified(self, display_name: str) -> bool:
        """True only for types the user supplied. Built-in types are provisional."""
        return venue_key(display_name) in self._verified

    def classify(self, display_name: str) -> VenueType:
        return self._types.get(venue_key(display_name), VenueType.UNKNOWN)
