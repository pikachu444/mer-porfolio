import unittest

from portfolio_output import build_output_model, build_markdown_report, public_status_note
from telegram_notify import build_structured_summary, split_telegram_message


def _item(name="삼성전자", code="005930", target=1.5, actual=1.5, **extra):
    return {
        "name": name,
        "code": code,
        "market": extra.pop("market", "KR"),
        "asset_type": extra.pop("asset_type", "stock"),
        "decision_actor": "AI",
        "action": extra.pop("action", "매수"),
        "basis": "종목 분석",
        "decision_date": "2026-07-15",
        "evidence_posts": [],
        "source_mentioned": True,
        "proposed_weight": target,
        "change_reason": "과거 action은 오늘 상태가 아님",
        "investment_rationale": "핵심 사업의 장기 경쟁력을 추적",
        "allocation_role": "satellite",
        "_actual_for_test": actual,
        **({"provenance_status": "verified", "origin_signal_type": "MER_THESIS", "origin_signal_ids": ["sig-1"]} if extra.pop("verified", True) else {"provenance_status": "legacy_unvalidated", "origin_signal_type": "LEGACY_UNVALIDATED", "origin_signal_ids": []}),
        **extra,
    }


def _state(items):
    return {
        "portfolio": items,
        "watchlist": [],
        "watchlist_archive": [],
        "closed_positions": [],
        "decision_history": [],
        "insights": [],
        "signal_events": [],
        "last_watchlist_changes": {"date": None, "added": [], "updated": [], "promoted": [], "rejected": [], "expired": [], "archived": []},
    }


def _performance(items):
    return {
        "portfolio_return_krw": 1.2,
        "inception_date": "2026-07-14",
        "actual_cash_weight": 97.0 - sum(float(x.get("actual_weight") or 0) for x in items),
        "active_positions": [
            {"key": f"{x['asset_type']}:{x['market']}:{x['code']}", "code": x["code"], "actual_weight": x.get("_actual_for_test"), "return_pct_krw": 2.0, "current_price": 100.0}
            for x in items if x.get("_actual_for_test") is not None
        ],
        "risk_metrics": {"max_drawdown": -0.01, "excess_return": 0.002},
        "cumulative_costs": 0.1,
    }


class UserOutputPolicyTest(unittest.TestCase):
    def test_historical_action_is_not_today_signal(self):
        item = _item(action="매수", target=1.5, actual=1.5)
        output = build_output_model(_state([item]), _performance([item]), today_str="2026-07-15")
        self.assertEqual(output["portfolio"][0]["today_action"], "유지")
        self.assertEqual(output["today_changes"], [])
        self.assertNotIn("매수", build_structured_summary(_state([item]), "2026-07-15", _performance([item])))

    def test_only_target_drift_is_today_change(self):
        first = _item(target=2.0, actual=1.0)
        second = _item(name="LS", code="006220", target=0.6, actual=0.6)
        output = build_output_model(_state([first, second]), _performance([first, second]), today_str="2026-07-15")
        self.assertEqual([x["name"] for x in output["today_changes"]], ["삼성전자"])
        self.assertEqual(output["today_changes"][0]["today_action"], "비중확대 검토")

    def test_missing_actual_is_not_target_disguised(self):
        item = _item(actual=None, target=10.0)
        output = build_output_model(_state([item]), _performance([item]), today_str="2026-07-15")
        self.assertIsNone(output["portfolio"][0]["actual_weight"])
        self.assertEqual(output["portfolio"][0]["today_action"], "데이터 없음")
        summary = build_structured_summary(_state([item]), "2026-07-15", _performance([item]))
        self.assertIn("데이터 없음 → 목표 10.00%", summary)

    def test_legacy_is_not_user_output(self):
        item = _item(name="레거시", code="000001", verified=False)
        output = build_output_model(_state([item]), _performance([]), today_str="2026-07-15")
        self.assertEqual(output["portfolio"], [])
        self.assertNotIn("재검증", build_markdown_report(output))

    def test_duplicate_watchlist_is_merged(self):
        state = _state([])
        state["watchlist"] = [
            {"name": "HLB", "code": "028300", "market": "KR", "latest_material_signal_date": "2026-07-12", "thesis_id": "a"},
            {"name": "HLB", "code": "028300", "market": "KR", "latest_material_signal_date": "2026-07-11", "thesis_id": "b"},
        ]
        output = build_output_model(state, {}, today_str="2026-07-15")
        self.assertEqual(len(output["watchlist"]), 1)

    def test_internal_note_is_sanitized(self):
        note = "포트폴리오 안전 검증으로 변경 보류: decisions[1] links signals incompatible"
        self.assertNotIn("decisions[", public_status_note(note))
        self.assertIn("기존 포트폴리오를 유지", public_status_note(note))

    def test_message_keeps_all_holdings_and_two_decimal_weights(self):
        items = [_item(name=f"종목{i}", code=f"{i:06d}", target=1.0, actual=1.0) for i in range(1, 21)]
        summary = build_structured_summary(_state(items), "2026-07-15", _performance(items))
        self.assertEqual(sum(f"종목{i}" in summary for i in range(1, 21)), 20)
        self.assertIn("1.00%", summary)
        self.assertNotIn("외 20건", summary)

    def test_message_split_does_not_drop_holdings(self):
        text = "상단\n" + "\n".join(f"• 종목{i} 1.00%" for i in range(50)) + "\n" + ("긴 인사이트 " * 500)
        parts = split_telegram_message(text, max_length=400)
        joined = "\n".join(parts)
        self.assertIn("종목49", joined)


if __name__ == "__main__":
    unittest.main()
