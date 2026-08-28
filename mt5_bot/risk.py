"""Risk guards.

The engine asks this module before every entry. It is deliberately the only
place that can say "no more trading today" - the strategy never overrides it.
"""

import time
from dataclasses import dataclass, field

from .config import Config, in_blackout
from .models import ClosedTrade, Position
from .sizing import money_for_points


@dataclass
class DayState:
    day: str = ""
    started_at: float = 0.0
    realized_pnl: float = 0.0
    trades: int = 0
    last_close_at: dict[str, float] = field(default_factory=dict)


def utc_day_start(now: float) -> tuple[str, float]:
    parts = time.gmtime(now)
    midnight = now - (parts.tm_hour * 3600 + parts.tm_min * 60 + parts.tm_sec)
    return time.strftime("%Y-%m-%d", parts), midnight


class RiskManager:
    def __init__(self, cfg: Config, broker):
        self.cfg = cfg
        self.broker = broker
        self.state = DayState()
        self.halted_reason = ""

    def refresh(self, now: float | None = None) -> list[ClosedTrade]:
        """Recompute today's realized P/L from the broker's own trade history.

        Reading the history back each cycle (instead of accumulating locally) keeps
        the numbers right even if the bot restarts mid-session or a position is
        closed by hand in the terminal.
        """
        now = time.time() if now is None else now
        day, midnight = utc_day_start(now)
        if day != self.state.day:
            self.state = DayState(day=day, started_at=midnight)
            self.halted_reason = ""

        trades = self.broker.closed_trades_since(midnight)
        self.state.realized_pnl = sum(t.profit for t in trades)
        self.state.trades = len({t.ticket for t in trades})
        for trade in trades:
            previous = self.state.last_close_at.get(trade.symbol, 0.0)
            self.state.last_close_at[trade.symbol] = max(previous, trade.time_close)
        return trades

    def day_blocked(self, now: float | None = None) -> str:
        """Reasons to stop opening anything at all today. Empty string = clear."""
        now = time.time() if now is None else now
        if self.halted_reason:
            return self.halted_reason

        if self.state.realized_pnl <= -abs(self.cfg.daily_loss_limit_usd):
            self.halted_reason = (
                f"daily loss limit hit ({self.state.realized_pnl:+.2f} USD) - "
                "no new trades until tomorrow"
            )
            return self.halted_reason
        if (
            self.cfg.daily_profit_target_usd > 0
            and self.state.realized_pnl >= self.cfg.daily_profit_target_usd
        ):
            self.halted_reason = (
                f"daily profit target reached ({self.state.realized_pnl:+.2f} USD) - "
                "stopping while ahead"
            )
            return self.halted_reason
        if self.cfg.max_trades_per_day and self.state.trades >= self.cfg.max_trades_per_day:
            return f"daily trade cap reached ({self.state.trades})"

        clock = time.gmtime(now)
        if in_blackout(self.cfg.blackout_windows, clock.tm_hour * 60 + clock.tm_min):
            return "inside a blackout window (rollover / thin liquidity)"
        return ""

    def symbol_blocked(self, symbol: str, positions: list[Position], now: float | None = None) -> str:
        """Per-symbol reasons to skip an entry. Empty string = clear."""
        now = time.time() if now is None else now
        if len(positions) >= self.cfg.max_open_positions:
            return f"already holding {len(positions)} position(s)"
        on_symbol = [p for p in positions if p.symbol == symbol]
        if len(on_symbol) >= self.cfg.max_positions_per_symbol:
            return f"already in {symbol}"

        last_close = self.state.last_close_at.get(symbol, 0.0)
        waited = now - last_close
        if last_close and waited < self.cfg.cooldown_seconds:
            return f"cooling down, {self.cfg.cooldown_seconds - waited:.0f}s left"
        return ""

    def open_risk(self, positions: list[Position]) -> float:
        """Money at stake right now, measured from each position's own stop.

        Commission counts too: a position stopped at break-even still pays it.
        """
        total = 0.0
        for position in positions:
            if not position.sl:
                total += self.cfg.max_loss_per_trade_usd  # unprotected: assume the cap
                continue
            spec = self.broker.symbol_spec(position.symbol)
            if position.is_buy:
                distance = position.price_open - position.sl
            else:
                distance = position.sl - position.price_open
            total += max(0.0, money_for_points(spec, position.volume, distance / spec.point))
            total += self.cfg.commission_per_lot_usd * position.volume
        return total

    def remaining_loss_budget(self) -> float:
        return max(0.0, self.cfg.daily_loss_limit_usd + min(0.0, self.state.realized_pnl))
