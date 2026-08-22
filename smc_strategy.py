#!/usr/bin/env python3
"""
SMC Strategy — detects Smart Money Concepts setups from OHLC price data:
swing structure (BOS/CHoCH), liquidity pools & sweeps, order blocks,
fair value gaps (FVG), and premium/discount zones.

Usage:
    python3 smc_strategy.py data.csv [--swing 3] [--tolerance 0.0015] [--impulse 1.8]

CSV must have a header row with columns: date,open,high,low,close
"""

import argparse
import sys

from trend_identifier import Candle, load_candles


def find_swings(candles: list[Candle], window: int = 3) -> list[dict]:
    """Fractal swing highs/lows: a bar is a swing point if it's the extreme
    within `window` bars on each side."""
    swings = []
    for i in range(window, len(candles) - window):
        segment = candles[i - window : i + window + 1]
        if candles[i].high == max(c.high for c in segment):
            swings.append({"index": i, "price": candles[i].high, "kind": "high"})
        if candles[i].low == min(c.low for c in segment):
            swings.append({"index": i, "price": candles[i].low, "kind": "low"})
    swings.sort(key=lambda s: s["index"])
    return swings


def classify_structure(candles: list[Candle], swings: list[dict], window: int) -> list[dict]:
    """Walk candles in order, tracking the most recently confirmed swing
    high/low. A close beyond it is a BOS if it continues the current bias,
    or a CHoCH if it flips the bias."""
    highs = sorted((s for s in swings if s["kind"] == "high"), key=lambda s: s["index"])
    lows = sorted((s for s in swings if s["kind"] == "low"), key=lambda s: s["index"])

    events = []
    bias = None
    hi_ptr = lo_ptr = 0
    active_high = active_low = None

    for i, candle in enumerate(candles):
        while hi_ptr < len(highs) and highs[hi_ptr]["index"] + window <= i:
            active_high = highs[hi_ptr]
            hi_ptr += 1
        while lo_ptr < len(lows) and lows[lo_ptr]["index"] + window <= i:
            active_low = lows[lo_ptr]
            lo_ptr += 1

        if active_high is not None and candle.close > active_high["price"]:
            events.append({
                "index": i, "type": "BOS" if bias == "up" else "CHoCH",
                "direction": "up", "level": active_high["price"],
            })
            bias = "up"
            active_high = None

        if active_low is not None and candle.close < active_low["price"]:
            events.append({
                "index": i, "type": "BOS" if bias == "down" else "CHoCH",
                "direction": "down", "level": active_low["price"],
            })
            bias = "down"
            active_low = None

    return events


def find_liquidity_pools(swings: list[dict], tolerance: float = 0.0015) -> list[dict]:
    """Equal highs/lows within `tolerance` of each other: resting liquidity."""
    pools = []
    for kind, pick in (("high", max), ("low", min)):
        points = sorted((s for s in swings if s["kind"] == kind), key=lambda s: s["index"])
        for a, b in zip(points, points[1:]):
            if abs(a["price"] - b["price"]) / a["price"] <= tolerance:
                pools.append({
                    "kind": kind, "level": pick(a["price"], b["price"]),
                    "indices": (a["index"], b["index"]),
                })
    return pools


def find_liquidity_sweeps(candles: list[Candle], pools: list[dict]) -> list[dict]:
    """A wick through a liquidity pool that closes back on the other side."""
    sweeps = []
    for pool in pools:
        start = max(pool["indices"])
        for i in range(start + 1, len(candles)):
            c = candles[i]
            if pool["kind"] == "high" and c.high > pool["level"] and c.close < pool["level"]:
                sweeps.append({"index": i, "kind": "high", "level": pool["level"]})
                break
            if pool["kind"] == "low" and c.low < pool["level"] and c.close > pool["level"]:
                sweeps.append({"index": i, "kind": "low", "level": pool["level"]})
                break
    return sweeps


def find_fvgs(candles: list[Candle]) -> list[dict]:
    """3-candle imbalance: a gap between candle[i].high/low and candle[i+2]'s."""
    fvgs = []
    for i in range(len(candles) - 2):
        c1, c3 = candles[i], candles[i + 2]
        if c1.high < c3.low:
            fvgs.append({"index": i + 1, "kind": "bullish", "bottom": c1.high, "top": c3.low})
        elif c1.low > c3.high:
            fvgs.append({"index": i + 1, "kind": "bearish", "bottom": c3.high, "top": c1.low})
    return fvgs


def find_order_blocks(candles: list[Candle], lookback: int = 10, impulse_mult: float = 1.8) -> list[dict]:
    """The last opposite-colored candle before an impulsive (above-average
    range) move — the zone smart money is expected to return to."""
    ranges = [c.high - c.low for c in candles]
    obs = []
    for i in range(lookback, len(candles)):
        avg_range = sum(ranges[i - lookback : i]) / lookback
        candle = candles[i]
        if abs(candle.close - candle.open) <= impulse_mult * avg_range:
            continue
        bearish_impulse = candle.close < candle.open
        for j in range(i - 1, -1, -1):
            prev = candles[j]
            prev_bullish = prev.close >= prev.open
            if bearish_impulse and prev_bullish:
                obs.append({"index": j, "kind": "bearish_ob", "top": prev.high, "bottom": prev.low, "impulse_index": i})
                break
            if not bearish_impulse and not prev_bullish:
                obs.append({"index": j, "kind": "bullish_ob", "top": prev.high, "bottom": prev.low, "impulse_index": i})
                break
    return obs


def find_latest_setup(candles: list[Candle], window: int = 3, tolerance: float = 0.0015, impulse_mult: float = 1.8) -> dict | None:
    """Combine every detector around the most recent CHoCH to describe the
    current SMC setup, if the pieces (order block / FVG / premium-discount)
    line up."""
    swings = find_swings(candles, window)
    structure = classify_structure(candles, swings, window)
    pools = find_liquidity_pools(swings, tolerance)
    sweeps = find_liquidity_sweeps(candles, pools)
    fvgs = find_fvgs(candles)
    obs = find_order_blocks(candles, impulse_mult=impulse_mult)

    choch_events = [e for e in structure if e["type"] == "CHoCH"]
    if not choch_events:
        return None
    last_choch = choch_events[-1]
    direction = "short" if last_choch["direction"] == "down" else "long"

    ob_kind = "bearish_ob" if direction == "short" else "bullish_ob"
    candidate_obs = [o for o in obs if o["kind"] == ob_kind and o["index"] <= last_choch["index"]]
    ob = candidate_obs[-1] if candidate_obs else None

    fvg_kind = "bearish" if direction == "short" else "bullish"
    candidate_fvgs = [f for f in fvgs if f["kind"] == fvg_kind and f["index"] >= last_choch["index"] - 5]
    fvg = candidate_fvgs[0] if candidate_fvgs else None

    candidate_sweeps = [s for s in sweeps if s["index"] <= last_choch["index"]]
    sweep = candidate_sweeps[-1] if candidate_sweeps else None

    leg = candles[last_choch["index"] :]
    if direction == "short":
        swing_high = sweep["level"] if sweep else last_choch["level"]
        swing_low = min(c.low for c in leg)
    else:
        swing_low = sweep["level"] if sweep else last_choch["level"]
        swing_high = max(c.high for c in leg)

    equilibrium = (swing_high + swing_low) / 2
    last_close = candles[-1].close
    in_zone = last_close > equilibrium if direction == "short" else last_close < equilibrium
    in_ob = ob is not None and ob["bottom"] <= last_close <= ob["top"]
    in_fvg = fvg is not None and fvg["bottom"] <= last_close <= fvg["top"]

    if direction == "short":
        stop_loss = max(swing_high, ob["top"] if ob else swing_high) * 1.001
        take_profit = swing_low * 0.999
    else:
        stop_loss = min(swing_low, ob["bottom"] if ob else swing_low) * 0.999
        take_profit = swing_high * 1.001

    return {
        "direction": direction,
        "choch_index": last_choch["index"],
        "sweep": sweep,
        "order_block": ob,
        "fvg": fvg,
        "equilibrium": equilibrium,
        "premium_discount_zone": "premium" if direction == "short" else "discount",
        "in_premium_or_discount": in_zone,
        "in_order_block": in_ob,
        "in_fvg": in_fvg,
        "entry_ready": in_zone and (in_ob or in_fvg),
        "last_close": last_close,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect SMC (Smart Money Concepts) setups from OHLC CSV data.")
    parser.add_argument("csv_path", help="Path to CSV with columns: date,open,high,low,close")
    parser.add_argument("--swing", type=int, default=3, help="Swing detection window (default: 3)")
    parser.add_argument("--tolerance", type=float, default=0.0015, help="Equal-highs/lows tolerance (default: 0.0015)")
    parser.add_argument("--impulse", type=float, default=1.8, help="Impulse candle size multiplier for order blocks (default: 1.8)")
    args = parser.parse_args()

    candles = load_candles(args.csv_path)
    setup = find_latest_setup(candles, args.swing, args.tolerance, args.impulse)

    if setup is None:
        print("No CHoCH detected yet — not enough structure to evaluate a setup.")
        return

    print(f"Direction        : {setup['direction'].upper()} (CHoCH at candle #{setup['choch_index']})")
    print(f"Liquidity sweep  : {setup['sweep']}")
    print(f"Order block      : {setup['order_block']}")
    print(f"FVG              : {setup['fvg']}")
    print(f"Equilibrium (50%): {setup['equilibrium']:.5f}")
    print(f"In {setup['premium_discount_zone']} zone   : {setup['in_premium_or_discount']}")
    print(f"In order block   : {setup['in_order_block']}")
    print(f"In FVG           : {setup['in_fvg']}")
    print(f"Entry ready      : {setup['entry_ready']}")
    print(f"Last close       : {setup['last_close']:.5f}")
    print(f"Suggested SL     : {setup['stop_loss']:.5f}")
    print(f"Suggested TP     : {setup['take_profit']:.5f}")


if __name__ == "__main__":
    try:
        main()
    except (FileNotFoundError, ValueError, KeyError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
