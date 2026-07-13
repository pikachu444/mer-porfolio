import json
import unittest
from unittest.mock import patch

import analyze
from portfolio_schema import parse_analysis_decision


DECISION_RESPONSE = {
    "analysis_date": "2026-06-01",
    "run_type": "regular",
    "insights": [
        {
            "id": "aluminum-supply",
            "title": "알루미늄 공급 제한",
            "summary": "기니의 보크사이트 수출 제한으로 공급 불안이 커짐",
            "investment_implication": "원문에 등장한 관련 종목의 수혜 가능성을 검토",
            "evidence_posts": [
                {
                    "title": "기니, 중국을 건드리나?",
                    "url": "https://blog.naver.com/ranto28/123",
                    "published_date": "2026-05-27",
                }
            ],
            "related_decision_codes": ["AA"],
        }
    ],
    "portfolio_decisions": [
        {
            "name": "Alcoa",
            "code": "AA",
            "market": "US",
            "asset_type": "stock",
            "decision_actor": "AI",
            "action": "매수",
            "basis": "종목 분석",
            "decision_date": "2026-06-01",
            "evidence_posts": [
                {
                    "title": "기니, 중국을 건드리나?",
                    "url": "https://blog.naver.com/ranto28/123",
                    "published_date": "2026-05-27",
                }
            ],
            "source_mentioned": True,
            "previous_weight": None,
            "proposed_weight": 8.0,
            "weight_source": "AI 제안",
            "change_reason": "알루미늄 공급 제한에 따른 수혜 추론",
            "allocation_role": "satellite",
            "source_scope": "source_named_security",
            "investment_rationale": "원문에 등장한 Alcoa가 알루미늄 공급 제한의 수혜를 받을 수 있음",
            "current_entry_reason": "공급 제한 발표로 투자 논리가 구체화됨",
            "key_risks": ["알루미늄 가격 변동성"],
            "linked_insight_ids": ["aluminum-supply"],
        }
    ],
    "watchlist": [],
}

POSTS = [
    {
        "title": "기니, 중국을 건드리나?",
        "date": "2026-05-27",
        "url": "https://blog.naver.com/ranto28/123",
        "content": "알루미늄 공급망 관련 본문",
        "summary": "알루미늄 공급망 관련 요약",
    }
]

VALID_REPORT = (
    "# 메르AI 보고서\n\n"
    "## 핵심 인사이트\n\n내용\n\n"
    "## 현재 모델 포트폴리오\n\n내용\n\n"
    "## Watchlist\n\n내용\n\n"
    "## 변경 및 종료 포지션\n\n내용"
)


class StructuredAnalysisTest(unittest.TestCase):
    def test_default_analysis_model_is_explicit_stable_flash(self):
        self.assertEqual(analyze.DECISION_MODEL, "gemini-3.5-flash")
        self.assertEqual(analyze._decision_model(), "gemini-3.5-flash")
        self.assertEqual(analyze._model_sequence(), ["gemini-3.5-flash"])

    def test_decision_uses_medium_thinking_without_legacy_sampling(self):
        response = unittest.mock.Mock(text='{"ok": true}')
        with patch.object(analyze, "call_gemini_with_retry", return_value=response) as call:
            text = analyze._call_model_text(
                object(),
                "gemini-3.5-flash",
                "request",
                "system",
                response_mime_type="application/json",
                response_json_schema=analyze.DECISION_RESPONSE_SCHEMA,
            )

        self.assertEqual(text, '{"ok": true}')
        config = call.call_args.kwargs["config"]
        self.assertEqual(config.max_output_tokens, 24_576)
        self.assertIsNone(config.temperature)
        self.assertIsNone(config.top_p)
        self.assertEqual(config.thinking_config.thinking_level.value, "MEDIUM")
        self.assertIs(config.response_json_schema, analyze.DECISION_RESPONSE_SCHEMA)

    def test_investment_decision_defers_instead_of_falling_back_when_model_busy(self):
        model_busy = RuntimeError("GEMINI_TRANSIENT 503 UNAVAILABLE high demand")

        with patch.object(analyze, "_get_client", return_value=object()), \
             patch.object(analyze, "_call_model_text", side_effect=model_busy) as call:
            with self.assertRaisesRegex(RuntimeError, "Gemini 투자 판단 보류"):
                analyze.analyze_posts_structured(
                    POSTS,
                    "2026-06-01",
                    {"last_rebalanced_date": "2026-05-14"},
                )

        call.assert_called_once()
        self.assertEqual(call.call_args.args[1], "gemini-3.5-flash")

    def test_pro_context_trims_only_transmitted_tail_over_safe_limit(self):
        client = unittest.mock.Mock()
        client.models.count_tokens.side_effect = lambda model, contents: unittest.mock.Mock(
            total_tokens=len(contents)
        )
        original = "x" * 120

        with patch.object(analyze, "MODEL_INPUT_TOKEN_LIMIT", 100):
            fitted = analyze._fit_context_to_budget(
                client,
                original,
                lambda context: "prefix:" + context,
            )

        self.assertIn("전송용 분석 문맥 끝부분 생략", fitted)
        self.assertEqual(original, "x" * 120)

    def test_report_validation_does_not_require_exact_insight_heading(self):
        report = VALID_REPORT

        self.assertEqual(analyze._validate_markdown_report(report), report)

    def test_report_validation_rejects_truncated_oversized_table(self):
        report = (
            "# 메르AI 보고서\n\n"
            "## 핵심 인사이트\n\n내용\n\n"
            "## 현재 모델 포트폴리오\n\n"
            "| 종목 | 판단 근거" + (" " * 25_000)
        )

        with self.assertRaisesRegex(ValueError, r"필수 보고서 섹션 누락"):
            analyze._validate_markdown_report(report)

    def test_analysis_input_rejects_post_without_summary(self):
        no_summary = dict(POSTS[0])
        no_summary["summary"] = ""

        with self.assertRaisesRegex(ValueError, "요약 없는 글"):
            analyze._analysis_text_for_post(no_summary)

    def test_structured_context_includes_host_verified_signal_ids(self):
        posts = [dict(POSTS[0])]
        posts[0]["signal_candidates"] = [
            {
                "signal_id": "a" * 64,
                "evidence_sha256": "b" * 64,
                "exact_text": "알루미늄 공급망 관련 본문",
                "classification": "DIRECTIONAL_THESIS",
                "entity_name": "알루미늄",
                "entity_type": "commodity",
                "direction": "positive",
                "horizon_kind": "cyclical",
                "catalysts": ["공급 제한"],
                "invalidation_conditions": [],
                "thesis_summary": "공급 제한 수혜",
            }
        ]

        context = analyze._structured_context(posts)

        self.assertIn("호스트 검증 원문 신호 후보", context)
        self.assertIn("a" * 64, context)
        self.assertIn("b" * 64, context)
        self.assertIn("DIRECTIONAL_THESIS", context)

    def test_excludes_unlisted_stock_suggestion_from_model_output(self):
        invalid = json.loads(json.dumps(DECISION_RESPONSE, ensure_ascii=False))
        invalid["portfolio_decisions"][0]["code"] = None

        parsed = analyze._parse_model_decision_json(
            json.dumps(invalid, ensure_ascii=False)
        )

        self.assertEqual(parsed.portfolio_decisions, [])

    def test_generates_decision_and_deterministic_report_with_one_llm_call(self):
        responses = [json.dumps(DECISION_RESPONSE, ensure_ascii=False)]

        with patch.object(analyze, "_get_client", return_value=object()), \
             patch.object(analyze, "_call_model_text", side_effect=responses) as call:
            result = analyze.analyze_posts_structured(
                POSTS,
                "2026-06-01",
                {"last_rebalanced_date": "2026-05-14"},
            )

        self.assertEqual(result.decision.portfolio_decisions[0]["name"], "Alcoa")
        self.assertIn("포트폴리오", result.report)
        call.assert_called_once()
        self.assertEqual(
            call.call_args.kwargs["response_json_schema"],
            analyze.DECISION_RESPONSE_SCHEMA,
        )

    def test_report_receives_full_state_after_empty_delta(self):
        current = {
            "schema_version": "2.0",
            "portfolio": [DECISION_RESPONSE["portfolio_decisions"][0]],
            "watchlist": [],
            "closed_positions": [],
            "decision_history": [],
            "insights": [],
            "last_rebalanced_date": "2026-05-14",
        }
        no_change = {
            **DECISION_RESPONSE,
            "portfolio_decisions": [],
        }
        responses = [json.dumps(no_change, ensure_ascii=False)]

        with patch.object(analyze, "_get_client", return_value=object()), \
             patch.object(analyze, "_call_model_text", side_effect=responses) as call:
            analyze.analyze_posts_structured(
                POSTS,
                "2026-06-01",
                current,
            )

        call.assert_called_once()

    def test_builds_deterministic_report_without_second_llm_call(self):
        responses = [json.dumps(DECISION_RESPONSE, ensure_ascii=False)]

        with patch.object(analyze, "_get_client", return_value=object()), \
             patch.object(analyze, "_call_model_text", side_effect=responses) as call:
            result = analyze.analyze_posts_structured(
                POSTS,
                "2026-06-01",
                {
                    "schema_version": "2.0",
                    "portfolio": [],
                    "watchlist": [],
                    "closed_positions": [],
                    "decision_history": [],
                    "insights": [],
                    "last_rebalanced_date": "2026-05-14",
                },
            )

        self.assertIn("## 핵심 인사이트", result.report)
        self.assertIn("## 현재 모델 포트폴리오", result.report)
        self.assertIn("## Watchlist", result.report)
        self.assertIn("## 변경 및 종료 포지션", result.report)
        self.assertIn("Alcoa", result.report)
        call.assert_called_once()

    def test_repairs_invalid_structured_decision_once(self):
        invalid = json.loads(json.dumps(DECISION_RESPONSE, ensure_ascii=False))
        invalid["portfolio_decisions"][0]["evidence_posts"] = []
        responses = [
            json.dumps(invalid, ensure_ascii=False),
            json.dumps(DECISION_RESPONSE, ensure_ascii=False),
        ]

        with patch.object(analyze, "_get_client", return_value=object()), \
             patch.object(analyze, "time") as clock, \
             patch.object(analyze, "_call_model_text", side_effect=responses) as call:
            clock.monotonic.side_effect = [100.0, 100.0, 110.0]
            result = analyze.analyze_posts_structured(
                POSTS,
                "2026-06-01",
                {"last_rebalanced_date": "2026-05-14"},
            )

        self.assertEqual(result.decision.portfolio_decisions[0]["name"], "Alcoa")
        self.assertEqual(call.call_count, 2)
        first_budget = call.call_args_list[0].kwargs["retry_budget_seconds"]
        repair_budget = call.call_args_list[1].kwargs["retry_budget_seconds"]
        self.assertEqual(first_budget, analyze.RETRY_BUDGET_SECONDS)
        self.assertEqual(repair_budget, analyze.RETRY_BUDGET_SECONDS - 10.0)

    def test_repairs_decision_when_applied_portfolio_exceeds_one_hundred(self):
        retained = json.loads(json.dumps(DECISION_RESPONSE["portfolio_decisions"][0]))
        retained["proposed_weight"] = 75.0
        current_state = {
            "schema_version": "2.0",
            "portfolio": [retained],
            "watchlist": [],
            "closed_positions": [],
            "decision_history": [],
            "last_rebalanced_date": "2026-05-14",
        }
        additional = json.loads(json.dumps(DECISION_RESPONSE, ensure_ascii=False))
        additional["portfolio_decisions"][0]["name"] = "Microsoft"
        additional["portfolio_decisions"][0]["code"] = "MSFT"
        additional["portfolio_decisions"][0]["proposed_weight"] = 30.0
        repaired = json.loads(json.dumps(additional, ensure_ascii=False))
        repaired["portfolio_decisions"][0]["proposed_weight"] = 5.0
        responses = [
            json.dumps(additional, ensure_ascii=False),
            json.dumps(repaired, ensure_ascii=False),
        ]

        with patch.object(analyze, "_get_client", return_value=object()), \
             patch.object(analyze, "_call_model_text", side_effect=responses) as call:
            result = analyze.analyze_posts_structured(
                POSTS,
                "2026-06-01",
                current_state,
            )

        self.assertEqual(result.decision.portfolio_decisions[0]["proposed_weight"], 5.0)
        self.assertEqual(call.call_count, 2)

    def test_repairs_decision_when_listing_code_has_no_price(self):
        responses = [
            json.dumps(DECISION_RESPONSE, ensure_ascii=False),
            json.dumps(DECISION_RESPONSE, ensure_ascii=False),
        ]
        validator = unittest.mock.Mock(
            side_effect=[ValueError("현재 가격을 가져오지 못했습니다: Alcoa"), None]
        )

        with patch.object(analyze, "_get_client", return_value=object()), \
             patch.object(analyze, "_call_model_text", side_effect=responses) as call:
            result = analyze.analyze_posts_structured(
                POSTS,
                "2026-06-01",
                {"last_rebalanced_date": "2026-05-14"},
                decision_validator=validator,
            )

        self.assertEqual(result.decision.portfolio_decisions[0]["name"], "Alcoa")
        self.assertEqual(validator.call_count, 2)
        self.assertEqual(call.call_count, 2)

    def test_report_uses_postprocessed_decision(self):
        responses = [json.dumps(DECISION_RESPONSE, ensure_ascii=False)]
        sanitized = parse_analysis_decision({
            **DECISION_RESPONSE,
            "portfolio_decisions": [],
        })

        with patch.object(analyze, "_get_client", return_value=object()), \
             patch.object(analyze, "_call_model_text", side_effect=responses) as call:
            result = analyze.analyze_posts_structured(
                POSTS,
                "2026-06-01",
                {"last_rebalanced_date": "2026-05-14"},
                decision_validator=lambda decision: sanitized,
            )

        self.assertEqual(result.decision.portfolio_decisions, [])
        self.assertNotIn("Alcoa", result.report)
        call.assert_called_once()

    def test_always_uses_deterministic_report(self):
        responses = [json.dumps(DECISION_RESPONSE, ensure_ascii=False)]

        with patch.object(analyze, "_get_client", return_value=object()), \
             patch.object(analyze, "_call_model_text", side_effect=responses):
            result = analyze.analyze_posts_structured(
                POSTS,
                "2026-06-01",
                {
                    "schema_version": "2.0",
                    "portfolio": [],
                    "watchlist": [],
                    "closed_positions": [],
                    "decision_history": [],
                    "insights": [],
                    "last_rebalanced_date": "2026-05-14",
                },
            )

        self.assertIn("## 현재 모델 포트폴리오", result.report)
        self.assertIn("Alcoa", result.report)


if __name__ == "__main__":
    unittest.main()
