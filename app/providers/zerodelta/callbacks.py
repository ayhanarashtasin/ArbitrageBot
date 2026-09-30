"""Dash callback payload builders.

The shapes below reproduce what the captured, authenticated browser session sends
(Capture_zero_Delta/all_callbacks.txt, zero_delta_fetcher.py). Nothing here is invented.
"""

from typing import Any

from app.core.config import ZeroDeltaSettings

from .exceptions import ZeroDeltaConfigError

# Exchange keys the capture tooling sent for the "all exchanges" view.
KNOWN_EXCHANGE_KEYS = [
    "01xyz", "arcus", "aster", "binance_usdc", "binance_usdt", "bitstamp",
    "blofin_usdc", "blofin_usdt", "bybit_usdc", "bybit_usdt", "coinbase_intl",
    "coinbase_us", "deribit", "dydx", "entropy", "extended", "gate", "grvt",
    "hyperliquid", "katana_perps", "kraken", "lighter", "lighter_rh",
    "markets_by_kinetiq", "mexc_usdc", "mexc_usdt", "nado", "okx",
    "okx_xperps", "ondoperps", "pacifica", "paradex", "paragon", "perpl",
    "trade_xyz", "variational",
]

HISTORY_WINDOW = "14d"  # the hourly chart window used by the captured requests


def _auth_store(settings: ZeroDeltaSettings) -> dict[str, Any]:
    if settings.user_id is None:
        raise ZeroDeltaConfigError("ZERO_DELTA_USER_ID is not set")
    return {"is_authenticated": True, "user_id": settings.user_id, "is_pro": True}


def asset_payload(asset: str, settings: ZeroDeltaSettings) -> dict[str, Any]:
    """Venue table + per-venue hourly/daily APR series for one asset."""
    changed = ["store-selected-coin.data", "show-hourly.value", "exchange-dropdown.value"]
    return {
        "output": "..apr-graph.figure...store-latest-hourly.data...store-best-combo.data..",
        "outputs": [
            {"id": "apr-graph", "property": "figure"},
            {"id": "store-latest-hourly", "property": "data"},
            {"id": "store-best-combo", "property": "data"},
        ],
        "inputs": [
            {"id": "store-selected-coin", "property": "data", "value": asset.upper()},
            {"id": "store-applied-exchanges", "property": "data", "value": KNOWN_EXCHANGE_KEYS},
            {"id": "store-applied-volume", "property": "data", "value": 1},
            {"id": "show-hourly", "property": "value", "value": ["hourly"]},
            {
                "id": {"aio_id": "theme", "component": "ThemeSwitchAIO", "subcomponent": "switch"},
                "property": "value",
                "value": True,
            },
            {"id": "leverage-slider", "property": "value", "value": 2},
            {"id": "fixed-leg-dropdown", "property": "value", "value": None},
            {"id": "best-combo-window", "property": "value", "value": HISTORY_WINDOW},
            {"id": "exchange-dropdown", "property": "value", "value": KNOWN_EXCHANGE_KEYS},
            {"id": "store-auth-session", "property": "data", "value": _auth_store(settings)},
            {"id": "store-apr-chart-window", "property": "data", "value": HISTORY_WINDOW},
        ],
        "changedPropIds": changed,
        "parsedChangedPropsIds": changed,
    }


def coin_table_payload(settings: ZeroDeltaSettings, selected_coin: str = "BTC") -> dict[str, Any]:
    """Coin table (one row per asset). Cheap authenticated call used by the health check.

    The site's callback also declares store-selected-coin as State (all_callbacks.txt, callback 33);
    the older capture request left it out and now gets HTTP 500.
    """
    changed = ["store-applied-exchanges.data"]
    return {
        "output": "..coin-table.rowData...coin-table.selectedRows..",
        "outputs": [
            {"id": "coin-table", "property": "rowData"},
            {"id": "coin-table", "property": "selectedRows"},
        ],
        "inputs": [
            {"id": "store-applied-exchanges", "property": "data", "value": KNOWN_EXCHANGE_KEYS},
            {"id": "store-applied-volume", "property": "data", "value": 1},
            {"id": "leverage-slider", "property": "value", "value": 2},
            {"id": "fixed-leg-dropdown", "property": "value", "value": None},
            {"id": "best-combo-window", "property": "value", "value": HISTORY_WINDOW},
            {"id": "exchange-dropdown", "property": "value", "value": KNOWN_EXCHANGE_KEYS},
            {"id": "store-auth-session", "property": "data", "value": _auth_store(settings)},
        ],
        "state": [{"id": "store-selected-coin", "property": "data", "value": selected_coin.upper()}],
        "changedPropIds": changed,
        "parsedChangedPropsIds": changed,
    }
