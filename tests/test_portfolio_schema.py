import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from portfolio_schema import (
    SCHEMA_VERSION,
    PortfolioSchemaError,
    add_business_days,
    advance_watchlist_lifecycle,
    apply_analysis_decision,
    apply_portfolio_decisions,
    append_signal_events,
    evidence_sha256,
    load_portfolio_state_file,
    load_or_migrate_portfolio_state,
    migrate_legacy_state,
    parse_analysis_decision,
    parse_analysis_decision_json,
    parse_portfolio_state,
    parse_portfolio_state_json,
    save_analysis_decision_file,
    save_portfolio_state_file,
    signal_event_id,
    validate_signal_ledger_append_only,
    watchlist_expiry_date,
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
        "basis": "종목 분석",
        "decision_date": "2026-05-28",
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
    value.update(overrides)
    return value


def insight(**overrides):
    value = {
        "id": "aluminum-supply",
        "title": "알루미늄 공급 제한",
        "summary": "기니의 보크사이트 수출 제한으로 알루미늄 공급 불안이 커짐",
        "investment_implication": "원문에 등장한 관련 종목의 수혜 가능성을 검토",
        "evidence_posts": decision()["evidence_posts"],
        "related_decision_codes": ["AA"],
    }
    value.update(overrides)
    return value


def signal_event(**overrides):
    value = {
        "signal_type": "MER_DIRECT",
        "post_id": "224300000001",
        "post_title": "메르 직접 보유 공개",
        "post_url": "https://blog.naver.com/ranto28/224300000001",
        "published_date": "2026-05-29",
        "evidence_text": "나는 Alcoa를 보유하고 있다.",
        "entity": {
            "name": "Alcoa",
            "code": "AA",
            "market": "US",
            "asset_type": "stock",
        },
        "direction": "bullish",
        "horizon": {"min_days": 60, "max_days": 240},
        "catalysts": ["알루미늄 공급 제한"],
        "invalidation_conditions": ["공급 제한 해소"],
        "thesis_id": "alcoa-direct",
        "parent_signal_ids": [],
        "created_by": "summary_model",
        "model_id": "gemini-test",
        "created_at": "2026-05-29T09:00:00+09:00",
    }
    value.update(overrides)
    value["evidence_sha256"] = evidence_sha256(value["evidence_text"])
    value["signal_id"] = signal_event_id(value)
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
                name="NVIDIA",
                code="NVDA",
                action="매도",
                previous_weight=8.0,
                proposed_weight=0.0,
                closed_date="2026-05-30",
                close_reason="투자 근거 훼손",
                closed_performance=3.5,
            )
        ],
        "decision_history": [decision()],
        "insights": [insight()],
        "last_rebalanced_date": "2026-05-14",
    }


class PortfolioSchemaTest(unittest.TestCase):
    def test_parses_first_call_decision_json(self):
        payload = {
            "analysis_date": "2026-06-01",
            "run_type": "regular",
            "insights": [insight()],
            "portfolio_decisions": [decision()],
            "watchlist": state_payload()["watchlist"],
        }

        parsed = parse_analysis_decision_json(
            "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
        )

        self.assertEqual(parsed.run_type, "regular")
        self.assertEqual(parsed.portfolio_decisions[0]["name"], "Alcoa")

    def test_rejects_position_that_is_active_and_closed(self):
        payload = state_payload()
        payload["closed_positions"] = [
            decision(
                action="매도",
                previous_weight=8.0,
                proposed_weight=0.0,
                closed_date="2026-05-30",
                close_reason="잘못된 종료 기록",
                closed_performance=None,
            )
        ]

        with self.assertRaisesRegex(PortfolioSchemaError, "overlaps with active portfolio"):
            parse_portfolio_state_json(json.dumps(payload, ensure_ascii=False))

    def test_allows_reentry_after_a_prior_closed_episode(self):
        state = parse_portfolio_state(state_payload())
        sold = apply_portfolio_decisions(state, [decision(
            action="매도",
            previous_weight=8.0,
            proposed_weight=0.0,
            decision_date="2026-06-01",
        )])

        reentered = apply_portfolio_decisions(sold, [decision(
            action="매수",
            previous_weight=None,
            proposed_weight=8.0,
            decision_date="2026-06-02",
        )])

        self.assertEqual(len(reentered.portfolio), 1)
        self.assertEqual(len(reentered.closed_positions), 2)

    def test_rejects_unknown_first_call_run_type(self):
        payload = {
            "analysis_date": "2026-06-01",
            "run_type": "adhoc",
            "insights": [insight()],
            "portfolio_decisions": [decision()],
            "watchlist": [],
        }

        with self.assertRaisesRegex(PortfolioSchemaError, r"analysis\.run_type"):
            parse_analysis_decision_json(json.dumps(payload, ensure_ascii=False))

    def test_allows_temporary_portfolio_weight_total_over_one_hundred(self):
        payload = state_payload()
        payload["portfolio"].append(
            decision(name="Microsoft", code="MSFT", proposed_weight=93.0)
        )

        parsed = parse_portfolio_state_json(json.dumps(payload, ensure_ascii=False))
        self.assertEqual(len(parsed.portfolio), 2)

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
            projected_state=state_payload(),
            analysis_date="2026-06-01",
        )

        self.assertIn("검증된 구조화 변경분 JSON", message)
        self.assertIn("변경 반영 후 전체 모델 포트폴리오 상태", message)
        self.assertIn('"portfolio_decisions"', message)

    def test_parses_structured_state(self):
        parsed = parse_portfolio_state_json(
            json.dumps(state_payload(), ensure_ascii=False)
        )

        self.assertEqual(parsed.schema_version, SCHEMA_VERSION)
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

    def test_rejects_unmentioned_ai_stock_buy(self):
        payload = state_payload()
        payload["portfolio"][0].update(
            {
                "basis": "섹터 분석",
                "source_mentioned": False,
                "source_scope": "sector_only",
            }
        )

        with self.assertRaisesRegex(
            PortfolioSchemaError,
            r"must stay on the Watchlist",
        ):
            parse_portfolio_state_json(json.dumps(payload, ensure_ascii=False))

    def test_allows_unmentioned_sector_etf_buy_with_required_details(self):
        payload = {
            "analysis_date": "2026-06-01",
            "run_type": "regular",
            "insights": [insight(related_decision_codes=["XME"])],
            "portfolio_decisions": [
                decision(
                    name="금속 ETF",
                    code="XME",
                    asset_type="etf",
                    basis="섹터 분석",
                    source_mentioned=False,
                    source_scope="sector_only",
                )
            ],
            "watchlist": [],
        }

        parsed = parse_analysis_decision(payload)

        self.assertEqual(parsed.portfolio_decisions[0]["asset_type"], "etf")

    def test_rejects_ai_buy_without_allocation_role(self):
        payload = {
            "analysis_date": "2026-06-01",
            "run_type": "regular",
            "insights": [insight()],
            "portfolio_decisions": [decision()],
            "watchlist": [],
        }
        del payload["portfolio_decisions"][0]["allocation_role"]

        with self.assertRaisesRegex(PortfolioSchemaError, r"allocation_role"):
            parse_analysis_decision(payload)

    def test_rejects_ai_hold_without_allocation_role(self):
        item = decision(action="보유", proposed_weight=8.0)
        del item["allocation_role"]
        payload = {
            "analysis_date": "2026-06-01",
            "run_type": "regular",
            "insights": [insight()],
            "portfolio_decisions": [item],
            "watchlist": [],
        }

        with self.assertRaisesRegex(PortfolioSchemaError, r"allocation_role"):
            parse_analysis_decision(payload)

    def test_rejects_changed_decision_without_linked_insight(self):
        payload = {
            "analysis_date": "2026-06-01",
            "run_type": "regular",
            "insights": [insight()],
            "portfolio_decisions": [decision(linked_insight_ids=[])],
            "watchlist": [],
        }

        with self.assertRaisesRegex(PortfolioSchemaError, r"linked_insight_ids must not be empty"):
            parse_analysis_decision(payload)

    def test_rejects_ai_buy_without_key_risk(self):
        payload = {
            "analysis_date": "2026-06-01",
            "run_type": "regular",
            "insights": [insight()],
            "portfolio_decisions": [decision(key_risks=[])],
            "watchlist": [],
        }

        with self.assertRaisesRegex(PortfolioSchemaError, r"key_risks must not be empty"):
            parse_analysis_decision(payload)

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
        payload = state_payload()
        # The test covers a normal v2.1 operating state.  A v2.0 payload is
        # deliberately normalized to a fresh rebalance baseline on migration.
        payload["schema_version"] = SCHEMA_VERSION
        state = parse_portfolio_state_json(json.dumps(payload, ensure_ascii=False))

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

    def test_rebalance_rejects_cash_below_defensive_target(self):
        payload = state_payload()
        payload["portfolio"][0]["action"] = "보유"
        payload["portfolio"][0]["previous_weight"] = 97.0
        payload["portfolio"][0]["proposed_weight"] = 97.0
        state = parse_portfolio_state_json(json.dumps(payload, ensure_ascii=False))

        updated = apply_portfolio_decisions(
            state,
            [decision(action="보유", previous_weight=97.0, proposed_weight=97.0, change_reason="기존 논리 유지")],
            rebalanced_date="2026-06-01",
        )
        self.assertEqual(updated.portfolio[0]["proposed_weight"], 97.0)

    def test_rebalance_allows_cash_restored_to_defensive_target(self):
        payload = state_payload()
        payload["portfolio"][0]["action"] = "보유"
        payload["portfolio"][0]["previous_weight"] = 97.0
        payload["portfolio"][0]["proposed_weight"] = 97.0
        state = parse_portfolio_state_json(json.dumps(payload, ensure_ascii=False))

        updated = apply_portfolio_decisions(
            state,
            [
                decision(
                    action="비중축소",
                    previous_weight=97.0,
                    proposed_weight=80.0,
                    change_reason="현금성 20% 방어 기준에 맞추기 위해 비중 축소",
                )
            ],
            rebalanced_date="2026-06-01",
        )

        self.assertEqual(updated.portfolio[0]["proposed_weight"], 80.0)
        self.assertEqual(updated.last_rebalanced_date, "2026-06-01")

    def test_rejects_reducing_cash_below_defensive_target_without_reason(self):
        payload = state_payload()
        payload["portfolio"][0]["proposed_weight"] = 75.0
        state = parse_portfolio_state_json(json.dumps(payload, ensure_ascii=False))
        new_buy = decision(
            name="Microsoft",
            code="MSFT",
            proposed_weight=10.0,
            change_reason="AI 인프라 수혜 추론",
        )

        updated = apply_portfolio_decisions(state, [new_buy])
        self.assertEqual(len(updated.portfolio), 2)

    def test_allows_reducing_cash_below_target_with_defensive_reason(self):
        payload = state_payload()
        payload["portfolio"][0]["proposed_weight"] = 75.0
        state = parse_portfolio_state_json(json.dumps(payload, ensure_ascii=False))
        new_buy = decision(
            name="Microsoft",
            code="MSFT",
            proposed_weight=10.0,
            change_reason="현금 방어 비중을 낮출 만큼 AI 인프라 근거가 강하지만 다음 리밸런싱에서 재점검",
        )

        updated = apply_portfolio_decisions(state, [new_buy])

        self.assertEqual(len(updated.portfolio), 2)

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
            "insights": [insight()],
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

    def test_migration_corrects_known_legacy_listing_code(self):
        legacy = {
            "schema_version": "1.0",
            "last_report_date": "2026-06-04",
            "holdings": [
                {
                    "name": "대한전선",
                    "code": "011440",
                    "market": "KR",
                    "weight": "3%",
                    "entry_date": "2026-05-12",
                    "last_confirmed_date": "2026-06-04",
                    "status": "active",
                }
            ],
        }

        migrated = migrate_legacy_state(legacy)

        self.assertEqual(migrated.portfolio[0]["code"], "001440")

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

    def test_loads_existing_v2_state_with_conservative_upgrade(self):
        payload = state_payload()

        loaded = load_or_migrate_portfolio_state(payload)

        self.assertEqual(loaded.schema_version, SCHEMA_VERSION)
        self.assertEqual(loaded.portfolio[0]["proposed_weight"], 8.0)
        self.assertEqual(loaded.portfolio[0]["provenance_status"], "legacy_unvalidated")
        self.assertEqual(loaded.portfolio[0]["origin_signal_type"], "LEGACY_UNVALIDATED")
        self.assertEqual(loaded.signal_events, [])
        self.assertEqual(loaded.watchlist_archive, [])

    def test_v20_upgrade_resets_only_legacy_rebalance_baseline(self):
        legacy = state_payload()

        upgraded = parse_portfolio_state(legacy)
        self.assertIsNone(upgraded.last_rebalanced_date)

        current = upgraded.to_dict()
        current["last_rebalanced_date"] = "2026-06-01"
        preserved = parse_portfolio_state(current)

        self.assertEqual(preserved.last_rebalanced_date, "2026-06-01")

    def test_rejects_unclassified_actor_in_new_gemini_decision(self):
        payload = {
            "analysis_date": "2026-06-01",
            "run_type": "regular",
            "insights": [insight()],
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

        self.assertEqual(saved["schema_version"], SCHEMA_VERSION)
        self.assertEqual(saved["portfolio"][0]["decision_actor"], "미분류")

    def test_applies_first_reevaluation_and_adds_watchlist_update(self):
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
                "insights": [insight()],
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

    def test_empty_delta_preserves_portfolio_and_watchlist(self):
        state = parse_portfolio_state(state_payload())
        analysis = parse_analysis_decision(
            {
                "analysis_date": "2026-06-01",
                "run_type": "regular",
                "insights": [],
                "portfolio_decisions": [],
                "watchlist": [],
            }
        )

        updated = apply_analysis_decision(state, analysis)

        self.assertEqual(updated.portfolio, state.portfolio)
        self.assertEqual(updated.watchlist[0]["name"], state.watchlist[0]["name"])
        self.assertEqual(updated.watchlist[0]["watchlist_duration_days"], 3)

    def test_watchlist_delta_updates_one_item_without_dropping_others(self):
        payload = state_payload()
        second = dict(payload["watchlist"][0])
        second["name"] = "헬륨"
        second["code"] = ""
        payload["watchlist"].append(second)
        state = parse_portfolio_state(payload)
        update = dict(payload["watchlist"][0])
        update["status"] = "재검토 필요"
        update["observation_reason"] = "새 글에서 추가 확인이 필요해짐"
        analysis = parse_analysis_decision(
            {
                "analysis_date": "2026-06-01",
                "run_type": "regular",
                "insights": [],
                "portfolio_decisions": [],
                "watchlist": [update],
            }
        )

        updated = apply_analysis_decision(state, analysis)

        self.assertEqual(len(updated.watchlist), 2)
        self.assertEqual(updated.watchlist[0]["status"], "재검토 필요")
        self.assertEqual(updated.watchlist[1]["name"], "헬륨")

    def test_v2_upgrade_is_idempotent_and_quarantines_unknown_provenance(self):
        first = parse_portfolio_state(state_payload())
        second = parse_portfolio_state(first.to_dict())

        self.assertEqual(second.to_dict(), first.to_dict())
        self.assertEqual(first.schema_version, SCHEMA_VERSION)
        self.assertEqual(first.portfolio[0]["provenance_status"], "legacy_unvalidated")
        self.assertEqual(first.portfolio[0]["origin_signal_ids"], [])
        self.assertTrue(first.portfolio[0]["thesis_id"].startswith("legacy-"))
        self.assertIn("expires_on", first.watchlist[0])

    def test_v2_upgrade_moves_terminal_watchlist_item_to_archive(self):
        payload = state_payload()
        payload["watchlist"][0]["status"] = "포트폴리오 편입"

        upgraded = parse_portfolio_state(payload)

        self.assertEqual(upgraded.watchlist, [])
        self.assertEqual(len(upgraded.watchlist_archive), 1)
        self.assertEqual(upgraded.watchlist_archive[0]["lifecycle_status"], "promoted")

    def test_v2_upgrade_deduplicates_same_watchlist_thesis(self):
        payload = state_payload()
        payload["watchlist"].append(dict(payload["watchlist"][0]))

        upgraded = parse_portfolio_state(payload)

        self.assertEqual(len(upgraded.watchlist), 1)

    def test_signal_ledger_append_is_content_addressed_and_idempotent(self):
        state = parse_portfolio_state(state_payload())
        event = signal_event()

        once = append_signal_events(state, [event])
        twice = append_signal_events(once, [event])

        self.assertEqual(len(once.signal_events), 1)
        self.assertEqual(twice.signal_events, once.signal_events)

    def test_signal_ledger_rejects_attempted_event_mutation(self):
        state = append_signal_events(
            parse_portfolio_state(state_payload()),
            [signal_event()],
        )
        mutated = dict(state.signal_events[0])
        mutated["direction"] = "bearish"

        with self.assertRaisesRegex(PortfolioSchemaError, "content-addressed id"):
            append_signal_events(state, [mutated])

    def test_signal_ledger_append_only_validator_rejects_deletion(self):
        event = signal_event()

        with self.assertRaisesRegex(PortfolioSchemaError, "missing prior signal_id"):
            validate_signal_ledger_append_only([event], [])

    def test_state_file_save_cannot_delete_persisted_signal(self):
        state = append_signal_events(parse_portfolio_state(state_payload()), [signal_event()])
        without_signal = parse_portfolio_state(state_payload())
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "portfolio_state.json"
            save_portfolio_state_file(state, path)

            with self.assertRaisesRegex(PortfolioSchemaError, "missing prior signal_id"):
                save_portfolio_state_file(without_signal, path)

    def test_signal_ledger_requires_known_parent_for_ai_inference(self):
        source = signal_event()
        inferred = signal_event(
            signal_type="AI_INFERRED",
            evidence_text="공급 제한의 수혜가 Alcoa 이익으로 이어질 수 있다.",
            parent_signal_ids=["sig_unknown"],
            created_by="decision_model",
        )

        with self.assertRaisesRegex(PortfolioSchemaError, "unknown ids"):
            append_signal_events(parse_portfolio_state(state_payload()), [source, inferred])

    def test_verified_mer_origin_survives_later_ai_management(self):
        event = signal_event()
        state = append_signal_events(parse_portfolio_state(state_payload()), [event])
        payload = state.to_dict()
        payload["portfolio"][0].update({
            "provenance_status": "verified",
            "origin_signal_type": "MER_DIRECT",
            "origin_signal_ids": [event["signal_id"]],
            "linked_signal_ids": [event["signal_id"]],
            "thesis_id": event["thesis_id"],
        })
        state = parse_portfolio_state(payload)
        ai_update = decision(
            action="보유",
            previous_weight=8.0,
            proposed_weight=8.0,
            decision_date="2026-06-01",
        )

        updated = apply_portfolio_decisions(state, [ai_update])

        self.assertEqual(updated.portfolio[0]["decision_actor"], "AI")
        self.assertEqual(updated.portfolio[0]["origin_signal_type"], "MER_DIRECT")
        self.assertEqual(updated.portfolio[0]["origin_signal_ids"], [event["signal_id"]])
        self.assertEqual(updated.decision_history[-1]["origin_signal_type"], "MER_DIRECT")

    def test_incompatible_new_signal_cannot_expand_existing_verified_long(self):
        bullish = signal_event()
        bearish = signal_event(
            evidence_text="Alcoa는 공급 정상화로 피해를 받을 수 있다.",
            direction="bearish",
            thesis_id="alcoa-bearish",
        )
        state = append_signal_events(
            parse_portfolio_state(state_payload()),
            [bullish, bearish],
        )
        payload = state.to_dict()
        payload["portfolio"][0].update({
            "provenance_status": "verified",
            "origin_signal_type": "MER_DIRECT",
            "origin_signal_ids": [bullish["signal_id"]],
            "linked_signal_ids": [bullish["signal_id"]],
            "thesis_id": bullish["thesis_id"],
        })
        state = parse_portfolio_state(payload)
        incompatible = decision(
            action="비중확대",
            previous_weight=8.0,
            proposed_weight=9.0,
            linked_signal_ids=[bearish["signal_id"]],
        )

        with self.assertRaisesRegex(PortfolioSchemaError, "signals incompatible"):
            apply_portfolio_decisions(state, [incompatible])

        discarded_link = decision(
            action="비중확대",
            previous_weight=8.0,
            proposed_weight=9.0,
            linked_signal_ids=[],
            rejected_linked_signal_ids=[bearish["signal_id"]],
        )
        with self.assertRaisesRegex(PortfolioSchemaError, "signals incompatible"):
            apply_portfolio_decisions(state, [discarded_link])

    def test_business_day_ttl_skips_weekends_for_all_watchlist_kinds(self):
        self.assertEqual(add_business_days("2026-05-29", 1), "2026-06-01")
        self.assertEqual(watchlist_expiry_date("2026-05-29", "mention"), "2026-06-12")
        self.assertEqual(watchlist_expiry_date("2026-05-29", "event"), "2026-06-26")
        self.assertEqual(watchlist_expiry_date("2026-05-29", "cyclical"), "2026-08-21")
        self.assertEqual(watchlist_expiry_date("2026-05-29", "structural"), "2026-11-13")

    def test_watchlist_expires_on_business_day_boundary(self):
        state = parse_portfolio_state(state_payload())

        before = advance_watchlist_lifecycle(state, "2026-06-11")
        expired = advance_watchlist_lifecycle(before, "2026-06-12")

        self.assertEqual(len(before.watchlist), 1)
        self.assertEqual(expired.watchlist, [])
        self.assertEqual(expired.watchlist_archive[0]["lifecycle_status"], "expired")
        self.assertEqual(
            expired.last_watchlist_changes["expired"],
            [state.watchlist[0]["thesis_id"]],
        )

    def test_repeated_watchlist_evidence_does_not_extend_expiry(self):
        state = parse_portfolio_state(state_payload())
        update = dict(state_payload()["watchlist"][0])
        update["latest_evidence_date"] = "2026-06-01"
        analysis = parse_analysis_decision({
            "analysis_date": "2026-06-01",
            "run_type": "regular",
            "insights": [],
            "portfolio_decisions": [],
            "watchlist": [update],
        })

        updated = apply_analysis_decision(state, analysis)

        self.assertEqual(updated.watchlist[0]["expires_on"], "2026-06-12")
        self.assertEqual(
            updated.watchlist[0]["latest_material_signal_date"],
            "2026-05-29",
        )

    def test_watchlist_is_promoted_when_security_enters_portfolio(self):
        payload = state_payload()
        watch = payload["watchlist"][0]
        watch.update({
            "name": "Alcoa",
            "code": "AA",
            "market": "US",
            "asset_type": "stock",
        })
        state = parse_portfolio_state(payload)

        updated = advance_watchlist_lifecycle(state, "2026-06-01")

        self.assertEqual(updated.watchlist, [])
        self.assertEqual(updated.watchlist_archive[0]["lifecycle_status"], "promoted")
        self.assertEqual(updated.watchlist_archive[0]["portfolio_entry_date"], "2026-06-01")

    def test_saves_validated_analysis_decision_file(self):
        analysis = parse_analysis_decision(
            {
                "analysis_date": "2026-06-01",
                "run_type": "regular",
                "insights": [insight()],
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
