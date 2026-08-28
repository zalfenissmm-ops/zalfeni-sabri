"""Shared test fixtures: a scripted price feed and candle builders."""

import time

from mt5_bot.models import Candle, SymbolSpec, Tick

FX_SPEC = SymbolSpec(
    name="EURUSD",
    digits=5,
    point=1e-5,
    tick_size=1e-5,
    tick_value=1.0,  # $1 per point on 1.00 lot
    volume_min=0.01,
    volume_max=100.0,
    volume_step=0.01,
    stops_level_points=0,
    freeze_level_points=0,
)


def candles_from(prices: list[float], wick: float = 0.00002, start: float = 0.0) -> list[Candle]:
    """Turn a close-price path into bars, each opening at the previous close."""
    bars = []
    previous = prices[0]
    for index, close in enumerate(prices):
        bars.append(
            Candle(
                time=start + index * 60,
                open=previous,
                high=max(previous, close) + wick,
                low=min(previous, close) - wick,
                close=close,
            )
        )
        previous = close
    return bars


class ScriptedFeed:
    """A feed whose quotes and bars the test sets directly."""

    def __init__(self, bars: list[Candle], bid: float, ask: float, spec: SymbolSpec = FX_SPEC):
        self.bars = bars
        self.bid = bid
        self.ask = ask
        self._spec = spec
        self.now = time.time()

    def move_to(self, bid: float, ask: float) -> None:
        self.bid, self.ask = bid, ask

    def spec(self, symbol: str) -> SymbolSpec:
        return self._spec

    def tick(self, symbol: str) -> Tick:
        return Tick(time=self.now, bid=self.bid, ask=self.ask)

    def candles(self, symbol: str, timeframe: str, count: int) -> list[Candle]:
        return self.bars[-count:]


def wave(n: int = 60, base: float = 1.10000, up: float = 0.00010,
         down: float = 0.00009, period: int = 5, ups: int = 3, sign: int = 1) -> list[float]:
    """A trending path that still retraces, so RSI stays in a realistic range
    instead of pinning at an extreme the way a straight line does."""
    prices, price = [], base
    for index in range(n):
        price += sign * up if index % period < ups else -sign * down
        prices.append(round(price, 5))
    return prices
