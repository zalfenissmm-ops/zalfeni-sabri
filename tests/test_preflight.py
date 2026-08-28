import unittest
from dataclasses import replace

from mt5_bot.config import Config
from mt5_bot.preflight import assess, report
from mt5_bot.sizing import points_for_money
from tests.helpers import FX_SPEC


class TestAssess(unittest.TestCase):
    def test_a_tight_spread_and_lively_market_are_viable(self):
        result = assess(Config(volume=0.02), FX_SPEC, spread_points=10, atr_points=120)
        self.assertTrue(result.ok)
        self.assertGreater(result.suggested_volume, 0)

    def test_the_suggested_volume_actually_satisfies_both_limits(self):
        cfg = Config(volume=0.02)
        result = assess(cfg, FX_SPEC, spread_points=10, atr_points=120)
        distance = points_for_money(FX_SPEC, result.suggested_volume, cfg.target_profit_min_usd)
        self.assertGreaterEqual(distance, result.min_target_points)
        self.assertLessEqual(distance, result.max_target_points)

    def test_a_wide_spread_in_a_dead_market_is_not_viable(self):
        result = assess(Config(), FX_SPEC, spread_points=30, atr_points=25)
        self.assertFalse(result.ok)
        self.assertIn("No lot size works", result.note)

    def test_a_bigger_target_needs_a_smaller_lot(self):
        cheap = assess(Config(target_profit_min_usd=1.0), FX_SPEC, 10, 120)
        rich = assess(Config(target_profit_min_usd=3.0, target_profit_max_usd=3.0), FX_SPEC, 10, 120)
        self.assertGreater(rich.suggested_volume, cheap.suggested_volume)

    def test_a_broker_stop_level_raises_the_minimum_distance(self):
        spec = replace(FX_SPEC, stops_level_points=200)
        result = assess(Config(), spec, spread_points=5, atr_points=120)
        self.assertGreaterEqual(result.min_target_points, 200)

    def test_lot_limits_outside_the_workable_band_are_flagged(self):
        spec = replace(FX_SPEC, volume_min=1.0, volume_max=100.0)
        result = assess(Config(), spec, spread_points=10, atr_points=120)
        self.assertFalse(result.ok)
        self.assertIn("lot limits", result.note)


class TestReport(unittest.TestCase):
    def test_warns_when_the_configured_volume_is_outside_the_band(self):
        cfg = Config(volume=5.0)
        text = report(cfg, assess(cfg, FX_SPEC, spread_points=10, atr_points=120))
        self.assertIn("WARNING", text)

    def test_stays_quiet_when_the_configured_volume_fits(self):
        cfg = Config(volume=0.02)
        text = report(cfg, assess(cfg, FX_SPEC, spread_points=10, atr_points=120))
        self.assertNotIn("WARNING", text)
        self.assertIn("OK", text)


if __name__ == "__main__":
    unittest.main()
