#!/usr/bin/env python3
"""
Liquidity Zones — locates the price areas where stop-loss and pending orders
are most likely resting, and tells you which of them are still untapped.

Method (no single pattern is trusted alone):
  1. Swing highs/lows are detected, then CLUSTERED into zones using an
     ATR-scaled tolerance, so "equal highs/lows" adapt to the instrument's
     own volatility instead of a fixed percentage.
  2. Each zone is scored by confluence: number of touches, how tight the
     cluster is, how recent it is, volume traded at the touches, proximity
     to a psychological round number, and whether it coincides with a
     reference level (previous day/week high-low, session high-low).
  3. Each zone gets a tap status: untapped (live target), swept (wick took
     it and closed back = liquidity grab), or broken (closed beyond it).
     Swept and broken zones are discounted — that liquidity is gone.

Usage:
    python3 liquidity_zones.py data.csv [--swing 3] [--tolerance 0.25]
                                        [--atr-period 14] [--top 8]

CSV must have a header row with columns: date,open,high,low,close
An optional `volume` column improves the score when present.
"""

import argparse
import math
import sys
from datetime import datetime

from smc_strategy import find_swings
from trend_identifier import Candle, load_candles

# Session windows in the data's own clock (UTC by convention), [start, end).
SESSIONS = (("Asia", 0, 8), ("London", 8, 13), ("NewYork", 13, 22))

DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
    "%Y/%m/%d",
)


def parse_datetime(value: str) -> datetime | None:
    """Best-effort parse of the CSV `date` column; None if the format is unknown."""
    value = value.strip()
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def true_ranges(candles: list[Candle]) -> list[float]:
    return [
        max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
        for prev, cur in zip(candles, candles[1:])
    ]


def atr(candles: list[Candle], period: int = 14) -> float:
    """Wilder's ATR, falling back to the mean of whatever ranges exist."""
    ranges = true_ranges(candles)
    if not ranges:
        return 0.0
    if len(ranges) < period:
        return sum(ranges) / len(ranges)
    value = sum(ranges[:period]) / period
    for r in ranges[period:]:
        value = (value * (period - 1) + r) / period
    return value


def cluster_swings(swings: list[dict], kind: str, tolerance: float) -> list[dict]:
    """Group swing points of one kind into zones. Points are walked in price
    order and kept in the same zone while the whole cluster stays inside
    `tolerance` — that cluster IS the liquidity pool (equal highs / lows)."""
    points = sorted((s for s in swings if s["kind"] == kind), key=lambda s: s["price"])
    groups: list[list[dict]] = []
    current: list[dict] = []
    for point in points:
        if current and point["price"] - current[0]["price"] > tolerance:
            groups.append(current)
            current = []
        current.append(point)
    if current:
        groups.append(current)

    zones = []
    for group in groups:
        prices = [p["price"] for p in group]
        # Stops rest just BEYOND the extreme of the cluster, not at its middle.
        zones.append({
            "kind": kind,
            "level": max(prices) if kind == "high" else min(prices),
            "top": max(prices),
            "bottom": min(prices),
            "width": max(prices) - min(prices),
            "touches": len(group),
            "indices": sorted(p["index"] for p in group),
        })
    return zones


def reference_levels(candles: list[Candle]) -> list[dict]:
    """Previous day/week high-low and the last completed session high-low —
    the levels every desk watches, so liquidity gathers around them."""
    stamps = [parse_datetime(c.date) for c in candles]
    if any(s is None for s in stamps):
        return []

    levels: list[dict] = []

    def add_period(key_fn, label_prefix: str) -> None:
        buckets: dict = {}
        order: list = []
        for stamp, candle in zip(stamps, candles):
            key = key_fn(stamp)
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(candle)
        if len(order) < 2:
            return
        previous = buckets[order[-2]]
        levels.append({"name": f"{label_prefix}H", "price": max(c.high for c in previous), "kind": "high"})
        levels.append({"name": f"{label_prefix}L", "price": min(c.low for c in previous), "kind": "low"})

    add_period(lambda s: s.date(), "PD")
    add_period(lambda s: s.isocalendar()[:2], "PW")

    if any(s.hour or s.minute for s in stamps):
        blocks: list[tuple] = []
        for stamp, candle in zip(stamps, candles):
            name = next((n for n, start, end in SESSIONS if start <= stamp.hour < end), None)
            if name is None:
                continue
            key = (stamp.date(), name)
            if blocks and blocks[-1][0] == key:
                blocks[-1][1].append(candle)
            else:
                blocks.append((key, [candle]))
        seen = set()
        for (_, name), block in reversed(blocks[:-1]):  # the last block may still be open
            if name in seen:
                continue
            seen.add(name)
            levels.append({"name": f"{name} High", "price": max(c.high for c in block), "kind": "high"})
            levels.append({"name": f"{name} Low", "price": min(c.low for c in block), "kind": "low"})

    return levels


def tap_status(candles: list[Candle], zone: dict) -> dict:
    """untapped -> still a live target; swept -> a wick grabbed it and price
    closed back (reversal fuel); broken -> price closed beyond it."""
    level = zone["level"]
    start = max(zone["indices"]) + 1
    penetration = None
    for i in range(start, len(candles)):
        c = candles[i]
        if zone["kind"] == "high" and c.high > level:
            penetration = penetration if penetration is not None else i
            if c.close > level:
                return {"status": "broken", "index": i, "sweep_index": penetration}
        elif zone["kind"] == "low" and c.low < level:
            penetration = penetration if penetration is not None else i
            if c.close < level:
                return {"status": "broken", "index": i, "sweep_index": penetration}
    if penetration is not None:
        return {"status": "swept", "index": penetration, "sweep_index": penetration}
    return {"status": "untapped", "index": None, "sweep_index": None}


def round_number_bonus(level: float) -> tuple[float, str | None]:
    """Psychological levels (…00 / …50) collect pending orders on their own."""
    if level <= 0:
        return 0.0, None
    step = 10 ** math.floor(math.log10(level) - 2)
    for size, points, tag in ((step * 10, 8.0, "major-round"), (step * 5, 4.0, "round")):
        if abs(level - round(level / size) * size) <= size * 0.1:
            return points, tag
    return 0.0, None


def score_zone(zone: dict, candles: list[Candle], atr_value: float, refs: list[dict], tolerance: float) -> dict:
    """Confluence score (0-100). Weights favour what actually parks orders:
    repeated touches first, then reference levels, then volume/round numbers."""
    tags: list[str] = []

    touch_score = min(zone["touches"], 4) / 4 * 30
    if zone["touches"] >= 2:
        tags.append("EQH" if zone["kind"] == "high" else "EQL")

    tightness = 12.0 if tolerance <= 0 else max(0.0, 1 - zone["width"] / tolerance) * 12

    last_touch = max(zone["indices"])
    recency = (last_touch / max(len(candles) - 1, 1)) * 10

    ref_score = 0.0
    for ref in refs:
        if ref["kind"] == zone["kind"] and abs(ref["price"] - zone["level"]) <= tolerance:
            ref_score = 20.0
            tags.append(ref["name"])
            break

    volumes = [candles[i].volume for i in zone["indices"] if candles[i].volume is not None]
    all_volumes = [c.volume for c in candles if c.volume is not None]
    if volumes and all_volumes:
        average = sum(all_volumes) / len(all_volumes)
        ratio = (sum(volumes) / len(volumes)) / average if average else 1.0
        volume_score = min(max(ratio - 1, 0.0), 1.0) * 12
        if ratio >= 1.3:
            tags.append("high-volume")
    else:
        volume_score = 6.0  # neutral when the CSV has no volume column

    round_score, round_tag = round_number_bonus(zone["level"])
    if round_tag:
        tags.append(round_tag)

    raw = touch_score + tightness + recency + ref_score + volume_score + round_score
    status = tap_status(candles, zone)
    penalty = {"untapped": 1.0, "swept": 0.5, "broken": 0.25}[status["status"]]

    return {
        **zone,
        **status,
        "tags": tags,
        "score": round(raw / 92 * 100 * penalty, 1),
    }


def find_liquidity_zones(candles: list[Candle], window: int = 3, tolerance_atr: float = 0.25, atr_period: int = 14) -> dict:
    """Full scan: cluster the swings, score every zone, and split what is left
    untapped above and below the current price."""
    atr_value = atr(candles, atr_period)
    last_close = candles[-1].close
    tolerance = atr_value * tolerance_atr or last_close * 0.001

    swings = find_swings(candles, window)
    if not swings:
        raise ValueError(f"No swing points found — try a smaller --swing (current: {window})")

    refs = reference_levels(candles)
    zones = [
        score_zone(zone, candles, atr_value, refs, tolerance)
        for kind in ("high", "low")
        for zone in cluster_swings(swings, kind, tolerance)
    ]
    for zone in zones:
        zone["side"] = "above" if zone["level"] > last_close else "below"
        zone["distance_pct"] = (zone["level"] - last_close) / last_close * 100
        zone["distance_atr"] = (zone["level"] - last_close) / atr_value if atr_value else 0.0

    zones.sort(key=lambda z: z["score"], reverse=True)
    untapped = [z for z in zones if z["status"] == "untapped"]
    above = [z for z in untapped if z["side"] == "above"]
    below = [z for z in untapped if z["side"] == "below"]

    return {
        "atr": atr_value,
        "atr_period": atr_period,
        "tolerance": tolerance,
        "last_close": last_close,
        "zones": zones,
        "reference_levels": refs,
        "nearest_untapped_above": min(above, key=lambda z: z["distance_pct"], default=None),
        "nearest_untapped_below": max(below, key=lambda z: z["distance_pct"], default=None),
        "strongest_untapped": untapped[0] if untapped else None,
    }


def format_zone(zone: dict) -> str:
    tags = f"  [{', '.join(zone['tags'])}]" if zone["tags"] else ""
    return (
        f"{zone['side']:<5} {zone['kind']:<4} {zone['level']:>12.5f} "
        f"({zone['bottom']:.5f}-{zone['top']:.5f})  touches={zone['touches']}  "
        f"{zone['status']:<8} score={zone['score']:>5.1f}  "
        f"{zone['distance_pct']:+.2f}% ({zone['distance_atr']:+.1f} ATR){tags}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Identify liquidity zones (resting orders) from OHLC CSV data.")
    parser.add_argument("csv_path", help="Path to CSV with columns: date,open,high,low,close[,volume]")
    parser.add_argument("--swing", type=int, default=3, help="Swing detection window (default: 3)")
    parser.add_argument("--tolerance", type=float, default=0.25, help="Cluster tolerance in ATR units (default: 0.25)")
    parser.add_argument("--atr-period", type=int, default=14, help="ATR period (default: 14)")
    parser.add_argument("--top", type=int, default=8, help="How many zones to print (default: 8)")
    parser.add_argument("--untapped-only", action="store_true", help="Hide zones that were already swept or broken")
    args = parser.parse_args()

    candles = load_candles(args.csv_path)
    result = find_liquidity_zones(candles, args.swing, args.tolerance, args.atr_period)

    print(f"Last close        : {result['last_close']:.5f}")
    print(f"ATR({result['atr_period']})           : {result['atr']:.5f}  (cluster tolerance = {result['tolerance']:.5f})")
    if result["reference_levels"]:
        refs = "  ".join(f"{r['name']}={r['price']:.5f}" for r in result["reference_levels"])
        print(f"Reference levels  : {refs}")

    zones = [z for z in result["zones"] if not args.untapped_only or z["status"] == "untapped"]
    print(f"\nLiquidity zones (top {min(args.top, len(zones))} by score):")
    for zone in zones[: args.top]:
        print(f"  {format_zone(zone)}")

    print("\nTargets:")
    for label, key in (("Nearest untapped above", "nearest_untapped_above"), ("Nearest untapped below", "nearest_untapped_below"), ("Strongest untapped    ", "strongest_untapped")):
        zone = result[key]
        print(f"  {label}: {format_zone(zone) if zone else 'none'}")


if __name__ == "__main__":
    try:
        main()
    except (FileNotFoundError, ValueError, KeyError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
