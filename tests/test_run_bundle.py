import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import run_bundle


class RunBundleTest(unittest.TestCase):
    def test_commits_all_json_payloads_and_removes_manifest(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "state.json"
            second = root / "ledger.json"
            manifest = root / ".pending-run.json"

            run_bundle.commit_json_bundle(
                {first: {"value": 1}, second: {"value": 2}},
                manifest_path=manifest,
            )

            self.assertEqual(json.loads(first.read_text(encoding="utf-8")), {"value": 1})
            self.assertEqual(json.loads(second.read_text(encoding="utf-8")), {"value": 2})
            self.assertFalse(manifest.exists())

    def test_recovers_after_interruption_during_publish(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "state.json"
            second = root / "ledger.json"
            manifest = root / ".pending-run.json"
            original_replace = run_bundle.os.replace
            published = 0

            def interrupted(source, target):
                nonlocal published
                if str(source).endswith(".staged"):
                    published += 1
                    if published == 2:
                        raise OSError("simulated interruption")
                return original_replace(source, target)

            with patch.object(run_bundle.os, "replace", side_effect=interrupted):
                with self.assertRaisesRegex(OSError, "simulated interruption"):
                    run_bundle.commit_json_bundle(
                        {first: {"value": 1}, second: {"value": 2}},
                        manifest_path=manifest,
                    )

            self.assertTrue(manifest.exists())
            self.assertTrue(run_bundle.recover_pending_bundle(manifest))
            self.assertEqual(json.loads(first.read_text(encoding="utf-8")), {"value": 1})
            self.assertEqual(json.loads(second.read_text(encoding="utf-8")), {"value": 2})
            self.assertFalse(manifest.exists())


if __name__ == "__main__":
    unittest.main()
