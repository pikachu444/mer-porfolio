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

    def test_current_portfolio_item_is_not_shown_as_closed_position(self):
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
        self.assertNotIn("001440", closed_codes)
        self.assertIn("NVDA", closed_codes)

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


if __name__ == "__main__":
    unittest.main()
