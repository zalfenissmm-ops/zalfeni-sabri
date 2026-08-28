"""Bot configuration: JSON file on disk, with environment-variable overrides
for the secrets you should not commit (login / password / server)."""

import json
import os
from dataclasses import dataclass, field, fields


@dataclass
class Config:
    # --- MT5 connection (leave empty to attach to an already-running terminal) ---
    login: int = 0
    password: str = ""
    server: str = ""
    terminal_path: str = ""

    # --- Instruments & size ---
    symbols: list[str] = field(default_factory=lambda: ["EURUSD"])
    volume: float = 0.01
    timeframe: str = "M1"

    # --- Profit target, in account currency (USD) ---
    # The per-trade target is picked from volatility and clamped into this band.
    target_profit_min_usd: float = 1.0
    target_profit_max_usd: float = 3.0
    atr_target_fraction: float = 0.5  # aim for this fraction of one ATR
    # Round-turn commission per lot; folded into the target so the *net* result
    # still lands inside the band above.
    commission_per_lot_usd: float = 0.0

    # --- Loss control, in account currency (USD) ---
    stop_loss_ratio: float = 1.0  # SL distance = target x this
    max_loss_per_trade_usd: float = 3.0
    daily_loss_limit_usd: float = 20.0
    # Ceiling on the money at stake across every open position at once. With a
    # basket of symbols this, not the position count, is what bounds a bad hour.
    max_total_risk_usd: float = 6.0
    daily_profit_target_usd: float = 0.0  # 0 = keep trading all day

    # --- Entry filters ---
    ema_fast: int = 8
    ema_slow: int = 21
    rsi_period: int = 14
    rsi_buy_max: float = 70.0  # do not buy into an already overbought push
    rsi_sell_min: float = 30.0
    atr_period: int = 14
    breakout_lookback: int = 3
    use_pullback_entries: bool = True
    use_breakout_entries: bool = True
    # Reject a trade whose target is further than this many ATRs away: it
    # would sit open for hours, which is the opposite of scalping.
    max_target_atr_multiple: float = 2.0
    candles_to_load: int = 200
    max_spread_points: float = 20.0
    # Skip the trade when the spread eats too much of the target distance.
    max_spread_to_target_ratio: float = 0.35

    # --- Position management ---
    max_open_positions: int = 2
    max_positions_per_symbol: int = 1
    max_hold_seconds: int = 900  # cut a trade loose if it stalls (0 = never)
    breakeven_at_fraction: float = 0.6  # move SL to entry once this much of TP is banked
    cooldown_seconds: int = 30  # per symbol, after a trade closes
    max_trades_per_day: int = 0  # 0 = unlimited

    # --- Loop & safety ---
    poll_seconds: float = 1.0
    deviation_points: int = 20
    magic: int = 20260828
    stale_tick_seconds: float = 120.0  # market closed / feed dead -> stand down
    blackout_windows: list[str] = field(default_factory=lambda: ["23:56-00:06"])
    heartbeat_seconds: int = 300  # periodic "still alive" summary (0 = off)
    journal_path: str = "trades.csv"
    log_path: str = "bot.log"

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not self.symbols:
            raise ValueError("config: `symbols` must list at least one instrument")
        if self.volume <= 0:
            raise ValueError("config: `volume` must be > 0")
        if self.target_profit_min_usd <= 0:
            raise ValueError("config: `target_profit_min_usd` must be > 0")
        if self.target_profit_max_usd < self.target_profit_min_usd:
            raise ValueError("config: `target_profit_max_usd` is below the minimum")
        if self.max_loss_per_trade_usd <= 0:
            raise ValueError("config: `max_loss_per_trade_usd` must be > 0")
        if self.daily_loss_limit_usd <= 0:
            raise ValueError("config: `daily_loss_limit_usd` must be > 0")
        if self.max_total_risk_usd <= 0:
            raise ValueError("config: `max_total_risk_usd` must be > 0")
        if self.heartbeat_seconds < 0:
            raise ValueError("config: `heartbeat_seconds` cannot be negative")
        if self.ema_fast >= self.ema_slow:
            raise ValueError("config: `ema_fast` must be shorter than `ema_slow`")
        if self.poll_seconds <= 0:
            raise ValueError("config: `poll_seconds` must be > 0")
        if self.candles_to_load <= max(self.ema_slow, self.rsi_period, self.atr_period) + 2:
            raise ValueError("config: `candles_to_load` is too small for the configured periods")
        if self.breakout_lookback < 1:
            raise ValueError("config: `breakout_lookback` must be >= 1")
        if not (self.use_pullback_entries or self.use_breakout_entries):
            raise ValueError("config: enable at least one of the entry triggers")
        if self.max_target_atr_multiple <= 0:
            raise ValueError("config: `max_target_atr_multiple` must be > 0")
        if not 0 <= self.breakeven_at_fraction <= 1:
            raise ValueError("config: `breakeven_at_fraction` must be between 0 and 1")
        for window in self.blackout_windows:
            parse_blackout_window(window)

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        data: dict = {}
        if path:
            with open(path) as f:
                data = json.load(f)
        data.pop("_comment", None)  # a note to the human editing the file
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"config: unknown key(s): {', '.join(sorted(unknown))}")
        cfg = cls(**data)
        cfg.apply_env_overrides()
        cfg.validate()
        return cfg

    def apply_env_overrides(self) -> None:
        """Credentials belong in the environment, not in a committed JSON file."""
        if login := os.environ.get("MT5_LOGIN"):
            self.login = int(login)
        if password := os.environ.get("MT5_PASSWORD"):
            self.password = password
        if server := os.environ.get("MT5_SERVER"):
            self.server = server
        if path := os.environ.get("MT5_TERMINAL_PATH"):
            self.terminal_path = path


def parse_blackout_window(window: str) -> tuple[int, int]:
    """Parse "HH:MM-HH:MM" into minutes-since-midnight. A window may wrap past
    midnight (the daily rollover blackout does exactly that)."""
    try:
        start_text, end_text = window.split("-")
        start_h, start_m = (int(part) for part in start_text.split(":"))
        end_h, end_m = (int(part) for part in end_text.split(":"))
    except ValueError as exc:
        raise ValueError(f"config: bad blackout window {window!r}, expected HH:MM-HH:MM") from exc
    for hour, minute in ((start_h, start_m), (end_h, end_m)):
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError(f"config: bad time in blackout window {window!r}")
    return start_h * 60 + start_m, end_h * 60 + end_m


def in_blackout(windows: list[str], minutes_since_midnight: int) -> bool:
    for window in windows:
        start, end = parse_blackout_window(window)
        if start <= end:
            if start <= minutes_since_midnight < end:
                return True
        elif minutes_since_midnight >= start or minutes_since_midnight < end:
            return True  # window wraps past midnight
    return False
