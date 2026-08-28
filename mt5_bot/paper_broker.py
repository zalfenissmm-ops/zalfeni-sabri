"""Paper broker: real strategy, simulated money.

Order handling, take-profit/stop-loss fills and P/L accounting are simulated
locally, while prices come from a pluggable feed:

* `SyntheticFeed` - a random walk, so the bot can be exercised anywhere
  (CI, Linux, a laptop with no terminal installed).
* `Mt5Feed` - live MT5 quotes with simulated fills, which is the honest way to
  watch the strategy on your broker's real spreads before risking money.
"""

import itertools
import math
import random
import time
from dataclasses import dataclass
from typing import Protocol

from .models import Candle, ClosedTrade, OrderResult, Position, SymbolSpec, Tick

_SECONDS_PER_BAR = {
    "M1": 60, "M2": 120, "M3": 180, "M5": 300, "M15": 900,
    "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400,
}


class PriceFeed(Protocol):
    def spec(self, symbol: str) -> SymbolSpec: ...

    def tick(self, symbol: str) -> Tick | None: ...

    def candles(self, symbol: str, timeframe: str, count: int) -> list[Candle]: ...


class SyntheticFeed:
    """A seeded random walk with a fixed spread, advanced by the wall clock.

    `speed` compresses time: at speed=60 one real second is one simulated
    minute, which is how you get a day's worth of trades out of a short run.
    """

    def __init__(
        self,
        start_price: float = 1.10000,
        digits: int = 5,
        spread_points: float = 10.0,
        volatility_points_per_second: float = 3.0,
        trend_points_per_second: float = 0.0,
        seed: int = 7,
        speed: float = 1.0,
    ):
        self.digits = digits
        self.point = 10 ** -digits
        self.spread = spread_points * self.point
        self.sigma = volatility_points_per_second * self.point
        self.drift = trend_points_per_second * self.point
        self.speed = speed
        self._rng = random.Random(seed)
        self._start_wall = time.time()
        self._start_price = start_price
        self._mid = start_price
        self._elapsed = 0.0
        self._history: list[tuple[float, float]] = [(self._start_wall, start_price)]

    def _sim_now(self) -> float:
        return (time.time() - self._start_wall) * self.speed

    def _advance(self) -> float:
        """Step the walk forward to the current simulated time."""
        now = self._sim_now()
        step = now - self._elapsed
        if step <= 0:
            return self._mid
        # Reflect off zero so a long run can never produce a negative price.
        shock = self._rng.gauss(0.0, 1.0) * self.sigma * math.sqrt(step)
        self._mid = abs(self._mid + self.drift * step + shock)
        self._elapsed = now
        self._history.append((self._start_wall + now, self._mid))
        if len(self._history) > 100_000:
            del self._history[:50_000]
        return self._mid

    def spec(self, symbol: str) -> SymbolSpec:
        return SymbolSpec(
            name=symbol,
            digits=self.digits,
            point=self.point,
            tick_size=self.point,
            # $1 per point on a 1.00 lot of a 100k-unit 5-digit FX pair.
            tick_value=1.0,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
            stops_level_points=0,
            freeze_level_points=0,
        )

    def tick(self, symbol: str) -> Tick | None:
        mid = self._advance()
        half = self.spread / 2
        return Tick(time=time.time(), bid=round(mid - half, self.digits), ask=round(mid + half, self.digits))

    def candles(self, symbol: str, timeframe: str, count: int) -> list[Candle]:
        """Aggregate the walk's history into bars in a single pass. When the run
        is younger than the requested history, bars are extended backwards from
        the seed price so the strategy still has enough data to warm up."""
        self._advance()
        seconds = _SECONDS_PER_BAR.get(timeframe.upper(), 60)
        newest = int(self._sim_now() // seconds)
        oldest = newest - count + 1

        buckets: dict[int, list[float]] = {}
        for stamp, price in self._history:
            index = int((stamp - self._start_wall) // seconds)
            if index >= oldest:
                buckets.setdefault(index, []).append(price)

        bars = [
            Candle(
                time=self._start_wall + (index + 1) * seconds,
                open=round(prices[0], self.digits),
                high=round(max(prices), self.digits),
                low=round(min(prices), self.digits),
                close=round(prices[-1], self.digits),
            )
            for index in range(oldest, newest + 1)
            if (prices := buckets.get(index))
        ]
        if len(bars) < count:
            bars = self._synthesize_backfill(count - len(bars), seconds, bars)
        return bars

    def _synthesize_backfill(self, missing: int, seconds: int, bars: list[Candle]) -> list[Candle]:
        """Invent plausible history before the run started, so indicators warm up."""
        rng = random.Random(hash((self._start_price, missing, seconds)) & 0xFFFF)
        anchor = bars[0].open if bars else self._mid
        first_time = bars[0].time if bars else time.time()
        back: list[Candle] = []
        price = anchor
        for index in range(missing):
            close = price
            price = abs(price - rng.gauss(0.0, 1.0) * self.sigma * math.sqrt(seconds))
            high = max(price, close) + abs(rng.gauss(0.0, 0.3)) * self.sigma * math.sqrt(seconds)
            low = min(price, close) - abs(rng.gauss(0.0, 0.3)) * self.sigma * math.sqrt(seconds)
            back.append(
                Candle(
                    time=first_time - (index + 1) * seconds,
                    open=price,
                    high=round(high, self.digits),
                    low=round(low, self.digits),
                    close=round(close, self.digits),
                )
            )
        back.reverse()
        return back + bars


class Mt5Feed:
    """Live MT5 quotes, so paper trading runs on your broker's real spreads."""

    def __init__(self, mt5_broker):
        self.broker = mt5_broker

    def spec(self, symbol: str) -> SymbolSpec:
        return self.broker.symbol_spec(symbol)

    def tick(self, symbol: str) -> Tick | None:
        return self.broker.tick(symbol)

    def candles(self, symbol: str, timeframe: str, count: int) -> list[Candle]:
        return self.broker.candles(symbol, timeframe, count)


@dataclass
class _PaperPosition:
    ticket: int
    symbol: str
    side: str
    volume: float
    price_open: float
    sl: float
    tp: float
    time_open: float
    profit: float = 0.0


class PaperBroker:
    def __init__(self, cfg, feed: PriceFeed, starting_balance: float = 1000.0):
        self.cfg = cfg
        self.feed = feed
        self.balance = starting_balance
        self._positions: dict[int, _PaperPosition] = {}
        self._closed: list[ClosedTrade] = []
        self._tickets = itertools.count(1)
        self._last_tick: dict[str, Tick] = {}

    # --- lifecycle -------------------------------------------------------

    def connect(self) -> None:
        if hasattr(self.feed, "broker"):
            self.feed.broker.connect()

    def shutdown(self) -> None:
        if hasattr(self.feed, "broker"):
            self.feed.broker.shutdown()

    # --- market data -----------------------------------------------------

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        return self.feed.spec(symbol)

    def tick(self, symbol: str) -> Tick | None:
        quote = self.feed.tick(symbol)
        if quote is not None:
            self._last_tick[symbol] = quote
            self._settle(symbol, quote)
        return quote

    def candles(self, symbol: str, timeframe: str, count: int) -> list[Candle]:
        return self.feed.candles(symbol, timeframe, count)

    # --- accounting ------------------------------------------------------

    def _settle(self, symbol: str, quote: Tick) -> None:
        """Mark open positions to market and fill any TP/SL the quote crossed."""
        spec = self.symbol_spec(symbol)
        for ticket, position in list(self._positions.items()):
            if position.symbol != symbol:
                continue
            exit_price = quote.bid if position.side == "buy" else quote.ask
            position.profit = self._profit(position, spec, exit_price)

            hit_tp = exit_price >= position.tp if position.side == "buy" else exit_price <= position.tp
            hit_sl = exit_price <= position.sl if position.side == "buy" else exit_price >= position.sl
            if position.tp and hit_tp:
                self._book(ticket, spec, position.tp)
            elif position.sl and hit_sl:
                self._book(ticket, spec, position.sl)

    def _profit(self, position: _PaperPosition, spec: SymbolSpec, exit_price: float) -> float:
        move = exit_price - position.price_open
        if position.side == "sell":
            move = -move
        return (move / spec.point) * spec.money_per_point(position.volume)

    def _book(self, ticket: int, spec: SymbolSpec, exit_price: float) -> None:
        position = self._positions.pop(ticket)
        gross = self._profit(position, spec, exit_price)
        net = gross - self.cfg.commission_per_lot_usd * position.volume
        self.balance += net
        self._closed.append(
            ClosedTrade(ticket=ticket, symbol=position.symbol, time_close=time.time(), profit=net)
        )

    def equity(self) -> float:
        return self.balance + sum(p.profit for p in self._positions.values())

    def positions(self) -> list[Position]:
        for symbol in {p.symbol for p in self._positions.values()}:
            self.tick(symbol)  # refresh floating P/L and fill pending TP/SL
        return [
            Position(
                ticket=p.ticket,
                symbol=p.symbol,
                side=p.side,
                volume=p.volume,
                price_open=p.price_open,
                sl=p.sl,
                tp=p.tp,
                profit=p.profit,
                time_open=p.time_open,
            )
            for p in self._positions.values()
        ]

    def closed_trades_since(self, since: float) -> list[ClosedTrade]:
        return [t for t in self._closed if t.time_close >= since]

    # --- trading ---------------------------------------------------------

    def open_position(
        self, symbol: str, side: str, volume: float, sl: float, tp: float, comment: str
    ) -> OrderResult:
        quote = self.tick(symbol)
        if quote is None:
            return OrderResult(ok=False, comment="no quote available")
        price = quote.ask if side == "buy" else quote.bid
        ticket = next(self._tickets)
        self._positions[ticket] = _PaperPosition(
            ticket=ticket,
            symbol=symbol,
            side=side,
            volume=volume,
            price_open=price,
            sl=sl,
            tp=tp,
            time_open=time.time(),
        )
        return OrderResult(ok=True, ticket=ticket, price=price, comment=comment)

    def close_position(self, ticket: int) -> OrderResult:
        position = self._positions.get(ticket)
        if position is None:
            return OrderResult(ok=False, comment=f"position {ticket} is already gone")
        quote = self._last_tick.get(position.symbol) or self.feed.tick(position.symbol)
        if quote is None:
            return OrderResult(ok=False, comment="no quote available")
        exit_price = quote.bid if position.side == "buy" else quote.ask
        self._book(ticket, self.symbol_spec(position.symbol), exit_price)
        return OrderResult(ok=True, ticket=ticket, price=exit_price)

    def modify_position(self, ticket: int, sl: float, tp: float) -> OrderResult:
        position = self._positions.get(ticket)
        if position is None:
            return OrderResult(ok=False, comment=f"position {ticket} is already gone")
        position.sl = sl
        position.tp = tp
        return OrderResult(ok=True, ticket=ticket)
