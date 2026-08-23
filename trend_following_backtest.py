#!/usr/bin/env python3
"""
Trend Following Backtest — walks the trend-following strategy
(trend_following_strategy.py) bar-by-bar over historical OHLC data and
reports how it would have performed: trade log, win rate, return, profit
factor and max drawdown.

At each bar the strategy is re-evaluated using only the candles up to and
including that bar (no lookahead) — the same `evaluate_trend_following`
function used for live signals, so the backtest and the live tool can never
disagree on what the signal was at a given point in time.

Simplifications: entries/exits happen at the signal bar's close (a real
system would need the bar to finish first, so live fills would lag by one
bar); the trailing stop is checked against the bar's high/low and, once
touched, exits at the stop price.

Usage:
    python3 trend_following_backtest.py data.csv [--fast 20] [--slow 50]
        [--adx-period 14] [--adx-threshold 25] [--atr-period 14]
        [--atr-mult 3] [--swing 5] [--capital 10000] [--fee 0.0] [--verbose]

CSV must have a header row with columns: date,open,high,low,close
"""

import argparse
import sys

from trend_following_strategy import evaluate_trend_following
from trend_identifier import load_candles


def run_backtest(
    candles,
    fast: int = 20,
    slow: int = 50,
    adx_period: int = 14,
    adx_threshold: float = 25.0,
    atr_period: int = 14,
    atr_mult: float = 3.0,
    swing_window: int = 5,
    fee_pct: float = 0.0,
) -> list[dict]:
    """Return the closed trade log (each trade as a dict)."""
    trades = []
    position = None  # {"direction", "entry_index", "entry_price", "stop"}

    for i in range(1, len(candles)):
        candle = candles[i]
        try:
            result = evaluate_trend_following(
                candles[: i + 1], fast, slow, adx_period, adx_threshold, atr_period, atr_mult, swing_window
            )
        except ValueError:
            continue  # not enough history yet for ADX/ATR

        signal = result["signal"]
        stop = result["trailing_stop"]

        if position is None:
            if signal in ("LONG", "SHORT") and stop is not None:
                position = {"direction": signal, "entry_index": i, "entry_price": candle.close, "stop": stop}
            continue

        if position["direction"] == "LONG":
            if stop is not None:
                position["stop"] = max(position["stop"], stop)  # only tighten, never loosen
            hit_stop = candle.low <= position["stop"]
            flipped = signal == "SHORT"
        else:
            if stop is not None:
                position["stop"] = min(position["stop"], stop)
            hit_stop = candle.high >= position["stop"]
            flipped = signal == "LONG"

        if hit_stop or flipped:
            exit_price = position["stop"] if hit_stop else candle.close
            direction_sign = 1 if position["direction"] == "LONG" else -1
            raw_pnl_pct = direction_sign * (exit_price - position["entry_price"]) / position["entry_price"]
            trades.append({
                "direction": position["direction"],
                "entry_index": position["entry_index"],
                "entry_date": candles[position["entry_index"]].date,
                "entry_price": position["entry_price"],
                "exit_index": i,
                "exit_date": candle.date,
                "exit_price": exit_price,
                "exit_reason": "stop" if hit_stop else "flip",
                "bars_held": i - position["entry_index"],
                "pnl_pct": (raw_pnl_pct - fee_pct / 100) * 100,
            })
            position = None
            if flipped and not hit_stop and stop is not None:
                position = {"direction": signal, "entry_index": i, "entry_price": candle.close, "stop": stop}

    if position is not None:
        last = candles[-1]
        direction_sign = 1 if position["direction"] == "LONG" else -1
        raw_pnl_pct = direction_sign * (last.close - position["entry_price"]) / position["entry_price"]
        trades.append({
            "direction": position["direction"],
            "entry_index": position["entry_index"],
            "entry_date": candles[position["entry_index"]].date,
            "entry_price": position["entry_price"],
            "exit_index": len(candles) - 1,
            "exit_date": last.date,
            "exit_price": last.close,
            "exit_reason": "open_at_end",
            "bars_held": len(candles) - 1 - position["entry_index"],
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

    peak = equity_curve[0]
    max_dd = 0.0
    for e in equity_curve:
        peak = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak * 100)

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100,
        "final_equity": equity,
        "total_return_pct": (equity - capital) / capital * 100,
        "avg_win_pct": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss_pct": sum(losses) / len(losses) if losses else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "max_drawdown_pct": max_dd,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the trend-following strategy over OHLC CSV data.")
    parser.add_argument("csv_path", help="Path to CSV with columns: date,open,high,low,close")
    parser.add_argument("--fast", type=int, default=20, help="Fast EMA period (default: 20)")
    parser.add_argument("--slow", type=int, default=50, help="Slow EMA period (default: 50)")
    parser.add_argument("--adx-period", type=int, default=14, help="ADX period (default: 14)")
    parser.add_argument("--adx-threshold", type=float, default=25.0, help="Minimum ADX to trust the trend (default: 25)")
    parser.add_argument("--atr-period", type=int, default=14, help="ATR period (default: 14)")
    parser.add_argument("--atr-mult", type=float, default=3.0, help="ATR multiple for the trailing stop (default: 3.0)")
    parser.add_argument("--swing", type=int, default=5, help="Swing detection window (default: 5)")
    parser.add_argument("--capital", type=float, default=10000.0, help="Starting capital (default: 10000)")
    parser.add_argument("--fee", type=float, default=0.0, help="Round-trip fee/slippage in %% of notional (default: 0.0)")
    parser.add_argument("--verbose", action="store_true", help="Print every trade, not just the summary")
    args = parser.parse_args()

    candles = load_candles(args.csv_path)
    trades = run_backtest(
        candles, args.fast, args.slow, args.adx_period, args.adx_threshold,
        args.atr_period, args.atr_mult, args.swing, args.fee,
    )
    stats = summarize(trades, args.capital)

    if args.verbose:
        print(f"{'#':>3} {'Dir':<5} {'Entry':<12} {'@':>10} {'Exit':<12} {'@':>10} {'Bars':>5} {'Reason':<12} {'PnL%':>8}")
        for n, t in enumerate(trades, 1):
            print(
                f"{n:>3} {t['direction']:<5} {t['entry_date']:<12} {t['entry_price']:>10.4f} "
                f"{t['exit_date']:<12} {t['exit_price']:>10.4f} {t['bars_held']:>5} "
                f"{t['exit_reason']:<12} {t['pnl_pct']:>+8.2f}"
            )
        print()

    if stats["trades"] == 0:
        print("No trades were taken over this period.")
        return

    print(f"Trades           : {stats['trades']} ({stats['wins']} win / {stats['losses']} loss)")
    print(f"Win rate         : {stats['win_rate']:.2f}%")
    print(f"Total return     : {stats['total_return_pct']:+.2f}% (equity {args.capital:.2f} -> {stats['final_equity']:.2f})")
    print(f"Avg win / loss   : {stats['avg_win_pct']:+.2f}% / {stats['avg_loss_pct']:+.2f}%")
    profit_factor = "inf" if stats["profit_factor"] == float("inf") else f"{stats['profit_factor']:.2f}"
    print(f"Profit factor    : {profit_factor}")
    print(f"Max drawdown     : {stats['max_drawdown_pct']:.2f}%")


if __name__ == "__main__":
    try:
        main()
    except (FileNotFoundError, ValueError, KeyError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
