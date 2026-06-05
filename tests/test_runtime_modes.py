import unittest
from datetime import date

from main import _extract_insights_from_report, _is_llm_service_unavailable_error
from runtime_modes import get_run_policy, should_rebalance


class RuntimeModesTest(unittest.TestCase):
    def test_verify_sends_telegram_without_persisting_operating_state(self):
        policy = get_run_policy("verify")

        self.assertFalse(policy.persist_operating_state)
        self.assertTrue(policy.send_telegram)
        self.assertTrue(policy.upload_artifact)

    def test_test_mode_uses_fixture_without_telegram_or_state_write(self):
        policy = get_run_policy("test")

        self.assertTrue(policy.use_fixture)
        self.assertFalse(policy.persist_operating_state)
        self.assertFalse(policy.send_telegram)

    def test_scheduled_rebalances_after_fourteen_days(self):
        today = date(2026, 6, 1)

        self.assertFalse(should_rebalance("scheduled", "2026-05-19", today))
        self.assertTrue(should_rebalance("scheduled", "2026-05-18", today))
        self.assertTrue(should_rebalance("scheduled", None, today))

    def test_manual_rebalance_always_rebalances(self):
        self.assertTrue(should_rebalance("rebalance", "2026-05-31", date(2026, 6, 1)))

    def test_rejects_old_adhoc_mode(self):
        with self.assertRaisesRegex(ValueError, "unknown RUN_MODE"):
            get_run_policy("adhoc")

    def test_identifies_first_stage_llm_service_unavailability(self):
        exc = RuntimeError("1차 포트폴리오 판단 실패. gemini-2.5-flash: 429 RESOURCE_EXHAUSTED")

        self.assertTrue(_is_llm_service_unavailable_error(exc))
        self.assertFalse(_is_llm_service_unavailable_error(RuntimeError("검증 오류")))

    def test_extracts_numbered_insights_from_legacy_markdown_report(self):
        report = """# 메르AI 포트폴리오 리포트

## 📌 시장 분석 핵심 인사이트

### 인사이트 1: AI/로봇

1. 엔비디아가 피지컬 AI를 강조함.
2. 두산로보틱스와 네이버가 협력 대상으로 언급됨.

**해석(나비효과):**
3. 관련 기업의 성장 동력이 커짐.

**투자판단:** Buy 강 — 협력 기대가 높음.

### 인사이트 2: 비만 치료제

1. 일라이 릴리 신약 결과가 긍정적임.

**투자판단:** Buy — 시장 확대 기대.

## 📊 포트폴리오 추천
"""

        insights = _extract_insights_from_report(report)

        self.assertEqual(len(insights), 2)
        self.assertEqual(insights[0]["title"], "AI/로봇")
        self.assertIn("엔비디아", insights[0]["summary"])
        self.assertIn("Buy 강", insights[0]["investment_implication"])


if __name__ == "__main__":
    unittest.main()
