#!/usr/bin/env python3
"""
SMC backtester — a simulated trader walking the chart one candle at a time.

The loop is deliberately ordered the way a person trades:

    for each candle:
        1. manage what is already open (stop, partial, breakeven, trail, target)
        2. see whether a resting limit order got filled
        3. only then look for a new setup

A signal found at bar i can therefore never fill before bar i+1, and the signal
engine itself never reads past bar i. No lookahead anywhere.

Money model (defaults match a 100 USD account trading 0.01 lot where one point
of price movement is worth 1 USD — e.g. gold):

    P/L = price_move / pip_size * pip_value * (lot / 0.01)

Usage:
    python3 backtest.py data.csv --balance 100 --lot 0.01 --pip-value 1 --pip-size 1

CSV must have a header row with columns: date,open,high,low,close
The CSV is the *entry* timeframe (15m by default); the 4H bias is resampled
from it with --htf-mult (16 bars of 15m = 4H).
"""

import argparse
import csv
import sys
from dataclasses import dataclass, field
from datetime import datetime

from smc_signals import Signal, SignalConfig, SignalEngine
from trend_identifier import Candle, load_candles

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


@dataclass
class Money:
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

    # ------------------------------------------------------------ managing

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


# ----------------------------------------------------------------- reporting


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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Backtest the SMC strategy in STRATEGY.md, bar by bar.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("csv_path", help="Entry-timeframe CSV: date,open,high,low,close")

    money = p.add_argument_group("account")
    money.add_argument("--balance", type=float, default=100.0, help="Starting balance in USD")
    money.add_argument("--lot", type=float, default=0.01, help="Fixed lot size per trade")
    money.add_argument("--pip-value", type=float, default=1.0, help="USD per pip at 0.01 lot")
    money.add_argument("--pip-size", type=float, default=1.0, help="Price units in one pip")
    money.add_argument("--cost", type=float, default=0.0, help="USD per round turn (spread + commission)")

    rules = p.add_argument_group("strategy")
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

    mgmt = p.add_argument_group("management")
    mgmt.add_argument("--partial-rr", type=float, default=2.0, help="R multiple at which to take partials")
    mgmt.add_argument("--partial-pct", type=float, default=50.0, help="Percent closed at the partial (0 = off)")
    mgmt.add_argument("--no-trail", action="store_true", help="Do not trail the stop after the partial")
    mgmt.add_argument("--order-expiry", type=int, default=20, help="Bars a limit order stays live")

    risk = p.add_argument_group("risk")
    risk.add_argument("--daily-stop", type=float, default=3.0, help="Daily loss limit, percent")
    risk.add_argument("--weekly-stop", type=float, default=6.0, help="Weekly loss limit, percent")
    risk.add_argument("--max-positions", type=int, default=2, help="Concurrent trades and resting orders")
    risk.add_argument("--max-consec-losses", type=int, default=2, help="Consecutive losses that end the day")
    risk.add_argument("--max-risk-pct", type=float, default=0.0, help="Skip trades risking more than this percent (0 = off)")

    p.add_argument("--export", help="Write the trade list to this CSV path")
    return p


def main() -> None:
    args = build_parser().parse_args()
    candles = load_candles(args.csv_path)

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
    money = Money(
        balance=args.balance,
        lot=args.lot,
        pip_value=args.pip_value,
        pip_size=args.pip_size,
        cost=args.cost,
    )
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


if __name__ == "__main__":
    try:
        main()
    except (FileNotFoundError, ValueError, KeyError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
