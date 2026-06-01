import unittest

from telegram_notify import build_structured_summary
from test_portfolio_schema import state_payload


class TelegramSummaryTest(unittest.TestCase):
    def test_summary_labels_model_portfolio_and_ai_decision(self):
        summary = build_structured_summary(state_payload(), "2026년 06월 01일")

        self.assertIn("메르 블로거의 실제 보유 내역이 아닙니다", summary)
        self.assertIn("[AI] Alcoa (AA) — 매수 8%", summary)
        self.assertIn("Watchlist: 1건", summary)

    def test_no_change_summary_is_short_performance_message(self):
        summary = build_structured_summary(
            state_payload(),
            "2026년 06월 01일",
            {"portfolio_return_krw": 3.25},
            no_changes=True,
        )

        self.assertIn("오늘의 성과 요약", summary)
        self.assertIn("모델 포트폴리오 수익률: +3.2%", summary)
        self.assertIn("포트폴리오 변경 없음", summary)
        self.assertNotIn("[AI] Alcoa", summary)


if __name__ == "__main__":
    unittest.main()
