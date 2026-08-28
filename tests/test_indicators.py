import unittest

from mt5_bot.indicators import atr, ema, rsi
from tests.helpers import candles_from


class TestEma(unittest.TestCase):
    def test_leading_slots_are_none_until_the_period_fills(self):
        values = ema([1, 2, 3, 4, 5], 3)
        self.assertEqual(values[:2], [None, None])
        self.assertEqual(values[2], 2.0)  # seeded with the simple average

    def test_length_matches_the_input(self):
        self.assertEqual(len(ema(list(range(30)), 10)), 30)

    def test_short_input_is_all_none(self):
        self.assertEqual(ema([1, 2], 5), [None, None])

    def test_tracks_a_constant_series(self):
        self.assertAlmostEqual(ema([7.0] * 20, 5)[-1], 7.0)

    def test_rejects_a_zero_period(self):
        with self.assertRaises(ValueError):
            ema([1, 2, 3], 0)


class TestRsi(unittest.TestCase):
    def test_a_pure_uptrend_pins_at_100(self):
        self.assertAlmostEqual(rsi([float(i) for i in range(1, 40)], 14)[-1], 100.0)

    def test_a_pure_downtrend_pins_at_zero(self):
        self.assertAlmostEqual(rsi([float(i) for i in range(40, 1, -1)], 14)[-1], 0.0)

    def test_first_value_lands_on_the_period_index(self):
        values = rsi([float(i % 5) for i in range(40)], 14)
        self.assertIsNone(values[13])
        self.assertIsNotNone(values[14])

    def test_a_flat_series_is_neutral(self):
        self.assertAlmostEqual(rsi([2.0] * 30, 14)[-1], 50.0)


class TestAtr(unittest.TestCase):
    def test_measures_a_constant_bar_range(self):
        bars = candles_from([1.0] * 30, wick=0.5)
        self.assertAlmostEqual(atr(bars, 14)[-1], 1.0)

    def test_is_none_before_enough_bars(self):
        self.assertIsNone(atr(candles_from([1.0] * 10), 14)[-1])


if __name__ == "__main__":
    unittest.main()
