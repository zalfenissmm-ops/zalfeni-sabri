"""The broker interface the engine talks to.

Both the live MT5 broker and the paper broker implement it, so the exact same
strategy and risk code runs in simulation and on a real account.
"""

from typing import Protocol

from .models import ClosedTrade, OrderResult, Position, SymbolSpec, Tick


class Broker(Protocol):
    def connect(self) -> None: ...

    def shutdown(self) -> None: ...

    def symbol_spec(self, symbol: str) -> SymbolSpec: ...

    def tick(self, symbol: str) -> Tick | None: ...

    def candles(self, symbol: str, timeframe: str, count: int) -> list: ...

    def positions(self) -> list[Position]: ...

    def equity(self) -> float: ...

    def closed_trades_since(self, since: float) -> list[ClosedTrade]: ...

    def open_position(
        self, symbol: str, side: str, volume: float, sl: float, tp: float, comment: str
    ) -> OrderResult: ...

    def close_position(self, ticket: int) -> OrderResult: ...

    def modify_position(self, ticket: int, sl: float, tp: float) -> OrderResult: ...


class BrokerError(RuntimeError):
    """Raised when the terminal cannot be reached or a symbol is unusable."""
