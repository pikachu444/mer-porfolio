import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from portfolio_schema import (
    PortfolioSchemaError,
    apply_analysis_decision,
    apply_portfolio_decisions,
    load_portfolio_state_file,
    load_or_migrate_portfolio_state,
    migrate_legacy_state,
    parse_analysis_decision,
    parse_analysis_decision_json,
    parse_portfolio_state_json,
    save_analysis_decision_file,
    save_portfolio_state_file,
)
from system_prompt import (
    DECISION_SYSTEM_PROMPT,
    build_decision_user_message,
    build_report_user_message,
)
from portfolio_validation import validate_structured_decisions


def decision(**overrides):
    value = {
        "name": "Alcoa",
        "code": "AA",
        "market": "US",
        "asset_type": "stock",
        "decision_actor": "AI",
        "action": "매수",
        "basis": "섹터 분석",
        "decision_date": "2026-05-28",
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
    value.update(overrides)
    return value


def state_payload():
    return {
        "schema_version": "2.0",
        "portfolio": [decision()],
        "watchlist": [
            {
                "name": "우주 데이터센터",
                "code": "",
                "market": "",
                "asset_type": "sector",
                "decision_actor": "메르",
                "basis": "직접 발언",
                "decision_date": "2026-05-29",
                "evidence_posts": [
                    {
                        "title": "달에서 데이터센터를 돌리면?",
                        "url": "https://blog.naver.com/ranto28/456",
                        "published_date": "2026-05-29",
                    }
                ],
                "source_mentioned": True,
                "watchlist_entry_date": "2026-05-29",
                "latest_evidence_date": "2026-05-29",
                "watchlist_duration_days": 0,
                "portfolio_entry_date": None,
                "watchlist_closed_date": None,
                "status": "관심",
            }
        ],
        "closed_positions": [
            decision(
                action="매도",
                previous_weight=8.0,
                proposed_weight=0.0,
                closed_date="2026-05-30",
                close_reason="투자 근거 훼손",
                closed_performance=3.5,
            )
        ],
        "decision_history": [decision()],
        "last_rebalanced_date": "2026-05-14",
    }


class PortfolioSchemaTest(unittest.TestCase):
    def test_parses_first_call_decision_json(self):
        payload = {
            "analysis_date": "2026-06-01",
            "run_type": "regular",
            "portfolio_decisions": [decision()],
            "watchlist": state_payload()["watchlist"],
        }

        parsed = parse_analysis_decision_json(
            "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
        )

        self.assertEqual(parsed.run_type, "regular")
        self.assertEqual(parsed.portfolio_decisions[0]["name"], "Alcoa")

    def test_rejects_unknown_first_call_run_type(self):
        payload = {
            "analysis_date": "2026-06-01",
            "run_type": "adhoc",
            "portfolio_decisions": [decision()],
            "watchlist": [],
        }

        with self.assertRaisesRegex(PortfolioSchemaError, r"analysis\.run_type"):
            parse_analysis_decision_json(json.dumps(payload, ensure_ascii=False))

    def test_decision_prompt_requests_structured_contract(self):
        message = build_decision_user_message(
            context="블로그 본문",
            analysis_date="2026-06-01",
            run_type="regular",
            current_state={"last_rebalanced_date": "2026-05-14"},
        )

        self.assertIn("JSON 객체 하나만 출력", DECISION_SYSTEM_PROMPT)
        self.assertIn('"last_rebalanced_date": "2026-05-14"', message)
        self.assertIn("블로그 본문", message)

    def test_report_prompt_uses_validated_decision_json(self):
        message = build_report_user_message(
            context="블로그 본문",
            decision_payload={
                "analysis_date": "2026-06-01",
                "run_type": "regular",
                "portfolio_decisions": [decision()],
                "watchlist": [],
            },
            analysis_date="2026-06-01",
        )

        self.assertIn("검증된 구조화 판단 JSON", message)
        self.assertIn('"portfolio_decisions"', message)

    def test_parses_structured_state(self):
        parsed = parse_portfolio_state_json(
            json.dumps(state_payload(), ensure_ascii=False)
        )

        self.assertEqual(parsed.schema_version, "2.0")
        self.assertEqual(parsed.portfolio[0]["decision_actor"], "AI")
        self.assertEqual(parsed.watchlist[0]["status"], "관심")
        self.assertEqual(parsed.closed_positions[0]["action"], "매도")

    def test_rejects_unknown_decision_actor(self):
        payload = state_payload()
        payload["portfolio"][0]["decision_actor"] = "사용자"

        with self.assertRaisesRegex(
            PortfolioSchemaError,
            r"state\.portfolio\[0\]\.decision_actor",
        ):
            parse_portfolio_state_json(json.dumps(payload, ensure_ascii=False))

    def test_rejects_stock_portfolio_decision_without_listing_code(self):
        payload = state_payload()
        payload["portfolio"][0]["code"] = ""

        with self.assertRaisesRegex(
            PortfolioSchemaError,
            r"state\.portfolio\[0\]\.code must not be empty",
        ):
            parse_portfolio_state_json(json.dumps(payload, ensure_ascii=False))

    def test_rejects_missing_change_reason(self):
        payload = state_payload()
        del payload["portfolio"][0]["change_reason"]

        with self.assertRaisesRegex(
            PortfolioSchemaError,
            r"state\.portfolio\[0\]\.change_reason",
        ):
            parse_portfolio_state_json(json.dumps(payload, ensure_ascii=False))

    def test_rejects_watchlist_weight(self):
        payload = state_payload()
        payload["watchlist"][0]["proposed_weight"] = 5.0

        with self.assertRaisesRegex(
            PortfolioSchemaError,
            r"state\.watchlist\[0\]\.proposed_weight",
        ):
            parse_portfolio_state_json(json.dumps(payload, ensure_ascii=False))

    def test_rejects_mer_decision_without_direct_statement_basis(self):
        payload = state_payload()
        payload["portfolio"][0].update(
            {
                "decision_actor": "메르",
                "basis": "종목 분석",
                "source_mentioned": True,
            }
        )

        with self.assertRaisesRegex(
            PortfolioSchemaError,
            r"state\.portfolio\[0\]\.basis",
        ):
            parse_portfolio_state_json(json.dumps(payload, ensure_ascii=False))

    def test_rejects_mer_decision_without_evidence_post(self):
        payload = state_payload()
        payload["portfolio"][0].update(
            {
                "decision_actor": "메르",
                "basis": "직접 발언",
                "source_mentioned": True,
                "evidence_posts": [],
            }
        )

        with self.assertRaisesRegex(
            PortfolioSchemaError,
            r"state\.portfolio\[0\]\.evidence_posts",
        ):
            parse_portfolio_state_json(json.dumps(payload, ensure_ascii=False))

    def test_rejects_mer_decision_not_mentioned_in_source(self):
        payload = state_payload()
        payload["portfolio"][0].update(
            {
                "decision_actor": "메르",
                "basis": "직접 발언",
                "source_mentioned": False,
            }
        )

        with self.assertRaisesRegex(
            PortfolioSchemaError,
            r"state\.portfolio\[0\]\.source_mentioned",
        ):
            parse_portfolio_state_json(json.dumps(payload, ensure_ascii=False))

    def test_rejects_unmentioned_ai_buy_without_sector_basis(self):
        payload = state_payload()
        payload["portfolio"][0]["basis"] = "종목 분석"

        with self.assertRaisesRegex(
            PortfolioSchemaError,
            r"state\.portfolio\[0\]\.basis",
        ):
            parse_portfolio_state_json(json.dumps(payload, ensure_ascii=False))

    def test_rejects_unmentioned_ai_buy_without_evidence_post(self):
        payload = state_payload()
        payload["portfolio"][0]["evidence_posts"] = []

        with self.assertRaisesRegex(
            PortfolioSchemaError,
            r"state\.portfolio\[0\]\.evidence_posts",
        ):
            parse_portfolio_state_json(json.dumps(payload, ensure_ascii=False))

    def test_allows_unmentioned_ai_hold_with_previous_decision_basis(self):
        payload = state_payload()
        payload["portfolio"][0].update(
            {
                "action": "보유",
                "basis": "이전 판단 유지",
            }
        )

        parsed = parse_portfolio_state_json(json.dumps(payload, ensure_ascii=False))

        self.assertEqual(parsed.portfolio[0]["basis"], "이전 판단 유지")

    def test_transition_keeps_unmentioned_existing_position(self):
        state = parse_portfolio_state_json(json.dumps(state_payload(), ensure_ascii=False))

        updated = apply_portfolio_decisions(state, [])

        self.assertEqual(updated.portfolio, state.portfolio)

    def test_transition_reduces_weight_without_closing_position(self):
        state = parse_portfolio_state_json(json.dumps(state_payload(), ensure_ascii=False))
        reduced = decision(
            action="비중축소",
            basis="이전 판단 유지",
            previous_weight=8.0,
            proposed_weight=5.0,
        )

        updated = apply_portfolio_decisions(state, [reduced])

        self.assertEqual(len(updated.portfolio), 1)
        self.assertEqual(updated.portfolio[0]["action"], "비중축소")
        self.assertEqual(updated.portfolio[0]["proposed_weight"], 5.0)

    def test_transition_moves_sell_to_closed_positions(self):
        state = parse_portfolio_state_json(json.dumps(state_payload(), ensure_ascii=False))
        sell = decision(
            action="매도",
            basis="종목 분석",
            previous_weight=8.0,
            proposed_weight=0.0,
            change_reason="공급 제한 수혜 근거가 훼손됨",
        )

        updated = apply_portfolio_decisions(
            state,
            [sell],
            closed_performance_by_key={"stock:US:AA": 4.2},
        )

        self.assertEqual(updated.portfolio, [])
        self.assertEqual(updated.closed_positions[-1]["action"], "매도")
        self.assertEqual(updated.closed_positions[-1]["closed_date"], "2026-05-28")
        self.assertEqual(updated.closed_positions[-1]["previous_weight"], 8.0)
        self.assertEqual(updated.closed_positions[-1]["closed_performance"], 4.2)
        self.assertEqual(updated.watchlist, state.watchlist)

    def test_transition_rejects_sell_for_unknown_position(self):
        state = parse_portfolio_state_json(json.dumps(state_payload(), ensure_ascii=False))
        sell = decision(
            name="NVIDIA",
            code="NVDA",
            action="매도",
            basis="종목 분석",
            proposed_weight=0.0,
        )

        with self.assertRaisesRegex(
            PortfolioSchemaError,
            r"cannot sell a position that is not in portfolio",
        ):
            apply_portfolio_decisions(state, [sell])

    def test_transition_keeps_rebalance_date_for_regular_analysis(self):
        state = parse_portfolio_state_json(json.dumps(state_payload(), ensure_ascii=False))

        updated = apply_portfolio_decisions(state, [decision(proposed_weight=9.0)])

        self.assertEqual(updated.last_rebalanced_date, "2026-05-14")

    def test_transition_updates_rebalance_date_only_when_requested(self):
        state = parse_portfolio_state_json(json.dumps(state_payload(), ensure_ascii=False))

        updated = apply_portfolio_decisions(
            state,
            [decision(proposed_weight=9.0)],
            rebalanced_date="2026-05-28",
        )

        self.assertEqual(updated.last_rebalanced_date, "2026-05-28")

    def test_rejects_weight_change_without_reason(self):
        payload = state_payload()
        payload["portfolio"][0]["change_reason"] = ""

        with self.assertRaisesRegex(
            PortfolioSchemaError,
            r"state\.portfolio\[0\]\.change_reason",
        ):
            parse_portfolio_state_json(json.dumps(payload, ensure_ascii=False))

    def test_rejects_invalid_evidence_url(self):
        payload = state_payload()
        payload["portfolio"][0]["evidence_posts"][0]["url"] = "not-a-url"

        with self.assertRaisesRegex(
            PortfolioSchemaError,
            r"state\.portfolio\[0\]\.evidence_posts\[0\]\.url",
        ):
            parse_portfolio_state_json(json.dumps(payload, ensure_ascii=False))

    def test_rejects_invalid_decision_date(self):
        payload = state_payload()
        payload["portfolio"][0]["decision_date"] = "2026-02-31"

        with self.assertRaisesRegex(
            PortfolioSchemaError,
            r"state\.portfolio\[0\]\.decision_date",
        ):
            parse_portfolio_state_json(json.dumps(payload, ensure_ascii=False))

    def test_rejects_weight_change_without_evidence_post(self):
        payload = state_payload()
        payload["portfolio"][0].update(
            {
                "previous_weight": 5.0,
                "proposed_weight": 8.0,
                "evidence_posts": [],
            }
        )

        with self.assertRaisesRegex(
            PortfolioSchemaError,
            r"state\.portfolio\[0\]\.evidence_posts",
        ):
            parse_portfolio_state_json(json.dumps(payload, ensure_ascii=False))

    def test_structured_validation_accepts_valid_payload(self):
        payload = {
            "analysis_date": "2026-06-01",
            "run_type": "regular",
            "portfolio_decisions": [decision()],
            "watchlist": [],
        }

        validated = validate_structured_decisions(payload)

        self.assertEqual(validated.portfolio_decisions[0]["name"], "Alcoa")

    def test_migrates_legacy_active_holding_as_unclassified(self):
        legacy = {
            "schema_version": "1.0",
            "last_report_date": "2026-05-31",
            "holdings": [
                {
                    "name": "LS",
                    "code": "006220",
                    "market": "KR",
                    "action": "보유",
                    "weight": "8%",
                    "entry_date": "2026-05-12",
                    "last_confirmed_date": "2026-05-31",
                    "status": "active",
                    "basis_type": "기존보유",
                }
            ],
        }

        migrated = migrate_legacy_state(legacy)

        self.assertIsNone(migrated.last_rebalanced_date)
        self.assertEqual(migrated.portfolio[0]["decision_actor"], "미분류")
        self.assertEqual(migrated.portfolio[0]["basis"], "이전 판단 유지")
        self.assertEqual(migrated.portfolio[0]["proposed_weight"], 8.0)

    def test_migrates_legacy_removed_holding_to_closed_positions(self):
        legacy = {
            "schema_version": "1.0",
            "last_report_date": "2026-05-31",
            "holdings": [
                {
                    "name": "NVIDIA",
                    "code": "NVDA",
                    "market": "US",
                    "action": "Buy",
                    "weight": "15%",
                    "entry_date": "2026-05-12",
                    "last_confirmed_date": "2026-05-29",
                    "status": "removed",
                    "removed_date": "2026-05-30",
                    "removed_reason": "기존 종료 사유",
                }
            ],
        }

        migrated = migrate_legacy_state(legacy)

        self.assertEqual(migrated.portfolio, [])
        self.assertEqual(migrated.closed_positions[0]["name"], "NVIDIA")
        self.assertEqual(migrated.closed_positions[0]["decision_actor"], "미분류")
        self.assertEqual(migrated.closed_positions[0]["close_reason"], "기존 종료 사유")
        self.assertEqual(migrated.closed_positions[0]["previous_weight"], 15.0)

    def test_loads_existing_v2_state_without_migration(self):
        payload = state_payload()

        loaded = load_or_migrate_portfolio_state(payload)

        self.assertEqual(loaded.to_dict(), payload)

    def test_rejects_unclassified_actor_in_new_gemini_decision(self):
        payload = {
            "analysis_date": "2026-06-01",
            "run_type": "regular",
            "portfolio_decisions": [
                decision(
                    decision_actor="미분류",
                    basis="이전 판단 유지",
                    source_mentioned=False,
                )
            ],
            "watchlist": [],
        }

        with self.assertRaisesRegex(
            PortfolioSchemaError,
            r"analysis\.portfolio_decisions\[0\]\.decision_actor",
        ):
            validate_structured_decisions(payload)

    def test_loads_legacy_file_and_saves_valid_v2_file(self):
        legacy = {
            "schema_version": "1.0",
            "last_report_date": "2026-05-31",
            "holdings": [
                {
                    "name": "LS",
                    "code": "006220",
                    "market": "KR",
                    "weight": "8%",
                    "entry_date": "2026-05-12",
                    "last_confirmed_date": "2026-05-31",
                    "status": "active",
                }
            ],
        }
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "portfolio_state.json"
            source.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

            migrated = load_portfolio_state_file(source)
            save_portfolio_state_file(migrated, source)
            saved = json.loads(source.read_text(encoding="utf-8"))

        self.assertEqual(saved["schema_version"], "2.0")
        self.assertEqual(saved["portfolio"][0]["decision_actor"], "미분류")

    def test_applies_first_reevaluation_and_replaces_watchlist(self):
        legacy = {
            "schema_version": "1.0",
            "last_report_date": "2026-05-31",
            "holdings": [
                {
                    "name": "Alcoa",
                    "code": "AA",
                    "market": "US",
                    "weight": "8%",
                    "entry_date": "2026-05-28",
                    "last_confirmed_date": "2026-05-31",
                    "status": "active",
                }
            ],
        }
        state = migrate_legacy_state(legacy)
        analysis = parse_analysis_decision(
            {
                "analysis_date": "2026-06-01",
                "run_type": "rebalance",
                "portfolio_decisions": [
                    decision(
                        action="보유",
                        basis="종목 분석",
                        source_mentioned=True,
                        previous_weight=8.0,
                        proposed_weight=8.0,
                    )
                ],
                "watchlist": state_payload()["watchlist"],
            }
        )

        updated = apply_analysis_decision(state, analysis)

        self.assertEqual(updated.portfolio[0]["decision_actor"], "AI")
        self.assertEqual(updated.watchlist[0]["name"], "우주 데이터센터")
        self.assertEqual(updated.last_rebalanced_date, "2026-06-01")

    def test_saves_validated_analysis_decision_file(self):
        analysis = parse_analysis_decision(
            {
                "analysis_date": "2026-06-01",
                "run_type": "regular",
                "portfolio_decisions": [decision()],
                "watchlist": [],
            }
        )
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "decision.json"

            save_analysis_decision_file(analysis, path)
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(saved["analysis_date"], "2026-06-01")
        self.assertEqual(saved["portfolio_decisions"][0]["name"], "Alcoa")


if __name__ == "__main__":
    unittest.main()
