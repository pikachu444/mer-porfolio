import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

try:
    import feedparser  # noqa: F401
except ModuleNotFoundError:
    sys.modules["feedparser"] = Mock()

import fetch_mer
import main


class FeedEntry(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


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

    def test_parse_summary_response_reports_truncated_json(self):
        with self.assertRaises(fetch_mer.SummaryResponseError) as ctx:
            fetch_mer._parse_summary_response('{"investment_relevant": true, "summary": "abc')

        self.assertIn("JSON 파싱 실패", str(ctx.exception))

    def test_summary_fields_defers_unusable_flash_response(self):
        with patch.object(
            fetch_mer,
            "summarize_single_post",
            side_effect=fetch_mer.SummaryResponseError("잘린 JSON"),
        ):
            fields = fetch_mer._summary_fields("본문", "테스트 글")

        self.assertFalse(fields["investment_relevant"])
        self.assertIsNone(fields["summary_version"])
        self.assertEqual(fields["summary_status"], "deferred")
        self.assertIn("잘린 JSON", fields["summary_error"])

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

    def test_posts_without_summary_are_not_sent_to_analysis(self):
        no_summary = post("2026-06-01", "no-summary")
        no_summary["summary"] = ""
        no_summary["summary_status"] = "skipped"
        ready = post("2026-06-01", "ready")

        selected = fetch_mer.select_new_relevant_posts(
            [no_summary, ready],
            {"no-summary", "ready"},
        )

        self.assertEqual([item["url"] for item in selected], ["ready"])

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

    def test_deferred_status_note_names_post_for_user_outputs(self):
        note = main._deferred_status_note([
            {"title": "코스트코와 이마트 트레이더스의 비밀(feat 97헌터)"}
        ])

        self.assertIn("새 글 1건 요약 실패", note)
        self.assertIn("코스트코와 이마트", note)

    def test_fetch_recent_posts_persists_deferred_summary(self):
        entry = FeedEntry(
            title="신규 글",
            link="https://blog.naver.com/ranto28/223456789012",
            published_parsed=(2026, 6, 1, 0, 0, 0, 0, 0, 0),
            summary="RSS 요약",
        )
        feed = Mock(bozo=False, entries=[entry])

        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(fetch_mer, "DB_FILE", str(Path(tmpdir) / "posts_db.json")), \
             patch.object(fetch_mer, "RSS_URL", "https://example.test/rss"), \
             patch.object(fetch_mer.feedparser, "parse", return_value=feed), \
             patch.object(fetch_mer, "fetch_full_post", return_value="전문"), \
             patch.object(fetch_mer, "summarize_single_post", side_effect=fetch_mer.SummaryResponseError("잘린 JSON")), \
             patch.object(fetch_mer, "_refresh_recent_summary_cache", return_value=False):
            result = fetch_mer.fetch_recent_posts(days=9999)

            db_path = Path(fetch_mer.DB_FILE)
            saved = json.loads(db_path.read_text(encoding="utf-8"))

        self.assertEqual(len(result), 1)
        self.assertEqual(saved[0]["summary_status"], "deferred")
        self.assertIsNone(saved[0]["summary_version"])
        self.assertFalse(saved[0]["investment_relevant"])

    def test_refresh_recent_summary_cache_retries_deferred_summary(self):
        posts = [post("2026-06-01", "https://blog.naver.com/ranto28/223456789012")]
        posts[0]["summary_version"] = None
        posts[0]["summary_status"] = "deferred"

        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(fetch_mer, "DB_FILE", str(Path(tmpdir) / "posts_db.json")), \
             patch.object(fetch_mer, "fetch_full_post", return_value="새 전문"), \
             patch.object(
                 fetch_mer,
                 "_summary_fields",
                 return_value={
                     "summary": "새 요약",
                     "investment_relevant": True,
                     "relevance_reason": "투자 관련",
                     "summary_version": fetch_mer.SUMMARY_VERSION,
                     "summary_status": "ok",
                     "summary_error": "",
                 },
             ) as summary_fields:
            changed = fetch_mer._refresh_recent_summary_cache(posts, datetime(2026, 6, 2))

        self.assertTrue(changed)
        summary_fields.assert_called_once_with("새 전문", posts[0]["title"])
        self.assertEqual(posts[0]["summary"], "새 요약")
        self.assertEqual(posts[0]["summary_version"], fetch_mer.SUMMARY_VERSION)


if __name__ == "__main__":
    unittest.main()
