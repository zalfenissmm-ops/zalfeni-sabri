#!/usr/bin/env python3
"""Entry point for the MT5 scalping bot.

    python3 run_bot.py --paper --speed 120 --duration 60     # test drive, no money
    python3 run_bot.py --config config.json --live           # real account

Paper mode is the default on purpose: `--live` is the only way to send a real
order, and it asks for confirmation unless you pass --yes.
"""

import argparse
import logging
import sys

from mt5_bot.broker import BrokerError
from mt5_bot.config import Config
from mt5_bot.engine import Engine
from mt5_bot.indicators import atr
from mt5_bot.mt5_broker import Mt5Broker
from mt5_bot.paper_broker import Mt5Feed, PaperBroker, SyntheticFeed
from mt5_bot.preflight import assess, report


def setup_logging(path: str, verbose: bool) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if path:
        handlers.append(logging.FileHandler(path, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="Path to a JSON config file")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="Trade the real account")
    mode.add_argument("--paper", action="store_true", help="Simulate fills (the default)")
    parser.add_argument("--yes", action="store_true", help="Skip the live-trading confirmation prompt")
    parser.add_argument(
        "--feed",
        choices=("synthetic", "mt5"),
        default="synthetic",
        help="Paper-mode price source: a random walk, or live MT5 quotes (default: synthetic)",
    )
    parser.add_argument(
        "--speed", type=float, default=1.0,
        help="Synthetic feed time compression, e.g. 120 = two simulated minutes per second",
    )
    parser.add_argument("--duration", type=float, default=0, help="Stop after N seconds (0 = run forever)")
    parser.add_argument("--symbols", help="Comma-separated symbols, overriding the config")
    parser.add_argument("--volume", type=float, help="Lot size, overriding the config")
    parser.add_argument("--target-min", type=float, help="Minimum profit target per trade, in USD")
    parser.add_argument("--target-max", type=float, help="Maximum profit target per trade, in USD")
    parser.add_argument(
        "--check", action="store_true",
        help=(
            "Report whether the target band is reachable on each symbol, then exit. "
            "Pair with --feed mt5 to measure your broker's real spread and volatility."
        ),
    )
    parser.add_argument("--verbose", action="store_true", help="Debug-level logging")
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> Config:
    cfg = Config.load(args.config)
    if args.symbols:
        cfg.symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if args.volume is not None:
        cfg.volume = args.volume
    if args.target_min is not None:
        cfg.target_profit_min_usd = args.target_min
    if args.target_max is not None:
        cfg.target_profit_max_usd = args.target_max
    cfg.validate()
    return cfg


def confirm_live(cfg: Config) -> bool:
    print("\n" + "=" * 68)
    print("  LIVE TRADING - this will send real orders to a real account.")
    print(f"  Symbols: {', '.join(cfg.symbols)}   Volume: {cfg.volume} lot")
    print(f"  Daily loss limit: ${cfg.daily_loss_limit_usd:.2f}")
    print("  Run it on a demo account first, for at least a full week.")
    print("=" * 68)
    try:
        return input("Type LIVE to continue: ").strip() == "LIVE"
    except (EOFError, KeyboardInterrupt):
        return False


def build_broker(cfg: Config, args: argparse.Namespace):
    if args.live:
        return Mt5Broker(cfg)
    if args.feed == "mt5":
        return PaperBroker(cfg, Mt5Feed(Mt5Broker(cfg)))
    return PaperBroker(cfg, SyntheticFeed(speed=args.speed))


def run_preflight(cfg: Config, broker, from_mt5: bool) -> int:
    """Print, per symbol, whether the configured dollar target is reachable.

    The data source is stated up front: sizing a real account off the synthetic
    feed's spread and volatility would be sizing it off invented numbers.
    """
    if from_mt5:
        print("Data source: live MT5 quotes from your broker.\n")
    else:
        print(
            "Data source: the SYNTHETIC random walk, not your broker.\n"
            "Re-run with --feed mt5 (or --live) before sizing a real account.\n"
        )
    broker.connect()
    try:
        for symbol in cfg.symbols:
            spec = broker.symbol_spec(symbol)
            quote = broker.tick(symbol)
            candles = broker.candles(symbol, cfg.timeframe, cfg.candles_to_load)
            volatility = atr(candles[:-1], cfg.atr_period)
            if quote is None or not volatility or volatility[-1] is None:
                print(f"--- {symbol} ---\n  no data yet (market closed, or symbol not in Market Watch)")
                continue
            spread_points = quote.spread_points(spec)
            atr_points = volatility[-1] / spec.point
            print(report(cfg, assess(cfg, spec, spread_points, atr_points)))
            print()
    finally:
        broker.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        cfg = build_config(args)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if args.check:
        try:
            return run_preflight(cfg, build_broker(cfg, args), args.live or args.feed == "mt5")
        except BrokerError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    if args.live and not args.yes and not confirm_live(cfg):
        print("Cancelled.")
        return 1

    setup_logging(cfg.log_path, args.verbose)
    engine = Engine(cfg, build_broker(cfg, args), live=args.live)
    engine.install_signal_handlers()
    try:
        engine.run(max_seconds=args.duration)
    except BrokerError as exc:
        logging.getLogger("mt5_bot").error("%s", exc)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
