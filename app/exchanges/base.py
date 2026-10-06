"""ExchangeAdapter: the one interface strategy, execution and risk code use for a venue (section 16).

Exchange-specific semantics stay inside adapters. These adapters implement the PUBLIC (market data) half only:
their account/order methods raise ExchangeNotSupported, so nothing can place an order through them by accident.
Paper mode wraps them in PaperVenue; live mode wraps them in LiveVenue together with the venue's SIGNED client
(app/exchanges/private), which is the only code that can place a real order.
"""

from abc import ABC, abstractmethod
from decimal import Decimal

from app.domain.market import (
    AccountState,
    Candle,
    FeeSchedule,
    FundingInfo,
    InstrumentMeta,
    OrderBook,
    OrderState,
    PositionState,
    Side,
    Ticker,
)


class ExchangeError(Exception):
    """Base class for adapter errors. Messages never contain keys, signatures or headers."""


class ExchangeNotSupported(ExchangeError):
    """The operation is not implemented for this adapter or phase."""


class ExchangeRequestError(ExchangeError):
    """Transport failure, timeout, HTTP error or a response the adapter cannot interpret."""


class SymbolNotFound(ExchangeError):
    """No tradable perpetual matches the requested base/quote/settle on this venue."""


class ExchangeAdapter(ABC):
    name: str  # matches the venue key used in the venue map, e.g. "binance_usdt_m"

    # ---- streams (optional; REST polling is acceptable for the MVP) --------------------------
    async def connect_market_streams(self) -> None:
        raise ExchangeNotSupported("market streams are not implemented")

    async def connect_private_streams(self) -> None:
        raise ExchangeNotSupported("private streams are not implemented (Phase 8)")

    # ---- public market data ------------------------------------------------------------------
    @abstractmethod
    async def get_server_time(self): ...

    @abstractmethod
    async def list_instruments(self) -> list[InstrumentMeta]: ...

    async def get_instrument_metadata(self, symbol: str) -> InstrumentMeta:
        for inst in await self.list_instruments():
            if inst.symbol == symbol:
                return inst
        raise SymbolNotFound(f"{self.name}: unknown symbol {symbol}")

    async def resolve_symbol(self, base: str, quote: str, settle: str) -> InstrumentMeta:
        """Find the tradable perpetual for BASE/QUOTE:SETTLE from the venue's own instrument list.

        Never guess a symbol from a display name (section 0.1).
        """
        matches = [
            i for i in await self.list_instruments()
            if i.is_perpetual and i.base.upper() == base.upper()
            and i.quote.upper() == quote.upper() and i.settle.upper() == settle.upper()
        ]
        tradable = [i for i in matches if i.is_tradable]
        if len(tradable) == 1:
            return tradable[0]
        if not matches:
            raise SymbolNotFound(f"{self.name}: no perpetual {base}/{quote}:{settle}")
        if not tradable:
            raise SymbolNotFound(f"{self.name}: {base}/{quote}:{settle} exists but is not tradable")
        raise SymbolNotFound(f"{self.name}: ambiguous match for {base}/{quote}:{settle}")

    @abstractmethod
    async def get_funding_info(self, symbol: str) -> FundingInfo: ...

    @abstractmethod
    async def get_funding_history(self, symbol: str, limit: int = 100) -> list[tuple]:
        """[(funding_time UTC, funding_rate fraction)], oldest first."""

    async def get_next_funding_time(self, symbol: str):
        return (await self.get_funding_info(symbol)).next_funding_time

    @abstractmethod
    async def get_ticker(self, symbol: str) -> Ticker: ...

    @abstractmethod
    async def get_order_book(self, symbol: str, depth: int = 50) -> OrderBook: ...

    @abstractmethod
    async def get_candles(self, symbol: str, timeframe: str = "1m", limit: int = 200) -> list[Candle]:
        """Oldest first. Only the 1m timeframe is required by the volatility filter."""

    async def get_closed_candles(self, symbol: str, timeframe: str = "1m", limit: int = 200) -> list[Candle]:
        return await self.get_candles(symbol, timeframe, limit)

    async def get_fee_schedule(self, symbol: str) -> FeeSchedule:
        raise ExchangeNotSupported("fee schedule needs account access (Phase 8)")

    # ---- account and orders (Phase 8) --------------------------------------------------------
    async def get_account_equity(self) -> AccountState:
        raise ExchangeNotSupported("account access is Phase 8")

    async def get_available_balance(self) -> Decimal:
        raise ExchangeNotSupported("account access is Phase 8")

    async def get_positions(self) -> list[PositionState]:
        raise ExchangeNotSupported("account access is Phase 8")

    async def get_position(self, symbol: str) -> PositionState | None:
        raise ExchangeNotSupported("account access is Phase 8")

    async def get_open_orders(self, symbol: str | None = None) -> list[OrderState]:
        raise ExchangeNotSupported("account access is Phase 8")

    async def get_order(self, order_id: str | None = None, client_order_id: str | None = None) -> OrderState:
        raise ExchangeNotSupported("account access is Phase 8")

    async def get_fills(self, symbol: str | None = None) -> list:
        raise ExchangeNotSupported("account access is Phase 8")

    async def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        raise ExchangeNotSupported("order placement is Phase 8")

    async def place_order(self, symbol: str, side: Side, quantity: Decimal, *, client_order_id: str,
                          price: Decimal | None = None, reduce_only: bool = False) -> OrderState:
        raise ExchangeNotSupported("order placement is Phase 8")

    async def cancel_order(self, symbol: str, *, order_id: str | None = None,
                           client_order_id: str | None = None) -> OrderState:
        raise ExchangeNotSupported("order placement is Phase 8")

    async def close(self) -> None:
        return None
