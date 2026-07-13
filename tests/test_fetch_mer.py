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
        "signal_candidates": [],
    }


class FetchMerTest(unittest.TestCase):
    def test_parse_summary_response_preserves_relevance(self):
        parsed = fetch_mer._parse_summary_response(
            json.dumps(
                {
                    "investment_relevant": False,
                    "relevance_reason": "일상 글",
                    "summary": "가벼운 주말 이야기",
                    "signal_candidates": [],
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

    def test_signal_candidates_require_exact_normalized_source_quote(self):
        source = "메르는 삼성전자를 보유하고 있다고 말했다.\n공급은 줄어든다."
        payload = {
            "investment_relevant": True,
            "relevance_reason": "직접 보유 발언",
            "summary": "삼성전자 보유 언급",
            "signal_candidates": [
                {
                    "exact_text": "메르는  삼성전자를 보유하고 있다고 말했다.",
                    "classification": "MER_DIRECT",
                    "entity_name": "삼성전자",
                    "entity_type": "company",
                    "direction": "positive",
                    "horizon_kind": "structural",
                    "catalysts": [],
                    "invalidation_conditions": [],
                    "thesis_summary": "메르가 삼성전자 보유를 직접 밝혔다.",
                },
                {
                    "exact_text": "원문에 존재하지 않는 매수 발언",
                    "classification": "MER_DIRECT",
                    "entity_name": "SK하이닉스",
                    "entity_type": "company",
                    "direction": "positive",
                    "horizon_kind": "tactical",
                    "catalysts": [],
                    "invalidation_conditions": [],
                    "thesis_summary": "존재하지 않는 근거",
                },
            ],
        }

        parsed = fetch_mer._parse_summary_response(
            json.dumps(payload, ensure_ascii=False),
            source_text=source,
            source_key="https://blog.naver.com/ranto28/1",
        )

        self.assertEqual(len(parsed["signal_candidates"]), 1)
        signal = parsed["signal_candidates"][0]
        self.assertEqual(signal["exact_text"], "메르는 삼성전자를 보유하고 있다고 말했다.")
        self.assertEqual(len(signal["signal_id"]), 64)
        self.assertEqual(len(signal["evidence_sha256"]), 64)
        self.assertEqual(
            signal["evidence_sha256"],
            fetch_mer.hashlib.sha256(signal["exact_text"].encode("utf-8")).hexdigest(),
        )

    def test_legacy_cached_post_gets_empty_signal_candidates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "posts_db.json"
            db_path.write_text(
                json.dumps([{"title": "legacy", "summary": "old"}], ensure_ascii=False),
                encoding="utf-8",
            )
            with patch.object(fetch_mer, "DB_FILE", str(db_path)):
                posts = fetch_mer.load_cached_posts()

        self.assertEqual(posts[0]["signal_candidates"], [])

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

    def test_summary_fields_defers_permanent_model_error(self):
        with patch.object(
            fetch_mer,
            "summarize_single_post",
            side_effect=RuntimeError("GEMINI_PERMANENT 404 NOT_FOUND model"),
        ):
            fields = fetch_mer._summary_fields("본문", "테스트 글")

        self.assertEqual(fields["summary_status"], "deferred")
        self.assertIn("GEMINI_PERMANENT", fields["summary_error"])

    def test_summary_uses_explicit_flash_lite_config(self):
        client = Mock()
        response = Mock(
            model_version="gemini-3.1-flash-lite-2026-05-07",
            text=json.dumps(
                {
                    "investment_relevant": True,
                    "relevance_reason": "투자 관련",
                    "summary": "요약",
                    "signal_candidates": [],
                },
                ensure_ascii=False,
            )
        )

        with patch.object(fetch_mer, "_get_summary_client", return_value=client), \
             patch.object(fetch_mer, "_fit_summary_request", return_value="request"), \
             patch.object(fetch_mer, "generate_content_with_retry", return_value=response) as call:
            result = fetch_mer.summarize_single_post("본문")

        self.assertEqual(result["summary"], "요약")
        self.assertEqual(result["summary_model_id"], "gemini-3.1-flash-lite")
        self.assertEqual(
            result["summary_model_version"],
            "gemini-3.1-flash-lite-2026-05-07",
        )
        self.assertEqual(call.call_args.kwargs["model"], "gemini-3.1-flash-lite")
        config = call.call_args.kwargs["config"]
        self.assertEqual(config.max_output_tokens, 2_048)
        self.assertIsNone(config.temperature)
        self.assertEqual(config.thinking_config.thinking_level.value, "MINIMAL")
        self.assertIn("signal_candidates", config.response_schema["properties"])

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

    def test_failed_pending_post_is_selected_again_without_new_url(self):
        pending = post("2026-06-01", "pending-relevant")
        pending["analysis_status"] = "pending"

        selected = fetch_mer.select_new_relevant_posts([pending], set())

        self.assertEqual([item["url"] for item in selected], ["pending-relevant"])

    def test_marks_post_completed_only_after_commit_acknowledgement(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "posts_db.json"
            path.write_text(
                json.dumps([{**post("2026-06-01", "done"), "analysis_status": "pending"}]),
                encoding="utf-8",
            )
            fetch_mer.mark_posts_analysis_completed({"done"}, "2026-06-02", path)
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(saved[0]["analysis_status"], "completed")
        self.assertEqual(saved[0]["analysis_completed_date"], "2026-06-02")

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

    def test_legacy_summary_version_is_not_ready_for_analysis(self):
        legacy = post("2026-06-01", "legacy")
        legacy["summary_version"] = fetch_mer.SUMMARY_VERSION - 1

        self.assertFalse(fetch_mer.is_ready_for_analysis(legacy))
        self.assertTrue(fetch_mer.is_ready_for_analysis(post("2026-06-01", "current")))

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
              patch.object(
                  fetch_mer,
                  "summarize_single_post",
                  side_effect=fetch_mer.SummaryResponseError("잘린 JSON"),
              ) as summarize:
            result = fetch_mer.fetch_recent_posts(days=9999)

            db_path = Path(fetch_mer.DB_FILE)
            saved = json.loads(db_path.read_text(encoding="utf-8"))

        self.assertEqual(len(result), 1)
        self.assertEqual(saved[0]["summary_status"], "deferred")
        self.assertIsNone(saved[0]["summary_version"])
        self.assertFalse(saved[0]["investment_relevant"])
        self.assertIn("summary_next_retry_at", saved[0])
        # The bounded retry scheduler must not call Gemini twice for the same
        # newly discovered post after its initial failure.
        summarize.assert_called_once()

    def test_refresh_recent_summary_cache_retries_deferred_summary(self):
        # A blocked pending post must remain retryable even after the ordinary
        # 14-day cache-upgrade window, otherwise it would block every run forever.
        posts = [post("2026-05-01", "https://blog.naver.com/ranto28/223456789012")]
        posts[0]["summary_version"] = None
        posts[0]["summary_status"] = "deferred"
        posts[0]["analysis_status"] = "not_relevant"

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
        summary_fields.assert_called_once_with(
            "새 전문",
            posts[0]["title"],
            posts[0]["url"],
        )
        self.assertEqual(posts[0]["summary"], "새 요약")
        self.assertEqual(posts[0]["summary_version"], fetch_mer.SUMMARY_VERSION)
        self.assertEqual(posts[0]["analysis_status"], "pending")

    def test_refresh_recent_summary_cache_bounds_legacy_schema_upgrade(self):
        now = datetime(2026, 7, 13, 0, 0, 0)
        posts = [
            post(f"2026-07-{day:02d}", f"https://blog.naver.com/ranto28/2234567890{day:02d}")
            for day in (12, 11, 10, 9, 8)
        ]
        for item in posts:
            item["summary_version"] = 2
            item["summary_status"] = "ok"
            item["analysis_status"] = "legacy_untracked"

        refreshed = {
            "summary": "v4 요약",
            "investment_relevant": True,
            "relevance_reason": "투자 관련",
            "summary_version": fetch_mer.SUMMARY_VERSION,
            "summary_status": "ok",
            "summary_error": "",
            "signal_candidates": [],
        }
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(fetch_mer, "DB_FILE", str(Path(tmpdir) / "posts_db.json")), \
             patch.object(fetch_mer, "SUMMARY_CACHE_UPGRADE_MAX_PER_RUN", 2), \
             patch.object(fetch_mer, "fetch_full_post", return_value=None), \
             patch.object(fetch_mer, "_summary_fields", return_value=refreshed) as summary_fields:
            changed = fetch_mer._refresh_recent_summary_cache(posts, now)

        self.assertTrue(changed)
        self.assertEqual(summary_fields.call_count, 2)
        self.assertEqual(posts[0]["summary_version"], fetch_mer.SUMMARY_VERSION)
        self.assertEqual(posts[1]["summary_version"], fetch_mer.SUMMARY_VERSION)
        self.assertEqual(posts[2]["summary_version"], 2)
        # Cache enrichment must not make a partial historical window look like
        # a newly pending investment signal.
        self.assertTrue(all(item["analysis_status"] == "legacy_untracked" for item in posts))

    def test_refresh_recent_summary_cache_honors_deferred_retry_cooldown(self):
        now = datetime(2026, 7, 13, 0, 0, 0)
        posts = [post("2026-05-01", "https://blog.naver.com/ranto28/223456789012")]
        posts[0].update({
            "summary": fetch_mer.SUMMARY_DEFERRED_TEXT,
            "summary_version": None,
            "summary_status": "deferred",
            "analysis_status": "pending",
            "summary_next_retry_at": "2026-07-14T00:00:00",
            "summary_retry_count": 1,
        })

        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(fetch_mer, "DB_FILE", str(Path(tmpdir) / "posts_db.json")), \
             patch.object(fetch_mer, "_summary_fields") as summary_fields:
            changed = fetch_mer._refresh_recent_summary_cache(posts, now)

        self.assertFalse(changed)
        summary_fields.assert_not_called()
        self.assertEqual(posts[0]["analysis_status"], "pending")

    def test_refresh_recent_summary_cache_limits_deferred_retries(self):
        now = datetime(2026, 7, 13, 0, 0, 0)
        posts = [
            post("2026-05-01", f"https://blog.naver.com/ranto28/2234567890{index:02d}")
            for index in range(3)
        ]
        for item in posts:
            item.update({
                "summary": fetch_mer.SUMMARY_DEFERRED_TEXT,
                "summary_version": None,
                "summary_status": "deferred",
                "analysis_status": "pending",
            })

        deferred = {
            "summary": fetch_mer.SUMMARY_DEFERRED_TEXT,
            "investment_relevant": False,
            "relevance_reason": fetch_mer.SUMMARY_DEFERRED_TEXT,
            "summary_version": None,
            "summary_status": "deferred",
            "summary_error": "temporary quota",
            "signal_candidates": [],
        }
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(fetch_mer, "DB_FILE", str(Path(tmpdir) / "posts_db.json")), \
             patch.object(fetch_mer, "SUMMARY_DEFERRED_RETRY_MAX_PER_RUN", 1), \
             patch.object(fetch_mer, "fetch_full_post", return_value=None), \
             patch.object(fetch_mer, "_summary_fields", return_value=deferred) as summary_fields:
            changed = fetch_mer._refresh_recent_summary_cache(posts, now)

        self.assertTrue(changed)
        self.assertEqual(summary_fields.call_count, 1)
        self.assertEqual(posts[0]["summary_retry_count"], 1)
        self.assertGreater(
            datetime.fromisoformat(posts[0]["summary_next_retry_at"]),
            now,
        )
        self.assertNotIn("summary_retry_count", posts[1])


if __name__ == "__main__":
    unittest.main()
