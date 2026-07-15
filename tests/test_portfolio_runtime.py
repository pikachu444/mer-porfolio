import unittest

from portfolio_runtime import allocate_projected_state
from portfolio_schema import parse_portfolio_state


def state_with(*positions):
    return parse_portfolio_state({
        "schema_version": "2.1", "portfolio": list(positions), "watchlist": [],
        "watchlist_archive": [], "closed_positions": [], "decision_history": [],
        "insights": [], "signal_events": [],
        "last_watchlist_changes": {"added": [], "updated": [], "promoted": [], "rejected": [], "expired": [], "archived": []},
        "last_rebalanced_date": None, "admin_review_queue": [],
    })


def position(code, asset_type, weight):
    return {
        "name": code, "code": code, "market": "US", "asset_type": asset_type,
        "decision_actor": "AI", "action": "매수", "basis": "종목 분석" if asset_type == "stock" else "섹터 분석",
        "decision_date": "2026-07-15", "evidence_posts": [{"title": "근거", "url": "https://example.test", "published_date": "2026-07-15"}],
        "source_mentioned": True, "previous_weight": 0.0, "proposed_weight": weight,
        "weight_source": "AI 제안", "change_reason": "원문 근거", "source_scope": "source_named_security" if asset_type == "stock" else "sector_only",
        "investment_rationale": "원문 근거", "current_entry_reason": "현재 편입 이유", "key_risks": ["위험"],
        "linked_insight_ids": [], "provenance_status": "legacy_unvalidated", "origin_signal_type": "LEGACY_UNVALIDATED",
        "origin_signal_ids": [], "linked_signal_ids": [], "thesis_id": code,
    }


class RuntimeWeightGuardrailTest(unittest.TestCase):
    def test_caps_and_residual_cash(self):
        state, summary = allocate_projected_state(state_with(position("AAA", "stock", 18), position("ETF", "etf", 45)))
        self.assertEqual([item["proposed_weight"] for item in state.portfolio], [10.0, 30.0])
        self.assertEqual(summary["cash_weight"], 60.0)

    def test_normalizes_only_when_capped_total_exceeds_one_hundred(self):
        projected = state_with(*(position(f"ETF{i}", "etf", 30) for i in range(4)))
        state, summary = allocate_projected_state(projected)
        self.assertEqual([item["proposed_weight"] for item in state.portfolio], [25.0] * 4)
        self.assertTrue(summary["normalized_to_100"])
        self.assertEqual(summary["cash_weight"], 0.0)
