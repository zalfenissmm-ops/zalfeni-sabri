"""End-to-end tests: the real engine, the real strategy, simulated fills."""

import os
import tempfile
import time
import unittest

from mt5_bot.config import Config
from mt5_bot.engine import Engine
from mt5_bot.models import ClosedTrade
from mt5_bot.paper_broker import PaperBroker
from tests.helpers import FX_SPEC, ScriptedFeed, candles_from, wave


# Volatility matters as much as direction here: the engine refuses a target it
# cannot reach inside `max_target_atr_multiple` ATRs, so the fixture has to move
# like a real session (~34 points of ATR) rather than drift a point at a time.
_STEP = 0.00040


def breakout_bars():
    """An uptrend that stalls for two bars, then breaks out on the last closed one."""
    base = wave(up=_STEP, down=_STEP * 0.9)
    stall = [round(base[-1] + 0.00008, 5), round(base[-1] + 0.00004, 5)]
    breakout = round(max(stall) + 0.00040, 5)
    return candles_from(base + stall + [breakout, breakout], wick=0.0)


class EngineHarness(unittest.TestCase):
    def build(self, **overrides):
        journal = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
        journal.close()
        self.addCleanup(os.unlink, journal.name)

        settings = dict(
            volume=0.02,
            target_profit_min_usd=1.0,
            target_profit_max_usd=1.0,
            max_loss_per_trade_usd=1.0,
            use_pullback_entries=False,
            cooldown_seconds=0,
            journal_path=journal.name,
            log_path="",
        )
        settings.update(overrides)
        cfg = Config(**settings)
        feed = ScriptedFeed(breakout_bars(), bid=1.10160, ask=1.10170)
        broker = PaperBroker(cfg, feed, starting_balance=1000.0)
        return cfg, feed, broker, Engine(cfg, broker)


class TestEntry(EngineHarness):
    def test_a_signal_opens_a_position_with_a_dollar_sized_tp(self):
        cfg, feed, broker, engine = self.build()
        engine.cycle()

        positions = broker.positions()
        self.assertEqual(len(positions), 1)
        held = positions[0]
        self.assertEqual(held.side, "buy")
        # $1.00 on 0.02 lot at $1/point/lot is 50 points.
        self.assertAlmostEqual((held.tp - held.price_open) / FX_SPEC.point, 50, delta=1)
        self.assertAlmostEqual((held.price_open - held.sl) / FX_SPEC.point, 50, delta=1)

    def test_the_position_cap_stops_a_second_entry(self):
        cfg, feed, broker, engine = self.build(max_open_positions=1)
        engine.cycle()
        engine.cycle()
        self.assertEqual(len(broker.positions()), 1)

    def test_a_wide_spread_blocks_the_entry(self):
        cfg, feed, broker, engine = self.build(max_spread_points=5)
        engine.cycle()
        self.assertEqual(broker.positions(), [])

    def test_a_halted_day_blocks_the_entry(self):
        cfg, feed, broker, engine = self.build(daily_loss_limit_usd=1.0)
        broker.balance -= 5.0
        broker._closed.append(  # a loss already booked today
            ClosedTrade(ticket=99, symbol="EURUSD", time_close=time.time(), profit=-5.0)
        )
        engine.cycle()
        self.assertEqual(broker.positions(), [])
        self.assertIn("daily loss limit", engine.risk.day_blocked())

    def test_a_quote_that_stops_moving_stands_the_bot_down(self):
        """A frozen quote means a closed session, whatever the broker's clock says."""
        cfg, feed, broker, engine = self.build(
            max_positions_per_symbol=2, stale_tick_seconds=0
        )
        engine.cycle()  # first sighting of this quote counts as live
        engine.cycle()  # same quote, now past the staleness threshold
        self.assertEqual(len(broker.positions()), 1)

    def test_a_moving_quote_keeps_trading(self):
        cfg, feed, broker, engine = self.build(
            max_positions_per_symbol=2, stale_tick_seconds=0
        )
        engine.cycle()
        feed.move_to(bid=feed.bid + 0.00001, ask=feed.ask + 0.00001)
        engine.cycle()
        self.assertEqual(len(broker.positions()), 2)

    def test_feed_liveness_tracks_quote_changes_not_the_local_clock(self):
        cfg, feed, broker, engine = self.build(stale_tick_seconds=30)
        quote = feed.tick("EURUSD")

        self.assertTrue(engine._feed_is_live("EURUSD", quote, 1000.0))
        self.assertTrue(engine._feed_is_live("EURUSD", quote, 1020.0))
        self.assertFalse(engine._feed_is_live("EURUSD", quote, 1040.0))

        feed.move_to(bid=feed.bid + 0.00002, ask=feed.ask + 0.00002)
        self.assertTrue(engine._feed_is_live("EURUSD", feed.tick("EURUSD"), 1040.0))


class TestManagement(EngineHarness):
    def test_price_reaching_the_target_books_the_configured_profit(self):
        cfg, feed, broker, engine = self.build(cooldown_seconds=300)
        engine.cycle()
        target = broker.positions()[0].tp

        feed.move_to(bid=target + 0.00001, ask=target + 0.00002)
        engine.cycle()

        self.assertEqual(broker.positions(), [])
        self.assertAlmostEqual(engine.risk.state.realized_pnl, 1.0, delta=0.05)

    def test_price_reaching_the_stop_caps_the_loss(self):
        cfg, feed, broker, engine = self.build(cooldown_seconds=300)
        engine.cycle()
        stop = broker.positions()[0].sl

        feed.move_to(bid=stop - 0.00001, ask=stop)
        engine.cycle()

        self.assertEqual(broker.positions(), [])
        self.assertAlmostEqual(engine.risk.state.realized_pnl, -1.0, delta=0.05)

    def test_the_time_stop_closes_a_stalled_trade(self):
        cfg, feed, broker, engine = self.build(max_hold_seconds=1, cooldown_seconds=300)
        engine.cycle()
        ticket = broker.positions()[0].ticket
        broker._positions[ticket].time_open -= 60

        engine.cycle()
        self.assertEqual(broker.positions(), [])

    def test_break_even_moves_the_stop_to_entry_once_ahead(self):
        cfg, feed, broker, engine = self.build(breakeven_at_fraction=0.5, max_hold_seconds=0)
        engine.cycle()
        held = broker.positions()[0]
        entry, original_stop = held.price_open, held.sl

        # Bank 60% of a 50-point target without touching it.
        feed.move_to(bid=entry + 0.00030, ask=entry + 0.00031)
        engine.cycle()

        moved = broker.positions()[0]
        self.assertGreater(moved.sl, original_stop)
        self.assertAlmostEqual(moved.sl, entry, places=5)

    def test_break_even_leaves_a_trade_that_is_not_ahead_alone(self):
        cfg, feed, broker, engine = self.build(breakeven_at_fraction=0.5, max_hold_seconds=0)
        engine.cycle()
        original_stop = broker.positions()[0].sl

        engine.cycle()
        self.assertAlmostEqual(broker.positions()[0].sl, original_stop)


class TestJournal(EngineHarness):
    def test_opens_and_closes_are_both_recorded(self):
        cfg, feed, broker, engine = self.build(cooldown_seconds=300)
        engine.cycle()
        feed.move_to(bid=broker.positions()[0].tp + 0.00001, ask=broker.positions()[0].tp + 0.00002)
        engine.cycle()

        with open(cfg.journal_path) as handle:
            rows = handle.read().splitlines()
        self.assertEqual(rows[0].split(",")[:2], ["timestamp", "event"])
        events = [row.split(",")[1] for row in rows[1:]]
        self.assertIn("open", events)
        self.assertIn("close", events)


if __name__ == "__main__":
    unittest.main()
