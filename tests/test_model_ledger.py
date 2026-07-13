import json
import math
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from test_portfolio_schema import decision
from track_returns import (
    apply_corporate_action_events,
    apply_structured_transactions,
    assert_structured_target_residuals,
    create_model_ledger,
    get_structured_corporate_action_events,
    get_structured_volatilities,
    load_model_ledger,
    refresh_structured_performance,
    refresh_structured_corporate_actions,
    sanitize_model_ledger_for_state,
    save_model_ledger,
    structured_actual_weights,
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

    def test_closed_episode_accumulates_partial_and_final_sale_pnl(self):
        ledger = apply_structured_transactions(
            create_model_ledger(),
            [decision(proposed_weight=20.0)],
            {KEY: 10.0},
            "2026-06-01",
        )
        ledger = apply_structured_transactions(
            ledger,
            [decision(action="비중축소", previous_weight=20.0, proposed_weight=10.0)],
            {KEY: 20.0},
            "2026-06-02",
        )
        ledger = apply_structured_transactions(
            ledger,
            [decision(action="매도", previous_weight=10.0, proposed_weight=0.0)],
            {KEY: 30.0},
            "2026-06-03",
        )

        self.assertAlmostEqual(
            ledger["closed_positions"][0]["realized_pnl"],
            ledger["realized_pnl"],
        )

    def test_transaction_costs_reduce_cash_and_are_recorded(self):
        ledger = apply_structured_transactions(
            create_model_ledger(),
            [decision(proposed_weight=20.0)],
            {KEY: 10.0},
            "2026-06-01",
            cost_bps_by_market={"US": 35.0},
        )

        self.assertAlmostEqual(ledger["transactions"][0]["trade_cost"], 0.0699510343)
        self.assertAlmostEqual(ledger["cumulative_costs"], 0.0699510343)
        self.assertAlmostEqual(ledger["cash"] / ledger["snapshots"][-1]["total_value"] * 100, 80.0)

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
        self.assertEqual(loaded["snapshots"][-1]["date"], "2026-06-02")

    def test_v3_ledger_starts_clean_v4_epoch_and_preserves_legacy_audit(self):
        legacy = {
            "schema_version": "3.0",
            "starting_value": 100.0,
            "cash": 20.0,
            "positions": [{"key": KEY, "quantity": 1.0}],
            "transactions": [{"date": "2026-06-01"}],
        }
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            migrated = load_model_ledger(path)

        self.assertEqual(migrated["schema_version"], "4.0")
        self.assertEqual(migrated["positions"], [])
        self.assertEqual(migrated["cash"], 100.0)
        self.assertEqual(migrated["legacy_epochs"][0]["ledger"]["schema_version"], "3.0")

    def test_first_exact_tracking_run_uses_surviving_portfolio_as_baseline(self):
        retained = decision(action="보유", previous_weight=8.0, proposed_weight=8.0)
        sold_legacy = decision(action="매도", previous_weight=5.0, proposed_weight=0.0)

        selected = transaction_decisions_for_run(
            create_model_ledger(),
            [retained],
            [retained, sold_legacy],
        )

        self.assertEqual(selected, [retained])

    def test_orphan_active_ledger_position_is_reconciled_with_a_full_exit(self):
        ledger = apply_structured_transactions(
            create_model_ledger(),
            [decision(proposed_weight=20.0)],
            {KEY: 10.0},
            "2026-06-01",
        )

        selected = transaction_decisions_for_run(ledger, [], [])
        updated = apply_structured_transactions(
            ledger,
            selected,
            {KEY: 10.0},
            "2026-06-02",
        )

        self.assertEqual(selected[0]["action"], "매도")
        self.assertEqual(updated["positions"], [])
        self.assertEqual(len(updated["closed_positions"]), 1)

    def test_unchanged_target_rebalances_actual_weight_after_price_move(self):
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

        self.assertEqual(len(updated["transactions"]), 2)
        self.assertEqual(updated["transactions"][-1]["type"], "일부 매도")
        self.assertEqual(updated["positions"][0]["target_weight"], 20.0)
        self.assertAlmostEqual(
            structured_actual_weights(updated, {KEY: 20.0})[KEY],
            20.0,
        )
        self.assertAlmostEqual(updated["snapshots"][-1]["return_pct"], 20.0)

    def test_actual_drift_inside_fifty_basis_points_does_not_trade(self):
        ledger = apply_structured_transactions(
            create_model_ledger(),
            [decision(proposed_weight=20.0)],
            {KEY: 10.0},
            "2026-06-01",
        )

        updated = apply_structured_transactions(
            ledger,
            [decision(action="보유", previous_weight=20.0, proposed_weight=20.0)],
            {KEY: 10.2},
            "2026-06-02",
        )

        self.assertEqual(len(updated["transactions"]), 1)
        self.assertEqual(updated["positions"][0]["target_weight"], 20.0)

    def test_rotation_sells_before_buying_even_when_buy_is_listed_first(self):
        ledger = apply_structured_transactions(
            create_model_ledger(),
            [decision(proposed_weight=80.0)],
            {KEY: 10.0},
            "2026-06-01",
        )
        microsoft = decision(
            name="Microsoft",
            code="MSFT",
            proposed_weight=80.0,
        )
        microsoft_key = "stock:US:MSFT"

        updated = apply_structured_transactions(
            ledger,
            [
                microsoft,
                decision(action="매도", previous_weight=80.0, proposed_weight=0.0),
            ],
            {KEY: 10.0, microsoft_key: 10.0},
            "2026-06-02",
        )

        self.assertEqual(
            [row["type"] for row in updated["transactions"][-2:]],
            ["전량 매도", "신규 매수"],
        )
        residuals = assert_structured_target_residuals(
            updated,
            {KEY: 0.0, microsoft_key: 80.0},
            {microsoft_key: 10.0},
        )
        self.assertAlmostEqual(residuals[KEY], 0.0)
        self.assertAlmostEqual(residuals[microsoft_key], 0.0)

    def test_target_residual_helper_rejects_inaccurate_execution(self):
        ledger = apply_structured_transactions(
            create_model_ledger(),
            [decision(proposed_weight=20.0)],
            {KEY: 10.0},
            "2026-06-01",
        )
        ledger["positions"][0]["quantity"] *= 2

        with self.assertRaisesRegex(ValueError, "target residual exceeds"):
            assert_structured_target_residuals(
                ledger,
                {KEY: 20.0},
                {KEY: 10.0},
            )

    def test_preserves_closed_episode_when_same_security_is_active_again(self):
        ledger = create_model_ledger()
        closed = {
            "key": "stock:KR:001440",
            "name": "대한전선",
            "code": "001440",
            "market": "KR",
            "asset_type": "stock",
            "quantity": 0.0,
            "average_cost": 100.0,
            "closed_date": "2026-06-03",
            "close_reason": "현재 포트폴리오에서 제외",
        }
        ledger["closed_positions"] = [closed]
        state = {
            "portfolio": [
                {"name": "대한전선", "code": "001440", "market": "KR", "asset_type": "stock"}
            ]
        }

        sanitized = sanitize_model_ledger_for_state(ledger, state)

        self.assertEqual(len(sanitized["closed_positions"]), 1)
        self.assertEqual(sanitized["closed_positions"][0]["closed_date"], "2026-06-03")

    def test_reentry_keeps_prior_closed_episode_in_ledger_and_cache(self):
        ledger = apply_structured_transactions(
            create_model_ledger(),
            [decision(proposed_weight=20.0)],
            {KEY: 10.0},
            "2026-06-01",
        )
        ledger = apply_structured_transactions(
            ledger,
            [decision(action="매도", previous_weight=20.0, proposed_weight=0.0)],
            {KEY: 11.0},
            "2026-06-02",
        )
        ledger = apply_structured_transactions(
            ledger,
            [decision(previous_weight=0.0, proposed_weight=20.0)],
            {KEY: 12.0},
            "2026-06-03",
        )

        sanitized = sanitize_model_ledger_for_state(
            ledger,
            {"portfolio": [decision(previous_weight=0.0, proposed_weight=20.0)]},
        )
        with TemporaryDirectory() as tmp:
            cache = refresh_structured_performance(
                sanitized,
                {KEY: 12.0},
                "2026-06-03",
                Path(tmp) / "performance_cache.json",
                persist=False,
            )

        self.assertEqual(len(sanitized["positions"]), 1)
        self.assertEqual(sanitized["positions"][0]["opened_date"], "2026-06-03")
        self.assertEqual(len(sanitized["closed_positions"]), 1)
        self.assertEqual(sanitized["closed_positions"][0]["closed_date"], "2026-06-02")
        self.assertEqual(len(cache["closed_positions"]), 1)

    def test_stock_split_adjusts_quantity_and_cost_once(self):
        ledger = apply_structured_transactions(
            create_model_ledger(),
            [decision(proposed_weight=20.0)],
            {KEY: 10.0},
            "2026-06-01",
        )
        event = {
            "event_id": "AA:split:2026-06-02:2",
            "type": "stock_split",
            "key": KEY,
            "effective_date": "2026-06-02",
            "ratio": 2.0,
        }

        adjusted = apply_corporate_action_events(ledger, [event])
        replayed = apply_corporate_action_events(adjusted, [event])

        self.assertAlmostEqual(adjusted["positions"][0]["quantity"], 4.0)
        self.assertAlmostEqual(adjusted["positions"][0]["average_cost"], 5.0)
        self.assertAlmostEqual(
            adjusted["cash"] + adjusted["positions"][0]["quantity"] * 5.0,
            100.0,
        )
        self.assertEqual(len(replayed["corporate_events"]), 1)
        self.assertAlmostEqual(replayed["positions"][0]["quantity"], 4.0)
        self.assertAlmostEqual(replayed["positions"][0]["average_cost"], 5.0)

    def test_cash_dividend_adds_cash_and_income_once(self):
        ledger = apply_structured_transactions(
            create_model_ledger(),
            [decision(proposed_weight=20.0)],
            {KEY: 10.0},
            "2026-06-01",
        )
        event = {
            "event_id": "AA:dividend:2026-06-02:1.5",
            "type": "cash_dividend",
            "key": KEY,
            "effective_date": "2026-06-02",
            "cash_per_share": 1.5,
        }

        adjusted = apply_corporate_action_events(ledger, [event])
        replayed = apply_corporate_action_events(adjusted, [event])

        self.assertAlmostEqual(adjusted["cash"], 83.0)
        self.assertAlmostEqual(adjusted["dividend_income"], 3.0)
        self.assertAlmostEqual(adjusted["corporate_events"][0]["cash_amount"], 3.0)
        self.assertEqual(len(replayed["corporate_events"]), 1)
        self.assertAlmostEqual(replayed["cash"], 83.0)
        self.assertAlmostEqual(replayed["dividend_income"], 3.0)

    def test_rejects_conflicting_corporate_event_id_without_mutating_source(self):
        ledger = apply_structured_transactions(
            create_model_ledger(),
            [decision(proposed_weight=20.0)],
            {KEY: 10.0},
            "2026-06-01",
        )
        applied = apply_corporate_action_events(ledger, [{
            "event_id": "AA:split:2026-06-02",
            "type": "stock_split",
            "key": KEY,
            "effective_date": "2026-06-02",
            "ratio": 2.0,
        }])

        with self.assertRaisesRegex(ValueError, "conflicting corporate action event_id"):
            apply_corporate_action_events(applied, [{
                "event_id": "AA:split:2026-06-02",
                "type": "stock_split",
                "key": KEY,
                "effective_date": "2026-06-02",
                "ratio": 3.0,
            }])

        self.assertAlmostEqual(applied["positions"][0]["quantity"], 4.0)

    def test_rejects_provider_revision_for_same_corporate_economic_event(self):
        ledger = apply_structured_transactions(
            create_model_ledger(),
            [decision(proposed_weight=20.0)],
            {KEY: 10.0},
            "2026-06-01",
        )
        applied = apply_corporate_action_events(ledger, [{
            "event_id": "provider:AA:dividend:2026-06-02:1.0",
            "type": "cash_dividend",
            "key": KEY,
            "effective_date": "2026-06-02",
            "cash_per_share": 1.0,
        }])

        with self.assertRaisesRegex(ValueError, "same security/type/date"):
            apply_corporate_action_events(applied, [{
                "event_id": "provider:AA:dividend:2026-06-02:1.1",
                "type": "cash_dividend",
                "key": KEY,
                "effective_date": "2026-06-02",
                "cash_per_share": 1.1,
            }])

        self.assertEqual(len(applied["corporate_events"]), 1)

    def test_refresh_corporate_actions_wires_fetch_apply_and_checkpoint(self):
        ledger = apply_structured_transactions(
            create_model_ledger(),
            [decision(proposed_weight=20.0)],
            {KEY: 10.0},
            "2026-06-01",
        )
        event = {
            "event_id": "provider:AA:dividend:2026-06-02",
            "type": "cash_dividend",
            "key": KEY,
            "effective_date": "2026-06-02",
            "cash_per_share": 1.5,
        }

        with patch(
            "track_returns.get_structured_corporate_action_events",
            return_value=[event],
        ) as fetch:
            refreshed = refresh_structured_corporate_actions(ledger, "2026-06-02")

        fetch.assert_called_once()
        self.assertAlmostEqual(refreshed["cash"], 83.0)
        self.assertEqual(
            refreshed["corporate_action_state"]["last_checked_date"],
            "2026-06-02",
        )
        self.assertEqual(
            refreshed["corporate_action_state"]["last_applied_date"],
            "2026-06-02",
        )

    def test_yfinance_boundary_emits_stable_split_and_krw_dividend_events(self):
        class FakeHistory:
            empty = False

            @staticmethod
            def iterrows():
                return iter([(
                    datetime(2026, 6, 2),
                    {"Stock Splits": 2.0, "Dividends": 1.5},
                )])

        class FakeTicker:
            @staticmethod
            def history(**kwargs):
                self.assertEqual(kwargs["start"], "2026-06-02")
                self.assertEqual(kwargs["end"], "2026-06-04")
                self.assertTrue(kwargs["actions"])
                return FakeHistory()

        ledger = apply_structured_transactions(
            create_model_ledger(),
            [decision(proposed_weight=20.0)],
            {KEY: 10.0},
            "2026-06-01",
        )
        with patch("track_returns.yf.Ticker", return_value=FakeTicker()), patch(
            "track_returns._get_usdkrw",
            return_value=1300.0,
        ):
            events = get_structured_corporate_action_events(ledger, "2026-06-03")

        self.assertEqual(
            [event["type"] for event in events],
            ["stock_split", "cash_dividend"],
        )
        self.assertEqual(
            events[0]["event_id"],
            "yfinance:AA:stock_split:2026-06-02:2",
        )
        self.assertEqual(
            events[1]["event_id"],
            "yfinance:AA:cash_dividend:2026-06-02:1.5",
        )
        self.assertAlmostEqual(events[1]["cash_per_share"], 1950.0)
        self.assertEqual(events[1]["native_currency"], "USD")

    def test_volatility_uses_last_twenty_returns_in_krw(self):
        dates = [datetime(2026, 4, 1) + timedelta(days=index) for index in range(31)]
        stock_values = [100.0, 200.0] * 5 + [100.0] * 21
        fx_values = [1300.0] * 10 + [1300.0 if index % 2 == 0 else 1313.0 for index in range(21)]

        class FakeSeries:
            def __init__(self, values):
                self.values = values

            def dropna(self):
                return self

            def items(self):
                return iter(zip(dates, self.values))

        class FakeHistory:
            def __init__(self, values):
                self.values = values

            def __getitem__(self, key):
                if key != "Close":
                    raise AssertionError(f"unexpected history column: {key}")
                return FakeSeries(self.values)

        class FakeTicker:
            def __init__(self, symbol):
                self.symbol = symbol

            def history(self, **kwargs):
                if kwargs.get("period") != "3mo" or not kwargs.get("auto_adjust"):
                    raise AssertionError(f"unexpected history options: {kwargs}")
                return FakeHistory(fx_values if self.symbol == "KRW=X" else stock_values)

        with patch("track_returns.YFINANCE_AVAILABLE", True), patch(
            "track_returns.yf.Ticker",
            side_effect=lambda symbol: FakeTicker(symbol),
        ):
            result = get_structured_volatilities([decision()])

        krw_closes = [stock * fx for stock, fx in zip(stock_values[-21:], fx_values[-21:])]
        returns = [
            krw_closes[index] / krw_closes[index - 1] - 1.0
            for index in range(1, len(krw_closes))
        ]
        mean_return = sum(returns) / len(returns)
        expected = math.sqrt(
            sum((value - mean_return) ** 2 for value in returns) / len(returns)
        ) * math.sqrt(252.0)
        self.assertAlmostEqual(result[KEY], expected)


if __name__ == "__main__":
    unittest.main()
