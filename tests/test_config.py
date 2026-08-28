import json
import os
import tempfile
import unittest

from mt5_bot.config import Config, in_blackout, parse_blackout_window


class TestValidation(unittest.TestCase):
    def test_rejects_an_empty_symbol_list(self):
        with self.assertRaises(ValueError):
            Config(symbols=[])

    def test_rejects_a_fast_ema_that_is_not_faster(self):
        with self.assertRaises(ValueError):
            Config(ema_fast=21, ema_slow=8)

    def test_rejects_a_target_band_that_is_inverted(self):
        with self.assertRaises(ValueError):
            Config(target_profit_min_usd=3.0, target_profit_max_usd=1.0)

    def test_rejects_too_little_history_for_the_periods(self):
        with self.assertRaises(ValueError):
            Config(candles_to_load=20, ema_slow=50)

    def test_rejects_disabling_both_entry_triggers(self):
        with self.assertRaises(ValueError):
            Config(use_pullback_entries=False, use_breakout_entries=False)


class TestLoading(unittest.TestCase):
    def _write(self, data: dict) -> str:
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(data, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_reads_values_from_a_file(self):
        cfg = Config.load(self._write({"symbols": ["XAUUSD"], "volume": 0.05}))
        self.assertEqual(cfg.symbols, ["XAUUSD"])
        self.assertEqual(cfg.volume, 0.05)

    def test_rejects_an_unknown_key_instead_of_ignoring_it(self):
        with self.assertRaises(ValueError) as caught:
            Config.load(self._write({"targetprofit": 5}))
        self.assertIn("targetprofit", str(caught.exception))

    def test_environment_overrides_the_credentials(self):
        os.environ["MT5_LOGIN"] = "12345"
        os.environ["MT5_SERVER"] = "Broker-Demo"
        self.addCleanup(os.environ.pop, "MT5_LOGIN", None)
        self.addCleanup(os.environ.pop, "MT5_SERVER", None)
        cfg = Config.load(self._write({"login": 1}))
        self.assertEqual(cfg.login, 12345)
        self.assertEqual(cfg.server, "Broker-Demo")


class TestBlackoutWindows(unittest.TestCase):
    def test_parses_into_minutes(self):
        self.assertEqual(parse_blackout_window("23:56-00:06"), (23 * 60 + 56, 6))

    def test_rejects_a_malformed_window(self):
        with self.assertRaises(ValueError):
            parse_blackout_window("2356-0006")

    def test_rejects_an_impossible_time(self):
        with self.assertRaises(ValueError):
            parse_blackout_window("25:00-01:00")

    def test_a_window_that_wraps_past_midnight_covers_both_sides(self):
        windows = ["23:56-00:06"]
        self.assertTrue(in_blackout(windows, 23 * 60 + 58))
        self.assertTrue(in_blackout(windows, 3))
        self.assertFalse(in_blackout(windows, 12 * 60))

    def test_a_normal_window_excludes_its_end_minute(self):
        self.assertTrue(in_blackout(["09:00-10:00"], 9 * 60))
        self.assertFalse(in_blackout(["09:00-10:00"], 10 * 60))


if __name__ == "__main__":
    unittest.main()
