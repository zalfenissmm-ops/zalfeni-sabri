#!/usr/bin/env python3
"""
Multi-Timeframe Trend Following Strategy — implements the agreed spec
(strategy_spec.md): 1H trend confirmation -> 15m pullback zone -> 5m entry
trigger, ATR-based ZigZag structure, structure+ATR stop loss, fixed 1:2
reward:risk take profit. Backtested bar-by-bar on the 5-minute clock with
no look-ahead, then optionally watched live with Telegram alerts.

Data sources (base timeframe is always M5; 15m/1H are built from it by
resampling):
    CSV       : python3 mtf_trend_strategy.py data.csv --days 30
    yfinance  : python3 mtf_trend_strategy.py --yfinance --symbol EURUSD=X --days 30
    MT5       : python3 mtf_trend_strategy.py --mt5 --symbol EURUSD --days 30 --live --interval 5

CSV must have 5-minute OHLC candles with columns: date,open,high,low,close
(date as an ISO timestamp, e.g. 2024-01-01 09:05:00)

Telegram alerts (optional, only used with --live): pass --telegram-token /
--telegram-chat-id, or set the TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
environment variables.
"""

import argparse
import csv
import os
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None

try:
    import yfinance as yf
except ImportError:
    yf = None


# ---------------------------------------------------------------- data ----

@dataclass
class Candle:
    dt: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass
class Trade:
    direction: str
    entry_dt: datetime
    entry_price: float
    stop_loss: float
    take_profit: float
    exit_dt: datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None

    def pnl_price(self) -> float:
        sign = 1 if self.direction == "LONG" else -1
        return sign * (self.exit_price - self.entry_price)


def _parse_dt(s: str) -> datetime:
    s = s.strip()
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    return datetime.fromisoformat(s)


def load_csv(path: str) -> list[Candle]:
    candles = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            candles.append(Candle(
                dt=_parse_dt(row["date"]), open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
            ))
    if len(candles) < 2:
        raise ValueError("Need at least 2 rows of 5-minute price data")
    candles.sort(key=lambda c: c.dt)
    return candles


def _copy_rates_robust(symbol: str, bars_needed: int, chunk: int = 5000, retries: int = 3):
    """Pull M5 bars via copy_rates_from_pos in chunks, walking further back
    in position until bars_needed is met or the terminal has no more to
    give. One huge copy_rates_range call for a long span often fails
    outright ("Terminal: Call failed") when that whole range isn't cached
    locally yet; small position-based chunks are what actually makes MT5
    fetch older history from the broker on demand, and a short retry
    absorbs the occasional transient failure."""
    collected = []
    pos = 0
    while len(collected) < bars_needed:
        take = min(chunk, bars_needed - len(collected))
        batch = None
        for attempt in range(retries):
            batch = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, pos, take)
            if batch is not None and len(batch) > 0:
                break
            time.sleep(0.5)
        if batch is None or len(batch) == 0:
            break
        collected = list(batch) + collected
        pos += len(batch)
        if len(batch) < take:
            break  # terminal had no older bars left to give
    return collected


def fetch_mt5(
    symbol: str, history_days: int, login: int | None = None, password: str | None = None,
    server: str | None = None, path: str | None = None,
) -> list[Candle]:
    if mt5 is None:
        raise RuntimeError("MetaTrader5 package not installed. Run: pip install MetaTrader5 (Windows only)")

    kwargs = {k: v for k, v in {"path": path, "login": login, "password": password, "server": server}.items() if v}
    if not mt5.initialize(**kwargs):
        code, desc = mt5.last_error()
        raise RuntimeError(f"MT5 initialize() failed: [{code}] {desc}")

    try:
        if not mt5.symbol_select(symbol, True):
            code, desc = mt5.last_error()
            raise ValueError(f"Symbol '{symbol}' not available: [{code}] {desc}")

        bars_needed = history_days * 288  # M5 bars/day if the market never closed; safe overestimate around weekends
        rates = _copy_rates_robust(symbol, bars_needed)
        if not rates:
            code, desc = mt5.last_error()
            raise ValueError(
                f"No M5 data returned for {symbol}: [{code}] {desc}. "
                "Open an M5 chart for this symbol in MT5 and scroll back (or press Home) "
                "to force the terminal to download older history, then retry."
            )

        return [
            Candle(
                dt=datetime.fromtimestamp(int(r["time"]), tz=timezone.utc).replace(tzinfo=None),
                open=float(r["open"]), high=float(r["high"]), low=float(r["low"]), close=float(r["close"]),
            )
            for r in rates
        ]
    finally:
        mt5.shutdown()


def fetch_yfinance(symbol: str, history_days: int) -> list[Candle]:
    if yf is None:
        raise RuntimeError("yfinance package not installed. Run: pip install yfinance")

    period_days = min(history_days, 59)  # Yahoo caps intraday 5m history at ~60 days
    df = yf.download(symbol, period=f"{period_days}d", interval="5m", progress=False)
    if df is None or df.empty:
        raise ValueError(f"No data returned for {symbol} from yfinance")
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)

    candles = []
    for idx, row in df.iterrows():
        dt = idx.to_pydatetime()
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        candles.append(Candle(dt=dt, open=float(row["Open"]), high=float(row["High"]), low=float(row["Low"]), close=float(row["Close"])))
    return candles


# ---------------------------------------------------------- resampling ----

def _bucket_start(dt: datetime, minutes: int) -> datetime:
    if minutes == 60:
        return dt.replace(minute=0, second=0, microsecond=0)
    total = dt.hour * 60 + dt.minute
    b = (total // minutes) * minutes
    return dt.replace(hour=b // 60, minute=b % 60, second=0, microsecond=0)


class Resampler:
    """Ingest 5m candles one at a time; only fully closed buckets ever land
    in `.closed` — the in-progress bucket is never exposed, so nothing that
    reads `.closed` can see a bar before it has actually finished (this is
    what keeps the backtest free of look-ahead bias)."""

    def __init__(self, minutes: int):
        self.minutes = minutes
        self.closed: list[Candle] = []
        self._bucket_dt = None
        self._o = self._h = self._l = self._c = None

    def push(self, candle: Candle) -> None:
        b = _bucket_start(candle.dt, self.minutes)
        if self._bucket_dt is None:
            self._bucket_dt, self._o, self._h, self._l, self._c = b, candle.open, candle.high, candle.low, candle.close
        elif b == self._bucket_dt:
            self._h = max(self._h, candle.high)
            self._l = min(self._l, candle.low)
            self._c = candle.close
        else:
            self.closed.append(Candle(self._bucket_dt, self._o, self._h, self._l, self._c))
            self._bucket_dt, self._o, self._h, self._l, self._c = b, candle.open, candle.high, candle.low, candle.close


def build_resamplers(candles_5m: list[Candle]) -> tuple[Resampler, Resampler]:
    r15, r1h = Resampler(15), Resampler(60)
    for c in candles_5m:
        r15.push(c)
        r1h.push(c)
    return r15, r1h


# ------------------------------------------------------------ indicators --

def compute_sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _tr_series(candles: list[Candle]) -> list[float]:
    return [
        max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
        for prev, cur in zip(candles, candles[1:])
    ]


def atr_series(candles: list[Candle], period: int = 14) -> list[float | None]:
    """Wilder ATR aligned to candle index (None until warmed up)."""
    trs = _tr_series(candles)
    result: list[float | None] = [None] * len(candles)
    if len(trs) < period:
        return result
    prev = sum(trs[:period]) / period
    result[period] = prev
    for k in range(period, len(trs)):
        prev = (prev * (period - 1) + trs[k]) / period
        result[k + 1] = prev
    return result


def compute_atr(candles: list[Candle], period: int = 14) -> float | None:
    for v in reversed(atr_series(candles, period)):
        if v is not None:
            return v
    return None


def _wilder_sum(values: list[float], period: int) -> list[float]:
    result = [sum(values[:period])]
    for v in values[period:]:
        result.append(result[-1] - result[-1] / period + v)
    return result


def _wilder_avg(values: list[float], period: int) -> list[float]:
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append((result[-1] * (period - 1) + v) / period)
    return result


def compute_adx_di(candles: list[Candle], period: int = 14) -> tuple[float | None, float | None, float | None]:
    if len(candles) < period * 2:
        return None, None, None

    plus_dm, minus_dm, tr = [], [], []
    for prev, cur in zip(candles, candles[1:]):
        up_move = cur.high - prev.high
        down_move = prev.low - cur.low
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        tr.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))

    smoothed_tr = _wilder_sum(tr, period)
    smoothed_plus = _wilder_sum(plus_dm, period)
    smoothed_minus = _wilder_sum(minus_dm, period)

    plus_dis, minus_dis, dx_values = [], [], []
    for t, pdm, mdm in zip(smoothed_tr, smoothed_plus, smoothed_minus):
        if t == 0:
            plus_dis.append(0.0); minus_dis.append(0.0); dx_values.append(0.0)
            continue
        pdi, mdi = 100 * pdm / t, 100 * mdm / t
        plus_dis.append(pdi); minus_dis.append(mdi)
        di_sum = pdi + mdi
        dx_values.append(0.0 if di_sum == 0 else 100 * abs(pdi - mdi) / di_sum)

    if len(dx_values) < period:
        return None, None, None
    return _wilder_avg(dx_values, period)[-1], plus_dis[-1], minus_dis[-1]


# ------------------------------------------------------- ZigZag (ATR) -----

@dataclass
class Pivot:
    index: int
    price: float
    kind: str  # "high" | "low"


def zigzag(candles: list[Candle], atr_period: int = 14, atr_mult: float = 1.5) -> list[Pivot]:
    """ATR-based ZigZag: while tracking a rising leg, the candidate top is
    re-anchored to every new higher high; only once price pulls back from
    that running high by >= atr_mult * ATR is the top confirmed as a pivot
    (mirror image for falling legs). The still-forming leg (the repainting
    part the spec warns about) is never appended, so every Pivot returned
    is final and won't change on later bars."""
    atrs = atr_series(candles, atr_period)
    start = next((i for i, a in enumerate(atrs) if a is not None), None)
    if start is None or start + 1 >= len(candles):
        return []

    pivots: list[Pivot] = []
    direction: str | None = None
    ext_idx, ext_price = start, candles[start].close
    hi_idx, hi_price = start, candles[start].high
    lo_idx, lo_price = start, candles[start].low

    for i in range(start + 1, len(candles)):
        atr = atrs[i]
        if atr is None:
            continue
        c = candles[i]
        threshold = atr_mult * atr

        if direction is None:
            if c.high > hi_price:
                hi_idx, hi_price = i, c.high
            if c.low < lo_price:
                lo_idx, lo_price = i, c.low
            if hi_price - lo_price >= threshold:
                if hi_idx < lo_idx:
                    pivots.append(Pivot(hi_idx, hi_price, "high"))
                    direction, ext_idx, ext_price = "down", lo_idx, lo_price
                else:
                    pivots.append(Pivot(lo_idx, lo_price, "low"))
                    direction, ext_idx, ext_price = "up", hi_idx, hi_price
        elif direction == "up":
            if c.high > ext_price:
                ext_idx, ext_price = i, c.high
            elif ext_price - c.low >= threshold:
                pivots.append(Pivot(ext_idx, ext_price, "high"))
                direction, ext_idx, ext_price = "down", i, c.low
        else:  # direction == "down"
            if c.low < ext_price:
                ext_idx, ext_price = i, c.low
            elif c.high - ext_price >= threshold:
                pivots.append(Pivot(ext_idx, ext_price, "low"))
                direction, ext_idx, ext_price = "up", i, c.high

    return pivots


def structure_bias_from_pivots(pivots: list[Pivot]) -> str:
    highs = [p.price for p in pivots if p.kind == "high"]
    lows = [p.price for p in pivots if p.kind == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return "unknown"
    higher_highs = highs[-1] > highs[-2]
    higher_lows = lows[-1] > lows[-2]
    if higher_highs and higher_lows:
        return "up"
    if not higher_highs and not higher_lows:
        return "down"
    return "mixed"


# ------------------------------------------------ Phase 1: trend (1H) -----

def evaluate_trend(
    candles: list[Candle], adx_period: int, adx_threshold: float, ma_fast: int, ma_slow: int,
    zigzag_atr_period: int, zigzag_atr_mult: float,
) -> dict:
    need = max(ma_slow, adx_period * 2, zigzag_atr_period + 2)
    if len(candles) < need:
        return {"direction": "none", "reason": f"need {need} candles, have {len(candles)}"}

    closes = [c.close for c in candles]
    ma_f = compute_sma(closes, ma_fast)
    ma_s = compute_sma(closes, ma_slow)
    pivots = zigzag(candles, zigzag_atr_period, zigzag_atr_mult)
    structure = structure_bias_from_pivots(pivots)
    adx_v, plus_di, minus_di = compute_adx_di(candles, adx_period)

    if adx_v is None or ma_f is None or ma_s is None:
        return {"direction": "none", "reason": "indicators not warmed up yet", "structure": structure}

    up_ma = closes[-1] > ma_f > ma_s
    down_ma = closes[-1] < ma_f < ma_s
    adx_ok = adx_v >= adx_threshold
    up_di = plus_di > minus_di
    down_di = minus_di > plus_di

    direction = "none"
    if structure == "up" and up_ma and adx_ok and up_di:
        direction = "up"
    elif structure == "down" and down_ma and adx_ok and down_di:
        direction = "down"

    return {
        "direction": direction, "structure": structure, "ma_fast": ma_f, "ma_slow": ma_s,
        "adx": adx_v, "plus_di": plus_di, "minus_di": minus_di, "adx_ok": adx_ok, "pivots": pivots,
    }


# ------------------------------------------- Phase 2a: pullback (15m) -----

def _recent_pivot(pivots: list[Pivot], kind: str, current_index: int, max_age: int | None) -> Pivot | None:
    """Most recent pivot of `kind`, but only if it's still within `max_age`
    bars of the current bar -- a swing from long ago is no longer a
    meaningful reference level for a pullback zone or a stop."""
    candidates = [p for p in pivots if p.kind == kind]
    if not candidates:
        return None
    latest = candidates[-1]
    if max_age is not None and (current_index - latest.index) > max_age:
        return None
    return latest


def evaluate_pullback_zone(
    candles_15m: list[Candle], direction: str, ma_fast: int, zigzag_atr_period: int, zigzag_atr_mult: float,
    pivot_max_age: int | None = None, zone_atr_tolerance: float = 0.25,
) -> dict:
    closes = [c.close for c in candles_15m]
    ma = compute_sma(closes, ma_fast)
    atr = compute_atr(candles_15m, zigzag_atr_period)
    pivots = zigzag(candles_15m, zigzag_atr_period, zigzag_atr_mult)
    swing = _recent_pivot(pivots, "low" if direction == "up" else "high", len(candles_15m) - 1, pivot_max_age)
    swing_level = swing.price if swing else None

    # A literal "the candle's wick touched the exact level" is stricter than
    # how a pullback is actually judged in practice -- allow price to come
    # within a small ATR-scaled band of the level too.
    band = zone_atr_tolerance * atr if atr else 0.0
    cur = candles_15m[-1]
    touched_ma = ma is not None and (cur.low - band) <= ma <= (cur.high + band)
    touched_swing = swing_level is not None and (cur.low - band) <= swing_level <= (cur.high + band)
    return {
        "in_zone": touched_ma or touched_swing, "ma": ma, "swing_level": swing_level,
        "touched_ma": touched_ma, "touched_swing": touched_swing,
    }


# ------------------------------------------ Phase 2b: confirmation (5m) --

def _is_bullish_engulfing(prev: Candle, cur: Candle) -> bool:
    return cur.close > cur.open and prev.close < prev.open and cur.close >= prev.open and cur.open <= prev.close


def _is_bearish_engulfing(prev: Candle, cur: Candle) -> bool:
    return cur.close < cur.open and prev.close > prev.open and cur.open >= prev.close and cur.close <= prev.open


def _is_bullish_pin_bar(c: Candle) -> bool:
    body = abs(c.close - c.open)
    lower_wick = min(c.close, c.open) - c.low
    upper_wick = c.high - max(c.close, c.open)
    return body > 0 and lower_wick >= 2 * body and lower_wick > upper_wick


def _is_bearish_pin_bar(c: Candle) -> bool:
    body = abs(c.close - c.open)
    upper_wick = c.high - max(c.close, c.open)
    lower_wick = min(c.close, c.open) - c.low
    return body > 0 and upper_wick >= 2 * body and upper_wick > lower_wick


def _breaks_range(candles: list[Candle], lookback: int, direction: str) -> bool:
    if len(candles) < lookback + 1:
        return False
    window = candles[-(lookback + 1):-1]
    cur = candles[-1]
    if direction == "up":
        return cur.close > max(c.high for c in window)
    return cur.close < min(c.low for c in window)


def evaluate_confirmation(candles_5m: list[Candle], direction: str, lookback: int = 5) -> dict:
    cur = candles_5m[-1]
    prev = candles_5m[-2] if len(candles_5m) >= 2 else None
    pattern = None

    if direction == "up":
        if prev is not None and _is_bullish_engulfing(prev, cur):
            pattern = "Bullish Engulfing"
        elif _is_bullish_pin_bar(cur):
            pattern = "Bullish Pin Bar"
        elif _breaks_range(candles_5m, lookback, "up"):
            pattern = f"Break of last {lookback} candles' high"
    else:
        if prev is not None and _is_bearish_engulfing(prev, cur):
            pattern = "Bearish Engulfing"
        elif _is_bearish_pin_bar(cur):
            pattern = "Bearish Pin Bar"
        elif _breaks_range(candles_5m, lookback, "down"):
            pattern = f"Break of last {lookback} candles' low"

    return {"confirmed": pattern is not None, "pattern": pattern}


# --------------------------------------------- Phase 3/4: SL / TP --------

def compute_stop_loss(pivots_15m: list[Pivot], direction: str, atr15: float | None, current_index: int, pivot_max_age: int | None = None) -> float | None:
    if atr15 is None:
        return None
    pivot = _recent_pivot(pivots_15m, "low" if direction == "up" else "high", current_index, pivot_max_age)
    if pivot is None:
        return None
    return pivot.price - 0.5 * atr15 if direction == "up" else pivot.price + 0.5 * atr15


def compute_take_profit(entry: float, stop: float, direction: str, rr: float = 2.0) -> float:
    risk = abs(entry - stop)
    return entry + rr * risk if direction == "up" else entry - rr * risk


# -------------------------------------------------------- decision core --

def new_cache() -> dict:
    return {"h1_count": 0, "m15_count": 0, "trend": {"direction": "none"}, "pullback": None}


def evaluate_state(m5_so_far: list[Candle], h1_closed: list[Candle], m15_closed: list[Candle], cache: dict, params: dict) -> dict:
    """One evaluation of all 4 phases at the current point in time. Trend
    (1H) and pullback (15m) are only recomputed when a new bar of that
    timeframe has actually closed (tracked via cache) — this is what keeps
    a multi-thousand-bar backtest fast without changing the result, since
    those states are constant between closes anyway."""
    if len(h1_closed) > cache["h1_count"]:
        cache["h1_count"] = len(h1_closed)
        cache["trend"] = (
            evaluate_trend(h1_closed, params["adx_period"], params["adx_threshold"], params["ma_fast"],
                            params["ma_slow"], params["zigzag_atr_period"], params["zigzag_atr_mult"])
            if len(h1_closed) >= 2 else {"direction": "none", "reason": "no closed 1H candle yet"}
        )

    trend = cache["trend"]

    if len(m15_closed) > cache["m15_count"]:
        cache["m15_count"] = len(m15_closed)
        cache["pullback"] = (
            evaluate_pullback_zone(m15_closed, trend["direction"], params["pullback_ma"],
                                    params["zigzag_atr_period"], params["zigzag_atr_mult"],
                                    params["pivot_max_age"], params["zone_atr_tolerance"])
            if trend["direction"] != "none" and len(m15_closed) >= 2 else None
        )

    pullback = cache["pullback"] if trend["direction"] != "none" else None

    confirmation = None
    signal, stop, take_profit = "FLAT", None, None
    if trend["direction"] != "none" and pullback and pullback["in_zone"] and len(m5_so_far) >= 2 and len(m15_closed) >= 2:
        confirmation = evaluate_confirmation(m5_so_far, trend["direction"], params["confirmation_lookback"])
        if confirmation["confirmed"]:
            atr15 = compute_atr(m15_closed, params["zigzag_atr_period"])
            pivots15 = zigzag(m15_closed, params["zigzag_atr_period"], params["zigzag_atr_mult"])
            stop = compute_stop_loss(pivots15, trend["direction"], atr15, len(m15_closed) - 1, params["pivot_max_age"])
            entry = m5_so_far[-1].close
            # A stale/far-away ZigZag pivot can land the stop on the wrong
            # side of entry (e.g. below entry for a short) — reject rather
            # than open a trade with an inverted, meaningless stop.
            valid_stop = stop is not None and (
                (trend["direction"] == "up" and stop < entry) or (trend["direction"] == "down" and stop > entry)
            )
            if valid_stop:
                take_profit = compute_take_profit(entry, stop, trend["direction"], params["rr"])
                signal = "LONG" if trend["direction"] == "up" else "SHORT"
            else:
                stop = None

    return {
        "trend_1h": trend, "pullback_15m": pullback, "confirmation_5m": confirmation,
        "signal": signal, "entry": m5_so_far[-1].close, "stop_loss": stop, "take_profit": take_profit,
    }


# -------------------------------------------------------------- backtest --

def run_backtest(m5_candles: list[Candle], params: dict) -> list[Trade]:
    r15, r1h = Resampler(15), Resampler(60)
    cache = new_cache()
    trades: list[Trade] = []
    position: Trade | None = None

    for i, cur in enumerate(m5_candles):
        r15.push(cur)
        r1h.push(cur)

        if position is not None:
            if position.direction == "LONG":
                hit_sl, hit_tp = cur.low <= position.stop_loss, cur.high >= position.take_profit
            else:
                hit_sl, hit_tp = cur.high >= position.stop_loss, cur.low <= position.take_profit
            if hit_sl or hit_tp:
                position.exit_dt = cur.dt
                position.exit_price = position.stop_loss if hit_sl else position.take_profit
                position.exit_reason = "sl" if hit_sl else "tp"
                trades.append(position)
                position = None
            continue

        state = evaluate_state(m5_candles[: i + 1], r1h.closed, r15.closed, cache, params)
        if state["signal"] in ("LONG", "SHORT"):
            position = Trade(
                direction=state["signal"], entry_dt=cur.dt, entry_price=state["entry"],
                stop_loss=state["stop_loss"], take_profit=state["take_profit"],
            )

    return trades


def summarize_trades(trades: list[Trade], lot: float, contract_size: float, capital: float) -> dict:
    if not trades:
        return {"trades": 0}

    equity = capital
    curve = [equity]
    wins, losses = [], []
    for t in trades:
        pnl = t.pnl_price() * lot * contract_size
        equity += pnl
        curve.append(equity)
        (wins if pnl > 0 else losses).append(pnl)

    peak, max_dd = curve[0], 0.0
    for e in curve:
        peak = max(peak, e)
        if peak:
            max_dd = max(max_dd, (peak - e) / peak * 100)

    gross_win, gross_loss = sum(wins), abs(sum(losses))
    return {
        "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100,
        "final_equity": equity, "total_pnl": equity - capital,
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "max_drawdown_pct": max_dd,
    }


# --------------------------------------------------------------- output --

def print_summary(stats: dict, label: str, capital: float) -> None:
    print(f"\n--- Performance report ({label}) ---")
    if stats["trades"] == 0:
        print("No trades in this period.")
        return
    print(f"Trades           : {stats['trades']} ({stats['wins']} win / {stats['losses']} loss)")
    print(f"Win rate         : {stats['win_rate']:.2f}%")
    print(f"Total P/L        : {stats['total_pnl']:+.2f} (equity {capital:.2f} -> {stats['final_equity']:.2f})")
    print(f"Avg win / loss   : {stats['avg_win']:+.2f} / {stats['avg_loss']:+.2f}")
    pf = "inf" if stats["profit_factor"] == float("inf") else f"{stats['profit_factor']:.2f}"
    print(f"Profit factor    : {pf}")
    print(f"Max drawdown     : {stats['max_drawdown_pct']:.2f}%")


def print_trade_tables(trades: list[Trade], pip_size: float) -> None:
    wins = [t for t in trades if t.pnl_price() > 0]
    losses = [t for t in trades if t.pnl_price() <= 0]

    def _table(title, trade_list):
        print(f"\n{title} ({len(trade_list)})")
        if not trade_list:
            return
        print(f"{'Dir':<5} {'Entry':<20} {'@':>10} {'Exit':<20} {'@':>10} {'Reason':<6} {'Pips':>8}")
        for t in trade_list:
            pips = t.pnl_price() / pip_size
            print(
                f"{t.direction:<5} {t.entry_dt.isoformat(sep=' '):<20} {t.entry_price:>10.5f} "
                f"{t.exit_dt.isoformat(sep=' '):<20} {t.exit_price:>10.5f} {t.exit_reason:<6} {pips:>+8.1f}"
            )

    _table("WINNING TRADES", wins)
    _table("LOSING TRADES", losses)


def print_phase_report(state: dict) -> None:
    t = state["trend_1h"]
    print("--- Phase 1: Trend Direction (1H) ---")
    if t["direction"] == "none":
        print(f"  1H: NONE ({t.get('reason', 'conditions not met')})")
    else:
        print(
            f"  1H: {t['direction'].upper()}  structure={t['structure']}  "
            f"MA_fast={t['ma_fast']:.5f} MA_slow={t['ma_slow']:.5f}  "
            f"ADX={t['adx']:.1f} (+DI={t['plus_di']:.1f} -DI={t['minus_di']:.1f})"
        )

    print("--- Phase 2: Entry Conditions ---")
    if t["direction"] == "none":
        print("  1H trend not confirmed -> no trades")
    else:
        p = state["pullback_15m"]
        if p is None:
            print("  15m: waiting for a closed 15m candle")
        else:
            print(f"  15m Pullback zone : {p['in_zone']}  (touched MA={p['touched_ma']}, touched swing={p['touched_swing']})")
            c = state["confirmation_5m"]
            if c is None:
                print("  5m confirmation    : n/a (not in pullback zone)")
            else:
                print(f"  5m confirmation    : {c['pattern'] or 'none'}")

    print("--- Phase 3/4: Trade Levels ---")
    if state["signal"] in ("LONG", "SHORT"):
        print(f"  Signal      : {state['signal']}")
        print(f"  Entry       : {state['entry']:.5f}")
        print(f"  Stop Loss   : {state['stop_loss']:.5f}")
        print(f"  Take Profit : {state['take_profit']:.5f}")
    else:
        print("  Signal      : FLAT")


# -------------------------------------------------------------- telegram --

def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10) as resp:
            resp.read()
    except Exception as e:
        print(f"  [Telegram] Failed to send: {e}")


# --------------------------------------------------------------- fetch ---

def fetch_candles(args, symbol: str | None = None) -> list[Candle]:
    if args.mt5:
        return fetch_mt5(symbol or args.symbol, args.history_days, args.login, args.password, args.server, args.path)
    if args.yfinance:
        return fetch_yfinance(symbol or args.symbol, args.history_days)
    return load_csv(args.csv_path)


# --------------------------------------------------------------- live ----

def live_loop(args, symbols: list[str], params: dict, telegram_token: str | None, telegram_chat_id: str | None) -> None:
    """Watches one symbol, or several in rotation each tick (CSV mode has
    no symbol concept, so `symbols` is empty and this runs a single
    unlabeled pass instead)."""
    label = ", ".join(symbols) if symbols else "data"
    print(f"\n============= LIVE MONITORING: {label} (every {args.interval} min, Ctrl+C to stop) =============")
    iter_symbols = symbols or [None]
    last_signal = {s: None for s in iter_symbols}
    caches = {s: new_cache() for s in iter_symbols}

    while True:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for sym in iter_symbols:
            tag = f"{sym}: " if sym else ""
            try:
                candles = fetch_candles(args, symbol=sym)
                r15, r1h = build_resamplers(candles)
                state = evaluate_state(candles, r1h.closed, r15.closed, caches[sym], params)
            except (ValueError, RuntimeError, FileNotFoundError) as e:
                print(f"[{now}] {tag}Fetch/analysis error: {e}")
                continue

            print(f"\n[{now}] {tag}".rstrip())
            print_phase_report(state)

            signal = state["signal"]
            if signal in ("LONG", "SHORT") and signal != last_signal[sym]:
                text = (
                    f"{sym + ' ' if sym else ''}TRADE: {signal}\n"
                    f"Entry: {state['entry']:.5f}\nStop Loss: {state['stop_loss']:.5f}\nTake Profit: {state['take_profit']:.5f}"
                )
                print(f"[{now}] {tag}>>> NEW TRADE — {signal} entry={state['entry']:.5f} sl={state['stop_loss']:.5f} tp={state['take_profit']:.5f}")
                if telegram_token and telegram_chat_id:
                    send_telegram(telegram_token, telegram_chat_id, text)
                else:
                    print("  [Telegram] not configured (--telegram-token/--telegram-chat-id) -- skipped")
            else:
                print(f"[{now}] {tag}No new trade (signal: {signal})")
            last_signal[sym] = signal

        time.sleep(args.interval * 60)


# --------------------------------------------------------- optimization --

def _rank_key(r: dict) -> tuple:
    return (r["profit_factor"] if r["profit_factor"] != float("inf") else 1e9, r["trades"])


def _refine_grid(center: float, step: float, min_val: float) -> list[float]:
    return sorted({v for v in (round(center - step, 4), round(center, 4), round(center + step, 4)) if v >= min_val})


def _grid_search(candles: list[Candle], base_params: dict, adx_vals, zigzag_vals, rr_vals, cutoff, lot, contract_size, capital, min_trades, tag) -> list[dict]:
    results = []
    total = len(adx_vals) * len(zigzag_vals) * len(rr_vals)
    for done, (adx_t, zz, rr) in enumerate(
        ((a, z, r) for a in adx_vals for z in zigzag_vals for r in rr_vals), 1
    ):
        params = dict(base_params, adx_threshold=adx_t, zigzag_atr_mult=zz, rr=rr)
        trades = run_backtest(candles, params)
        recent = [t for t in trades if t.entry_dt >= cutoff]
        stats = summarize_trades(recent, lot, contract_size, capital)
        print(f"  [{tag} {done}/{total}] adx>={adx_t:g} zigzag={zz:g} rr={rr:g} -> trades={stats.get('trades', 0)}", flush=True)
        if stats.get("trades", 0) >= min_trades:
            results.append({"adx_threshold": adx_t, "zigzag_mult": zz, "rr": rr, **stats})
    return results


def run_optimization(candles: list[Candle], base_params: dict, days: int, lot: float, contract_size: float, capital: float, min_trades: int = 5) -> list[dict]:
    """Coarse grid-search adx_threshold / zigzag_mult / rr on the already-
    fetched history, then a finer second pass around the coarse winner
    (its immediate neighbours on each axis) to check whether a nearby,
    untested combination does even better -- lets the data decide, rather
    than a guess, and without the cost of a full fine grid everywhere.
    Combos with fewer than `min_trades` in the report window are dropped
    since their stats aren't meaningful."""
    coarse_adx, coarse_zigzag, coarse_rr = [15.0, 20.0, 25.0, 30.0], [1.0, 1.5, 2.0], [1.5, 2.0, 2.5, 3.0]
    cutoff = candles[-1].dt - timedelta(days=days)

    coarse = _grid_search(candles, base_params, coarse_adx, coarse_zigzag, coarse_rr, cutoff, lot, contract_size, capital, min_trades, "coarse")
    if not coarse:
        return []

    coarse.sort(key=_rank_key, reverse=True)
    best = coarse[0]
    fine_adx = _refine_grid(best["adx_threshold"], 2.5, min_val=10.0)
    fine_zigzag = _refine_grid(best["zigzag_mult"], 0.2, min_val=0.5)
    fine_rr = _refine_grid(best["rr"], 0.25, min_val=1.0)

    print(f"\n  Refining around adx>={best['adx_threshold']:g} zigzag={best['zigzag_mult']:g} rr={best['rr']:g} ...")
    fine = _grid_search(candles, base_params, fine_adx, fine_zigzag, fine_rr, cutoff, lot, contract_size, capital, min_trades, "fine")

    merged = {(r["adx_threshold"], r["zigzag_mult"], r["rr"]): r for r in coarse + fine}
    results = sorted(merged.values(), key=_rank_key, reverse=True)
    return results


def print_optimization_results(results: list[dict], days: int, min_trades: int) -> None:
    print(f"\n=== Optimization results (last {days} days, min {min_trades} trades) ===")
    if not results:
        print(f"No combination reached {min_trades} trades in this window. Try --history-days with more data, or lower the bar by editing min_trades.")
        return

    print(f"{'ADX>=':>6} {'ZigZag':>7} {'RR':>5} {'Trades':>7} {'Win%':>7} {'PF':>7} {'PnL':>10}")
    for r in results[:15]:
        pf = "inf" if r["profit_factor"] == float("inf") else f"{r['profit_factor']:.2f}"
        print(f"{r['adx_threshold']:>6.0f} {r['zigzag_mult']:>7.1f} {r['rr']:>5.1f} {r['trades']:>7} {r['win_rate']:>6.1f}% {pf:>7} {r['total_pnl']:>+10.2f}")

    best = results[0]
    print(f"\nBest by profit factor: --adx-threshold {best['adx_threshold']:g} --zigzag-mult {best['zigzag_mult']:g} --rr {best['rr']:g}")
    print("Re-run without --optimize using those flags to get the normal backtest + live analysis with them.")


# ---------------------------------------------------------------- main ---

def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-timeframe trend-following strategy: backtest + live signals.")
    parser.add_argument("csv_path", nargs="?", help="Path to a 5-minute OHLC CSV: date,open,high,low,close")
    parser.add_argument("--mt5", action="store_true", help="Fetch M5 candles live from a running MT5 terminal")
    parser.add_argument("--yfinance", action="store_true", help="Fetch M5 candles from Yahoo Finance")
    parser.add_argument("--symbol", help="Symbol, or a comma-separated list to scan several at once, e.g. EURUSD,GBPUSD,XAUUSD (required with --mt5/--yfinance)")
    parser.add_argument("--optimize", action="store_true", help="Grid-search adx-threshold/zigzag-mult/rr on the fetched history instead of running once with the given values (single symbol only)")
    parser.add_argument("--opt-min-trades", type=int, default=5, help="--optimize: minimum trades in the report window for a combination to be shown (default: 5)")
    parser.add_argument("--history-days", type=int, default=90, help="How many days of M5 history to fetch (default: 90, gives warm-up + report window)")

    parser.add_argument("--login", type=int, help="MT5 account login")
    parser.add_argument("--password", help="MT5 account password")
    parser.add_argument("--server", help="MT5 broker server name")
    parser.add_argument("--path", help="Path to terminal64.exe, if MT5 isn't already running")

    parser.add_argument("--days", type=int, default=30, help="Backtest report window in days (default: 30)")
    parser.add_argument("--adx-period", type=int, default=14, help="ADX period (default: 14)")
    parser.add_argument("--adx-threshold", type=float, default=20.0, help="Minimum ADX to confirm a trend (default: 20)")
    parser.add_argument("--ma-fast", type=int, default=50, help="Fast MA period (default: 50)")
    parser.add_argument("--ma-slow", type=int, default=200, help="Slow MA period (default: 200)")
    parser.add_argument("--zigzag-atr-period", type=int, default=14, help="ATR period for ZigZag/stop-loss (default: 14)")
    parser.add_argument("--zigzag-mult", type=float, default=1.0, help="ATR multiple for ZigZag pivot confirmation (default: 1.0)")
    parser.add_argument("--pullback-ma", type=int, default=50, help="MA period checked for the 15m pullback zone (default: 50)")
    parser.add_argument("--confirm-lookback", type=int, default=5, help="Bars used for the 5m break-of-range confirmation (default: 5)")
    parser.add_argument("--rr", type=float, default=1.5, help="Reward:risk ratio for take-profit (default: 1.5)")
    parser.add_argument("--pivot-max-age", type=int, default=200, help="Ignore a ZigZag swing (for pullback/stop) older than this many 15m bars (default: 200 ~ 50h); 0 disables the limit")
    parser.add_argument("--zone-atr-tolerance", type=float, default=0.25, help="ATR multiple of tolerance around MA/swing to count as 'touched' for the pullback zone (default: 0.25)")

    parser.add_argument("--lot", type=float, default=0.01, help="Fixed lot size per trade (default: 0.01)")
    parser.add_argument("--contract-size", type=float, default=100000, help="Units per 1.0 lot (default: 100000)")
    parser.add_argument("--pip-size", type=float, default=0.0001, help="Price move that equals 1 pip (default: 0.0001; use 0.01 for JPY pairs)")
    parser.add_argument("--capital", type=float, default=10000.0, help="Starting capital for the equity curve (default: 10000)")

    parser.add_argument("--live", action="store_true", help="After the one-shot backtest + analysis, keep checking for new trades every --interval minutes")
    parser.add_argument("--interval", type=float, default=5.0, help="Live-mode: minutes between checks (default: 5)")
    parser.add_argument("--telegram-token", help="Telegram bot token (or set TELEGRAM_BOT_TOKEN)")
    parser.add_argument("--telegram-chat-id", help="Telegram chat id (or set TELEGRAM_CHAT_ID)")
    args = parser.parse_args()

    if args.mt5 and args.yfinance:
        parser.error("choose only one of --mt5 / --yfinance")
    if (args.mt5 or args.yfinance) and not args.symbol:
        parser.error("--mt5/--yfinance require --symbol")
    if not args.mt5 and not args.yfinance and not args.csv_path:
        parser.error("provide a csv_path, or use --mt5/--yfinance --symbol ...")

    symbols = [s.strip() for s in args.symbol.split(",")] if (args.mt5 or args.yfinance) and args.symbol else []
    if args.optimize and len(symbols) > 1:
        parser.error("--optimize works on a single symbol at a time -- pass just one --symbol")

    params = dict(
        adx_period=args.adx_period, adx_threshold=args.adx_threshold, ma_fast=args.ma_fast, ma_slow=args.ma_slow,
        zigzag_atr_period=args.zigzag_atr_period, zigzag_atr_mult=args.zigzag_mult, pullback_ma=args.pullback_ma,
        confirmation_lookback=args.confirm_lookback, rr=args.rr,
        pivot_max_age=(args.pivot_max_age or None), zone_atr_tolerance=args.zone_atr_tolerance,
    )

    if args.optimize:
        candles = fetch_candles(args, symbol=symbols[0] if symbols else None)
        print(f"Loaded {len(candles)} M5 candles ({candles[0].dt} -> {candles[-1].dt})")
        print("\n============= OPTIMIZING (coarse grid, then a finer pass around the best result -- this can take a while) =============")
        results = run_optimization(candles, params, args.days, args.lot, args.contract_size, args.capital, args.opt_min_trades)
        print_optimization_results(results, args.days, args.opt_min_trades)
        return

    telegram_token = args.telegram_token or os.environ.get("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = args.telegram_chat_id or os.environ.get("TELEGRAM_CHAT_ID")

    for sym in (symbols or [None]):
        header = f" -- {sym}" if sym else ""
        candles = fetch_candles(args, symbol=sym)
        print(f"\nLoaded {len(candles)} M5 candles{header} ({candles[0].dt} -> {candles[-1].dt})")

        print(f"\n============= BACKTEST{header} =============")
        all_trades = run_backtest(candles, params)
        cutoff = candles[-1].dt - timedelta(days=args.days)
        recent_trades = [t for t in all_trades if t.entry_dt >= cutoff]
        print(f"Trades in last {args.days} days: {len(recent_trades)} (of {len(all_trades)} total over {len(candles)} M5 candles fetched)")
        stats = summarize_trades(recent_trades, args.lot, args.contract_size, args.capital)
        print_summary(stats, f"last {args.days} days", args.capital)
        print_trade_tables(recent_trades, args.pip_size)

        print(f"\n============= CURRENT ANALYSIS{header} =============")
        r15, r1h = build_resamplers(candles)
        cache = new_cache()
        state = evaluate_state(candles, r1h.closed, r15.closed, cache, params)
        print_phase_report(state)

    if args.live:
        live_loop(args, symbols, params, telegram_token, telegram_chat_id)


if __name__ == "__main__":
    try:
        main()
    except (FileNotFoundError, ValueError, KeyError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)
