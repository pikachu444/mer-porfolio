import argparse
import json
import tempfile
import unittest
from pathlib import Path

from scripts.evaluate_llm_provider import (
    PROVIDERS,
    build_chat_payload,
    prepare_evaluation,
    select_evaluation_posts,
)


class EvaluateLlmProviderTests(unittest.TestCase):
    def test_provider_defaults_are_explicit(self):
        self.assertEqual(PROVIDERS["cerebras"].api_key_env, "CEREBRAS_API_KEY")
        self.assertEqual(PROVIDERS["opencode-zen"].api_key_env, "OPENCODE_API_KEY")

    def test_selects_recent_posts_and_excludes_known_unrelated_posts(self):
        posts = [
            {"date": "2999-01-03", "title": "관련", "investment_relevant": True},
            {"date": "2999-01-02", "title": "미분류"},
            {"date": "2999-01-01", "title": "무관", "investment_relevant": False},
        ]
        selected = select_evaluation_posts(posts, days=365000, limit=None)
        self.assertEqual([post["title"] for post in selected], ["관련", "미분류"])

    def test_builds_openai_compatible_messages(self):
        payload = build_chat_payload("model-id", "system", "user")
        self.assertEqual(payload["model"], "model-id")
        self.assertEqual(payload["messages"][0], {"role": "system", "content": "system"})
        self.assertEqual(payload["messages"][1], {"role": "user", "content": "user"})

    def test_prepares_decision_request_without_api_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            posts = root / "posts.json"
            state = root / "state.json"
            posts.write_text(
                json.dumps(
                    [{"date": "2999-01-01", "title": "테스트", "url": "https://example.com", "content": "본문"}],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            state.write_text(
                json.dumps(
                    {
                        "schema_version": "2.0",
                        "portfolio": [],
                        "watchlist": [],
                        "closed_positions": [],
                        "decision_history": [],
                        "insights": [],
                        "last_rebalanced_date": None,
                        "last_updated": "2999-01-01",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            metadata, payload, selected, _ = prepare_evaluation(
                posts, state, "2999-01-02", 365000, None, "model-id"
            )
        self.assertEqual(metadata["post_count"], 1)
        self.assertEqual(len(selected), 1)
        self.assertIn("테스트", payload["messages"][1]["content"])


if __name__ == "__main__":
    unittest.main()
