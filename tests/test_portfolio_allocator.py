import unittest

from portfolio_allocator import (
    AllocationPolicy,
    AI_INFERRED,
    CASH,
    MER_DIRECT,
    PASSIVE_INDEX,
    allocate_portfolio,
    compute_signal_quality,
    drawdown_risk_scale,
    portfolio_risk_scale,
    sleeve_for_origin,
)


QUALITY_KEYS = (
    "explicitness",
    "causality",
    "catalyst",
    "confirmation",
    "invalidation",
    "recency",
)


def candidate(
    key,
    origin,
    *,
    country="KR",
    issuer=None,
    themes=None,
    quality=1.0,
    volatility=0.20,
    fixed_weight=None,
    decision_actor="AI",
):
    item = {
        "key": key,
        "origin": origin,
        "country_code": country,
        "decision_actor": decision_actor,
    }
    if origin == "PASSIVE_INDEX":
        item["fixed_weight"] = 1.0 if fixed_weight is None else fixed_weight
        return item
    item.update({
        "issuer_id": issuer or key,
        "theme_ids": themes or [f"theme-{key}"],
        "quality_components": {name: quality for name in QUALITY_KEYS},
        "annualized_volatility": volatility,
    })
    return item


def fully_investable_candidates():
    items = [
        candidate("passive-kr", "PASSIVE_INDEX", country="KR", fixed_weight=10),
        candidate("passive-us", "PASSIVE_INDEX", country="US", fixed_weight=10),
    ]
    items.extend(
        candidate(f"mer-{index:02d}", "MER_DIRECT", country="KR" if index < 4 else "US")
        for index in range(8)
    )
    items.extend(
        candidate(f"ai-{index:02d}", "AI_INFERRED", country="KR" if index < 5 else "US")
        for index in range(10)
    )
    return items


class PortfolioAllocatorTest(unittest.TestCase):
    def test_quality_formula_uses_agreed_weights(self):
        score = compute_signal_quality({
            "explicitness": 1.0,
            "causality": 0.5,
            "catalyst": 0.0,
            "confirmation": 0.0,
            "invalidation": 0.0,
            "recency": 0.0,
        })

        self.assertAlmostEqual(score, 0.40)
        with self.assertRaisesRegex(ValueError, "missing quality components"):
            compute_signal_quality({"explicitness": 1.0})

    def test_origin_not_latest_actor_selects_sleeve(self):
        self.assertEqual(sleeve_for_origin("MER_DIRECT"), MER_DIRECT)
        self.assertEqual(sleeve_for_origin("AI_INFERRED"), AI_INFERRED)

        result = allocate_portfolio([
            candidate(
                "ai-reviewed-mer",
                "MER_DIRECT",
                decision_actor="AI",
            )
        ])

        self.assertEqual(result.targets["ai-reviewed-mer"], 5.0)
        self.assertEqual(result.sleeve_weights[MER_DIRECT], 5.0)

        schema_named = candidate("schema-named-origin", "MER_DIRECT")
        schema_named["origin_signal_type"] = schema_named.pop("origin")
        schema_result = allocate_portfolio([schema_named])
        self.assertEqual(schema_result.targets["schema-named-origin"], 5.0)

    def test_full_candidate_set_produces_twenty_forty_twenty_twenty(self):
        result = allocate_portfolio(fully_investable_candidates())

        self.assertEqual(result.sleeve_weights[PASSIVE_INDEX], 20.0)
        self.assertEqual(result.sleeve_weights[MER_DIRECT], 40.0)
        self.assertEqual(result.sleeve_weights[AI_INFERRED], 20.0)
        self.assertEqual(result.sleeve_weights[CASH], 20.0)
        self.assertLessEqual(
            max(result.targets[key] for key in result.targets if key.startswith("mer-")),
            5.0,
        )
        self.assertLessEqual(
            max(result.targets[key] for key in result.targets if key.startswith("ai-")),
            2.0,
        )

    def test_insufficient_eligible_signals_remain_cash(self):
        result = allocate_portfolio([
            candidate("passive-kr", "PASSIVE_INDEX", fixed_weight=10),
            candidate("passive-us", "PASSIVE_INDEX", country="US", fixed_weight=10),
            candidate("one-mer", "MER_DIRECT"),
            candidate("weak-ai", "AI_INFERRED", quality=0.69),
        ])

        self.assertEqual(result.targets["one-mer"], 5.0)
        self.assertNotIn("weak-ai", result.targets)
        self.assertEqual(result.cash_weight, 75.0)
        self.assertIn("weak-ai", result.rejected)

    def test_issuer_and_theme_caps_apply_across_active_candidates(self):
        items = fully_investable_candidates()
        for item in items:
            if item["key"] in {"mer-00", "mer-01"}:
                item["issuer_id"] = "shared-issuer"
            if item["key"] in {"mer-02", "mer-03", "mer-04", "mer-05"}:
                item["theme_ids"] = ["shared-theme"]

        result = allocate_portfolio(items)

        issuer_weight = result.targets.get("mer-00", 0) + result.targets.get("mer-01", 0)
        theme_weight = sum(result.targets.get(f"mer-{index:02d}", 0) for index in range(2, 6))
        self.assertLessEqual(issuer_weight, 5.0 + 1e-8)
        self.assertLessEqual(theme_weight, 15.0 + 1e-8)

    def test_country_cap_includes_passive_and_unfilled_weight_becomes_cash(self):
        items = [
            candidate("passive-kr", "PASSIVE_INDEX", country="KR", fixed_weight=20),
        ]
        items.extend(
            candidate(f"mer-{index:02d}", "MER_DIRECT", country="KR")
            for index in range(8)
        )
        items.extend(
            candidate(f"ai-{index:02d}", "AI_INFERRED", country="KR")
            for index in range(10)
        )

        result = allocate_portfolio(items)

        self.assertAlmostEqual(sum(result.targets.values()), 55.0)
        self.assertAlmostEqual(result.cash_weight, 45.0)

    def test_volatility_and_drawdown_scale_only_active_sleeves(self):
        items = fully_investable_candidates()

        result = allocate_portfolio(
            items,
            portfolio_volatility=0.24,
            max_drawdown=0.125,
        )

        self.assertEqual(result.risk_scale, 0.5)
        self.assertEqual(result.sleeve_weights[PASSIVE_INDEX], 20.0)
        self.assertEqual(result.sleeve_weights[MER_DIRECT], 20.0)
        self.assertEqual(result.sleeve_weights[AI_INFERRED], 10.0)
        self.assertEqual(result.cash_weight, 50.0)
        self.assertEqual(portfolio_risk_scale(0.48, 0.0), 0.25)
        self.assertEqual(drawdown_risk_scale(0.10), 0.75)
        self.assertEqual(drawdown_risk_scale(0.125), 0.50)
        self.assertEqual(drawdown_risk_scale(0.15), 0.25)

    def test_result_is_independent_of_candidate_order(self):
        items = fully_investable_candidates()

        forward = allocate_portfolio(items)
        reverse = allocate_portfolio(reversed(items))

        self.assertEqual(forward.targets, reverse.targets)
        self.assertEqual(forward.sleeve_weights, reverse.sleeve_weights)
        self.assertEqual(list(forward.targets), sorted(forward.targets))

    def test_missing_cap_metadata_fails_closed(self):
        item = candidate("mer", "MER_DIRECT")
        item.pop("issuer_id")

        result = allocate_portfolio([item])

        self.assertEqual(result.targets, {})
        self.assertEqual(result.cash_weight, 100.0)
        self.assertEqual(result.rejected["mer"], "missing issuer_id")

    def test_turnover_cap_policy_must_be_non_negative(self):
        self.assertEqual(AllocationPolicy().normal_rebalance_turnover_cap, 15.0)
        with self.assertRaisesRegex(ValueError, "normal_rebalance_turnover_cap"):
            AllocationPolicy(normal_rebalance_turnover_cap=-0.01)


if __name__ == "__main__":
    unittest.main()
