#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
ICT / SMC Intraday Bot  --  ملف واحد
================================================================================
الاستراتيجية (كيما اتفقنا):

    H1  → السياق: نصنّف الصفقة (مع الاتجاه / انعكاس أو تصحيح). ما يمنعش الصفقة.
    M15 → Liquidity + Sweep  (كسر قمة/قاع + رجوع = فخ سيولة).
    M5  → MSS + Displacement + FVG.
    ثم Retracement للـ FVG = الدخول.

    Sweep → MSS → Displacement → FVG → Retracement → RR مناسب → إشارة.

قواعد ذهبية:
    - كسر القاع وحده ≠ SELL   /   كسر القمة وحده ≠ BUY.
    - لازم Sweep + MSS.
    - لازم Displacement + FVG.
    - SL تحت/فوق أدنى/أعلى نقطة عملها الـ Sweep.
    - TP عند السيولة المقابلة.
    - RR >= 1.5 و إلا NO TRADE.

--------------------------------------------------------------------------------
طريقة التشغيل (كيما طلبت):
    1) أول ما يشتغل  → يعمل Backtest على 90 يوم مرة وحدة، يطبع كل الإشارات
       مصنّفة (رابحة / خاسرة) بتفاصيلها + ملخّص.
    2) بعدها يدخل في وضع مباشر: كل 5 دقايق يجيب بيانات جديدة من MT5 و يعطي:
         - كان فما صفقة  → Entry / SL / TP / RR  و يبعث إشعار تيليقرام.
         - كان NO TRADE  → يطبع علاش (أي مرحلة وقفت).

--------------------------------------------------------------------------------
الأوامر:
    python3 ict_smc_bot.py                 # backtest 90 يوم ثم مباشر كل 5 د
    python3 ict_smc_bot.py --backtest-only # backtest برك
    python3 ict_smc_bot.py --live-only     # مباشر برك (بلا backtest)
    python3 ict_smc_bot.py --selftest      # تجربة المحرّك ببيانات مولّدة (بلا MT5)
    python3 ict_smc_bot.py --symbol EURUSD --rr-min 2.0 ...

المتطلبات للتشغيل الحقيقي:  MetaTrader5 مثبّت (Windows) + الحزمة:
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
# 1) الإعدادات  --  التوكن و الـ Chat ID متاع تيليقرام: حطهم هنا
# ============================================================================
# طريقة الحصول عليهم:
#   1. في تيليقرام كلّم @BotFather  → /newbot  → خذ الـ TOKEN
#   2. ابعث أي رسالة للبوت متاعك، ثم افتح في المتصفح:
#        https://api.telegram.org/bot<TOKEN>/getUpdates
#      و خذ الرقم  "chat":{"id": ... }
#
# تنجم تخليهم هنا، ولا تحطهم كمتغيرات بيئة  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "PUT_YOUR_CHAT_ID_HERE")

# إعدادات MT5 (اختيارية: إذا حطيتهم الكود يعمل login بروحه، و إلا يستعمل
# التيرمينال المفتوح عندك)
MT5_LOGIN         = os.environ.get("MT5_LOGIN")          # مثال "51234567"
MT5_PASSWORD      = os.environ.get("MT5_PASSWORD")
MT5_SERVER        = os.environ.get("MT5_SERVER")         # مثال "ICMarkets-Demo"
MT5_TERMINAL_PATH = os.environ.get("MT5_TERMINAL_PATH")  # مسار terminal64.exe

_PLACEHOLDERS = ("PUT_YOUR_BOT_TOKEN_HERE", "PUT_YOUR_CHAT_ID_HERE", "", None)


def telegram_configured() -> bool:
    return TELEGRAM_BOT_TOKEN not in _PLACEHOLDERS and TELEGRAM_CHAT_ID not in _PLACEHOLDERS


# ============================================================================
# 2) نموذج الشمعة + الفريمات
# ============================================================================
TF_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "H1": 3600}


@dataclass
class Candle:
    t: int      # وقت الفتح (epoch ثواني، UTC)
    o: float
    h: float
    l: float
    c: float

    def close_time(self, tf: str) -> int:
        return self.t + TF_SECONDS[tf]


@dataclass
class Params:
    """كل الأرقام القابلة للتعديل. تنجم تبدّلهم من سطر الأوامر."""
    symbol: str = "EURUSD"

    # اكتشاف القمم/القيعان (نافذة fractal لكل فريم)
    sw_h1: int = 4
    sw_m15: int = 3
    sw_m5: int = 2

    equal_tol: float = 0.0007     # تفاوت "القمم المتساوية" (نسبة) = مجمع سيولة

    sweep_lookback: int = 24      # نبحث على Sweep في آخر كم شمعة M15
    mss_window: int = 12          # الـ MSS لازم يصير خلال كم شمعة M5 بعد الـ Sweep

    atr_period: int = 14
    disp_atr_mult: float = 1.5    # شمعة الاندفاع: جسمها >= 1.5 × ATR
    disp_body_ratio: float = 0.5  # و جسمها >= 50% من مداها

    rr_min: float = 1.5           # أقل Risk:Reward مقبول

    # نوافذ القرار في الوضع المباشر و الباكتيست (عدد شمعات لكل فريم)
    win_m5: int = 300
    win_m15: int = 220
    win_h1: int = 200


# ============================================================================
# 3) مؤشرات و أدوات هيكلية (بايثون صافي، بلا مكتبات)
# ============================================================================
def atr_series(candles, period):
    """ATR (متوسط المدى الحقيقي) — قائمة بنفس طول الشمعات (None قبل ما يكمل period)."""
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
    """قمم/قيعان مؤكّدة (fractal): الشمعة قمة/قاع كان كانت الأعلى/الأدنى
    وسط نافذة w على كل جيهة. آخر w شمعات ما تتأكدش (باش ما فماش نظر للمستقبل)."""
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
    """اتجاه الهيكل من آخر قمتين/قاعين."""
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
# 4) مراحل الاستراتيجية
# ============================================================================
def find_recent_sweep(m15, p: Params):
    """آخر Liquidity Sweep على M15:
       - Bull sweep : السعر كسر قاع (swing low) و رجع أغلق فوقه  → نبحث BUY.
       - Bear sweep : السعر كسر قمة (swing high) و رجع أغلق تحتها → نبحث SELL.
       نرجّعو الأحدث."""
    highs, lows = swings(m15, p.sw_m15)
    n = len(m15)
    floor_idx = max(0, n - p.sweep_lookback)
    best = None

    for (si, level) in lows:                       # sell-side liquidity → bull sweep
        for j in range(si + 1, n):
            c = m15[j]
            if c.l < level and c.c > level:        # كسر بالفتيل + إغلاق فوق
                if j >= floor_idx and (best is None or j > best["idx"]):
                    best = {"side": "bull", "level": level, "idx": j,
                            "extreme": c.l, "t": c.t}
                break

    for (si, level) in highs:                      # buy-side liquidity → bear sweep
        for j in range(si + 1, n):
            c = m15[j]
            if c.h > level and c.c < level:
                if j >= floor_idx and (best is None or j > best["idx"]):
                    best = {"side": "bear", "level": level, "idx": j,
                            "extreme": c.h, "t": c.t}
                break

    return best


def detect_mss(m5, start_idx, side, p: Params):
    """MSS على M5 بعد الـ Sweep، بالإغلاق (أقوى من الفتيل)، داخل النافذة الزمنية.
       Bull: كسر آخر Lower High فوق.   Bear: كسر آخر Higher Low تحت."""
    n = len(m5)
    if start_idx >= n:
        return None

    if side == "bull":
        rl_idx = min(range(start_idx, n), key=lambda k: m5[k].l)   # قاع الارتداد
        highs, _ = swings(m5, p.sw_m5)
        for (hi, pr) in [(i, v) for (i, v) in highs if i > rl_idx]:
            for k in range(hi + 1, n):
                if k - start_idx > p.mss_window:
                    break
                if m5[k].c > pr:
                    return {"ref_idx": hi, "ref": pr, "break_idx": k, "ext_idx": rl_idx}
        return None
    else:
        rh_idx = max(range(start_idx, n), key=lambda k: m5[k].h)   # قمة الارتداد
        _, lows = swings(m5, p.sw_m5)
        for (li, pr) in [(i, v) for (i, v) in lows if i > rh_idx]:
            for k in range(li + 1, n):
                if k - start_idx > p.mss_window:
                    break
                if m5[k].c < pr:
                    return {"ref_idx": li, "ref": pr, "break_idx": k, "ext_idx": rh_idx}
        return None


def is_displacement(m5, k, atr_arr, side, p: Params):
    """اندفاع واضح: جسم الشمعة >= disp_atr_mult×ATR و >= disp_body_ratio من المدى،
       و في اتجاه الصفقة. نقبلو شمعة الكسر k ولا الي قبلها."""
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
    """FVG (فجوة القيمة العادلة) بين شمعتين تحوطو بشمعة الاندفاع، في مجال [lo..hi].
       Bull: c1.high < c3.low.   Bear: c1.low > c3.high.  نرجّعو الأحدث."""
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
    """TP = أقرب سيولة مقابلة (قمة فوق للـ BUY / قاع تحت للـ SELL) من M15 و H1."""
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
# 5) القرار: إشارة ولا NO TRADE + السبب
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
    """تصنيف H1: مع الاتجاه ولا انعكاس/تصحيح (تصنيف برك، ما يمنعش)."""
    highs, lows = swings(h1, p.sw_h1)
    bias = structure_bias(highs, lows)
    if bias == "up":
        trend = "H1 صاعد"
    elif bias == "down":
        trend = "H1 هابط"
    elif bias == "mixed":
        trend = "H1 عرضي/متذبذب"
    else:
        trend = "H1 غير واضح"
    if (side == "bull" and bias == "up") or (side == "bear" and bias == "down"):
        kind = "مع الاتجاه"
    elif bias in ("up", "down"):
        kind = "انعكاس/تصحيح"
    else:
        kind = "بلا سياق واضح"
    return f"{trend} ({kind})"


def evaluate(h1, m15, m5, p: Params) -> Decision:
    """المحرّك: يمشي مرحلة مرحلة و يوقف عند أول وحدة تفشل، مع السبب."""
    need = max(p.sw_m5 * 2 + 2, p.atr_period + 2)
    if len(m5) < need or len(m15) < (p.sw_m15 * 2 + 2) or len(h1) < (p.sw_h1 * 2 + 2):
        return Decision(False, "بيانات غير كافية بعد (ننتظر شمعات أكثر).")

    # (1) Sweep على M15
    sweep = find_recent_sweep(m15, p)
    if not sweep:
        return Decision(False, "لا يوجد Liquidity Sweep حديث على M15 — ما فماش فخ سيولة.")
    side = sweep["side"]
    ctx = h1_context(h1, side, p)
    dir_txt = "BUY" if side == "bull" else "SELL"

    # نحدّد بداية الارتداد على M5 (أول شمعة M5 بعد فتح شمعة الـ Sweep)
    m5_times = [c.t for c in m5]
    start_idx = bisect.bisect_left(m5_times, sweep["t"])
    if start_idx >= len(m5):
        return Decision(False, f"[{dir_txt}] صار Sweep على M15 أما مازال ما توفّرتش شمعات M5 بعده.", ctx)

    # (2) MSS على M5
    mss = detect_mss(m5, start_idx, side, p)
    if not mss:
        return Decision(False,
                        f"[{dir_txt}] فما Sweep أما ما صارش MSS على M5 داخل النافذة ({p.mss_window} شمعة).",
                        ctx)

    # (3) Displacement
    atr_arr = atr_series(m5, p.atr_period)
    if not is_displacement(m5, mss["break_idx"], atr_arr, side, p):
        return Decision(False,
                        f"[{dir_txt}] صار MSS أما بلا Displacement قوي (الاندفاع ضعيف).",
                        ctx)

    # (4) FVG
    fvg = find_fvg(m5, mss["ref_idx"], mss["break_idx"] + 1, side)
    if not fvg:
        return Decision(False,
                        f"[{dir_txt}] Displacement موجود أما ما خلّاش FVG (فجوة سعرية).",
                        ctx)

    # نقطة الـ Sweep المتطرّفة (لتحديد SL) + الدخول (حافة الـ FVG القريبة)
    seg = m5[start_idx:mss["break_idx"] + 1]
    a = atr_arr[mss["break_idx"]] or (m5[-1].h - m5[-1].l)
    buf = 0.1 * a
    if side == "bull":
        react = min(x.l for x in seg)
        sl = react - buf
        entry = fvg["top"]                     # حافة الـ FVG العليا (proximal من فوق)
    else:
        react = max(x.h for x in seg)
        sl = react + buf
        entry = fvg["bottom"]                  # حافة الـ FVG السفلى (proximal من تحت)

    # (5) إبطال: كان بعد الـ MSS السعر أغلق ورا نقطة الـ Sweep قبل الرجوع للـ FVG
    for k in range(mss["break_idx"] + 1, len(m5)):
        if side == "bull" and m5[k].c < react:
            return Decision(False, f"[{dir_txt}] الفكرة أُبطلت: السعر أغلق تحت قاع الـ Sweep.", ctx)
        if side == "bear" and m5[k].c > react:
            return Decision(False, f"[{dir_txt}] الفكرة أُبطلت: السعر أغلق فوق قمة الـ Sweep.", ctx)

    # (6) TP = السيولة المقابلة  +  فلتر RR
    tp = target_liquidity(h1, m15, entry, side, p)
    if tp is None:
        return Decision(False, f"[{dir_txt}] ما فماش سيولة مقابلة واضحة كهدف (TP).", ctx)

    risk = abs(entry - sl)
    reward = abs(tp - entry)
    if risk <= 0:
        return Decision(False, f"[{dir_txt}] مسافة الوقف صفر — تجاهل.", ctx)
    rr = reward / risk
    key = (sweep["idx"], mss["break_idx"], fvg["idx"])
    base_info = {"sweep": sweep, "mss": mss, "fvg": fvg, "react": react}

    if rr < p.rr_min:
        return Decision(False,
                        f"[{dir_txt}] RR ضعيف {rr:.2f} < {p.rr_min:.2f} (الهدف قريب برشة) → NO TRADE.",
                        ctx, side, entry, sl, tp, rr, key, base_info)

    # (7) Retracement: لازم السعر يرجع يلمس الـ FVG توّا (أول لمسة)
    last = m5[-1]
    prev = m5[-2] if len(m5) >= 2 else last
    if side == "bull":
        tagged_now = last.l <= fvg["top"] and prev.l > fvg["top"]
    else:
        tagged_now = last.h >= fvg["bottom"] and prev.h < fvg["bottom"]

    if not tagged_now:
        return Decision(False,
                        f"[{dir_txt}] الإعداد جاهز و RR={rr:.2f} أما السعر مازال ما رجعش للـ FVG "
                        f"(ننتظر Retracement).",
                        ctx, side, entry, sl, tp, rr, key, base_info)

    # إشارة كاملة
    return Decision(True,
                    f"إشارة {dir_txt} — Sweep→MSS→Displacement→FVG→Retracement ✔",
                    ctx, side, entry, sl, tp, rr, key, base_info)


# ============================================================================
# 6) تنسيق النص + تيليقرام
# ============================================================================
def fmt(x, d=5):
    return f"{x:.{d}f}"


def signal_text(sym, dec: Decision) -> str:
    arrow = "🟢 BUY" if dec.side == "bull" else "🔴 SELL"
    return (
        f"{arrow}  {sym}\n"
        f"— — — — — — — —\n"
        f"Entry : {fmt(dec.entry)}\n"
        f"SL    : {fmt(dec.sl)}\n"
        f"TP    : {fmt(dec.tp)}\n"
        f"RR    : 1 : {dec.rr:.2f}\n"
        f"Context: {dec.context}\n"
        f"Setup : {dec.reason}"
    )


def telegram_send(text: str) -> bool:
    if not telegram_configured():
        print("[تيليقرام] غير مهيّأ (حط التوكن و الـ Chat ID) — تخطّي الإرسال.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as r:
            ok = json.loads(r.read().decode()).get("ok", False)
            if not ok:
                print("[تيليقرام] الرد ما كانش ok.")
            return ok
    except Exception as e:                      # noqa: BLE001
        print(f"[تيليقرام] فشل الإرسال: {e}")
        return False


# ============================================================================
# 7) MT5 — جلب البيانات
# ============================================================================
class MT5Feed:
    def __init__(self, params: Params):
        try:
            import MetaTrader5 as mt5           # noqa: N813
        except Exception as e:                  # noqa: BLE001
            raise RuntimeError(
                "حزمة MetaTrader5 غير متوفّرة. ثبّتها على Windows:  pip install MetaTrader5"
            ) from e
        self.mt5 = mt5
        self.p = params
        self.tf_map = {
            "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15, "H1": mt5.TIMEFRAME_H1,
        }

    def connect(self):
        kwargs = {}
        if MT5_TERMINAL_PATH:
            kwargs["path"] = MT5_TERMINAL_PATH
        if MT5_LOGIN and MT5_PASSWORD and MT5_SERVER:
            kwargs.update(login=int(MT5_LOGIN), password=MT5_PASSWORD, server=MT5_SERVER)
        if not self.mt5.initialize(**kwargs):
            raise RuntimeError(f"فشل تهيئة MT5: {self.mt5.last_error()}")
        if not self.mt5.symbol_select(self.p.symbol, True):
            raise RuntimeError(f"الرمز {self.p.symbol} غير متوفّر في MT5.")
        print(f"[MT5] متّصل. الرمز: {self.p.symbol}")

    def shutdown(self):
        try:
            self.mt5.shutdown()
        except Exception:                       # noqa: BLE001
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
        # نحيّدو الشمعة الأخيرة إذا مازالت ما سكّرتش
        now = time.time()
        while c and c[-1].close_time(tf) > now:
            c.pop()
        return c

    def range(self, tf: str, dt_from: datetime, dt_to: datetime):
        rates = self.mt5.copy_rates_range(self.p.symbol, self.tf_map[tf], dt_from, dt_to)
        return self._to_candles(rates)


# ============================================================================
# 8) الباكتيست — 90 يوم، تصنيف رابحة/خاسرة، طباعة كل صفقة
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
    """آخر win شمعة مسكّرة قبل upto_close."""
    # arr_times = أوقات الفتح؛ الشمعة مسكّرة كان t + tf <= upto_close
    j = bisect.bisect_right(arr_times, upto_close - TF_SECONDS[tf])
    return arr[max(0, j - win):j]


def run_backtest(h1, m15, m5, p: Params, days: int):
    """يمشي على شمعات M5 (سبب القرار كل 5 د)، يقيّم بنفس المحرّك بلا نظر للمستقبل،
       ثم يحاكي كل إشارة للأمام باش يعرف WIN/LOSS."""
    if not m5:
        print("لا توجد بيانات M5 للباكتيست.")
        return

    cutoff = m5[-1].close_time("M5") - days * 86400
    start_i = bisect.bisect_left([c.t for c in m5], cutoff)
    start_i = max(start_i, p.win_m5)

    m15_t = [c.t for c in m15]
    h1_t = [c.t for c in h1]

    trades = []
    seen_keys = set()
    open_trade = None
    i = start_i
    n = len(m5)

    while i < n:
        bar = m5[i]

        # كان عندنا صفقة مفتوحة نتابعها لين تسكّر (صفقة وحدة في نفس الوقت)
        if open_trade is not None:
            hit = _check_exit(open_trade, bar)
            if hit:
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
        if dec.signal and dec.key not in seen_keys:
            seen_keys.add(dec.key)
            open_trade = Trade(dec.side, dec.context, dec.entry, dec.sl, dec.tp,
                               dec.rr, bar.t)
            # المحاكاة تبدا من الشمعة الجاية
        i += 1

    if open_trade is not None:
        trades.append(open_trade)   # مازالت مفتوحة في آخر البيانات

    _print_backtest_report(p, days, trades)


def _check_exit(tr: Trade, bar: Candle):
    """هل هذي الشمعة سكّرت الصفقة؟ (نفترض الوقف يضرب قبل الهدف عند التعارض = تحفّظ)."""
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


def _print_backtest_report(p: Params, days: int, trades):
    wins = [t for t in trades if t.result == "WIN"]
    losses = [t for t in trades if t.result == "LOSS"]
    opens = [t for t in trades if t.result == "OPEN"]
    closed = len(wins) + len(losses)

    print("\n" + "=" * 70)
    print(f"  BACKTEST — {p.symbol} — آخر {days} يوم")
    print("=" * 70)
    print(f"  إجمالي الإشارات      : {len(trades)}")
    print(f"  رابحة (WIN)          : {len(wins)}")
    print(f"  خاسرة (LOSS)         : {len(losses)}")
    if opens:
        print(f"  مازالت مفتوحة        : {len(opens)}")
    if closed:
        wr = 100.0 * len(wins) / closed
        total_r = sum(t.r_mult for t in wins + losses)
        print(f"  نسبة الربح (Win rate): {wr:.1f}%")
        print(f"  صافي R               : {total_r:+.2f} R")
    print("=" * 70)

    if not trades:
        print("  ما طلعت حتى إشارة على هالفترة بهالإعدادات. جرّب تخفّض --rr-min "
              "ولا --disp-atr-mult ولا تزيد --sweep-lookback.")
        return

    print("\n  تفاصيل كل صفقة:")
    print("  " + "-" * 66)
    for n_, t in enumerate(trades, 1):
        side = "BUY " if t.side == "bull" else "SELL"
        tag = {"WIN": "✅ WIN ", "LOSS": "❌ LOSS", "OPEN": "⏳ OPEN"}[t.result]
        print(f"  #{n_:<3} {side} {tag}  RR=1:{t.rr:.2f}  R={t.r_mult:+.2f}")
        print(f"       دخول : {_ts(t.t_entry)}   @ {fmt(t.entry)}")
        print(f"       SL   : {fmt(t.sl)}    TP: {fmt(t.tp)}")
        if t.result != "OPEN":
            print(f"       خروج : {_ts(t.t_exit)}   @ {fmt(t.exit_price)}")
        print(f"       سياق : {t.context}")
        print("  " + "-" * 66)


# ============================================================================
# 9) الوضع المباشر — كل 5 دقايق
# ============================================================================
def load_sent(path="sent_signals.json"):
    try:
        with open(path) as f:
            return set(tuple(k) for k in json.load(f))
    except Exception:                           # noqa: BLE001
        return set()


def save_sent(keys, path="sent_signals.json"):
    try:
        with open(path, "w") as f:
            json.dump([list(k) for k in keys], f)
    except Exception:                           # noqa: BLE001
        pass


def live_loop(feed: MT5Feed, p: Params):
    print("\n" + "=" * 70)
    print(f"  الوضع المباشر — {p.symbol} — تحديث كل {TF_SECONDS['M5'] // 60} دقايق")
    print("=" * 70)
    sent = load_sent()

    while True:
        # نستنّى إغلاق شمعة M5 الجاية + هامش صغير باش البروكر يحدّث
        now = time.time()
        nxt = (int(now) // 300 + 1) * 300 + 8
        time.sleep(max(1, nxt - now))

        try:
            h1 = feed.latest("H1", 600)
            m15 = feed.latest("M15", 1500)
            m5 = feed.latest("M5", p.win_m5 + 50)
        except Exception as e:                  # noqa: BLE001
            print(f"[{_ts(int(time.time()))}] خطأ في جلب البيانات: {e}")
            continue

        dec = evaluate(h1, m15, m5, p)
        stamp = _ts(m5[-1].close_time("M5")) if m5 else _ts(int(time.time()))

        if dec.signal:
            if dec.key in sent:
                print(f"[{stamp}] إشارة مكرّرة (تبعثت قبل) — تخطّي.")
                continue
            text = signal_text(p.symbol, dec)
            print(f"\n[{stamp}] === إشارة ===\n{text}\n")
            telegram_send(text)
            sent.add(dec.key)
            save_sent(sent)
        else:
            print(f"[{stamp}] NO TRADE — {dec.reason}"
                  + (f"  | {dec.context}" if dec.context else ""))


# ============================================================================
# 10) Self-test — يجرّب المحرّك و الباكتيست ببيانات مولّدة (بلا MT5)
# ============================================================================
def _gen_synthetic_m5(n=26000, seed=7, start_price=1.10000):
    """random-walk بسيط باش نتأكدو أن الكود يشتغل بلا crash."""
    import random
    rnd = random.Random(seed)
    t0 = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
    price = start_price
    out = []
    for i in range(n):
        drift = math.sin(i / 500.0) * 0.00005
        o = price
        step = rnd.gauss(drift, 0.00035)
        c = max(0.5, o + step)
        hi = max(o, c) + abs(rnd.gauss(0, 0.00025))
        lo = min(o, c) - abs(rnd.gauss(0, 0.00025))
        out.append(Candle(t0 + i * 300, round(o, 5), round(hi, 5), round(lo, 5), round(c, 5)))
        price = c
    return out


def _aggregate(m5, tf):
    """نجمّع M5 لفريم أكبر (M15/H1) حسب الوقت."""
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
    print("[selftest] توليد بيانات وهمية (random-walk) و تشغيل الباكتيست…")
    m5 = _gen_synthetic_m5()
    m15 = _aggregate(m5, "M15")
    h1 = _aggregate(m5, "H1")
    print(f"[selftest] M5={len(m5)}  M15={len(m15)}  H1={len(h1)} شمعة")
    run_backtest(h1, m15, m5, p, days=90)
    print("\n[selftest] المحرّك اشتغل بلا أخطاء ✔ (النتائج على بيانات وهمية، للتجربة برك).")


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
    ap = argparse.ArgumentParser(description="ICT/SMC Intraday Bot (MT5 + Telegram) — ملف واحد.")
    ap.add_argument("--symbol", default="EURUSD")
    ap.add_argument("--backtest-only", action="store_true", help="backtest برك ثم يخرج")
    ap.add_argument("--live-only", action="store_true", help="مباشر برك بلا backtest")
    ap.add_argument("--selftest", action="store_true", help="تجربة ببيانات وهمية بلا MT5")
    ap.add_argument("--days", type=int, default=90, help="عدد أيام الباكتيست (افتراضي 90)")
    # ضبط الاستراتيجية
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
            # (1) Backtest مرة وحدة عند التشغيل
            now = datetime.now(timezone.utc)
            dt_from = now - timedelta(days=args.days + 15)   # +15 تسخين
            print(f"[MT5] جلب تاريخ {args.days}+15 يوم…")
            h1 = feed.range("H1", dt_from, now)
            m15 = feed.range("M15", dt_from, now)
            m5 = feed.range("M5", dt_from, now)
            print(f"[MT5] H1={len(h1)}  M15={len(m15)}  M5={len(m5)} شمعة")
            run_backtest(h1, m15, m5, p, days=args.days)

        if not args.backtest_only:
            # (2) مباشر كل 5 دقايق
            live_loop(feed, p)
    finally:
        feed.shutdown()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nتوقّف بطلب المستخدم.")
    except RuntimeError as e:
        print(f"خطأ: {e}", file=sys.stderr)
        sys.exit(1)
