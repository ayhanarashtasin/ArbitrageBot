import asyncio

import httpx
import pytest

from app.exchanges.binance import BinanceUsdmAdapter
from app.exchanges.bybit import BybitLinearAdapter
from app.exchanges.hyperliquid import HyperliquidAdapter
from app.exchanges.okx import OkxSwapAdapter
from app.exchanges.gate import GateUsdtFuturesAdapter
from app.exchanges.mexc import MexcContractAdapter
from app.exchanges.blofin import BlofinSwapAdapter
from app.exchanges.bitget import BitgetUsdtAdapter
from app.exchanges.registry import (
    ADAPTER_FOR_ZERODELTA_VENUE,
    UNRESOLVED_ZERODELTA_VENUES,
    binding_for,
    get_adapter,
)

# Display names exactly as Zero Delta reports them (tests/fixtures/zerodelta/*_dash_response.json).
EXPECTED = {
    "Binance USDT": (BinanceUsdmAdapter, "USDT", "USDT", "binance_usdt_m"),
    "Binance USDC": (BinanceUsdmAdapter, "USDC", "USDC", "binance_usdt_m"),
    "Bybit USDT": (BybitLinearAdapter, "USDT", "USDT", "bybit_linear"),
    "Bybit USDC": (BybitLinearAdapter, "USDC", "USDC", "bybit_linear"),
    "Hyperliquid": (HyperliquidAdapter, "USDC", "USDC", "hyperliquid"),
    "Trade.xyz": (HyperliquidAdapter, "USDC", "USDC", "hyperliquid:xyz"),
    "OKX": (OkxSwapAdapter, "USDT", "USDT", "okx_swap"),
    "Gate": (GateUsdtFuturesAdapter, "USDT", "USDT", "gate_usdt"),
    "MEXC USDT": (MexcContractAdapter, "USDT", "USDT", "mexc_contract"),
    "MEXC USDC": (MexcContractAdapter, "USDC", "USDC", "mexc_contract"),
    "BloFin USDT": (BlofinSwapAdapter, "USDT", "USDT", "blofin_swap"),
    "BloFin USDC": (BlofinSwapAdapter, "USDC", "USDC", "blofin_swap"),
    "Bitget": (BitgetUsdtAdapter, "USDT", "USDT", "bitget_usdt"),
}


OFFLINE = httpx.MockTransport(lambda request: httpx.Response(500))  # never reached; avoids TLS setup cost


def close(adapter):
    asyncio.run(adapter.close())


def test_only_implemented_venues_are_registered():
    assert set(ADAPTER_FOR_ZERODELTA_VENUE) == set(EXPECTED)


@pytest.mark.parametrize("display_name", sorted(EXPECTED))
def test_binding_and_adapter_for_each_supported_venue(display_name):
    cls, quote, settle, adapter_name = EXPECTED[display_name]
    binding = ADAPTER_FOR_ZERODELTA_VENUE[display_name]
    factory, b_quote, b_settle = binding  # a plain (factory, quote, settle) tuple
    assert (b_quote, b_settle) == (quote, settle)
    adapter = get_adapter(display_name, transport=OFFLINE)
    try:
        assert isinstance(adapter, cls) and adapter.name == adapter_name
        second = factory(transport=OFFLINE)
        assert second.name == adapter_name
        close(second)
    finally:
        close(adapter)


@pytest.mark.parametrize("name", ["Kraken", "Lighter", "Markets by Kinetiq", "n1.xyz", "", "Binance"])
def test_unsupported_venues_return_none(name):
    assert get_adapter(name, transport=OFFLINE) is None and binding_for(name) is None


def test_kinetiq_is_deliberately_unmapped_with_a_reason():
    assert "Markets by Kinetiq" in UNRESOLVED_ZERODELTA_VENUES
    assert "km" in UNRESOLVED_ZERODELTA_VENUES["Markets by Kinetiq"]
    assert "mkts" in UNRESOLVED_ZERODELTA_VENUES["Markets by Kinetiq"]
    assert not set(UNRESOLVED_ZERODELTA_VENUES) & set(ADAPTER_FOR_ZERODELTA_VENUE)


@pytest.mark.parametrize("spelling", ["binance usdt", "BINANCE-USDT", " Binance  USDT "])
def test_display_name_matching_ignores_case_and_punctuation(spelling):
    adapter = get_adapter(spelling, transport=OFFLINE)
    assert isinstance(adapter, BinanceUsdmAdapter)
    close(adapter)


def test_adapter_kwargs_reach_the_constructor():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    adapter = get_adapter("Bybit USDC", transport=transport, base_url="https://example.invalid")
    try:
        assert adapter.base_url == "https://example.invalid"
    finally:
        close(adapter)
    hip3 = get_adapter("Trade.xyz", transport=transport, instrument_ttl_seconds=1.0)
    try:
        assert hip3.dex == "xyz"
    finally:
        close(hip3)


def test_default_adapters_point_at_mainnet():
    for name, host in (("Binance USDT", "https://fapi.binance.com"), ("Bybit USDT", "https://api.bybit.com"),
                       ("Hyperliquid", "https://api.hyperliquid.xyz")):
        adapter = get_adapter(name, transport=OFFLINE)
        try:
            assert adapter.base_url == host
        finally:
            close(adapter)
