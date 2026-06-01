import json
import unittest
from unittest.mock import patch

import analyze


DECISION_RESPONSE = {
    "analysis_date": "2026-06-01",
    "run_type": "regular",
    "portfolio_decisions": [
        {
            "name": "Alcoa",
            "code": "AA",
            "market": "US",
            "asset_type": "stock",
            "decision_actor": "AI",
            "action": "매수",
            "basis": "섹터 분석",
            "decision_date": "2026-06-01",
            "evidence_posts": [
                {
                    "title": "기니, 중국을 건드리나?",
                    "url": "https://blog.naver.com/ranto28/123",
                    "published_date": "2026-05-27",
                }
            ],
            "source_mentioned": False,
            "previous_weight": None,
            "proposed_weight": 8.0,
            "weight_source": "AI 제안",
            "change_reason": "알루미늄 공급 제한에 따른 수혜 추론",
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
        "summary": "",
    }
]


class StructuredAnalysisTest(unittest.TestCase):
    def test_report_validation_does_not_require_exact_insight_heading(self):
        report = "# 메르AI 보고서\n\n## 현재 모델 포트폴리오\n\n내용"

        self.assertEqual(analyze._validate_markdown_report(report), report)

    def test_excludes_unlisted_stock_suggestion_from_model_output(self):
        invalid = json.loads(json.dumps(DECISION_RESPONSE, ensure_ascii=False))
        invalid["portfolio_decisions"][0]["code"] = None

        parsed = analyze._parse_model_decision_json(
            json.dumps(invalid, ensure_ascii=False)
        )

        self.assertEqual(parsed.portfolio_decisions, [])

    def test_generates_decision_before_markdown_report(self):
        responses = [
            json.dumps(DECISION_RESPONSE, ensure_ascii=False),
            "# 메르AI 포트폴리오 분석\n\n## 인사이트\n\n내용\n\n## 포트폴리오\n\n내용",
        ]

        with patch.object(analyze, "_get_client", return_value=object()), \
             patch.object(analyze, "_call_model_text", side_effect=responses) as call:
            result = analyze.analyze_posts_structured(
                POSTS,
                "2026-06-01",
                {"last_rebalanced_date": "2026-05-14"},
            )

        self.assertEqual(result.decision.portfolio_decisions[0]["name"], "Alcoa")
        self.assertIn("포트폴리오", result.report)
        self.assertEqual(call.call_count, 2)
        report_request = call.call_args_list[1].args[2]
        self.assertIn("구조화 판단 JSON", report_request)

    def test_repairs_invalid_structured_decision_once(self):
        invalid = json.loads(json.dumps(DECISION_RESPONSE, ensure_ascii=False))
        invalid["portfolio_decisions"][0]["evidence_posts"] = []
        responses = [
            json.dumps(invalid, ensure_ascii=False),
            json.dumps(DECISION_RESPONSE, ensure_ascii=False),
            "# 메르AI 보고서\n\n## 현재 모델 포트폴리오\n\n내용",
        ]

        with patch.object(analyze, "_get_client", return_value=object()), \
             patch.object(analyze, "_call_model_text", side_effect=responses) as call:
            result = analyze.analyze_posts_structured(
                POSTS,
                "2026-06-01",
                {"last_rebalanced_date": "2026-05-14"},
            )

        self.assertEqual(result.decision.portfolio_decisions[0]["name"], "Alcoa")
        self.assertEqual(call.call_count, 3)

    def test_repairs_decision_when_applied_portfolio_exceeds_one_hundred(self):
        retained = json.loads(json.dumps(DECISION_RESPONSE["portfolio_decisions"][0]))
        retained["proposed_weight"] = 95.0
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
        repaired = json.loads(json.dumps(additional, ensure_ascii=False))
        repaired["portfolio_decisions"][0]["proposed_weight"] = 5.0
        responses = [
            json.dumps(additional, ensure_ascii=False),
            json.dumps(repaired, ensure_ascii=False),
            "# 메르AI 보고서\n\n## 현재 모델 포트폴리오\n\n내용",
        ]

        with patch.object(analyze, "_get_client", return_value=object()), \
             patch.object(analyze, "_call_model_text", side_effect=responses) as call:
            result = analyze.analyze_posts_structured(
                POSTS,
                "2026-06-01",
                current_state,
            )

        self.assertEqual(result.decision.portfolio_decisions[0]["proposed_weight"], 5.0)
        self.assertEqual(call.call_count, 3)

    def test_repairs_decision_when_listing_code_has_no_price(self):
        responses = [
            json.dumps(DECISION_RESPONSE, ensure_ascii=False),
            json.dumps(DECISION_RESPONSE, ensure_ascii=False),
            "# 메르AI 보고서\n\n## 현재 모델 포트폴리오\n\n내용",
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
        self.assertEqual(call.call_count, 3)

    def test_does_not_return_partial_result_when_report_fails(self):
        responses = [
            json.dumps(DECISION_RESPONSE, ensure_ascii=False),
            "# 제목만 있음",
            "# 제목만 있음",
        ]

        with patch.object(analyze, "_get_client", return_value=object()), \
             patch.object(analyze, "_call_model_text", side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, r"2차 사용자용 보고서 실패"):
                analyze.analyze_posts_structured(
                    POSTS,
                    "2026-06-01",
                    {"last_rebalanced_date": "2026-05-14"},
                )


if __name__ == "__main__":
    unittest.main()
