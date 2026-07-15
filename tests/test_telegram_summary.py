import unittest

from telegram_notify import build_structured_summary, split_telegram_message
from test_portfolio_schema import state_payload


class TelegramSummaryTest(unittest.TestCase):
    def test_briefing_shows_article_insight_holding_weights_and_cash(self):
        summary = build_structured_summary(state_payload(), "2026-06-01")
        for text in ("메르AI 투자 브리핑", "오늘 읽은 글", "투자 인사이트", "Alcoa", "메르 논지 기반 AI 추론", "목표 8.00%", "현금 92.00%"):
            self.assertIn(text, summary)

    def test_policy_correction_is_described_as_administrative(self):
        state = state_payload()
        state["policy_correction_notice"] = True
        summary = build_structured_summary(state, "2026-06-01")
        self.assertIn("광범위 지수 ETF 고정 편입 정책을 제거했습니다", summary)
        self.assertIn("시스템 정책 정정", summary)

    def test_all_holdings_are_kept_when_message_is_split(self):
        state = state_payload()
        base = state["portfolio"][0]
        state["portfolio"] = [dict(base, name=f"종목{i}", code=f"{i:06d}") for i in range(24)]
        summary = build_structured_summary(state, "2026-06-01")
        joined = "\n".join(split_telegram_message(summary, max_length=400))
        self.assertIn("종목23", joined)

    def test_split_preserves_text(self):
        text = "\n".join("줄 " + "x" * 40 for _ in range(10))
        self.assertEqual("\n".join(split_telegram_message(text, max_length=120)), text)
