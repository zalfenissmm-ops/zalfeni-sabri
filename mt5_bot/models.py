"""Broker-neutral data models shared by the live MT5 broker and the paper broker."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SymbolSpec:
    """Everything the bot needs to know about a tradable instrument.

    `tick_value` is the profit, in the *account* currency, of a one-`tick_size`
    move on a 1.00 lot position. MT5 already does the currency conversion, so
    the bot never has to touch exchange rates itself.
    """

    name: str
    digits: int
    point: float
    tick_size: float
    tick_value: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level_points: int
    freeze_level_points: int

    def money_per_point(self, volume: float) -> float:
        """Account-currency value of a one-point move on `volume` lots."""
        if self.tick_size <= 0:
            raise ValueError(f"{self.name}: broker reported tick_size={self.tick_size}")
        return self.tick_value * (self.point / self.tick_size) * volume

    def round_price(self, price: float) -> float:
        return round(price, self.digits)

    def round_volume(self, volume: float) -> float:
        """Snap a lot size onto the broker's volume grid, then clamp to limits."""
        if self.volume_step <= 0:
            return max(self.volume_min, min(volume, self.volume_max))
        steps = round((volume - self.volume_min) / self.volume_step)
        snapped = self.volume_min + steps * self.volume_step
        snapped = max(self.volume_min, min(snapped, self.volume_max))
        # Volume steps are decimal (0.01), so re-round to kill float drift.
        return round(snapped, 8)


@dataclass(frozen=True)
class Tick:
    time: float
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    def spread_points(self, spec: SymbolSpec) -> float:
        return (self.ask - self.bid) / spec.point


@dataclass(frozen=True)
class Candle:
    time: float
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Position:
    ticket: int
    symbol: str
    side: str  # "buy" or "sell"
    volume: float
    price_open: float
    sl: float
    tp: float
    profit: float  # floating P/L in account currency, as reported by the broker
    time_open: float

    @property
    def is_buy(self) -> bool:
        return self.side == "buy"


@dataclass(frozen=True)
class ClosedTrade:
    ticket: int
    symbol: str
    time_close: float
    profit: float  # net: gross P/L + commission + swap


@dataclass(frozen=True)
class OrderResult:
    ok: bool
    ticket: int = 0
    price: float = 0.0
    retcode: int = 0
    comment: str = ""
