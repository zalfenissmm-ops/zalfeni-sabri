import unittest
from dataclasses import replace

from mt5_bot.config import Config
from mt5_bot.sizing import build_plan, choose_target_usd, money_for_points, points_for_money
from tests.helpers import FX_SPEC

BID, ASK = 1.10000, 1.10010  # a 10-point spread
ATR = 0.00100  # 100 points


def config(**overrides) -> Config:
    return Config(volume=0.02, **overrides)


class TestMoneyConversion(unittest.TestCase):
    def test_points_and_money_are_inverses(self):
        points = points_for_money(FX_SPEC, 0.02, 1.50)
        self.assertAlmostEqual(money_for_points(FX_SPEC, 0.02, points), 1.50)

    def test_bigger_lots_need_fewer_points_for_the_same_dollars(self):
        self.assertLess(
            points_for_money(FX_SPEC, 0.10, 1.0), points_for_money(FX_SPEC, 0.01, 1.0)
        )

    def test_rejects_a_broken_tick_value(self):
        with self.assertRaises(ValueError):
            points_for_money(replace(FX_SPEC, tick_value=0.0), 0.01, 1.0)


class TestTargetSelection(unittest.TestCase):
    def test_stays_inside_the_configured_band(self):
        cfg = config()
        for atr_price in (0.00001, 0.00050, 0.50000):
            target = choose_target_usd(cfg, FX_SPEC, cfg.volume, atr_price)
            self.assertGreaterEqual(target, cfg.target_profit_min_usd)
            self.assertLessEqual(target, cfg.target_profit_max_usd)

    def test_a_livelier_market_earns_a_bigger_target(self):
        cfg = config()
        quiet = choose_target_usd(cfg, FX_SPEC, cfg.volume, 0.00060)
        busy = choose_target_usd(cfg, FX_SPEC, cfg.volume, 0.00200)
        self.assertGreater(busy, quiet)


class TestBuildPlan(unittest.TestCase):
    def test_a_buy_puts_tp_above_and_sl_below_the_ask(self):
        plan, why = build_plan(config(), FX_SPEC, "buy", BID, ASK, ATR, 0.02)
        self.assertIsNotNone(plan, why)
        self.assertEqual(plan.entry, ASK)
        self.assertGreater(plan.tp, plan.entry)
        self.assertLess(plan.sl, plan.entry)

    def test_a_sell_puts_tp_below_and_sl_above_the_bid(self):
        plan, why = build_plan(config(), FX_SPEC, "sell", BID, ASK, ATR, 0.02)
        self.assertIsNotNone(plan, why)
        self.assertEqual(plan.entry, BID)
        self.assertLess(plan.tp, plan.entry)
        self.assertGreater(plan.sl, plan.entry)

    def test_the_tp_distance_is_worth_the_dollar_target(self):
        cfg = config()
        plan, _ = build_plan(cfg, FX_SPEC, "buy", BID, ASK, ATR, 0.02)
        distance_points = (plan.tp - plan.entry) / FX_SPEC.point
        self.assertAlmostEqual(
            money_for_points(FX_SPEC, 0.02, distance_points), plan.target_usd, places=2
        )

    def test_commission_is_added_on_top_of_the_net_target(self):
        plain, _ = build_plan(config(), FX_SPEC, "buy", BID, ASK, ATR, 0.02)
        charged, _ = build_plan(
            config(commission_per_lot_usd=7.0), FX_SPEC, "buy", BID, ASK, ATR, 0.02
        )
        self.assertGreater(charged.target_points, plain.target_points)

    def test_risk_is_capped_at_the_configured_maximum(self):
        cfg = config(stop_loss_ratio=10.0, max_loss_per_trade_usd=2.0)
        plan, _ = build_plan(cfg, FX_SPEC, "buy", BID, ASK, ATR, 0.02)
        self.assertLessEqual(plan.risk_usd, 2.0 + 1e-9)

    def test_a_wide_spread_is_refused(self):
        plan, why = build_plan(config(max_spread_points=5), FX_SPEC, "buy", BID, ASK, ATR, 0.02)
        self.assertIsNone(plan)
        self.assertIn("spread", why)

    def test_a_spread_that_eats_the_target_is_refused(self):
        # 1.00 lot puts the $1 target one point away, far inside the spread.
        plan, why = build_plan(config(), FX_SPEC, "buy", BID, ASK, ATR, 1.00)
        self.assertIsNone(plan)
        self.assertIn("spread", why)

    def test_a_target_beyond_the_brokers_stop_level_is_refused(self):
        spec = replace(FX_SPEC, stops_level_points=200)
        plan, why = build_plan(config(), spec, "buy", BID, ASK, ATR, 0.02)
        self.assertIsNone(plan)
        self.assertIn("stop level", why)

    def test_a_target_too_far_for_the_volatility_is_refused(self):
        plan, why = build_plan(config(), FX_SPEC, "buy", BID, ASK, 0.00005, 0.02)
        self.assertIsNone(plan)
        self.assertIn("ATR", why)

    def test_rejects_an_unknown_side(self):
        with self.assertRaises(ValueError):
            build_plan(config(), FX_SPEC, "hold", BID, ASK, ATR, 0.02)


class TestVolumeRounding(unittest.TestCase):
    def test_snaps_onto_the_brokers_step(self):
        self.assertAlmostEqual(FX_SPEC.round_volume(0.0234), 0.02)

    def test_clamps_below_the_minimum(self):
        self.assertAlmostEqual(FX_SPEC.round_volume(0.0001), 0.01)

    def test_clamps_above_the_maximum(self):
        self.assertAlmostEqual(FX_SPEC.round_volume(500.0), 100.0)


if __name__ == "__main__":
    unittest.main()


class TestCommissionIsNetted(unittest.TestCase):
    """`target_profit_*` and `max_loss_per_trade_usd` both mean net of costs."""

    COMMISSION = 7.0  # per lot, round turn

    def plan(self, **overrides):
        cfg = config(commission_per_lot_usd=self.COMMISSION, **overrides)
        plan, why = build_plan(cfg, FX_SPEC, "buy", BID, ASK, ATR, 0.02)
        self.assertIsNotNone(plan, why)
        return cfg, plan

    def test_the_win_clears_the_target_after_commission(self):
        cfg, plan = self.plan()
        gross = money_for_points(FX_SPEC, 0.02, plan.target_points)
        self.assertAlmostEqual(gross - self.COMMISSION * 0.02, plan.target_usd, places=2)

    def test_the_loss_stays_inside_the_cap_after_commission(self):
        cfg, plan = self.plan()
        gross = money_for_points(FX_SPEC, 0.02, plan.stop_points)
        self.assertAlmostEqual(gross + self.COMMISSION * 0.02, plan.risk_usd, places=2)
        self.assertLessEqual(plan.risk_usd, cfg.max_loss_per_trade_usd + 1e-9)

    def test_commission_tightens_the_stop_rather_than_widening_the_loss(self):
        _, charged = self.plan()
        free, _ = build_plan(config(), FX_SPEC, "buy", BID, ASK, ATR, 0.02)
        self.assertLess(charged.stop_points, free.stop_points)

    def test_a_trade_whose_commission_eats_the_risk_budget_is_refused(self):
        cfg = config(commission_per_lot_usd=200.0, max_loss_per_trade_usd=1.0)
        plan, why = build_plan(cfg, FX_SPEC, "buy", BID, ASK, ATR, 0.02)
        self.assertIsNone(plan)
        self.assertIn("commission", why)
