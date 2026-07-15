import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CurrentOutputSnapshotTest(unittest.TestCase):
    def test_current_approved_state_and_admin_queue_are_separate(self):
        state = json.loads((ROOT / "output" / "portfolio_state.json").read_text(encoding="utf-8"))
        approved = state["portfolio"]
        queue = state.get("admin_review_queue", [])
        self.assertEqual(len(approved), 2)
        self.assertEqual(len(queue), 16)
        self.assertTrue(all(item.get("provenance_status") == "verified" for item in approved))
        self.assertTrue(all(item.get("queue_status") == "pending_admin" for item in queue))
        self.assertEqual(sum(item.get("name") == "HLB" for item in state["watchlist"]), 1)

    def test_current_user_outputs_have_no_internal_validator_terms(self):
        report = (ROOT / "output" / "report_20260715.md").read_text(encoding="utf-8")
        dashboard = (ROOT / "output" / "dashboard.html").read_text(encoding="utf-8")
        summary_terms = "\n".join((report, dashboard))
        for term in (
            "legacy_unvalidated",
            "provenance_status",
            "source_mentioned",
            "weight_source",
            "clean epoch",
            "decisions[",
            "links signals",
            "validator",
            "재검증 필요 포지션",
            "국내주식 추천",
            "해외주식 추천",
        ):
            self.assertNotIn(term, summary_terms)
        self.assertIn("현재 보유 종목", report)
        self.assertIn("오늘의 조정", report)
        self.assertIn('name="viewport"', dashboard)
        self.assertGreaterEqual(len(re.findall(r"\d+\.\d{2}%", report)), 8)


if __name__ == "__main__":
    unittest.main()
