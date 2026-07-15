"""Move unvalidated holdings out of the approved model portfolio.

The migration is intentionally conservative: a legacy row is not promoted just
because it has an old narrative. It is placed in the internal administrator
queue and closed from the approved model ledger at the latest recorded price.
That keeps the subscriber-facing portfolio honest while preserving an audit
trail for a later human evidence review.
"""

from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from portfolio_schema import load_portfolio_state_file, parse_portfolio_state, save_portfolio_state_file
from track_returns import (
    apply_structured_transactions,
    get_structured_prices,
    load_model_ledger,
    refresh_structured_performance,
    sanitize_model_ledger_for_state,
)


OUTPUT_DIR = Path("output")
STATE_PATH = OUTPUT_DIR / "portfolio_state.json"
LEDGER_PATH = OUTPUT_DIR / "model_portfolio_ledger.json"
CACHE_PATH = OUTPUT_DIR / "performance_cache.json"


def _key(item: dict[str, Any]) -> str:
    market = str(item.get("market") or "").upper()
    asset_type = str(item.get("asset_type") or "stock").lower()
    code = str(item.get("code") or "").upper()
    return f"{asset_type}:{market}:{code or str(item.get('name') or '').strip().lower()}"


def _identity(item: dict[str, Any]) -> tuple[str, str]:
    return str(item.get("market") or "").upper(), str(item.get("code") or "").upper()


def _queue_reason(item: dict[str, Any]) -> str:
    linked = item.get("linked_signal_ids") or []
    if linked:
        return "현재 연결된 원문 신호가 승인 포지션의 유지·확대를 충분히 뒷받침하는지 관리자 확인이 필요합니다."
    return "승인된 원문 신호 연결이 없어 관리자 확인 전까지 승인 포트폴리오에서 제외합니다."


def _merge_watchlist(state: dict[str, Any]) -> None:
    """Merge active duplicate securities while retaining the newest thesis."""
    active = state.get("watchlist", []) or []
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    replacements: dict[str, str] = {}
    for item in active:
        identity = _identity(item)
        if identity not in merged:
            merged[identity] = item
            continue
        current = merged[identity]
        current_date = str(current.get("latest_material_signal_date") or "")
        item_date = str(item.get("latest_material_signal_date") or "")
        if item_date > current_date:
            preferred, other = item, current
            merged[identity] = preferred
        else:
            preferred, other = current, item
        replacements[str(other.get("thesis_id") or "")] = str(preferred.get("thesis_id") or "")
        preferred["linked_signal_ids"] = list(dict.fromkeys([
            *(preferred.get("linked_signal_ids") or []),
            *(other.get("linked_signal_ids") or []),
        ]))
        preferred["merged_thesis_ids"] = list(dict.fromkeys([
            *(preferred.get("merged_thesis_ids") or []),
            str(other.get("thesis_id") or ""),
        ]))
        if not preferred.get("observation_reason"):
            preferred["observation_reason"] = other.get("observation_reason")
    state["watchlist"] = list(merged.values())
    changes = state.get("last_watchlist_changes", {}) or {}
    for category, values in changes.items():
        if not isinstance(values, list):
            continue
        changes[category] = list(dict.fromkeys(replacements.get(str(value), str(value)) for value in values))
    state["last_watchlist_changes"] = changes


def migrate_payload(
    state_payload: dict[str, Any],
    ledger: dict[str, Any],
    cache: dict[str, Any],
    *,
    migration_date: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    state = deepcopy(state_payload)
    state.setdefault("admin_review_queue", [])
    existing_queue = {
        str(item.get("queue_id")): item
        for item in state["admin_review_queue"]
        if isinstance(item, dict)
    }
    legacy = [
        item for item in state.get("portfolio", []) or []
        if str(item.get("provenance_status") or "") != "verified"
    ]
    if not legacy:
        _merge_watchlist(state)
        return state, ledger, cache, []

    remaining = [
        item for item in state.get("portfolio", []) or []
        if str(item.get("provenance_status") or "") == "verified"
    ]
    state["portfolio"] = remaining
    state.setdefault("closed_positions", [])
    closed_keys = {
        (_identity(item), str(item.get("closed_date") or ""))
        for item in state["closed_positions"]
    }

    price_by_key: dict[str, float] = {}
    for row in cache.get("active_positions", []) or []:
        key = str(row.get("key") or _key(row))
        try:
            price_by_key[key] = float(row["current_price"])
        except (KeyError, TypeError, ValueError):
            continue
    missing = [item for item in legacy if _key(item) not in price_by_key]
    if missing:
        price_by_key.update(get_structured_prices(missing))

    decisions: list[dict[str, Any]] = []
    migrated_queue: list[dict[str, Any]] = []
    for item in legacy:
        key = _key(item)
        queue_id = f"admin-{migration_date}-{key}"
        queue_item = existing_queue.get(queue_id) or {
            "queue_id": queue_id,
            "name": item.get("name", ""),
            "code": item.get("code", ""),
            "market": item.get("market", ""),
            "asset_type": item.get("asset_type", "stock"),
            "queue_status": "pending_admin",
            "queued_date": migration_date,
            "reason": _queue_reason(item),
            "recommended_action": "편출 검토",
            "original_target_weight": float(item.get("proposed_weight") or 0.0),
            "linked_signal_ids": list(item.get("linked_signal_ids") or []),
            "origin_signal_type": item.get("origin_signal_type"),
            "provenance_status": item.get("provenance_status"),
            "administrative_exit": True,
        }
        existing_queue[queue_id] = queue_item
        migrated_queue.append(queue_item)
        decision = deepcopy(item)
        decision.update({
            "action": "매도",
            "previous_weight": float(item.get("proposed_weight") or 0.0),
            "proposed_weight": 0.0,
            "decision_actor": "AI",
            "change_reason": "승인 근거 확인 전 모델 포트폴리오에서 행정적으로 편출",
        })
        decisions.append(decision)
        close_key = (_identity(item), migration_date)
        if close_key not in closed_keys:
            closed = deepcopy(item)
            closed.update({
                "action": "매도",
                "previous_weight": float(item.get("proposed_weight") or 0.0),
                "proposed_weight": 0.0,
                "decision_actor": "AI",
                "closed_date": migration_date,
                "close_reason": "승인 근거 확인 전 모델 포트폴리오에서 행정적으로 편출",
                "closed_performance": None,
                "administrative_exit": True,
            })
            state["closed_positions"].append(closed)
            closed_keys.add(close_key)

    migrated_ledger = apply_structured_transactions(
        ledger,
        decisions,
        price_by_key,
        migration_date,
        cost_bps_by_market={"KR": 0.0, "US": 0.0},
    )
    migrated_ledger = sanitize_model_ledger_for_state(migrated_ledger, state)
    for transaction in migrated_ledger.get("transactions", [])[-len(decisions):]:
        transaction["administrative_exit"] = True
        transaction["reason"] = "승인 근거 확인 전 모델 포트폴리오에서 행정적으로 편출"
    for closed in migrated_ledger.get("closed_positions", [])[-len(decisions):]:
        closed["administrative_exit"] = True
        closed["close_reason"] = "승인 근거 확인 전 모델 포트폴리오에서 행정적으로 편출"

    refresh_items = migrated_ledger.get("positions", []) or []
    refresh_prices = {key: value for key, value in price_by_key.items() if any(
        str(position.get("key")) == key for position in refresh_items
    )}
    migrated_cache = refresh_structured_performance(
        migrated_ledger,
        refresh_prices,
        migration_date,
        persist=False,
        fetch_benchmark=False,
    ) if refresh_items else {
        "updated": migration_date,
        "epoch_id": migrated_ledger.get("epoch_id"),
        "inception_date": migrated_ledger.get("inception_date"),
        "portfolio_return_krw": 0.0,
        "cash": migrated_ledger.get("cash", 100.0),
        "actual_cash_weight": 100.0,
        "active_positions": [],
        "closed_positions": migrated_ledger.get("closed_positions", []),
        "report_summaries": [],
        "risk_metrics": {},
        "benchmark": {"status": "insufficient_history", "period_returns": []},
        "cumulative_costs": migrated_ledger.get("cumulative_costs", 0.0),
    }
    state["admin_review_queue"] = list(existing_queue.values())
    _merge_watchlist(state)
    return state, migrated_ledger, migrated_cache, migrated_queue


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--state", type=Path, default=STATE_PATH)
    parser.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    parser.add_argument("--cache", type=Path, default=CACHE_PATH)
    args = parser.parse_args()

    state = load_portfolio_state_file(args.state).to_dict()
    ledger = json.loads(args.ledger.read_text(encoding="utf-8"))
    cache = json.loads(args.cache.read_text(encoding="utf-8")) if args.cache.exists() else {}
    migrated_state, migrated_ledger, migrated_cache, queue = migrate_payload(
        state, ledger, cache, migration_date=args.date
    )
    print(f"legacy positions moved to admin queue: {len(queue)}")
    if not args.apply:
        return 0
    save_portfolio_state_file(parse_portfolio_state(migrated_state), args.state)
    args.ledger.write_text(json.dumps(migrated_ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.cache.write_text(json.dumps(migrated_cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"updated {args.state}, {args.ledger}, {args.cache}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
