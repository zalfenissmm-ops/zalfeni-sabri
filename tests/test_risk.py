import time
import unittest

from mt5_bot.config import Config
from mt5_bot.models import ClosedTrade, Position
from mt5_bot.risk import RiskManager, utc_day_start


class FakeBroker:
    def __init__(self, trades=None):
        self.trades = trades or []

    def closed_trades_since(self, since):
        return [t for t in self.trades if t.time_close >= since]


NOON = time.mktime(time.strptime("2026-06-15 12:00:00", "%Y-%m-%d %H:%M:%S"))


def closed(profit, symbol="EURUSD", ticket=1, at=NOON):
    return ClosedTrade(ticket=ticket, symbol=symbol, time_close=at, profit=profit)


def position(symbol="EURUSD", ticket=1):
    return Position(
        ticket=ticket, symbol=symbol, side="buy", volume=0.02, price_open=1.1,
        sl=1.09, tp=1.11, profit=0.0, time_open=NOON,
    )


class TestDayAccounting(unittest.TestCase):
    def test_day_start_is_utc_midnight(self):
        day, midnight = utc_day_start(NOON)
        self.assertEqual(time.strftime("%H:%M:%S", time.gmtime(midnight)), "00:00:00")
        self.assertEqual(day, time.strftime("%Y-%m-%d", time.gmtime(NOON)))

    def test_realized_pnl_is_recomputed_from_broker_history(self):
        risk = RiskManager(Config(), FakeBroker([closed(1.5, ticket=1), closed(-0.5, ticket=2)]))
        risk.refresh(NOON)
        self.assertAlmostEqual(risk.state.realized_pnl, 1.0)
        self.assertEqual(risk.state.trades, 2)

    def test_refreshing_twice_does_not_double_count(self):
        risk = RiskManager(Config(), FakeBroker([closed(2.0)]))
        risk.refresh(NOON)
        risk.refresh(NOON)
        self.assertAlmostEqual(risk.state.realized_pnl, 2.0)

    def test_a_new_day_resets_the_counters_and_the_halt(self):
        risk = RiskManager(Config(daily_loss_limit_usd=1.0), FakeBroker([closed(-5.0)]))
        risk.refresh(NOON)
        self.assertTrue(risk.day_blocked(NOON))
        risk.broker.trades = []
        risk.refresh(NOON + 86400)
        self.assertEqual(risk.day_blocked(NOON + 86400), "")


class TestDayGuards(unittest.TestCase):
    def test_the_daily_loss_limit_stops_new_trades(self):
        risk = RiskManager(Config(daily_loss_limit_usd=5.0), FakeBroker([closed(-5.0)]))
        risk.refresh(NOON)
        self.assertIn("daily loss limit", risk.day_blocked(NOON))

    def test_a_halt_stays_in_force_even_if_the_pnl_recovers(self):
        broker = FakeBroker([closed(-5.0)])
        risk = RiskManager(Config(daily_loss_limit_usd=5.0), broker)
        risk.refresh(NOON)
        risk.day_blocked(NOON)
        broker.trades.append(closed(9.0, ticket=2))
        risk.refresh(NOON)
        self.assertIn("daily loss limit", risk.day_blocked(NOON))

    def test_the_daily_profit_target_stops_trading_while_ahead(self):
        risk = RiskManager(Config(daily_profit_target_usd=3.0), FakeBroker([closed(3.5)]))
        risk.refresh(NOON)
        self.assertIn("profit target", risk.day_blocked(NOON))

    def test_the_trade_cap_stops_new_trades(self):
        trades = [closed(0.1, ticket=i) for i in range(4)]
        risk = RiskManager(Config(max_trades_per_day=4), FakeBroker(trades))
        risk.refresh(NOON)
        self.assertIn("trade cap", risk.day_blocked(NOON))

    def test_the_remaining_budget_shrinks_with_losses(self):
        risk = RiskManager(Config(daily_loss_limit_usd=10.0), FakeBroker([closed(-4.0)]))
        risk.refresh(NOON)
        self.assertAlmostEqual(risk.remaining_loss_budget(), 6.0)

    def test_profits_do_not_inflate_the_loss_budget(self):
        risk = RiskManager(Config(daily_loss_limit_usd=10.0), FakeBroker([closed(50.0)]))
        risk.refresh(NOON)
        self.assertAlmostEqual(risk.remaining_loss_budget(), 10.0)


class TestSymbolGuards(unittest.TestCase):
    def test_the_global_position_cap_blocks_a_new_symbol(self):
        risk = RiskManager(Config(max_open_positions=1), FakeBroker())
        risk.refresh(NOON)
        self.assertIn("already holding", risk.symbol_blocked("GBPUSD", [position()], NOON))

    def test_a_symbol_is_not_traded_twice_at_once(self):
        cfg = Config(max_open_positions=5, max_positions_per_symbol=1)
        risk = RiskManager(cfg, FakeBroker())
        risk.refresh(NOON)
        self.assertIn("already in EURUSD", risk.symbol_blocked("EURUSD", [position()], NOON))

    def test_the_cooldown_blocks_an_immediate_re_entry(self):
        risk = RiskManager(Config(cooldown_seconds=60), FakeBroker([closed(1.0, at=NOON)]))
        risk.refresh(NOON)
        self.assertIn("cooling down", risk.symbol_blocked("EURUSD", [], NOON + 10))

    def test_the_cooldown_expires(self):
        risk = RiskManager(Config(cooldown_seconds=60), FakeBroker([closed(1.0, at=NOON)]))
        risk.refresh(NOON)
        self.assertEqual(risk.symbol_blocked("EURUSD", [], NOON + 61), "")

    def test_a_cooldown_on_one_symbol_does_not_block_another(self):
        risk = RiskManager(Config(cooldown_seconds=60), FakeBroker([closed(1.0, at=NOON)]))
        risk.refresh(NOON)
        self.assertEqual(risk.symbol_blocked("GBPUSD", [], NOON + 10), "")


if __name__ == "__main__":
    unittest.main()


class TestOpenRisk(unittest.TestCase):
    class SpecBroker(FakeBroker):
        def symbol_spec(self, symbol):
            from tests.helpers import FX_SPEC

            return FX_SPEC

    def manager(self, **overrides):
        return RiskManager(Config(**overrides), self.SpecBroker())

    def test_measures_each_position_from_its_own_stop(self):
        risk = self.manager()
        # 0.02 lot, 100 points of stop distance at $1/point/lot = $2.00.
        held = Position(
            ticket=1, symbol="EURUSD", side="buy", volume=0.02, price_open=1.10000,
            sl=1.09900, tp=1.10100, profit=0.0, time_open=NOON,
        )
        self.assertAlmostEqual(risk.open_risk([held]), 2.0, places=2)

    def test_sums_across_the_basket(self):
        risk = self.manager()
        held = [
            Position(ticket=i, symbol=name, side="sell", volume=0.02, price_open=1.10000,
                     sl=1.10100, tp=1.09900, profit=0.0, time_open=NOON)
            for i, name in enumerate(("EURUSD", "GBPUSD"))
        ]
        self.assertAlmostEqual(risk.open_risk(held), 4.0, places=2)

    def test_a_break_even_stop_risks_only_the_commission(self):
        risk = self.manager(commission_per_lot_usd=7.0)
        held = Position(
            ticket=1, symbol="EURUSD", side="buy", volume=0.02, price_open=1.10000,
            sl=1.10000, tp=1.10100, profit=0.0, time_open=NOON,
        )
        self.assertAlmostEqual(risk.open_risk([held]), 7.0 * 0.02, places=4)

    def test_a_position_without_a_stop_counts_as_the_full_cap(self):
        risk = self.manager(max_loss_per_trade_usd=3.0)
        held = Position(
            ticket=1, symbol="EURUSD", side="buy", volume=0.02, price_open=1.10000,
            sl=0.0, tp=1.10100, profit=0.0, time_open=NOON,
        )
        self.assertAlmostEqual(risk.open_risk([held]), 3.0)

    def test_an_empty_book_risks_nothing(self):
        self.assertEqual(self.manager().open_risk([]), 0.0)
