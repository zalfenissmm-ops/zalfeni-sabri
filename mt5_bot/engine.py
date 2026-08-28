"""The trading loop.

One pass per `poll_seconds`: reconcile what the broker closed since last time,
check the day's risk budget, manage open positions, then look for one new entry
per symbol. The loop is designed to run unattended around the clock, so every
broker failure is caught and retried instead of killing the process.
"""

import logging
import signal
import time

from .broker import BrokerError
from .config import Config
from .journal import Journal
from .models import Position, SymbolSpec
from .risk import RiskManager
from .sizing import build_plan, money_for_points
from . import strategy

log = logging.getLogger("mt5_bot")

_RECONNECT_BACKOFF = [5, 15, 30, 60, 120]


class Engine:
    def __init__(self, cfg: Config, broker, live: bool = False):
        self.cfg = cfg
        self.broker = broker
        self.live = live
        self.risk = RiskManager(cfg, broker)
        self.journal = Journal(cfg.journal_path)
        self.running = False
        self._seen_closes: set[tuple[int, float]] = set()
        self._quiet_reasons: dict[str, str] = {}
        self._quotes: dict[str, tuple[tuple, float]] = {}
        self._idle_symbols: set[str] = set()
        self._seen_day = ""
        self._failures = 0

    # --- lifecycle -------------------------------------------------------

    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._request_stop)

    def _request_stop(self, signum, _frame) -> None:
        log.info("Signal %s received - finishing the current cycle and stopping.", signum)
        self.running = False

    def run(self, max_seconds: float = 0) -> None:
        """Trade until stopped. `max_seconds` > 0 ends the run on a timer,
        which is how the paper mode does a bounded test drive."""
        self.broker.connect()
        self._log_startup()
        deadline = time.time() + max_seconds if max_seconds else 0
        self.running = True
        while self.running:
            if deadline and time.time() >= deadline:
                log.info("Reached the requested run length - stopping.")
                break
            try:
                self.cycle()
                self._failures = 0
            except BrokerError as exc:
                self._handle_broker_error(exc)
            except Exception:  # keep a 24/7 bot alive through unexpected faults
                log.exception("Unexpected error in trading cycle")
            time.sleep(self.cfg.poll_seconds)
        self._shutdown()

    def _handle_broker_error(self, exc: BrokerError) -> None:
        delay = _RECONNECT_BACKOFF[min(self._failures, len(_RECONNECT_BACKOFF) - 1)]
        self._failures += 1
        log.error("Broker error: %s - reconnecting in %ss", exc, delay)
        time.sleep(delay)
        try:
            self.broker.shutdown()
            self.broker.connect()
            log.info("Reconnected to the terminal.")
        except BrokerError as retry_exc:
            log.error("Reconnect failed: %s", retry_exc)

    def _shutdown(self) -> None:
        open_positions = []
        try:
            open_positions = self.broker.positions()
        except BrokerError:
            pass
        if open_positions:
            log.warning(
                "Stopping with %d position(s) still open - their SL/TP stay live on the "
                "broker's server, but nothing is managing the time stop any more.",
                len(open_positions),
            )
        log.info(
            "Session finished. Realized today: %+.2f USD over %d trade(s).",
            self.risk.state.realized_pnl,
            self.risk.state.trades,
        )
        self.broker.shutdown()

    def _log_startup(self) -> None:
        mode = "LIVE - real money" if self.live else "PAPER - simulated fills"
        log.info("Mode          : %s", mode)
        log.info("Symbols       : %s", ", ".join(self.cfg.symbols))
        log.info("Volume        : %.2f lot", self.cfg.volume)
        log.info(
            "Target/trade  : $%.2f - $%.2f (net of commission)",
            self.cfg.target_profit_min_usd,
            self.cfg.target_profit_max_usd,
        )
        log.info(
            "Loss controls : max $%.2f per trade, $%.2f per day",
            self.cfg.max_loss_per_trade_usd,
            self.cfg.daily_loss_limit_usd,
        )
        log.info("Equity        : %.2f", self.broker.equity())

    # --- one pass --------------------------------------------------------

    def cycle(self) -> None:
        now = time.time()
        self._record_closes(self.risk.refresh(now))

        for position in self.broker.positions():
            self.manage(position, now)

        # Positions can vanish mid-pass: the broker fills a TP or SL, or the
        # time stop above closes one. Re-read before looking for entries so a
        # fresh exit arms its cooldown and cannot be re-entered on this pass.
        self._record_closes(self.risk.refresh(now))
        positions = self.broker.positions()

        blocked = self.risk.day_blocked(now)
        if blocked:
            self._say_once("day", f"Not opening trades: {blocked}")
            return
        self._quiet_reasons.pop("day", None)

        for symbol in self.cfg.symbols:
            try:
                self.consider(symbol, positions, now)
            except BrokerError:
                raise
            except Exception:
                log.exception("Failed while evaluating %s", symbol)

    def _record_closes(self, trades) -> None:
        if self.risk.state.day != self._seen_day:
            self._seen_day = self.risk.state.day
            self._seen_closes.clear()
        for trade in trades:
            key = (trade.ticket, trade.time_close)
            if key in self._seen_closes:
                continue
            self._seen_closes.add(key)
            log.info(
                "CLOSED %s #%s  %+.2f USD  |  day %+.2f USD over %d trade(s)",
                trade.symbol, trade.ticket, trade.profit,
                self.risk.state.realized_pnl, self.risk.state.trades,
            )
            self.journal.write(
                "close", symbol=trade.symbol, profit_usd=round(trade.profit, 2),
                note=f"ticket {trade.ticket}",
            )

    # --- managing what is already open -----------------------------------

    def manage(self, position: Position, now: float) -> None:
        """Apply the time stop and the break-even move to one open position."""
        spec = self.broker.symbol_spec(position.symbol)
        held = now - position.time_open

        if self.cfg.max_hold_seconds and held > self.cfg.max_hold_seconds:
            result = self.broker.close_position(position.ticket)
            log.info(
                "TIME STOP %s #%s after %.0fs at %+.2f USD (%s)",
                position.symbol, position.ticket, held, position.profit,
                "closed" if result.ok else result.comment,
            )
            self.journal.write(
                "time_stop", symbol=position.symbol, side=position.side,
                volume=position.volume, profit_usd=round(position.profit, 2),
                note=f"held {held:.0f}s",
            )
            return

        self._try_breakeven(position, spec)

    def _try_breakeven(self, position: Position, spec: SymbolSpec) -> None:
        """Once most of the target is banked, stop letting the trade turn red."""
        if not self.cfg.breakeven_at_fraction or not position.tp:
            return
        already_safe = (
            position.sl >= position.price_open if position.is_buy else 0 < position.sl <= position.price_open
        )
        if already_safe:
            return

        target_points = abs(position.tp - position.price_open) / spec.point
        target_money = money_for_points(spec, position.volume, target_points)
        if target_money <= 0 or position.profit < target_money * self.cfg.breakeven_at_fraction:
            return

        quote = self.broker.tick(position.symbol)
        if quote is None:
            return
        # The broker rejects a stop placed inside its freeze/stops band.
        market = quote.bid if position.is_buy else quote.ask
        min_distance = max(spec.stops_level_points, spec.freeze_level_points) * spec.point
        if abs(market - position.price_open) <= min_distance:
            return

        result = self.broker.modify_position(position.ticket, position.price_open, position.tp)
        if result.ok:
            log.info(
                "BREAK-EVEN %s #%s  SL -> %.*f (locked at %+.2f USD)",
                position.symbol, position.ticket, spec.digits, position.price_open, position.profit,
            )
        else:
            log.debug("Break-even move rejected for #%s: %s", position.ticket, result.comment)

    # --- looking for a new entry -----------------------------------------

    def consider(self, symbol: str, positions: list[Position], now: float) -> None:
        blocked = self.risk.symbol_blocked(symbol, positions, now)
        if blocked:
            self._say_once(f"risk:{symbol}", f"{symbol}: {blocked}")
            return

        quote = self.broker.tick(symbol)
        if quote is None:
            self._say_once(f"quote:{symbol}", f"{symbol}: no quote from the terminal")
            return
        if not self._feed_is_live(symbol, quote, now):
            if symbol not in self._idle_symbols:
                log.info(
                    "%s: quote unchanged for over %.0fs - market looks closed, standing by.",
                    symbol, self.cfg.stale_tick_seconds,
                )
                self._idle_symbols.add(symbol)
            return
        self._idle_symbols.discard(symbol)

        spec = self.broker.symbol_spec(symbol)
        volume = spec.round_volume(self.cfg.volume)
        candles = self.broker.candles(symbol, self.cfg.timeframe, self.cfg.candles_to_load)
        signal = strategy.evaluate(self.cfg, candles)
        if not signal.actionable:
            self._say_once(f"signal:{symbol}", f"{symbol}: {signal.reason}")
            return

        plan, why_not = build_plan(
            self.cfg, spec, signal.side, quote.bid, quote.ask, signal.atr, volume
        )
        if plan is None:
            self._say_once(f"plan:{symbol}", f"{symbol}: skipping {signal.side} - {why_not}")
            return
        if plan.risk_usd > self.risk.remaining_loss_budget():
            self._say_once(
                f"budget:{symbol}",
                f"{symbol}: ${plan.risk_usd:.2f} risk exceeds the "
                f"${self.risk.remaining_loss_budget():.2f} left in today's budget",
            )
            return

        self._quiet_reasons.clear()
        self.open_trade(symbol, spec, plan, signal.reason)

    def open_trade(self, symbol: str, spec: SymbolSpec, plan, reason: str) -> None:
        result = self.broker.open_position(
            symbol, plan.side, plan.volume, plan.sl, plan.tp, f"scalp {plan.target_usd:.2f}"
        )
        if not result.ok:
            log.warning("%s: order rejected - %s", symbol, result.comment)
            self.journal.write("reject", symbol=symbol, side=plan.side, note=result.comment)
            return

        log.info(
            "OPEN %s %s %.2f @ %.*f | TP %.*f (+$%.2f) SL %.*f (-$%.2f) | %s",
            plan.side.upper(), symbol, plan.volume, spec.digits, result.price,
            spec.digits, plan.tp, plan.target_usd,
            spec.digits, plan.sl, plan.risk_usd, reason,
        )
        self.journal.write(
            "open", symbol=symbol, side=plan.side, volume=plan.volume,
            price=round(result.price, spec.digits), sl=plan.sl, tp=plan.tp,
            target_usd=round(plan.target_usd, 2), risk_usd=round(plan.risk_usd, 2), note=reason,
        )

    def _feed_is_live(self, symbol: str, quote, now: float) -> bool:
        """Is this symbol still quoting?

        Tick timestamps come from the broker's own clock, so they are compared
        against each other rather than against ours: a quote that has not moved
        for `stale_tick_seconds` means the session is closed.
        """
        fingerprint = (quote.time, quote.bid, quote.ask)
        seen = self._quotes.get(symbol)
        if seen is None or seen[0] != fingerprint:
            self._quotes[symbol] = (fingerprint, now)
            return True
        return now - seen[1] <= self.cfg.stale_tick_seconds

    def _say_once(self, key: str, message: str) -> None:
        """Log a standing reason once instead of every second of the poll loop."""
        if self._quiet_reasons.get(key) != message:
            self._quiet_reasons[key] = message
            log.info(message)
