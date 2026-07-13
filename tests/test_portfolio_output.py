from copy import deepcopy
import unittest

from portfolio_output import build_markdown_report, build_output_model
from test_portfolio_schema import decision, state_payload


class PortfolioOutputTest(unittest.TestCase):
    def test_output_keeps_portfolio_item_without_return_data(self):
        state = state_payload()
        state["portfolio"].append(
            decision(
                name="대한전선",
                code="001440",
                market="KR",
                proposed_weight=3.0,
            )
        )
        performance = {
            "portfolio_return_krw": 1.9,
            "active_positions": [
                {"code": "AA", "return_pct_krw": 8.15, "entry_date": "2026-05-28"}
            ],
        }

        output = build_output_model(
            state,
            performance,
            today_str="2026-06-07",
            status_note="신규 글 분석 보류",
        )

        self.assertEqual(len(output["portfolio"]), 2)
        self.assertEqual(output["portfolio"][1]["name"], "대한전선")
        self.assertEqual(output["portfolio"][1]["return_label"], "집계 전")
        self.assertEqual(output["portfolio_return_label"], "집계 전")
        self.assertEqual(output["missing_return_codes"], ["001440"])

    def test_markdown_report_uses_same_domestic_and_overseas_sections(self):
        state = state_payload()
        state["portfolio"].append(
            decision(
                name="대한전선",
                code="001440",
                market="KR",
                proposed_weight=3.0,
            )
        )
        output = build_output_model(state, {}, today_str="2026-06-07")

        report = build_markdown_report(output)

        self.assertIn("### 국내주식 추천", report)
        self.assertIn("| 대한전선 | 001440 | AI 제안 · 매수 | 위성 | 3%", report)
        self.assertIn("### 해외주식 추천", report)
        self.assertIn("| Alcoa | AA | AI 제안 · 매수 | 위성 | 8%", report)
        self.assertIn("수익률 집계 전 종목: AA, 001440", report)

    def test_unclassified_migration_position_is_review_required_not_recommendation(self):
        state = state_payload()
        state["portfolio"].append(
            decision(
                name="대한전선",
                code="001440",
                market="KR",
                decision_actor="미분류",
                action="보유",
                basis="이전 판단 유지",
                evidence_posts=[],
                source_mentioned=False,
                proposed_weight=3.0,
                change_reason="기존 상태 마이그레이션: 최초 재평가 전까지 보존",
            )
        )

        output = build_output_model(state, {}, today_str="2026-06-07")
        report = build_markdown_report(output)

        self.assertEqual(output["domestic"], [])
        self.assertEqual(output["review_required_positions"][0]["name"], "대한전선")
        self.assertIn("## 재검증 필요 포지션", report)
        self.assertIn("| 대한전선 | 001440 | 3% | 판단 주체가 미분류", report)

    def test_ai_position_without_role_is_review_required_not_recommendation(self):
        state = state_payload()
        legacy_item = decision(
            name="대한전선",
            code="001440",
            market="KR",
            action="보유",
            proposed_weight=3.0,
            change_reason="기존 AI 판단 유지",
        )
        legacy_item.pop("allocation_role", None)
        state["portfolio"].append(legacy_item)

        output = build_output_model(state, {}, today_str="2026-06-07")
        report = build_markdown_report(output)

        self.assertEqual(output["domestic"], [])
        self.assertEqual(output["review_required_positions"][0]["name"], "대한전선")
        self.assertIn("AI 판단이지만 포트폴리오 역할이 없어", report)

    def test_reentered_security_keeps_its_prior_closed_episode_in_output(self):
        state = state_payload()
        state["portfolio"].append(
            decision(
                name="대한전선",
                code="001440",
                market="KR",
                proposed_weight=3.0,
            )
        )
        performance = {
            "closed_positions": [
                {
                    "name": "대한전선",
                    "code": "001440",
                    "market": "KR",
                    "closed_date": "2026-06-03",
                    "close_reason": "현재 포트폴리오에서 제외",
                },
                {
                    "name": "NVIDIA",
                    "code": "NVDA",
                    "market": "US",
                    "closed_date": "2026-05-31",
                    "close_reason": "현재 포트폴리오에서 제외",
                },
            ],
        }

        output = build_output_model(state, performance, today_str="2026-06-07")

        closed_codes = {item.get("code") for item in output["closed_positions"]}
        self.assertIn("001440", closed_codes)
        self.assertIn("NVDA", closed_codes)

    def test_same_closed_episode_from_state_and_ledger_is_deduplicated(self):
        state = state_payload()
        state["closed_positions"] = [{
            "name": "NVIDIA",
            "code": "NVDA",
            "market": "US",
            "asset_type": "stock",
            "decision_date": "2026-06-01",
            "closed_date": "2026-06-03",
        }]
        performance = {"closed_positions": [{
            "name": "NVIDIA",
            "code": "NVDA",
            "market": "US",
            "asset_type": "stock",
            "opened_date": "2026-06-01",
            "closed_date": "2026-06-03",
            "closed_price": 150.0,
            "realized_pnl": 12.5,
        }]}

        output = build_output_model(state, performance, today_str="2026-06-07")

        self.assertEqual(len(output["closed_positions"]), 1)
        self.assertEqual(output["closed_positions"][0]["closed_price"], 150.0)
        self.assertEqual(output["closed_positions"][0]["realized_pnl"], 12.5)

    def test_markdown_report_lists_deferred_post_urls(self):
        state = state_payload()
        state["deferred_posts"] = [
            {
                "title": "요약 실패 글",
                "date": "2026-06-08",
                "reason": "SummaryResponseError: 잘린 JSON",
                "url": "https://blog.naver.com/ranto28/223456789012",
            }
        ]
        output = build_output_model(state, {}, today_str="2026-06-08")

        report = build_markdown_report(output)

        self.assertIn("## 분석 보류 글", report)
        self.assertIn("요약 실패 글", report)
        self.assertIn("https://blog.naver.com/ranto28/223456789012", report)

    def test_output_distinguishes_mer_origin_from_ai_management_and_actual_weight(self):
        state = state_payload()
        state["portfolio"][0].update({
            "provenance_status": "verified",
            "origin_signal_type": "MER_DIRECT",
        })
        performance = {
            "active_positions": [{
                "code": "AA",
                "return_pct_krw": 1.0,
                "actual_weight": 7.25,
            }],
            "actual_cash_weight": 24.5,
        }

        output = build_output_model(state, performance, today_str="2026-06-08")

        self.assertEqual(output["portfolio"][0]["actor_label"], "메르 직접 신호 · AI 관리")
        self.assertEqual(output["portfolio"][0]["actual_weight"], 7.25)
        self.assertEqual(output["chart_rows"][0]["weight"], 7.25)
        self.assertEqual(output["actual_cash_weight"], 24.5)
        self.assertEqual(output["actual_stock_weight"], 7.25)

    def test_output_limits_active_watchlist_to_ten_and_keeps_change_counts(self):
        state = state_payload()
        template = state["watchlist"][0]
        state["watchlist"] = [
            {
                **template,
                "name": f"관심 {index}",
                "code": str(index),
                "thesis_id": f"t-{index}",
                "latest_material_signal_date": f"2026-05-{index + 1:02d}",
            }
            for index in range(12)
        ]
        state["last_watchlist_changes"] = {
            "date": "2026-06-08",
            "added": ["t-11"],
            "updated": [],
            "promoted": ["t-10"],
            "rejected": [],
            "expired": [],
            "archived": [],
        }

        output = build_output_model(state, {}, today_str="2026-06-08")

        self.assertEqual(len(output["watchlist"]), 10)
        self.assertEqual(output["watchlist_total"], 12)
        self.assertEqual(output["watchlist_hidden_count"], 2)
        self.assertEqual(output["watchlist_changes"]["added"], ["t-11"])
        self.assertEqual(output["watchlist"][0]["name"], "관심 11")

    def test_actual_weight_matching_uses_full_security_identity(self):
        state = state_payload()
        us_item = state["portfolio"][0]
        us_item.update({"name": "US Security", "code": "123456", "market": "US"})
        kr_item = deepcopy(us_item)
        kr_item.update({"name": "KR Security", "market": "KR"})
        state["portfolio"].append(kr_item)
        performance = {
            "active_positions": [
                {"key": "stock:US:123456", "code": "123456", "market": "US", "asset_type": "stock", "actual_weight": 3.0},
                {"key": "stock:KR:123456", "code": "123456", "market": "KR", "asset_type": "stock", "actual_weight": 7.0},
            ]
        }

        output = build_output_model(state, performance, today_str="2026-06-08")

        weights = {item["market"]: item["actual_weight"] for item in output["portfolio"]}
        self.assertEqual(weights, {"US": 3.0, "KR": 7.0})


if __name__ == "__main__":
    unittest.main()
