#!/usr/bin/env python3
"""
Support & Resistance — finds price zones (not single lines) from OHLC data and
ranks them by how much they actually matter.

A level is only worth watching if price has repeatedly reacted to it, so every
zone is scored on five things instead of being drawn by eye:
  1. Touches      — how many swing points cluster inside the same band.
  2. Reaction     — how far price travelled away from the zone after touching it
                    (measured in ATR, so it works on any instrument/timeframe).
  3. Recency      — a zone touched last week beats one from a year ago.
  4. Flip         — the zone acted as both support and resistance (role reversal).
  5. Round number — proximity to a psychological level (1.2000, 100, 50 ...).

Usage:
    python3 support_resistance.py data.csv [--swing 3] [--tolerance 0.005]
                                           [--atr-period 14] [--top 6]

CSV must have a header row with columns: date,open,high,low,close
"""

import argparse
import math
import sys

from trend_identifier import Candle, load_candles

# Score weights — must sum to 1.0.
WEIGHTS = {"touches": 0.30, "reaction": 0.25, "recency": 0.20, "flip": 0.15, "round": 0.10}

TOUCHES_FOR_FULL_SCORE = 5
ATR_MOVE_FOR_FULL_SCORE = 3.0
REACTION_LOOKAHEAD = 10


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
    """How far price ran away from the level after the touch, in ATR units.
    A level price barely bounced off is not a level."""
    if atr_value == 0:
        return 0.0
    segment = candles[pivot["index"] + 1 : pivot["index"] + 1 + lookahead]
    if not segment:
        return 0.0
    if pivot["kind"] == "high":
        move = pivot["price"] - min(c.low for c in segment)
    else:
        move = max(c.high for c in segment) - pivot["price"]
    return max(move, 0.0) / atr_value


def round_number_score(price: float) -> float:
    """Distance to the nearest psychological round number, normalised 0..1.
    The step scales with the price: 0.01 for 1.2345, 10 for 1432."""
    if price <= 0:
        return 0.0
    step = 10 ** math.floor(math.log10(price) - 1)
    nearest = round(price / step) * step
    return max(0.0, 1.0 - abs(price - nearest) / (step / 2))


def score_zone(candles: list[Candle], members: list[dict], atr_value: float, lookahead: int, min_width: float) -> dict:
    """Turn one cluster of pivots into a scored zone. Support and resistance
    are areas, not lines, so a zone is never reported narrower than the band
    the pivots were clustered with."""
    prices = [p["price"] for p in members]
    center = sum(prices) / len(prices)
    bottom, top = min(prices), max(prices)
    if top - bottom < min_width:
        bottom, top = center - min_width / 2, center + min_width / 2
    last_touch = max(p["index"] for p in members)
    kinds = {p["kind"] for p in members}

    reactions = [reaction_strength(candles, p, atr_value, lookahead) for p in members]
    avg_reaction = sum(reactions) / len(reactions)

    parts = {
        "touches": min(len(members), TOUCHES_FOR_FULL_SCORE) / TOUCHES_FOR_FULL_SCORE,
        "reaction": min(avg_reaction / ATR_MOVE_FOR_FULL_SCORE, 1.0),
        "recency": last_touch / (len(candles) - 1) if len(candles) > 1 else 0.0,
        "flip": 1.0 if len(kinds) > 1 else 0.0,
        "round": round_number_score(center),
    }
    score = 100 * sum(WEIGHTS[name] * value for name, value in parts.items())

    return {
        "bottom": bottom,
        "top": top,
        "center": center,
        "touches": len(members),
        "last_touch_index": last_touch,
        "bars_since_touch": len(candles) - 1 - last_touch,
        "flipped": len(kinds) > 1,
        "avg_reaction_atr": avg_reaction,
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
    lookahead: int = REACTION_LOOKAHEAD,
) -> dict:
    """Full pipeline: pivots -> clusters -> scored zones -> nearest S/R."""
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
        f"reaction={zone['avg_reaction_atr']:.2f}xATR  "
        f"last touch {zone['bars_since_touch']} bars ago  "
        f"({zone['distance_pct']:+.2f}%){suffix}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Find and rank support/resistance zones from OHLC CSV data.")
    parser.add_argument("csv_path", help="Path to CSV with columns: date,open,high,low,close")
    parser.add_argument("--swing", type=int, default=3, help="Swing detection window (default: 3)")
    parser.add_argument("--tolerance", type=float, default=0.005, help="Zone width as a fraction of price (default: 0.005)")
    parser.add_argument("--atr-period", type=int, default=14, help="ATR period (default: 14)")
    parser.add_argument("--min-touches", type=int, default=2, help="Minimum touches for a zone to count (default: 2)")
    parser.add_argument("--top", type=int, default=6, help="How many zones to print (default: 6)")
    args = parser.parse_args()

    candles = load_candles(args.csv_path)
    result = find_zones(candles, args.swing, args.tolerance, args.atr_period, args.min_touches)

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
        print("\nNo zone reached the minimum touch count — lower --min-touches or widen --tolerance.")
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
