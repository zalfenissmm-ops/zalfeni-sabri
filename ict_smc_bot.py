#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
ICT / SMC Intraday Bot  --  single file  (Gold / XAUUSD by default)
================================================================================
Strategy:

    H1  -> Context: classify the trade (with-trend / reversal-correction).
           Label only; it does NOT block the trade.
    M15 -> Liquidity + Sweep  (break a high/low then close back = liquidity trap).
    M5  -> MSS + Displacement + FVG.
    Then Retracement into the FVG = entry.

    Sweep -> MSS -> Displacement -> FVG -> Retracement -> good RR -> signal.

Golden rules:
    - Breaking a low alone  != SELL.
    - Breaking a high alone != BUY.
    - Sweep + MSS required.
    - Displacement + FVG required.
    - SL below/above the sweep extreme.
    - TP at the opposing liquidity.
    - RR >= 1.5 or NO TRADE.

--------------------------------------------------------------------------------
How it runs:
    1) On start -> one 90-day backtest: prints every signal classified as
       WIN / LOSS with details, plus a summary.
    2) Then live mode: every 5 minutes it pulls fresh data from MT5 and prints:
         - a trade    -> Entry / SL / TP / RR  and sends a Telegram alert.
         - NO TRADE    -> the reason (which stage stopped).

    Runs 24h; there is no trading-session filter (gold trades ~23h/weekday).

--------------------------------------------------------------------------------
Commands:
    python3 ict_smc_bot.py                  # 90-day backtest then live every 5m
    python3 ict_smc_bot.py --backtest-only  # backtest only
    python3 ict_smc_bot.py --live-only      # live only (no backtest)
    python3 ict_smc_bot.py --selftest       # test the engine on synthetic data
    python3 ict_smc_bot.py --symbol XAUUSD --rr-min 2.0 ...

Requirements for live trading: MetaTrader5 installed (Windows) + package:
    pip install MetaTrader5
================================================================================
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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# ============================================================================
# 1) Settings  --  put your Telegram bot token and chat id here
# ============================================================================
# How to get them:
#   1. In Telegram talk to @BotFather -> /newbot -> copy the TOKEN
#   2. Send any message to your bot, then open in a browser:
#        https://api.telegram.org/bot<TOKEN>/getUpdates
#      and copy the number  "chat":{"id": ... }
#
# You can hard-code them here, or set them as environment variables
# TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "PUT_YOUR_CHAT_ID_HERE")

# MT5 settings (optional: if set, the bot logs in itself; otherwise it uses
# the terminal you already have open and logged in).
MT5_LOGIN         = os.environ.get("MT5_LOGIN")          # e.g. "51234567"
MT5_PASSWORD      = os.environ.get("MT5_PASSWORD")
MT5_SERVER        = os.environ.get("MT5_SERVER")         # e.g. "ICMarkets-Demo"
MT5_TERMINAL_PATH = os.environ.get("MT5_TERMINAL_PATH")  # path to terminal64.exe

_PLACEHOLDERS = ("PUT_YOUR_BOT_TOKEN_HERE", "PUT_YOUR_CHAT_ID_HERE", "", None)

# Number of decimals used to print prices. Gold is usually 2.
# Auto-updated from MT5 symbol info at connect time.
PRICE_DIGITS = 2


def telegram_configured() -> bool:
    return TELEGRAM_BOT_TOKEN not in _PLACEHOLDERS and TELEGRAM_CHAT_ID not in _PLACEHOLDERS


# ============================================================================
# 2) Candle model + timeframes
# ============================================================================
TF_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "H1": 3600}


@dataclass
class Candle:
    t: int      # open time (epoch seconds, UTC)
    o: float
    h: float
    l: float
    c: float

    def close_time(self, tf: str) -> int:
        return self.t + TF_SECONDS[tf]


@dataclass
class Params:
    """All tunable numbers. Overridable from the command line."""
    symbol: str = "XAUUSD"        # gold
    digits: int = 2               # price decimals for display

    # swing detection (fractal window per timeframe)
    sw_h1: int = 4
    sw_m15: int = 3
    sw_m5: int = 2

    equal_tol: float = 0.0007     # "equal highs/lows" tolerance (relative) = liquidity pool

    sweep_lookback: int = 30      # look for a sweep in the last N M15 candles
    mss_window: int = 18          # MSS must occur within N M5 candles after the sweep

    atr_period: int = 14
    disp_atr_mult: float = 1.3    # displacement candle: body >= 1.3 x ATR
    disp_body_ratio: float = 0.5  # and body >= 50% of its range

    rr_min: float = 1.5           # minimum acceptable Risk:Reward

    # decision windows in live/backtest (candles per timeframe)
    win_m5: int = 300
    win_m15: int = 220
    win_h1: int = 200


# ============================================================================
# 3) Indicators & structure tools (pure Python, no libraries)
# ============================================================================
def atr_series(candles, period):
    """ATR (average true range) -- list as long as candles (None before period fills)."""
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
    """Confirmed swing highs/lows (fractal): a bar is a swing if it is the
    extreme within w bars on each side. The last w bars are not confirmed
    (so there is no look-ahead)."""
    highs, lows = [], []
    n = len(candles)
    for i in range(w, n - w):
        seg = candles[i - w:i + w + 1]
        hi = candles[i].h
        lo = candles[i].l
        if hi == max(x.h for x in seg):
            highs.append((i, hi))
        if lo == min(x.l for x in seg):
            lows.append((i, lo))
    return highs, lows


def structure_bias(highs, lows):
    """Structure direction from the last two swing highs/lows."""
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
# 4) Strategy stages
# ============================================================================
def find_recent_sweep(m15, p: Params):
    """Most recent liquidity sweep on M15:
       - Bull sweep : price breaks a swing low and closes back above  -> look BUY.
       - Bear sweep : price breaks a swing high and closes back below  -> look SELL.
       Returns the latest one."""
    highs, lows = swings(m15, p.sw_m15)
    n = len(m15)
    floor_idx = max(0, n - p.sweep_lookback)
    best = None

    for (si, level) in lows:                       # sell-side liquidity -> bull sweep
        for j in range(si + 1, n):
            c = m15[j]
            if c.l < level and c.c > level:        # wick below + close above
                if j >= floor_idx and (best is None or j > best["idx"]):
                    best = {"side": "bull", "level": level, "idx": j,
                            "extreme": c.l, "t": c.t}
                break

    for (si, level) in highs:                      # buy-side liquidity -> bear sweep
        for j in range(si + 1, n):
            c = m15[j]
            if c.h > level and c.c < level:
                if j >= floor_idx and (best is None or j > best["idx"]):
                    best = {"side": "bear", "level": level, "idx": j,
                            "extreme": c.h, "t": c.t}
                break

    return best


def detect_mss(m5, start_idx, side, p: Params):
    """MSS on M5 after the sweep, by close (stronger than a wick), within the
       validity window. Bull: break the last Lower High up. Bear: break the last
       Higher Low down."""
    n = len(m5)
    if start_idx >= n:
        return None

    if side == "bull":
        rl_idx = min(range(start_idx, n), key=lambda k: m5[k].l)   # reaction low
        highs, _ = swings(m5, p.sw_m5)
        for (hi, pr) in [(i, v) for (i, v) in highs if i > rl_idx]:
            for k in range(hi + 1, n):
                if k - start_idx > p.mss_window:
                    break
                if m5[k].c > pr:
                    return {"ref_idx": hi, "ref": pr, "break_idx": k, "ext_idx": rl_idx}
        return None
    else:
        rh_idx = max(range(start_idx, n), key=lambda k: m5[k].h)   # reaction high
        _, lows = swings(m5, p.sw_m5)
        for (li, pr) in [(i, v) for (i, v) in lows if i > rh_idx]:
            for k in range(li + 1, n):
                if k - start_idx > p.mss_window:
                    break
                if m5[k].c < pr:
                    return {"ref_idx": li, "ref": pr, "break_idx": k, "ext_idx": rh_idx}
        return None


def is_displacement(m5, k, atr_arr, side, p: Params):
    """Clear displacement: body >= disp_atr_mult x ATR and >= disp_body_ratio of
       the range, in the trade direction. Accept the break candle k or the one
       before it."""
    for idx in (k, k - 1):
        if idx < 0 or idx >= len(m5):
            continue
        a = atr_arr[idx] if atr_arr[idx] else None
        if not a:
            continue
        c = m5[idx]
        body = abs(c.c - c.o)
        rng = (c.h - c.l) or 1e-9
        strong = body >= p.disp_atr_mult * a and (body / rng) >= p.disp_body_ratio
        dir_ok = (c.c > c.o) if side == "bull" else (c.c < c.o)
        if strong and dir_ok:
            return True
    return False


def find_fvg(m5, lo, hi, side):
    """FVG (fair value gap): imbalance between the two candles around the
       displacement candle, within [lo..hi].
       Bull: c1.high < c3.low.   Bear: c1.low > c3.high.  Returns the latest."""
    best = None
    top_i = min(len(m5) - 2, hi)
    for i in range(max(1, lo), top_i + 1):
        c1, c3 = m5[i - 1], m5[i + 1]
        if side == "bull" and c1.h < c3.l:
            best = {"idx": i, "bottom": c1.h, "top": c3.l}
        elif side == "bear" and c1.l > c3.h:
            best = {"idx": i, "top": c1.l, "bottom": c3.h}
    return best


def target_liquidity(h1, m15, entry, side, p: Params):
    """TP = nearest opposing liquidity (a high above for BUY / a low below for
       SELL) taken from M15 and H1 swings."""
    levels = []
    for tf, w in ((m15, p.sw_m15), (h1, p.sw_h1)):
        hs, ls = swings(tf, w)
        if side == "bull":
            levels += [v for (_, v) in hs if v > entry]
        else:
            levels += [v for (_, v) in ls if v < entry]
    if not levels:
        return None
    return min(levels) if side == "bull" else max(levels)


# ============================================================================
# 5) Decision: a signal or NO TRADE + reason
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
    info: dict = field(default_factory=dict)


def h1_context(h1, side, p: Params):
    """Classify H1: with-trend or reversal/correction (label only, no blocking)."""
    highs, lows = swings(h1, p.sw_h1)
    bias = structure_bias(highs, lows)
    if bias == "up":
        trend = "H1 up"
    elif bias == "down":
        trend = "H1 down"
    elif bias == "mixed":
        trend = "H1 ranging"
    else:
        trend = "H1 unclear"
    if (side == "bull" and bias == "up") or (side == "bear" and bias == "down"):
        kind = "with-trend"
    elif bias in ("up", "down"):
        kind = "reversal/correction"
    else:
        kind = "no clear context"
    return f"{trend} ({kind})"


def evaluate(h1, m15, m5, p: Params) -> Decision:
    """The engine: walks stage by stage and stops at the first that fails,
       returning the reason."""
    need = max(p.sw_m5 * 2 + 2, p.atr_period + 2)
    if len(m5) < need or len(m15) < (p.sw_m15 * 2 + 2) or len(h1) < (p.sw_h1 * 2 + 2):
        return Decision(False, "Not enough data yet (waiting for more candles).")

    # (1) Sweep on M15
    sweep = find_recent_sweep(m15, p)
    if not sweep:
        return Decision(False, "No recent liquidity sweep on M15 - no liquidity trap.")
    side = sweep["side"]
    ctx = h1_context(h1, side, p)
    dir_txt = "BUY" if side == "bull" else "SELL"

    # find the reaction start on M5 (first M5 candle after the sweep candle opened)
    m5_times = [c.t for c in m5]
    start_idx = bisect.bisect_left(m5_times, sweep["t"])
    if start_idx >= len(m5):
        return Decision(False, f"[{dir_txt}] Sweep on M15 but no M5 candles after it yet.", ctx)

    # (2) MSS on M5
    mss = detect_mss(m5, start_idx, side, p)
    if not mss:
        return Decision(False,
                        f"[{dir_txt}] Sweep found but no MSS on M5 within the window ({p.mss_window} candles).",
                        ctx)

    # (3) Displacement
    atr_arr = atr_series(m5, p.atr_period)
    if not is_displacement(m5, mss["break_idx"], atr_arr, side, p):
        return Decision(False,
                        f"[{dir_txt}] MSS occurred but without strong displacement (weak impulse).",
                        ctx)

    # (4) FVG
    fvg = find_fvg(m5, mss["ref_idx"], mss["break_idx"] + 1, side)
    if not fvg:
        return Decision(False,
                        f"[{dir_txt}] Displacement present but left no FVG.",
                        ctx)

    # sweep extreme (for SL) + entry (near edge of the FVG)
    seg = m5[start_idx:mss["break_idx"] + 1]
    a = atr_arr[mss["break_idx"]] or (m5[-1].h - m5[-1].l)
    buf = 0.1 * a
    if side == "bull":
        react = min(x.l for x in seg)
        sl = react - buf
        entry = fvg["top"]                     # upper edge of the FVG (proximal from above)
    else:
        react = max(x.h for x in seg)
        sl = react + buf
        entry = fvg["bottom"]                  # lower edge of the FVG (proximal from below)

    # (5) invalidation: after MSS, price closed beyond the sweep point before returning to the FVG
    for k in range(mss["break_idx"] + 1, len(m5)):
        if side == "bull" and m5[k].c < react:
            return Decision(False, f"[{dir_txt}] Idea invalidated: price closed below the sweep low.", ctx)
        if side == "bear" and m5[k].c > react:
            return Decision(False, f"[{dir_txt}] Idea invalidated: price closed above the sweep high.", ctx)

    # (6) TP = opposing liquidity + RR filter
    tp = target_liquidity(h1, m15, entry, side, p)
    if tp is None:
        return Decision(False, f"[{dir_txt}] No clear opposing liquidity to use as TP.", ctx)

    risk = abs(entry - sl)
    reward = abs(tp - entry)
    if risk <= 0:
        return Decision(False, f"[{dir_txt}] Stop distance is zero - skip.", ctx)
    rr = reward / risk
    key = (sweep["idx"], mss["break_idx"], fvg["idx"])
    base_info = {"sweep": sweep, "mss": mss, "fvg": fvg, "react": react}

    if rr < p.rr_min:
        return Decision(False,
                        f"[{dir_txt}] RR too small {rr:.2f} < {p.rr_min:.2f} (target too close) -> NO TRADE.",
                        ctx, side, entry, sl, tp, rr, key, base_info)

    # (7) Retracement: price must tag the FVG now (first touch)
    last = m5[-1]
    prev = m5[-2] if len(m5) >= 2 else last
    if side == "bull":
        tagged_now = last.l <= fvg["top"] and prev.l > fvg["top"]
    else:
        tagged_now = last.h >= fvg["bottom"] and prev.h < fvg["bottom"]

    if not tagged_now:
        return Decision(False,
                        f"[{dir_txt}] Setup ready and RR={rr:.2f}, but price has not returned to the FVG "
                        f"yet (waiting for retracement).",
                        ctx, side, entry, sl, tp, rr, key, base_info)

    # full signal
    return Decision(True,
                    f"{dir_txt} signal - Sweep->MSS->Displacement->FVG->Retracement OK",
                    ctx, side, entry, sl, tp, rr, key, base_info)


# ============================================================================
# 6) Text formatting + Telegram
# ============================================================================
def fmt(x, d=None):
    return f"{x:.{PRICE_DIGITS if d is None else d}f}"


def signal_text(sym, dec: Decision) -> str:
    arrow = "BUY" if dec.side == "bull" else "SELL"
    emoji = "\U0001F7E2" if dec.side == "bull" else "\U0001F534"  # green / red circle
    return (
        f"{emoji} {arrow}  {sym}\n"
        f"----------------\n"
        f"Entry : {fmt(dec.entry)}\n"
        f"SL    : {fmt(dec.sl)}\n"
        f"TP    : {fmt(dec.tp)}\n"
        f"RR    : 1 : {dec.rr:.2f}\n"
        f"Context: {dec.context}\n"
        f"Setup : {dec.reason}"
    )


def telegram_send(text: str) -> bool:
    if not telegram_configured():
        print("[Telegram] Not configured (set the token and chat id) - skipping send.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as r:
            ok = json.loads(r.read().decode()).get("ok", False)
            if not ok:
                print("[Telegram] Response was not ok.")
            return ok
    except Exception as e:                       # noqa: BLE001
        print(f"[Telegram] Send failed: {e}")
        return False


# ============================================================================
# 7) MT5 - data feed
# ============================================================================
class MT5Feed:
    def __init__(self, params: Params):
        try:
            import MetaTrader5 as mt5            # noqa: N813
        except Exception as e:                   # noqa: BLE001
            raise RuntimeError(
                "MetaTrader5 package not available. Install it on Windows:  pip install MetaTrader5"
            ) from e
        self.mt5 = mt5
        self.p = params
        self.tf_map = {
            "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15, "H1": mt5.TIMEFRAME_H1,
        }

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
        out = []
        if rates is None:
            return out
        for r in rates:
            out.append(Candle(int(r["time"]), float(r["open"]), float(r["high"]),
                              float(r["low"]), float(r["close"])))
        return out

    def latest(self, tf: str, count: int):
        rates = self.mt5.copy_rates_from_pos(self.p.symbol, self.tf_map[tf], 0, count)
        c = self._to_candles(rates)
        # drop the last candle if it has not closed yet
        now = time.time()
        while c and c[-1].close_time(tf) > now:
            c.pop()
        return c

    def range(self, tf: str, dt_from: datetime, dt_to: datetime):
        rates = self.mt5.copy_rates_range(self.p.symbol, self.tf_map[tf], dt_from, dt_to)
        return self._to_candles(rates)


# ============================================================================
# 8) Backtest - 90 days, WIN/LOSS classification, print every trade
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
    result: str = "OPEN"      # WIN / LOSS / OPEN
    r_mult: float = 0.0


def _slice_tail(arr_times, arr, upto_close, tf, win):
    """Last `win` candles closed before upto_close."""
    j = bisect.bisect_right(arr_times, upto_close - TF_SECONDS[tf])
    return arr[max(0, j - win):j]


def _classify_stage(dec):
    """Map a Decision to the funnel stage it stopped at (for tuning)."""
    if dec.signal:
        return "9  SIGNAL"
    r = dec.reason
    if "Not enough data" in r:            return "0  insufficient data"
    if "No recent liquidity sweep" in r:  return "1  no M15 sweep"
    if "no M5 candles after" in r:        return "2  sweep, no M5 yet"
    if "no MSS" in r:                     return "2  sweep, no MSS"
    if "without strong displacement" in r:return "3  MSS, no displacement"
    if "left no FVG" in r:                return "4  displacement, no FVG"
    if "invalidated" in r:                return "5  invalidated before retrace"
    if "opposing liquidity" in r:         return "6  no TP liquidity"
    if "Stop distance is zero" in r:      return "6  zero-stop skip"
    if "RR too small" in r:               return "7  RR below minimum"
    if "waiting for retracement" in r:    return "8  valid setup, waiting FVG tap"
    return "?  other"


def run_backtest(h1, m15, m5, p: Params, days: int):
    """Walks M5 candles (a decision every 5m), evaluates with the same engine
       and no look-ahead, then simulates each signal forward to mark WIN/LOSS.
       Also builds a funnel of where setups stopped, to help tuning."""
    from collections import Counter
    if not m5:
        print("No M5 data for the backtest.")
        return

    cutoff = m5[-1].close_time("M5") - days * 86400
    start_i = bisect.bisect_left([c.t for c in m5], cutoff)
    start_i = max(start_i, p.win_m5)

    m15_t = [c.t for c in m15]
    h1_t = [c.t for c in h1]

    trades = []
    seen_keys = set()
    funnel = Counter()
    rr_rejects = {}
    waiting = {}
    open_trade = None
    i = start_i
    n = len(m5)

    while i < n:
        bar = m5[i]

        # if a trade is open, follow it until it closes (one trade at a time)
        if open_trade is not None:
            if _check_exit(open_trade, bar):
                open_trade.t_exit = bar.t
                trades.append(open_trade)
                open_trade = None
            i += 1
            continue

        upto = bar.close_time("M5")
        m5s = m5[max(0, i - p.win_m5 + 1):i + 1]
        m15s = _slice_tail(m15_t, m15, upto, "M15", p.win_m15)
        h1s = _slice_tail(h1_t, h1, upto, "H1", p.win_h1)

        dec = evaluate(h1s, m15s, m5s, p)
        funnel[_classify_stage(dec)] += 1
        if dec.key:
            if "RR too small" in dec.reason:
                rr_rejects[dec.key] = dec.rr
            elif "waiting for retracement" in dec.reason:
                waiting[dec.key] = dec.rr

        if dec.signal and dec.key not in seen_keys:
            seen_keys.add(dec.key)
            open_trade = Trade(dec.side, dec.context, dec.entry, dec.sl, dec.tp,
                               dec.rr, bar.t)
            # simulation starts on the next candle
        i += 1

    if open_trade is not None:
        trades.append(open_trade)   # still open at the end of the data

    _print_backtest_report(p, days, trades, funnel, rr_rejects, waiting)


def _check_exit(tr: Trade, bar: Candle):
    """Did this candle close the trade? (assume SL fills before TP on conflict = conservative)."""
    if tr.side == "bull":
        if bar.l <= tr.sl:
            tr.result = "LOSS"; tr.exit_price = tr.sl
        elif bar.h >= tr.tp:
            tr.result = "WIN"; tr.exit_price = tr.tp
    else:
        if bar.h >= tr.sl:
            tr.result = "LOSS"; tr.exit_price = tr.sl
        elif bar.l <= tr.tp:
            tr.result = "WIN"; tr.exit_price = tr.tp
    if tr.result in ("WIN", "LOSS"):
        risk = abs(tr.entry - tr.sl) or 1e-9
        tr.r_mult = (tr.exit_price - tr.entry) / risk if tr.side == "bull" \
            else (tr.entry - tr.exit_price) / risk
        return True
    return False


def _ts(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _print_backtest_report(p: Params, days: int, trades, funnel=None,
                           rr_rejects=None, waiting=None):
    wins = [t for t in trades if t.result == "WIN"]
    losses = [t for t in trades if t.result == "LOSS"]
    opens = [t for t in trades if t.result == "OPEN"]
    closed = len(wins) + len(losses)

    print("\n" + "=" * 70)
    print(f"  BACKTEST - {p.symbol} - last {days} days")
    print("=" * 70)
    print(f"  Total signals   : {len(trades)}")
    print(f"  Wins            : {len(wins)}")
    print(f"  Losses          : {len(losses)}")
    if opens:
        print(f"  Still open      : {len(opens)}")
    if closed:
        wr = 100.0 * len(wins) / closed
        total_r = sum(t.r_mult for t in wins + losses)
        print(f"  Win rate        : {wr:.1f}%")
        print(f"  Net R           : {total_r:+.2f} R")
    print("=" * 70)

    # funnel: where setups stopped (helps tune when signals are few)
    if funnel:
        print("\n  Funnel (per 5-min check, where the setup stopped):")
        for label in sorted(funnel):
            print(f"    {label:34s}: {funnel[label]}")
        if rr_rejects:
            vals = list(rr_rejects.values())
            print(f"  Distinct setups rejected by RR : {len(rr_rejects)} "
                  f"(avg {sum(vals) / len(vals):.2f}, best {max(vals):.2f})")
        if waiting:
            print(f"  Distinct valid setups awaiting FVG tap : {len(waiting)}")

    if not trades:
        print("\n  No signals. The funnel above shows the wall. Typical fixes:")
        print("    - stage 3 biggest -> lower --disp-atr-mult (try 1.0)")
        print("    - stage 7 biggest -> lower --rr-min (try 1.2)")
        print("    - stage 2 biggest -> raise --mss-window (try 24) or --sweep-lookback (try 40)")
        return

    print("\n  Trade details:")
    print("  " + "-" * 66)
    for n_, t in enumerate(trades, 1):
        side = "BUY " if t.side == "bull" else "SELL"
        tag = {"WIN": "WIN ", "LOSS": "LOSS", "OPEN": "OPEN"}[t.result]
        print(f"  #{n_:<3} {side} [{tag}]  RR=1:{t.rr:.2f}  R={t.r_mult:+.2f}")
        print(f"       entry: {_ts(t.t_entry)}   @ {fmt(t.entry)}")
        print(f"       SL   : {fmt(t.sl)}    TP: {fmt(t.tp)}")
        if t.result != "OPEN":
            print(f"       exit : {_ts(t.t_exit)}   @ {fmt(t.exit_price)}")
        print(f"       ctx  : {t.context}")
        print("  " + "-" * 66)


# ============================================================================
# 9) Live mode - every 5 minutes, 24h (no session filter)
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


def live_loop(feed: MT5Feed, p: Params):
    print("\n" + "=" * 70)
    print(f"  LIVE - {p.symbol} - refresh every {TF_SECONDS['M5'] // 60} minutes (24h, no session filter)")
    print("=" * 70)
    sent = load_sent()

    while True:
        # wait for the next M5 candle to close + a small margin for the broker to update
        now = time.time()
        nxt = (int(now) // 300 + 1) * 300 + 8
        time.sleep(max(1, nxt - now))

        try:
            h1 = feed.latest("H1", 600)
            m15 = feed.latest("M15", 1500)
            m5 = feed.latest("M5", p.win_m5 + 50)
        except Exception as e:                   # noqa: BLE001
            print(f"[{_ts(int(time.time()))}] Data fetch error: {e}")
            continue

        dec = evaluate(h1, m15, m5, p)
        stamp = _ts(m5[-1].close_time("M5")) if m5 else _ts(int(time.time()))

        if dec.signal:
            if dec.key in sent:
                print(f"[{stamp}] Duplicate signal (already sent) - skipping.")
                continue
            text = signal_text(p.symbol, dec)
            print(f"\n[{stamp}] === SIGNAL ===\n{text}\n")
            telegram_send(text)
            sent.add(dec.key)
            save_sent(sent)
        else:
            print(f"[{stamp}] NO TRADE - {dec.reason}"
                  + (f"  | {dec.context}" if dec.context else ""))


# ============================================================================
# 10) Self-test - exercises the engine and backtest on synthetic data (no MT5)
# ============================================================================
def _gen_synthetic(n=26000, seed=7, start_price=2000.0):
    """Simple random walk (gold-like prices) to prove the code runs without crashing."""
    import random
    rnd = random.Random(seed)
    t0 = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
    price = start_price
    out = []
    for i in range(n):
        drift = math.sin(i / 500.0) * 0.05
        o = price
        step = rnd.gauss(drift, 0.6)
        c = max(1.0, o + step)
        hi = max(o, c) + abs(rnd.gauss(0, 0.4))
        lo = min(o, c) - abs(rnd.gauss(0, 0.4))
        out.append(Candle(t0 + i * 300, round(o, 2), round(hi, 2), round(lo, 2), round(c, 2)))
        price = c
    return out


def _aggregate(m5, tf):
    """Aggregate M5 into a higher timeframe (M15/H1) by time bucket."""
    step = TF_SECONDS[tf]
    buckets = {}
    order = []
    for c in m5:
        b = (c.t // step) * step
        if b not in buckets:
            buckets[b] = [c.o, c.h, c.l, c.c]
            order.append(b)
        else:
            g = buckets[b]
            g[1] = max(g[1], c.h)
            g[2] = min(g[2], c.l)
            g[3] = c.c
    return [Candle(b, buckets[b][0], buckets[b][1], buckets[b][2], buckets[b][3]) for b in order]


def selftest(p: Params):
    global PRICE_DIGITS
    PRICE_DIGITS = 2
    print("[selftest] Generating synthetic data (random walk) and running the backtest...")
    m5 = _gen_synthetic()
    m15 = _aggregate(m5, "M15")
    h1 = _aggregate(m5, "H1")
    print(f"[selftest] M5={len(m5)}  M15={len(m15)}  H1={len(h1)} candles")
    run_backtest(h1, m15, m5, p, days=90)
    print("\n[selftest] Engine ran without errors. (Results are on synthetic data, for testing only.)")


# ============================================================================
# 11) main
# ============================================================================
def build_params(args) -> Params:
    p = Params()
    for f_ in ("symbol", "sweep_lookback", "mss_window", "atr_period",
               "disp_atr_mult", "disp_body_ratio", "rr_min",
               "sw_h1", "sw_m15", "sw_m5"):
        v = getattr(args, f_, None)
        if v is not None:
            setattr(p, f_, v)
    return p


def main():
    ap = argparse.ArgumentParser(description="ICT/SMC Intraday Bot (MT5 + Telegram) - single file.")
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--backtest-only", action="store_true", help="backtest only then exit")
    ap.add_argument("--live-only", action="store_true", help="live only, no backtest")
    ap.add_argument("--selftest", action="store_true", help="test on synthetic data without MT5")
    ap.add_argument("--days", type=int, default=90, help="backtest days (default 90)")
    # strategy tuning
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

    if args.selftest:
        selftest(p)
        return

    feed = MT5Feed(p)
    feed.connect()
    try:
        if not args.live_only:
            # (1) one backtest on start
            now = datetime.now(timezone.utc)
            dt_from = now - timedelta(days=args.days + 15)   # +15 warmup
            print(f"[MT5] Fetching {args.days}+15 days of history...")
            h1 = feed.range("H1", dt_from, now)
            m15 = feed.range("M15", dt_from, now)
            m5 = feed.range("M5", dt_from, now)
            print(f"[MT5] H1={len(h1)}  M15={len(m15)}  M5={len(m5)} candles")
            run_backtest(h1, m15, m5, p, days=args.days)

        if not args.backtest_only:
            # (2) live every 5 minutes
            live_loop(feed, p)
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
