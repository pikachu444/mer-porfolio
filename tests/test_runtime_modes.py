import unittest
import sys
from datetime import date
from datetime import datetime
from unittest.mock import Mock, patch

try:
    import feedparser  # noqa: F401
except ModuleNotFoundError:
    sys.modules["feedparser"] = Mock()

import main
from main import _is_llm_service_unavailable_error
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

    def test_no_change_update_creates_today_report_and_passes_status_note_to_dashboard(self):
        note = "새 글 1건 요약 실패로 투자 분석 보류: 코스트코와 이마트"
        state = main._empty_state()

        with patch.object(main, "load_model_ledger", return_value={}), \
             patch.object(main, "save_portfolio_state_file"), \
             patch.object(main, "_save_report") as save_report, \
             patch.object(main, "generate_all", return_value=(None, None)) as generate_all, \
             patch.object(main, "RUN_POLICY", Mock(send_telegram=False)):
            result = main._run_no_change_update(
                state,
                datetime(2026, 6, 7),
                status_note=note,
            )

        self.assertEqual(result, 0)
        saved_report = save_report.call_args.args[0]
        self.assertIn("기준일: 2026-06-07", saved_report)
        self.assertIn(note, saved_report)
        generated_state = generate_all.call_args.kwargs["state"]
        self.assertEqual(generated_state["status_note"], note)


if __name__ == "__main__":
    unittest.main()
