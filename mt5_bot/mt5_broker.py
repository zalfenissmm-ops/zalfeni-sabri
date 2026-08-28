"""Live broker backed by the MetaTrader 5 terminal.

The `MetaTrader5` package only exists on Windows, so it is imported lazily:
the rest of the bot (and the whole test suite) runs anywhere.
"""

import time

from .broker import BrokerError
from .config import Config
from .models import Candle, ClosedTrade, OrderResult, Position, SymbolSpec, Tick

_TIMEFRAMES = {
    "M1": "TIMEFRAME_M1",
    "M2": "TIMEFRAME_M2",
    "M3": "TIMEFRAME_M3",
    "M5": "TIMEFRAME_M5",
    "M15": "TIMEFRAME_M15",
    "M30": "TIMEFRAME_M30",
    "H1": "TIMEFRAME_H1",
    "H4": "TIMEFRAME_H4",
    "D1": "TIMEFRAME_D1",
}

# Brokers run their servers on their own timezone, never further from UTC
# than this. A larger apparent gap means the feed is stale (weekend, holiday),
# not that the broker moved to Mars - so the measurement is discarded.
_MAX_SERVER_OFFSET = 14 * 3600
_OFFSET_GRANULARITY = 900  # timezones land on quarter-hour boundaries
_HISTORY_MARGIN = 6 * 3600  # widen history queries to survive a wrong offset

# Retcodes worth a second attempt: the price moved between quote and send.
_RETRYABLE = {10004, 10020, 10021}  # REQUOTE, PRICE_CHANGED, PRICE_OFF
_ORDER_RETRIES = 3


def load_mt5():
    try:
        import MetaTrader5 as mt5  # noqa: N813 - upstream package name
    except ImportError as exc:  # pragma: no cover - depends on the host OS
        raise BrokerError(
            "The `MetaTrader5` package is not installed. It is Windows-only; "
            "run the bot on the machine hosting your MT5 terminal, or use --paper."
        ) from exc
    return mt5


class Mt5Broker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.mt5 = None
        self._specs: dict[str, SymbolSpec] = {}
        self._filling: dict[str, int] = {}
        # Server time minus UTC, in seconds. Everything this class hands out is
        # converted to UTC so the engine only ever reasons in one timezone.
        self.server_offset = 0.0

    # --- lifecycle -------------------------------------------------------

    def connect(self) -> None:
        self.mt5 = load_mt5()
        kwargs = {}
        if self.cfg.terminal_path:
            kwargs["path"] = self.cfg.terminal_path
        if self.cfg.login:
            kwargs.update(
                login=self.cfg.login, password=self.cfg.password, server=self.cfg.server
            )
        if not self.mt5.initialize(**kwargs):
            raise BrokerError(f"MT5 initialize failed: {self.mt5.last_error()}")

        account = self.mt5.account_info()
        if account is None:
            raise BrokerError(f"MT5 has no account session: {self.mt5.last_error()}")
        if not account.trade_allowed:
            raise BrokerError(
                "Algo trading is disabled for this account/terminal. Enable "
                "'Algo Trading' in the MT5 toolbar and check the account permissions."
            )
        for symbol in self.cfg.symbols:
            self.symbol_spec(symbol)  # fails fast on a typo in the symbol name

    def shutdown(self) -> None:
        if self.mt5 is not None:
            self.mt5.shutdown()
            self.mt5 = None

    def _api(self):
        if self.mt5 is None:
            raise BrokerError("Broker is not connected")
        return self.mt5

    # --- market data -----------------------------------------------------

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        if symbol in self._specs:
            return self._specs[symbol]
        mt5 = self._api()
        info = mt5.symbol_info(symbol)
        if info is None:
            raise BrokerError(f"Unknown symbol {symbol!r} on this account")
        if not info.visible and not mt5.symbol_select(symbol, True):
            raise BrokerError(f"Could not add {symbol!r} to Market Watch")
        info = mt5.symbol_info(symbol)

        spec = SymbolSpec(
            name=info.name,
            digits=info.digits,
            point=info.point,
            tick_size=info.trade_tick_size or info.point,
            tick_value=info.trade_tick_value,
            volume_min=info.volume_min,
            volume_max=info.volume_max,
            volume_step=info.volume_step,
            stops_level_points=info.trade_stops_level,
            freeze_level_points=info.trade_freeze_level,
        )
        if spec.tick_value <= 0:
            raise BrokerError(
                f"{symbol}: broker reports tick_value={spec.tick_value}; the dollar "
                "target cannot be computed. Open the symbol in Market Watch first."
            )
        self._specs[symbol] = spec
        self._filling[symbol] = self._filling_mode(info)
        return spec

    def _filling_mode(self, info) -> int:
        """Pick a filling mode the symbol actually accepts."""
        mt5 = self._api()
        if info.filling_mode & 2:  # SYMBOL_FILLING_IOC
            return mt5.ORDER_FILLING_IOC
        if info.filling_mode & 1:  # SYMBOL_FILLING_FOK
            return mt5.ORDER_FILLING_FOK
        return mt5.ORDER_FILLING_RETURN

    def tick(self, symbol: str) -> Tick | None:
        raw = self._api().symbol_info_tick(symbol)
        if raw is None or raw.bid <= 0 or raw.ask <= 0:
            return None
        self._measure_server_offset(raw.time)
        return Tick(time=raw.time - self.server_offset, bid=raw.bid, ask=raw.ask)

    def _measure_server_offset(self, server_time: float) -> None:
        """Re-measure the broker's timezone from a live tick.

        A stale tick (market closed) would read as a huge offset, so anything
        beyond a real timezone is ignored and the last good value stands.
        """
        drift = server_time - time.time()
        if abs(drift) <= _MAX_SERVER_OFFSET:
            self.server_offset = round(drift / _OFFSET_GRANULARITY) * _OFFSET_GRANULARITY

    def candles(self, symbol: str, timeframe: str, count: int) -> list[Candle]:
        mt5 = self._api()
        name = _TIMEFRAMES.get(timeframe.upper())
        if name is None:
            raise BrokerError(f"Unsupported timeframe {timeframe!r}")
        rates = mt5.copy_rates_from_pos(symbol, getattr(mt5, name), 0, count)
        if rates is None:
            return []
        return [
            Candle(
                time=float(r["time"]) - self.server_offset,
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
            )
            for r in rates
        ]

    # --- account state ---------------------------------------------------

    def equity(self) -> float:
        account = self._api().account_info()
        return float(account.equity) if account else 0.0

    def positions(self) -> list[Position]:
        mt5 = self._api()
        raw = mt5.positions_get()
        if raw is None:
            return []
        return [
            Position(
                ticket=p.ticket,
                symbol=p.symbol,
                side="buy" if p.type == mt5.POSITION_TYPE_BUY else "sell",
                volume=p.volume,
                price_open=p.price_open,
                sl=p.sl,
                tp=p.tp,
                profit=p.profit + p.swap,
                time_open=float(p.time) - self.server_offset,
            )
            for p in raw
            if p.magic == self.cfg.magic
        ]

    def closed_trades_since(self, since: float) -> list[ClosedTrade]:
        """Closed deals since a UTC timestamp.

        The query window is widened on both sides and the results are filtered
        after converting to UTC, so a slightly wrong offset costs an extra row
        to read rather than a missing trade in the day's P/L.
        """
        mt5 = self._api()
        window_start = int(since + self.server_offset - _HISTORY_MARGIN)
        window_end = int(time.time() + self.server_offset + _HISTORY_MARGIN)
        deals = mt5.history_deals_get(window_start, window_end)
        if deals is None:
            return []

        trades = []
        for deal in deals:
            if deal.magic != self.cfg.magic:
                continue
            if deal.entry not in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT):
                continue
            closed_at = float(deal.time) - self.server_offset
            if closed_at < since:
                continue
            trades.append(
                ClosedTrade(
                    ticket=deal.position_id,
                    symbol=deal.symbol,
                    time_close=closed_at,
                    profit=deal.profit + deal.commission + deal.swap,
                )
            )
        return trades

    # --- trading ---------------------------------------------------------

    def open_position(
        self, symbol: str, side: str, volume: float, sl: float, tp: float, comment: str
    ) -> OrderResult:
        mt5 = self._api()
        spec = self.symbol_spec(symbol)
        order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL

        for attempt in range(_ORDER_RETRIES):
            quote = self.tick(symbol)
            if quote is None:
                return OrderResult(ok=False, comment="no quote available")
            price = quote.ask if side == "buy" else quote.bid
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": volume,
                "type": order_type,
                "price": spec.round_price(price),
                "sl": spec.round_price(sl),
                "tp": spec.round_price(tp),
                "deviation": self.cfg.deviation_points,
                "magic": self.cfg.magic,
                "comment": comment[:31],  # MT5 truncates past 31 chars
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self._filling[symbol],
            }
            result = mt5.order_send(request)
            if result is None:
                return OrderResult(ok=False, comment=f"order_send returned None: {mt5.last_error()}")
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                return OrderResult(
                    ok=True,
                    ticket=result.order,
                    price=result.price,
                    retcode=result.retcode,
                    comment=result.comment,
                )
            if result.retcode not in _RETRYABLE or attempt == _ORDER_RETRIES - 1:
                return OrderResult(
                    ok=False, retcode=result.retcode, comment=f"{result.retcode}: {result.comment}"
                )
        return OrderResult(ok=False, comment="retries exhausted")

    def close_position(self, ticket: int) -> OrderResult:
        mt5 = self._api()
        found = [p for p in self.positions() if p.ticket == ticket]
        if not found:
            return OrderResult(ok=False, comment=f"position {ticket} is already gone")
        position = found[0]
        spec = self.symbol_spec(position.symbol)

        for attempt in range(_ORDER_RETRIES):
            quote = self.tick(position.symbol)
            if quote is None:
                return OrderResult(ok=False, comment="no quote available")
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": position.symbol,
                "volume": position.volume,
                "type": mt5.ORDER_TYPE_SELL if position.is_buy else mt5.ORDER_TYPE_BUY,
                "position": ticket,
                "price": spec.round_price(quote.bid if position.is_buy else quote.ask),
                "deviation": self.cfg.deviation_points,
                "magic": self.cfg.magic,
                "comment": "close",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self._filling[position.symbol],
            }
            result = mt5.order_send(request)
            if result is None:
                return OrderResult(ok=False, comment=f"order_send returned None: {mt5.last_error()}")
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                return OrderResult(ok=True, ticket=ticket, price=result.price, retcode=result.retcode)
            if result.retcode not in _RETRYABLE or attempt == _ORDER_RETRIES - 1:
                return OrderResult(
                    ok=False, retcode=result.retcode, comment=f"{result.retcode}: {result.comment}"
                )
        return OrderResult(ok=False, comment="retries exhausted")

    def modify_position(self, ticket: int, sl: float, tp: float) -> OrderResult:
        mt5 = self._api()
        found = [p for p in self.positions() if p.ticket == ticket]
        if not found:
            return OrderResult(ok=False, comment=f"position {ticket} is already gone")
        spec = self.symbol_spec(found[0].symbol)
        result = mt5.order_send(
            {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": found[0].symbol,
                "position": ticket,
                "sl": spec.round_price(sl),
                "tp": spec.round_price(tp),
                "magic": self.cfg.magic,
            }
        )
        if result is None:
            return OrderResult(ok=False, comment=f"order_send returned None: {mt5.last_error()}")
        ok = result.retcode == mt5.TRADE_RETCODE_DONE
        return OrderResult(ok=ok, ticket=ticket, retcode=result.retcode, comment=result.comment)
