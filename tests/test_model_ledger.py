import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from test_portfolio_schema import decision
from track_returns import (
    apply_structured_transactions,
    create_model_ledger,
    load_model_ledger,
    refresh_structured_performance,
    save_model_ledger,
    transaction_decisions_for_run,
)


KEY = "stock:US:AA"


class ModelLedgerTest(unittest.TestCase):
    def test_tracks_buy_increase_reduce_and_sell_as_transactions(self):
        ledger = create_model_ledger()
        ledger = apply_structured_transactions(
            ledger,
            [decision(proposed_weight=20.0)],
            {KEY: 10.0},
            "2026-06-01",
        )
        ledger = apply_structured_transactions(
            ledger,
            [decision(action="비중확대", previous_weight=20.0, proposed_weight=30.0)],
            {KEY: 10.0},
            "2026-06-02",
        )
        ledger = apply_structured_transactions(
            ledger,
            [decision(action="비중축소", previous_weight=30.0, proposed_weight=10.0)],
            {KEY: 10.0},
            "2026-06-03",
        )
        ledger = apply_structured_transactions(
            ledger,
            [decision(action="매도", previous_weight=10.0, proposed_weight=0.0)],
            {KEY: 10.0},
            "2026-06-04",
        )

        self.assertEqual(
            [item["type"] for item in ledger["transactions"]],
            ["신규 매수", "추가 매수", "일부 매도", "전량 매도"],
        )
        self.assertAlmostEqual(ledger["cash"], 100.0)
        self.assertEqual(ledger["positions"], [])
        self.assertEqual(ledger["closed_positions"][0]["close_reason"], "알루미늄 공급 제한에 따른 수혜 추론")

    def test_snapshot_includes_cash_and_realized_profit(self):
        ledger = create_model_ledger()
        ledger = apply_structured_transactions(
            ledger,
            [decision(proposed_weight=20.0)],
            {KEY: 10.0},
            "2026-06-01",
        )
        ledger = apply_structured_transactions(
            ledger,
            [decision(action="매도", previous_weight=20.0, proposed_weight=0.0)],
            {KEY: 20.0},
            "2026-06-02",
        )

        self.assertAlmostEqual(ledger["cash"], 120.0)
        self.assertAlmostEqual(ledger["realized_pnl"], 20.0)
        self.assertAlmostEqual(ledger["snapshots"][-1]["return_pct"], 20.0)

    def test_rejects_trade_without_price(self):
        with self.assertRaisesRegex(ValueError, "missing positive trade price"):
            apply_structured_transactions(
                create_model_ledger(),
                [decision(proposed_weight=20.0)],
                {},
                "2026-06-01",
            )

    def test_saves_ledger_and_refreshes_structured_cache(self):
        ledger = apply_structured_transactions(
            create_model_ledger(),
            [decision(proposed_weight=20.0)],
            {KEY: 10.0},
            "2026-06-01",
        )
        with TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "model_portfolio_ledger.json"
            cache_path = Path(tmp) / "performance_cache.json"

            save_model_ledger(ledger, ledger_path)
            loaded = load_model_ledger(ledger_path)
            cache = refresh_structured_performance(
                loaded,
                {KEY: 11.0},
                "2026-06-02",
                cache_path,
            )

        self.assertAlmostEqual(cache["portfolio_return_krw"], 2.0)
        self.assertAlmostEqual(cache["active_positions"][0]["return_pct_krw"], 10.0)

    def test_first_exact_tracking_run_uses_surviving_portfolio_as_baseline(self):
        retained = decision(action="보유", previous_weight=8.0, proposed_weight=8.0)
        sold_legacy = decision(action="매도", previous_weight=5.0, proposed_weight=0.0)

        selected = transaction_decisions_for_run(
            create_model_ledger(),
            [retained],
            [retained, sold_legacy],
        )

        self.assertEqual(selected, [retained])

    def test_unchanged_weight_does_not_trade_after_price_move(self):
        ledger = apply_structured_transactions(
            create_model_ledger(),
            [decision(proposed_weight=20.0)],
            {KEY: 10.0},
            "2026-06-01",
        )

        updated = apply_structured_transactions(
            ledger,
            [decision(action="보유", previous_weight=20.0, proposed_weight=20.0)],
            {KEY: 20.0},
            "2026-06-02",
        )

        self.assertEqual(len(updated["transactions"]), 1)
        self.assertAlmostEqual(updated["snapshots"][-1]["return_pct"], 20.0)


if __name__ == "__main__":
    unittest.main()
