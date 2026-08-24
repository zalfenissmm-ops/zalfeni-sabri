#!/usr/bin/env python3
"""
ICT Gold Bot — XAU/USD strategy: live MetaTrader 5 data, the ICT rule set,
and a 30-day backtest on a $1,000 account trading a fixed 0.01 lot.

Everything lives in this one file, in three stages:

  1. DATA      Real candles pulled straight from a running MetaTrader 5
               terminal (D1 / H4 / H1 / M5), or replayed from CSVs that an
               earlier `export` run saved.
  2. STRATEGY  The trading plan from the ICT document, encoded step by step:
               daily bias -> liquidity target -> kill zone -> liquidity sweep
               -> CHoCH + displacement -> OB/FVG inside OTE -> SL/TP -> trade
               management.
  3. BACKTEST  A walk-forward, bar-by-bar replay of the last 30 days with no
               look-ahead: every decision is taken on a closed M5 bar and can
               only be executed by later bars.

Account model: 0.01 lot of XAU/USD = 1 troy ounce, so a $1.00 move in gold is
$1.00 of P/L — the "point value = 1 dollar" the account is sized around.

Note on the document's step 10 (trade journal): it is deliberately not
implemented. Journalling is a manual record-keeping habit, not an executable
market rule; the backtest's trade table below is its automated equivalent.

Usage
    python3 ict_gold_bot.py backtest                      # live MT5 data, 30 days
    python3 ict_gold_bot.py backtest --days 30 --verbose
    python3 ict_gold_bot.py export --csv-dir data         # save MT5 candles to CSV
    python3 ict_gold_bot.py backtest --source csv --csv-dir data
    python3 ict_gold_bot.py scan                          # setup on live data right now

Live data needs the MetaTrader5 package and a running terminal
(`pip install MetaTrader5`; Windows, or Linux under Wine). The CSV source
needs nothing but the standard library.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

# ===========================================================================
# 1. CONFIGURATION
# ===========================================================================

# Bumped whenever behaviour changes, and printed on every run: the fastest way
# to tell whether the file in front of you is the one being talked about.
VERSION = "1.5"

TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}

# Kill zones from the document, in London local time (hours, fractional).
KILL_ZONES = {
    "asia": (0.0, 7.0),           # accumulation range, reference only
    "london": (7.0, 10.0),        # London open — Judas swing window
    "newyork": (12.0, 15.0),      # NY overlap — highest liquidity
    "london_close": (15.0, 17.0),
}

# How much history each timeframe needs *before* the backtest window starts,
# so that structure, swings and ATR are already warmed up on day one.
WARMUP_DAYS = {"D1": 240, "H4": 90, "H1": 45, "M15": 12, "M5": 7}


@dataclass
class Config:
    # --- instrument & account -------------------------------------------
    symbol: str = "XAUUSD"
    balance: float = 1000.0
    lot: float = 0.01
    contract_size: float = 100.0     # 1.00 lot = 100 oz -> 0.01 lot = $1 per $1 move
    spread: float = 0.20             # price units, charged once per round turn
    auto_spread: bool = True         # replace it with the broker's live spread
    commission_per_lot: float = 0.0  # $ per lot, round turn

    # --- backtest window --------------------------------------------------
    days: int = 30

    # --- timeframe ladder (document §3, top-down analysis) ----------------
    bias_tf: str = "D1"              # long-term bias + dealing range
    mid_tf: str = "H4"               # mid-term bias alignment
    poi_tf: str = "H1"               # liquidity pools used as targets
    entry_tf: str = "M5"             # sweep / CHoCH / entry timing
    pd_tf: str = "H4"                # range whose 50% splits premium/discount
    use_mid_filter: bool = True
    use_pd_filter: bool = True
    bias_mode: str = "htf"           # "htf": only the higher-timeframe direction
                                     # "both": hunt sweeps on both sides of the book

    # --- risk & capital (document §6) -------------------------------------
    risk_pct: float = 1.0            # max % of balance a single trade may risk
    min_rr: float = 2.0              # never enter below 1:2
    max_consecutive_losses: int = 2  # stop for the day after 2 losses in a row
    max_daily_loss_pct: float = 3.0  # daily loss ceiling

    # --- ICT parameters ---------------------------------------------------
    swing_window_htf: int = 2        # fractal window on D1/H4/H1
    swing_window_entry: int = 2      # fractal window on the entry timeframe
    atr_period: int = 14
    equal_level_atr: float = 0.15    # equal-highs/lows tolerance, in ATR
    displacement_atr: float = 1.2    # body size that counts as displacement
    sweep_lookback: int = 72         # entry-TF bars searched for a sweep (6h on M5)
    choch_max_bars: int = 24         # CHoCH must follow the sweep within N bars
    min_leg_atr: float = 1.5         # impulse leg must be at least this big
    ote_low: float = 0.618           # OTE band, document §2.6
    ote_high: float = 0.79
    require_poi: bool = True         # entry must be an OB/FVG inside the OTE band
    sl_mode: str = "structure"       # "structure": behind the sweep/block (document)
                                     # "atr": a fixed multiple of ATR
    sl_atr: float = 1.0              # stop distance when sl_mode is "atr"
    tp_atr: float = 1.0              # target distance when tp_mode is "atr"
    sl_buffer_atr: float = 0.5       # safety margin behind the sweep / block
    min_sl_atr: float = 1.0          # a stop tighter than 1 ATR is spread noise
    min_sl_distance: float = 1.00    # $, absolute floor under the stop distance
    max_sweep_candidates: int = 6    # how many recent sweeps to test for a CHoCH
    tp_mode: str = "nearest"         # "nearest" (per document) or "first-valid"

    # --- orders & management (document §5, steps 7 and 9) ------------------
    order_expiry_bars: int = 24      # a limit order lives 2 hours on M5
    max_open: int = 1                # positions + resting orders allowed at once
    max_hold_bars: int = 288         # flatten after one trading day (0 = off)
    breakeven_r: float = 1.0         # move SL to entry at +1R (0 = off)
    partial_r: float = 0.0           # partial profit at +xR (0 = off)
    partial_fraction: float = 0.5

    # --- sessions & news --------------------------------------------------
    sessions: tuple[str, ...] = ("london", "newyork")
    blackouts: list[tuple[datetime, datetime]] = field(default_factory=list)

    # --- data feed --------------------------------------------------------
    source: str = "mt5"
    csv_dir: str = "data"
    broker_utc_offset: float | None = None   # None = auto-detect from the terminal
    mt5_login: int | None = None
    mt5_password: str | None = None
    mt5_server: str | None = None
    mt5_path: str | None = None

    @property
    def value_per_unit(self) -> float:
        """Account currency gained per 1.00 of gold price, at the configured lot."""
        return self.lot * self.contract_size

    @property
    def timeframes(self) -> list[str]:
        tfs = [self.bias_tf, self.poi_tf, self.entry_tf]
        if self.use_pd_filter:
            tfs.append(self.pd_tf)
        if self.use_mid_filter:
            tfs.insert(1, self.mid_tf)
        seen, ordered = set(), []
        for tf in tfs:
            if tf not in seen:
                seen.add(tf)
                ordered.append(tf)
        return ordered


# ===========================================================================
# 2. TIME HELPERS  (kill zones are London local time — document §4)
# ===========================================================================


def _last_sunday(year: int, month: int) -> datetime:
    """00:00 UTC of the last Sunday in the given month."""
    day = 31
    while True:
        try:
            d = datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            day -= 1
            continue
        return d - timedelta(days=(d.weekday() + 1) % 7)


def is_bst(dt_utc: datetime) -> bool:
    """British Summer Time: 01:00 UTC last Sunday of March -> 01:00 UTC last
    Sunday of October. Computed here so the file needs no tz database."""
    start = _last_sunday(dt_utc.year, 3) + timedelta(hours=1)
    end = _last_sunday(dt_utc.year, 10) + timedelta(hours=1)
    return start <= dt_utc < end


def to_london(dt_utc: datetime) -> datetime:
    return dt_utc + timedelta(hours=1 if is_bst(dt_utc) else 0)


def london_hour(dt_utc: datetime) -> float:
    lon = to_london(dt_utc)
    return lon.hour + lon.minute / 60.0


def london_day(dt_utc: datetime) -> datetime:
    """Midnight London (as a UTC instant) of the day this timestamp falls in."""
    lon = to_london(dt_utc)
    midnight = lon.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(hours=1 if is_bst(dt_utc) else 0)


def active_session(dt_utc: datetime, names: tuple[str, ...]) -> str | None:
    h = london_hour(dt_utc)
    for name in names:
        lo, hi = KILL_ZONES[name]
        if lo <= h < hi:
            return name
    return None


def in_blackout(dt_utc: datetime, blackouts: list[tuple[datetime, datetime]]) -> bool:
    return any(start <= dt_utc < end for start, end in blackouts)


# ===========================================================================
# 3. DATA LAYER — real candles from MetaTrader 5, or from exported CSV
# ===========================================================================


@dataclass(slots=True)
class Candle:
    time: datetime      # UTC, bar OPEN time
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def bullish(self) -> bool:
        return self.close >= self.open


class MT5Feed:
    """Live feed. Talks to a running MetaTrader 5 terminal through the official
    MetaTrader5 python package and returns candles stamped in real UTC."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.mt5 = None
        self.offset_hours = cfg.broker_utc_offset or 0.0
        self.symbol = cfg.symbol

    def __enter__(self) -> "MT5Feed":
        try:
            import MetaTrader5 as mt5
        except ImportError:
            raise SystemExit(
                "MetaTrader5 package not installed.\n"
                "  Windows / Wine:  pip install MetaTrader5   (needs a running MT5 terminal)\n"
                "  Anywhere else :  export the candles once on a machine that has MT5\n"
                "                   (python3 ict_gold_bot.py export --csv-dir data)\n"
                "                   then run with --source csv --csv-dir data"
            )
        self.mt5 = mt5

        kwargs = {}
        if self.cfg.mt5_path:
            kwargs["path"] = self.cfg.mt5_path
        if self.cfg.mt5_login:
            kwargs.update(login=int(self.cfg.mt5_login),
                          password=self.cfg.mt5_password or "",
                          server=self.cfg.mt5_server or "")
        if not mt5.initialize(**kwargs):
            raise SystemExit(f"MT5 initialize() failed: {mt5.last_error()}")

        self.symbol = self._resolve_symbol()
        if not mt5.symbol_select(self.symbol, True):
            raise SystemExit(f"Cannot select symbol {self.symbol}: {mt5.last_error()}")

        if self.cfg.broker_utc_offset is None:
            self.offset_hours = self._detect_offset()
            tick = mt5.symbol_info_tick(self.symbol)
            server = datetime.fromtimestamp(tick.time, tz=timezone.utc) if tick else None
            print(f"[mt5] broker clock detected at UTC{self.offset_hours:+g}"
                  + (f"  (terminal shows {server:%Y-%m-%d %H:%M}, "
                     f"real UTC is {datetime.now(timezone.utc):%Y-%m-%d %H:%M})"
                     if server else ""))
            print("[mt5] if that clock does not match your terminal's Market Watch, "
                  "the kill zones will be shifted -- pass --broker-utc-offset <hours>")
        else:
            self.offset_hours = float(self.cfg.broker_utc_offset)

        info = mt5.symbol_info(self.symbol)
        if info is not None:
            print(f"[mt5] {self.symbol}: contract={info.trade_contract_size:g} "
                  f"min_lot={info.volume_min:g} spread={info.spread} points, "
                  f"digits={info.digits}")
            if self.cfg.contract_size != info.trade_contract_size:
                print(f"[mt5] note: --contract-size is {self.cfg.contract_size:g} but this "
                      f"broker uses {info.trade_contract_size:g}; the P/L per $1 of gold "
                      "follows the value you passed.")
            # the broker's own spread beats a hard-coded guess
            live_spread = info.spread * info.point
            if self.cfg.auto_spread and live_spread > 0:
                self.cfg.spread = live_spread
                print(f"[mt5] spread taken from the broker: ${live_spread:.3f} per round turn "
                      "(override with --spread)")
        return self

    def __exit__(self, *exc) -> None:
        if self.mt5 is not None:
            self.mt5.shutdown()

    def _resolve_symbol(self) -> str:
        """Brokers name gold differently (XAUUSD, XAUUSD.m, GOLD...). Take the
        configured name if it exists, otherwise show what is available."""
        mt5 = self.mt5
        if mt5.symbol_info(self.cfg.symbol) is not None:
            return self.cfg.symbol
        candidates = [s.name for s in (mt5.symbols_get() or [])
                      if "XAU" in s.name.upper() or "GOLD" in s.name.upper()]
        if len(candidates) == 1:
            print(f"[mt5] '{self.cfg.symbol}' not found, using '{candidates[0]}'")
            return candidates[0]
        raise SystemExit(
            f"Symbol '{self.cfg.symbol}' not found on this broker.\n"
            f"Gold-like symbols available: {', '.join(candidates) or '(none)'}\n"
            "Re-run with --symbol <name>."
        )

    def _detect_offset(self) -> float:
        """MT5 stamps candles in broker server time. Compare the latest tick's
        clock with real UTC to learn the offset (whole hours in practice --
        gold brokers run EET/EEST, so +2 in winter and +3 in summer).

        The tick only carries this information while it is fresh: over a
        weekend or a market break it is the last price of the session, and the
        gap it shows is age, not time zone. A residual far from a whole hour is
        exactly that case, so it is reported instead of silently believed."""
        tick = self.mt5.symbol_info_tick(self.symbol)
        if tick is None or not tick.time:
            print("[mt5] no tick to read the server clock from -- assuming UTC+0; "
                  "pass --broker-utc-offset if that is wrong")
            return 0.0
        server_now = datetime.fromtimestamp(tick.time, tz=timezone.utc)
        delta_h = (server_now - datetime.now(timezone.utc)).total_seconds() / 3600.0
        offset = float(round(delta_h))
        if not -12 <= offset <= 14:
            # no time zone is that far out: the tick is old, not exotic
            print(f"[mt5] WARNING: the last tick is {abs(delta_h):.0f} hours from now, so the "
                  "market is closed and its clock says nothing about the time zone. "
                  "Assuming UTC+0 -- pass --broker-utc-offset <hours> to set it "
                  "(gold brokers are usually +2 in winter, +3 in summer).")
            return 0.0
        drift_minutes = abs(delta_h - offset) * 60
        if drift_minutes > 15:
            print(f"[mt5] WARNING: the last tick is {drift_minutes:.0f} minutes off a whole "
                  "hour, so it may be stale. The detected offset "
                  f"UTC{offset:+g} may be wrong -- check your terminal's clock and pass "
                  "--broker-utc-offset if it disagrees.")
        return offset

    def _prime_history(self, timeframe: str, start_utc: datetime) -> None:
        """MT5 only hands over candles the terminal has already downloaded --
        which is why history normally means opening a chart and scrolling back.
        Asking for bars at progressively older dates makes the terminal request
        them from the server, so the download happens by itself."""
        mt5 = self.mt5
        tf_const = getattr(mt5, f"TIMEFRAME_{timeframe}")
        off = timedelta(hours=self.offset_hours)
        target = start_utc + off
        for attempt in range(8):
            rates = mt5.copy_rates_from(self.symbol, tf_const, target, 10)
            if rates is not None and len(rates) > 0:
                oldest = datetime.fromtimestamp(int(rates[0]["time"]), tz=timezone.utc)
                if oldest <= target + timedelta(days=2):
                    return                      # the terminal reaches far enough back
            if attempt == 0:
                print(f"[mt5] {timeframe}: asking the terminal to download history "
                      f"back to {start_utc:%Y-%m-%d}...")
            # each round trip pulls another block; give the terminal time to store it
            mt5.copy_rates_from(self.symbol, tf_const, target, 20000)
            time.sleep(1.0)

    def fetch(self, timeframe: str, start_utc: datetime, end_utc: datetime) -> list[Candle]:
        mt5 = self.mt5
        tf_const = getattr(mt5, f"TIMEFRAME_{timeframe}")
        off = timedelta(hours=self.offset_hours)
        self._prime_history(timeframe, start_utc)
        rates = mt5.copy_rates_range(self.symbol, tf_const, start_utc + off, end_utc + off)
        if rates is None or len(rates) == 0:
            raise SystemExit(
                f"MT5 returned no {timeframe} data for {self.symbol} "
                f"({start_utc:%Y-%m-%d} -> {end_utc:%Y-%m-%d}): {mt5.last_error()}\n"
                "Open the symbol's chart on that timeframe once so the terminal "
                "downloads its history, then retry."
            )
        candles = [
            Candle(
                time=datetime.fromtimestamp(int(r["time"]), tz=timezone.utc) - off,
                open=float(r["open"]), high=float(r["high"]),
                low=float(r["low"]), close=float(r["close"]),
                volume=float(r["tick_volume"]),
            )
            for r in rates
        ]
        print(f"[mt5] {timeframe:>3}: {len(candles):>6} candles  "
              f"{candles[0].time:%Y-%m-%d %H:%M} -> {candles[-1].time:%Y-%m-%d %H:%M} UTC")
        short_by = (candles[0].time - start_utc).days
        if short_by > 1:
            print(f"[mt5] note: {timeframe} history starts {short_by} days later than asked "
                  f"({start_utc:%Y-%m-%d}) -- this broker does not serve more than that.")
        return candles


    # how far back one request should reach per timeframe, sized so each chunk
    # comes back with a few thousand bars rather than hitting the terminal's cap
    CHUNK_DAYS = {"M1": 5, "M5": 25, "M15": 75, "M30": 150,
                  "H1": 300, "H4": 1200, "D1": 7000}

    def fetch_max(self, timeframe: str, max_days: int = 7300,
                  known: list[Candle] | None = None) -> list[Candle]:
        """Pull as much history as this broker will serve, walking backwards in
        chunks until requests stop returning anything new. Each request also
        makes the terminal download that stretch, so the archive deepens as the
        walk goes on."""
        mt5 = self.mt5
        tf_const = getattr(mt5, f"TIMEFRAME_{timeframe}")
        off = timedelta(hours=self.offset_hours)
        span = timedelta(days=self.CHUNK_DAYS.get(timeframe, 100))
        floor = datetime.now(timezone.utc) - timedelta(days=max_days)

        collected: dict[int, tuple] = {}
        if known:                                   # keep what an earlier run saved
            for c in known:
                collected[int(c.time.timestamp())] = (c.open, c.high, c.low, c.close, c.volume)

        cursor = datetime.now(timezone.utc) + timedelta(days=1)
        empty_rounds = 0
        while cursor > floor and empty_rounds < 3:
            window_start = max(cursor - span, floor)
            rates = mt5.copy_rates_range(self.symbol, tf_const,
                                         window_start + off, cursor + off)
            if rates is None or len(rates) == 0:
                empty_rounds += 1
                time.sleep(1.0)                     # the terminal may still be downloading
                if empty_rounds < 3:
                    continue
                break

            added = 0
            oldest_ts = None
            for r in rates:
                ts = int(r["time"])
                oldest_ts = ts if oldest_ts is None else min(oldest_ts, ts)
                real = ts - int(self.offset_hours * 3600)
                if real not in collected:
                    collected[real] = (float(r["open"]), float(r["high"]),
                                       float(r["low"]), float(r["close"]),
                                       float(r["tick_volume"]))
                    added += 1

            empty_rounds = 0 if added else empty_rounds + 1
            oldest_real = datetime.fromtimestamp(oldest_ts, tz=timezone.utc) - off
            print(f"\r[mt5] {timeframe:>3}: {len(collected):>7} bars, "
                  f"back to {oldest_real:%Y-%m-%d}   ", end="", flush=True)
            # step the window past the oldest bar we just saw
            cursor = min(oldest_real, cursor - timedelta(hours=1))

        print()
        return [Candle(datetime.fromtimestamp(ts, tz=timezone.utc), *v)
                for ts, v in sorted(collected.items())]


def load_csv(path: str) -> list[Candle]:
    candles = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            candles.append(Candle(
                time=datetime.fromisoformat(row["time"]).replace(tzinfo=timezone.utc)
                if "+" not in row["time"] else datetime.fromisoformat(row["time"]),
                open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
                volume=float(row.get("volume") or 0),
            ))
    candles.sort(key=lambda c: c.time)
    return candles


def save_csv(path: str, candles: list[Candle]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for c in candles:
            w.writerow([c.time.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                        c.open, c.high, c.low, c.close, c.volume])


def download_history(cfg: Config, timeframes: list[str], max_days: int) -> None:
    """Build the deepest candle archive this broker allows, merging into
    whatever earlier runs already saved. Safe to stop and re-run."""
    os.makedirs(cfg.csv_dir, exist_ok=True)
    print(f"Downloading {cfg.symbol} into {cfg.csv_dir}{os.sep} "
          f"({', '.join(timeframes)}, up to {max_days} days back)")
    print("If this stops early, the terminal is capping stored bars: "
          "MT5 -> Tools -> Options -> Charts -> 'Max bars in chart' -> Unlimited,\n"
          "then run this again -- it resumes from what is already saved.\n")

    with MT5Feed(cfg) as feed:
        for tf in timeframes:
            path = os.path.join(cfg.csv_dir, f"{cfg.symbol}_{tf}.csv")
            known = load_csv(path) if os.path.exists(path) else []
            before = len(known)
            candles = feed.fetch_max(tf, max_days, known)
            save_csv(path, candles)
            span = ((candles[-1].time - candles[0].time).days if candles else 0)
            print(f"[out] {tf:>3}: {len(candles):>7} bars ({len(candles) - before:+} new) "
                  f"covering {span} days -> {path}")


def check_session_clock(candles: list[Candle], timeframe: str,
                        offset_used: float | None) -> None:
    """Cross-check the broker clock against the candles themselves.

    Gold stops trading for the weekend at a fixed instant in real UTC -- 21:00
    in summer, 22:00 in winter -- so once the timestamps are converted, the
    weekly gap has to land there. If it does not, the offset is wrong and every
    kill zone is shifted by the same amount. This beats reading the server's
    clock off a tick: it works on a Sunday, it works on a CSV, and it is
    measured from the data being traded rather than from a live connection."""
    if TF_MINUTES[timeframe] > 60 or len(candles) < 2:
        return
    step = timedelta(minutes=TF_MINUTES[timeframe])
    closes = [a.time + step for a, b in zip(candles, candles[1:])
              if b.time - a.time > timedelta(hours=24)]
    if len(closes) < 2:
        return                                   # not enough weekends to judge

    hours = sorted(c.hour + c.minute / 60 for c in closes)
    observed = hours[len(hours) // 2]
    expected = 21.0 if is_bst(closes[-1]) else 22.0
    drift = observed - expected
    if drift > 12:
        drift -= 24
    elif drift < -12:
        drift += 24

    if abs(drift) <= 1.5:
        print(f"[clock] weekly close lands at {int(observed):02d}:{int(observed % 1 * 60):02d} "
              f"UTC -- matches the real gold close, the broker clock is right")
        return
    suggestion = ("" if offset_used is None
                  else f" -- re-run with --broker-utc-offset {offset_used + drift:g}")
    print(f"[clock] WARNING: the weekly close lands at "
          f"{int(observed):02d}:{int(observed % 1 * 60):02d} UTC but gold closes at "
          f"{expected:02.0f}:00. The broker offset looks wrong by {drift:+.0f}h, which "
          f"shifts every kill zone by the same amount{suggestion}.")


def load_dataset(cfg: Config, end_utc: datetime) -> dict[str, list[Candle]]:
    """Every timeframe the strategy needs, warmed up before the test window."""
    start_utc = end_utc - timedelta(days=cfg.days)
    data: dict[str, list[Candle]] = {}

    used_offset = cfg.broker_utc_offset
    if cfg.source == "mt5":
        with MT5Feed(cfg) as feed:
            for tf in cfg.timeframes:
                warm = timedelta(days=WARMUP_DAYS.get(tf, 30))
                data[tf] = feed.fetch(tf, start_utc - warm, end_utc)
            used_offset = feed.offset_hours
    else:
        for tf in cfg.timeframes:
            path = os.path.join(cfg.csv_dir, f"{cfg.symbol}_{tf}.csv")
            if not os.path.exists(path):
                raise SystemExit(f"Missing {path}. Run `export --csv-dir {cfg.csv_dir}` "
                                 "on a machine with MetaTrader 5 first.")
            data[tf] = load_csv(path)
            print(f"[csv] {tf:>3}: {len(data[tf]):>6} candles  "
                  f"{data[tf][0].time:%Y-%m-%d %H:%M} -> {data[tf][-1].time:%Y-%m-%d %H:%M} UTC")

    for tf, candles in data.items():
        if len(candles) < 50:
            raise SystemExit(f"Only {len(candles)} {tf} candles — not enough history.")

    check_session_clock(data[cfg.entry_tf], cfg.entry_tf, used_offset)
    return data


# ===========================================================================
# 4. ICT PRIMITIVES  (document §2 — the vocabulary the plan is written in)
# ===========================================================================


@dataclass(slots=True)
class Swing:
    index: int
    price: float
    kind: str           # "high" | "low"
    # a fractal is only *known* `window` bars later; every caller adds that
    # delay itself so nothing is ever read from the future


@dataclass(slots=True)
class StructureEvent:
    index: int
    type: str           # "BOS" | "CHoCH"
    direction: str      # "up" | "down"
    level: float


@dataclass(slots=True)
class Zone:
    """An order block or a fair value gap — a price band price may return to."""
    index: int
    kind: str           # "bullish_ob" | "bearish_ob" | "bullish_fvg" | "bearish_fvg"
    bottom: float
    top: float

    @property
    def bullish(self) -> bool:
        return self.kind.startswith("bullish")


@dataclass(slots=True)
class Pool:
    """Resting liquidity: equal highs/lows, or a single untouched swing."""
    level: float
    kind: str           # "high" (buy-side) | "low" (sell-side)
    index: int
    equal: bool = False


def atr_series(candles: list[Candle], period: int) -> list[float]:
    """Wilder's ATR, forward-filled so index i is always usable."""
    if not candles:
        return []
    trs = [candles[0].range]
    for prev, cur in zip(candles, candles[1:]):
        trs.append(max(cur.high - cur.low,
                       abs(cur.high - prev.close),
                       abs(cur.low - prev.close)))
    out = [trs[0]] * len(trs)
    if len(trs) <= period:
        running = sum(trs) / len(trs)
        return [running] * len(trs)
    seed = sum(trs[:period]) / period
    out[period - 1] = seed
    for i in range(period, len(trs)):
        out[i] = (out[i - 1] * (period - 1) + trs[i]) / period
    for i in range(period - 1):
        out[i] = seed
    return out


def swing_points(candles: list[Candle], window: int) -> list[Swing]:
    """Fractal swing highs/lows — the bar is the extreme of the `window` bars
    on each side of it (document §2.1)."""
    swings: list[Swing] = []
    for i in range(window, len(candles) - window):
        seg = candles[i - window: i + window + 1]
        if candles[i].high == max(c.high for c in seg):
            swings.append(Swing(i, candles[i].high, "high"))
        if candles[i].low == min(c.low for c in seg):
            swings.append(Swing(i, candles[i].low, "low"))
    swings.sort(key=lambda s: s.index)
    return swings


def structure_events(candles: list[Candle], swings: list[Swing], window: int) -> list[StructureEvent]:
    """Walk forward tracking the last *confirmed* swing high/low. A close beyond
    it continues the bias (BOS) or flips it (CHoCH) — document §2.1."""
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]
    events: list[StructureEvent] = []
    bias: str | None = None
    hi_ptr = lo_ptr = 0
    active_high = active_low = None

    for i, candle in enumerate(candles):
        # a fractal only becomes visible `window` bars after it printed
        while hi_ptr < len(highs) and highs[hi_ptr].index + window <= i:
            active_high = highs[hi_ptr]
            hi_ptr += 1
        while lo_ptr < len(lows) and lows[lo_ptr].index + window <= i:
            active_low = lows[lo_ptr]
            lo_ptr += 1

        if active_high is not None and candle.close > active_high.price:
            events.append(StructureEvent(i, "BOS" if bias == "up" else "CHoCH",
                                         "up", active_high.price))
            bias, active_high = "up", None
        if active_low is not None and candle.close < active_low.price:
            events.append(StructureEvent(i, "BOS" if bias == "down" else "CHoCH",
                                         "down", active_low.price))
            bias, active_low = "down", None

    return events


def dealing_range(swings: list[Swing]) -> tuple[float, float] | None:
    """The most recent swing high and swing low — the range whose 50% splits
    premium from discount (document §2.5)."""
    last_high = next((s.price for s in reversed(swings) if s.kind == "high"), None)
    last_low = next((s.price for s in reversed(swings) if s.kind == "low"), None)
    if last_high is None or last_low is None or last_high <= last_low:
        return None
    return last_low, last_high


def liquidity_pools(candles: list[Candle], swings: list[Swing], tolerance: float) -> list[Pool]:
    """Buy-side liquidity above equal highs, sell-side below equal lows, plus
    the untouched single swings around them (document §2.2)."""
    pools: list[Pool] = []
    # running extremes to the right of each bar: a level is still "resting"
    # liquidity only while nothing after it has traded through
    n = len(candles)
    suffix_max = [float("-inf")] * (n + 1)
    suffix_min = [float("inf")] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix_max[i] = max(suffix_max[i + 1], candles[i].high)
        suffix_min[i] = min(suffix_min[i + 1], candles[i].low)

    for kind in ("high", "low"):
        points = [s for s in swings if s.kind == kind]
        for a, b in zip(points, points[1:]):
            if abs(a.price - b.price) <= tolerance:
                level = max(a.price, b.price) if kind == "high" else min(a.price, b.price)
                pools.append(Pool(level, kind, b.index, equal=True))
        for s in points:
            # a swing still counts as liquidity until price trades through it
            if kind == "high" and suffix_max[s.index + 1] <= s.price:
                pools.append(Pool(s.price, kind, s.index))
            elif kind == "low" and suffix_min[s.index + 1] >= s.price:
                pools.append(Pool(s.price, kind, s.index))
    pools.sort(key=lambda p: p.index)
    return pools


def fair_value_gaps(candles: list[Candle], start: int = 0) -> list[Zone]:
    """Three-candle imbalance: candle 1 and candle 3 do not overlap
    (document §2.3). Indexed on the middle candle."""
    gaps: list[Zone] = []
    for i in range(max(start, 0), len(candles) - 2):
        c1, c3 = candles[i], candles[i + 2]
        if c1.high < c3.low:
            gaps.append(Zone(i + 1, "bullish_fvg", c1.high, c3.low))
        elif c1.low > c3.high:
            gaps.append(Zone(i + 1, "bearish_fvg", c3.high, c1.low))
    return gaps


def order_blocks(candles: list[Candle], atr: list[float], mult: float,
                 start: int = 0) -> list[Zone]:
    """Last opposite candle before a displacement move (document §2.4)."""
    blocks: list[Zone] = []
    for i in range(max(start, 1), len(candles)):
        candle = candles[i]
        if candle.body <= mult * atr[i]:
            continue
        bearish_impulse = candle.close < candle.open
        for j in range(i - 1, max(i - 12, -1), -1):
            prev = candles[j]
            if bearish_impulse and prev.bullish:
                blocks.append(Zone(j, "bearish_ob", prev.low, prev.high))
                break
            if not bearish_impulse and not prev.bullish:
                blocks.append(Zone(j, "bullish_ob", prev.low, prev.high))
                break
    return blocks


def has_displacement(candles: list[Candle], atr: list[float], lo: int, hi: int,
                     mult: float) -> bool:
    """A strong one-way move — a body above `mult` x ATR, or the consecutive
    FVGs such a move leaves behind (document §2.9)."""
    for i in range(max(lo, 0), min(hi + 1, len(candles))):
        if candles[i].body > mult * atr[i]:
            return True
    window = candles[max(lo - 1, 0): hi + 2]
    return bool(fair_value_gaps(window))


def ote_band(low: float, high: float, direction: str,
             ote_low: float, ote_high: float) -> tuple[float, float]:
    """Optimal Trade Entry: the 61.8%–79% retracement of the impulse leg
    (document §2.6). Returned as (bottom, top)."""
    span = high - low
    if direction == "long":                 # retracing down from the leg high
        return high - ote_high * span, high - ote_low * span
    return low + ote_low * span, low + ote_high * span


def overlap(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float] | None:
    bottom, top = max(a[0], b[0]), min(a[1], b[1])
    return (bottom, top) if top > bottom else None



# ===========================================================================
# 5. TOP-DOWN CONTEXT  (document §3 — bias flows from the big timeframe down)
# ===========================================================================


@dataclass
class TimeframeView:
    bias: str | None = None                    # "up" | "down" | None
    range_low: float | None = None
    range_high: float | None = None
    equilibrium: float | None = None
    pools: list[Pool] = field(default_factory=list)
    atr: float = 0.0
    last_close: float = 0.0
    prev_high: float | None = None             # previous completed candle
    prev_low: float | None = None


class TimeframeAnalyzer:
    """Higher-timeframe structure for a walk-forward run. Only candles that
    have already *closed* at the decision time are ever visible, and the
    analysis is recomputed only when a new candle closes."""

    def __init__(self, timeframe: str, candles: list[Candle], cfg: Config):
        self.tf = timeframe
        self.candles = candles
        self.cfg = cfg
        step = timedelta(minutes=TF_MINUTES[timeframe])
        self.close_times = [c.time + step for c in candles]
        self._cached_n = -1
        self._cached_view = TimeframeView()

    def closed_count(self, now_utc: datetime) -> int:
        """How many candles are fully closed at `now_utc`."""
        return bisect_right(self.close_times, now_utc)

    def view(self, now_utc: datetime) -> TimeframeView:
        n = self.closed_count(now_utc)
        if n == self._cached_n:
            return self._cached_view
        self._cached_n = n
        self._cached_view = self._compute(n)
        return self._cached_view

    def _compute(self, n: int) -> TimeframeView:
        cfg = self.cfg
        if n < 30:
            return TimeframeView()
        candles = self.candles[:n]
        atr = atr_series(candles, cfg.atr_period)
        swings = swing_points(candles, cfg.swing_window_htf)
        events = structure_events(candles, swings, cfg.swing_window_htf)

        view = TimeframeView(
            bias=events[-1].direction if events else None,
            pools=liquidity_pools(candles, swings, cfg.equal_level_atr * atr[-1]),
            atr=atr[-1],
            last_close=candles[-1].close,
            prev_high=candles[-1].high,
            prev_low=candles[-1].low,
        )
        rng = dealing_range(swings)
        if rng:
            view.range_low, view.range_high = rng
            view.equilibrium = (rng[0] + rng[1]) / 2
        return view


# ===========================================================================
# 6. THE SETUP FINDER — document §5, one function per step of the plan
# ===========================================================================


# The document's pre-trade checklist (§8), in the order the plan checks it.
PLAN_STAGES = [
    "1  inside a kill zone",
    "2  higher-timeframe bias aligned",
    "3  price in the right half (premium/discount)",
    "4  liquidity swept",
    "5  sweep still valid",
    "6  CHoCH with displacement",
    "7  impulse leg large enough",
    "8  order block / FVG inside the OTE band",
    "9  entry still reachable",
    "10 reward:risk at or above the minimum",
    "11 risk within the % cap",
]


@dataclass
class Setup:
    direction: str          # "long" | "short"
    entry: float
    sl: float
    tp: float
    rr: float
    risk_usd: float
    order_kind: str         # "limit" | "market"
    session: str
    judas: bool
    swept: str              # what liquidity was taken: swing / asia / prev_day
    poi: str                # which POI the entry sits in
    sweep_bar: int          # absolute index on the entry timeframe
    choch_bar: int
    time: datetime

    @property
    def key(self) -> tuple:
        return (self.direction, self.sweep_bar, self.choch_bar)


def _sweep_candidates(win: list[Candle], k: int, direction: str, cfg: Config,
                      ctx: dict) -> list[tuple[float, int, str]]:
    """Liquidity levels a sweep could be run against, each with the earliest
    bar at which that level was already known (document §5, step 2)."""
    kind = "low" if direction == "long" else "high"
    out: list[tuple[float, int, str]] = []

    for s in swing_points(win[: k + 1], cfg.swing_window_entry):
        if s.kind == kind:
            out.append((s.price, s.index + cfg.swing_window_entry + 1, "swing"))

    asia = ctx.get("asia_range")
    if asia and ctx.get("asia_known_from") is not None:
        level = asia[0] if kind == "low" else asia[1]
        out.append((level, ctx["asia_known_from"], "asia"))

    prev_day = ctx.get("prev_day_range")
    if prev_day and ctx.get("day_known_from") is not None:
        level = prev_day[0] if kind == "low" else prev_day[1]
        out.append((level, ctx["day_known_from"], "prev_day"))

    return out


def _find_sweeps(win: list[Candle], k: int, direction: str, cfg: Config,
                 ctx: dict) -> list[tuple[int, float, str]]:
    """Step 4 — wicks through resting liquidity that close back inside
    (document §2.2 Liquidity Sweep), most recent first. More than one is
    returned because the sweep that matters is the one the CHoCH follows,
    which is not always the very latest wick."""
    kind = "low" if direction == "long" else "high"
    earliest = max(0, k - cfg.sweep_lookback + 1)
    found: dict[int, tuple[float, str]] = {}

    for level, known_from, label in _sweep_candidates(win, k, direction, cfg, ctx):
        for j in range(max(earliest, known_from), k + 1):
            c = win[j]
            hit = (c.low < level and c.close > level) if kind == "low" \
                else (c.high > level and c.close < level)
            if not hit:
                continue
            # a level from the day's reference ranges beats an ordinary swing
            if j not in found or (label != "swing" and found[j][1] == "swing"):
                found[j] = (level, label)
    return [(j, lvl, lbl) for j, (lvl, lbl) in sorted(found.items(), reverse=True)]


def _find_choch(win: list[Candle], atr: list[float], sweep_bar: int, k: int,
                direction: str, cfg: Config) -> int | None:
    """Step 5 — after the sweep, price must break structure the other way and
    do it with displacement (document §2.1 CHoCH + §2.9 Displacement)."""
    swings = swing_points(win[: k + 1], cfg.swing_window_entry)
    kind = "high" if direction == "long" else "low"
    w = cfg.swing_window_entry
    refs = [s for s in swings if s.kind == kind and s.index <= sweep_bar]
    if not refs:
        return None

    last_bar = min(k, sweep_bar + cfg.choch_max_bars)
    for m in range(sweep_bar + 1, last_bar + 1):
        visible = [s for s in refs if s.index + w <= m]
        if not visible:
            continue
        level = visible[-1].price
        broke = win[m].close > level if direction == "long" else win[m].close < level
        if broke and has_displacement(win, atr, sweep_bar, m, cfg.displacement_atr):
            return m
    return None


def _find_poi(win: list[Candle], atr: list[float], lo: int, k: int, direction: str,
              band: tuple[float, float], cfg: Config) -> tuple[Zone, tuple[float, float]] | None:
    """Step 6 — the order block or fair value gap that overlaps the OTE band.
    The biggest overlap wins (documents §2.3, §2.4, §2.6)."""
    want = "bullish" if direction == "long" else "bearish"
    zones = [z for z in order_blocks(win[: k + 1], atr, cfg.displacement_atr, start=lo)
             if z.kind.startswith(want) and z.index >= lo]
    zones += [z for z in fair_value_gaps(win[: k + 1], start=lo)
              if z.kind.startswith(want) and z.index >= lo]

    best = None
    for zone in zones:
        ov = overlap((zone.bottom, zone.top), band)
        if ov and (best is None or (ov[1] - ov[0]) > (best[1][1] - best[1][0])):
            best = (zone, ov)
    return best


def _liquidity_target(pools: list[Pool], direction: str, entry: float,
                      min_distance: float, sl: float, cfg: Config) -> float | None:
    """Step 8 — the target is the nearest opposite liquidity pool, and the
    trade is only taken if that target pays at least 1:2 (document §5)."""
    if direction == "long":
        levels = sorted({p.level for p in pools
                         if p.kind == "high" and p.level > entry + min_distance})
    else:
        levels = sorted({p.level for p in pools
                         if p.kind == "low" and p.level < entry - min_distance},
                        reverse=True)
    if not levels:
        return None

    risk = abs(entry - sl)
    if cfg.tp_mode == "nearest":
        nearest = levels[0]
        return nearest if abs(nearest - entry) / risk >= cfg.min_rr else None
    for level in levels:
        if abs(level - entry) / risk >= cfg.min_rr:
            return level
    return None


def find_setup(entry_candles: list[Candle], entry_atr: list[float], i: int,
               ctx: dict, cfg: Config, balance: float,
               funnel: Counter | None = None) -> Setup | None:
    """The document's trading plan, steps 1-8, evaluated on the close of bar
    `i`. Returns a ready-to-place order, or None with the reason counted in
    the funnel. Nothing after bar `i` is ever read."""
    counted: set[str] = set()

    def tick(stage: str) -> None:
        """Count each bar once per stage. Both directions are evaluated on the
        same bar, so without this a stage could out-count the bars above it."""
        if funnel is not None and stage not in counted:
            counted.add(stage)
            funnel[stage] += 1

    bar = entry_candles[i]
    tick("0  bars evaluated")

    # --- step 3: kill zone (document §4) ---------------------------------
    session = active_session(bar.time, cfg.sessions)
    if session is None or in_blackout(bar.time, cfg.blackouts):
        return None
    tick(PLAN_STAGES[0])

    # --- step 1: bias from the higher timeframes (document §3) -----------
    bias_view: TimeframeView = ctx["bias"]
    if cfg.bias_mode == "both":
        # scalping variant: take the raid on whichever side sets up first,
        # letting the sweep itself pick the direction instead of the daily bias
        directions = ["long", "short"]
    else:
        if bias_view.bias is None:
            return None
        mid_view: TimeframeView | None = ctx.get("mid")
        if cfg.use_mid_filter and mid_view is not None:
            if mid_view.bias is not None and mid_view.bias != bias_view.bias:
                return None
        directions = ["long" if bias_view.bias == "up" else "short"]
    tick(PLAN_STAGES[1])

    for direction in directions:
        setup = _setup_for_direction(entry_candles, entry_atr, i, ctx, cfg, balance,
                                     direction, session, tick)
        if setup is not None:
            return setup
    return None


def _setup_for_direction(entry_candles: list[Candle], entry_atr: list[float], i: int,
                         ctx: dict, cfg: Config, balance: float, direction: str,
                         session: str, tick) -> Setup | None:
    """Steps 1b to 8 of the plan, for one side of the market."""
    bar = entry_candles[i]
    bias_view: TimeframeView = ctx["bias"]

    # --- step 1b: premium / discount of the most recent dealing range -----
    # (document §2.5: buy the discount half, sell the premium half)
    pd_view: TimeframeView = ctx.get("pd") or bias_view
    if cfg.use_pd_filter:
        if pd_view.equilibrium is None:
            return None
        in_zone = bar.close < pd_view.equilibrium if direction == "long" \
            else bar.close > pd_view.equilibrium
        if not in_zone:
            return None
    tick(PLAN_STAGES[2])

    # --- window of entry-timeframe bars this decision may look at ---------
    lo = max(0, i - cfg.sweep_lookback - 3 * cfg.swing_window_entry - 5)
    win = entry_candles[lo: i + 1]
    atr_win = entry_atr[lo: i + 1]
    k = i - lo
    atr = atr_win[k]
    if atr <= 0:
        return None

    local_ctx = dict(ctx)
    for key in ("asia_known_from", "day_known_from"):
        if ctx.get(key) is not None:
            local_ctx[key] = max(0, ctx[key] - lo)

    # --- step 4: liquidity sweep -----------------------------------------
    sweeps = _find_sweeps(win, k, direction, cfg, local_ctx)
    if not sweeps:
        return None
    tick(PLAN_STAGES[3])

    # --- step 5: the sweep must still hold, then break structure back ----
    sweep_bar = choch_bar = -1
    swept_what = ""
    any_valid = False
    for cand_bar, _level, label in sweeps[: cfg.max_sweep_candidates]:
        # if price traded back through the sweep extreme the setup is dead
        if direction == "long":
            if min(c.low for c in win[cand_bar: k + 1]) < win[cand_bar].low:
                continue
        elif max(c.high for c in win[cand_bar: k + 1]) > win[cand_bar].high:
            continue
        any_valid = True
        found = _find_choch(win, atr_win, cand_bar, k, direction, cfg)
        if found is not None:
            sweep_bar, choch_bar, swept_what = cand_bar, found, label
            break
    if any_valid:
        tick(PLAN_STAGES[4])
    if choch_bar < 0:
        return None
    tick(PLAN_STAGES[5])

    # --- step 6: OTE band of the impulse leg ------------------------------
    leg_low = min(c.low for c in win[sweep_bar: k + 1])
    leg_high = max(c.high for c in win[sweep_bar: k + 1])
    if leg_high - leg_low < cfg.min_leg_atr * atr:
        return None
    band = ote_band(leg_low, leg_high, direction, cfg.ote_low, cfg.ote_high)
    tick(PLAN_STAGES[6])

    poi_hit = _find_poi(win, atr_win, sweep_bar, k, direction, band, cfg)
    if poi_hit is None:
        if cfg.require_poi:
            return None
        zone_label, entry_band = "ote-only", band
    else:
        zone, ov = poi_hit
        zone_label, entry_band = zone.kind, ov
    tick(PLAN_STAGES[7])

    entry = (entry_band[0] + entry_band[1]) / 2

    # price must still be on the right side of the zone for a retracement entry
    if direction == "long":
        if bar.close < entry_band[0]:
            return None
        order_kind = "market" if bar.close <= entry_band[1] else "limit"
    else:
        if bar.close > entry_band[1]:
            return None
        order_kind = "market" if bar.close >= entry_band[0] else "limit"
    tick(PLAN_STAGES[8])

    # --- step 7: the stop ------------------------------------------------
    # "structure" is the document's rule: behind the swept extreme or the block.
    # "atr" places it at a fixed distance instead, for when the measured
    # excursion after a signal -- not the structure -- is what should size it.
    if cfg.sl_mode == "atr":
        sl = entry - cfg.sl_atr * atr if direction == "long" else entry + cfg.sl_atr * atr
    else:
        buffer = cfg.sl_buffer_atr * atr
        floor = max(cfg.min_sl_atr * atr, cfg.min_sl_distance)
        if direction == "long":
            sl = min(min(leg_low, entry_band[0]) - buffer, entry - floor)
        else:
            sl = max(max(leg_high, entry_band[1]) + buffer, entry + floor)

    # --- step 8: the target ----------------------------------------------
    poi_view: TimeframeView = ctx["poi"]
    if cfg.tp_mode == "atr":
        tp = entry + cfg.tp_atr * atr if direction == "long" else entry - cfg.tp_atr * atr
    else:
        tp = _liquidity_target(poi_view.pools, direction, entry,
                               0.5 * max(poi_view.atr, atr), sl, cfg)
    if tp is None:
        return None
    rr = abs(tp - entry) / abs(entry - sl)
    tick(PLAN_STAGES[9])

    # --- risk cap: the lot is fixed, so an oversized stop is a no-trade ---
    risk_usd = abs(entry - sl) * cfg.value_per_unit
    if risk_usd > cfg.risk_pct / 100.0 * balance:
        return None
    tick(PLAN_STAGES[10])

    judas = swept_what in ("asia", "prev_day") and session == "london"
    return Setup(direction=direction, entry=entry, sl=sl, tp=tp, rr=rr,
                 risk_usd=risk_usd, order_kind=order_kind, session=session,
                 judas=judas, swept=swept_what, poi=zone_label,
                 sweep_bar=lo + sweep_bar, choch_bar=lo + choch_bar, time=bar.time)


# ===========================================================================
# 7. BACKTEST ENGINE — $1,000 account, fixed 0.01 lot, walk-forward
# ===========================================================================


@dataclass
class Trade:
    direction: str
    entry_time: datetime
    entry: float
    sl: float
    tp: float
    exit_time: datetime
    exit: float
    reason: str             # SL | TP | BE | TIME | END
    pnl: float
    r_multiple: float
    balance: float
    risk_usd: float
    rr_planned: float
    session: str
    swept: str
    poi: str
    judas: bool
    bars_held: int


@dataclass
class Position:
    setup: Setup
    entry: float
    entry_bar: int
    entry_time: datetime
    sl: float
    initial_sl: float
    tp: float
    volume: float
    risk_price: float
    filled_intrabar: bool = False   # a limit fill happens at an unknown moment
                                    # inside its bar, so that bar's target is
                                    # not safe to claim -- only its stop is
    realized: float = 0.0       # money already banked by a partial close
    closed_volume: float = 0.0
    breakeven_done: bool = False
    partial_done: bool = False


@dataclass
class PendingOrder:
    setup: Setup
    expires_bar: int


@dataclass
class DayState:
    day: datetime | None = None
    pnl: float = 0.0
    consecutive_losses: int = 0
    start_balance: float = 0.0
    blocked: bool = False


@dataclass
class BacktestResult:
    trades: list[Trade]
    equity: list[tuple[datetime, float]]
    funnel: Counter
    orders_placed: int
    orders_filled: int
    orders_expired: int
    start: datetime
    end: datetime
    cfg: Config


def _day_index(entry: list[Candle]) -> dict[datetime, dict]:
    """Per London day: where the day starts on the entry timeframe, and the
    Asia accumulation range the Judas swing runs (document §4)."""
    index: dict[datetime, dict] = {}
    for i, candle in enumerate(entry):
        day = london_day(candle.time)
        rec = index.setdefault(day, {"start_idx": i, "asia_low": None,
                                     "asia_high": None, "asia_end_idx": None})
        if london_hour(candle.time) < KILL_ZONES["asia"][1]:
            rec["asia_low"] = candle.low if rec["asia_low"] is None else min(rec["asia_low"], candle.low)
            rec["asia_high"] = candle.high if rec["asia_high"] is None else max(rec["asia_high"], candle.high)
        elif rec["asia_end_idx"] is None:
            rec["asia_end_idx"] = i
    return index


def build_context(entry: list[Candle], i: int, analyzers: dict[str, TimeframeAnalyzer],
                  day_index: dict[datetime, dict], cfg: Config) -> dict:
    """Everything the setup finder is allowed to know at the close of bar i."""
    now = entry[i].time + timedelta(minutes=TF_MINUTES[cfg.entry_tf])
    ctx: dict = {
        "bias": analyzers[cfg.bias_tf].view(now),
        "poi": analyzers[cfg.poi_tf].view(now),
    }
    # the premium/discount and mid-bias references fall back to the bias
    # timeframe when their own timeframe was not loaded
    pd_analyzer = analyzers.get(cfg.pd_tf) or analyzers[cfg.bias_tf]
    ctx["pd"] = pd_analyzer.view(now)
    if cfg.use_mid_filter and cfg.mid_tf in analyzers:
        ctx["mid"] = analyzers[cfg.mid_tf].view(now)

    bias_view: TimeframeView = ctx["bias"]
    if bias_view.prev_low is not None:
        ctx["prev_day_range"] = (bias_view.prev_low, bias_view.prev_high)

    day = day_index.get(london_day(entry[i].time))
    if day:
        ctx["day_known_from"] = day["start_idx"]
        if day["asia_low"] is not None and day["asia_end_idx"] is not None \
                and i >= day["asia_end_idx"]:
            ctx["asia_range"] = (day["asia_low"], day["asia_high"])
            ctx["asia_known_from"] = day["asia_end_idx"]
    return ctx


def run_backtest(data: dict[str, list[Candle]], cfg: Config,
                 start_utc: datetime, end_utc: datetime) -> BacktestResult:
    entry = data[cfg.entry_tf]
    entry_atr = atr_series(entry, cfg.atr_period)
    analyzers = {tf: TimeframeAnalyzer(tf, candles, cfg) for tf, candles in data.items()}
    day_index = _day_index(entry)

    balance = cfg.balance
    trades: list[Trade] = []
    equity: list[tuple[datetime, float]] = [(start_utc, balance)]
    funnel: Counter = Counter()
    positions: list[Position] = []
    pendings: list[PendingOrder] = []
    day = DayState(start_balance=balance)
    last_setup_key: tuple | None = None
    orders_placed = orders_filled = orders_expired = 0

    def unit_value(volume: float) -> float:
        return volume * cfg.contract_size

    def close_position(pos: Position, price: float, when: datetime, bar_idx: int,
                       reason: str) -> Trade:
        nonlocal balance
        volume = pos.volume - pos.closed_volume
        sign = 1.0 if pos.setup.direction == "long" else -1.0
        gross = pos.realized + sign * (price - pos.entry) * unit_value(volume)
        costs = cfg.spread * unit_value(pos.volume) + cfg.commission_per_lot * pos.volume
        pnl = gross - costs
        balance += pnl
        risk_usd = pos.risk_price * unit_value(pos.volume)
        trade = Trade(
            direction=pos.setup.direction, entry_time=pos.entry_time, entry=pos.entry,
            sl=pos.initial_sl, tp=pos.tp, exit_time=when, exit=price, reason=reason,
            pnl=pnl, r_multiple=pnl / risk_usd if risk_usd else 0.0, balance=balance,
            risk_usd=risk_usd, rr_planned=pos.setup.rr, session=pos.setup.session,
            swept=pos.setup.swept, poi=pos.setup.poi, judas=pos.setup.judas,
            bars_held=bar_idx - pos.entry_bar,
        )
        trades.append(trade)
        equity.append((when, balance))
        return trade

    start_idx = bisect_right([c.time for c in entry], start_utc)
    if start_idx >= len(entry):
        raise SystemExit(f"No {cfg.entry_tf} candles inside the test window "
                         f"({start_utc:%Y-%m-%d} -> {end_utc:%Y-%m-%d}).")
    i = start_idx
    for i in range(start_idx, len(entry)):
        bar = entry[i]
        if bar.time >= end_utc:
            break

        # --- new London day: risk counters reset (document §6) ------------
        today = london_day(bar.time)
        if day.day != today:
            day = DayState(day=today, start_balance=balance)

        # --- fill resting orders ------------------------------------------
        still_pending: list[PendingOrder] = []
        for order in pendings:
            setup = order.setup
            if setup.order_kind == "market":
                positions.append(Position(
                    setup=setup, entry=bar.open, entry_bar=i, entry_time=bar.time,
                    sl=setup.sl, initial_sl=setup.sl, tp=setup.tp, volume=cfg.lot,
                    risk_price=abs(bar.open - setup.sl)))
                orders_filled += 1
                continue
            touched = bar.low <= setup.entry if setup.direction == "long" \
                else bar.high >= setup.entry
            if touched:
                positions.append(Position(
                    setup=setup, entry=setup.entry, entry_bar=i, entry_time=bar.time,
                    sl=setup.sl, initial_sl=setup.sl, tp=setup.tp, volume=cfg.lot,
                    risk_price=abs(setup.entry - setup.sl), filled_intrabar=True))
                orders_filled += 1
            elif i >= order.expires_bar:
                orders_expired += 1
            else:
                still_pending.append(order)
        pendings = still_pending

        # --- manage the open trades (document §5, step 9) -----------------
        survivors: list[Position] = []
        for position in positions:
            long = position.setup.direction == "long"
            hit_sl = bar.low <= position.sl if long else bar.high >= position.sl
            hit_tp = bar.high >= position.tp if long else bar.low <= position.tp
            if position.filled_intrabar and position.entry_bar == i:
                # the limit filled somewhere inside this bar; the extreme that
                # would pay the target may well have printed before that, so
                # only the stop is honoured here
                hit_tp = False

            if hit_sl:      # stop is checked first — the pessimistic assumption
                reason = "BE" if position.breakeven_done and \
                    abs(position.sl - position.entry) < 1e-9 else "SL"
                trade = close_position(position, position.sl, bar.time, i, reason)
            elif hit_tp:
                trade = close_position(position, position.tp, bar.time, i, "TP")
            elif cfg.max_hold_bars and i - position.entry_bar >= cfg.max_hold_bars:
                trade = close_position(position, bar.close, bar.time, i, "TIME")
            else:
                trade = None
                risk = position.risk_price
                reach = (bar.high - position.entry) if long else (position.entry - bar.low)
                if cfg.partial_r > 0 and not position.partial_done and reach >= cfg.partial_r * risk:
                    volume = position.volume * cfg.partial_fraction
                    level = position.entry + (risk * cfg.partial_r) * (1 if long else -1)
                    sign = 1.0 if long else -1.0
                    position.realized += sign * (level - position.entry) * unit_value(volume)
                    position.closed_volume += volume
                    position.partial_done = True
                if cfg.breakeven_r > 0 and not position.breakeven_done \
                        and reach >= cfg.breakeven_r * risk:
                    position.sl = position.entry
                    position.breakeven_done = True
                survivors.append(position)

            if trade is not None:
                day.pnl += trade.pnl
                day.consecutive_losses = day.consecutive_losses + 1 if trade.pnl < 0 else 0
                if day.consecutive_losses >= cfg.max_consecutive_losses:
                    day.blocked = True
                if day.pnl <= -cfg.max_daily_loss_pct / 100.0 * day.start_balance:
                    day.blocked = True
        positions = survivors

        # --- look for the next setup on this closed bar -------------------
        if len(positions) + len(pendings) < cfg.max_open and not day.blocked:
            ctx = build_context(entry, i, analyzers, day_index, cfg)
            setup = find_setup(entry, entry_atr, i, ctx, cfg, balance, funnel)
            live = {p.setup.key for p in positions} | {o.setup.key for o in pendings}
            if setup is not None and setup.key != last_setup_key and setup.key not in live:
                pendings.append(PendingOrder(setup, expires_bar=i + cfg.order_expiry_bars))
                last_setup_key = setup.key
                orders_placed += 1

    for position in positions:                     # still open at the last bar
        last = entry[min(len(entry) - 1, i)]
        close_position(position, last.close, last.time, i, "END")

    return BacktestResult(trades=trades, equity=equity, funnel=funnel,
                          orders_placed=orders_placed, orders_filled=orders_filled,
                          orders_expired=orders_expired, start=start_utc,
                          end=end_utc, cfg=cfg)


# ===========================================================================
# 8. REPORTING
# ===========================================================================


def max_drawdown(equity: list[tuple[datetime, float]]) -> tuple[float, float]:
    peak = equity[0][1]
    worst_abs = worst_pct = 0.0
    for _, value in equity:
        peak = max(peak, value)
        drop = peak - value
        if drop > worst_abs:
            worst_abs = drop
            worst_pct = drop / peak * 100.0 if peak else 0.0
    return worst_abs, worst_pct


def _streaks(trades: list[Trade]) -> tuple[int, int]:
    best = worst = cur_w = cur_l = 0
    for t in trades:
        if t.pnl > 0:
            cur_w, cur_l = cur_w + 1, 0
        else:
            cur_l, cur_w = cur_l + 1, 0
        best, worst = max(best, cur_w), max(worst, cur_l)
    return best, worst


def print_report(result: BacktestResult, verbose: bool = False) -> None:
    cfg = result.cfg
    trades = result.trades
    bar = "=" * 78

    print()
    print(bar)
    print(f" ICT GOLD BACKTEST — {cfg.symbol}   "
          f"{result.start:%Y-%m-%d} -> {result.end:%Y-%m-%d}  ({cfg.days} days)")
    print(bar)
    print(f" Account      : ${cfg.balance:,.2f}   lot {cfg.lot:g} "
          f"(${cfg.value_per_unit:.2f} per $1.00 of gold)")
    print(f" Risk rules   : {cfg.risk_pct:g}% max per trade, min RR 1:{cfg.min_rr:g}, "
          f"stop after {cfg.max_consecutive_losses} losses/day, "
          f"daily cap {cfg.max_daily_loss_pct:g}%")
    print(f" Timeframes   : bias {cfg.bias_tf}"
          f"{' + ' + cfg.mid_tf if cfg.use_mid_filter else ''}"
          f" | liquidity {cfg.poi_tf} | entry {cfg.entry_tf}")
    print(f" Kill zones   : {', '.join(cfg.sessions)} (London time)")
    print(f" Direction    : "
          + ("both sides (sweep picks the direction)" if cfg.bias_mode == "both"
             else f"{cfg.bias_tf} bias only")
          + f" | up to {cfg.max_open} open at once")
    print(f" Costs        : spread ${cfg.spread:.2f}/round turn"
          + (f", commission ${cfg.commission_per_lot:g}/lot" if cfg.commission_per_lot else ""))
    print(bar)

    if not trades:
        print(" No trades were taken in this window.")
        _print_funnel(result)
        return

    print(f"{'#':>3} {'entry (UTC)':<17}{'dir':<6}{'sess':<9}{'entry':>9}{'SL':>9}"
          f"{'TP':>9}{'exit':>9}{'why':>5}{'R':>7}{'P/L $':>9}{'bal $':>10}")
    print("-" * 78)
    for n, t in enumerate(trades, 1):
        print(f"{n:>3} {t.entry_time:%Y-%m-%d %H:%M} "
              f"{t.direction:<6}{t.session:<9}{t.entry:>9.2f}{t.sl:>9.2f}{t.tp:>9.2f}"
              f"{t.exit:>9.2f}{t.reason:>5}{t.r_multiple:>7.2f}{t.pnl:>9.2f}{t.balance:>10.2f}")
    print("-" * 78)

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    net = sum(t.pnl for t in trades)
    dd_abs, dd_pct = max_drawdown(result.equity)
    best_streak, worst_streak = _streaks(trades)
    final = cfg.balance + net

    print(" RESULTS")
    print(f"   Trades           : {len(trades)}   "
          f"(wins {len(wins)} / losses {len(losses)})")
    print(f"   Win rate         : {len(wins) / len(trades) * 100:.1f}%")
    print(f"   Net P/L          : ${net:+,.2f}   ({net / cfg.balance * 100:+.2f}% of start)")
    print(f"   Final balance    : ${final:,.2f}")
    print(f"   Gross profit     : ${gross_win:,.2f}    gross loss: ${gross_loss:,.2f}")
    print(f"   Profit factor    : "
          + (f"{gross_win / gross_loss:.2f}" if gross_loss else "n/a (no losses)"))
    print(f"   Expectancy       : {sum(t.r_multiple for t in trades) / len(trades):+.2f} R "
          f"(${net / len(trades):+.2f}) per trade")
    print(f"   Avg risk / trade : ${sum(t.risk_usd for t in trades) / len(trades):.2f} "
          f"({sum(t.risk_usd for t in trades) / len(trades) / cfg.balance * 100:.2f}% of start)")
    print(f"   Best / worst     : ${max(t.pnl for t in trades):+,.2f} / "
          f"${min(t.pnl for t in trades):+,.2f}")
    print(f"   Max drawdown     : ${dd_abs:,.2f} ({dd_pct:.2f}%)")
    print(f"   Longest streak   : {best_streak} wins / {worst_streak} losses")
    print(f"   Avg hold         : {sum(t.bars_held for t in trades) / len(trades):.0f} "
          f"{cfg.entry_tf} bars    avg planned RR: "
          f"1:{sum(t.rr_planned for t in trades) / len(trades):.2f}")

    by_reason = Counter(t.reason for t in trades)
    print(f"   Exits            : " + ", ".join(f"{k} {v}" for k, v in by_reason.most_common()))
    for label, key in (("By direction     ", "direction"), ("By kill zone     ", "session"),
                       ("By liquidity     ", "swept")):
        groups: dict[str, list[Trade]] = {}
        for t in trades:
            groups.setdefault(getattr(t, key), []).append(t)
        parts = [f"{name} {len(g)}t ${sum(x.pnl for x in g):+.2f}"
                 for name, g in sorted(groups.items())]
        print(f"   {label}: " + ", ".join(parts))

    judas = [t for t in trades if t.judas]
    if judas:
        print(f"   Judas swings     : {len(judas)} trades, "
              f"${sum(t.pnl for t in judas):+,.2f}")
    print(bar)
    _print_funnel(result, verbose)


def _print_funnel(result: BacktestResult, verbose: bool = True) -> None:
    if not verbose:
        return
    print(f" SIGNAL FUNNEL — how many {result.cfg.entry_tf} bars survived each step of the plan")
    width = max(len(s) for s in PLAN_STAGES) + 2
    print(f"   {'0  bars evaluated':<{width}} {result.funnel['0  bars evaluated']:>7}")
    for stage in PLAN_STAGES:
        print(f"   {stage:<{width}} {result.funnel[stage]:>7}")
    print(f"   {'-> orders placed':<{width}} {result.orders_placed:>7}")
    print(f"   {'-> orders filled':<{width}} {result.orders_filled:>7}")
    print(f"   {'-> expired unfilled':<{width}} {result.orders_expired:>7}")
    print("=" * 78)


def write_trades_csv(path: str, trades: list[Trade]) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["entry_time", "exit_time", "direction", "session", "swept", "poi",
                    "judas", "entry", "sl", "tp", "exit", "reason", "rr_planned",
                    "risk_usd", "r_multiple", "pnl", "balance", "bars_held"])
        for t in trades:
            w.writerow([t.entry_time.isoformat(), t.exit_time.isoformat(), t.direction,
                        t.session, t.swept, t.poi, int(t.judas), f"{t.entry:.2f}",
                        f"{t.sl:.2f}", f"{t.tp:.2f}", f"{t.exit:.2f}", t.reason,
                        f"{t.rr_planned:.2f}", f"{t.risk_usd:.2f}", f"{t.r_multiple:.3f}",
                        f"{t.pnl:.2f}", f"{t.balance:.2f}", t.bars_held])
    print(f"[out] {len(trades)} trades written to {path}")


# ===========================================================================
# 8b. WALK-FORWARD — the test that tells you whether an edge is real
# ===========================================================================


def walk_forward(data: dict[str, list[Candle]], cfg: Config, start_utc: datetime,
                 end_utc: datetime, segment_days: int, label: str = "") -> dict:
    """Replay the strategy over consecutive slices of time and report each one
    separately.

    A single number over one window says almost nothing: settings tuned on that
    window will always flatter themselves on it. What matters is whether the
    result repeats on stretches the settings were never fitted to. Segments that
    alternate between profit and loss mean the edge is noise, however good the
    total looks."""
    segments = []
    cursor = start_utc
    while cursor < end_utc:
        seg_end = min(cursor + timedelta(days=segment_days), end_utc)
        if (seg_end - cursor).days < max(3, segment_days // 3):
            break                                   # ignore a stub at the tail
        result = run_backtest(data, cfg, cursor, seg_end)
        trades = result.trades
        wins = [t for t in trades if t.pnl > 0]
        gross_loss = -sum(t.pnl for t in trades if t.pnl <= 0)
        gross_win = sum(t.pnl for t in wins)
        dd, _ = max_drawdown(result.equity)
        segments.append({
            "start": cursor, "end": seg_end, "trades": len(trades),
            "net": sum(t.pnl for t in trades), "dd": dd,
            "wr": (len(wins) / len(trades) * 100) if trades else 0.0,
            "pf": (gross_win / gross_loss) if gross_loss else (99.0 if gross_win else 0.0),
        })
        cursor = seg_end

    total = sum(s["net"] for s in segments)
    traded = [s for s in segments if s["trades"] > 0]
    winning = [s for s in traded if s["net"] > 0]

    bar = "=" * 78
    print()
    print(bar)
    print(f" WALK-FORWARD{' — ' + label if label else ''}   "
          f"{start_utc:%Y-%m-%d} -> {end_utc:%Y-%m-%d}, {segment_days}-day segments")
    print(bar)
    print(f" {'segment':<25}{'trades':>8}{'win':>8}{'net $':>10}{'pf':>7}{'maxDD $':>10}")
    print("-" * 78)
    for s in segments:
        print(f" {s['start']:%Y-%m-%d} -> {s['end']:%Y-%m-%d}{s['trades']:>8}"
              f"{s['wr']:>7.1f}%{s['net']:>+10.2f}{s['pf']:>7.2f}{s['dd']:>10.2f}")
    print("-" * 78)

    if not traded:
        print(" No trades in any segment.")
        print(bar)
        return {"segments": segments, "total": 0.0, "verdict": "no trades"}

    share = len(winning) / len(traded)
    print(f" Total          : ${total:+,.2f} over {sum(s['trades'] for s in segments)} trades")
    print(f" Segments won   : {len(winning)} of {len(traded)}  ({share * 100:.0f}%)")
    print(f" Worst segment  : ${min(s['net'] for s in traded):+,.2f}")

    if len(traded) < 3:
        verdict = ("Not enough segments to judge. Download more history "
                   "(`download`) and re-run over a longer window.")
    elif share >= 0.7 and total > 0:
        verdict = ("Profitable in most segments. That is the strongest evidence "
                   "this file can give you -- it is still not a promise.")
    elif total > 0:
        verdict = (f"Positive overall but only {share * 100:.0f}% of segments won: the total "
                   "is carried by a few good stretches, not a repeatable edge.")
    else:
        verdict = ("Negative overall. Whatever this configuration looked like on the "
                   "window it was chosen on, it does not hold up here.")
    print(f" Verdict        : {verdict}")
    print(bar)
    return {"segments": segments, "total": total, "share": share, "verdict": verdict}


# ===========================================================================
# 8c. SIGNAL QUALITY — do the entries beat a coin flip at all?
# ===========================================================================


def analyse_signals(data: dict[str, list[Candle]], cfg: Config, start_utc: datetime,
                    end_utc: datetime, horizons: tuple[int, ...] = (3, 6, 12, 24, 48)) -> None:
    """Measure the entries themselves, with no stop, target or position sizing
    in the way.

    For every signal, how far does price run in the trade's favour before it
    runs against it? Comparing that with random entries taken in the same
    sessions separates two very different failures: a signal that predicts
    nothing, and a signal that predicts something the exits are throwing away.
    Only the second one is worth tuning."""
    import random

    entry = data[cfg.entry_tf]
    atr = atr_series(entry, cfg.atr_period)
    analyzers = {tf: TimeframeAnalyzer(tf, c, cfg) for tf, c in data.items()}
    day_index = _day_index(entry)

    # the RR and risk gates decide trade selection, not signal quality, so open
    # them up: this asks what the pattern is worth, not what survives sizing
    probe = replace(cfg, min_rr=0.0, risk_pct=10_000.0)

    # measure from the price the trade would actually have been filled at, not
    # from the signal bar's close: the plan enters on a retracement, so the two
    # are different prices and only the fill is the strategy's real entry
    signals: list[tuple[int, str, float]] = []
    start_idx = bisect_right([c.time for c in entry], start_utc)
    seen: set[tuple] = set()
    for i in range(start_idx, len(entry)):
        if entry[i].time >= end_utc:
            break
        ctx = build_context(entry, i, analyzers, day_index, probe)
        setup = find_setup(entry, atr, i, ctx, probe, probe.balance)
        if setup is None or setup.key in seen:
            continue
        seen.add(setup.key)
        if setup.order_kind == "market":
            signals.append((i + 1, setup.direction, entry[i + 1].close
                            if i + 1 < len(entry) else setup.entry))
            continue
        for j in range(i + 1, min(i + 1 + cfg.order_expiry_bars, len(entry))):
            filled = entry[j].low <= setup.entry if setup.direction == "long" \
                else entry[j].high >= setup.entry
            if filled:
                signals.append((j, setup.direction, setup.entry))
                break

    if len(signals) < 20:
        print(f"\nOnly {len(signals)} signals in this window -- too few to measure.")
        return

    # a fair control: same session hours, same number of entries, coin-flip side
    rng = random.Random(20260824)
    eligible = [i for i in range(start_idx, len(entry))
                if entry[i].time < end_utc and active_session(entry[i].time, cfg.sessions)]
    controls = [(k := rng.choice(eligible), rng.choice(("long", "short")), entry[k].close)
                for _ in range(max(len(signals) * 5, 500))]

    def excursions(items: list[tuple[int, str, float]], horizon: int) -> tuple[float, float, float]:
        """Average best-case and worst-case move within `horizon` bars, in ATR,
        plus how often the favourable move came first."""
        mfe, mae, first = [], [], 0
        for i, direction, base in items:
            window = entry[i + 1: i + 1 + horizon]
            if len(window) < horizon or atr[i] <= 0:
                continue
            if direction == "long":
                up, down = max(c.high for c in window) - base, base - min(c.low for c in window)
                hit_up = next((n for n, c in enumerate(window) if c.high >= base + atr[i]), None)
                hit_dn = next((n for n, c in enumerate(window) if c.low <= base - atr[i]), None)
            else:
                up, down = base - min(c.low for c in window), max(c.high for c in window) - base
                hit_up = next((n for n, c in enumerate(window) if c.low <= base - atr[i]), None)
                hit_dn = next((n for n, c in enumerate(window) if c.high >= base + atr[i]), None)
            mfe.append(up / atr[i])
            mae.append(down / atr[i])
            if hit_up is not None and (hit_dn is None or hit_up < hit_dn):
                first += 1
        n = len(mfe) or 1
        return sum(mfe) / n, sum(mae) / n, first / n * 100

    bar = "=" * 78
    print()
    print(bar)
    print(f" SIGNAL QUALITY — {len(signals)} signals vs {len(controls)} random entries "
          f"in the same sessions")
    print(bar)
    print(" Move within N bars, measured in ATR. 'first' = how often the trade went")
    print(" 1 ATR the right way before it went 1 ATR the wrong way.")
    print()
    print(f" {'bars':>6} | {'signal MFE':>11}{'MAE':>8}{'first':>8} | "
          f"{'random MFE':>11}{'MAE':>8}{'first':>8} | {'edge':>7}")
    print("-" * 78)
    verdicts = []
    for h in horizons:
        s_mfe, s_mae, s_first = excursions(signals, h)
        r_mfe, r_mae, r_first = excursions(controls, h)
        edge = s_first - r_first
        verdicts.append(edge)
        print(f" {h:>6} | {s_mfe:>11.2f}{s_mae:>8.2f}{s_first:>7.1f}% | "
              f"{r_mfe:>11.2f}{r_mae:>8.2f}{r_first:>7.1f}% | {edge:>+6.1f}%")
    print("-" * 78)

    best = max(verdicts)
    if best < 2:
        print(" VERDICT: the signals behave like random entries. No exit rule, stop or")
        print("          target can rescue that -- the pattern itself has to change.")
    elif best < 5:
        print(f" VERDICT: a small edge ({best:+.1f}% over random). Real but thin: costs and")
        print("          spread can eat it, so exits have to be efficient to keep any of it.")
    else:
        print(f" VERDICT: the signals do beat random by {best:+.1f}%. If the backtest still")
        print("          loses, the exits are throwing the edge away, not the entries.")
    print(bar)


# ===========================================================================
# 9. LIVE SCAN — the same rules applied to the newest closed bar
# ===========================================================================


def scan_now(data: dict[str, list[Candle]], cfg: Config) -> None:
    entry = data[cfg.entry_tf]
    entry_atr = atr_series(entry, cfg.atr_period)
    analyzers = {tf: TimeframeAnalyzer(tf, candles, cfg) for tf, candles in data.items()}
    day_index = _day_index(entry)

    i = len(entry) - 1
    bar = entry[i]
    ctx = build_context(entry, i, analyzers, day_index, cfg)
    funnel: Counter = Counter()
    setup = find_setup(entry, entry_atr, i, ctx, cfg, cfg.balance, funnel)

    bias_view: TimeframeView = ctx["bias"]
    poi_view: TimeframeView = ctx["poi"]
    session = active_session(bar.time, cfg.sessions)

    print()
    print("=" * 78)
    print(f" LIVE SCAN — {cfg.symbol} on the {cfg.entry_tf} close of "
          f"{bar.time:%Y-%m-%d %H:%M} UTC ({to_london(bar.time):%H:%M} London)")
    print("=" * 78)
    print(f" Price            : {bar.close:.2f}")
    print(f" {cfg.bias_tf} bias          : {bias_view.bias or 'undefined'}")
    if ctx.get("mid"):
        print(f" {cfg.mid_tf} bias          : {ctx['mid'].bias or 'undefined'}")
    pd_view: TimeframeView = ctx.get("pd") or bias_view
    if pd_view.equilibrium:
        zone = "premium" if bar.close > pd_view.equilibrium else "discount"
        print(f" {cfg.pd_tf} range         : {pd_view.range_low:.2f} - {pd_view.range_high:.2f}"
              f"   equilibrium {pd_view.equilibrium:.2f}  ->  price in {zone}")
    print(f" Kill zone        : {session or 'outside ' + '/'.join(cfg.sessions)}")
    if ctx.get("asia_range"):
        print(f" Asia range today : {ctx['asia_range'][0]:.2f} - {ctx['asia_range'][1]:.2f}")
    if ctx.get("prev_day_range"):
        print(f" Previous {cfg.bias_tf}      : {ctx['prev_day_range'][0]:.2f} - "
              f"{ctx['prev_day_range'][1]:.2f}")

    above = sorted({p.level for p in poi_view.pools if p.level > bar.close})[:3]
    below = sorted({p.level for p in poi_view.pools if p.level < bar.close}, reverse=True)[:3]
    print(f" Buy-side pools   : {', '.join(f'{x:.2f}' for x in above) or '-'}")
    print(f" Sell-side pools  : {', '.join(f'{x:.2f}' for x in below) or '-'}")
    print("-" * 78)

    if setup is None:
        print(" No valid setup on this bar — pre-trade checklist (document §8):")
        for stage in PLAN_STAGES:
            print(f"   [{'x' if funnel[stage] else ' '}] {stage}")
        print(" The first unticked box is what this setup is still waiting for.")
    else:
        print(f" SETUP: {setup.direction.upper()}  ({setup.order_kind} order)")
        print(f"   Entry          : {setup.entry:.2f}")
        print(f"   Stop loss      : {setup.sl:.2f}   "
              f"(risk ${setup.risk_usd:.2f} at {cfg.lot:g} lot)")
        print(f"   Take profit    : {setup.tp:.2f}   (RR 1:{setup.rr:.2f})")
        print(f"   Liquidity taken: {setup.swept}"
              + ("  [Judas swing]" if setup.judas else ""))
        print(f"   Entry zone     : {setup.poi} inside the "
              f"{cfg.ote_low:.3f}-{cfg.ote_high:.2f} OTE band")
        print(f"   Kill zone      : {setup.session}")
    print("=" * 78)


# ===========================================================================
# 10. CLI
# ===========================================================================


def parse_blackouts(raw: str | None, minutes: int) -> list[tuple[datetime, datetime]]:
    """News blackout windows (document §7): --blackout 2026-08-01T12:30,..."""
    if not raw:
        return []
    windows = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        when = datetime.fromisoformat(item).replace(tzinfo=timezone.utc)
        windows.append((when - timedelta(minutes=minutes), when + timedelta(minutes=minutes)))
    return windows


# Ready-made setups. "document" is the file's own defaults -- the ICT plan
# read literally. The other two were fitted on 30 days of real XAU/USD M5 data
# and then checked on each half of that window separately; see README.
PRESETS: dict[str, dict] = {
    "document": {},
    "balanced": {
        "bias_mode": "both", "no_poi": True, "no_pd_filter": True, "no_mid_filter": True,
        "min_rr": 1.5, "breakeven_r": 0.0, "order_expiry_bars": 6, "max_hold_bars": 72,
        "max_consecutive_losses": 4, "max_open": 1,
        "sessions": "london,newyork,london_close",
    },
    "frequent": {
        "bias_mode": "both", "no_poi": True, "no_pd_filter": True, "no_mid_filter": True,
        "min_rr": 1.5, "breakeven_r": 0.0, "order_expiry_bars": 12, "max_hold_bars": 72,
        "max_consecutive_losses": 4, "max_open": 3,
        "sessions": "london,newyork,london_close",
    },
    # Built from measurement rather than from tuning profit. `signals` showed
    # that of the document's filters, premium/discount is the one carrying a
    # persistent edge (~+11% over random at every horizon), while the entries
    # without it are indistinguishable from coin flips. The same measurement
    # showed the favourable move arriving early and small -- around 1 ATR --
    # with the adverse move overtaking it on longer holds, so the stop and the
    # target are set there and the trade is given a day at most.
    #
    # NOTE: this deliberately breaks the document's 1:2 minimum. At 1:1 the
    # measured hit rate carries the expectancy instead; forcing 1:2 means
    # waiting for a move the data says does not reliably come.
    "measured": {
        "bias_mode": "both", "no_poi": True, "no_mid_filter": True, "pd_tf": "H4",
        "sl_mode": "atr", "sl_atr": 1.0, "tp_mode": "atr", "tp_atr": 1.0,
        "min_rr": 0.0, "breakeven_r": 0.0, "order_expiry_bars": 6,
        "max_hold_bars": 24, "max_consecutive_losses": 4, "max_open": 1,
        "sessions": "london,newyork,london_close",
    },
}

# What is actually known about each preset, from testing rather than hope.
PRESET_NOTES = {
    "document": "the ICT plan read literally -- very few trades, so its result "
                "is not statistically meaningful either way",
    "balanced": "fitted on 30 days of XAU/USD (+11%), then LOST 4.2% when retested "
                "on 90 days of the same symbol -- fitted, not validated",
    "frequent": "reaches ~4 trades a day, but its profit came from 3 trades out of "
                "85 and it lost money in the second half -- not validated",
    "measured": "entry filter and exit distances chosen from measured signal behaviour "
                "rather than from tuning profit (+107 over 30 days, 72% win rate). "
                "Still fitted on one month -- walkforward it before believing it",
}

# Nothing here has been shown to hold up out of sample. Run `walkforward`
# before trusting any of it.
UNPROVEN = {"balanced", "frequent", "measured"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="ICT XAU/USD strategy: MetaTrader 5 data feed, rule engine, "
                    "and a 30-day backtest on a $1,000 / 0.01-lot account.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--version", action="version", version=f"ict_gold_bot {VERSION}")
    p.add_argument("mode", nargs="?", default="backtest",
                   choices=["backtest", "walkforward", "signals", "scan", "export",
                            "download"],
                   help="backtest the last N days, walk-forward test it segment by "
                        "segment, scan live data now, export the candles a backtest "
                        "needs, or download the deepest history this broker serves")
    p.add_argument("--preset", choices=list(PRESETS), default="document",
                   help="'document' follows the ICT plan literally (few trades); "
                        "'balanced' trades both sides for ~2.5 setups a day; "
                        "'frequent' reaches ~4 a day but did NOT hold up out of sample. "
                        "Any flag you pass yourself still wins over the preset.")

    g = p.add_argument_group("data")
    g.add_argument("--source", choices=["mt5", "csv"], default="mt5")
    g.add_argument("--csv-dir", default="data", help="folder for exported candles")
    g.add_argument("--symbol", default="XAUUSD")
    g.add_argument("--days", type=int, default=30, help="length of the backtest window")
    g.add_argument("--timeframes", default="M1,M5,M15,H1,H4,D1",
                   help="download mode: which timeframes to archive")
    g.add_argument("--max-days", type=int, default=7300,
                   help="download mode: how far back to try (default ~20 years)")
    g.add_argument("--broker-utc-offset", type=float, default=None,
                   help="broker server clock vs UTC (default: auto-detect)")
    g.add_argument("--mt5-login", type=int, default=None)
    g.add_argument("--mt5-password", default=None)
    g.add_argument("--mt5-server", default=None)
    g.add_argument("--mt5-path", default=None, help="path to terminal64.exe")

    g = p.add_argument_group("account")
    g.add_argument("--balance", type=float, default=1000.0)
    g.add_argument("--lot", type=float, default=0.01)
    g.add_argument("--contract-size", type=float, default=100.0,
                   help="ounces per 1.00 lot (0.01 lot x 100 = $1 per $1 move)")
    g.add_argument("--spread", type=float, default=None,
                   help="price units, per round turn (default: read from the broker; "
                        "0.20 when replaying CSV)")
    g.add_argument("--commission", type=float, default=0.0, help="$ per lot, round turn")

    g = p.add_argument_group("risk (document section 6)")
    g.add_argument("--risk-pct", type=float, default=1.0)
    g.add_argument("--min-rr", type=float, default=2.0)
    g.add_argument("--max-consecutive-losses", type=int, default=2)
    g.add_argument("--max-daily-loss-pct", type=float, default=3.0)

    g = p.add_argument_group("strategy")
    g.add_argument("--sessions", default="london,newyork",
                   help="kill zones to trade: " + ",".join(KILL_ZONES))
    g.add_argument("--entry-tf", default="M5", choices=["M5", "M15", "M30"])
    g.add_argument("--poi-tf", default="H1", choices=["M30", "H1", "H4"])
    g.add_argument("--bias-tf", default="D1", choices=["H4", "D1"])
    g.add_argument("--pd-tf", default="H4", choices=["H1", "H4", "D1"],
                   help="timeframe whose dealing range defines premium/discount "
                        "(D1 is the strictest reading of the document)")
    g.add_argument("--no-mid-filter", action="store_true",
                   help="drop the H4 bias-alignment filter")
    g.add_argument("--no-pd-filter", action="store_true",
                   help="drop the premium/discount filter (OTE still applies)")
    g.add_argument("--bias-mode", choices=["htf", "both"], default="htf",
                   help="'htf' trades only the higher-timeframe direction (the "
                        "document); 'both' hunts sweeps on either side")
    g.add_argument("--no-poi", action="store_true",
                   help="allow an OTE entry without an order block / FVG")
    g.add_argument("--tp-mode", choices=["nearest", "first-valid", "atr"], default="nearest",
                   help="'nearest' follows the document (nearest liquidity pool, skip if "
                        "RR < min); 'atr' targets a fixed multiple of ATR instead")
    g.add_argument("--tp-atr", type=float, default=1.0,
                   help="target distance in ATR when --tp-mode atr")
    g.add_argument("--sl-mode", choices=["structure", "atr"], default="structure",
                   help="'structure' puts the stop behind the sweep (the document); "
                        "'atr' uses a fixed multiple of ATR")
    g.add_argument("--sl-atr", type=float, default=1.0,
                   help="stop distance in ATR when --sl-mode atr")
    g.add_argument("--sweep-lookback", type=int, default=72,
                   help="entry-TF bars searched backwards for a liquidity sweep")
    g.add_argument("--choch-max-bars", type=int, default=24,
                   help="the CHoCH must follow the sweep within this many bars")
    g.add_argument("--displacement-atr", type=float, default=1.2)
    g.add_argument("--min-leg-atr", type=float, default=1.5,
                   help="minimum impulse-leg size, in entry-TF ATR")
    g.add_argument("--min-sl-atr", type=float, default=1.0,
                   help="minimum stop distance in ATR (a tighter stop is spread noise)")
    g.add_argument("--min-sl-distance", type=float, default=1.0,
                   help="absolute floor under the stop distance, in dollars")
    g.add_argument("--order-expiry-bars", type=int, default=24)
    g.add_argument("--max-open", type=int, default=1,
                   help="positions + resting orders allowed at the same time; "
                        "raising it multiplies both the trade count and the risk on the table")
    g.add_argument("--max-hold-bars", type=int, default=288, help="0 = hold until SL/TP")
    g.add_argument("--breakeven-r", type=float, default=1.0, help="0 = never move the stop")
    g.add_argument("--partial-r", type=float, default=0.0,
                   help="take partial profit at xR (needs lot >= 2x the broker minimum)")
    g.add_argument("--partial-fraction", type=float, default=0.5)
    g.add_argument("--blackout", default=None,
                   help="comma-separated UTC news times, e.g. 2026-08-01T12:30")
    g.add_argument("--blackout-minutes", type=int, default=30)

    g = p.add_argument_group("output")
    g.add_argument("--save-data", action="store_true",
                   help="also write the downloaded candles to --csv-dir, so later "
                        "runs can replay them without MetaTrader 5")
    g.add_argument("--segment-days", type=int, default=30,
                   help="walkforward mode: length of each test segment")
    g.add_argument("--compare", action="store_true",
                   help="walkforward mode: run every preset over the same segments")
    g.add_argument("--verbose", action="store_true", help="print the signal funnel")
    g.add_argument("--csv-out", default=None, help="write the trade list to this CSV")
    return p


def config_from_args(args: argparse.Namespace) -> Config:
    sessions = tuple(s.strip() for s in args.sessions.split(",") if s.strip())
    unknown = [s for s in sessions if s not in KILL_ZONES]
    if unknown:
        raise SystemExit(f"Unknown kill zone(s): {', '.join(unknown)}. "
                         f"Choose from: {', '.join(KILL_ZONES)}")
    if args.partial_r > 0 and args.lot < 0.02:
        raise SystemExit(
            f"--partial-r needs a lot of at least 0.02: a partial close of {args.lot:g} "
            "would leave less than the 0.01 broker minimum. Run without --partial-r, "
            "or size up.")

    return Config(
        symbol=args.symbol, balance=args.balance, lot=args.lot,
        contract_size=args.contract_size,
        spread=0.20 if args.spread is None else args.spread,
        auto_spread=args.spread is None,
        commission_per_lot=args.commission, days=args.days,
        bias_tf=args.bias_tf, poi_tf=args.poi_tf, entry_tf=args.entry_tf,
        pd_tf=args.pd_tf, use_mid_filter=not args.no_mid_filter,
        use_pd_filter=not args.no_pd_filter, bias_mode=args.bias_mode,
        risk_pct=args.risk_pct, min_rr=args.min_rr,
        max_consecutive_losses=args.max_consecutive_losses,
        max_daily_loss_pct=args.max_daily_loss_pct,
        displacement_atr=args.displacement_atr, sweep_lookback=args.sweep_lookback,
        choch_max_bars=args.choch_max_bars, max_open=args.max_open,
        min_leg_atr=args.min_leg_atr, min_sl_atr=args.min_sl_atr,
        min_sl_distance=args.min_sl_distance, require_poi=not args.no_poi,
        tp_mode=args.tp_mode, tp_atr=args.tp_atr, sl_mode=args.sl_mode,
        sl_atr=args.sl_atr, order_expiry_bars=args.order_expiry_bars,
        max_hold_bars=args.max_hold_bars, breakeven_r=args.breakeven_r,
        partial_r=args.partial_r, partial_fraction=args.partial_fraction,
        sessions=sessions,
        blackouts=parse_blackouts(args.blackout, args.blackout_minutes),
        source=args.source, csv_dir=args.csv_dir,
        broker_utc_offset=args.broker_utc_offset, mt5_login=args.mt5_login,
        mt5_password=args.mt5_password, mt5_server=args.mt5_server,
        mt5_path=args.mt5_path,
    )


def drop_forming_candles(data: dict[str, list[Candle]], now: datetime) -> None:
    """A backtest may only ever see finished candles."""
    for tf, candles in data.items():
        step = timedelta(minutes=TF_MINUTES[tf])
        data[tf] = [c for c in candles if c.time + step <= now]


def main(argv: list[str] | None = None) -> int:
    # a French/OEM Windows console cannot encode every character in the report
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    # parse once to learn the preset, fold it into the defaults, parse again --
    # so a flag typed on the command line still beats the preset
    parser = build_parser()
    args = parser.parse_args(argv)
    print(f"[ict_gold_bot {VERSION}]  mode={args.mode}")
    if PRESETS[args.preset]:
        parser.set_defaults(**PRESETS[args.preset])
        args = parser.parse_args(argv)
        print(f"[preset] {args.preset}: {PRESET_NOTES[args.preset]}")
        if args.preset in UNPROVEN:
            print("[preset] Check it on your own data with: "
                  f"{os.path.basename(sys.argv[0])} walkforward --preset {args.preset}")
    cfg = config_from_args(args)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

    if args.mode == "download":
        cfg.source = "mt5"
        tfs = [t.strip().upper() for t in args.timeframes.split(",") if t.strip()]
        unknown = [t for t in tfs if t not in TF_MINUTES]
        if unknown:
            raise SystemExit(f"Unknown timeframe(s): {', '.join(unknown)}. "
                             f"Choose from: {', '.join(TF_MINUTES)}")
        download_history(cfg, tfs, args.max_days)
        return 0

    if args.mode == "export":
        cfg.source = "mt5"
        data = load_dataset(cfg, now)
        for tf, candles in data.items():
            path = os.path.join(cfg.csv_dir, f"{cfg.symbol}_{tf}.csv")
            save_csv(path, candles)
            print(f"[out] {len(candles):>6} {tf} candles -> {path}")
        return 0

    data = load_dataset(cfg, now)
    drop_forming_candles(data, now)

    if args.save_data and cfg.source == "mt5":
        for tf, candles in data.items():
            path = os.path.join(cfg.csv_dir, f"{cfg.symbol}_{tf}.csv")
            save_csv(path, candles)
        print(f"[out] candles saved to {cfg.csv_dir}{os.sep} "
              f"(replay them with --source csv --csv-dir {cfg.csv_dir})")

    if args.mode == "scan":
        scan_now(data, cfg)
        return 0

    # the window ends at the last closed entry-timeframe bar we actually have
    end_utc = data[cfg.entry_tf][-1].time + timedelta(minutes=TF_MINUTES[cfg.entry_tf])
    start_utc = end_utc - timedelta(days=cfg.days)

    if args.mode == "signals":
        analyse_signals(data, cfg, start_utc, end_utc)
        return 0

    if args.mode == "walkforward":
        if args.compare:
            for name in PRESETS:
                variant = build_parser()
                variant.set_defaults(**PRESETS[name])
                walk_forward(data, config_from_args(variant.parse_args(argv)),
                             start_utc, end_utc, args.segment_days, label=name)
        else:
            walk_forward(data, cfg, start_utc, end_utc, args.segment_days,
                         label=args.preset)
        return 0

    result = run_backtest(data, cfg, start_utc, end_utc)
    print_report(result, verbose=args.verbose)
    if args.csv_out:
        write_trades_csv(args.csv_out, result.trades)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
