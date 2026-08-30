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

Run (one command does everything: 90-day backtest once, then live every 5 min):
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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# ============================================================================
# Settings -- put your Telegram bot token and chat id here (or as env vars)
# ============================================================================
# @BotFather -> /newbot for the TOKEN. Send your bot a message then open
# https://api.telegram.org/bot<TOKEN>/getUpdates to read your chat id.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "PUT_YOUR_CHAT_ID_HERE")

# Optional MT5 login (else it uses the terminal you already have open).
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
    t: int      # open time (epoch seconds, UTC)
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
    sw_m15: int = 3
    sw_m5: int = 2

    sweep_lookback: int = 30      # look for a sweep in the last N M15 candles
    mss_window: int = 18          # MSS must occur within N M5 candles after the sweep

    atr_period: int = 14
    disp_atr_mult: float = 1.3    # displacement candle: body >= mult x ATR
    disp_body_ratio: float = 0.5  # and body >= ratio of its range

    rr_min: float = 1.5           # minimum Risk:Reward

    win_m5: int = 300             # decision windows (candles per timeframe)
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
    """Confirmed fractal swing highs/lows. The last w bars are not confirmed
    (no look-ahead)."""
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
# Strategy stages
# ============================================================================
def find_recent_sweep(m15, p: Params):
    """Most recent M15 liquidity sweep. Bull: break a swing low, close back
    above. Bear: break a swing high, close back below."""
    highs, lows = swings(m15, p.sw_m15)
    n = len(m15)
    floor_idx = max(0, n - p.sweep_lookback)
    best = None
    for (si, level) in lows:
        for j in range(si + 1, n):
            c = m15[j]
            if c.l < level and c.c > level:
                if j >= floor_idx and (best is None or j > best["idx"]):
                    best = {"side": "bull", "level": level, "idx": j, "extreme": c.l, "t": c.t}
                break
    for (si, level) in highs:
        for j in range(si + 1, n):
            c = m15[j]
            if c.h > level and c.c < level:
                if j >= floor_idx and (best is None or j > best["idx"]):
                    best = {"side": "bear", "level": level, "idx": j, "extreme": c.h, "t": c.t}
                break
    return best


def detect_mss(m5, start_idx, side, p: Params):
    """MSS on M5 after the sweep, by close, within the validity window."""
    n = len(m5)
    if start_idx >= n:
        return None
    if side == "bull":
        rl_idx = min(range(start_idx, n), key=lambda k: m5[k].l)
        highs, _ = swings(m5, p.sw_m5)
        for (hi, pr) in [(i, v) for (i, v) in highs if i > rl_idx]:
            for k in range(hi + 1, n):
                if k - start_idx > p.mss_window:
                    break
                if m5[k].c > pr:
                    return {"ref_idx": hi, "break_idx": k, "ext_idx": rl_idx}
        return None
    else:
        rh_idx = max(range(start_idx, n), key=lambda k: m5[k].h)
        _, lows = swings(m5, p.sw_m5)
        for (li, pr) in [(i, v) for (i, v) in lows if i > rh_idx]:
            for k in range(li + 1, n):
                if k - start_idx > p.mss_window:
                    break
                if m5[k].c < pr:
                    return {"ref_idx": li, "break_idx": k, "ext_idx": rh_idx}
        return None


def is_displacement(m5, k, atr_arr, side, p: Params):
    """Break candle (or the one before) with body >= mult x ATR and >= ratio
    of range, in the trade direction."""
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
    """Latest FVG (3-candle imbalance) within [lo..hi]."""
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
    """Nearest opposing liquidity pool at least min_dist from entry (so RR is
    acceptable). None if no pool is far enough."""
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


# ============================================================================
# Decision
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


def evaluate(h1, m15, m5, p: Params) -> Decision:
    need = max(p.sw_m5 * 2 + 2, p.atr_period + 2)
    if len(m5) < need or len(m15) < (p.sw_m15 * 2 + 2) or len(h1) < (p.sw_h1 * 2 + 2):
        return Decision(False, "Not enough data yet.")

    sweep = find_recent_sweep(m15, p)
    if not sweep:
        return Decision(False, "No recent liquidity sweep on M15.")
    side = sweep["side"]
    ctx = h1_context(h1, side, p)
    dir_txt = "BUY" if side == "bull" else "SELL"

    start_idx = bisect.bisect_left([c.t for c in m5], sweep["t"])
    if start_idx >= len(m5):
        return Decision(False, f"[{dir_txt}] Sweep on M15 but no M5 candles after it yet.", ctx)

    mss = detect_mss(m5, start_idx, side, p)
    if not mss:
        return Decision(False, f"[{dir_txt}] Sweep found but no MSS on M5 within the window.", ctx)

    atr_arr = atr_series(m5, p.atr_period)
    if not is_displacement(m5, mss["break_idx"], atr_arr, side, p):
        return Decision(False, f"[{dir_txt}] MSS but no strong displacement.", ctx)

    fvg = find_fvg(m5, mss["ref_idx"], mss["break_idx"] + 1, side)
    if not fvg:
        return Decision(False, f"[{dir_txt}] Displacement but no FVG.", ctx)

    seg = m5[start_idx:mss["break_idx"] + 1]
    buf = 0.1 * (atr_arr[mss["break_idx"]] or (m5[-1].h - m5[-1].l))
    if side == "bull":
        react = min(x.l for x in seg)
        sl = react - buf
        entry = fvg["top"]
    else:
        react = max(x.h for x in seg)
        sl = react + buf
        entry = fvg["bottom"]

    for k in range(mss["break_idx"] + 1, len(m5)):
        if side == "bull" and m5[k].c < react:
            return Decision(False, f"[{dir_txt}] Invalidated: closed below the sweep low.", ctx)
        if side == "bear" and m5[k].c > react:
            return Decision(False, f"[{dir_txt}] Invalidated: closed above the sweep high.", ctx)

    risk = abs(entry - sl)
    if risk <= 0:
        return Decision(False, f"[{dir_txt}] Stop distance is zero - skip.", ctx)
    key = (sweep["idx"], mss["break_idx"], fvg["idx"])

    tp = target_liquidity(h1, m15, entry, side, p, p.rr_min * risk)
    if tp is None:
        return Decision(False,
                        f"[{dir_txt}] No opposing liquidity far enough for RR>={p.rr_min:.2f} "
                        f"(target too close).", ctx, side, entry, sl, 0.0, 0.0, key)
    rr = abs(tp - entry) / risk

    last = m5[-1]
    prev = m5[-2] if len(m5) >= 2 else last
    if side == "bull":
        tagged_now = last.l <= fvg["top"] and prev.l > fvg["top"]
    else:
        tagged_now = last.h >= fvg["bottom"] and prev.h < fvg["bottom"]
    if not tagged_now:
        return Decision(False,
                        f"[{dir_txt}] Setup ready (RR={rr:.2f}) but price has not returned to the "
                        f"FVG yet (waiting for retracement).", ctx, side, entry, sl, tp, rr, key)

    return Decision(True, f"{dir_txt} signal - Sweep->MSS->Displacement->FVG->Retracement OK",
                    ctx, side, entry, sl, tp, rr, key)


# ============================================================================
# Text + Telegram
# ============================================================================
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

    def latest(self, tf: str, count: int):
        c = self._to_candles(self.mt5.copy_rates_from_pos(self.p.symbol, self.tf_map[tf], 0, count))
        now = time.time()
        while c and c[-1].close_time(tf) > now:
            c.pop()
        return c

    def range(self, tf: str, dt_from: datetime, dt_to: datetime):
        return self._to_candles(self.mt5.copy_rates_range(self.p.symbol, self.tf_map[tf], dt_from, dt_to))


# ============================================================================
# Backtest: 90 days, WIN/LOSS, print every trade
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


def _slice_tail(arr_times, arr, upto_close, tf, win):
    j = bisect.bisect_right(arr_times, upto_close - TF_SECONDS[tf])
    return arr[max(0, j - win):j]


def run_backtest(h1, m15, m5, p: Params, days: int):
    if not m5:
        print("No M5 data for the backtest.")
        return
    cutoff = m5[-1].close_time("M5") - days * 86400
    start_i = max(bisect.bisect_left([c.t for c in m5], cutoff), p.win_m5)
    m15_t = [c.t for c in m15]
    h1_t = [c.t for c in h1]

    trades, seen_keys, open_trade = [], set(), None
    i, n = start_i, len(m5)
    while i < n:
        bar = m5[i]
        if open_trade is not None:
            if _check_exit(open_trade, bar):
                open_trade.t_exit = bar.t
                trades.append(open_trade)
                open_trade = None
            i += 1
            continue
        upto = bar.close_time("M5")
        dec = evaluate(_slice_tail(h1_t, h1, upto, "H1", p.win_h1),
                       _slice_tail(m15_t, m15, upto, "M15", p.win_m15),
                       m5[max(0, i - p.win_m5 + 1):i + 1], p)
        if dec.signal and dec.key not in seen_keys:
            seen_keys.add(dec.key)
            open_trade = Trade(dec.side, dec.context, dec.entry, dec.sl, dec.tp, dec.rr, bar.t)
        i += 1
    if open_trade is not None:
        trades.append(open_trade)
    _print_backtest_report(p, days, trades)


def _check_exit(tr: Trade, bar: Candle):
    """SL assumed to fill before TP on conflict (conservative)."""
    if tr.side == "bull":
        if bar.l <= tr.sl:
            tr.result, tr.exit_price = "LOSS", tr.sl
        elif bar.h >= tr.tp:
            tr.result, tr.exit_price = "WIN", tr.tp
    else:
        if bar.h >= tr.sl:
            tr.result, tr.exit_price = "LOSS", tr.sl
        elif bar.l <= tr.tp:
            tr.result, tr.exit_price = "WIN", tr.tp
    if tr.result in ("WIN", "LOSS"):
        risk = abs(tr.entry - tr.sl) or 1e-9
        tr.r_mult = (tr.exit_price - tr.entry) / risk if tr.side == "bull" else (tr.entry - tr.exit_price) / risk
        return True
    return False


def _ts(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _print_backtest_report(p: Params, days: int, trades):
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
        print(f"  Win rate        : {100.0 * len(wins) / closed:.1f}%")
        print(f"  Net R           : {sum(t.r_mult for t in wins + losses):+.2f} R")
    print("=" * 70)

    if not trades:
        print("  No signals in this period. Try --rr-min 1.2 or --disp-atr-mult 1.0.")
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


def live_loop(feed: MT5Feed, p: Params):
    print("\n" + "=" * 70)
    print(f"  LIVE - {p.symbol} - refresh every 5 minutes (24h, no session filter)")
    print("=" * 70)
    sent = load_sent()
    while True:
        now = time.time()
        time.sleep(max(1, (int(now) // 300 + 1) * 300 + 8 - now))
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
                print(f"[{stamp}] Duplicate signal - skipping.")
                continue
            text = signal_text(p.symbol, dec)
            print(f"\n[{stamp}] === SIGNAL ===\n{text}\n")
            telegram_send(text)
            sent.add(dec.key)
            save_sent(sent)
        else:
            print(f"[{stamp}] NO TRADE - {dec.reason}" + (f"  | {dec.context}" if dec.context else ""))


# ============================================================================
# Self-test (no MT5)
# ============================================================================
def _gen_synthetic(n=26000, seed=7, start_price=2000.0):
    import random
    rnd = random.Random(seed)
    t0 = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
    price = start_price
    out = []
    for i in range(n):
        o = price
        c = max(1.0, o + rnd.gauss(math.sin(i / 500.0) * 0.05, 0.6))
        hi = max(o, c) + abs(rnd.gauss(0, 0.4))
        lo = min(o, c) - abs(rnd.gauss(0, 0.4))
        out.append(Candle(t0 + i * 300, round(o, 2), round(hi, 2), round(lo, 2), round(c, 2)))
        price = c
    return out


def _aggregate(m5, tf):
    step = TF_SECONDS[tf]
    buckets, order = {}, []
    for c in m5:
        b = (c.t // step) * step
        if b not in buckets:
            buckets[b] = [c.o, c.h, c.l, c.c]
            order.append(b)
        else:
            g = buckets[b]
            g[1] = max(g[1], c.h); g[2] = min(g[2], c.l); g[3] = c.c
    return [Candle(b, *buckets[b]) for b in order]


def selftest(p: Params):
    print("[selftest] Synthetic data, running the backtest (no MT5)...")
    m5 = _gen_synthetic()
    run_backtest(_aggregate(m5, "H1"), _aggregate(m5, "M15"), m5, p, days=90)


# ============================================================================
# main
# ============================================================================
def build_params(args) -> Params:
    p = Params()
    for f_ in ("symbol", "sweep_lookback", "mss_window", "atr_period",
               "disp_atr_mult", "disp_body_ratio", "rr_min", "sw_h1", "sw_m15", "sw_m5"):
        v = getattr(args, f_, None)
        if v is not None:
            setattr(p, f_, v)
    return p


def main():
    ap = argparse.ArgumentParser(description="ICT/SMC Intraday Bot (MT5 + Telegram) - single file.")
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--backtest-only", action="store_true")
    ap.add_argument("--live-only", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--days", type=int, default=90)
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
            now = datetime.now(timezone.utc)
            print(f"[MT5] Fetching {args.days}+15 days of history...")
            h1 = feed.range("H1", now - timedelta(days=args.days + 15), now)
            m15 = feed.range("M15", now - timedelta(days=args.days + 15), now)
            m5 = feed.range("M5", now - timedelta(days=args.days + 15), now)
            print(f"[MT5] H1={len(h1)}  M15={len(m15)}  M5={len(m5)} candles")
            run_backtest(h1, m15, m5, p, days=args.days)
        if not args.backtest_only:
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
