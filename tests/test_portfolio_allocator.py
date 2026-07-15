import unittest

from portfolio_allocator import (
    SECTOR_ETF_TARGET_WEIGHT_CAP,
    STOCK_TARGET_WEIGHT_CAP,
    cap_target_weight,
)


class TargetWeightCapTest(unittest.TestCase):
    def test_stock_is_capped_at_ten_percent(self):
        self.assertEqual(cap_target_weight({"asset_type": "stock"}, 18.0), STOCK_TARGET_WEIGHT_CAP)

    def test_sector_etf_is_capped_at_thirty_percent(self):
        self.assertEqual(cap_target_weight({"asset_type": "etf"}, 42.0), SECTOR_ETF_TARGET_WEIGHT_CAP)

    def test_proposal_below_cap_is_unchanged(self):
        self.assertEqual(cap_target_weight({"asset_type": "stock"}, 7.5), 7.5)
