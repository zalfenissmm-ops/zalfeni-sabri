#!/usr/bin/env python3
"""
SMC — Smart Money Concepts toolkit in a single file, no external libraries.

The whole thing, one command:

    python smc.py XAUUSD

That backtests the strategy on MT5's own M15 candles. Everything else is
optional detail on top of it:

    python smc.py XAUUSD --tf M5 --bars 20000   # different timeframe / history
    python smc.py data.csv                      # a CSV file instead of MT5
    python smc.py trend XAUUSD --tf H4          # just the trend
    python smc.py scan  XAUUSD                  # just the setup on the chart now
    python smc.py fetch XAUUSD --out data.csv   # save the candles to a CSV

MT5 mode needs Windows and the terminal open and logged in; the MetaTrader5
package installs itself on first use. A CSV needs a header row with the
columns: date,open,high,low,close

The backtester simulates a trader in the order a person actually works:

    for each candle:
        1. manage what is already open (stop, partial, breakeven, trail, target)
        2. see whether a resting limit order got filled
        3. only then look for a new setup

Every feature (swing, liquidity pool, structure event, FVG) carries the bar
index that *confirms* it, so a signal at bar i never reads a candle after i,
and a signal found at bar i can never fill before bar i+1. No lookahead.

Layout of this file:
    1. data          candles, CSV and MT5 loading, date parsing
    2. math          EMA, Wilder smoothing, ADX
    3. detectors     swings, structure (BOS/CHoCH), liquidity, FVG, order blocks
    4. trend         the `trend` command
    5. scan          the `scan` command
    6. signals       the six STRATEGY.md conditions as causal signals
    7. backtest      money, risk rules, the trader loop, reporting
    8. cli           argument parsing
"""

VERSION = "1.1"

import argparse
import csv
import os
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

# ============================================================== 1. data


@dataclass
class Candle:
    date: str
    open: float
    high: float
    low: float
    close: float


def load_candles(path: str) -> list[Candle]:
    candles = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            candles.append(
                Candle(
                    date=row["date"],
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                )
            )
    if len(candles) < 2:
        raise ValueError("Need at least 2 rows of price data")
    return candles


DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y.%m.%d %H:%M:%S",
    "%Y.%m.%d %H:%M",
    "%Y.%m.%d",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
)


def parse_date(text: str) -> datetime | None:
    text = text.strip()
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _import_mt5():
    """Import the MetaTrader5 package, installing it the first time if needed.
    Asking someone to run a second command before the first one works is a step
    this script can take for itself."""
    try:
        import MetaTrader5 as mt5

        return mt5
    except ImportError:
        pass

    print("First run: installing the MetaTrader5 package (one time only)...")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "MetaTrader5"]
        )
    except (subprocess.CalledProcessError, OSError) as e:
        raise ValueError(
            f"Could not install the MetaTrader5 package automatically ({e}).\n"
            "Install it by hand with:  pip install MetaTrader5\n"
            "Note it only exists for Windows — on Mac or Linux, export a CSV from "
            "MT5 instead and pass the file."
        )

    try:
        import MetaTrader5 as mt5
    except ImportError:
        raise ValueError(
            "The MetaTrader5 package installed but will not import. It is Windows-only; "
            "on Mac or Linux export a CSV from MT5 and pass the file instead."
        )
    print("Done.\n")
    return mt5


MT5_TIMEFRAMES = (
    "M1", "M2", "M3", "M4", "M5", "M6", "M10", "M12", "M15", "M20", "M30",
    "H1", "H2", "H3", "H4", "H6", "H8", "H12", "D1", "W1", "MN1",
)


def load_from_mt5(symbol: str, timeframe: str = "M15", bars: int = 5000) -> tuple[list[Candle], dict]:
    """Pull closed candles straight out of a running MetaTrader 5 terminal.

    Needs Windows, `pip install MetaTrader5`, and the terminal open and logged in.
    Bar 0 is the candle still forming, so we start at 1 and take only closed bars —
    a half-built candle would give the backtest a high and low it could not know.

    Also returns the symbol's contract specs, so the backtest can price a trade
    from the broker's own tick value instead of a guess.
    """
    mt5 = _import_mt5()

    name = timeframe.upper()
    if name not in MT5_TIMEFRAMES:
        raise ValueError(f"Unknown timeframe {timeframe!r}. Pick one of: {', '.join(MT5_TIMEFRAMES)}")

    if not mt5.initialize():
        raise ValueError(
            f"Cannot reach the MT5 terminal ({mt5.last_error()}). "
            "Open MetaTrader 5, log in to your account, and try again."
        )
    try:
        if not mt5.symbol_select(symbol, True):
            raise ValueError(
                f"Symbol {symbol!r} not found. Check the exact spelling in the MT5 Market Watch "
                "window — brokers add suffixes like XAUUSD.m or XAUUSDm."
            )
        info = mt5.symbol_info(symbol)
        rates = mt5.copy_rates_from_pos(symbol, getattr(mt5, f"TIMEFRAME_{name}"), 1, bars)
        if rates is None or len(rates) == 0:
            raise ValueError(f"MT5 returned no bars for {symbol} {name} ({mt5.last_error()})")

        spec = {}
        if info is not None:
            spec = {
                "digits": info.digits,
                "point": info.point,
                "tick_size": info.trade_tick_size or info.point,
                "tick_value": info.trade_tick_value,
            }
        candles = [
            Candle(
                date=datetime.fromtimestamp(int(r["time"]), timezone.utc).strftime("%Y-%m-%d %H:%M"),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
            )
            for r in rates
        ]
    finally:
        mt5.shutdown()

    if len(candles) < 2:
        raise ValueError(f"MT5 only returned {len(candles)} candle(s) — ask for more with --bars")
    return candles, spec


def _looks_like_file(source: str) -> bool:
    """`XAUUSD` is a symbol, `data.csv` is a file. Nobody should have to say which."""
    return os.path.exists(source) or source.lower().endswith((".csv", ".txt", ".tsv"))


def get_candles(args: argparse.Namespace) -> tuple[list[Candle], dict]:
    """Candles come from MT5 for a symbol, or from disk for a filename."""
    source = getattr(args, "mt5", None) or args.csv_path
    if source and not _looks_like_file(source):
        candles, spec = load_from_mt5(source, args.tf, args.bars)
        print(f"Loaded {len(candles)} closed {args.tf.upper()} candles for {source} from MT5")
        print(f"Range: {candles[0].date}  ->  {candles[-1].date}\n")
        return candles, spec
    if not source:
        raise ValueError("No data source. Give an MT5 symbol or a CSV file:\n    python smc.py XAUUSD")
    return load_candles(source), {}


# ============================================================== 2. math


def ema(values: list[float], period: int) -> list[float]:
    k = 2 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def wilder_smooth_sum(values: list[float], period: int) -> list[float]:
    """Wilder smoothing for TR/+DM/-DM: seeded with the raw sum of the first
    `period` values, then each step keeps a running sum-like total."""
    result = [sum(values[:period])]
    for v in values[period:]:
        result.append(result[-1] - result[-1] / period + v)
    return result


def wilder_smooth_avg(values: list[float], period: int) -> list[float]:
    """Wilder smoothing for DX -> ADX: seeded with the average of the first
    `period` values, then each step is a weighted running average."""
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append((result[-1] * (period - 1) + v) / period)
    return result


def adx_series(candles: list[Candle], period: int = 14) -> list[float | None]:
    """ADX aligned to candle index: element i is the value a trader could read
    at candle i's close, or None while Wilder's warm-up (2*period-1 candles) is
    still filling. Aligned indexing lets the backtest read ADX at an arbitrary
    bar without recomputing the series."""
    aligned: list[float | None] = [None] * len(candles)
    if len(candles) < period * 2:
        return aligned

    plus_dm, minus_dm, tr = [], [], []
    for prev, cur in zip(candles, candles[1:]):
        up_move = cur.high - prev.high
        down_move = prev.low - cur.low
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        tr.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))

    smoothed_tr = wilder_smooth_sum(tr, period)
    smoothed_plus_dm = wilder_smooth_sum(plus_dm, period)
    smoothed_minus_dm = wilder_smooth_sum(minus_dm, period)

    dx_values = []
    for t, pdm, mdm in zip(smoothed_tr, smoothed_plus_dm, smoothed_minus_dm):
        if t == 0:
            dx_values.append(0.0)
            continue
        plus_di = 100 * pdm / t
        minus_di = 100 * mdm / t
        di_sum = plus_di + minus_di
        dx_values.append(0.0 if di_sum == 0 else 100 * abs(plus_di - minus_di) / di_sum)

    values = wilder_smooth_avg(dx_values, period)
    offset = 2 * period - 1  # first candle index the smoothed DX average covers
    for j, value in enumerate(values):
        aligned[offset + j] = value
    return aligned


def adx(candles: list[Candle], period: int = 14) -> float:
    """Latest ADX value using Wilder's original method."""
    if len(candles) < period * 2:
        raise ValueError(f"Need at least {period * 2} candles to compute ADX({period})")
    return adx_series(candles, period)[-1]


# ============================================================== 3. detectors


def find_swings(candles: list[Candle], window: int = 3) -> list[dict]:
    """Fractal swing highs/lows: a bar is a swing point if it's the extreme
    within `window` bars on each side. `confirmed_at` is the bar where a trader
    could first know about it — the swing itself plus `window` bars of proof."""
    swings = []
    for i in range(window, len(candles) - window):
        segment = candles[i - window : i + window + 1]
        if candles[i].high == max(c.high for c in segment):
            swings.append({"index": i, "price": candles[i].high, "kind": "high", "confirmed_at": i + window})
        if candles[i].low == min(c.low for c in segment):
            swings.append({"index": i, "price": candles[i].low, "kind": "low", "confirmed_at": i + window})
    swings.sort(key=lambda s: s["index"])
    return swings


def structure_bias(swings: list[dict]) -> str:
    """Compare the last two swing highs and the last two swing lows."""
    highs = [s["price"] for s in swings if s["kind"] == "high"]
    lows = [s["price"] for s in swings if s["kind"] == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return "unknown"
    higher_highs = highs[-1] > highs[-2]
    higher_lows = lows[-1] > lows[-2]
    if higher_highs and higher_lows:
        return "up"
    if not higher_highs and not higher_lows:
        return "down"
    return "mixed"


def classify_structure(candles: list[Candle], swings: list[dict], window: int) -> list[dict]:
    """Walk candles in order, tracking the most recently confirmed swing high/low.
    A close beyond it is a BOS if it continues the current bias, or a CHoCH if it
    flips it."""
    highs = sorted((s for s in swings if s["kind"] == "high"), key=lambda s: s["index"])
    lows = sorted((s for s in swings if s["kind"] == "low"), key=lambda s: s["index"])

    events = []
    bias = None
    hi_ptr = lo_ptr = 0
    active_high = active_low = None

    for i, candle in enumerate(candles):
        while hi_ptr < len(highs) and highs[hi_ptr]["index"] + window <= i:
            active_high = highs[hi_ptr]
            hi_ptr += 1
        while lo_ptr < len(lows) and lows[lo_ptr]["index"] + window <= i:
            active_low = lows[lo_ptr]
            lo_ptr += 1

        if active_high is not None and candle.close > active_high["price"]:
            events.append({
                "index": i, "type": "BOS" if bias == "up" else "CHoCH",
                "direction": "up", "level": active_high["price"],
            })
            bias = "up"
            active_high = None

        if active_low is not None and candle.close < active_low["price"]:
            events.append({
                "index": i, "type": "BOS" if bias == "down" else "CHoCH",
                "direction": "down", "level": active_low["price"],
            })
            bias = "down"
            active_low = None

    return events


def find_liquidity_pools(swings: list[dict], tolerance: float = 0.0015) -> list[dict]:
    """Equal highs/lows within `tolerance` of each other: resting liquidity."""
    pools = []
    for kind, pick in (("high", max), ("low", min)):
        points = sorted((s for s in swings if s["kind"] == kind), key=lambda s: s["index"])
        for a, b in zip(points, points[1:]):
            if abs(a["price"] - b["price"]) / a["price"] <= tolerance:
                pools.append({
                    "kind": kind, "level": pick(a["price"], b["price"]),
                    "indices": (a["index"], b["index"]),
                })
    return pools


def find_liquidity_sweeps(candles: list[Candle], pools: list[dict]) -> list[dict]:
    """A wick through a liquidity pool that closes back on the other side."""
    sweeps = []
    for pool in pools:
        start = max(pool["indices"])
        for i in range(start + 1, len(candles)):
            c = candles[i]
            if pool["kind"] == "high" and c.high > pool["level"] and c.close < pool["level"]:
                sweeps.append({"index": i, "kind": "high", "level": pool["level"]})
                break
            if pool["kind"] == "low" and c.low < pool["level"] and c.close > pool["level"]:
                sweeps.append({"index": i, "kind": "low", "level": pool["level"]})
                break
    return sweeps


def find_fvgs(candles: list[Candle]) -> list[dict]:
    """3-candle imbalance: a gap between candle[i].high/low and candle[i+2]'s."""
    fvgs = []
    for i in range(len(candles) - 2):
        c1, c3 = candles[i], candles[i + 2]
        if c1.high < c3.low:
            fvgs.append({"index": i + 1, "kind": "bullish", "bottom": c1.high, "top": c3.low})
        elif c1.low > c3.high:
            fvgs.append({"index": i + 1, "kind": "bearish", "bottom": c3.high, "top": c1.low})
    return fvgs


def find_order_blocks(candles: list[Candle], lookback: int = 10, impulse_mult: float = 1.8) -> list[dict]:
    """The last opposite-coloured candle before an impulsive (above-average range)
    move — the zone smart money is expected to return to."""
    ranges = [c.high - c.low for c in candles]
    obs = []
    for i in range(lookback, len(candles)):
        avg_range = sum(ranges[i - lookback : i]) / lookback
        candle = candles[i]
        if abs(candle.close - candle.open) <= impulse_mult * avg_range:
            continue
        bearish_impulse = candle.close < candle.open
        for j in range(i - 1, -1, -1):
            prev = candles[j]
            prev_bullish = prev.close >= prev.open
            if bearish_impulse and prev_bullish:
                obs.append({"index": j, "kind": "bearish_ob", "top": prev.high, "bottom": prev.low, "impulse_index": i})
                break
            if not bearish_impulse and not prev_bullish:
                obs.append({"index": j, "kind": "bullish_ob", "top": prev.high, "bottom": prev.low, "impulse_index": i})
                break
    return obs


# ============================================================== 4. trend


def identify_trend(candles: list[Candle], ema_period: int = 50, adx_period: int = 14, swing_window: int = 5) -> dict:
    """Three confirming signals — no single indicator is trusted alone."""
    closes = [c.close for c in candles]
    ema_values = ema(closes, ema_period)
    price_vs_ema = "up" if closes[-1] > ema_values[-1] else "down"

    structure = structure_bias(find_swings(candles, swing_window))

    try:
        adx_value = adx(candles, adx_period)
    except ValueError:
        adx_value = None

    votes_up = (price_vs_ema == "up") + (structure == "up")
    votes_down = (price_vs_ema == "down") + (structure == "down")

    if votes_up > votes_down:
        direction = "Uptrend"
    elif votes_down > votes_up:
        direction = "Downtrend"
    else:
        direction = "Sideways / Unclear"

    if adx_value is None:
        strength = "unknown (not enough data)"
    elif adx_value < 20:
        strength = "weak / ranging"
    elif adx_value < 25:
        strength = "developing"
    else:
        strength = "strong"

    return {
        "direction": direction,
        "strength": strength,
        "adx": adx_value,
        "price_vs_ema": price_vs_ema,
        "ema_period": ema_period,
        "structure": structure,
        "last_close": closes[-1],
        "last_ema": ema_values[-1],
    }


# ============================================================== 5. scan


def find_latest_setup(candles: list[Candle], window: int = 3, tolerance: float = 0.0015, impulse_mult: float = 1.8) -> dict | None:
    """Combine every detector around the most recent CHoCH to describe the SMC
    setup on the chart right now."""
    swings = find_swings(candles, window)
    structure = classify_structure(candles, swings, window)
    pools = find_liquidity_pools(swings, tolerance)
    sweeps = find_liquidity_sweeps(candles, pools)
    fvgs = find_fvgs(candles)
    obs = find_order_blocks(candles, impulse_mult=impulse_mult)

    choch_events = [e for e in structure if e["type"] == "CHoCH"]
    if not choch_events:
        return None
    last_choch = choch_events[-1]
    direction = "short" if last_choch["direction"] == "down" else "long"

    ob_kind = "bearish_ob" if direction == "short" else "bullish_ob"
    candidate_obs = [o for o in obs if o["kind"] == ob_kind and o["index"] <= last_choch["index"]]
    ob = candidate_obs[-1] if candidate_obs else None

    fvg_kind = "bearish" if direction == "short" else "bullish"
    candidate_fvgs = [f for f in fvgs if f["kind"] == fvg_kind and f["index"] >= last_choch["index"] - 5]
    fvg = candidate_fvgs[0] if candidate_fvgs else None

    candidate_sweeps = [s for s in sweeps if s["index"] <= last_choch["index"]]
    sweep = candidate_sweeps[-1] if candidate_sweeps else None

    leg = candles[last_choch["index"] :]
    if direction == "short":
        swing_high = sweep["level"] if sweep else last_choch["level"]
        swing_low = min(c.low for c in leg)
    else:
        swing_low = sweep["level"] if sweep else last_choch["level"]
        swing_high = max(c.high for c in leg)

    equilibrium = (swing_high + swing_low) / 2
    last_close = candles[-1].close
    in_zone = last_close > equilibrium if direction == "short" else last_close < equilibrium
    in_ob = ob is not None and ob["bottom"] <= last_close <= ob["top"]
    in_fvg = fvg is not None and fvg["bottom"] <= last_close <= fvg["top"]

    if direction == "short":
        stop_loss = max(swing_high, ob["top"] if ob else swing_high) * 1.001
        take_profit = swing_low * 0.999
    else:
        stop_loss = min(swing_low, ob["bottom"] if ob else swing_low) * 0.999
        take_profit = swing_high * 1.001

    return {
        "direction": direction,
        "choch_index": last_choch["index"],
        "sweep": sweep,
        "order_block": ob,
        "fvg": fvg,
        "equilibrium": equilibrium,
        "premium_discount_zone": "premium" if direction == "short" else "discount",
        "in_premium_or_discount": in_zone,
        "in_order_block": in_ob,
        "in_fvg": in_fvg,
        "entry_ready": in_zone and (in_ob or in_fvg),
        "last_close": last_close,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
    }


# ============================================================== 6. signals


@dataclass
class Zone:
    """A point of interest — an order block, an FVG, or their overlap."""

    top: float
    bottom: float
    source: str


@dataclass
class Signal:
    direction: str  # "long" | "short"
    entry: float
    stop_loss: float
    take_profit: float
    zone: Zone
    risk: float  # price distance from entry to stop
    rr: float
    signal_index: int
    sweep_index: int
    sweep_level: float
    choch_index: int
    equilibrium: float


@dataclass
class SignalConfig:
    swing_window: int = 3
    tolerance: float = 0.0015  # equal-highs/lows tolerance, as a fraction
    htf_mult: int = 16  # entry bars per higher-timeframe bar (15m -> 4H)
    htf_swing_window: int = 2
    htf_ema: int = 50  # 0 disables the EMA agreement filter
    min_adx: float = 0.0  # 0 disables the ADX strength filter
    adx_period: int = 14
    require_bias: bool = True
    confirm: str = "choch"  # "choch" = reversal only, "any" = BOS or CHoCH
    choch_window: int = 12  # bars a sweep stays live waiting for confirmation
    min_rr: float = 3.0
    sl_buffer_pct: float = 0.1  # stop padding beyond the sweep wick, in percent
    require_pd: bool = True  # enforce the premium/discount filter


@dataclass
class _Sweep:
    index: int
    direction: str
    level: float
    extreme: float  # the wick tip — where the stop goes


@dataclass
class _Pool:
    kind: str
    level: float
    confirmed_at: int
    swept: bool = False


@dataclass
class _Feature:
    confirmed_at: int
    data: dict = field(default_factory=dict)


class SignalEngine:
    """The six STRATEGY.md conditions as bar-by-bar signals.

        1. HTF bias         resampled higher-timeframe structure (+ EMA / ADX)
        2. liquidity        equal highs/lows built from confirmed swings
        3. sweep            wick through the pool, close back on the other side
        4. confirmation     CHoCH on the entry timeframe within `choch_window`
        5. POI              order block and/or FVG inside the impulse leg
        6. premium/discount the POI must sit on the right side of the leg's 50%

    Drive with `step(i)` for i = 0..n-1 in order; it returns a Signal on the bar
    where everything lines up, otherwise None. Rejections are tallied in
    `self.rejections` so a backtest can report which condition filters most."""

    def __init__(self, candles: list[Candle], config: SignalConfig | None = None):
        self.candles = candles
        self.cfg = config or SignalConfig()
        self.rejections: Counter = Counter()
        self.sweeps_seen = 0

        window = self.cfg.swing_window
        swings = find_swings(candles, window)
        self._swings = swings
        self._swing_ptr = 0
        self.confirmed_swings: list[dict] = []

        self._pools = [
            _Pool(kind=p["kind"], level=p["level"], confirmed_at=max(p["indices"]) + window)
            for p in find_liquidity_pools(swings, self.cfg.tolerance)
        ]
        self._pools.sort(key=lambda p: p.confirmed_at)
        self._pool_ptr = 0
        self._active_pools: list[_Pool] = []

        self._fvgs = [_Feature(confirmed_at=f["index"] + 1, data=f) for f in find_fvgs(candles)]
        self._fvgs.sort(key=lambda f: f.confirmed_at)
        self._fvg_ptr = 0
        self.confirmed_fvgs: list[dict] = []

        self._events_by_index: dict[int, list[dict]] = {}
        for event in classify_structure(candles, swings, window):
            self._events_by_index.setdefault(event["index"], []).append(event)

        self.htf_bias = self._build_htf_bias()
        self._live_sweeps: list[_Sweep] = []

    # ---------------------------------------------------------------- bias

    def _build_htf_bias(self) -> list[str | None]:
        """Resample the entry timeframe into higher-timeframe candles and read the
        bias off their structure. Each HTF candle only becomes usable on the entry
        bar that closes it, so `bias[i]` is always knowable at bar i."""
        n = len(self.candles)
        bias: list[str | None] = [None] * n
        if not self.cfg.require_bias:
            return ["any"] * n

        mult = max(1, self.cfg.htf_mult)
        htf: list[Candle] = []
        end_index: list[int] = []
        for start in range(0, n - mult + 1, mult):
            group = self.candles[start : start + mult]
            htf.append(
                Candle(
                    date=group[0].date,
                    open=group[0].open,
                    high=max(c.high for c in group),
                    low=min(c.low for c in group),
                    close=group[-1].close,
                )
            )
            end_index.append(start + mult - 1)

        if len(htf) < 4:
            return bias

        window = self.cfg.htf_swing_window
        events = classify_structure(htf, find_swings(htf, window), window)
        events_by_index: dict[int, dict] = {e["index"]: e for e in events}

        closes = [c.close for c in htf]
        ema_values = ema(closes, self.cfg.htf_ema) if self.cfg.htf_ema else None
        adx_values = adx_series(htf, self.cfg.adx_period) if self.cfg.min_adx > 0 else None

        current = None
        for k in range(len(htf)):
            event = events_by_index.get(k)
            if event is not None:
                current = event["direction"]

            resolved = current
            if resolved is not None and ema_values is not None:
                if k < self.cfg.htf_ema:  # EMA still seeded on too few closes
                    resolved = None
                elif (closes[k] > ema_values[k]) != (resolved == "up"):
                    resolved = None
            if resolved is not None and adx_values is not None:
                value = adx_values[k]
                if value is None or value < self.cfg.min_adx:
                    resolved = None

            stop = end_index[k + 1] if k + 1 < len(end_index) else n
            for i in range(end_index[k], stop):
                bias[i] = resolved
        return bias

    # ------------------------------------------------------------- stepping

    def step(self, i: int) -> Signal | None:
        self._advance(i)
        self._detect_sweeps(i)
        signal = self._confirm(i)
        self._expire_sweeps(i)
        return signal

    def _advance(self, i: int) -> None:
        while self._swing_ptr < len(self._swings) and self._swings[self._swing_ptr]["confirmed_at"] <= i:
            self.confirmed_swings.append(self._swings[self._swing_ptr])
            self._swing_ptr += 1
        while self._pool_ptr < len(self._pools) and self._pools[self._pool_ptr].confirmed_at <= i:
            self._active_pools.append(self._pools[self._pool_ptr])
            self._pool_ptr += 1
        while self._fvg_ptr < len(self._fvgs) and self._fvgs[self._fvg_ptr].confirmed_at <= i:
            self.confirmed_fvgs.append(self._fvgs[self._fvg_ptr].data)
            self._fvg_ptr += 1

    def _detect_sweeps(self, i: int) -> None:
        """Condition 3: a wick pierces resting liquidity and the candle closes back
        on the other side. A close *beyond* the level is a real break, not a sweep —
        the pool is simply consumed and never traded."""
        candle = self.candles[i]
        bias = self.htf_bias[i]
        for pool in self._active_pools:
            if pool.swept or pool.confirmed_at >= i:
                continue
            if pool.kind == "high" and candle.high > pool.level:
                pool.swept = True
                if candle.close >= pool.level:
                    continue
                self._open_sweep(_Sweep(i, "short", pool.level, candle.high), bias)
            elif pool.kind == "low" and candle.low < pool.level:
                pool.swept = True
                if candle.close <= pool.level:
                    continue
                self._open_sweep(_Sweep(i, "long", pool.level, candle.low), bias)

    def _open_sweep(self, sweep: _Sweep, bias: str | None) -> None:
        self.sweeps_seen += 1
        if bias is None:
            self.rejections["no_htf_bias"] += 1
            return
        wanted = "up" if sweep.direction == "long" else "down"
        if bias != "any" and bias != wanted:
            self.rejections["against_htf_bias"] += 1
            return
        self._live_sweeps.append(sweep)

    def _confirm(self, i: int) -> Signal | None:
        """Condition 4: a close that breaks entry-timeframe structure back in the
        sweep's direction."""
        events = self._events_by_index.get(i)
        if not events:
            return None
        allowed = {"CHoCH"} if self.cfg.confirm == "choch" else {"CHoCH", "BOS"}
        for event in events:
            if event["type"] not in allowed:
                continue
            wanted = "up" if event["direction"] == "up" else "down"
            for sweep in list(self._live_sweeps):
                if (sweep.direction == "long") != (wanted == "up"):
                    continue
                self._live_sweeps.remove(sweep)
                signal = self._build_setup(sweep, i)
                if signal is not None:
                    return signal
        return None

    def _expire_sweeps(self, i: int) -> None:
        for sweep in list(self._live_sweeps):
            if i - sweep.index > self.cfg.choch_window:
                self._live_sweeps.remove(sweep)
                self.rejections["no_confirmation"] += 1

    # ---------------------------------------------------------------- setup

    def _build_setup(self, sweep: _Sweep, choch_index: int) -> Signal | None:
        """Conditions 5 and 6 plus the entry/stop/target rules."""
        short = sweep.direction == "short"
        leg = self.candles[sweep.index : choch_index + 1]
        leg_high = max(c.high for c in leg)
        leg_low = min(c.low for c in leg)
        equilibrium = (leg_high + leg_low) / 2

        zone = self._find_zone(sweep.index, choch_index, short)
        if zone is None:
            self.rejections["no_poi"] += 1
            return None

        # Entry sits on the edge the price reaches first on its way back.
        entry = zone.bottom if short else zone.top

        if self.cfg.require_pd and ((entry <= equilibrium) if short else (entry >= equilibrium)):
            self.rejections["wrong_pd_zone"] += 1
            return None

        buffer = 1 + self.cfg.sl_buffer_pct / 100
        if short:
            stop_loss = max(sweep.extreme, zone.top) * buffer
        else:
            stop_loss = min(sweep.extreme, zone.bottom) * (2 - buffer)

        risk = (stop_loss - entry) if short else (entry - stop_loss)
        if risk <= 0:
            self.rejections["invalid_stop"] += 1
            return None

        close = self.candles[choch_index].close
        if (close >= entry) if short else (close <= entry):
            self.rejections["entry_already_passed"] += 1
            return None

        target = self._find_target(entry, risk, short)
        if target is None:
            self.rejections["rr_below_minimum"] += 1
            return None

        reward = (entry - target) if short else (target - entry)
        return Signal(
            direction=sweep.direction,
            entry=entry,
            stop_loss=stop_loss,
            take_profit=target,
            zone=zone,
            risk=risk,
            rr=reward / risk,
            signal_index=choch_index,
            sweep_index=sweep.index,
            sweep_level=sweep.level,
            choch_index=choch_index,
            equilibrium=equilibrium,
        )

    def _find_zone(self, start: int, end: int, short: bool) -> Zone | None:
        """The order block (last opposite-coloured candle before the impulse) and
        the FVG left behind by that impulse. Their overlap is the A+ zone; either
        one alone still counts."""
        ob = None
        for j in range(end, start - 1, -1):
            candle = self.candles[j]
            bullish = candle.close >= candle.open
            if bullish == short:  # last up-candle before a drop, or vice versa
                ob = Zone(top=candle.high, bottom=candle.low, source="OB")
                break

        kind = "bearish" if short else "bullish"
        in_leg = [f for f in self.confirmed_fvgs if f["kind"] == kind and start <= f["index"] <= end]
        fvg = None
        if in_leg:
            overlapping = [
                f for f in in_leg if ob is not None and f["bottom"] < ob.top and f["top"] > ob.bottom
            ]
            chosen = overlapping[-1] if overlapping else in_leg[-1]
            fvg = Zone(top=chosen["top"], bottom=chosen["bottom"], source="FVG")

        if ob is not None and fvg is not None and fvg.bottom < ob.top and fvg.top > ob.bottom:
            return Zone(top=min(ob.top, fvg.top), bottom=max(ob.bottom, fvg.bottom), source="OB+FVG")
        return ob or fvg

    def _find_target(self, entry: float, risk: float, short: bool) -> float | None:
        """Aim at resting liquidity, not at a round number. Walk the confirmed
        swings outward from the entry and take the first level that still pays
        `min_rr`; if none does, there is no trade."""
        kind = "low" if short else "high"
        levels = sorted(
            {
                s["price"]
                for s in self.confirmed_swings
                if s["kind"] == kind and ((s["price"] < entry) if short else (s["price"] > entry))
            },
            reverse=short,
        )
        needed = self.cfg.min_rr * risk
        for level in levels:
            if ((entry - level) if short else (level - entry)) >= needed:
                return level
        return None

    def last_swing(self, kind: str, before: int) -> float | None:
        """Most recent confirmed swing of `kind` — used for the trailing stop."""
        for s in reversed(self.confirmed_swings):
            if s["kind"] == kind and s["confirmed_at"] <= before:
                return s["price"]
        return None


# ============================================================== 7. backtest


@dataclass
class Money:
    """Defaults: a 100 USD account trading 0.01 lot where one point of price
    movement is worth 1 USD (gold-like).

        P/L = price_move / pip_size * pip_value * (lot / 0.01)
    """

    balance: float = 100.0
    lot: float = 0.01
    pip_value: float = 1.0  # USD per pip at `base_lot`
    pip_size: float = 1.0  # price units in one pip
    base_lot: float = 0.01
    cost: float = 0.0  # USD per round turn (spread + commission)

    @property
    def per_price_unit(self) -> float:
        """USD earned per 1.0 of price movement at the configured lot size."""
        return (self.lot / self.base_lot) * self.pip_value / self.pip_size


@dataclass
class RiskRules:
    daily_stop_pct: float = 3.0
    weekly_stop_pct: float = 6.0
    max_positions: int = 2
    max_consecutive_losses: int = 2
    max_risk_pct: float = 0.0  # 0 = disabled (fixed lot, no per-trade risk cap)


@dataclass
class Order:
    signal: Signal
    placed_index: int


@dataclass
class Trade:
    direction: str
    entry_index: int
    entry_price: float
    initial_stop: float
    stop_loss: float
    take_profit: float
    partial_level: float
    partial_enabled: bool
    risk_price: float
    risk_usd: float
    planned_rr: float
    zone_source: str
    remaining: float = 1.0
    realized: float = 0.0
    partial_done: bool = False
    exit_index: int | None = None
    exit_price: float | None = None
    exit_reason: str = ""

    @property
    def r_multiple(self) -> float:
        return self.realized / self.risk_usd if self.risk_usd else 0.0


class Backtester:
    """A simulated trader walking the chart one candle at a time."""

    def __init__(
        self,
        candles: list[Candle],
        signal_config: SignalConfig,
        money: Money,
        risk: RiskRules,
        partial_rr: float = 2.0,
        partial_pct: float = 50.0,
        trail: bool = True,
        order_expiry: int = 20,
    ):
        self.candles = candles
        self.money = money
        self.risk = risk
        self.partial_rr = partial_rr
        self.partial_fraction = partial_pct / 100
        self.trail = trail
        self.order_expiry = order_expiry

        self.engine = SignalEngine(candles, signal_config)
        self.balance = money.balance
        self.equity_curve: list[float] = [money.balance]
        self.trades: list[Trade] = []
        self.open_trades: list[Trade] = []
        self.orders: list[Order] = []
        self.blocked: dict[str, int] = {}
        self.orders_expired = 0

        self.dates = [parse_date(c.date) for c in candles]
        self.dates_ok = all(d is not None for d in self.dates)
        self._day: object = None
        self._week: object = None
        self._day_start_balance = self.balance
        self._week_start_balance = self.balance
        self._day_losses = 0
        self._day_locked = False
        self._week_locked = False

    # ------------------------------------------------------------------ run

    def run(self) -> None:
        for i, candle in enumerate(self.candles):
            self._roll_periods(i)
            for trade in list(self.open_trades):
                self._manage(trade, i, candle)
            self._fill_orders(i, candle)
            self._expire_orders(i)

            signal = self.engine.step(i)
            if signal is not None:
                self._place(signal, i)

        for trade in list(self.open_trades):
            self._close(trade, len(self.candles) - 1, self.candles[-1].close, "end_of_data", trade.remaining)

    # -------------------------------------------------------------- periods

    def _roll_periods(self, i: int) -> None:
        if not self.dates_ok:
            return
        date = self.dates[i]
        day = date.date()
        week = date.isocalendar()[:2]
        if day != self._day:
            self._day = day
            self._day_start_balance = self.balance
            self._day_losses = 0
            self._day_locked = False
        if week != self._week:
            self._week = week
            self._week_start_balance = self.balance
            self._week_locked = False

    def _check_locks(self) -> None:
        if not self.dates_ok:
            return
        day_loss = self._day_start_balance - self.balance
        if day_loss >= self._day_start_balance * self.risk.daily_stop_pct / 100:
            self._day_locked = True
        if self._day_losses >= self.risk.max_consecutive_losses:
            self._day_locked = True
        week_loss = self._week_start_balance - self.balance
        if week_loss >= self._week_start_balance * self.risk.weekly_stop_pct / 100:
            self._week_locked = True

    # --------------------------------------------------------------- orders

    def _place(self, signal: Signal, i: int) -> None:
        if self.balance <= 0:
            self._block("account_blown")
            return
        if self._day_locked:
            self._block("daily_stop_hit")
            return
        if self._week_locked:
            self._block("weekly_stop_hit")
            return
        if len(self.open_trades) + len(self.orders) >= self.risk.max_positions:
            self._block("max_positions")
            return
        risk_usd = signal.risk * self.money.per_price_unit
        if self.risk.max_risk_pct > 0 and risk_usd > self.balance * self.risk.max_risk_pct / 100:
            self._block("risk_per_trade_too_large")
            return
        self.orders.append(Order(signal=signal, placed_index=i))

    def _block(self, reason: str) -> None:
        self.blocked[reason] = self.blocked.get(reason, 0) + 1

    def _fill_orders(self, i: int, candle: Candle) -> None:
        for order in list(self.orders):
            if order.placed_index >= i:
                continue  # a signal found on this close cannot fill on it
            signal = order.signal
            touched = candle.high >= signal.entry if signal.direction == "short" else candle.low <= signal.entry
            if not touched:
                continue
            self.orders.remove(order)
            trade = self._open(signal, i)
            self._manage(trade, i, candle)

    def _expire_orders(self, i: int) -> None:
        for order in list(self.orders):
            if i - order.placed_index >= self.order_expiry:
                self.orders.remove(order)
                self.orders_expired += 1

    def _open(self, signal: Signal, i: int) -> Trade:
        short = signal.direction == "short"
        offset = self.partial_rr * signal.risk
        trade = Trade(
            direction=signal.direction,
            entry_index=i,
            entry_price=signal.entry,
            initial_stop=signal.stop_loss,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            partial_level=signal.entry - offset if short else signal.entry + offset,
            # A partial sitting beyond the target would never be worth taking.
            partial_enabled=self.partial_fraction > 0 and offset < signal.rr * signal.risk,
            risk_price=signal.risk,
            risk_usd=signal.risk * self.money.per_price_unit,
            planned_rr=signal.rr,
            zone_source=signal.zone.source,
        )
        trade.realized -= self.money.cost
        self.balance -= self.money.cost
        self.open_trades.append(trade)
        return trade

    # ------------------------------------------------------------- managing

    def _manage(self, trade: Trade, i: int, candle: Candle) -> None:
        short = trade.direction == "short"

        # Stop first: when one candle spans both the stop and the target, assume
        # the loss. Optimism here is what makes a backtest lie.
        if (candle.high >= trade.stop_loss) if short else (candle.low <= trade.stop_loss):
            reason = "breakeven" if trade.partial_done and trade.stop_loss == trade.entry_price else "stop_loss"
            self._close(trade, i, trade.stop_loss, reason, trade.remaining)
            return

        if not trade.partial_done and trade.partial_enabled:
            hit = (candle.low <= trade.partial_level) if short else (candle.high >= trade.partial_level)
            if hit:
                self._close(trade, i, trade.partial_level, "partial", self.partial_fraction, final=False)
                trade.partial_done = True
                trade.stop_loss = trade.entry_price  # move to breakeven

        if (candle.low <= trade.take_profit) if short else (candle.high >= trade.take_profit):
            self._close(trade, i, trade.take_profit, "take_profit", trade.remaining)
            return

        if self.trail and trade.partial_done:
            level = self.engine.last_swing("high" if short else "low", before=i)
            if level is not None:
                if short and level < trade.stop_loss:
                    trade.stop_loss = level
                elif not short and level > trade.stop_loss:
                    trade.stop_loss = level

    def _close(self, trade: Trade, i: int, price: float, reason: str, fraction: float, final: bool = True) -> None:
        move = (trade.entry_price - price) if trade.direction == "short" else (price - trade.entry_price)
        pnl = move * self.money.per_price_unit * fraction
        trade.realized += pnl
        trade.remaining -= fraction
        self.balance += pnl

        if not final:
            return

        trade.exit_index = i
        trade.exit_price = price
        trade.exit_reason = reason
        self.open_trades.remove(trade)
        self.trades.append(trade)
        self.equity_curve.append(self.balance)
        self._day_losses = self._day_losses + 1 if trade.realized < 0 else 0
        self._check_locks()


def summarize(bt: Backtester) -> dict:
    trades = bt.trades
    wins = [t for t in trades if t.realized > 0]
    losses = [t for t in trades if t.realized < 0]
    gross_win = sum(t.realized for t in wins)
    gross_loss = -sum(t.realized for t in losses)
    net = sum(t.realized for t in trades)

    peak = bt.equity_curve[0]
    max_dd = max_dd_pct = 0.0
    for value in bt.equity_curve:
        peak = max(peak, value)
        drawdown = peak - value
        if drawdown > max_dd:
            max_dd = drawdown
            max_dd_pct = drawdown / peak * 100 if peak else 0.0

    best_streak = worst_streak = run = 0
    for t in trades:
        won = t.realized > 0
        run = run + 1 if run > 0 and won else (run - 1 if run < 0 and not won else (1 if won else -1))
        best_streak = max(best_streak, run)
        worst_streak = min(worst_streak, run)

    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0.0,
        "net": net,
        "gross_win": gross_win,
        "gross_loss": gross_loss,
        "profit_factor": gross_win / gross_loss if gross_loss else float("inf") if gross_win else 0.0,
        "expectancy": net / len(trades) if trades else 0.0,
        "expectancy_r": sum(t.r_multiple for t in trades) / len(trades) if trades else 0.0,
        "avg_win": gross_win / len(wins) if wins else 0.0,
        "avg_loss": -gross_loss / len(losses) if losses else 0.0,
        "best": max((t.realized for t in trades), default=0.0),
        "worst": min((t.realized for t in trades), default=0.0),
        "max_dd": max_dd,
        "max_dd_pct": max_dd_pct,
        "best_streak": best_streak,
        "worst_streak": -worst_streak,
        "avg_risk": sum(t.risk_usd for t in trades) / len(trades) if trades else 0.0,
        "max_risk": max((t.risk_usd for t in trades), default=0.0),
        "avg_planned_rr": sum(t.planned_rr for t in trades) / len(trades) if trades else 0.0,
    }


def print_report(bt: Backtester, stats: dict, money: Money) -> None:
    print("=== Money model ===")
    print(f"Starting balance : ${money.balance:,.2f}")
    print(f"Lot / point value: {money.lot} lot  =  ${money.per_price_unit:,.2f} per 1.0 of price")
    print(f"Cost per trade   : ${money.cost:,.2f}")
    if not bt.dates_ok:
        print("Note             : dates unparsable — daily/weekly loss limits are OFF")

    print("\n=== Results ===")
    print(f"Trades           : {stats['trades']}  ({stats['wins']}W / {stats['losses']}L)")
    print(f"Win rate         : {stats['win_rate']:.1f}%")
    print(f"Net P/L          : ${stats['net']:,.2f}   ({stats['net'] / money.balance * 100:+.1f}%)")
    print(f"Final balance    : ${bt.balance:,.2f}")
    print(f"Profit factor    : {stats['profit_factor']:.2f}")
    print(f"Expectancy       : ${stats['expectancy']:,.2f} per trade  ({stats['expectancy_r']:+.2f}R)")
    print(f"Avg win / loss   : ${stats['avg_win']:,.2f} / ${stats['avg_loss']:,.2f}")
    print(f"Best / worst     : ${stats['best']:,.2f} / ${stats['worst']:,.2f}")
    print(f"Max drawdown     : ${stats['max_dd']:,.2f}  ({stats['max_dd_pct']:.1f}%)")
    print(f"Longest streak   : {stats['best_streak']}W / {stats['worst_streak']}L")

    print("\n=== Risk reality check ===")
    print(f"Avg risk / trade : ${stats['avg_risk']:,.2f}  ({stats['avg_risk'] / money.balance * 100:.1f}% of starting balance)")
    print(f"Max risk / trade : ${stats['max_risk']:,.2f}  ({stats['max_risk'] / money.balance * 100:.1f}% of starting balance)")
    print(f"Avg planned RR   : 1:{stats['avg_planned_rr']:.1f}")
    if stats["avg_risk"] > money.balance * 0.02:
        print("WARNING          : fixed lot risks well over the 1% rule in STRATEGY.md.")
        print("                   The lot is too big for this balance, or the stops are too wide.")

    print("\n=== Funnel (why setups did not become trades) ===")
    print(f"Liquidity sweeps detected : {bt.engine.sweeps_seen}")
    for reason, count in bt.engine.rejections.most_common():
        print(f"  rejected: {reason:<24} {count}")
    for reason, count in sorted(bt.blocked.items(), key=lambda kv: -kv[1]):
        print(f"  blocked by risk rules: {reason:<11} {count}")
    if bt.orders_expired:
        print(f"  limit orders never filled : {bt.orders_expired}")

    if bt.trades:
        by_exit: dict[str, int] = {}
        for t in bt.trades:
            by_exit[t.exit_reason] = by_exit.get(t.exit_reason, 0) + 1
        print("\n=== Exits ===")
        for reason, count in sorted(by_exit.items(), key=lambda kv: -kv[1]):
            print(f"  {reason:<16} {count}")


def export_trades(bt: Backtester, path: str) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "entry_date", "exit_date", "direction", "zone", "entry", "stop", "target",
                "exit_price", "exit_reason", "partial_taken", "planned_rr", "risk_usd",
                "pnl_usd", "r_multiple",
            ]
        )
        for t in bt.trades:
            writer.writerow(
                [
                    bt.candles[t.entry_index].date,
                    bt.candles[t.exit_index].date if t.exit_index is not None else "",
                    t.direction, t.zone_source,
                    f"{t.entry_price:.5f}", f"{t.initial_stop:.5f}", f"{t.take_profit:.5f}",
                    f"{t.exit_price:.5f}" if t.exit_price is not None else "",
                    t.exit_reason, "yes" if t.partial_done else "no",
                    f"{t.planned_rr:.2f}", f"{t.risk_usd:.2f}",
                    f"{t.realized:.2f}", f"{t.r_multiple:.2f}",
                ]
            )


# ============================================================== 8. cli


def cmd_trend(args: argparse.Namespace) -> None:
    candles, _ = get_candles(args)
    result = identify_trend(candles, args.ema, args.adx_period, args.swing)
    print(f"Direction : {result['direction']}")
    if result["adx"] is not None:
        print(f"Strength  : {result['strength']} (ADX={result['adx']:.2f})")
    else:
        print(f"Strength  : {result['strength']}")
    print(f"Price vs EMA{result['ema_period']}: {result['price_vs_ema']} (close={result['last_close']:.5f}, ema={result['last_ema']:.5f})")
    print(f"Swing structure: {result['structure']}")


def cmd_scan(args: argparse.Namespace) -> None:
    candles, _ = get_candles(args)
    setup = find_latest_setup(candles, args.swing, args.tolerance, args.impulse)
    if setup is None:
        print("No CHoCH detected yet — not enough structure to evaluate a setup.")
        return
    print(f"Direction        : {setup['direction'].upper()} (CHoCH at candle #{setup['choch_index']})")
    print(f"Liquidity sweep  : {setup['sweep']}")
    print(f"Order block      : {setup['order_block']}")
    print(f"FVG              : {setup['fvg']}")
    print(f"Equilibrium (50%): {setup['equilibrium']:.5f}")
    print(f"In {setup['premium_discount_zone']} zone   : {setup['in_premium_or_discount']}")
    print(f"In order block   : {setup['in_order_block']}")
    print(f"In FVG           : {setup['in_fvg']}")
    print(f"Entry ready      : {setup['entry_ready']}")
    print(f"Last close       : {setup['last_close']:.5f}")
    print(f"Suggested SL     : {setup['stop_loss']:.5f}")
    print(f"Suggested TP     : {setup['take_profit']:.5f}")


def resolve_money(args: argparse.Namespace, spec: dict) -> Money:
    """Price a point of movement. The broker's own tick value beats a guess, so
    MT5 specs fill in whatever the command line left unset."""
    pip_size, pip_value = args.pip_size, args.pip_value
    if spec and spec.get("tick_size") and spec.get("tick_value"):
        if pip_size is None:
            pip_size = spec["tick_size"]
        if pip_value is None:
            pip_value = spec["tick_value"] * Money.base_lot  # tick_value is per 1.00 lot
        print(
            f"Contract specs from MT5: 1 tick = {spec['tick_size']} of price "
            f"= ${spec['tick_value']:,.2f} per 1.00 lot\n"
        )
    return Money(
        balance=args.balance,
        lot=args.lot,
        pip_value=1.0 if pip_value is None else pip_value,
        pip_size=1.0 if pip_size is None else pip_size,
        cost=args.cost,
    )


def cmd_fetch(args: argparse.Namespace) -> None:
    candles, spec = load_from_mt5(args.symbol, args.tf, args.bars)
    with open(args.out, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "open", "high", "low", "close"])
        for c in candles:
            writer.writerow([c.date, c.open, c.high, c.low, c.close])
    print(f"Wrote {len(candles)} closed {args.tf.upper()} candles for {args.symbol} to {args.out}")
    print(f"Range: {candles[0].date}  ->  {candles[-1].date}")
    if spec:
        print(f"Contract specs: digits={spec['digits']}  tick_size={spec['tick_size']}  tick_value={spec['tick_value']}")


def cmd_backtest(args: argparse.Namespace) -> None:
    candles, spec = get_candles(args)
    signal_config = SignalConfig(
        swing_window=args.swing,
        tolerance=args.tolerance,
        htf_mult=args.htf_mult,
        htf_ema=args.htf_ema,
        min_adx=args.min_adx,
        require_bias=not args.no_bias_filter,
        confirm=args.confirm,
        choch_window=args.choch_window,
        min_rr=args.min_rr,
        sl_buffer_pct=args.sl_buffer,
        require_pd=not args.no_pd_filter,
    )
    money = resolve_money(args, spec)
    risk = RiskRules(
        daily_stop_pct=args.daily_stop,
        weekly_stop_pct=args.weekly_stop,
        max_positions=args.max_positions,
        max_consecutive_losses=args.max_consec_losses,
        max_risk_pct=args.max_risk_pct,
    )
    bt = Backtester(
        candles, signal_config, money, risk,
        partial_rr=args.partial_rr,
        partial_pct=args.partial_pct,
        trail=not args.no_trail,
        order_expiry=args.order_expiry,
    )
    bt.run()
    print_report(bt, summarize(bt), money)

    if args.export:
        export_trades(bt, args.export)
        print(f"\nTrade list written to {args.export}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="smc",
        description="Smart Money Concepts: trend, live setup scan, and backtest.",
        epilog=(
            "The short version:\n"
            "  python smc.py XAUUSD          backtest MT5's own M15 candles\n"
            "  python smc.py data.csv        the same, from a CSV file\n"
            "Naming a command (trend / scan / backtest / fetch) is optional;\n"
            "without one it backtests."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"smc {VERSION}")
    sub = p.add_subparsers(dest="command", required=True)
    fmt = argparse.ArgumentDefaultsHelpFormatter

    def add_source(parser: argparse.ArgumentParser) -> None:
        """Candles come from a CSV file or straight from a running MT5 terminal."""
        parser.add_argument("csv_path", nargs="?", metavar="SYMBOL_OR_CSV",
                            help="An MT5 symbol (XAUUSD) or a CSV file (data.csv)")
        parser.add_argument("--mt5", metavar="SYMBOL", help="Same thing, spelled out explicitly")
        parser.add_argument("--tf", default="M15", help="MT5 timeframe: M1 M5 M15 M30 H1 H4 D1 ...")
        parser.add_argument("--bars", type=int, default=5000, help="How many closed candles to pull from MT5")

    t = sub.add_parser("trend", help="Trend direction and strength", formatter_class=fmt)
    add_source(t)
    t.add_argument("--ema", type=int, default=50, help="EMA period")
    t.add_argument("--adx-period", type=int, default=14, help="ADX period")
    t.add_argument("--swing", type=int, default=5, help="Swing detection window")
    t.set_defaults(func=cmd_trend)

    s = sub.add_parser("scan", help="The SMC setup on the chart right now", formatter_class=fmt)
    add_source(s)
    s.add_argument("--swing", type=int, default=3, help="Swing detection window")
    s.add_argument("--tolerance", type=float, default=0.0015, help="Equal highs/lows tolerance")
    s.add_argument("--impulse", type=float, default=1.8, help="Impulse candle size multiplier")
    s.set_defaults(func=cmd_scan)

    b = sub.add_parser("backtest", help="Walk the rules bar by bar over history", formatter_class=fmt)
    add_source(b)

    money = b.add_argument_group("account")
    money.add_argument("--balance", type=float, default=100.0, help="Starting balance in USD")
    money.add_argument("--lot", type=float, default=0.01, help="Fixed lot size per trade")
    money.add_argument("--pip-value", type=float, help="USD per pip at 0.01 lot (default: MT5 specs, else 1.0)")
    money.add_argument("--pip-size", type=float, help="Price units in one pip (default: MT5 specs, else 1.0)")
    money.add_argument("--cost", type=float, default=0.0, help="USD per round turn (spread + commission)")

    rules = b.add_argument_group("strategy")
    rules.add_argument("--swing", type=int, default=3, help="Entry-timeframe swing window")
    rules.add_argument("--tolerance", type=float, default=0.0015, help="Equal highs/lows tolerance")
    rules.add_argument("--htf-mult", type=int, default=16, help="Entry bars per HTF bar (15m x16 = 4H)")
    rules.add_argument("--htf-ema", type=int, default=50, help="HTF EMA agreement filter (0 = off)")
    rules.add_argument("--min-adx", type=float, default=0.0, help="Minimum HTF ADX (0 = off)")
    rules.add_argument("--no-bias-filter", action="store_true", help="Trade both directions regardless of HTF")
    rules.add_argument("--confirm", choices=("choch", "any"), default="choch", help="Confirmation event required")
    rules.add_argument("--choch-window", type=int, default=12, help="Bars a sweep waits for confirmation")
    rules.add_argument("--min-rr", type=float, default=3.0, help="Reject setups paying less than this")
    rules.add_argument("--sl-buffer", type=float, default=0.1, help="Stop padding beyond the sweep wick, in percent")
    rules.add_argument("--no-pd-filter", action="store_true", help="Skip the premium/discount check")

    mgmt = b.add_argument_group("management")
    mgmt.add_argument("--partial-rr", type=float, default=2.0, help="R multiple at which to take partials")
    mgmt.add_argument("--partial-pct", type=float, default=50.0, help="Percent closed at the partial (0 = off)")
    mgmt.add_argument("--no-trail", action="store_true", help="Do not trail the stop after the partial")
    mgmt.add_argument("--order-expiry", type=int, default=20, help="Bars a limit order stays live")

    risk = b.add_argument_group("risk")
    risk.add_argument("--daily-stop", type=float, default=3.0, help="Daily loss limit, percent")
    risk.add_argument("--weekly-stop", type=float, default=6.0, help="Weekly loss limit, percent")
    risk.add_argument("--max-positions", type=int, default=2, help="Concurrent trades and resting orders")
    risk.add_argument("--max-consec-losses", type=int, default=2, help="Consecutive losses that end the day")
    risk.add_argument("--max-risk-pct", type=float, default=0.0, help="Skip trades risking more than this percent (0 = off)")

    b.add_argument("--export", help="Write the trade list to this CSV path")
    b.set_defaults(func=cmd_backtest)

    f = sub.add_parser("fetch", help="Save MT5 candles to a CSV file", formatter_class=fmt)
    f.add_argument("symbol", help="MT5 symbol exactly as it appears in Market Watch, e.g. XAUUSD")
    f.add_argument("--tf", default="M15", help="MT5 timeframe: M1 M5 M15 M30 H1 H4 D1 ...")
    f.add_argument("--bars", type=int, default=5000, help="How many closed candles to pull")
    f.add_argument("--out", default="data.csv", help="Where to write the CSV")
    f.set_defaults(func=cmd_fetch)

    return p


COMMANDS = ("trend", "scan", "backtest", "fetch")


def normalize_argv(argv: list[str]) -> list[str]:
    """`smc.py XAUUSD` means `smc.py backtest XAUUSD`. The thing people want most
    should be the thing they type least."""
    if not argv or argv[0] in COMMANDS or argv[0].startswith("-"):
        return argv
    return ["backtest", *argv]


def main() -> None:
    args = build_parser().parse_args(normalize_argv(sys.argv[1:]))
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except (FileNotFoundError, ValueError, KeyError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
