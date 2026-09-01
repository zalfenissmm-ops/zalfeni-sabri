#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ICT / SMC Intraday Bot  --  single file  (Gold / XAUUSD)

Strategy:
    H1  -> context (with-trend / reversal-correction), label only.
    M15 -> liquidity sweep (break a high/low then close back).
    M5  -> MSS + Displacement + FVG.
    then retracement into the FVG = entry.
    SL beyond the sweep extreme. TP = nearest opposing liquidity that gives
    RR >= rr_min. RR too small or no target far enough -> NO TRADE.

Honest backtest:
    Setups are detected on M5/M15/H1, but each trade's WIN/LOSS is resolved on
    M1 (1-minute) candles so we know which of TP/SL was hit FIRST inside every
    5-minute bar. Fills are pessimistic: if the same minute touches both SL and
    TP, it counts as a LOSS. TP uses only liquidity that existed at setup time
    (no look-ahead). Every sweep is one opportunity; the analysis prints where
    each one stopped.

Run (one command does everything: 90-day analysis once, then live every 5 min):
    python ict_smc_bot.py

Other modes:
    python ict_smc_bot.py --backtest-only
    python ict_smc_bot.py --live-only
    python ict_smc_bot.py --selftest        # engine test on synthetic data, no MT5

Requirements for live: MetaTrader5 installed (Windows) + pip install MetaTrader5
"""

import argparse
import bisect
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# ============================================================================
# Settings -- put your Telegram bot token and chat id here (or as env vars)
# ============================================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "PUT_YOUR_CHAT_ID_HERE")

MT5_LOGIN         = os.environ.get("MT5_LOGIN")
MT5_PASSWORD      = os.environ.get("MT5_PASSWORD")
MT5_SERVER        = os.environ.get("MT5_SERVER")
MT5_TERMINAL_PATH = os.environ.get("MT5_TERMINAL_PATH")

_PLACEHOLDERS = ("PUT_YOUR_BOT_TOKEN_HERE", "PUT_YOUR_CHAT_ID_HERE", "", None)
PRICE_DIGITS = 2   # price decimals for display; auto-set from MT5 (2 for gold)


def telegram_configured() -> bool:
    return TELEGRAM_BOT_TOKEN not in _PLACEHOLDERS and TELEGRAM_CHAT_ID not in _PLACEHOLDERS


# ============================================================================
# Candle model + timeframes
# ============================================================================
TF_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "H1": 3600}


@dataclass
class Candle:
    t: int
    o: float
    h: float
    l: float
    c: float

    def close_time(self, tf: str) -> int:
        return self.t + TF_SECONDS[tf]


@dataclass
class Params:
    symbol: str = "XAUUSD"
    digits: int = 2

    sw_h1: int = 4
    sw_m15: int = 2
    sw_m5: int = 1

    sweep_lookback: int = 40      # live: look for a sweep in the last N M15 candles
    mss_window: int = 60          # MSS must occur within N M5 candles after the sweep

    atr_period: int = 14
    disp_atr_mult: float = 1.0
    disp_body_ratio: float = 0.4

    rr_min: float = 1.5
    entry_mode: str = "edge"      # entry inside the FVG: edge (proximal, best in tests) / ce (50%) / far (deep)
    balance: float = 1000.0       # backtest account example
    risk_pct: float = 1.0         # % of equity risked per trade
    max_hold_hours: float = 24.0  # time-stop: close if neither TP nor SL hit (0 = off)

    win_m5: int = 300             # live decision windows (candles per timeframe)
    win_m15: int = 220
    win_h1: int = 200


# ============================================================================
# Indicators & structure
# ============================================================================
def atr_series(candles, period):
    n = len(candles)
    trs = [0.0] * n
    for i, c in enumerate(candles):
        if i == 0:
            trs[i] = c.h - c.l
        else:
            pc = candles[i - 1].c
            trs[i] = max(c.h - c.l, abs(c.h - pc), abs(c.l - pc))
    atr = [None] * n
    if n >= period:
        s = sum(trs[:period])
        atr[period - 1] = s / period
        for i in range(period, n):
            s += trs[i] - trs[i - period]
            atr[i] = s / period
    return atr


def swings(candles, w):
    """Confirmed fractal swing highs/lows. Last w bars unconfirmed (no look-ahead)."""
    highs, lows = [], []
    for i in range(w, len(candles) - w):
        seg = candles[i - w:i + w + 1]
        if candles[i].h == max(x.h for x in seg):
            highs.append((i, candles[i].h))
        if candles[i].l == min(x.l for x in seg):
            lows.append((i, candles[i].l))
    return highs, lows


def structure_bias(highs, lows):
    if len(highs) < 2 or len(lows) < 2:
        return "unknown"
    hh = highs[-1][1] > highs[-2][1]
    hl = lows[-1][1] > lows[-2][1]
    if hh and hl:
        return "up"
    if not hh and not hl:
        return "down"
    return "mixed"


# ============================================================================
# Strategy stages (shared by live and analysis)
# ============================================================================
def find_all_sweeps(m15, p: Params, use_lookback=True):
    highs, lows = swings(m15, p.sw_m15)
    n = len(m15)
    floor_idx = max(0, n - p.sweep_lookback) if use_lookback else 0
    found = {}
    for (si, level) in lows:
        for j in range(si + 1, n):
            c = m15[j]
            if c.l < level and c.c > level:
                if j >= floor_idx:
                    found[("bull", j)] = {"side": "bull", "level": level, "idx": j, "extreme": c.l, "t": c.t}
                break
    for (si, level) in highs:
        for j in range(si + 1, n):
            c = m15[j]
            if c.h > level and c.c < level:
                if j >= floor_idx:
                    found[("bear", j)] = {"side": "bear", "level": level, "idx": j, "extreme": c.h, "t": c.t}
                break
    return [found[k] for k in sorted(found, key=lambda x: x[1])]


def detect_mss(m5, start_idx, side, p: Params, m5_swings=None):
    n = len(m5)
    if start_idx >= n:
        return None
    end = min(n, start_idx + p.mss_window + 1)          # search ONLY inside the window after the sweep
    highs, lows = m5_swings if m5_swings is not None else swings(m5, p.sw_m5)
    if side == "bull":
        rl_idx = min(range(start_idx, end), key=lambda k: m5[k].l)   # local reaction low (not a future one)
        for (hi, pr) in [(i, v) for (i, v) in highs if rl_idx < i < end]:
            for k in range(hi + 1, end):
                if m5[k].c > pr:
                    return {"ref_idx": hi, "break_idx": k, "ext_idx": rl_idx}
        return None
    else:
        rh_idx = max(range(start_idx, end), key=lambda k: m5[k].h)   # local reaction high
        for (li, pr) in [(i, v) for (i, v) in lows if rh_idx < i < end]:
            for k in range(li + 1, end):
                if m5[k].c < pr:
                    return {"ref_idx": li, "break_idx": k, "ext_idx": rh_idx}
        return None


def _why_no_mss(m5, start_idx, side, p: Params, m5_swings):
    """Diagnose why MSS was not found: no structure (no swing to break) inside
    the window, or structure existed but price never closed through it."""
    n = len(m5)
    end = min(n, start_idx + p.mss_window + 1)
    highs, lows = m5_swings
    if side == "bull":
        rl_idx = min(range(start_idx, end), key=lambda k: m5[k].l)
        cands = [i for (i, _) in highs if rl_idx < i < end]
    else:
        rh_idx = max(range(start_idx, end), key=lambda k: m5[k].h)
        cands = [i for (i, _) in lows if rh_idx < i < end]
    return "not_broken" if cands else "no_structure"


def is_displacement(m5, k, atr_arr, side, p: Params):
    for idx in (k, k - 1):
        if idx < 0 or idx >= len(m5) or not atr_arr[idx]:
            continue
        c = m5[idx]
        body = abs(c.c - c.o)
        rng = (c.h - c.l) or 1e-9
        strong = body >= p.disp_atr_mult * atr_arr[idx] and (body / rng) >= p.disp_body_ratio
        dir_ok = (c.c > c.o) if side == "bull" else (c.c < c.o)
        if strong and dir_ok:
            return True
    return False


def find_fvg(m5, lo, hi, side):
    best = None
    top_i = min(len(m5) - 2, hi)
    for i in range(max(1, lo), top_i + 1):
        c1, c3 = m5[i - 1], m5[i + 1]
        if side == "bull" and c1.h < c3.l:
            best = {"idx": i, "bottom": c1.h, "top": c3.l}
        elif side == "bear" and c1.l > c3.h:
            best = {"idx": i, "top": c1.l, "bottom": c3.h}
    return best


def target_liquidity(h1, m15, entry, side, p: Params, min_dist=0.0):
    levels = []
    for tf, w in ((m15, p.sw_m15), (h1, p.sw_h1)):
        hs, ls = swings(tf, w)
        if side == "bull":
            levels += [v for (_, v) in hs if v >= entry + min_dist]
        else:
            levels += [v for (_, v) in ls if v <= entry - min_dist]
    if not levels:
        return None
    return min(levels) if side == "bull" else max(levels)


def sl_entry(m5, start_idx, mss, atr_arr, side, fvg, entry_mode="ce"):
    """SL beyond the reaction extreme; entry inside the FVG. entry_mode:
    'edge' = proximal (first touch), 'ce' = 50% mid (optimal entry), 'far' = deep."""
    seg = m5[start_idx:mss["break_idx"] + 1]
    buf = 0.1 * (atr_arr[mss["break_idx"]] or (m5[-1].h - m5[-1].l))
    mid = (fvg["top"] + fvg["bottom"]) / 2
    if side == "bull":
        react = min(x.l for x in seg)
        entry = fvg["top"] if entry_mode == "edge" else (fvg["bottom"] if entry_mode == "far" else mid)
        return react, react - buf, entry
    react = max(x.h for x in seg)
    entry = fvg["bottom"] if entry_mode == "edge" else (fvg["top"] if entry_mode == "far" else mid)
    return react, react + buf, entry


# ============================================================================
# Decision (live)
# ============================================================================
@dataclass
class Decision:
    signal: bool
    reason: str
    context: str = ""
    side: str = ""
    entry: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    rr: float = 0.0
    key: tuple = ()


def h1_context(h1, side, p: Params):
    highs, lows = swings(h1, p.sw_h1)
    bias = structure_bias(highs, lows)
    trend = {"up": "H1 up", "down": "H1 down", "mixed": "H1 ranging"}.get(bias, "H1 unclear")
    if (side == "bull" and bias == "up") or (side == "bear" and bias == "down"):
        kind = "with-trend"
    elif bias in ("up", "down"):
        kind = "reversal/correction"
    else:
        kind = "no clear context"
    return f"{trend} ({kind})"


def _eval_one(sweep, h1, m15, m5, m5_swings, atr_arr, m5_times, p) -> Decision:
    side = sweep["side"]
    ctx = h1_context(h1, side, p)
    dir_txt = "BUY" if side == "bull" else "SELL"

    start_idx = bisect.bisect_left(m5_times, sweep["t"])
    if start_idx >= len(m5):
        return Decision(False, f"[{dir_txt}] Sweep on M15 but no M5 candles after it yet.", ctx)
    mss = detect_mss(m5, start_idx, side, p, m5_swings)
    if not mss:
        return Decision(False, f"[{dir_txt}] Sweep found but no MSS on M5 within the window.", ctx)
    if not is_displacement(m5, mss["break_idx"], atr_arr, side, p):
        return Decision(False, f"[{dir_txt}] MSS but no strong displacement.", ctx)
    fvg = find_fvg(m5, mss["ref_idx"], mss["break_idx"] + 1, side)
    if not fvg:
        return Decision(False, f"[{dir_txt}] Displacement but no FVG.", ctx)

    react, sl, entry = sl_entry(m5, start_idx, mss, atr_arr, side, fvg, p.entry_mode)
    for k in range(mss["break_idx"] + 1, len(m5)):
        if side == "bull" and m5[k].c < react:
            return Decision(False, f"[{dir_txt}] Invalidated: closed below the sweep low.", ctx)
        if side == "bear" and m5[k].c > react:
            return Decision(False, f"[{dir_txt}] Invalidated: closed above the sweep high.", ctx)

    risk = abs(entry - sl)
    if risk <= 0:
        return Decision(False, f"[{dir_txt}] Stop distance is zero - skip.", ctx)
    key = (side, m5[fvg["idx"]].t)
    tp = target_liquidity(h1, m15, entry, side, p, p.rr_min * risk)
    if tp is None:
        return Decision(False, f"[{dir_txt}] No opposing liquidity far enough for RR>={p.rr_min:.2f}.",
                        ctx, side, entry, sl, 0.0, 0.0, key)
    rr = abs(tp - entry) / risk

    last, prev = m5[-1], (m5[-2] if len(m5) >= 2 else m5[-1])
    if side == "bull":
        tagged_now = last.l <= entry and prev.l > entry
    else:
        tagged_now = last.h >= entry and prev.h < entry
    if not tagged_now:
        return Decision(False, f"[{dir_txt}] Setup ready (RR={rr:.2f}) but waiting for retracement to FVG.",
                        ctx, side, entry, sl, tp, rr, key)

    return Decision(True, f"{dir_txt} signal - Sweep->MSS->Displacement->FVG->Retracement OK",
                    ctx, side, entry, sl, tp, rr, key)


def evaluate(h1, m15, m5, p: Params) -> Decision:
    if len(m5) < max(p.sw_m5 * 2 + 2, p.atr_period + 2) or \
       len(m15) < (p.sw_m15 * 2 + 2) or len(h1) < (p.sw_h1 * 2 + 2):
        return Decision(False, "Not enough data yet.")
    sweeps = find_all_sweeps(m15, p, use_lookback=True)
    if not sweeps:
        return Decision(False, "No recent liquidity sweep on M15.")
    m5_swings = swings(m5, p.sw_m5)
    atr_arr = atr_series(m5, p.atr_period)
    m5_times = [c.t for c in m5]
    first = None
    for sweep in reversed(sweeps):
        res = _eval_one(sweep, h1, m15, m5, m5_swings, atr_arr, m5_times, p)
        if res.signal:
            return res
        if first is None:
            first = res
    return first


# ============================================================================
# Text + Telegram
# ============================================================================
def _set_digits(dg):
    global PRICE_DIGITS
    PRICE_DIGITS = dg


def fmt(x):
    return f"{x:.{PRICE_DIGITS}f}"


def signal_text(sym, dec: Decision) -> str:
    emoji = "\U0001F7E2" if dec.side == "bull" else "\U0001F534"
    arrow = "BUY" if dec.side == "bull" else "SELL"
    return (f"{emoji} {arrow}  {sym}\n----------------\n"
            f"Entry : {fmt(dec.entry)}\nSL    : {fmt(dec.sl)}\nTP    : {fmt(dec.tp)}\n"
            f"RR    : 1 : {dec.rr:.2f}\nContext: {dec.context}\nSetup : {dec.reason}")


def telegram_send(text: str) -> bool:
    if not telegram_configured():
        print("[Telegram] Not configured (set the token and chat id) - skipping send.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as r:
            return json.loads(r.read().decode()).get("ok", False)
    except Exception as e:                       # noqa: BLE001
        print(f"[Telegram] Send failed: {e}")
        return False


# ============================================================================
# MT5 data feed
# ============================================================================
class MT5Feed:
    def __init__(self, params: Params):
        try:
            import MetaTrader5 as mt5            # noqa: N813
        except Exception as e:                   # noqa: BLE001
            raise RuntimeError("MetaTrader5 package not available. On Windows: pip install MetaTrader5") from e
        self.mt5 = mt5
        self.p = params
        self.tf_map = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
                       "M15": mt5.TIMEFRAME_M15, "H1": mt5.TIMEFRAME_H1}

    def connect(self):
        global PRICE_DIGITS
        kwargs = {}
        if MT5_TERMINAL_PATH:
            kwargs["path"] = MT5_TERMINAL_PATH
        if MT5_LOGIN and MT5_PASSWORD and MT5_SERVER:
            kwargs.update(login=int(MT5_LOGIN), password=MT5_PASSWORD, server=MT5_SERVER)
        if not self.mt5.initialize(**kwargs):
            raise RuntimeError(f"MT5 initialize failed: {self.mt5.last_error()}")
        if not self.mt5.symbol_select(self.p.symbol, True):
            raise RuntimeError(f"Symbol {self.p.symbol} is not available in MT5.")
        info = self.mt5.symbol_info(self.p.symbol)
        if info is not None and getattr(info, "digits", None) is not None:
            self.p.digits = info.digits
            PRICE_DIGITS = info.digits
        print(f"[MT5] Connected. Symbol: {self.p.symbol}  digits={self.p.digits}")

    def shutdown(self):
        try:
            self.mt5.shutdown()
        except Exception:                        # noqa: BLE001
            pass

    @staticmethod
    def _to_candles(rates):
        if rates is None:
            return []
        return [Candle(int(r["time"]), float(r["open"]), float(r["high"]),
                       float(r["low"]), float(r["close"])) for r in rates]

    def ensure(self, symbol: str) -> int:
        if not self.mt5.symbol_select(symbol, True):
            raise RuntimeError(f"Symbol {symbol} not available in MT5.")
        info = self.mt5.symbol_info(symbol)
        return info.digits if info is not None and getattr(info, "digits", None) is not None else 2

    def latest(self, tf: str, count: int, symbol=None):
        sym = symbol or self.p.symbol
        c = self._to_candles(self.mt5.copy_rates_from_pos(sym, self.tf_map[tf], 0, count))
        now = time.time()
        while c and c[-1].close_time(tf) > now:
            c.pop()
        return c

    def range(self, tf: str, dt_from: datetime, dt_to: datetime, symbol=None):
        sym = symbol or self.p.symbol
        return self._to_candles(self.mt5.copy_rates_range(sym, self.tf_map[tf], dt_from, dt_to))

    def current_price(self, symbol=None):
        tick = self.mt5.symbol_info_tick(symbol or self.p.symbol)
        if tick is None:
            return None
        return tick.bid, tick.ask, int(tick.time)


# ============================================================================
# Trade + M1 outcome resolution
# ============================================================================
@dataclass
class Trade:
    side: str
    context: str
    entry: float
    sl: float
    tp: float
    rr: float
    t_entry: int
    t_exit: int = 0
    exit_price: float = 0.0
    result: str = "OPEN"
    r_mult: float = 0.0


def _resolve_on_m1(tr: Trade, m1, m1_times, entry_open, max_hold=0):
    """Fill on M1 (limit at entry) then walk M1 forward. SL is checked before TP
    within each minute (pessimistic when both are touched in the same minute).
    If max_hold seconds pass without TP/SL, close at market (time-stop)."""
    i = bisect.bisect_left(m1_times, entry_open)
    filled = False
    fill_t = None
    for j in range(i, len(m1)):
        b = m1[j]
        if not filled:
            if tr.side == "bull":
                if b.l <= tr.entry:
                    filled = True; tr.t_entry = b.t; fill_t = b.t
                else:
                    continue
            else:
                if b.h >= tr.entry:
                    filled = True; tr.t_entry = b.t; fill_t = b.t
                else:
                    continue
        if tr.side == "bull":
            if b.l <= tr.sl:
                tr.result, tr.exit_price, tr.t_exit = "LOSS", tr.sl, b.t; break
            if b.h >= tr.tp:
                tr.result, tr.exit_price, tr.t_exit = "WIN", tr.tp, b.t; break
        else:
            if b.h >= tr.sl:
                tr.result, tr.exit_price, tr.t_exit = "LOSS", tr.sl, b.t; break
            if b.l <= tr.tp:
                tr.result, tr.exit_price, tr.t_exit = "WIN", tr.tp, b.t; break
        if max_hold and (b.t - fill_t) >= max_hold:
            gain = (b.c - tr.entry) if tr.side == "bull" else (tr.entry - b.c)
            tr.result = "WIN" if gain >= 0 else "LOSS"
            tr.exit_price, tr.t_exit = b.c, b.t
            break
    if tr.result in ("WIN", "LOSS"):
        risk = abs(tr.entry - tr.sl) or 1e-9
        tr.r_mult = (tr.exit_price - tr.entry) / risk if tr.side == "bull" else (tr.entry - tr.exit_price) / risk


def _ts(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


# ============================================================================
# Analysis / Backtest -- counts EVERY opportunity, M1-resolved outcomes
# ============================================================================
STAGES = [
    ("2_no_mss",     "sweep, no MSS"),
    ("3_no_disp",    "MSS, no displacement"),
    ("4_no_fvg",     "displacement, no FVG"),
    ("6_no_tp",      "valid, no TP for RR>=min"),
    ("5_invalid",    "invalidated before retrace"),
    ("7_no_retrace", "never retraced to FVG"),
    ("9_signal",     "ENTERED (signal)"),
]


def analyze(h1, m15, m5, m1, p: Params, days: int,
            ctx_tf="H1", sweep_tf="M15", entry_tf="M5"):
    if not m5:
        print("No entry-timeframe data.")
        return
    cutoff = m5[-1].close_time(entry_tf) - days * 86400
    m5_swings = swings(m5, p.sw_m5)
    atr_arr = atr_series(m5, p.atr_period)
    m5_times = [c.t for c in m5]
    m1_times = [c.t for c in m1]
    h1_ct = [c.close_time(ctx_tf) for c in h1]
    m15_ct = [c.close_time(sweep_tf) for c in m15]
    sweeps = find_all_sweeps(m15, p, use_lookback=False)

    st = Counter()
    trades = []
    seen = set()
    for sweep in sweeps:
        if sweep["t"] < cutoff:
            continue
        st["0_sweeps"] += 1
        side = sweep["side"]
        st["sweep_" + side] += 1
        start_idx = bisect.bisect_left(m5_times, sweep["t"])
        if start_idx >= len(m5):
            continue

        mss = detect_mss(m5, start_idx, side, p, m5_swings)
        if not mss:
            st["2_no_mss"] += 1
            st["nomss_" + _why_no_mss(m5, start_idx, side, p, m5_swings)] += 1
            continue
        if not is_displacement(m5, mss["break_idx"], atr_arr, side, p):
            st["3_no_disp"] += 1
            continue
        fvg = find_fvg(m5, mss["ref_idx"], mss["break_idx"] + 1, side)
        if not fvg:
            st["4_no_fvg"] += 1
            continue

        key = (side, m5[fvg["idx"]].t)
        if key in seen:
            continue
        seen.add(key)

        react, sl, entry = sl_entry(m5, start_idx, mss, atr_arr, side, fvg, p.entry_mode)
        risk = abs(entry - sl)
        if risk <= 0:
            continue
        asof = m5[mss["break_idx"]].close_time(entry_tf)
        h1a = h1[:bisect.bisect_right(h1_ct, asof)]
        m15a = m15[:bisect.bisect_right(m15_ct, asof)]
        tp = target_liquidity(h1a, m15a, entry, side, p, p.rr_min * risk)
        if tp is None:
            st["6_no_tp"] += 1
            continue
        rr = abs(tp - entry) / risk

        retrace_i, invalid = None, False
        for k in range(mss["break_idx"] + 1, len(m5)):
            c = m5[k]
            if side == "bull":
                if c.c < react:
                    invalid = True; break
                if c.l <= entry:
                    retrace_i = k; break
            else:
                if c.c > react:
                    invalid = True; break
                if c.h >= entry:
                    retrace_i = k; break
        if invalid:
            st["5_invalid"] += 1
            continue
        if retrace_i is None:
            st["7_no_retrace"] += 1
            continue

        st["9_signal"] += 1
        tr = Trade(side, h1_context(h1a, side, p), entry, sl, tp, rr, m5[retrace_i].t)
        if m1:
            _resolve_on_m1(tr, m1, m1_times, m5[retrace_i].t, p.max_hold_hours * 3600)
        trades.append(tr)

    _print_analysis(p, days, st, trades, bool(m1), entry_tf)
    return trades


def _equity_stats(trades, balance, risk_pct):
    """Compound each closed trade by risking risk_pct of current equity; return
    final balance and max peak-to-trough drawdown."""
    eq = peak = balance
    max_dd = 0.0
    for t in sorted((x for x in trades if x.result in ("WIN", "LOSS")), key=lambda x: x.t_entry):
        eq += eq * (risk_pct / 100.0) * t.r_mult
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)
    return eq, max_dd


def _print_analysis(p: Params, days: int, st: Counter, trades, m1_used, entry_tf="M5"):
    wins = [t for t in trades if t.result == "WIN"]
    losses = [t for t in trades if t.result == "LOSS"]
    opens = [t for t in trades if t.result == "OPEN"]
    closed = len(wins) + len(losses)
    sweeps = st.get("0_sweeps", 0)

    print("\n" + "=" * 70)
    print(f"  ANALYSIS - {p.symbol} - last {days} days   [entry {entry_tf}]"
          + ("   [outcomes on M1]" if m1_used else "   [outcomes unresolved]"))
    print("=" * 70)
    print(f"  Opportunities (sweeps found) : {sweeps}")
    print("  Where each opportunity stopped:")
    for kk, label in STAGES:
        n = st.get(kk, 0)
        pct = (100.0 * n / sweeps) if sweeps else 0.0
        print(f"    {label:28s}: {n:6d}  ({pct:4.1f}%)")
    print("  Diagnostics of the 'sweep, no MSS' wall:")
    print(f"    sweeps  bull / bear        : {st.get('sweep_bull', 0)} / {st.get('sweep_bear', 0)}")
    print(f"    no-MSS: structure NOT broken: {st.get('nomss_not_broken', 0)}"
          f"   (real - price never broke structure)")
    print(f"    no-MSS: NO structure formed : {st.get('nomss_no_structure', 0)}"
          f"   (widen --mss-window / lower --sw-m5)")
    print("=" * 70)
    print(f"  ENTERED signals : {len(trades)}   (~{len(trades) / max(days, 1):.2f}/day)")
    print(f"  Wins            : {len(wins)}")
    print(f"  Losses          : {len(losses)}")
    if opens:
        print(f"  Unresolved      : {len(opens)}  (no M1 data / still open at end)")
    if closed:
        print(f"  Win rate        : {100.0 * len(wins) / closed:.1f}%")
        print(f"  Net R           : {sum(t.r_mult for t in wins + losses):+.2f} R")
        final, max_dd = _equity_stats(trades, p.balance, p.risk_pct)
        print(f"  --- ${p.balance:.0f} account, risk {p.risk_pct:.1f}%/trade ---")
        print(f"  Final balance   : ${final:,.2f}  ({(final / p.balance - 1) * 100:+.1f}%)")
        print(f"  Max drawdown    : {max_dd * 100:.1f}%")
    print("=" * 70)

    if not trades:
        print("  No entered signals. See the funnel above for the wall.")
        return
    print("\n  Trade details:")
    print("  " + "-" * 66)
    for n_, t in enumerate(trades, 1):
        side = "BUY " if t.side == "bull" else "SELL"
        print(f"  #{n_:<3} {side} [{t.result:4s}]  RR=1:{t.rr:.2f}  R={t.r_mult:+.2f}")
        print(f"       entry: {_ts(t.t_entry)}   @ {fmt(t.entry)}")
        print(f"       SL   : {fmt(t.sl)}    TP: {fmt(t.tp)}")
        if t.result != "OPEN":
            print(f"       exit : {_ts(t.t_exit)}   @ {fmt(t.exit_price)}")
        print(f"       ctx  : {t.context}")
        print("  " + "-" * 66)


def _print_combined(p, days, n_symbols, trades):
    wins = [t for t in trades if t.result == "WIN"]
    losses = [t for t in trades if t.result == "LOSS"]
    closed = len(wins) + len(losses)
    print("\n" + "#" * 70)
    print(f"  COMBINED - {n_symbols} symbols - last {days} days")
    print("#" * 70)
    print(f"  Total signals : {len(trades)}   (~{len(trades) / max(days, 1):.2f}/day)")
    print(f"  Wins / Losses : {len(wins)} / {len(losses)}")
    if closed:
        print(f"  Win rate      : {100.0 * len(wins) / closed:.1f}%")
        print(f"  Net R         : {sum(t.r_mult for t in wins + losses):+.2f} R")
        final, max_dd = _equity_stats(trades, p.balance, p.risk_pct)
        print(f"  ${p.balance:.0f} acct (risk {p.risk_pct:.1f}%): final ${final:,.2f}"
              f"  ({(final / p.balance - 1) * 100:+.1f}%)   max DD {max_dd * 100:.1f}%")
    print("#" * 70)


# ============================================================================
# Live mode: every 5 minutes, 24h (no session filter)
# ============================================================================
def load_sent(path="sent_signals.json"):
    try:
        with open(path) as f:
            return set(tuple(k) for k in json.load(f))
    except Exception:                            # noqa: BLE001
        return set()


def save_sent(keys, path="sent_signals.json"):
    try:
        with open(path, "w") as f:
            json.dump([list(k) for k in keys], f)
    except Exception:                            # noqa: BLE001
        pass


def live_loop(feed: MT5Feed, p: Params, symbols, ctx_tf="H1", sweep_tf="M15", entry_tf="M5", refresh=300):
    print("\n" + "=" * 70)
    print(f"  LIVE - {len(symbols)} symbol(s) - every {refresh // 60} min "
          f"({ctx_tf}/{sweep_tf}/{entry_tf}, 24h)")
    print("=" * 70)
    sent = load_sent()
    digits = {}
    while True:
        now = time.time()
        time.sleep(max(1, (int(now) // refresh + 1) * refresh + 5 - now))
        found = 0
        for sym in symbols:
            try:
                if sym not in digits:
                    digits[sym] = feed.ensure(sym)
                _set_digits(digits[sym])
                h1 = feed.latest(ctx_tf, 600, sym)
                m15 = feed.latest(sweep_tf, 1500, sym)
                m5 = feed.latest(entry_tf, p.win_m5 + 50, sym)
            except Exception as e:               # noqa: BLE001
                print(f"[{_ts(int(time.time()))}] {sym} data error: {e}")
                continue
            dec = evaluate(h1, m15, m5, p)
            stamp = _ts(m5[-1].close_time(entry_tf)) if m5 else _ts(int(time.time()))
            if dec.signal:
                key = (sym,) + tuple(dec.key)
                if key in sent:
                    continue
                text = signal_text(sym, dec)
                print(f"\n[{stamp}] === SIGNAL ({sym}) ===\n{text}\n")
                telegram_send(text)
                sent.add(key)
                save_sent(sent)
                found += 1
            elif len(symbols) == 1:
                print(f"[{stamp}] NO TRADE - {dec.reason}" + (f"  | {dec.context}" if dec.context else ""))
        if len(symbols) > 1:
            print(f"[{_ts(int(time.time()))}] scanned {len(symbols)} symbols - {found} signal(s)")


# ============================================================================
# Self-test (no MT5) -- builds M1 base then aggregates
# ============================================================================
def _gen_m1(n=137000, seed=7, start_price=2000.0):
    import random
    rnd = random.Random(seed)
    t0 = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
    price = start_price
    out = []
    for i in range(n):
        o = price
        c = max(1.0, o + rnd.gauss(math.sin(i / 2500.0) * 0.02, 0.25))
        hi = max(o, c) + abs(rnd.gauss(0, 0.15))
        lo = min(o, c) - abs(rnd.gauss(0, 0.15))
        out.append(Candle(t0 + i * 60, round(o, 2), round(hi, 2), round(lo, 2), round(c, 2)))
        price = c
    return out


def _aggregate(base, tf):
    step = TF_SECONDS[tf]
    buckets, order = {}, []
    for c in base:
        b = (c.t // step) * step
        if b not in buckets:
            buckets[b] = [c.o, c.h, c.l, c.c]
            order.append(b)
        else:
            g = buckets[b]
            g[1] = max(g[1], c.h); g[2] = min(g[2], c.l); g[3] = c.c
    return [Candle(b, *buckets[b]) for b in order]


def selftest(p: Params, fast=False):
    print("[selftest] Synthetic data, honest analysis (M1-resolved, no MT5)...")
    m1 = _gen_m1()
    if fast:
        analyze(_aggregate(m1, "M15"), _aggregate(m1, "M5"), m1, m1, p, 90, "M15", "M5", "M1")
    else:
        analyze(_aggregate(m1, "H1"), _aggregate(m1, "M15"), _aggregate(m1, "M5"), m1, p, 90)


# ============================================================================
# main
# ============================================================================
def build_params(args) -> Params:
    p = Params()
    for f_ in ("symbol", "sweep_lookback", "mss_window", "atr_period",
               "disp_atr_mult", "disp_body_ratio", "rr_min", "sw_h1", "sw_m15", "sw_m5",
               "balance", "risk_pct", "max_hold_hours", "entry_mode"):
        v = getattr(args, f_, None)
        if v is not None:
            setattr(p, f_, v)
    return p


def main():
    ap = argparse.ArgumentParser(description="ICT/SMC Intraday Bot (MT5 + Telegram) - single file.")
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--symbols", default=None,
                    help="comma-separated basket, overrides --symbol (e.g. XAUUSD,EURUSD,GBPUSD)")
    ap.add_argument("--backtest-only", action="store_true")
    ap.add_argument("--live-only", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--test-telegram", action="store_true",
                    help="send one test message to Telegram and exit")
    ap.add_argument("--fast", action="store_true", help="faster timeframes M15/M5/M1 (more trades)")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--balance", type=float, dest="balance", help="account example balance (default 1000)")
    ap.add_argument("--risk-pct", type=float, dest="risk_pct", help="%% of equity risked per trade (default 1.0)")
    ap.add_argument("--max-hold-hours", type=float, dest="max_hold_hours", help="time-stop hours (0 = off, default 24)")
    ap.add_argument("--entry", dest="entry_mode", choices=["ce", "edge", "far"], help="entry inside FVG (default ce)")
    ap.add_argument("--rr-min", type=float, dest="rr_min")
    ap.add_argument("--disp-atr-mult", type=float, dest="disp_atr_mult")
    ap.add_argument("--disp-body-ratio", type=float, dest="disp_body_ratio")
    ap.add_argument("--sweep-lookback", type=int, dest="sweep_lookback")
    ap.add_argument("--mss-window", type=int, dest="mss_window")
    ap.add_argument("--atr-period", type=int, dest="atr_period")
    ap.add_argument("--sw-h1", type=int, dest="sw_h1")
    ap.add_argument("--sw-m15", type=int, dest="sw_m15")
    ap.add_argument("--sw-m5", type=int, dest="sw_m5")
    args = ap.parse_args()
    p = build_params(args)

    if args.test_telegram:
        if not telegram_configured():
            print("[Telegram] Not configured. Edit TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
                  "near the top of this file (or set them as env vars).")
            return
        ok = telegram_send("✅ ICT/SMC Bot - Telegram test message. "
                           "Token and chat id are working.")
        print("[Telegram] Test message sent OK - check your Telegram." if ok
              else "[Telegram] Test FAILED - check token, chat id, and that "
                   "you pressed Start on the bot.")
        return

    if args.selftest:
        selftest(p, fast=args.fast)
        return

    if args.fast:
        ctx_tf, sweep_tf, entry_tf, res_tf, refresh = "M15", "M5", "M1", "M1", 60
    else:
        ctx_tf, sweep_tf, entry_tf, res_tf, refresh = "H1", "M15", "M5", "M1", 300

    symbols = [x.strip() for x in args.symbols.split(",") if x.strip()] if args.symbols else [p.symbol]

    feed = MT5Feed(p)
    feed.connect()
    telegram_send("ICT/SMC Bot started (XAUUSD). Telegram connected - signals will arrive here.")
    try:
        if not args.live_only:
            now = datetime.now(timezone.utc)
            dt_from = now - timedelta(days=args.days + 15)
            all_trades = []
            for sym in symbols:
                _set_digits(feed.ensure(sym))
                p.symbol = sym
                px = feed.current_price(sym)
                if px:
                    print(f"\n[{sym}] price bid={px[0]:.{PRICE_DIGITS}f} ask={px[1]:.{PRICE_DIGITS}f}"
                          f" ({_ts(px[2])} UTC) - fetching {ctx_tf}/{sweep_tf}/{entry_tf}...")
                ctx = feed.range(ctx_tf, dt_from, now, sym)
                sweepc = feed.range(sweep_tf, dt_from, now, sym)
                entryc = feed.range(entry_tf, dt_from, now, sym)
                resc = entryc if res_tf == entry_tf else feed.range(res_tf, dt_from, now, sym)
                if not resc:
                    print("[warn] No resolve-timeframe data - outcomes unresolved. Increase MT5 max bars.")
                all_trades += analyze(ctx, sweepc, entryc, resc, p, args.days, ctx_tf, sweep_tf, entry_tf)
            if len(symbols) > 1:
                _print_combined(p, args.days, len(symbols), all_trades)
        if not args.backtest_only:
            live_loop(feed, p, symbols, ctx_tf, sweep_tf, entry_tf, refresh)
    finally:
        feed.shutdown()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.")
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
