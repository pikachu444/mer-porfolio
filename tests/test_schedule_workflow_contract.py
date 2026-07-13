"""Static contracts for the live scheduled-report workflow.

The deployment environment parses the workflow, but these checks protect the
two delivery invariants that are easy to accidentally remove in a YAML edit:
one KST-day recovery trigger and a durable at-most-once delivery receipt.
"""

from pathlib import Path
import unittest


WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "schedule.yml"


class ScheduleWorkflowContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    def test_primary_and_morning_recovery_crons_are_present(self):
        self.assertIn('- cron: "17 15 * * *"', self.workflow)  # 00:17 KST
        self.assertIn('- cron: "17 0 * * *"', self.workflow)   # 09:17 KST

    def test_schedule_guard_uses_remote_state_and_legacy_scheduled_receipt(self):
        self.assertIn('git fetch origin "refs/heads/$TARGET_BRANCH:refs/remotes/origin/$TARGET_BRANCH"', self.workflow)
        self.assertIn('f"origin/{branch}:{path}"', self.workflow)
        self.assertIn('remote_json("output/scheduled_delivery_receipt.json")', self.workflow)
        self.assertIn('remote_json("output/run_status.json")', self.workflow)
        self.assertIn('legacy_scheduled_delivery_status_unknown', self.workflow)
        self.assertIn('invalid_run_status_for_delivery_guard', self.workflow)
        self.assertIn('mode = run_status.get("mode")', self.workflow)
        self.assertIn('mode == "scheduled" and completed_date == today', self.workflow)

    def test_prepared_and_delivered_receipts_are_both_enforced(self):
        self.assertIn('"delivery_status": "prepared"', self.workflow)
        self.assertIn('receipt["delivery_status"] = "delivered"', self.workflow)
        self.assertIn('Stop unknown scheduled delivery from being resent', self.workflow)
        self.assertIn('Enforce scheduled delivery receipt', self.workflow)
        self.assertIn('automatic retry is blocked to prevent duplicates', self.workflow)


if __name__ == "__main__":
    unittest.main()
