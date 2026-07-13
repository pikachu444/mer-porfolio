import unittest
import sys
import tempfile
import json
from datetime import date
from datetime import datetime
from datetime import timezone
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace

try:
    import feedparser  # noqa: F401
except ModuleNotFoundError:
    sys.modules["feedparser"] = Mock()

import main
from main import _gemini_deferral_note, _is_llm_service_unavailable_error
from portfolio_schema import parse_portfolio_state
from portfolio_provenance import prepare_post_signal_events
from runtime_modes import get_run_policy, should_rebalance
from test_portfolio_schema import decision, insight, state_payload
from portfolio_schema import parse_analysis_decision
from track_returns import create_model_ledger


class RuntimeModesTest(unittest.TestCase):
    def setUp(self):
        # GitHub Actions exposes GITHUB_ACTIONS=true to the test process too.
        # Runtime receipts belong to a real main.py invocation, never to mocked
        # unit-test report paths.  Individual receipt tests opt in explicitly.
        self._run_status_enabled = main.RUN_STATUS_ENABLED
        main.RUN_STATUS_ENABLED = False

    def tearDown(self):
        main.RUN_STATUS_ENABLED = self._run_status_enabled

    def test_verify_sends_telegram_without_persisting_operating_state(self):
        policy = get_run_policy("verify")

        self.assertFalse(policy.persist_operating_state)
        self.assertTrue(policy.send_telegram)
        self.assertTrue(policy.upload_artifact)

    def test_full_verify_sends_telegram_without_persisting_operating_state(self):
        policy = get_run_policy("full_verify")

        self.assertFalse(policy.persist_operating_state)
        self.assertTrue(policy.send_telegram)
        self.assertTrue(policy.upload_artifact)
        self.assertEqual(policy.fetch_days, 2)

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

    def test_manual_rebalance_uses_fetch_window_even_after_today_rebalance(self):
        state = Mock(last_rebalanced_date="2026-06-01")

        with patch.object(main, "RUN_POLICY", Mock(mode="rebalance")), \
             patch.object(main, "FETCH_DAYS", 14):
            cutoff = main._rebalance_cutoff_date(state, datetime(2026, 6, 15))

        self.assertEqual(cutoff.date(), date(2026, 6, 1))

    def test_scheduled_rebalance_uses_last_rebalance_date_as_cutoff(self):
        state = Mock(last_rebalanced_date="2026-05-18")

        with patch.object(main, "RUN_POLICY", Mock(mode="scheduled")), \
             patch.object(main, "FETCH_DAYS", 14):
            cutoff = main._rebalance_cutoff_date(state, datetime(2026, 6, 15))

        self.assertEqual(cutoff.date(), date(2026, 5, 18))

    def test_verify_does_not_force_rebalance(self):
        today = date(2026, 6, 1)

        self.assertFalse(should_rebalance("verify", "2026-05-31", today))
        self.assertFalse(should_rebalance("full_verify", "2026-05-31", today))

    def test_rejects_old_adhoc_mode(self):
        with self.assertRaisesRegex(ValueError, "unknown RUN_MODE"):
            get_run_policy("adhoc")

    def test_identifies_first_stage_llm_service_unavailability(self):
        exc = RuntimeError("Gemini 투자 판단 보류. model=gemini-3.5-flash. GEMINI_RATE_LIMIT 429")
        busy = RuntimeError("Gemini 투자 판단 보류. model=gemini-3.5-flash. GEMINI_TRANSIENT 503")

        self.assertTrue(_is_llm_service_unavailable_error(exc))
        self.assertTrue(_is_llm_service_unavailable_error(busy))
        self.assertFalse(_is_llm_service_unavailable_error(RuntimeError("검증 오류")))

    def test_gemini_deferral_note_distinguishes_permanent_and_transient_errors(self):
        permanent = RuntimeError("Gemini 투자 판단 보류. GEMINI_PERMANENT 404 NOT_FOUND")
        transient = RuntimeError("Gemini 투자 판단 보류. GEMINI_TRANSIENT 503 UNAVAILABLE")

        self.assertIn("모델 종료", _gemini_deferral_note(permanent))
        self.assertIn("일시 장애", _gemini_deferral_note(transient))

    def test_no_change_update_creates_today_report_and_passes_status_note_to_dashboard(self):
        note = "새 글 1건 요약 실패로 투자 분석 보류: 코스트코와 이마트"
        state = main._empty_state()

        with patch.object(main, "load_model_ledger", return_value={}), \
             patch.object(main, "sanitize_performance_files_for_state", return_value={}), \
             patch.object(main, "commit_json_bundle") as commit_bundle, \
             patch.object(main, "_save_report") as save_report, \
             patch.object(main, "generate_all", return_value=(None, None)) as generate_all, \
             patch.object(main, "RUN_POLICY", Mock(send_telegram=False)):
            result = main._run_no_change_update(
                state,
                datetime(2026, 6, 7),
                status_note=note,
            )

        self.assertEqual(result, 0)
        commit_bundle.assert_called_once()
        saved_report = save_report.call_args.args[0]
        self.assertIn("기준일: 2026-06-07", saved_report)
        self.assertIn(note, saved_report)
        generated_state = generate_all.call_args.kwargs["state"]
        self.assertEqual(generated_state["status_note"], note)

    def test_verify_main_does_not_collect_or_analyze_posts(self):
        state = main._empty_state()

        with patch.object(main, "RUN_MODE", "verify"), \
             patch.object(main, "_load_state", return_value=state), \
             patch.object(main, "_collect_posts") as collect_posts, \
             patch.object(main, "analyze_posts_structured") as analyze, \
             patch.object(main, "_run_no_change_update", return_value=0) as no_change:
            result = main.main()

        self.assertEqual(result, 0)
        collect_posts.assert_not_called()
        analyze.assert_not_called()
        self.assertIn("Gemini 분석 없음", no_change.call_args.kwargs["status_note"])

    def test_telegram_report_fails_when_chart_delivery_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            chart_path = Path(temp_dir) / "chart.png"
            chart_path.write_bytes(b"png")
            with patch.object(main, "RUN_POLICY", Mock(
                mode="scheduled",
                persist_operating_state=True,
            )), \
                 patch.object(main, "send_photo", return_value=False) as photo, \
                 patch.object(main, "send_structured_summary", return_value=True) as summary:
                delivered = main._send_user_report(
                    chart_path,
                    {},
                    datetime(2026, 7, 13),
                    {},
                    run_label="scheduled_rebalance",
                )

        self.assertFalse(delivered)
        photo.assert_called_once()
        summary.assert_called_once()
        self.assertEqual(
            summary.call_args.kwargs["run_label"],
            "scheduled_rebalance",
        )

    def test_telegram_report_requires_structured_summary_too(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            chart_path = Path(temp_dir) / "chart.png"
            chart_path.write_bytes(b"png")
            with patch.object(main, "RUN_POLICY", Mock(
                mode="scheduled",
                persist_operating_state=True,
            )), \
                 patch.object(main, "send_photo", return_value=True), \
                 patch.object(main, "send_structured_summary", return_value=False):
                delivered = main._send_user_report(
                    chart_path,
                    {},
                    datetime(2026, 7, 13),
                    {},
                )

        self.assertFalse(delivered)

    def test_actions_run_status_records_safe_delivery_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            status_path = Path(temp_dir) / "run_status.json"
            report_path = Path(temp_dir) / "report.md"
            report_path.write_text("report", encoding="utf-8")
            chart_path = Path(temp_dir) / "chart.png"
            chart_path.write_bytes(b"png")
            with patch.object(main, "RUN_STATUS_ENABLED", True), \
                 patch.object(main, "RUN_STATUS_PATH", status_path), \
                 patch.dict(main.os.environ, {"GITHUB_RUN_ID": "12345"}, clear=False):
                main._write_run_status(
                    today=datetime(2026, 7, 13, 0, 17),
                    report_path=report_path,
                    chart_path=chart_path,
                    no_changes=True,
                    run_label="scheduled",
                    delivery_required=True,
                    delivery_deferred=False,
                    delivery_accepted=False,
                    status_note="delivery rejected",
                )

            receipt = json.loads(status_path.read_text(encoding="utf-8"))

        self.assertEqual(receipt["github_run_id"], "12345")
        self.assertTrue(receipt["state_bundle_committed"])
        self.assertFalse(receipt["telegram_delivery_deferred"])
        self.assertFalse(receipt["telegram_delivery_accepted"])

    def test_live_action_defers_telegram_until_workflow_persists_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / "report.md"
            report_path.write_text("report", encoding="utf-8")
            chart_path = Path(temp_dir) / "chart.png"
            chart_path.write_bytes(b"png")
            with patch.object(main, "RUN_POLICY", Mock(
                mode="scheduled",
                send_telegram=True,
                persist_operating_state=True,
            )), \
                 patch.object(main, "_send_user_report") as send_report, \
                 patch.object(main, "_write_run_status") as write_receipt, \
                 patch.dict(
                     main.os.environ,
                     {"DEFER_TELEGRAM_DELIVERY_TO_WORKFLOW": "true"},
                     clear=False,
                 ):
                result = main._finalize_user_report(
                    report_path=report_path,
                    png_path=chart_path,
                    state={},
                    today=datetime(2026, 7, 13, 0, 17),
                    performance={},
                    no_changes=True,
                    status_note="cache upgrade in progress",
                )

        self.assertEqual(result, 0)
        send_report.assert_not_called()
        self.assertTrue(write_receipt.call_args.kwargs["delivery_deferred"])
        self.assertIsNone(write_receipt.call_args.kwargs["delivery_accepted"])
        self.assertEqual(
            write_receipt.call_args.kwargs["status_note"],
            "cache upgrade in progress",
        )

    def test_normal_no_post_run_advances_watchlist_but_failure_path_does_not(self):
        payload = state_payload()
        payload["watchlist"][0].update({
            "watchlist_kind": "mention",
            "latest_material_signal_date": "2026-05-20",
            "expires_on": "2026-06-03",
            "lifecycle_status": "active",
        })
        state = parse_portfolio_state(payload)

        with patch.object(main, "load_model_ledger", return_value={}), \
             patch.object(main, "sanitize_performance_files_for_state", return_value={}), \
             patch.object(main, "commit_json_bundle") as commit_bundle, \
             patch.object(main, "_save_report"), \
             patch.object(main, "generate_all", return_value=(None, None)), \
             patch.object(main, "RUN_POLICY", Mock(send_telegram=False)):
            main._run_no_change_update(
                state,
                datetime(2026, 6, 7),
                advance_lifecycle=True,
            )

        committed = commit_bundle.call_args.args[0][main.STATE_PATH]
        self.assertEqual(committed["watchlist"], [])
        self.assertEqual(committed["watchlist_archive"][0]["lifecycle_status"], "expired")

    def test_regular_block_scope_includes_prior_pending_post(self):
        pending = {
            "url": "https://blog.naver.com/ranto28/pending",
            "date": "2026-06-01",
            "analysis_status": "pending",
            "summary_status": "deferred",
        }

        with patch.object(main, "get_last_fetch_new_post_urls", return_value=set()):
            scoped = main._posts_in_current_analysis_scope(
                [pending],
                is_rebalance=False,
                state=main._empty_state(),
                today=datetime(2026, 6, 2),
            )

        self.assertEqual(scoped, [pending])

    def test_any_blocked_summary_prevents_partial_source_decision(self):
        state = main._empty_state()
        ready = {"url": "https://blog.naver.com/ranto28/ready"}
        blocked = [{
            "title": "요약 보류",
            "url": "https://blog.naver.com/ranto28/blocked",
            "date": "2026-06-01",
            "reason": "잘린 JSON",
        }]

        with patch.object(main, "RUN_MODE", "scheduled"), \
             patch.object(main, "RUN_POLICY", Mock(mode="scheduled", send_telegram=False)), \
             patch.object(main, "_now_kst", return_value=datetime(2026, 6, 2)), \
             patch.object(main, "_load_state", return_value=state), \
             patch.object(main, "should_rebalance", return_value=False), \
             patch.object(main, "_collect_posts", return_value=[ready]), \
             patch.object(main, "select_new_relevant_posts", return_value=[ready]), \
             patch.object(main, "_blocked_summary_posts_for_run", return_value=blocked), \
             patch.object(main, "analyze_posts_structured") as analyze, \
             patch.object(main, "_run_no_change_update", return_value=0) as no_change:
            result = main.main()

        self.assertEqual(result, 0)
        analyze.assert_not_called()
        self.assertEqual(no_change.call_args.kwargs["deferred_posts"], blocked)
        self.assertTrue(no_change.call_args.kwargs["advance_lifecycle"])
        self.assertFalse(no_change.call_args.kwargs["allow_maintenance"])

    def test_gemini_deferral_advances_watchlist_without_maintenance(self):
        state = main._empty_state()
        ready = {
            "url": "https://blog.naver.com/ranto28/ready",
            "summary_version": main._current_summary_version(),
        }
        unavailable = RuntimeError(
            "Gemini 투자 판단 보류. GEMINI_TRANSIENT 503 UNAVAILABLE"
        )

        with patch.object(main, "RUN_MODE", "scheduled"), \
             patch.object(main, "RUN_POLICY", Mock(mode="scheduled", send_telegram=False)), \
             patch.object(main, "_now_kst", return_value=datetime(2026, 6, 2)), \
             patch.object(main, "_load_state", return_value=state), \
             patch.object(main, "should_rebalance", return_value=False), \
             patch.object(main, "_collect_posts", return_value=[ready]), \
             patch.object(main, "select_new_relevant_posts", return_value=[ready]), \
             patch.object(main, "_blocked_summary_posts_for_run", return_value=[]), \
             patch.object(main, "prepare_post_signal_events", return_value=([], [])), \
             patch.object(main, "analyze_posts_structured", side_effect=unavailable), \
             patch.object(main, "_save_error_log"), \
             patch.object(main, "_run_no_change_update", return_value=0) as no_change:
            result = main.main()

        self.assertEqual(result, 0)
        self.assertTrue(no_change.call_args.kwargs["advance_lifecycle"])
        self.assertFalse(no_change.call_args.kwargs["allow_maintenance"])

    def test_policy_block_advances_watchlist_without_maintenance(self):
        state = main._empty_state()
        ready = {
            "url": "https://blog.naver.com/ranto28/ready",
            "summary_version": main._current_summary_version(),
        }

        with patch.object(main, "RUN_MODE", "scheduled"), \
             patch.object(main, "RUN_POLICY", Mock(mode="scheduled", send_telegram=False)), \
             patch.object(main, "_now_kst", return_value=datetime(2026, 6, 2)), \
             patch.object(main, "_load_state", return_value=state), \
             patch.object(main, "should_rebalance", return_value=False), \
             patch.object(main, "_collect_posts", return_value=[ready]), \
             patch.object(main, "select_new_relevant_posts", return_value=[ready]), \
             patch.object(main, "_blocked_summary_posts_for_run", return_value=[]), \
             patch.object(main, "prepare_post_signal_events", return_value=([], [])), \
             patch.object(
                 main,
                 "analyze_posts_structured",
                 return_value=SimpleNamespace(decision=object()),
             ), \
             patch.object(
                 main,
                 "_exclude_unpriceable_new_suggestions",
                 side_effect=main.PortfolioPolicyBlocked("blocked"),
             ), \
             patch.object(main, "_save_error_log"), \
             patch.object(main, "_run_no_change_update", return_value=0) as no_change:
            result = main.main()

        self.assertEqual(result, 0)
        self.assertTrue(no_change.call_args.kwargs["advance_lifecycle"])
        self.assertFalse(no_change.call_args.kwargs["allow_maintenance"])

    def test_rebalance_waits_for_all_in_scope_summary_schema_upgrades(self):
        state = main._empty_state()
        upgraded = {
            "title": "upgraded",
            "url": "https://blog.naver.com/ranto28/upgraded",
            "date": "2026-06-02",
            "summary": "current summary",
            "investment_relevant": True,
            "summary_status": "ok",
            "summary_version": main._current_summary_version(),
        }
        legacy = {
            "title": "legacy v2",
            "url": "https://blog.naver.com/ranto28/legacy",
            "date": "2026-06-01",
            "summary": "old summary",
            "investment_relevant": True,
            "summary_status": "ok",
            "summary_version": 2,
            "analysis_status": "legacy_untracked",
        }

        with patch.object(main, "RUN_MODE", "rebalance"), \
             patch.object(main, "RUN_POLICY", Mock(mode="rebalance", send_telegram=False)), \
             patch.object(main, "_now_kst", return_value=datetime(2026, 6, 2)), \
             patch.object(main, "_load_state", return_value=state), \
             patch.object(main, "should_rebalance", return_value=True), \
             patch.object(main, "_collect_posts", return_value=[upgraded, legacy]), \
             patch.object(main, "select_rebalance_posts", return_value=[upgraded, legacy]), \
             patch.object(main, "_blocked_summary_posts_for_run", return_value=[]), \
             patch.object(main, "analyze_posts_structured") as analyze, \
             patch.object(main, "_run_no_change_update", return_value=0) as no_change:
            result = main.main()

        self.assertEqual(result, 0)
        analyze.assert_not_called()
        blocked = no_change.call_args.kwargs["deferred_posts"]
        self.assertEqual([item["url"] for item in blocked], [legacy["url"]])
        self.assertIn("업그레이드 대기", blocked[0]["reason"])
        self.assertTrue(no_change.call_args.kwargs["advance_lifecycle"])
        self.assertFalse(no_change.call_args.kwargs["allow_maintenance"])

    def test_execution_date_uses_korean_time(self):
        utc_time = datetime(2026, 6, 7, 16, 20, tzinfo=timezone.utc)

        kst_time = main._to_kst_naive(utc_time)

        self.assertEqual(kst_time.strftime("%Y-%m-%d %H:%M"), "2026-06-08 01:20")

    def test_allocator_rejection_is_zeroed_in_committed_decision_artifact(self):
        analysis = parse_analysis_decision({
            "analysis_date": "2026-06-01",
            "run_type": "regular",
            "insights": [insight()],
            "portfolio_decisions": [decision(proposed_weight=8.0)],
            "watchlist": [],
        })

        updated = main._decision_with_allocated_weights(
            analysis,
            main._empty_state(),
        )

        self.assertEqual(updated.portfolio_decisions[0]["proposed_weight"], 0.0)
        self.assertEqual(
            updated.portfolio_decisions[0]["allocation_method"],
            "allocator_rejected_or_risk_blocked",
        )

        sell_analysis = parse_analysis_decision({
            "analysis_date": "2026-06-01",
            "run_type": "regular",
            "insights": [insight()],
            "portfolio_decisions": [decision(
                action="매도",
                previous_weight=8.0,
                proposed_weight=0.0,
            )],
            "watchlist": [],
        })
        sold = main._decision_with_allocated_weights(
            sell_analysis,
            main._empty_state(),
        )

        self.assertEqual(sold.portfolio_decisions[0]["policy_action"], "매도")
        self.assertEqual(
            sold.portfolio_decisions[0]["allocation_method"],
            "model_or_policy_exit",
        )

    def test_successful_run_links_signal_allocates_and_commits_one_bundle(self):
        state = main._empty_state()
        raw_posts = [{
            "post_id": "123",
            "title": "알루미늄 공급",
            "url": "https://blog.naver.com/ranto28/123",
            "date": "2026-06-01",
            "summary_version": main._current_summary_version(),
            "summary": "Alcoa 공급 제한 수혜",
            "signal_candidates": [{
                "exact_text": "Alcoa가 공급 제한의 수혜를 받을 수 있다.",
                "classification": "DIRECTIONAL_THESIS",
                "entity_name": "Alcoa",
                "entity_type": "company",
                "direction": "수혜",
                "horizon_kind": "cyclical",
                "catalysts": ["공급 제한"],
                "invalidation_conditions": ["공급 정상화"],
                "thesis_summary": "공급 제한 수혜",
            }],
        }]
        prepared, events = prepare_post_signal_events(
            raw_posts,
            created_at="2026-06-01",
            model_id="gemini-3.1-flash-lite",
        )
        item = decision(proposed_weight=8.0)
        item.update({
            "linked_signal_ids": [events[0]["signal_id"]],
            "thesis_id": events[0]["thesis_id"],
            "quality_components": {key: 1.0 for key in (
                "explicitness", "causality", "catalyst", "confirmation", "invalidation", "recency"
            )},
            "issuer_id": "ALCOA",
            "theme_ids": ["ALUMINUM"],
            "country_code": "US",
        })
        analysis = parse_analysis_decision({
            "analysis_date": "2026-06-01",
            "run_type": "rebalance",
            "insights": [insight()],
            "portfolio_decisions": [item],
            "watchlist": [],
        })

        with patch.object(main, "RUN_MODE", "scheduled"), \
             patch.object(main, "RUN_POLICY", Mock(mode="scheduled", send_telegram=False)), \
             patch.object(main, "_now_kst", return_value=datetime(2026, 6, 1)), \
             patch.object(main, "_load_state", return_value=state), \
             patch.object(main, "_collect_posts", return_value=raw_posts), \
             patch.object(main, "select_rebalance_posts", return_value=raw_posts), \
             patch.object(main, "_blocked_summary_posts_for_run", return_value=[]), \
             patch.object(main, "prepare_post_signal_events", return_value=(prepared, events)), \
             patch.object(main, "analyze_posts_structured", return_value=SimpleNamespace(decision=analysis)), \
             patch.object(main, "load_model_ledger", return_value=create_model_ledger()), \
             patch.object(main, "get_structured_volatilities", return_value={"stock:US:AA": 0.3}), \
             patch.object(main, "get_structured_prices", return_value={
                 "stock:US:AA": 10.0,
                 "etf:KR:069500": 10.0,
                 "etf:KR:360750": 10.0,
             }), \
             patch.object(main, "mark_posts_analysis_completed"), \
             patch.object(main, "commit_json_bundle") as commit_bundle, \
             patch.object(main, "_save_report"), \
             patch.object(main, "generate_all", return_value=(None, None)):
            result = main.main()

        self.assertEqual(result, 0)
        state_payload_committed = commit_bundle.call_args.args[0][main.STATE_PATH]
        self.assertEqual(state_payload_committed["schema_version"], "2.1")
        alcoa = next(item for item in state_payload_committed["portfolio"] if item["code"] == "AA")
        self.assertEqual(alcoa["origin_signal_type"], "MER_THESIS")
        self.assertEqual(alcoa["proposed_weight"], 5.0)
        self.assertEqual(
            sum(item["proposed_weight"] for item in state_payload_committed["portfolio"] if item["origin_signal_type"] == "PASSIVE_INDEX"),
            20.0,
        )
        self.assertEqual(len(state_payload_committed["signal_events"]), 1)


if __name__ == "__main__":
    unittest.main()
