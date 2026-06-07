import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import generate_dashboard
from test_portfolio_schema import state_payload


class GenerateDashboardTest(unittest.TestCase):
    def test_png_weights_use_structured_state_and_include_cash(self):
        weights = generate_dashboard._weights_from_state(state_payload())

        self.assertEqual(weights[0]["name"], "Alcoa")
        self.assertEqual(weights[0]["weight"], 8.0)
        self.assertEqual(weights[-1], {
            "name": "현금",
            "weight": 92.0,
            "action": "보유",
            "market": "ETF",
        })

    def test_html_distinguishes_model_portfolio_and_decision_actor(self):
        cache = {
            "active_positions": [
                {
                    "code": "AA",
                    "return_pct_krw": 3.6,
                    "entry_date": "2026-05-28",
                }
            ],
            "report_summaries": [
                {"date": "2026-05-28", "avg_return_krw": 3.6},
            ],
        }

        with TemporaryDirectory() as tmp:
            previous = generate_dashboard.DASHBOARD_FILE
            generate_dashboard.DASHBOARD_FILE = Path(tmp) / "dashboard.html"
            try:
                state = state_payload()
                state["status_note"] = "새 글 1건 요약 실패로 투자 분석 보류: 코스트코와 이마트"
                path = generate_dashboard.generate_html(
                    cache,
                    "# 메르AI 포트폴리오 분석\n\n테스트 보고서",
                    "2026-06-01",
                    state=state,
                )
                html = path.read_text(encoding="utf-8")
            finally:
                generate_dashboard.DASHBOARD_FILE = previous

        self.assertIn("메르 블로거의 실제 보유 내역이 아닙니다", html)
        self.assertIn('"decision_actor": "AI"', html)
        self.assertIn("Watchlist", html)
        self.assertIn("${i+1}. ${esc(r.title)}", html)
        self.assertIn("알루미늄 공급 제한", html)
        self.assertIn("메르 직접 발언", html)
        self.assertIn("AI 제안", html)
        self.assertIn("https://blog.naver.com/ranto28/123", html)
        self.assertIn("새 글 1건 요약 실패로 투자 분석 보류", html)
        self.assertIn("코스트코와 이마트", html)
        self.assertIn("모두 펼치기", html)
        self.assertIn("type:'doughnut'", html)
        self.assertNotIn("type:'bar'", html)


if __name__ == "__main__":
    unittest.main()
