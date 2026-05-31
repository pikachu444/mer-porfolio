import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import portfolio_state
import track_returns


REPORT_ONE = """
## 포트폴리오 추천

### 🇰🇷 국내주식 (한국)

| 종목명 | 코드 | 판단 | 목표비중 | 근거유형 | 핵심 근거 |
|--------|------|------|----------|----------|-----------|
| LS | 006220 | 보유 | 8% | 직접언급 | 전력 인프라 |

### 🇺🇸 해외주식 (미국)

| 종목명 | 티커 | 판단 | 목표비중 | 근거유형 | 핵심 근거 |
|--------|------|------|----------|----------|-----------|
| NVIDIA | NVDA | Buy | 15% | 직접언급 | AI |
"""


REPORT_TWO = """
## 포트폴리오 추천

### 🇰🇷 국내주식 (한국)

| 종목명 | 코드 | 판단 | 목표비중 | 근거유형 | 핵심 근거 |
|--------|------|------|----------|----------|-----------|
| LS | 006220 | 보유 | 10% | 직접언급 | 전력 인프라 |

### 🇺🇸 해외주식 (미국)

| 종목명 | 티커 | 판단 | 목표비중 | 근거유형 | 핵심 근거 |
|--------|------|------|----------|----------|-----------|
| NVIDIA | NVDA | Buy | 15% | 직접언급 | AI |
"""


class PortfolioConsistencyTest(unittest.TestCase):
    def test_missing_existing_holding_is_kept_in_synced_report(self):
        state = {
            "schema_version": "1.0",
            "holdings": [
                {
                    "name": "LS",
                    "code": "006220",
                    "market": "KR",
                    "action": "보유",
                    "weight": "8%",
                    "basis_type": "직접언급",
                    "thesis": "전력 인프라",
                    "entry_date": "2026-05-01",
                    "status": "active",
                },
                {
                    "name": "대한전선",
                    "code": "001440",
                    "market": "KR",
                    "action": "보유",
                    "weight": "8%",
                    "basis_type": "기존보유",
                    "thesis": "기존 보유 유지",
                    "entry_date": "2026-05-01",
                    "status": "active",
                },
            ],
        }
        parsed = [{"name": "LS", "code": "006220", "market": "KR", "action": "보유", "weight": "8%"}]

        updated = portfolio_state.update_state_from_report(state, REPORT_ONE, "2026-05-29", parsed_portfolio=parsed)
        synced = portfolio_state.sync_report_with_state(REPORT_ONE, updated)

        self.assertIn("| 대한전선 | 001440 | 보유 | 8% | 기존보유 | 기존 보유 유지 |", synced)

    def test_repeated_recommendation_updates_single_position(self):
        with TemporaryDirectory() as tmp:
            track_returns.HISTORY_FILE = Path(tmp) / "portfolio_history.json"
            track_returns.CACHE_FILE = Path(tmp) / "performance_cache.json"
            with patch.object(track_returns, "YFINANCE_AVAILABLE", True), \
                 patch.object(track_returns, "_get_usdkrw", return_value=1400.0), \
                 patch.object(track_returns, "_resolve_kr_ticker", return_value="006220.KS"), \
                 patch.object(track_returns, "_get_price_on_date", return_value=100.0), \
                 patch.object(track_returns, "_get_current_price", return_value=110.0):
                track_returns.update_and_get_performance(REPORT_ONE, datetime(2026, 5, 28))
                track_returns.update_and_get_performance(REPORT_TWO, datetime(2026, 5, 29))

            history = track_returns._load_history()
            active = [p for p in history["positions"] if p.get("status") == "active"]
            ls_positions = [p for p in active if p["key"] == "KR:006220"]
            self.assertEqual(len(ls_positions), 1)
            self.assertEqual(ls_positions[0]["entry_date"], "2026-05-28")
            self.assertEqual(ls_positions[0]["weight"], "10%")

    def test_consistency_rejects_extra_active_row(self):
        current = [{"type": "KR", "market": "KR", "name": "LS", "code": "006220"}]
        rows = [
            {"type": "KR", "market": "KR", "name": "LS", "code": "006220"},
            {"type": "KR", "market": "KR", "name": "삼성전자", "code": "005930"},
        ]

        with self.assertRaises(ValueError):
            track_returns._assert_consistent(current, rows)


if __name__ == "__main__":
    unittest.main()
