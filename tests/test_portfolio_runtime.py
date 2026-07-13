import unittest

from portfolio_provenance import enrich_decision_provenance, prepare_post_signal_events
from portfolio_runtime import (
    PortfolioPolicyBlocked,
    allocate_projected_state,
    cap_normal_rebalance_turnover,
    ensure_policy_positions,
    security_key,
    update_ledger_risk_state,
    validate_rebalance_coverage,
)
from portfolio_schema import apply_analysis_decision, parse_analysis_decision, parse_portfolio_state
from test_portfolio_schema import decision, insight, state_payload


class PortfolioRuntimeTest(unittest.TestCase):
    def test_normal_turnover_is_gross_notional_and_capped_pro_rata(self):
        result = cap_normal_rebalance_turnover(
            {"stock:KR:A": 20.0, "stock:KR:B": 0.0},
            {"stock:KR:A": 10.0, "stock:KR:B": 20.0},
        )

        self.assertEqual(result.raw_turnover, 30.0)
        self.assertEqual(result.applied_turnover, 15.0)
        self.assertEqual(result.targets, {
            "stock:KR:A": 15.0,
            "stock:KR:B": 10.0,
        })
        self.assertTrue(result.capped)

    def test_forced_reduction_does_not_consume_normal_turnover_budget(self):
        result = cap_normal_rebalance_turnover(
            {"forced": 20.0, "normal-buy": 0.0},
            {"forced": 5.0, "normal-buy": 20.0},
            exemptions_by_key={"forced": "forced_risk_reduction"},
        )

        self.assertEqual(result.raw_turnover, 35.0)
        self.assertEqual(result.exempt_turnover, 15.0)
        self.assertEqual(result.applied_normal_turnover, 15.0)
        self.assertEqual(result.applied_turnover, 30.0)
        self.assertEqual(result.targets, {"forced": 5.0, "normal-buy": 15.0})
        self.assertTrue(result.capped)

    def test_full_exit_and_explicit_invalidation_are_applied_in_full(self):
        result = cap_normal_rebalance_turnover(
            {"exit": 12.0, "invalidated": 8.0, "normal": 0.0},
            {"exit": 0.0, "invalidated": 2.0, "normal": 20.0},
            exemptions_by_key={
                "exit": "full_exit",
                "invalidated": "thesis_invalidation",
            },
        )

        self.assertEqual(result.targets, {"invalidated": 2.0, "normal": 15.0})
        self.assertEqual(result.exempt_turnover_by_reason, {
            "full_exit": 12.0,
            "thesis_invalidation": 6.0,
        })
        self.assertEqual(result.applied_turnover, 33.0)

    def test_initial_passive_buy_exempts_only_its_required_funding_sell(self):
        result = cap_normal_rebalance_turnover(
            {"active-a": 20.0, "active-b": 20.0, "passive": 0.0, "new": 0.0},
            {"active-a": 10.0, "active-b": 15.0, "passive": 10.0, "new": 20.0},
            exemptions_by_key={"passive": "passive_initialization"},
        )

        # The passive 10% buy and 10% of desired sells are paired.  The other
        # 5% sell plus 20% normal buy are scaled to the 15% normal budget.
        self.assertAlmostEqual(result.exempt_turnover, 20.0)
        self.assertAlmostEqual(result.raw_normal_turnover, 25.0)
        self.assertAlmostEqual(result.applied_normal_turnover, 15.0)
        self.assertEqual(result.targets, {
            "active-a": 10.0,
            "active-b": 17.0,
            "new": 12.0,
            "passive": 10.0,
        })
        self.assertAlmostEqual(sum(result.targets.values()), 49.0)

    def test_turnover_result_is_independent_of_mapping_order(self):
        forward = cap_normal_rebalance_turnover(
            {"a": 20.0, "b": 20.0},
            {"a": 10.0, "b": 10.0, "c": 20.0},
        )
        reverse = cap_normal_rebalance_turnover(
            {"b": 20.0, "a": 20.0},
            {"c": 20.0, "b": 10.0, "a": 10.0},
        )

        self.assertEqual(forward, reverse)

    def test_rebalance_requires_every_current_holding(self):
        state = parse_portfolio_state(state_payload())
        analysis = parse_analysis_decision({
            "analysis_date": "2026-06-01",
            "run_type": "rebalance",
            "insights": [],
            "portfolio_decisions": [],
            "watchlist": [],
        })

        with self.assertRaisesRegex(PortfolioPolicyBlocked, "every current holding"):
            validate_rebalance_coverage(state, analysis)

    def test_allocator_preserves_legacy_weight_and_allocates_only_remaining_capacity(self):
        legacy = state_payload()
        legacy["portfolio"][0]["proposed_weight"] = 70.0
        state = parse_portfolio_state(legacy)
        item = decision(name="Microsoft", code="MSFT", proposed_weight=5.0)
        item.update({
            "provenance_status": "legacy_unvalidated",
            "origin_signal_type": "LEGACY_UNVALIDATED",
            "origin_signal_ids": [],
            "linked_signal_ids": [],
            "thesis_id": "new-thesis",
            "quality_components": {key: 1.0 for key in (
                "explicitness", "causality", "catalyst", "confirmation", "invalidation", "recency"
            )},
            "issuer_id": "MSFT",
            "theme_ids": ["AI"],
            "country_code": "US",
        })
        payload = state.to_dict()
        payload["portfolio"].append(item)
        projected = parse_portfolio_state(payload)

        allocated, summary = allocate_projected_state(
            projected,
            volatility_by_key={},
            portfolio_volatility=None,
            max_portfolio_drawdown=0.0,
        )

        self.assertEqual(sum(row["proposed_weight"] for row in allocated.portfolio), 10.0)
        self.assertEqual(summary["legacy_reserved_weight"], 10.0)
        self.assertEqual(summary["legacy_raw_weight"], 75.0)

    def test_policy_seeds_two_passive_indexes_at_ten_percent_each(self):
        seeded = ensure_policy_positions(parse_portfolio_state(state_payload()), "2026-06-01")

        passive = [item for item in seeded.portfolio if item["origin_signal_type"] == "PASSIVE_INDEX"]
        self.assertEqual({item["code"] for item in passive}, {"069500", "360750"})
        self.assertEqual(sum(item["proposed_weight"] for item in passive), 0.0)

    def test_allocator_uses_pretrade_actual_weights_for_turnover_summary(self):
        payload = state_payload()
        payload["portfolio"] = []
        seeded = ensure_policy_positions(parse_portfolio_state(payload), "2026-06-01")
        current = {security_key(item): 30.0 for item in seeded.portfolio}

        allocated, summary = allocate_projected_state(
            seeded,
            volatility_by_key={},
            portfolio_volatility=None,
            max_portfolio_drawdown=0.0,
            current_weights_by_key=current,
            as_of_date="2026-06-01",
        )

        self.assertTrue(summary["turnover_capped"])
        self.assertEqual(summary["raw_turnover"], 40.0)
        self.assertEqual(summary["applied_turnover"], 15.0)
        self.assertEqual(summary["turnover_cap"], 15.0)
        self.assertEqual(
            {item["code"]: item["proposed_weight"] for item in allocated.portfolio},
            {"069500": 22.5, "360750": 22.5},
        )
        self.assertTrue(all(item["policy_action"] == "비중축소" for item in allocated.portfolio))

    def test_critical_drawdown_blocks_genuinely_new_ai_position(self):
        base_payload = state_payload()
        for field in ("portfolio", "watchlist", "closed_positions", "decision_history", "insights"):
            base_payload[field] = []
        base = parse_portfolio_state(base_payload)
        posts = [{
            "post_id": "123",
            "title": "알루미늄 공급",
            "url": "https://blog.naver.com/ranto28/123",
            "date": "2026-06-01",
            "signal_candidates": [{
                "exact_text": "알루미늄 공급 부족이 이어질 수 있다.",
                "classification": "DIRECTIONAL_THESIS",
                "entity_name": "알루미늄",
                "entity_type": "sector",
                "direction": "수혜",
                "horizon_kind": "cyclical",
                "catalysts": ["공급 부족"],
                "invalidation_conditions": ["공급 정상화"],
                "thesis_summary": "공급 부족",
            }],
        }]
        prepared, events = prepare_post_signal_events(
            posts, created_at="2026-06-01", model_id="summary-model"
        )
        item = decision(
            name="알루미늄 ETF",
            code="ALUM",
            asset_type="etf",
            source_mentioned=False,
            source_scope="sector_only",
            basis="섹터 분석",
            previous_weight=None,
        )
        item["linked_signal_ids"] = [events[0]["signal_id"]]
        item.update({
            "quality_components": {key: 1.0 for key in (
                "explicitness", "causality", "catalyst", "confirmation", "invalidation", "recency"
            )},
            "issuer_id": "ALUM-ETF",
            "theme_ids": ["ALUMINUM"],
            "country_code": "US",
        })
        analysis = parse_analysis_decision({
            "analysis_date": "2026-06-01",
            "run_type": "regular",
            "insights": [insight()],
            "portfolio_decisions": [item],
            "watchlist": [],
        })
        enriched, all_events = enrich_decision_provenance(
            analysis, events, created_at="2026-06-01", model_id="decision-model"
        )
        projected = apply_analysis_decision(
            base, enriched, new_signal_events=all_events
        )

        allocated, _ = allocate_projected_state(
            projected,
            volatility_by_key={"etf:US:ALUM": 0.20},
            portfolio_volatility=None,
            max_portfolio_drawdown=-0.15,
            risk_scale_override=0.25,
            current_weights_by_key={},
            as_of_date="2026-06-01",
        )

        self.assertEqual(allocated.portfolio, [])
        self.assertEqual(allocated.closed_positions, [])

    def test_passive_first_funding_is_reported_as_turnover_exempt(self):
        payload = state_payload()
        payload["portfolio"] = []
        seeded = ensure_policy_positions(parse_portfolio_state(payload), "2026-06-01")

        allocated, summary = allocate_projected_state(
            seeded,
            volatility_by_key={},
            portfolio_volatility=None,
            max_portfolio_drawdown=0.0,
            current_weights_by_key={},
            as_of_date="2026-06-01",
        )

        self.assertFalse(summary["turnover_capped"])
        self.assertEqual(summary["raw_turnover"], 20.0)
        self.assertEqual(summary["applied_turnover"], 20.0)
        self.assertEqual(
            summary["turnover_exempt_by_reason"],
            {"passive_initialization": 20.0},
        )
        self.assertEqual(sum(item["proposed_weight"] for item in allocated.portfolio), 20.0)

    def test_policy_action_matches_the_fifty_basis_point_execution_band(self):
        payload = state_payload()
        payload["portfolio"] = []
        seeded = ensure_policy_positions(parse_portfolio_state(payload), "2026-06-01")
        current = {security_key(item): 10.1 for item in seeded.portfolio}

        allocated, _ = allocate_projected_state(
            seeded,
            volatility_by_key={},
            portfolio_volatility=None,
            max_portfolio_drawdown=0.0,
            current_weights_by_key=current,
            as_of_date="2026-06-01",
        )

        self.assertTrue(all(item["policy_action"] == "보유" for item in allocated.portfolio))

    def test_security_key_normalizes_korean_codes_and_known_legacy_typo(self):
        self.assertEqual(
            security_key({
                "name": "대한전선",
                "code": "1440",
                "market": "KR",
                "asset_type": "stock",
            }),
            "stock:KR:001440",
        )
        self.assertEqual(
            security_key({
                "name": "대한전선",
                "code": "011440",
                "market": "KR",
                "asset_type": "stock",
            }),
            "stock:KR:001440",
        )

    def test_risk_state_derisks_immediately_and_recovers_after_twenty_days(self):
        ledger = {}
        self.assertEqual(
            update_ledger_risk_state(ledger, -0.16, as_of_date="2026-06-01"),
            0.25,
        )
        for day in range(2, 21):
            scale = update_ledger_risk_state(
                ledger,
                -0.05,
                as_of_date=f"2026-06-{day:02d}",
            )
        self.assertEqual(scale, 0.25)
        self.assertEqual(
            update_ledger_risk_state(ledger, -0.05, as_of_date="2026-06-21"),
            0.50,
        )


if __name__ == "__main__":
    unittest.main()
