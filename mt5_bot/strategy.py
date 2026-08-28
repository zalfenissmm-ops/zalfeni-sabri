"""Entry logic.

Two triggers, both taken only in the direction of the fast/slow EMA trend:

* **Pullback**: price dips to the fast EMA and closes back through it.
* **Breakout**: price closes beyond the last few bars' range.

Both are evaluated on *closed* bars only. The bar the broker is still building
repaints on every tick, and acting on it is the classic way to get a signal that
disappears a second after the order goes in.
"""

from dataclasses import dataclass

from .config import Config
from .indicators import atr, ema, rsi
from .models import Candle


@dataclass(frozen=True)
class Signal:
    side: str | None  # "buy", "sell", or None
    reason: str
    atr: float = 0.0

    @property
    def actionable(self) -> bool:
        return self.side is not None


def evaluate(cfg: Config, candles: list[Candle]) -> Signal:
    bars = candles[:-1]  # drop the bar still forming
    needed = max(cfg.ema_slow, cfg.rsi_period, cfg.atr_period, cfg.breakout_lookback) + 2
    if len(bars) < needed:
        return Signal(None, f"only {len(bars)} closed bars, need {needed}")

    closes = [b.close for b in bars]
    fast = ema(closes, cfg.ema_fast)
    slow = ema(closes, cfg.ema_slow)
    momentum = rsi(closes, cfg.rsi_period)
    volatility = atr(bars, cfg.atr_period)

    if None in (fast[-1], slow[-1], fast[-2], momentum[-1], volatility[-1]):
        return Signal(None, "indicators not warmed up")

    current_atr = volatility[-1]
    if current_atr <= 0:
        return Signal(None, "flat market (ATR is zero)")

    trend = "buy" if fast[-1] > slow[-1] else "sell"
    strength = momentum[-1]
    if trend == "buy" and strength > cfg.rsi_buy_max:
        return Signal(None, f"uptrend but RSI {strength:.0f} is overbought", current_atr)
    if trend == "sell" and strength < cfg.rsi_sell_min:
        return Signal(None, f"downtrend but RSI {strength:.0f} is oversold", current_atr)

    last, prev = bars[-1], bars[-2]
    if cfg.use_pullback_entries and _pullback(trend, last, prev, fast[-1], fast[-2]):
        return Signal(trend, f"pullback into EMA{cfg.ema_fast} (RSI {strength:.0f})", current_atr)
    if cfg.use_breakout_entries and _breakout(trend, bars, cfg.breakout_lookback):
        return Signal(trend, f"{cfg.breakout_lookback}-bar breakout (RSI {strength:.0f})", current_atr)
    return Signal(None, f"{trend} trend, no trigger yet", current_atr)


def _pullback(trend: str, last: Candle, prev: Candle, fast_now: float, fast_prev: float) -> bool:
    """Previous bar sat on the wrong side of the fast EMA, this one closed back through it."""
    if trend == "buy":
        return prev.close <= fast_prev and last.close > fast_now
    return prev.close >= fast_prev and last.close < fast_now


def _breakout(trend: str, bars: list[Candle], lookback: int) -> bool:
    window = bars[-1 - lookback : -1]
    if not window:
        return False
    if trend == "buy":
        return bars[-1].close > max(b.high for b in window)
    return bars[-1].close < min(b.low for b in window)
