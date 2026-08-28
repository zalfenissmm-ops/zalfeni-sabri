"""Indicators used by the scalping strategy.

Every function returns a list the same length as its input, with `None` in the
leading slots that do not have enough history yet. That keeps indexing aligned
with the candle list, so `values[-1]` is always "the latest bar".
"""

from .models import Candle


def ema(values: list[float], period: int) -> list[float | None]:
    """Exponential moving average, seeded with a simple average of the first
    `period` values so the early bars are not dominated by a single price."""
    if period < 1:
        raise ValueError("EMA period must be >= 1")
    if len(values) < period:
        return [None] * len(values)

    k = 2 / (period + 1)
    out: list[float | None] = [None] * (period - 1)
    current = sum(values[:period]) / period
    out.append(current)
    for value in values[period:]:
        current = value * k + current * (1 - k)
        out.append(current)
    return out


def rsi(closes: list[float], period: int = 14) -> list[float | None]:
    """Wilder's RSI."""
    if period < 1:
        raise ValueError("RSI period must be >= 1")
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out

    gains, losses = [], []
    for prev, cur in zip(closes, closes[1:]):
        change = cur - prev
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = _rsi_from_averages(avg_gain, avg_loss)

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = _rsi_from_averages(avg_gain, avg_loss)
    return out


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def true_range(prev: Candle, cur: Candle) -> float:
    return max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))


def atr(candles: list[Candle], period: int = 14) -> list[float | None]:
    """Wilder's Average True Range, in price units."""
    if period < 1:
        raise ValueError("ATR period must be >= 1")
    out: list[float | None] = [None] * len(candles)
    if len(candles) <= period:
        return out

    ranges = [true_range(prev, cur) for prev, cur in zip(candles, candles[1:])]
    current = sum(ranges[:period]) / period
    out[period] = current
    for i in range(period, len(ranges)):
        current = (current * (period - 1) + ranges[i]) / period
        out[i + 1] = current
    return out
