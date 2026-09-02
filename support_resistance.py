#!/usr/bin/env python3
"""
Support & Resistance — finds price zones (not single lines) from OHLC data and
ranks them by how much they actually matter.

A level is only worth watching if price has repeatedly reacted to it, so every
zone is scored on five things instead of being drawn by eye:
  1. Touches      — how many swing points cluster inside the same band.
  2. Reaction     — how far price travelled away from the zone after touching it,
                    measured against what a random walk covers over the same
                    horizon (ATR x sqrt(bars)), so noise does not score marks.
  3. Recency      — a zone touched last week beats one from a year ago.
  4. Flip         — the zone acted as both support and resistance (role reversal).
  5. Round number — proximity to a psychological level (1.2000, 100, 50 ...).

Usage:
    python3 support_resistance.py data.csv [--swing 3] [--min-score 60]
                                           [--atr-period 14] [--top 6]

CSV must have a header row with columns: date,open,high,low,close
"""

import argparse
import math
import sys

from trend_identifier import Candle, load_candles

# Score weights — must sum to 1.0. Reaction carries the most weight because it
# is the component that actually separates a real level from random wandering.
WEIGHTS = {"reaction": 0.40, "touches": 0.20, "recency": 0.20, "flip": 0.10, "round": 0.10}

TOUCHES_FOR_FULL_SCORE = 5
REACTION_LOOKAHEAD = 10
# Reaction is measured against what a random walk covers over the same horizon
# (ATR x sqrt(bars)): 1.0 is pure noise, 2.0 is twice that.
REACTION_NOISE_FLOOR = 1.0
REACTION_FULL_SCORE = 2.0
FLIP_LOOKBACK = 40


def atr(candles: list[Candle], period: int = 14) -> float:
    """Average True Range — used as the unit for zone width and reactions."""
    if len(candles) < period + 1:
        raise ValueError(f"Need at least {period + 1} candles to compute ATR({period})")
    true_ranges = []
    for prev, cur in zip(candles, candles[1:]):
        true_ranges.append(
            max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
        )
    return sum(true_ranges[-period:]) / period


def find_pivots(candles: list[Candle], window: int = 3) -> list[dict]:
    """Fractal swing highs/lows: a bar is a pivot if it's the extreme within
    `window` bars on each side. These are the raw candidates for S/R."""
    pivots = []
    for i in range(window, len(candles) - window):
        segment = candles[i - window : i + window + 1]
        if candles[i].high == max(c.high for c in segment):
            pivots.append({"index": i, "price": candles[i].high, "kind": "high"})
        if candles[i].low == min(c.low for c in segment):
            pivots.append({"index": i, "price": candles[i].low, "kind": "low"})
    pivots.sort(key=lambda p: p["index"])
    return pivots


def cluster_pivots(pivots: list[dict], tolerance: float, band: float) -> list[list[dict]]:
    """Group pivots that sit within the same price band. Highs and lows are
    clustered together on purpose: a broken resistance becomes support, and
    both touches belong to the same zone.

    A pivot joins the current cluster when it is within one band of the
    previous pivot *and* the whole cluster stays under two bands wide — near
    misses still merge, but a long chain of pivots can't drift into one
    meaninglessly wide "zone"."""
    clusters: list[list[dict]] = []
    for pivot in sorted(pivots, key=lambda p: p["price"]):
        if clusters:
            cluster = clusters[-1]
            width = max(tolerance * cluster[0]["price"], band)
            near_previous = pivot["price"] - cluster[-1]["price"] <= width
            stays_narrow = pivot["price"] - cluster[0]["price"] <= 2 * width
            if near_previous and stays_narrow:
                cluster.append(pivot)
                continue
        clusters.append([pivot])
    return clusters


def reaction_strength(candles: list[Candle], pivot: dict, atr_value: float, lookahead: int) -> float:
    """How far price ran away from the level after the touch, measured against
    what a random walk covers over the same horizon (ATR x sqrt(bars)).

    Comparing against a flat ATR multiple scores noise full marks: over 10 bars
    a drift-free walk already travels about 3.2 x ATR."""
    if atr_value <= 0:
        return 0.0
    segment = candles[pivot["index"] + 1 : pivot["index"] + 1 + lookahead]
    if not segment:
        return 0.0
    noise = atr_value * math.sqrt(len(segment))
    if pivot["kind"] == "high":
        move = pivot["price"] - min(c.low for c in segment)
    else:
        move = max(c.high for c in segment) - pivot["price"]
    return max(move, 0.0) / noise


def distinct_touches(candles: list[Candle], members: list[dict], bottom: float, top: float) -> list[dict]:
    """Pivots belonging to the same visit are one touch. A new touch counts only
    once price has closed outside the zone since the previous one — otherwise a
    single swing inflates the score into a fake "strong" zone."""
    ordered = sorted(members, key=lambda p: p["index"])
    touches = [ordered[0]]
    for pivot in ordered[1:]:
        between = candles[touches[-1]["index"] + 1 : pivot["index"]]
        if any(c.close > top or c.close < bottom for c in between):
            touches.append(pivot)
    return touches


def is_flip(candles: list[Candle], touches: list[dict], bottom: float, top: float, atr_value: float) -> bool:
    """A real role reversal: the zone was approached from both sides, and price
    genuinely traded a full ATR clear of it on both sides rather than grazing."""
    from_above = from_below = False
    for touch in touches:
        start = max(0, touch["index"] - FLIP_LOOKBACK)
        for candle in reversed(candles[start : touch["index"]]):
            if candle.close > top:
                from_above = True
                break
            if candle.close < bottom:
                from_below = True
                break
    if not (from_above and from_below):
        return False
    span = candles[touches[0]["index"] : touches[-1]["index"] + 1]
    held_above = any(c.close > top + atr_value for c in span)
    held_below = any(c.close < bottom - atr_value for c in span)
    return held_above and held_below


def round_number_score(price: float) -> float:
    """Distance to the nearest psychological round number, normalised 0..1.
    The step scales with the price: 0.01 for 1.2345, 10 for 1432."""
    if price <= 0:
        return 0.0
    step = 10 ** math.floor(math.log10(price) - 1)
    nearest = round(price / step) * step
    return max(0.0, 1.0 - abs(price - nearest) / (step / 2))


def score_zone(candles: list[Candle], members: list[dict], atr_value: float, lookahead: int,
               min_width: float) -> dict | None:
    """Turn one cluster of pivots into a scored zone. Support and resistance
    are areas, not lines, so a zone is never reported narrower than the band
    the pivots were clustered with. Returns None if the cluster collapses to
    fewer distinct touches than a zone needs."""
    prices = [p["price"] for p in members]
    bottom, top = min(prices), max(prices)
    if top - bottom < min_width:
        mid = (bottom + top) / 2
        bottom, top = mid - min_width / 2, mid + min_width / 2

    touches = distinct_touches(candles, members, bottom, top)
    center = sum(p["price"] for p in touches) / len(touches)
    last_touch = touches[-1]["index"]

    reactions = [reaction_strength(candles, p, atr_value, lookahead) for p in touches]
    avg_reaction = sum(reactions) / len(reactions)
    flipped = is_flip(candles, touches, bottom, top, atr_value)

    excess = (avg_reaction - REACTION_NOISE_FLOOR) / (REACTION_FULL_SCORE - REACTION_NOISE_FLOOR)
    parts = {
        "reaction": max(0.0, min(excess, 1.0)),
        "touches": min(len(touches), TOUCHES_FOR_FULL_SCORE) / TOUCHES_FOR_FULL_SCORE,
        "recency": last_touch / (len(candles) - 1) if len(candles) > 1 else 0.0,
        "flip": 1.0 if flipped else 0.0,
        "round": round_number_score(center),
    }
    score = 100 * sum(WEIGHTS[name] * value for name, value in parts.items())

    return {
        "bottom": bottom,
        "top": top,
        "center": center,
        "touches": len(touches),
        "last_touch_index": last_touch,
        "bars_since_touch": len(candles) - 1 - last_touch,
        "flipped": flipped,
        "reaction": avg_reaction,
        "score": score,
        "parts": parts,
    }


def classify_role(zone: dict, last_close: float) -> str:
    """Where the zone sits relative to the current price."""
    if zone["top"] < last_close:
        return "support"
    if zone["bottom"] > last_close:
        return "resistance"
    return "price inside zone"


def find_zones(
    candles: list[Candle],
    swing_window: int = 3,
    tolerance: float = 0.005,
    atr_period: int = 14,
    min_touches: int = 2,
    min_score: float = 60.0,
    lookback: int = 600,
    lookahead: int = REACTION_LOOKAHEAD,
) -> dict:
    """Full pipeline: pivots -> clusters -> scored zones -> nearest S/R.

    Only the last `lookback` candles are analysed. An unbounded window makes
    both the touch count and the recency score meaningless: over thousands of
    bars any price band collects a dozen visits, and "recent" stops meaning
    close to now."""
    if lookback and len(candles) > lookback:
        candles = candles[-lookback:]
    if len(candles) < swing_window * 2 + 1:
        raise ValueError(f"Need at least {swing_window * 2 + 1} candles for a swing window of {swing_window}")

    try:
        atr_value = atr(candles, atr_period)
    except ValueError:
        atr_value = sum(c.high - c.low for c in candles) / len(candles)

    pivots = find_pivots(candles, swing_window)
    clusters = cluster_pivots(pivots, tolerance, atr_value / 2)

    last_close = candles[-1].close
    zones = []
    for members in clusters:
        if len(members) < min_touches:
            continue
        width = max(tolerance * members[0]["price"], atr_value / 2)
        zone = score_zone(candles, members, atr_value, lookahead, width)
        if zone["touches"] < min_touches or zone["score"] < min_score:
            continue
        zone["role"] = classify_role(zone, last_close)
        zone["distance_pct"] = 100 * (zone["center"] - last_close) / last_close
        zones.append(zone)

    zones.sort(key=lambda z: z["score"], reverse=True)

    supports = sorted((z for z in zones if z["role"] == "support"), key=lambda z: z["top"], reverse=True)
    resistances = sorted((z for z in zones if z["role"] == "resistance"), key=lambda z: z["bottom"])

    return {
        "atr": atr_value,
        "last_close": last_close,
        "zones": zones,
        "nearest_support": supports[0] if supports else None,
        "nearest_resistance": resistances[0] if resistances else None,
        "inside": next((z for z in zones if z["role"] == "price inside zone"), None),
    }


def format_zone(zone: dict) -> str:
    flags = []
    if zone["flipped"]:
        flags.append("flip")
    if zone["parts"]["round"] > 0.5:
        flags.append("round-number")
    suffix = f" [{', '.join(flags)}]" if flags else ""
    return (
        f"{zone['bottom']:.5f} - {zone['top']:.5f}  "
        f"score={zone['score']:5.1f}  touches={zone['touches']}  "
        f"reaction={zone['reaction']:.2f}x  "
        f"last touch {zone['bars_since_touch']} bars ago  "
        f"({zone['distance_pct']:+.2f}%){suffix}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Find and rank support/resistance zones from OHLC CSV data.")
    parser.add_argument("csv_path", help="Path to CSV with columns: date,open,high,low,close")
    parser.add_argument("--swing", type=int, default=3, help="Swing detection window (default: 3)")
    parser.add_argument("--tolerance", type=float, default=0.0,
                        help="Optional zone-width floor as a fraction of price (default: 0, "
                             "so the ATR-based band governs). 0.005 on a 1.10 forex pair "
                             "would be a 55-pip zone — set it only for instruments where "
                             "you really want a percentage floor.")
    parser.add_argument("--atr-period", type=int, default=14, help="ATR period (default: 14)")
    parser.add_argument("--min-touches", type=int, default=2, help="Minimum distinct touches for a zone (default: 2)")
    parser.add_argument("--min-score", type=float, default=60.0, help="Minimum zone score 0-100 (default: 60)")
    parser.add_argument("--lookback", type=int, default=600, help="Bars of history to analyse (default: 600)")
    parser.add_argument("--top", type=int, default=6, help="How many zones to print (default: 6)")
    args = parser.parse_args()

    candles = load_candles(args.csv_path)
    result = find_zones(candles, args.swing, args.tolerance, args.atr_period,
                        args.min_touches, args.min_score, args.lookback)

    print(f"Last close       : {result['last_close']:.5f}  (ATR={result['atr']:.5f})")

    if result["inside"] is not None:
        print(f"Price inside zone: {format_zone(result['inside'])}")
    if result["nearest_resistance"] is not None:
        print(f"Nearest resistance: {format_zone(result['nearest_resistance'])}")
    else:
        print("Nearest resistance: none above price")
    if result["nearest_support"] is not None:
        print(f"Nearest support   : {format_zone(result['nearest_support'])}")
    else:
        print("Nearest support   : none below price")

    if not result["zones"]:
        print("\nNo zone passed the filters — lower --min-score/--min-touches or widen --tolerance.")
        return

    print(f"\nStrongest zones (top {args.top}):")
    for i, zone in enumerate(result["zones"][: args.top], start=1):
        print(f"  {i}. {zone['role']:<18} {format_zone(zone)}")


if __name__ == "__main__":
    try:
        main()
    except (FileNotFoundError, ValueError, KeyError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
