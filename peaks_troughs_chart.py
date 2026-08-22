#!/usr/bin/env python3
"""
Peaks & Troughs Chart — fractal swing-point detector with delayed confirmation,
cancellation, zigzag, main range, range breaks and trend, drawn straight onto
an OHLC candlestick chart (self-contained HTML/SVG, no external libraries).

Rules implemented (in priority order):
  1. Choose N (3, 5 or 7 odd candles) = window size.
  2. The middle candle of the window is the candidate:
       peak candidate   -> high(mid) is the max high in the window
       trough candidate -> low(mid)  is the min low  in the window
  3. Confirmation lags (N-1)/2 candles after the middle candle.
  4. Cancellation: two same-type points in a row with no opposite point
     between them -> keep the stronger one (lower low / higher high),
     drop the other.
  5. Zigzag: link every confirmed point (after rule 4) to the one before
     it -> must strictly alternate peak/trough/peak/trough.
  6. Main range: the last two consecutive zigzag points (a peak + a
     trough) fix two flat horizontal boundaries.
  7. Any move that stays inside that range is secondary (never joins the
     main line; still usable for fib/divergence/patterns).
  8. A range break creates a new main point (break above = trend
     continuation, break below = potential reversal) -> a new range is
     drawn from the new point + the point right before it.
  9. Trend: consecutive main HH+HL = uptrend, consecutive main LH+LL =
     downtrend.

Usage:
    python3 peaks_troughs_chart.py data.csv --window 5 --out chart.html

CSV must have a header row with columns: date,open,high,low,close
"""

import argparse
import sys
from dataclasses import dataclass

from trend_identifier import Candle, load_candles


@dataclass
class SwingPoint:
    index: int             # candle index of the actual extreme (source bar)
    confirmed_index: int   # candle index at which the point becomes confirmed
    kind: str              # "peak" | "trough"
    price: float


def find_candidates(candles: list[Candle], n: int) -> list[dict]:
    """Rule 1-3: fractal candidates with their confirmation lag."""
    if n not in (3, 5, 7):
        raise ValueError("Window N must be one of 3, 5, or 7 (rule 1)")
    half = (n - 1) // 2
    if len(candles) < n:
        raise ValueError(f"Need at least {n} candles for a window of N={n}")

    candidates = []
    for i in range(half, len(candles) - half):
        window = candles[i - half : i + half + 1]
        mid = candles[i]
        if mid.high == max(c.high for c in window):
            candidates.append({"index": i, "confirmed_index": i + half, "kind": "peak", "price": mid.high})
        if mid.low == min(c.low for c in window):
            candidates.append({"index": i, "confirmed_index": i + half, "kind": "trough", "price": mid.low})
    return candidates


def resolve_zigzag(candidates: list[dict]) -> list[SwingPoint]:
    """Rule 4-5: cancel same-type runs down to the strongest point, which
    leaves a strictly alternating peak/trough/peak/trough zigzag."""
    ordered = sorted(candidates, key=lambda c: (c["confirmed_index"], c["index"]))
    stack: list[dict] = []
    for c in ordered:
        if stack and stack[-1]["kind"] == c["kind"]:
            is_stronger = c["price"] > stack[-1]["price"] if c["kind"] == "peak" else c["price"] < stack[-1]["price"]
            if is_stronger:
                stack[-1] = c
        else:
            stack.append(c)
    return [SwingPoint(c["index"], c["confirmed_index"], c["kind"], c["price"]) for c in stack]


def build_main_structure(zigzag: list[SwingPoint]) -> dict:
    """Rule 6-8: track the active main range and grow it only on a break."""
    if len(zigzag) < 2:
        return {"segments": [], "main_highs": [], "main_lows": [], "main_points": []}

    p0, p1 = zigzag[0], zigzag[1]
    upper, lower = (p0, p1) if p0.kind == "peak" else (p1, p0)

    main_highs = [upper]
    main_lows = [lower]
    main_points = [p0, p1]
    segments = [{"upper": upper, "lower": lower, "end_index": None, "break": None}]

    for k in range(2, len(zigzag)):
        prev_pt, cur_pt = zigzag[k - 1], zigzag[k]

        broke = None
        if cur_pt.kind == "peak" and cur_pt.price > upper.price:
            broke = "up"
        elif cur_pt.kind == "trough" and cur_pt.price < lower.price:
            broke = "down"
        if broke is None:
            continue  # inside the range -> secondary move (rule 7), ignored for main structure

        segments[-1]["end_index"] = cur_pt.index
        upper, lower = (cur_pt, prev_pt) if broke == "up" else (prev_pt, cur_pt)
        main_points.append(cur_pt)
        (main_highs if cur_pt.kind == "peak" else main_lows).append(cur_pt)
        segments.append({"upper": upper, "lower": lower, "end_index": None, "break": broke})

    return {
        "segments": segments,
        "main_highs": main_highs,
        "main_lows": main_lows,
        "main_points": main_points,
        "current_upper": upper,
        "current_lower": lower,
    }


def determine_trend(main_highs: list[SwingPoint], main_lows: list[SwingPoint]) -> str:
    """Rule 9: consecutive main HH+HL -> uptrend, LH+LL -> downtrend."""
    if len(main_highs) < 2 or len(main_lows) < 2:
        return "unclear"
    higher_high = main_highs[-1].price > main_highs[-2].price
    higher_low = main_lows[-1].price > main_lows[-2].price
    lower_high = main_highs[-1].price < main_highs[-2].price
    lower_low = main_lows[-1].price < main_lows[-2].price
    if higher_high and higher_low:
        return "uptrend"
    if lower_high and lower_low:
        return "downtrend"
    return "mixed"


def _point_labels(main_highs: list[SwingPoint], main_lows: list[SwingPoint]) -> dict:
    labels = {}
    for j, sp in enumerate(main_highs):
        labels[(sp.kind, sp.index)] = "H" if j == 0 else ("HH" if sp.price > main_highs[j - 1].price else "LH")
    for j, sp in enumerate(main_lows):
        labels[(sp.kind, sp.index)] = "L" if j == 0 else ("HL" if sp.price > main_lows[j - 1].price else "LL")
    return labels


def render_chart(candles: list[Candle], zigzag: list[SwingPoint], structure: dict, trend: str, window: int) -> str:
    n = len(candles)
    spacing = 14
    candle_w = 8
    margin_left, margin_right, margin_top, margin_bottom = 60, 210, 70, 40
    plot_h = 560
    width = margin_left + margin_right + n * spacing
    height = margin_top + plot_h + margin_bottom

    price_min = min(c.low for c in candles)
    price_max = max(c.high for c in candles)
    pad = (price_max - price_min) * 0.06 or price_max * 0.01 or 1.0
    lo, hi = price_min - pad, price_max + pad

    def x(i: int) -> float:
        return margin_left + i * spacing + spacing / 2

    def y(price: float) -> float:
        return margin_top + plot_h - (price - lo) / (hi - lo) * plot_h

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" font-family="Menlo, Consolas, monospace" font-size="11">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#0f1117"/>',
    ]

    # grid + y-axis price labels
    for k in range(6):
        price = lo + (hi - lo) * k / 5
        gy = y(price)
        parts.append(f'<line x1="{margin_left}" y1="{gy:.1f}" x2="{width - margin_right}" y2="{gy:.1f}" stroke="#232733" stroke-width="1"/>')
        parts.append(f'<text x="8" y="{gy + 4:.1f}" fill="#8a91a3">{price:.5f}</text>')

    # x-axis date ticks
    tick_every = max(1, n // 12)
    for i in range(0, n, tick_every):
        parts.append(f'<text x="{x(i) - 20:.1f}" y="{height - 12}" fill="#8a91a3">{candles[i].date}</text>')

    # candlesticks
    for i, c in enumerate(candles):
        bullish = c.close >= c.open
        color = "#26a69a" if bullish else "#ef5350"
        cx = x(i)
        parts.append(f'<line x1="{cx:.1f}" y1="{y(c.high):.1f}" x2="{cx:.1f}" y2="{y(c.low):.1f}" stroke="{color}" stroke-width="1.4"/>')
        top, bottom = y(max(c.open, c.close)), y(min(c.open, c.close))
        body_h = max(bottom - top, 1.0)
        parts.append(f'<rect x="{cx - candle_w / 2:.1f}" y="{top:.1f}" width="{candle_w}" height="{body_h:.1f}" fill="{color}"/>')

    # range segments (rule 6-8): shaded rectangles, most recent one highlighted
    last_seg_i = len(structure["segments"]) - 1
    for si, seg in enumerate(structure["segments"]):
        x_start = x(min(seg["upper"].index, seg["lower"].index))
        x_end = x(seg["end_index"]) if seg["end_index"] is not None else (width - margin_right + 20)
        top, bottom = y(seg["upper"].price), y(seg["lower"].price)
        is_active = si == last_seg_i
        fill = "#3d7bff" if is_active else "#555f77"
        opacity = "0.16" if is_active else "0.06"
        dash = "" if is_active else 'stroke-dasharray="4 3"'
        parts.append(
            f'<rect x="{x_start:.1f}" y="{top:.1f}" width="{x_end - x_start:.1f}" height="{bottom - top:.1f}" '
            f'fill="{fill}" fill-opacity="{opacity}" stroke="{fill}" stroke-width="1" {dash}/>'
        )
        if is_active:
            parts.append(f'<text x="{x_end + 6:.1f}" y="{top + 4:.1f}" fill="{fill}" font-size="10">range upper {seg["upper"].price:.5f}</text>')
            parts.append(f'<text x="{x_end + 6:.1f}" y="{bottom + 4:.1f}" fill="{fill}" font-size="10">range lower {seg["lower"].price:.5f}</text>')

    # secondary zigzag line (rule 5): thin dashed, connects every confirmed point
    if len(zigzag) >= 2:
        pts = " ".join(f"{x(sp.index):.1f},{y(sp.price):.1f}" for sp in zigzag)
        parts.append(f'<polyline points="{pts}" fill="none" stroke="#8a91a3" stroke-width="1.2" stroke-dasharray="3 3"/>')
    for sp in zigzag:
        parts.append(f'<circle cx="{x(sp.index):.1f}" cy="{y(sp.price):.1f}" r="2.6" fill="none" stroke="#c6cbd8" stroke-width="1.2"/>')

    # main structure line (rule 6-9): bold, only main points, labeled HH/HL/LH/LL
    main_points = structure["main_points"]
    labels = _point_labels(structure["main_highs"], structure["main_lows"])
    if len(main_points) >= 2:
        pts = " ".join(f"{x(sp.index):.1f},{y(sp.price):.1f}" for sp in main_points)
        parts.append(f'<polyline points="{pts}" fill="none" stroke="#ffb020" stroke-width="2.2"/>')
    for sp in main_points:
        cx, cy = x(sp.index), y(sp.price)
        label = labels.get((sp.kind, sp.index), "")
        label_y = cy - 10 if sp.kind == "peak" else cy + 18
        parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4.2" fill="#ffb020"/>')
        parts.append(f'<text x="{cx - 8:.1f}" y="{label_y:.1f}" fill="#ffb020" font-weight="bold">{label}</text>')

    # title / legend
    trend_color = {"uptrend": "#26a69a", "downtrend": "#ef5350"}.get(trend, "#8a91a3")
    parts.append(f'<text x="{margin_left}" y="26" fill="#e6e8ef" font-size="16" font-weight="bold">Peaks &amp; Troughs — N={window}</text>')
    parts.append(f'<text x="{margin_left}" y="46" fill="{trend_color}" font-size="13" font-weight="bold">Trend: {trend.upper()}</text>')

    parts.append("</svg>")
    svg = "\n".join(parts)

    return f"""<!doctype html>
<html lang="ar">
<head>
<meta charset="utf-8"/>
<title>Peaks &amp; Troughs Chart</title>
<style>
  body {{ background:#0f1117; margin:0; padding:16px; direction:ltr; }}
  .wrap {{ overflow-x:auto; direction:ltr; display:block; }}
  .wrap svg {{ display:block; }}
</style>
</head>
<body>
<div class="wrap">
{svg}
</div>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect peaks/troughs (fractal rules) and draw them on an OHLC chart.")
    parser.add_argument("csv_path", help="Path to CSV with columns: date,open,high,low,close")
    parser.add_argument("--window", type=int, choices=[3, 5, 7], default=5, help="نافذة الفراكتال N — قاعدة 1 (default: 5)")
    parser.add_argument("--last", type=int, default=None, help="اقتصر على آخر N شمعة من الملف قبل التحليل")
    parser.add_argument("--out", default="peaks_troughs_chart.html", help="مسار ملف HTML للرسم الناتج (default: peaks_troughs_chart.html)")
    args = parser.parse_args()

    candles = load_candles(args.csv_path)
    if args.last:
        candles = candles[-args.last :]

    candidates = find_candidates(candles, args.window)
    zigzag = resolve_zigzag(candidates)
    if len(zigzag) < 2:
        raise ValueError("لم يتكوّن عدد كافٍ من نقاط القمم/القيعان المؤكدة لرسم رينج رئيسي")

    structure = build_main_structure(zigzag)
    trend = determine_trend(structure["main_highs"], structure["main_lows"])

    html = render_chart(candles, zigzag, structure, trend, args.window)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)

    half = (args.window - 1) // 2
    upper, lower = structure["current_upper"], structure["current_lower"]
    print(f"Window (N)             : {args.window}  (confirmation lag = {half} candle(s) after the middle bar)")
    print(f"Confirmed swing points  : {len(zigzag)}  (after cancellation rule)")
    print(f"Main structure points   : {len(structure['main_points'])}")
    print(f"Active main range       : upper={upper.price:.5f} (candle #{upper.index}, {candles[upper.index].date})")
    print(f"                          lower={lower.price:.5f} (candle #{lower.index}, {candles[lower.index].date})")
    print(f"Trend                   : {trend}")
    print(f"Chart written to        : {args.out}")


if __name__ == "__main__":
    try:
        main()
    except (FileNotFoundError, ValueError, KeyError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
