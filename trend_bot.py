#!/usr/bin/env python3
"""
Trend Bot — all-in-one trend-following tool: reads OHLC candles (from a CSV,
or live from a running MT5 terminal), prints the current LONG/SHORT/FLAT
signal, and backtests the same strategy over the full history. One file,
one command.

Usage (local CSV):
    python3 trend_bot.py data.csv
    python3 trend_bot.py data.csv --fast 10 --slow 30 --atr-mult 2.5 --verbose

Usage (live from MT5 — needs MT5 terminal running locally + `pip install MetaTrader5`):
    python3 trend_bot.py --mt5 --symbol EURUSD --timeframe H1 --count 1000
    python3 trend_bot.py --mt5 --symbol EURUSD --timeframe H1 --count 1000 --out eurusd_h1.csv

Usage (run once, print the last 30 days' backtest, then watch for new trades
every 5 minutes — entry/take-profit/stop-loss printed only when a new trade
triggers):
    python3 trend_bot.py --mt5 --symbol EURUSD --timeframe M15 --live --days 30 --interval 5

CSV must have a header row with columns: date,open,high,low,close
"""

import argparse
import csv
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None


# ---------------------------------------------------------------- data ----

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
            candles.append(Candle(
                date=row["date"], open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
            ))
    if len(candles) < 2:
        raise ValueError("Need at least 2 rows of price data")
    return candles


# ------------------------------------------------------------ indicators --

def ema(values: list[float], period: int) -> list[float]:
    k = 2 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def _wilder_smooth_sum(values: list[float], period: int) -> list[float]:
    result = [sum(values[:period])]
    for v in values[period:]:
        result.append(result[-1] - result[-1] / period + v)
    return result


def _wilder_smooth_avg(values: list[float], period: int) -> list[float]:
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append((result[-1] * (period - 1) + v) / period)
    return result


def adx(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < period * 2:
        raise ValueError(f"Need at least {period * 2} candles to compute ADX({period})")

    plus_dm, minus_dm, tr = [], [], []
    for prev, cur in zip(candles, candles[1:]):
        up_move = cur.high - prev.high
        down_move = prev.low - cur.low
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        tr.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))

    smoothed_tr = _wilder_smooth_sum(tr, period)
    smoothed_plus_dm = _wilder_smooth_sum(plus_dm, period)
    smoothed_minus_dm = _wilder_smooth_sum(minus_dm, period)

    dx_values = []
    for t, pdm, mdm in zip(smoothed_tr, smoothed_plus_dm, smoothed_minus_dm):
        if t == 0:
            dx_values.append(0.0)
            continue
        plus_di = 100 * pdm / t
        minus_di = 100 * mdm / t
        di_sum = plus_di + minus_di
        dx_values.append(0.0 if di_sum == 0 else 100 * abs(plus_di - minus_di) / di_sum)

    if len(dx_values) < period:
        raise ValueError(f"Need at least {period * 2} candles to compute ADX({period})")

    return _wilder_smooth_avg(dx_values, period)[-1]


def atr(candles: list[Candle], period: int = 14) -> float:
    trs = []
    for prev, cur in zip(candles, candles[1:]):
        trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    if len(trs) < period:
        raise ValueError(f"Need at least {period + 1} candles to compute ATR({period})")
    result = [sum(trs[:period]) / period]
    for t in trs[period:]:
        result.append((result[-1] * (period - 1) + t) / period)
    return result[-1]


def swing_points(candles: list[Candle], window: int = 5) -> tuple[list[float], list[float]]:
    highs, lows = [], []
    for i in range(window, len(candles) - window):
        segment = candles[i - window : i + window + 1]
        if candles[i].high == max(c.high for c in segment):
            highs.append(candles[i].high)
        if candles[i].low == min(c.low for c in segment):
            lows.append(candles[i].low)
    return highs, lows


def structure_bias(highs: list[float], lows: list[float]) -> str:
    if len(highs) < 2 or len(lows) < 2:
        return "unknown"
    higher_highs = highs[-1] > highs[-2]
    higher_lows = lows[-1] > lows[-2]
    if higher_highs and higher_lows:
        return "up"
    if not higher_highs and not higher_lows:
        return "down"
    return "mixed"


def chandelier_stop(candles: list[Candle], direction: str, atr_value: float, atr_mult: float = 3.0, lookback: int = 22) -> float:
    window = candles[-lookback:] if len(candles) >= lookback else candles
    if direction == "long":
        return max(c.high for c in window) - atr_mult * atr_value
    return min(c.low for c in window) + atr_mult * atr_value


# -------------------------------------------------------------- strategy --

def evaluate_trend_following(
    candles: list[Candle], fast: int = 20, slow: int = 50, adx_period: int = 14,
    adx_threshold: float = 25.0, atr_period: int = 14, atr_mult: float = 3.0, swing_window: int = 5,
) -> dict:
    closes = [c.close for c in candles]
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)

    trend_up = ema_fast[-1] > ema_slow[-1]
    price_above_fast = closes[-1] > ema_fast[-1]

    highs, lows = swing_points(candles, swing_window)
    structure = structure_bias(highs, lows)

    adx_value = adx(candles, adx_period)
    strong = adx_value >= adx_threshold
    atr_value = atr(candles, atr_period)

    signal = "FLAT"
    if strong and trend_up and price_above_fast and structure != "down":
        signal = "LONG"
    elif strong and not trend_up and not price_above_fast and structure != "up":
        signal = "SHORT"

    trailing_stop = None
    if signal in ("LONG", "SHORT"):
        trailing_stop = chandelier_stop(candles, "long" if signal == "LONG" else "short", atr_value, atr_mult)

    return {
        "signal": signal, "fast_period": fast, "slow_period": slow,
        "ema_fast": ema_fast[-1], "ema_slow": ema_slow[-1], "structure": structure,
        "adx_period": adx_period, "adx": adx_value, "adx_strong": strong,
        "atr_period": atr_period, "atr": atr_value, "last_close": closes[-1],
        "trailing_stop": trailing_stop,
    }


# -------------------------------------------------------------- backtest --

def run_backtest(candles: list[Candle], fee_pct: float = 0.0, **strategy_kwargs) -> list[dict]:
    trades = []
    position = None

    for i in range(1, len(candles)):
        candle = candles[i]
        try:
            result = evaluate_trend_following(candles[: i + 1], **strategy_kwargs)
        except ValueError:
            continue

        signal, stop = result["signal"], result["trailing_stop"]

        if position is None:
            if signal in ("LONG", "SHORT") and stop is not None:
                position = {"direction": signal, "entry_index": i, "entry_price": candle.close, "stop": stop}
            continue

        if position["direction"] == "LONG":
            if stop is not None:
                position["stop"] = max(position["stop"], stop)
            hit_stop = candle.low <= position["stop"]
            flipped = signal == "SHORT"
        else:
            if stop is not None:
                position["stop"] = min(position["stop"], stop)
            hit_stop = candle.high >= position["stop"]
            flipped = signal == "LONG"

        if hit_stop or flipped:
            exit_price = position["stop"] if hit_stop else candle.close
            sign = 1 if position["direction"] == "LONG" else -1
            raw_pnl_pct = sign * (exit_price - position["entry_price"]) / position["entry_price"]
            trades.append({
                "direction": position["direction"], "entry_index": position["entry_index"],
                "entry_date": candles[position["entry_index"]].date, "entry_price": position["entry_price"],
                "exit_index": i, "exit_date": candle.date, "exit_price": exit_price,
                "exit_reason": "stop" if hit_stop else "flip", "bars_held": i - position["entry_index"],
                "pnl_pct": (raw_pnl_pct - fee_pct / 100) * 100,
            })
            position = None
            if flipped and not hit_stop and stop is not None:
                position = {"direction": signal, "entry_index": i, "entry_price": candle.close, "stop": stop}

    if position is not None:
        last = candles[-1]
        sign = 1 if position["direction"] == "LONG" else -1
        raw_pnl_pct = sign * (last.close - position["entry_price"]) / position["entry_price"]
        trades.append({
            "direction": position["direction"], "entry_index": position["entry_index"],
            "entry_date": candles[position["entry_index"]].date, "entry_price": position["entry_price"],
            "exit_index": len(candles) - 1, "exit_date": last.date, "exit_price": last.close,
            "exit_reason": "open_at_end", "bars_held": len(candles) - 1 - position["entry_index"],
            "pnl_pct": (raw_pnl_pct - fee_pct / 100) * 100,
        })

    return trades


def summarize(trades: list[dict], capital: float = 10000.0) -> dict:
    if not trades:
        return {"trades": 0}

    equity = capital
    equity_curve = [equity]
    wins, losses = [], []
    for t in trades:
        equity *= 1 + t["pnl_pct"] / 100
        equity_curve.append(equity)
        (wins if t["pnl_pct"] > 0 else losses).append(t["pnl_pct"])

    peak, max_dd = equity_curve[0], 0.0
    for e in equity_curve:
        peak = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak * 100)

    gross_win, gross_loss = sum(wins), abs(sum(losses))
    return {
        "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100, "final_equity": equity,
        "total_return_pct": (equity - capital) / capital * 100,
        "avg_win_pct": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss_pct": sum(losses) / len(losses) if losses else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "max_drawdown_pct": max_dd,
    }


# --------------------------------------------------------------- MT5 i/o --

TIMEFRAME_NAMES = [
    "M1", "M2", "M3", "M4", "M5", "M6", "M10", "M12", "M15", "M20", "M30",
    "H1", "H2", "H3", "H4", "H6", "H8", "H12", "D1", "W1", "MN1",
]


def fetch_candles_from_mt5(
    symbol: str, timeframe: str, count: int, date_from: str | None, date_to: str | None,
    login: int | None, password: str | None, server: str | None, path: str | None,
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

        tf = getattr(mt5, f"TIMEFRAME_{timeframe}")
        if date_from and date_to:
            start = datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc)
            end = datetime.fromisoformat(date_to).replace(tzinfo=timezone.utc)
            rates = mt5.copy_rates_range(symbol, tf, start, end)
        else:
            rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)

        if rates is None or len(rates) == 0:
            code, desc = mt5.last_error()
            raise ValueError(f"No data returned for {symbol}/{timeframe}: [{code}] {desc}")

        return [
            Candle(
                date=datetime.fromtimestamp(int(r["time"]), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                open=float(r["open"]), high=float(r["high"]), low=float(r["low"]), close=float(r["close"]),
            )
            for r in rates
        ]
    finally:
        mt5.shutdown()


def write_csv(candles: list[Candle], out_path: str) -> None:
    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "open", "high", "low", "close"])
        for c in candles:
            writer.writerow([c.date, f"{c.open:.5f}", f"{c.high:.5f}", f"{c.low:.5f}", f"{c.close:.5f}"])


# ------------------------------------------------------------------ time --

def _parse_date(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def filter_last_days(candles: list[Candle], days: int) -> list[Candle]:
    """Keep only candles within `days` of the most recent one. Falls back to
    the full list if dates can't be parsed (e.g. a non-ISO date column)."""
    last_dt = _parse_date(candles[-1].date)
    if last_dt is None:
        return candles
    cutoff = last_dt - timedelta(days=days)
    filtered = [c for c in candles if (dt := _parse_date(c.date)) is not None and dt >= cutoff]
    return filtered if len(filtered) >= 2 else candles


def compute_take_profit(signal: str, entry: float, stop: float, rr: float) -> float:
    """Fixed target at `rr` times the stop-loss distance — the trailing stop
    still governs the actual exit; this is a reference TP level only."""
    risk = abs(entry - stop)
    return entry + risk * rr if signal == "LONG" else entry - risk * rr


# ---------------------------------------------------------------- live ----

def live_loop(fetch_fn, strategy_kwargs: dict, rr: float, interval_minutes: float) -> None:
    """Re-fetch fresh candles every `interval_minutes` and report only when
    the signal changes into a new LONG/SHORT position (an actual new trade),
    printing entry/take-profit/stop-loss for it. Runs until Ctrl+C."""
    print(f"\n=== Live monitoring (every {interval_minutes} min, Ctrl+C to stop) ===")
    last_signal = None
    while True:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            candles = fetch_fn()
            result = evaluate_trend_following(candles, **strategy_kwargs)
        except ValueError as e:
            print(f"[{now}] Skipped (not enough data yet): {e}")
            time.sleep(interval_minutes * 60)
            continue

        signal = result["signal"]
        if signal in ("LONG", "SHORT") and signal != last_signal:
            entry, stop = result["last_close"], result["trailing_stop"]
            tp = compute_take_profit(signal, entry, stop, rr)
            print(f"[{now}] NEW TRADE : {signal}")
            print(f"  Entry       : {entry:.5f}")
            print(f"  Take Profit : {tp:.5f}  (RR 1:{rr:g})")
            print(f"  Stop Loss   : {stop:.5f}")
        else:
            print(f"[{now}] No new trade (signal: {signal})")
        last_signal = signal
        time.sleep(interval_minutes * 60)


# --------------------------------------------------------------- CLI ------

def main() -> None:
    parser = argparse.ArgumentParser(description="Trend-following signal + backtest, from a CSV or live MT5 data.")
    parser.add_argument("csv_path", nargs="?", help="Path to CSV with columns: date,open,high,low,close")
    parser.add_argument("--mt5", action="store_true", help="Fetch candles live from a running MT5 terminal instead of a CSV")
    parser.add_argument("--symbol", help="MT5 symbol, e.g. EURUSD (required with --mt5)")
    parser.add_argument("--timeframe", choices=TIMEFRAME_NAMES, help="MT5 timeframe (required with --mt5)")
    parser.add_argument("--count", type=int, default=1000, help="MT5: number of most recent bars (default: 1000)")
    parser.add_argument("--from", dest="date_from", help="MT5: start date (YYYY-MM-DD), UTC")
    parser.add_argument("--to", dest="date_to", help="MT5: end date (YYYY-MM-DD), UTC")
    parser.add_argument("--login", type=int, help="MT5 account login")
    parser.add_argument("--password", help="MT5 account password")
    parser.add_argument("--server", help="MT5 broker server name")
    parser.add_argument("--path", help="Path to terminal64.exe, if MT5 isn't already running")
    parser.add_argument("--out", help="MT5: also save the fetched candles to this CSV path")

    parser.add_argument("--fast", type=int, default=20, help="Fast EMA period (default: 20)")
    parser.add_argument("--slow", type=int, default=50, help="Slow EMA period (default: 50)")
    parser.add_argument("--adx-period", type=int, default=14, help="ADX period (default: 14)")
    parser.add_argument("--adx-threshold", type=float, default=25.0, help="Minimum ADX to trust the trend (default: 25)")
    parser.add_argument("--atr-period", type=int, default=14, help="ATR period (default: 14)")
    parser.add_argument("--atr-mult", type=float, default=3.0, help="ATR multiple for the trailing stop (default: 3.0)")
    parser.add_argument("--swing", type=int, default=5, help="Swing detection window (default: 5)")
    parser.add_argument("--capital", type=float, default=10000.0, help="Backtest starting capital (default: 10000)")
    parser.add_argument("--fee", type=float, default=0.0, help="Backtest round-trip fee %% of notional (default: 0.0)")
    parser.add_argument("--verbose", action="store_true", help="Print every backtest trade, not just the summary")
    parser.add_argument("--days", type=int, help="Limit the printed backtest to the last N days (default: all fetched data)")
    parser.add_argument("--rr", type=float, default=2.0, help="Reward:risk ratio used for the suggested take-profit (default: 2.0)")
    parser.add_argument("--live", action="store_true", help="After the one-time backtest, keep re-checking for new trades every --interval minutes")
    parser.add_argument("--interval", type=float, default=5.0, help="Live-mode: minutes between checks (default: 5)")
    args = parser.parse_args()

    if args.mt5:
        if not args.symbol or not args.timeframe:
            parser.error("--mt5 requires --symbol and --timeframe")

        def fetch_fn():
            return fetch_candles_from_mt5(
                args.symbol, args.timeframe, args.count, args.date_from, args.date_to,
                args.login, args.password, args.server, args.path,
            )

        candles = fetch_fn()
        print(f"Fetched {len(candles)} candles from MT5 ({args.symbol} {args.timeframe})")
        if args.out:
            write_csv(candles, args.out)
            print(f"Saved to {args.out}")
    else:
        if not args.csv_path:
            parser.error("provide a csv_path, or use --mt5 --symbol ... --timeframe ...")

        def fetch_fn():
            return load_candles(args.csv_path)

        candles = fetch_fn()

    strategy_kwargs = dict(
        fast=args.fast, slow=args.slow, adx_period=args.adx_period, adx_threshold=args.adx_threshold,
        atr_period=args.atr_period, atr_mult=args.atr_mult, swing_window=args.swing,
    )

    print("\n=== Signal ===")
    result = evaluate_trend_following(candles, **strategy_kwargs)
    print(f"Signal              : {result['signal']}")
    print(f"EMA{result['fast_period']}/EMA{result['slow_period']}        : {result['ema_fast']:.5f} / {result['ema_slow']:.5f}")
    print(f"Structure           : {result['structure']}")
    strength = "strong" if result["adx_strong"] else "weak / ranging"
    print(f"ADX({result['adx_period']})            : {result['adx']:.2f} ({strength})")
    print(f"ATR({result['atr_period']})            : {result['atr']:.5f}")
    print(f"Last close          : {result['last_close']:.5f}")
    if result["trailing_stop"] is not None:
        tp = compute_take_profit(result["signal"], result["last_close"], result["trailing_stop"], args.rr)
        print(f"Take Profit         : {tp:.5f}  (RR 1:{args.rr:g})")
        print(f"Stop Loss           : {result['trailing_stop']:.5f}")
    else:
        print("Take Profit         : n/a (no active signal)")
        print("Stop Loss           : n/a (no active signal)")

    backtest_candles = filter_last_days(candles, args.days) if args.days else candles
    print(f"\n=== Backtest (last {args.days} days) ===" if args.days else "\n=== Backtest ===")
    trades = run_backtest(backtest_candles, fee_pct=args.fee, **strategy_kwargs)
    stats = summarize(trades, args.capital)

    if args.verbose and trades:
        print(f"{'#':>3} {'Dir':<5} {'Entry':<20} {'@':>10} {'Exit':<20} {'@':>10} {'Bars':>5} {'Reason':<12} {'PnL%':>8}")
        for n, t in enumerate(trades, 1):
            print(
                f"{n:>3} {t['direction']:<5} {t['entry_date']:<20} {t['entry_price']:>10.4f} "
                f"{t['exit_date']:<20} {t['exit_price']:>10.4f} {t['bars_held']:>5} "
                f"{t['exit_reason']:<12} {t['pnl_pct']:>+8.2f}"
            )
        print()

    if stats["trades"] == 0:
        print("No trades were taken over this period.")
    else:
        print(f"Trades           : {stats['trades']} ({stats['wins']} win / {stats['losses']} loss)")
        print(f"Win rate         : {stats['win_rate']:.2f}%")
        print(f"Total return     : {stats['total_return_pct']:+.2f}% (equity {args.capital:.2f} -> {stats['final_equity']:.2f})")
        print(f"Avg win / loss   : {stats['avg_win_pct']:+.2f}% / {stats['avg_loss_pct']:+.2f}%")
        profit_factor = "inf" if stats["profit_factor"] == float("inf") else f"{stats['profit_factor']:.2f}"
        print(f"Profit factor    : {profit_factor}")
        print(f"Max drawdown     : {stats['max_drawdown_pct']:.2f}%")

    if args.live:
        live_loop(fetch_fn, strategy_kwargs, args.rr, args.interval)


if __name__ == "__main__":
    try:
        main()
    except (FileNotFoundError, ValueError, KeyError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)
