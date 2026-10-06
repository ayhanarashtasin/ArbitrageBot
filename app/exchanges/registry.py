"""Zero Delta venue display name -> direct-venue adapter (Phase 3, public market data only).

Only venues that have an implemented and verified adapter are listed. Everything else returns None
from get_adapter(): an unsupported venue is never traded and never guessed.

quote / settle are the venue's own values (what the instrument list reports), so the caller can do:

    binding = binding_for("Binance USDC")
    adapter = binding.factory(...)
    meta = await adapter.resolve_symbol("BTC", binding.quote, binding.settle)

Callers should keep ONE adapter instance per adapter name (for example both "Binance USDT" and
"Binance USDC" are served by the same BinanceUsdmAdapter) so the 10-minute instrument cache is shared.
"""

import re
from collections.abc import Callable
from functools import partial
from typing import NamedTuple

from .base import ExchangeAdapter
from .binance import BinanceUsdmAdapter
from .bybit import BybitLinearAdapter
from .hyperliquid import HyperliquidAdapter
from .okx import OkxSwapAdapter
from .gate import GateUsdtFuturesAdapter
from .mexc import MexcContractAdapter
from .blofin import BlofinSwapAdapter
from .bitget import BitgetUsdtAdapter


class AdapterBinding(NamedTuple):
    factory: Callable[..., ExchangeAdapter]  # called with the adapter constructor kwargs
    quote: str
    settle: str


ADAPTER_FOR_ZERODELTA_VENUE: dict[str, AdapterBinding] = {
    "Binance USDT": AdapterBinding(BinanceUsdmAdapter, "USDT", "USDT"),
    "Binance USDC": AdapterBinding(BinanceUsdmAdapter, "USDC", "USDC"),
    "Bybit USDT": AdapterBinding(BybitLinearAdapter, "USDT", "USDT"),
    "Bybit USDC": AdapterBinding(BybitLinearAdapter, "USDC", "USDC"),
    "OKX": AdapterBinding(OkxSwapAdapter, "USDT", "USDT"),
    "Gate": AdapterBinding(GateUsdtFuturesAdapter, "USDT", "USDT"),
    "MEXC USDT": AdapterBinding(MexcContractAdapter, "USDT", "USDT"),
    "MEXC USDC": AdapterBinding(MexcContractAdapter, "USDC", "USDC"),  # USDC-margined perps verified live 2026-09-30
    "BloFin USDT": AdapterBinding(BlofinSwapAdapter, "USDT", "USDT"),
    "BloFin USDC": AdapterBinding(BlofinSwapAdapter, "USDC", "USDC"),  # 10 USDC-margined perps verified live 2026-09-30
    "Bitget": AdapterBinding(BitgetUsdtAdapter, "USDT", "USDT"),
    # Core perps (first perp dex). Collateral token 0 is USDC (verified via spotMeta).
    "Hyperliquid": AdapterBinding(HyperliquidAdapter, "USDC", "USDC"),
    # HIP-3: the builder dex named "xyz" (perpDexs fullName "XYZ") lists xyz:TSLA, xyz:GOLD, ...;
    # its collateral token is USDC (verified). The display-name -> dex link rests on the dex name and
    # public secondary sources: REQUIRED_BEFORE_LIVE confirm it against a Zero Delta "Trade.xyz" row.
    "Trade.xyz": AdapterBinding(partial(HyperliquidAdapter, dex="xyz", underlying_type="TRADFI"), "USDC", "USDC"),
}

# Zero Delta venues that look implementable but are deliberately NOT mapped, with the reason.
UNRESOLVED_ZERODELTA_VENUES: dict[str, str] = {
    "Markets by Kinetiq": (
        "two Hyperliquid perp dexes carry this name in perpDexs: 'km' ('Markets by Kinetiq', collateral "
        "USDH, every asset isDelisted, zero volume) and 'mkts' ('Markets By Kinetiq', collateral USDC, "
        "live). The HIP-3 route works for both but the display name cannot be tied to one dex from "
        "public data; the user must confirm which dex Zero Delta means."
    ),
}


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


_BY_KEY = {_key(name): binding for name, binding in ADAPTER_FOR_ZERODELTA_VENUE.items()}


def binding_for(zd_venue_name: str) -> AdapterBinding | None:
    """Binding for a Zero Delta display name (case/punctuation-insensitive), or None."""
    return _BY_KEY.get(_key(zd_venue_name))


def get_adapter(zd_venue_name: str, **adapter_kwargs) -> ExchangeAdapter | None:
    """A NEW public-market-data adapter for the venue, or None when the venue is unsupported.

    adapter_kwargs go to the adapter constructor (transport=, base_url=, timeout=, ...).
    """
    binding = binding_for(zd_venue_name)
    return binding.factory(**adapter_kwargs) if binding else None


def excluded_venues(entries: list[str]) -> frozenset[str]:
    """The supported venues that EXCLUDE_VENUES names, as Zero Delta display names.

    An entry is a venue ("BloFin USDT") or a whole exchange ("BloFin" = BloFin USDT and BloFin USDC), matched without
    regard to case or punctuation. An entry that names no supported venue raises ValueError, so a typo cannot leave
    the venue in use without anyone noticing.
    """
    out: set[str] = set()
    for entry in entries:
        key = _key(entry)
        exact = [name for name in ADAPTER_FOR_ZERODELTA_VENUE if _key(name) == key]
        matched = exact or [name for name in ADAPTER_FOR_ZERODELTA_VENUE if key and _key(name).startswith(key)]
        if not matched:
            raise ValueError(f"EXCLUDE_VENUES: {entry!r} is not a supported venue; use one of "
                             f"{', '.join(ADAPTER_FOR_ZERODELTA_VENUE)} (or an exchange name such as BloFin)")
        out.update(matched)
    return frozenset(out)


def is_excluded(zd_venue_name: str, excluded: frozenset[str]) -> bool:
    """True when the Zero Delta venue is one of `excluded` (case/punctuation-insensitive)."""
    return _key(zd_venue_name) in {_key(name) for name in excluded}
