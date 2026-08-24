#!/usr/bin/env python3
"""
Trend Identifier — determines market trend direction and strength from OHLC price data.

Combines three confirming signals (no single indicator is trusted alone):
  1. Swing structure: Higher-Highs/Higher-Lows vs Lower-Highs/Lower-Lows.
  2. Price position relative to an EMA.
  3. ADX to gauge whether the trend has enough strength to trust, or the
     market is ranging.

Usage:
    python3 trend_identifier.py data.csv [--ema 50] [--adx-period 14] [--swing 5]

CSV must have a header row with columns: date,open,high,low,close
"""

import argparse
import csv
import sys
from dataclasses import dataclass


@dataclass
class Candle:
    date: str
    open: float
    high: float
    low: float
    close: float


def load_candles(path: str) -> list[Candle]:
    candles = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            candles.append(
                Candle(
                    date=row["date"],
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                )
            )
    if len(candles) < 2:
        raise ValueError("Need at least 2 rows of price data")
    return candles


def ema(values: list[float], period: int) -> list[float]:
    k = 2 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def wilder_smooth_sum(values: list[float], period: int) -> list[float]:
    """Wilder smoothing for TR/+DM/-DM: seeded with the raw sum of the first
    `period` values, then each step keeps a running sum-like total."""
    result = [sum(values[:period])]
    for v in values[period:]:
        result.append(result[-1] - result[-1] / period + v)
    return result


def wilder_smooth_avg(values: list[float], period: int) -> list[float]:
    """Wilder smoothing for DX -> ADX: seeded with the average of the first
    `period` values, then each step is a weighted running average."""
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append((result[-1] * (period - 1) + v) / period)
    return result


def adx_series(candles: list[Candle], period: int = 14) -> list[float | None]:
    """ADX aligned to candle index: element i is the value a trader could read
    at candle i's close, or None while Wilder's warm-up (2*period-1 candles)
    is still filling. Aligned indexing is what lets the backtest read ADX at an
    arbitrary bar without recomputing the whole series."""
    aligned: list[float | None] = [None] * len(candles)
    if len(candles) < period * 2:
        return aligned

    plus_dm, minus_dm, tr = [], [], []
    for prev, cur in zip(candles, candles[1:]):
        up_move = cur.high - prev.high
        down_move = prev.low - cur.low
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        tr.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))

    smoothed_tr = wilder_smooth_sum(tr, period)
    smoothed_plus_dm = wilder_smooth_sum(plus_dm, period)
    smoothed_minus_dm = wilder_smooth_sum(minus_dm, period)

    dx_values = []
    for t, pdm, mdm in zip(smoothed_tr, smoothed_plus_dm, smoothed_minus_dm):
        if t == 0:
            dx_values.append(0.0)
            continue
        plus_di = 100 * pdm / t
        minus_di = 100 * mdm / t
        di_sum = plus_di + minus_di
        dx_values.append(0.0 if di_sum == 0 else 100 * abs(plus_di - minus_di) / di_sum)

    values = wilder_smooth_avg(dx_values, period)
    offset = 2 * period - 1  # first candle index the smoothed DX average covers
    for j, value in enumerate(values):
        aligned[offset + j] = value
    return aligned


def adx(candles: list[Candle], period: int = 14) -> float:
    """Latest ADX value using Wilder's original method."""
    if len(candles) < period * 2:
        raise ValueError(f"Need at least {period * 2} candles to compute ADX({period})")
    return adx_series(candles, period)[-1]


def swing_points(candles: list[Candle], window: int = 5) -> tuple[list[float], list[float]]:
    """Fractal-style swing highs/lows: a bar is a swing point if it's the
    extreme within `window` bars on each side."""
    highs, lows = [], []
    for i in range(window, len(candles) - window):
        segment = candles[i - window : i + window + 1]
        if candles[i].high == max(c.high for c in segment):
            highs.append(candles[i].high)
        if candles[i].low == min(c.low for c in segment):
            lows.append(candles[i].low)
    return highs, lows


def structure_bias(highs: list[float], lows: list[float]) -> str:
    """Compare the last two swing highs and last two swing lows."""
    if len(highs) < 2 or len(lows) < 2:
        return "unknown"
    higher_highs = highs[-1] > highs[-2]
    higher_lows = lows[-1] > lows[-2]
    if higher_highs and higher_lows:
        return "up"
    if not higher_highs and not higher_lows:
        return "down"
    return "mixed"


def identify_trend(candles: list[Candle], ema_period: int = 50, adx_period: int = 14, swing_window: int = 5) -> dict:
    closes = [c.close for c in candles]
    ema_values = ema(closes, ema_period)
    price_vs_ema = "up" if closes[-1] > ema_values[-1] else "down"

    highs, lows = swing_points(candles, swing_window)
    structure = structure_bias(highs, lows)

    try:
        adx_value = adx(candles, adx_period)
    except ValueError:
        adx_value = None

    votes_up = (price_vs_ema == "up") + (structure == "up")
    votes_down = (price_vs_ema == "down") + (structure == "down")

    if votes_up > votes_down:
        direction = "Uptrend"
    elif votes_down > votes_up:
        direction = "Downtrend"
    else:
        direction = "Sideways / Unclear"

    if adx_value is None:
        strength = "unknown (not enough data)"
    elif adx_value < 20:
        strength = "weak / ranging"
    elif adx_value < 25:
        strength = "developing"
    else:
        strength = "strong"

    return {
        "direction": direction,
        "strength": strength,
        "adx": adx_value,
        "price_vs_ema": price_vs_ema,
        "ema_period": ema_period,
        "structure": structure,
        "last_close": closes[-1],
        "last_ema": ema_values[-1],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Identify market trend from OHLC CSV data.")
    parser.add_argument("csv_path", help="Path to CSV with columns: date,open,high,low,close")
    parser.add_argument("--ema", type=int, default=50, help="EMA period (default: 50)")
    parser.add_argument("--adx-period", type=int, default=14, help="ADX period (default: 14)")
    parser.add_argument("--swing", type=int, default=5, help="Swing detection window (default: 5)")
    args = parser.parse_args()

    candles = load_candles(args.csv_path)
    result = identify_trend(candles, args.ema, args.adx_period, args.swing)

    print(f"Direction : {result['direction']}")
    print(f"Strength  : {result['strength']} (ADX={result['adx']:.2f})" if result["adx"] is not None else f"Strength  : {result['strength']}")
    print(f"Price vs EMA{result['ema_period']}: {result['price_vs_ema']} (close={result['last_close']:.5f}, ema={result['last_ema']:.5f})")
    print(f"Swing structure: {result['structure']}")


if __name__ == "__main__":
    try:
        main()
    except (FileNotFoundError, ValueError, KeyError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
