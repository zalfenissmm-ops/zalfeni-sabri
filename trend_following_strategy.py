#!/usr/bin/env python3
"""
Trend Following Strategy — generates LONG/SHORT/FLAT signals from OHLC price
data by combining an EMA crossover, price structure and an ADX strength
filter, then trails a chandelier-style ATR stop instead of a fixed target so
a winning position can keep riding the trend.

Signal rules:
  - LONG  : fast EMA above slow EMA, price above the fast EMA, swing
            structure not bearish, and ADX confirms the trend is strong.
  - SHORT : the mirror image (fast EMA below slow EMA, price below it,
            structure not bullish, ADX strong).
  - FLAT  : otherwise (weak/ranging ADX, or the signals disagree).

Usage:
    python3 trend_following_strategy.py data.csv [--fast 20] [--slow 50]
        [--adx-period 14] [--adx-threshold 25] [--atr-period 14]
        [--atr-mult 3] [--swing 5]

CSV must have a header row with columns: date,open,high,low,close
"""

import argparse
import sys

from trend_identifier import adx, ema, load_candles, structure_bias, swing_points


def atr(candles, period: int = 14) -> list[float]:
    """Wilder's ATR: smoothed average true range."""
    trs = []
    for prev, cur in zip(candles, candles[1:]):
        trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    if len(trs) < period:
        raise ValueError(f"Need at least {period + 1} candles to compute ATR({period})")

    result = [sum(trs[:period]) / period]
    for t in trs[period:]:
        result.append((result[-1] * (period - 1) + t) / period)
    return result


def chandelier_stop(candles, direction: str, atr_value: float, atr_mult: float = 3.0, lookback: int = 22) -> float:
    """Trailing exit: highest-high (long) / lowest-low (short) over the last
    `lookback` bars, offset by atr_mult * ATR — lets the stop follow price
    without capping the upside like a fixed take-profit would."""
    window = candles[-lookback:] if len(candles) >= lookback else candles
    if direction == "long":
        return max(c.high for c in window) - atr_mult * atr_value
    return min(c.low for c in window) + atr_mult * atr_value


def evaluate_trend_following(
    candles,
    fast: int = 20,
    slow: int = 50,
    adx_period: int = 14,
    adx_threshold: float = 25.0,
    atr_period: int = 14,
    atr_mult: float = 3.0,
    swing_window: int = 5,
) -> dict:
    closes = [c.close for c in candles]
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)

    trend_up = ema_fast[-1] > ema_slow[-1]
    price_above_fast = closes[-1] > ema_fast[-1]

    highs, lows = swing_points(candles, swing_window)
    structure = structure_bias(highs, lows)

    adx_value = adx(candles, adx_period)
    strong = adx_value >= adx_threshold

    atr_value = atr(candles, atr_period)[-1]

    signal = "FLAT"
    if strong and trend_up and price_above_fast and structure != "down":
        signal = "LONG"
    elif strong and not trend_up and not price_above_fast and structure != "up":
        signal = "SHORT"

    trailing_stop = None
    if signal in ("LONG", "SHORT"):
        direction = "long" if signal == "LONG" else "short"
        trailing_stop = chandelier_stop(candles, direction, atr_value, atr_mult)

    return {
        "signal": signal,
        "fast_period": fast,
        "slow_period": slow,
        "ema_fast": ema_fast[-1],
        "ema_slow": ema_slow[-1],
        "structure": structure,
        "adx_period": adx_period,
        "adx": adx_value,
        "adx_strong": strong,
        "atr_period": atr_period,
        "atr": atr_value,
        "last_close": closes[-1],
        "trailing_stop": trailing_stop,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Trend-following LONG/SHORT signal generator from OHLC CSV data.")
    parser.add_argument("csv_path", help="Path to CSV with columns: date,open,high,low,close")
    parser.add_argument("--fast", type=int, default=20, help="Fast EMA period (default: 20)")
    parser.add_argument("--slow", type=int, default=50, help="Slow EMA period (default: 50)")
    parser.add_argument("--adx-period", type=int, default=14, help="ADX period (default: 14)")
    parser.add_argument("--adx-threshold", type=float, default=25.0, help="Minimum ADX to trust the trend (default: 25)")
    parser.add_argument("--atr-period", type=int, default=14, help="ATR period (default: 14)")
    parser.add_argument("--atr-mult", type=float, default=3.0, help="ATR multiple for the trailing stop (default: 3.0)")
    parser.add_argument("--swing", type=int, default=5, help="Swing detection window (default: 5)")
    args = parser.parse_args()

    candles = load_candles(args.csv_path)
    result = evaluate_trend_following(
        candles,
        args.fast,
        args.slow,
        args.adx_period,
        args.adx_threshold,
        args.atr_period,
        args.atr_mult,
        args.swing,
    )

    print(f"Signal              : {result['signal']}")
    print(f"EMA{result['fast_period']}/EMA{result['slow_period']}        : {result['ema_fast']:.5f} / {result['ema_slow']:.5f}")
    print(f"Structure           : {result['structure']}")
    strength = "strong" if result["adx_strong"] else "weak / ranging"
    print(f"ADX({result['adx_period']})            : {result['adx']:.2f} ({strength})")
    print(f"ATR({result['atr_period']})            : {result['atr']:.5f}")
    print(f"Last close          : {result['last_close']:.5f}")
    if result["trailing_stop"] is not None:
        print(f"Trailing stop       : {result['trailing_stop']:.5f}")
    else:
        print("Trailing stop       : n/a (no active signal)")


if __name__ == "__main__":
    try:
        main()
    except (FileNotFoundError, ValueError, KeyError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
