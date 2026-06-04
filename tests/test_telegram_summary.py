import unittest

from telegram_notify import build_structured_summary, split_telegram_message
from test_portfolio_schema import state_payload


class TelegramSummaryTest(unittest.TestCase):
    def test_summary_labels_model_portfolio_and_ai_decision(self):
        summary = build_structured_summary(state_payload(), "2026년 06월 01일")

        self.assertIn("메르 블로거의 실제 보유 내역이 아닙니다", summary)
        self.assertIn("알루미늄 공급 제한", summary)
        self.assertIn("Alcoa (AA) | AI 제안 · 매수 | 8%", summary)
        self.assertIn("우주 데이터센터", summary)

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

    def test_changed_summary_still_includes_performance(self):
        summary = build_structured_summary(
            state_payload(),
            "2026년 06월 01일",
            {"portfolio_return_krw": 1.94},
        )

        self.assertIn("오늘의 성과 요약", summary)
        self.assertIn("모델 포트폴리오 수익률: +1.9%", summary)
        self.assertIn("핵심 인사이트", summary)

    def test_long_summary_is_split_without_dropping_tail(self):
        text = "\n".join(f"인사이트 {index}: " + ("x" * 40) for index in range(10))

        messages = split_telegram_message(text, max_length=120)

        self.assertGreater(len(messages), 1)
        self.assertEqual("\n".join(messages), text)
        self.assertTrue(all(len(message) <= 120 for message in messages))


if __name__ == "__main__":
    unittest.main()
