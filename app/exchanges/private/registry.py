"""Which venues can trade live, and their signed clients.

Live trading is limited to USDT-margined perpetuals on these Zero Delta venues (the public adapter of each is in
app/exchanges/registry.py). USDC- and USD1-margined markets stay paper-only: their margin and fees are not in USDT.
"""

from app.exchanges.private.base import PrivateClient
from app.exchanges.private.binance import BinancePrivate
from app.exchanges.private.bitget import BitgetPrivate
from app.exchanges.private.blofin import BlofinPrivate
from app.exchanges.private.bybit import BybitPrivate
from app.exchanges.private.credentials import LiveCredentials
from app.exchanges.private.mexc import MexcPrivate

PRIVATE_CLIENTS: dict[str, type[PrivateClient]] = {
    "binance_usdt_m": BinancePrivate,
    "bybit_linear": BybitPrivate,
    "mexc_contract": MexcPrivate,
    "blofin_swap": BlofinPrivate,
    "bitget_usdt": BitgetPrivate,
}

# Zero Delta display names that may be traded live (USDT-margined only).
LIVE_ZERODELTA_VENUES = frozenset({"Binance USDT", "Bybit USDT", "MEXC USDT", "BloFin USDT", "Bitget"})


def build_private_clients(credentials: LiveCredentials) -> dict[str, PrivateClient]:
    """A signed client for every venue that has complete credentials."""
    clients: dict[str, PrivateClient] = {}
    for name in credentials.configured():
        cls = PRIVATE_CLIENTS.get(name)
        creds = credentials.for_venue(name)
        if cls is not None and creds is not None:
            clients[name] = cls(creds)
    return clients
