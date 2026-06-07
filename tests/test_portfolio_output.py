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
        self.assertIn("| 대한전선 | 001440 | AI 제안 · 매수 | 3%", report)
        self.assertIn("### 해외주식 추천", report)
        self.assertIn("| Alcoa | AA | AI 제안 · 매수 | 8%", report)
        self.assertIn("수익률 집계 전 종목: AA, 001440", report)

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


if __name__ == "__main__":
    unittest.main()
