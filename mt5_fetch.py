#!/usr/bin/env python3
"""
MT5 Data Fetch — pulls OHLC candles directly from a running MetaTrader 5
terminal and writes them to a CSV in the date,open,high,low,close format
used by trend_identifier.py, smc_strategy.py and the trend-following tools.

Requires:
  - The MetaTrader 5 terminal installed and running on THIS machine
    (Windows, or Wine) with the account you want to read logged in.
  - pip install MetaTrader5

Usage:
    python3 mt5_fetch.py --symbol EURUSD --timeframe H1 --count 1000 --out eurusd_h1.csv
    python3 mt5_fetch.py --symbol EURUSD --timeframe M15 --from 2024-01-01 --to 2024-06-01 --out data.csv

    # Log into a specific account instead of using the terminal's current session:
    python3 mt5_fetch.py --symbol XAUUSD --timeframe H4 --count 2000 --out xauusd_h4.csv \
        --login 12345678 --password "..." --server "Broker-Server"

Timeframes: M1 M2 M3 M4 M5 M6 M10 M12 M15 M20 M30 H1 H2 H3 H4 H6 H8 H12 D1 W1 MN1
"""

import argparse
import csv
import sys
from datetime import datetime, timezone

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None


TIMEFRAME_NAMES = [
    "M1", "M2", "M3", "M4", "M5", "M6", "M10", "M12", "M15", "M20", "M30",
    "H1", "H2", "H3", "H4", "H6", "H8", "H12", "D1", "W1", "MN1",
]


def _timeframes() -> dict:
    return {name: getattr(mt5, f"TIMEFRAME_{name}") for name in TIMEFRAME_NAMES}


def connect(login: int | None, password: str | None, server: str | None, path: str | None) -> None:
    if mt5 is None:
        raise RuntimeError("MetaTrader5 package not installed. Run: pip install MetaTrader5 (Windows only)")

    kwargs = {}
    if path:
        kwargs["path"] = path
    if login:
        kwargs["login"] = login
    if password:
        kwargs["password"] = password
    if server:
        kwargs["server"] = server

    if not mt5.initialize(**kwargs):
        code, desc = mt5.last_error()
        raise RuntimeError(f"MT5 initialize() failed: [{code}] {desc}")


def fetch_rates(symbol: str, timeframe: str, count: int, date_from: str | None, date_to: str | None):
    if not mt5.symbol_select(symbol, True):
        code, desc = mt5.last_error()
        raise ValueError(f"Symbol '{symbol}' not available: [{code}] {desc}")

    tf = _timeframes()[timeframe]
    if date_from and date_to:
        start = datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(date_to).replace(tzinfo=timezone.utc)
        rates = mt5.copy_rates_range(symbol, tf, start, end)
    else:
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)

    if rates is None or len(rates) == 0:
        code, desc = mt5.last_error()
        raise ValueError(f"No data returned for {symbol}/{timeframe}: [{code}] {desc}")

    return rates


def rates_to_rows(rates) -> list[list[str]]:
    rows = []
    for r in rates:
        dt = datetime.fromtimestamp(int(r["time"]), tz=timezone.utc)
        rows.append([
            dt.strftime("%Y-%m-%d %H:%M:%S"),
            f"{r['open']:.5f}",
            f"{r['high']:.5f}",
            f"{r['low']:.5f}",
            f"{r['close']:.5f}",
        ])
    return rows


def write_csv(rows: list[list[str]], out_path: str) -> None:
    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "open", "high", "low", "close"])
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch OHLC candles from a running MT5 terminal into a CSV.")
    parser.add_argument("--symbol", required=True, help="Symbol name as it appears in MT5, e.g. EURUSD")
    parser.add_argument("--timeframe", required=True, choices=TIMEFRAME_NAMES, help="Candle timeframe")
    parser.add_argument("--count", type=int, default=500, help="Number of most recent bars (default: 500, ignored if --from/--to given)")
    parser.add_argument("--from", dest="date_from", help="Start date (YYYY-MM-DD), UTC")
    parser.add_argument("--to", dest="date_to", help="End date (YYYY-MM-DD), UTC")
    parser.add_argument("--out", required=True, help="Output CSV path")
    parser.add_argument("--login", type=int, help="MT5 account login (omit to use the terminal's already-logged-in session)")
    parser.add_argument("--password", help="MT5 account password (required with --login)")
    parser.add_argument("--server", help="MT5 broker server name (required with --login)")
    parser.add_argument("--path", help="Path to terminal64.exe, if MT5 isn't already running")
    args = parser.parse_args()

    if bool(args.date_from) != bool(args.date_to):
        parser.error("--from and --to must be given together")

    connect(args.login, args.password, args.server, args.path)
    try:
        rates = fetch_rates(args.symbol, args.timeframe, args.count, args.date_from, args.date_to)
        rows = rates_to_rows(rates)
        write_csv(rows, args.out)
        print(f"Wrote {len(rows)} candles ({args.symbol} {args.timeframe}) to {args.out}")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
