import math
import unittest

from portfolio_metrics import benchmark_returns, max_drawdown, performance_metrics


class PortfolioMetricsTest(unittest.TestCase):
    def test_strategic_benchmark_uses_fixed_forty_forty_twenty_weights(self):
        result = benchmark_returns([0.01, -0.01], [0.02, 0.01])

        self.assertEqual(result, [0.012, 0.0])

    def test_max_drawdown_uses_high_water_mark(self):
        self.assertAlmostEqual(max_drawdown([100.0, 110.0, 88.0, 99.0]), -0.2)

    def test_reports_excess_return_and_risk_metrics(self):
        result = performance_metrics(
            [0.01, -0.005, 0.012, -0.002],
            [0.005, -0.004, 0.006, -0.001],
        )

        self.assertIsNotNone(result["annualized_volatility"])
        self.assertIsNotNone(result["max_drawdown"])
        self.assertGreater(result["excess_return"], 0)
        self.assertIsNotNone(result["information_ratio"])

    def test_requires_aligned_benchmark(self):
        with self.assertRaisesRegex(ValueError, "lengths must match"):
            performance_metrics([0.01], [0.01, 0.02])

    def test_sortino_uses_downside_rms_around_zero_target(self):
        result = performance_metrics([0.02, -0.01])

        expected = 0.005 / math.sqrt((0.0 ** 2 + 0.01 ** 2) / 2.0) * math.sqrt(252.0)
        self.assertAlmostEqual(result["sortino"], expected)


if __name__ == "__main__":
    unittest.main()
