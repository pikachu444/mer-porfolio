import unittest

from telegram_notify import build_structured_summary, split_telegram_message
from test_portfolio_schema import state_payload


class TelegramSummaryTest(unittest.TestCase):
    def test_summary_labels_model_portfolio_and_ai_decision(self):
        summary = build_structured_summary(state_payload(), "2026년 06월 01일")

        self.assertIn("메르 블로거의 실제 보유 내역이 아닙니다", summary)
        self.assertIn("알루미늄 공급 제한", summary)
        self.assertIn("*해외주식 추천*", summary)
        self.assertIn("Alcoa (AA) — Buy (목표 8% / 실제 집계 전)", summary)
        self.assertNotIn("AI 제안 · 매수", summary)
        self.assertIn("우주 데이터센터", summary)

    def test_no_change_summary_is_short_performance_message(self):
        summary = build_structured_summary(
            state_payload(),
            "2026년 06월 01일",
            {
                "portfolio_return_krw": 3.25,
                "active_positions": [{"code": "AA", "return_pct_krw": 3.25}],
            },
            no_changes=True,
        )

        self.assertIn("오늘의 성과 요약", summary)
        self.assertIn("모델 포트폴리오 수익률: +3.2%", summary)
        self.assertIn("포트폴리오 변경 없음", summary)
        self.assertIn("*해외주식 추천*", summary)
        self.assertIn("Alcoa (AA) — Buy (목표 8% / 실제 집계 전)", summary)
        self.assertNotIn("수익률", summary.split("*해외주식 추천*", 1)[1])

    def test_no_change_summary_can_explain_deferred_analysis(self):
        state = state_payload()
        state["insights"].append({
            "id": "robotics",
            "title": "피지컬 AI 확산",
            "summary": "로봇과 AI 결합이 산업 자동화 수요를 키움",
            "investment_implication": "원문에 등장한 로봇 관련 기업을 추적",
            "evidence_posts": [],
            "related_decision_codes": [],
        })
        summary = build_structured_summary(
            state,
            "2026년 06월 01일",
            {
                "portfolio_return_krw": 3.25,
                "active_positions": [{"code": "AA", "return_pct_krw": 3.25}],
            },
            no_changes=True,
            status_note="LLM 한도 초과 또는 일시 장애로 신규 글 분석 보류",
        )

        self.assertIn("LLM 한도 초과 또는 일시 장애로 신규 글 분석 보류", summary)
        self.assertIn("포트폴리오 변경 없음", summary)
        self.assertIn("1. *알루미늄 공급 제한*", summary)
        self.assertIn("2. *피지컬 AI 확산*", summary)

    def test_status_note_can_name_deferred_post(self):
        state = state_payload()
        state["deferred_posts"] = [
            {
                "title": "코스트코와 이마트 트레이더스",
                "url": "https://blog.naver.com/ranto28/223456789012",
                "date": "2026-06-08",
                "reason": "SummaryResponseError: 잘린 JSON",
            }
        ]
        summary = build_structured_summary(
            state,
            "2026년 06월 01일",
            {
                "portfolio_return_krw": 3.25,
                "active_positions": [{"code": "AA", "return_pct_krw": 3.25}],
            },
            status_note="새 글 1건 요약 실패로 투자 분석 보류: 코스트코와 이마트 트레이더스",
        )

        self.assertIn("새 글 1건 요약 실패", summary)
        self.assertIn("코스트코와 이마트 트레이더스", summary)
        self.assertIn("분석 보류 글", summary)
        self.assertIn("https://blog.naver.com/ranto28/223456789012", summary)

    def test_changed_summary_still_includes_performance(self):
        summary = build_structured_summary(
            state_payload(),
            "2026년 06월 01일",
            {
                "portfolio_return_krw": 1.94,
                "active_positions": [{"code": "AA", "return_pct_krw": 1.94}],
            },
        )

        self.assertIn("오늘의 성과 요약", summary)
        self.assertIn("모델 포트폴리오 수익률: +1.9%", summary)
        self.assertIn("핵심 인사이트", summary)
        self.assertIn("*해외주식 추천*", summary)

    def test_summary_uses_actual_position_weights_and_distinguishes_targets(self):
        summary = build_structured_summary(
            state_payload(),
            "2026년 06월 01일",
            {
                "portfolio_return_krw": 1.25,
                "active_positions": [{
                    "code": "AA",
                    "return_pct_krw": 1.25,
                    "actual_weight": 6.5,
                }],
                "actual_cash_weight": 93.5,
            },
        )

        self.assertIn("주식 노출 목표: 8% / 실제: 6.5%", summary)
        self.assertIn("Alcoa (AA) — Buy (목표 8% / 실제 6.5%)", summary)

    def test_insights_are_numbered_without_dropping_items(self):
        state = state_payload()
        state["insights"].append({
            "id": "robotics",
            "title": "피지컬 AI 확산",
            "summary": "로봇과 AI 결합이 산업 자동화 수요를 키움",
            "investment_implication": "원문에 등장한 로봇 관련 기업을 추적",
            "evidence_posts": [],
            "related_decision_codes": [],
        })

        summary = build_structured_summary(state, "2026년 06월 01일")

        self.assertIn("1. *알루미늄 공급 제한*", summary)
        self.assertIn("2. *피지컬 AI 확산*", summary)
        self.assertEqual(summary.count("시사점:"), len(state["insights"]))

    def test_validation_summary_does_not_link_stale_operating_dashboard(self):
        summary = build_structured_summary(
            state_payload(),
            "2026년 06월 01일",
            include_dashboard_link=False,
        )

        self.assertNotIn("대시보드 전체 보기", summary)
        self.assertIn("검증 모드", summary)
        self.assertIn("GitHub Actions artifact", summary)

    def test_long_summary_is_split_without_dropping_tail(self):
        text = "\n".join(f"인사이트 {index}: " + ("x" * 40) for index in range(10))

        messages = split_telegram_message(text, max_length=120)

        self.assertGreater(len(messages), 1)
        self.assertEqual("\n".join(messages), text)
        self.assertTrue(all(len(message) <= 120 for message in messages))


if __name__ == "__main__":
    unittest.main()
