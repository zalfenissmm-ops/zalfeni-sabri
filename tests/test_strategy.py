import unittest

from mt5_bot.config import Config
from mt5_bot.indicators import ema
from mt5_bot.strategy import evaluate
from tests.helpers import candles_from, wave

# The strategy ignores the bar the broker is still building, so every scripted
# path ends with one spare "forming" bar after the bar meant to trigger.


def uptrend() -> list[float]:
    return wave()


def downtrend() -> list[float]:
    return wave(base=1.20000, sign=-1)


class TestGuards(unittest.TestCase):
    def test_refuses_to_act_without_enough_closed_bars(self):
        signal = evaluate(Config(), candles_from([1.1] * 10))
        self.assertIsNone(signal.side)
        self.assertIn("closed bars", signal.reason)

    def test_a_flat_market_produces_no_signal(self):
        signal = evaluate(Config(), candles_from([1.10000] * 80, wick=0.0))
        self.assertIsNone(signal.side)

    def test_a_spike_on_the_forming_bar_cannot_trigger_an_entry(self):
        prices = uptrend() + [1.10150, 1.10148, 1.10120]
        calm = evaluate(Config(), candles_from(prices + [1.10121], wick=0.0))
        spiked = evaluate(Config(), candles_from(prices + [9.99999], wick=0.0))
        self.assertIsNone(calm.side)
        self.assertEqual(calm.side, spiked.side)


class TestFilters(unittest.TestCase):
    def test_will_not_buy_an_overbought_push(self):
        prices = uptrend() + [1.10150, 1.10148, 1.10160, 1.10160]
        signal = evaluate(Config(rsi_buy_max=50.0), candles_from(prices, wick=0.0))
        self.assertIsNone(signal.side)
        self.assertIn("overbought", signal.reason)

    def test_will_not_sell_an_oversold_flush(self):
        prices = downtrend() + [1.19850, 1.19852, 1.19845, 1.19845]
        signal = evaluate(Config(rsi_sell_min=50.0), candles_from(prices, wick=0.0))
        self.assertIsNone(signal.side)
        self.assertIn("oversold", signal.reason)


class TestTriggers(unittest.TestCase):
    def test_a_breakout_in_an_uptrend_is_a_buy(self):
        prices = uptrend() + [1.10150, 1.10148, 1.10165, 1.10165]
        signal = evaluate(Config(use_pullback_entries=False), candles_from(prices, wick=0.0))
        self.assertEqual(signal.side, "buy")
        self.assertIn("breakout", signal.reason)

    def test_a_breakdown_in_a_downtrend_is_a_sell(self):
        prices = downtrend() + [1.19850, 1.19852, 1.19840, 1.19840]
        signal = evaluate(Config(use_pullback_entries=False), candles_from(prices, wick=0.0))
        self.assertEqual(signal.side, "sell")
        self.assertIn("breakout", signal.reason)

    def test_a_pullback_through_the_fast_ema_is_a_buy(self):
        base = uptrend()
        fast = ema(base, 8)[-1]
        prices = base + [round(fast - 0.00030, 5), round(fast + 0.00030, 5), 1.10160]
        signal = evaluate(Config(use_breakout_entries=False), candles_from(prices, wick=0.0))
        self.assertEqual(signal.side, "buy")
        self.assertIn("pullback", signal.reason)

    def test_no_trigger_leaves_the_trend_reported_but_unactioned(self):
        prices = uptrend() + [1.10150, 1.10148, 1.10120, 1.10121]
        signal = evaluate(Config(), candles_from(prices, wick=0.0))
        self.assertIsNone(signal.side)
        self.assertIn("no trigger", signal.reason)

    def test_reports_the_atr_alongside_the_signal(self):
        prices = uptrend() + [1.10150, 1.10148, 1.10165, 1.10165]
        signal = evaluate(Config(use_pullback_entries=False), candles_from(prices, wick=0.0))
        self.assertGreater(signal.atr, 0.0)
        self.assertTrue(signal.actionable)


if __name__ == "__main__":
    unittest.main()
