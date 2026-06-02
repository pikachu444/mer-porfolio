import json
import sys
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

try:
    import feedparser  # noqa: F401
except ModuleNotFoundError:
    sys.modules["feedparser"] = Mock()

import fetch_mer
import main


def post(date: str, url: str, relevant=True) -> dict:
    return {
        "title": url,
        "date": date,
        "url": url,
        "content": "본문",
        "summary": "요약",
        "investment_relevant": relevant,
        "relevance_reason": "테스트",
        "summary_version": fetch_mer.SUMMARY_VERSION,
    }


class FetchMerTest(unittest.TestCase):
    def test_parse_summary_response_preserves_relevance(self):
        parsed = fetch_mer._parse_summary_response(
            json.dumps(
                {
                    "investment_relevant": False,
                    "relevance_reason": "일상 글",
                    "summary": "가벼운 주말 이야기",
                },
                ensure_ascii=False,
            )
        )

        self.assertFalse(parsed["investment_relevant"])
        self.assertEqual(parsed["summary_version"], fetch_mer.SUMMARY_VERSION)

    def test_summary_request_trims_only_transmitted_tail_over_safe_limit(self):
        client = Mock()
        client.models.count_tokens.side_effect = lambda model, contents: Mock(
            total_tokens=len(contents)
        )
        original = "x" * 120

        with patch.object(fetch_mer, "MODEL_INPUT_TOKEN_LIMIT", 100):
            request = fetch_mer._fit_summary_request(client, original)

        self.assertIn("전송용 본문 끝부분 생략", request)
        self.assertEqual(original, "x" * 120)

    def test_daily_analysis_selects_only_new_relevant_posts(self):
        posts = [
            post("2026-06-01", "new-relevant"),
            post("2026-06-01", "new-irrelevant", relevant=False),
            post("2026-05-31", "old-relevant"),
        ]

        selected = fetch_mer.select_new_relevant_posts(
            posts,
            {"new-relevant", "new-irrelevant"},
        )

        self.assertEqual([item["url"] for item in selected], ["new-relevant"])

    def test_rebalance_selects_relevant_posts_after_last_actual_rebalance(self):
        posts = [
            post("2026-06-01", "new"),
            post("2026-05-29", "irrelevant", relevant=False),
            post("2026-05-18", "old"),
        ]

        selected = fetch_mer.select_rebalance_posts(
            posts,
            "2026-05-20",
            datetime(2026, 6, 1),
        )

        self.assertEqual([item["url"] for item in selected], ["new"])

    def test_collection_does_not_expand_to_thirty_day_fallback(self):
        cached = [post("2026-06-01", "cached")]
        with patch.object(main, "fetch_recent_posts") as fetch, \
             patch.object(main, "load_cached_posts", return_value=cached):
            selected = main._collect_posts()

        fetch.assert_called_once_with(days=main.FETCH_DAYS)
        self.assertEqual(selected, cached)


if __name__ == "__main__":
    unittest.main()
