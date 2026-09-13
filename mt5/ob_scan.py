#!/usr/bin/env python3
"""أوردر بلوك — نفس قواعد mt5/OB_Sweep_FVG.mq5 لكن على ملف CSV.

Order block = sweep of a confirmed swing + displacement that shifts structure
+ imbalance (FVG). Use it to get the exact zones of a chart without opening MT5.

كيفاش تصدّر الشموع من MT5:
    View -> Symbols (Ctrl+U) -> اختار الزوج -> تبويب Bars -> اختار الفريم
    -> Export -> يسجّل ملف CSV

    python3 mt5/ob_scan.py XAUUSD_M15.csv

يقبل أيضًا أي CSV فيه الأعمدة: date,open,high,low,close
"""

import argparse
import csv
import sys
from datetime import datetime

BULL, BEAR = 1, -1
LIVE, TESTED, BROKEN = "LIVE", "TESTED", "BROKEN"


class Candle:
    __slots__ = ("t", "o", "h", "l", "c")

    def __init__(self, t, o, h, l, c):
        self.t, self.o, self.h, self.l, self.c = t, o, h, l, c


def _sniff(path):
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        head = fh.readline()
    for d in ("\t", ";", ","):
        if head.count(d) >= 4:
            return d
    return ","


def _num(x):
    return float(str(x).replace(",", ".").strip())


def load(path):
    """Read MT5 'Bars -> Export' files as well as plain date,o,h,l,c files."""
    delim = _sniff(path)
    rows = []
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        for row in csv.reader(fh, delimiter=delim):
            row = [c.strip() for c in row if c.strip() != ""]
            if len(row) < 5:
                continue
            # MT5 export: <DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL> ...
            try:
                if len(row) >= 6 and ":" in row[1]:
                    stamp = row[0] + " " + row[1]
                    o, h, l, c = (_num(v) for v in row[2:6])
                else:
                    stamp = row[0]
                    o, h, l, c = (_num(v) for v in row[1:5])
            except ValueError:
                continue  # header line
            rows.append(Candle(stamp, o, h, l, c))
    if not rows:
        sys.exit("ما لقيت ولا شمعة في الملف — تثبّت من الصيغة (date,open,high,low,close)")
    if len(rows) > 1 and rows[0].t > rows[-1].t:
        rows.reverse()  # newest-first files
    return rows


def atr_at(k, s, n):
    if s - n < 1:
        return 0.0
    total = 0.0
    for i in range(s - n + 1, s + 1):
        prev = k[i - 1].c
        total += max(k[i].h - k[i].l, abs(k[i].h - prev), abs(k[i].l - prev))
    return total / n


def swing_low(k, s, strength, search):
    """Nearest confirmed swing low still unbroken when bar s prints."""
    run = float("inf")
    for q in range(s - 1, max(strength, s - search) - 1, -1):
        if k[q].l < run:
            run = k[q].l
        else:
            continue
        if q + strength > s - 1:
            continue
        if all(0 <= j < s and k[j].l >= k[q].l for j in range(q - strength, q + strength + 1)):
            return q
    return -1


def swing_high(k, s, strength, search):
    run = -float("inf")
    for q in range(s - 1, max(strength, s - search) - 1, -1):
        if k[q].h > run:
            run = k[q].h
        else:
            continue
        if q + strength > s - 1:
            continue
        if all(0 <= j < s and k[j].h <= k[q].h for j in range(q - strength, q + strength + 1)):
            return q
    return -1


def detect(k, s, cfg, last):
    a = atr_at(k, s, cfg.atr)
    if a <= 0:
        return None
    min_body, min_fvg = cfg.displace * a, cfg.fvg_atr * a  # leg size / gap size

    p = swing_low(k, s, cfg.pivot, cfg.search)
    win = min(s + cfg.mss_bars, last)
    if p >= 0 and k[s].l < k[p].l and k[s].l == min(c.l for c in k[s:win + 1]) \
            and (not cfg.opp_colour or k[s].c < k[s].o):
        target = max(c.h for c in k[p:s + 1]) if cfg.mss else k[s].h
        j = -1
        for x in range(s + 1, win + 1):
            if k[x].c > target and (k[x].c - k[s].l) >= min_body:
                j = x
                break
        if j > 0:
            fvg = None
            for b in range(s, min(j + 1, last - 2) + 1):
                if k[b + 2].l - k[b].h >= min_fvg:
                    fvg = (k[b].h, k[b + 2].l)
                    break
            if fvg or not cfg.require_fvg:
                top = min(k[s].h, k[s].l + cfg.max_zone * a)
                return dict(dir=BULL, i=s, time=k[s].t, bottom=k[s].l, top=top,
                            fvg=fvg, swept=k[p].l, checked=j + 1)

    ph = swing_high(k, s, cfg.pivot, cfg.search)
    if ph >= 0 and k[s].h > k[ph].h and k[s].h == max(c.h for c in k[s:win + 1]) \
            and (not cfg.opp_colour or k[s].c > k[s].o):
        target = min(c.l for c in k[ph:s + 1]) if cfg.mss else k[s].l
        j = -1
        for x in range(s + 1, win + 1):
            if k[x].c < target and (k[s].h - k[x].c) >= min_body:
                j = x
                break
        if j > 0:
            fvg = None
            for b in range(s, min(j + 1, last - 2) + 1):
                if k[b].l - k[b + 2].h >= min_fvg:
                    fvg = (k[b + 2].h, k[b].l)
                    break
            if fvg or not cfg.require_fvg:
                bottom = max(k[s].l, k[s].h - cfg.max_zone * a)
                return dict(dir=BEAR, i=s, time=k[s].t, bottom=bottom, top=k[s].h,
                            fvg=fvg, swept=k[ph].h, checked=j + 1)
    return None


def overlap_pct(a, b):
    inter = min(a["top"], b["top"]) - max(a["bottom"], b["bottom"])
    if inter <= 0:
        return 0.0
    smaller = min(a["top"] - a["bottom"], b["top"] - b["bottom"])
    return inter / smaller * 100 if smaller > 0 else 0.0


def scan(k, cfg):
    last = len(k) - 1
    start = max(cfg.search + cfg.atr + cfg.mss_bars + 8, len(k) - cfg.max_bars)
    zones = []
    for s in range(start, last - (cfg.mss_bars + 2) + 1):
        z = detect(k, s, cfg, last)
        if not z:
            continue
        if cfg.no_overlap:
            zones = [e for e in zones
                     if e["state"] == BROKEN or e["dir"] != z["dir"]
                     or overlap_pct(e, z) < cfg.overlap]
        z["state"] = LIVE
        zones.append(z)
        # walk the zones forward over the bars that already printed
        for e in zones:
            if e["state"] == BROKEN:
                continue
            for b in range(e["checked"], last + 1):
                if k[b].l <= e["top"] and k[b].h >= e["bottom"] and e["state"] == LIVE:
                    e["state"] = TESTED
                if (e["dir"] == BULL and k[b].c < e["bottom"]) or \
                   (e["dir"] == BEAR and k[b].c > e["top"]):
                    e["state"] = BROKEN
                    e["broken_at"] = k[b].t
                    break
            e["checked"] = last + 1
    return zones


def main():
    ap = argparse.ArgumentParser(description="Order blocks: sweep + shift + FVG")
    ap.add_argument("csv")
    ap.add_argument("--pivot", type=int, default=3, help="swing strength")
    ap.add_argument("--search", type=int, default=40, help="bars searched for the swing")
    ap.add_argument("--mss-bars", type=int, default=5, help="bars allowed for the shift")
    ap.add_argument("--displace", type=float, default=1.0,
                    help="size of the shift leg (close - sweep extreme) >= x*ATR")
    ap.add_argument("--max-zone", type=float, default=2.0,
                    help="clamp the zone height to x*ATR")
    ap.add_argument("--opp-colour", action="store_true",
                    help="also require the OB candle to be the opposite colour")
    ap.add_argument("--fvg-atr", type=float, default=0.15, help="FVG >= x*ATR")
    ap.add_argument("--atr", type=int, default=14)
    ap.add_argument("--max-bars", type=int, default=3000)
    ap.add_argument("--overlap", type=float, default=50.0)
    ap.add_argument("--no-mss", dest="mss", action="store_false", help="skip the structure break")
    ap.add_argument("--keep-overlap", dest="no_overlap", action="store_false")
    ap.add_argument("--allow-no-fvg", dest="require_fvg", action="store_false")
    ap.add_argument("--show-broken", action="store_true")
    ap.add_argument("--digits", type=int, default=2)
    cfg = ap.parse_args()

    k = load(cfg.csv)
    zones = scan(k, cfg)
    price = k[-1].c
    d = cfg.digits

    live = [z for z in zones if z["state"] != BROKEN]
    live.sort(key=lambda z: z["top"], reverse=True)

    print(f"\nالشموع: {len(k)}   من {k[0].t}   إلى {k[-1].t}   آخر سعر: {price:.{d}f}")
    print(f"مناطق حيّة: {len(live)}   |   مكسورة: {len(zones) - len(live)}\n")
    print(f"{'النوع':<10}{'الحالة':<8}{'من':<12}{'إلى':<12}{'الفجوة FVG':<22}{'المسافة':<10}الوقت")
    print("-" * 96)
    for z in live:
        kind = "Bull OB" if z["dir"] == BULL else "Bear OB"
        fvg = f"{z['fvg'][0]:.{d}f} - {z['fvg'][1]:.{d}f}" if z["fvg"] else "-"
        mid = (z["top"] + z["bottom"]) / 2
        dist = f"{mid - price:+.{d}f}"
        print(f"{kind:<10}{z['state']:<8}{z['bottom']:<12.{d}f}{z['top']:<12.{d}f}{fvg:<22}{dist:<10}{z['time']}")

    if cfg.show_broken:
        print("\n--- مكسورة ---")
        for z in zones:
            if z["state"] != BROKEN:
                continue
            kind = "Bull OB" if z["dir"] == BULL else "Bear OB"
            print(f"{kind:<10}{'BROKEN':<8}{z['bottom']:<12.{d}f}{z['top']:<12.{d}f}"
                  f"{'':<22}{'':<10}{z['time']}  ->  {z.get('broken_at','')}")
    print()


if __name__ == "__main__":
    main()
