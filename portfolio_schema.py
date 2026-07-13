"""
Structured portfolio state schema.

The v2 schema is intentionally separate from the legacy Markdown-derived
portfolio_state.py flow. Migration and runtime adoption happen in later tasks.
"""

from __future__ import annotations

import json
import hashlib
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


SCHEMA_VERSION = "2.1"
PREVIOUS_SCHEMA_VERSION = "2.0"

DECISION_ACTORS = {"메르", "AI"}
STATE_DECISION_ACTORS = DECISION_ACTORS | {"미분류"}
ACTIONS = {"매수", "보유", "비중확대", "비중축소", "매도"}
BASES = {"직접 발언", "종목 분석", "섹터 분석", "이전 판단 유지"}
ASSET_TYPES = {"stock", "etf", "sector", "cash"}
WEIGHT_SOURCES = {"메르 직접 발언 기반", "AI 제안"}
WATCHLIST_STATUSES = {"관심", "재검토 필요", "포트폴리오 편입", "종료"}
WATCHLIST_LIFECYCLE_STATUSES = {
    "candidate",
    "active",
    "promoted",
    "rejected",
    "expired",
    "archived",
}
ACTIVE_WATCHLIST_STATUSES = {"candidate", "active"}
TERMINAL_WATCHLIST_STATUSES = WATCHLIST_LIFECYCLE_STATUSES - ACTIVE_WATCHLIST_STATUSES
WATCHLIST_KINDS = {"mention", "event", "cyclical", "structural"}
WATCHLIST_TTL_BUSINESS_DAYS = {
    "mention": 10,
    "event": 20,
    "cyclical": 60,
    "structural": 120,
}
PROVENANCE_STATUSES = {"verified", "legacy_unvalidated"}
ORIGIN_SIGNAL_TYPES = {
    "MER_DIRECT",
    "MER_THESIS",
    "AI_INFERRED",
    "PASSIVE_INDEX",
    "LEGACY_UNVALIDATED",
}
SIGNAL_TYPES = {"MER_DIRECT", "MER_THESIS", "AI_INFERRED", "MENTION_ONLY"}
SIGNAL_DIRECTIONS = {"bullish", "bearish", "neutral"}
RUN_TYPES = {"regular", "rebalance"}
ALLOCATION_ROLES = {"core", "satellite", "risk", "defensive", "watch"}
DEFENSIVE_CASH_TARGET = 20.0
SOURCE_SCOPES = {
    "blogger_trade_disclosure",
    "source_named_security",
    "sector_only",
    "previous_decision",
}

KNOWN_LEGACY_CODE_CORRECTIONS = {
    ("KR", "대한전선", "011440"): "001440",
}


class PortfolioSchemaError(ValueError):
    """Raised when structured portfolio state does not satisfy the v2 contract."""


def normalize_security_code(name: Any, market: Any, code: Any) -> str:
    market_value = str(market or "").strip().upper()
    name_value = str(name or "").strip()
    code_value = str(code or "").strip().upper()
    if market_value == "KR":
        digits = re.sub(r"[^0-9]", "", code_value)
        if digits:
            code_value = digits.zfill(6)
    return KNOWN_LEGACY_CODE_CORRECTIONS.get(
        (market_value, name_value, code_value),
        code_value,
    )


@dataclass(frozen=True)
class PortfolioStateV2:
    portfolio: list[dict[str, Any]]
    watchlist: list[dict[str, Any]]
    watchlist_archive: list[dict[str, Any]]
    closed_positions: list[dict[str, Any]]
    decision_history: list[dict[str, Any]]
    insights: list[dict[str, Any]]
    signal_events: list[dict[str, Any]]
    last_watchlist_changes: dict[str, Any]
    last_rebalanced_date: str | None
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "portfolio": self.portfolio,
            "watchlist": self.watchlist,
            "watchlist_archive": self.watchlist_archive,
            "closed_positions": self.closed_positions,
            "decision_history": self.decision_history,
            "insights": self.insights,
            "signal_events": self.signal_events,
            "last_watchlist_changes": self.last_watchlist_changes,
            "last_rebalanced_date": self.last_rebalanced_date,
        }


@dataclass(frozen=True)
class AnalysisDecisionV2:
    analysis_date: str
    run_type: str
    insights: list[dict[str, Any]]
    portfolio_decisions: list[dict[str, Any]]
    watchlist: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "analysis_date": self.analysis_date,
            "run_type": self.run_type,
            "insights": self.insights,
            "portfolio_decisions": self.portfolio_decisions,
            "watchlist": self.watchlist,
        }


def parse_portfolio_state_json(text: str) -> PortfolioStateV2:
    """Parse a JSON string and validate the structured portfolio state."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PortfolioSchemaError(f"invalid JSON: {exc.msg}") from exc
    return parse_portfolio_state(payload)


def parse_analysis_decision_json(text: str) -> AnalysisDecisionV2:
    """Parse a Gemini first-call JSON response and validate its decisions."""
    try:
        payload = json.loads(_strip_json_fence(text))
    except json.JSONDecodeError as exc:
        raise PortfolioSchemaError(f"invalid JSON: {exc.msg}") from exc
    return parse_analysis_decision(payload)


def parse_analysis_decision(payload: Any) -> AnalysisDecisionV2:
    """Validate a decoded Gemini first-call JSON response."""
    root = _require_object(payload, "analysis")
    analysis_date = _require_date(root, "analysis_date", "analysis")
    run_type = _require_allowed(root, "run_type", RUN_TYPES, "analysis")
    insights = _require_object_list(root, "insights", "analysis")
    portfolio_decisions = _require_object_list(
        root,
        "portfolio_decisions",
        "analysis",
    )
    watchlist = _require_object_list(root, "watchlist", "analysis")
    portfolio_decisions = [
        _with_decision_provenance_defaults(item)
        for item in portfolio_decisions
    ]
    watchlist = [
        _with_watchlist_compatibility_defaults(item)
        for item in watchlist
    ]

    insight_ids = set()
    for index, item in enumerate(insights):
        insight_id = _validate_insight(item, f"analysis.insights[{index}]")
        if insight_id in insight_ids:
            raise PortfolioSchemaError(f"analysis.insights contains duplicate id: {insight_id}")
        insight_ids.add(insight_id)
    for index, item in enumerate(portfolio_decisions):
        _validate_decision(
            item,
            f"analysis.portfolio_decisions[{index}]",
            strict_candidate_details=True,
        )
        if _requires_linked_insight(item):
            linked = _require_string_list(item, "linked_insight_ids", f"analysis.portfolio_decisions[{index}]")
            if not linked:
                raise PortfolioSchemaError(
                    f"analysis.portfolio_decisions[{index}].linked_insight_ids must not be empty for a changed decision"
                )
            unknown = sorted(set(linked) - insight_ids)
            if unknown:
                raise PortfolioSchemaError(
                    f"analysis.portfolio_decisions[{index}].linked_insight_ids contains unknown ids: {', '.join(unknown)}"
                )
    for index, item in enumerate(watchlist):
        _validate_watchlist_item(item, f"analysis.watchlist[{index}]")

    return AnalysisDecisionV2(
        analysis_date=analysis_date,
        run_type=run_type,
        insights=insights,
        portfolio_decisions=portfolio_decisions,
        watchlist=watchlist,
    )


def parse_portfolio_state(payload: Any) -> PortfolioStateV2:
    """Validate current state, upgrading a decoded v2.0 payload in memory."""
    root = _require_object(payload, "state")
    schema_version = _require_string(root, "schema_version", "state")
    if schema_version == PREVIOUS_SCHEMA_VERSION:
        return upgrade_v2_state(root)
    if schema_version != SCHEMA_VERSION:
        raise PortfolioSchemaError(
            f"state.schema_version must be {SCHEMA_VERSION!r}, got {schema_version!r}"
        )

    root = _with_state_compatibility_defaults(root)

    portfolio = _require_object_list(root, "portfolio", "state")
    watchlist = _require_object_list(root, "watchlist", "state")
    watchlist_archive = _require_object_list(root, "watchlist_archive", "state")
    closed_positions = _require_object_list(root, "closed_positions", "state")
    decision_history = _require_object_list(root, "decision_history", "state")
    signal_events = _require_object_list(root, "signal_events", "state")
    last_watchlist_changes = _require_object(
        root.get("last_watchlist_changes"),
        "state.last_watchlist_changes",
    )
    insights = root.get("insights", [])
    if not isinstance(insights, list) or any(not isinstance(item, dict) for item in insights):
        raise PortfolioSchemaError("state.insights must be a list of objects")
    last_rebalanced_date = _require_optional_date(
        root,
        "last_rebalanced_date",
        "state",
    )

    signal_by_id = _validate_signal_event_ledger(signal_events, "state.signal_events")
    for index, item in enumerate(portfolio):
        _validate_portfolio_item(
            item,
            f"state.portfolio[{index}]",
            allow_unclassified=True,
        )
        _validate_provenance(item, f"state.portfolio[{index}]", signal_by_id)
    total_weight = sum(item["proposed_weight"] for item in portfolio)
    if total_weight > 100:
        raise PortfolioSchemaError(
            f"state.portfolio proposed_weight total must not exceed 100, got {total_weight}"
        )
    for index, item in enumerate(watchlist):
        _validate_watchlist_item(item, f"state.watchlist[{index}]")
        _validate_provenance(item, f"state.watchlist[{index}]", signal_by_id)
        if item["lifecycle_status"] not in ACTIVE_WATCHLIST_STATUSES:
            raise PortfolioSchemaError(
                f"state.watchlist[{index}].lifecycle_status must be candidate or active"
            )
    for index, item in enumerate(watchlist_archive):
        _validate_watchlist_item(item, f"state.watchlist_archive[{index}]")
        _validate_provenance(item, f"state.watchlist_archive[{index}]", signal_by_id)
        if item["lifecycle_status"] not in TERMINAL_WATCHLIST_STATUSES:
            raise PortfolioSchemaError(
                f"state.watchlist_archive[{index}].lifecycle_status must be terminal"
            )
    _validate_unique_watchlist_keys(watchlist, "state.watchlist")
    _validate_unique_watchlist_keys(watchlist_archive, "state.watchlist_archive")
    for index, item in enumerate(closed_positions):
        _validate_closed_position(item, f"state.closed_positions[{index}]")
        _validate_provenance(item, f"state.closed_positions[{index}]", signal_by_id)
    _validate_no_current_closed_overlap(portfolio, closed_positions, "state")
    _validate_no_watchlist_archive_overlap(watchlist, watchlist_archive, "state")
    for index, item in enumerate(decision_history):
        _validate_decision(item, f"state.decision_history[{index}]")
        _validate_provenance(item, f"state.decision_history[{index}]", signal_by_id)
    for index, item in enumerate(insights):
        _validate_insight(item, f"state.insights[{index}]")
    _validate_last_watchlist_changes(last_watchlist_changes, "state.last_watchlist_changes")

    return PortfolioStateV2(
        schema_version=schema_version,
        portfolio=portfolio,
        watchlist=watchlist,
        watchlist_archive=watchlist_archive,
        closed_positions=closed_positions,
        decision_history=decision_history,
        insights=insights,
        signal_events=signal_events,
        last_watchlist_changes=last_watchlist_changes,
        last_rebalanced_date=last_rebalanced_date,
    )


def upgrade_v2_state(payload: Any) -> PortfolioStateV2:
    """Conservatively upgrade a structured v2.0 state without guessing provenance."""
    root = deepcopy(_require_object(payload, "v2_state"))
    version = root.get("schema_version")
    if version not in {PREVIOUS_SCHEMA_VERSION, SCHEMA_VERSION}:
        raise PortfolioSchemaError(
            f"v2_state.schema_version must be {PREVIOUS_SCHEMA_VERSION!r} or {SCHEMA_VERSION!r}"
        )
    root["schema_version"] = SCHEMA_VERSION
    if version == PREVIOUS_SCHEMA_VERSION:
        # v2.0 did not prove that its recorded rebalance covered every active
        # position with validated source provenance.  Treat it as a migration
        # baseline, so the next eligible run requires one complete rebalance.
        root["last_rebalanced_date"] = None
    root.setdefault("signal_events", [])
    root.setdefault("watchlist_archive", [])
    root.setdefault("last_watchlist_changes", _empty_watchlist_changes())
    root.setdefault("portfolio", [])
    root.setdefault("watchlist", [])
    root.setdefault("closed_positions", [])
    root.setdefault("decision_history", [])
    root.setdefault("insights", [])
    root.setdefault("last_rebalanced_date", None)
    for key in (
        "portfolio",
        "watchlist",
        "watchlist_archive",
        "closed_positions",
        "decision_history",
        "insights",
        "signal_events",
    ):
        _require_object_list(root, key, "v2_state")

    root["portfolio"] = [
        _with_decision_provenance_defaults(item)
        for item in root["portfolio"]
    ]
    root["closed_positions"] = [
        _with_decision_provenance_defaults(item)
        for item in root["closed_positions"]
    ]
    root["decision_history"] = [
        _with_decision_provenance_defaults(item)
        for item in root["decision_history"]
    ]

    active_watchlist: list[dict[str, Any]] = []
    archived_watchlist = [
        _with_watchlist_compatibility_defaults(item, archived=True)
        for item in root["watchlist_archive"]
    ]
    for raw_item in root["watchlist"]:
        item = _with_watchlist_compatibility_defaults(raw_item)
        if item["lifecycle_status"] in TERMINAL_WATCHLIST_STATUSES:
            archived_watchlist.append(item)
        else:
            active_watchlist.append(item)
    root["watchlist"] = _deduplicate_watchlist(active_watchlist)
    root["watchlist_archive"] = _deduplicate_watchlist(archived_watchlist)
    return parse_portfolio_state(root)


def migrate_legacy_state(payload: Any) -> PortfolioStateV2:
    """Convert the legacy holdings state to a conservative v2 state."""
    legacy = _require_object(payload, "legacy_state")
    holdings = legacy.get("holdings")
    if not isinstance(holdings, list):
        raise PortfolioSchemaError("legacy_state.holdings must be a list")

    portfolio: list[dict[str, Any]] = []
    closed_positions: list[dict[str, Any]] = []
    for index, raw_holding in enumerate(holdings):
        holding = _require_object(raw_holding, f"legacy_state.holdings[{index}]")
        migrated = _migrate_legacy_holding(holding, index)
        if holding.get("status") == "active":
            portfolio.append(migrated)
        else:
            closed_positions.append(migrated)

    return parse_portfolio_state(
        {
            "schema_version": SCHEMA_VERSION,
            "portfolio": portfolio,
            "watchlist": [],
            "closed_positions": closed_positions,
            "decision_history": [],
            "insights": [],
            "last_rebalanced_date": None,
        }
    )


def load_or_migrate_portfolio_state(payload: Any) -> PortfolioStateV2:
    """Load v2 state or migrate a legacy holdings payload in memory."""
    root = _require_object(payload, "state")
    if root.get("schema_version") in {PREVIOUS_SCHEMA_VERSION, SCHEMA_VERSION} and "portfolio" in root:
        return parse_portfolio_state(root)
    return migrate_legacy_state(root)


def load_portfolio_state_file(path: Path) -> PortfolioStateV2:
    """Read a portfolio state file and migrate legacy contents in memory."""
    with open(path, encoding="utf-8") as file:
        return load_or_migrate_portfolio_state(json.load(file))


def save_portfolio_state_file(state: PortfolioStateV2, path: Path) -> None:
    """Atomically persist validated v2 portfolio state."""
    validated = parse_portfolio_state(state.to_dict())
    if path.exists():
        previous = load_portfolio_state_file(path)
        validate_signal_ledger_append_only(
            previous.signal_events,
            validated.signal_events,
        )
    _atomic_write_json(validated.to_dict(), path)


def save_analysis_decision_file(decision: AnalysisDecisionV2, path: Path) -> None:
    """Atomically persist one validated Gemini decision response."""
    validated = parse_analysis_decision(decision.to_dict())
    _atomic_write_json(validated.to_dict(), path)


def apply_analysis_decision(
    state: PortfolioStateV2,
    analysis: AnalysisDecisionV2,
    *,
    closed_performance_by_key: dict[str, float | int | None] | None = None,
    new_signal_events: list[dict[str, Any]] | None = None,
) -> PortfolioStateV2:
    """Apply a validated first-call result as one state transition."""
    if new_signal_events:
        state = append_signal_events(state, new_signal_events)
    updated = apply_portfolio_decisions(
        state,
        analysis.portfolio_decisions,
        closed_performance_by_key=closed_performance_by_key,
        rebalanced_date=(
            analysis.analysis_date
            if analysis.run_type == "rebalance"
            else None
        ),
    ).to_dict()
    before_watchlist = deepcopy(updated["watchlist"])
    updated["watchlist"] = _apply_watchlist_updates(
        updated["watchlist"],
        analysis.watchlist,
    )
    updated["insights"] = deepcopy(analysis.insights)
    added, changed = _watchlist_update_changes(before_watchlist, updated["watchlist"])
    return _advance_watchlist_lifecycle_payload(
        updated,
        analysis.analysis_date,
        added=added,
        updated_ids=changed,
    )


def _apply_watchlist_updates(
    watchlist: list[dict[str, Any]],
    updates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge Watchlist deltas without extending TTL for repeated evidence."""
    merged = [_with_watchlist_compatibility_defaults(item) for item in watchlist]
    by_key = {_watchlist_key(item): item for item in merged}
    for update in updates:
        candidate = _with_watchlist_compatibility_defaults(update)
        key = _watchlist_key(candidate)
        current = by_key.get(key)
        if current is None:
            merged.append(candidate)
            by_key[key] = candidate
        else:
            old_material = _material_evidence_keys(current)
            old_latest = current["latest_material_signal_date"]
            old_expiry = current["expires_on"]
            preserved_entry = current["watchlist_entry_date"]
            preserved_origin = {
                key: deepcopy(current.get(key))
                for key in (
                    "provenance_status",
                    "origin_signal_type",
                    "origin_signal_ids",
                    "thesis_id",
                )
            }
            evidence_by_url = {
                str(post.get("url") or ""): deepcopy(post)
                for post in current.get("evidence_posts", []) or []
            }
            linked_ids = list(current.get("linked_signal_ids", []) or [])
            current.update(candidate)
            current["watchlist_entry_date"] = preserved_entry
            if preserved_origin["provenance_status"] == "verified":
                current.update(preserved_origin)
            for post in candidate.get("evidence_posts", []) or []:
                evidence_by_url[str(post.get("url") or "")] = deepcopy(post)
            current["evidence_posts"] = list(evidence_by_url.values())
            current["linked_signal_ids"] = list(dict.fromkeys(
                linked_ids + list(candidate.get("linked_signal_ids", []) or [])
            ))
            current = _with_watchlist_compatibility_defaults(current)
            if not (_material_evidence_keys(current) - old_material):
                current["latest_material_signal_date"] = old_latest
                current["expires_on"] = old_expiry
            by_key[key] = current
            for index, item in enumerate(merged):
                if _watchlist_key(item) == key:
                    merged[index] = current
                    break
    return merged


def _with_state_compatibility_defaults(payload: dict[str, Any]) -> dict[str, Any]:
    root = deepcopy(payload)
    root.setdefault("signal_events", [])
    root.setdefault("watchlist_archive", [])
    root.setdefault("last_watchlist_changes", _empty_watchlist_changes())
    if "portfolio" in root and isinstance(root["portfolio"], list):
        root["portfolio"] = [
            _with_decision_provenance_defaults(item) if isinstance(item, dict) else item
            for item in root["portfolio"]
        ]
    if "closed_positions" in root and isinstance(root["closed_positions"], list):
        root["closed_positions"] = [
            _with_decision_provenance_defaults(item) if isinstance(item, dict) else item
            for item in root["closed_positions"]
        ]
    if "decision_history" in root and isinstance(root["decision_history"], list):
        root["decision_history"] = [
            _with_decision_provenance_defaults(item) if isinstance(item, dict) else item
            for item in root["decision_history"]
        ]
    if "watchlist" in root and isinstance(root["watchlist"], list):
        root["watchlist"] = [
            _with_watchlist_compatibility_defaults(item) if isinstance(item, dict) else item
            for item in root["watchlist"]
        ]
    if isinstance(root["watchlist_archive"], list):
        root["watchlist_archive"] = [
            _with_watchlist_compatibility_defaults(item, archived=True)
            if isinstance(item, dict)
            else item
            for item in root["watchlist_archive"]
        ]
    return root


def _legacy_thesis_id(item: dict[str, Any]) -> str:
    identity = _item_key(item)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return f"legacy-{digest}"


def _with_decision_provenance_defaults(item: dict[str, Any]) -> dict[str, Any]:
    value = deepcopy(item)
    value.setdefault("provenance_status", "legacy_unvalidated")
    value.setdefault("origin_signal_type", "LEGACY_UNVALIDATED")
    value.setdefault("origin_signal_ids", [])
    value.setdefault("thesis_id", _legacy_thesis_id(value))
    value.setdefault("linked_signal_ids", [])
    return value


def _legacy_status_to_lifecycle(status: Any) -> str:
    return {
        "관심": "active",
        "재검토 필요": "candidate",
        "포트폴리오 편입": "promoted",
        "종료": "archived",
    }.get(str(status or "").strip(), str(status or "").strip())


def _lifecycle_to_legacy_status(status: str) -> str:
    return {
        "candidate": "재검토 필요",
        "active": "관심",
        "promoted": "포트폴리오 편입",
        "rejected": "종료",
        "expired": "종료",
        "archived": "종료",
    }[status]


def add_business_days(start_date: str, business_days: int) -> str:
    """Add weekdays to an ISO date; exchange holidays are intentionally excluded."""
    if not isinstance(business_days, int) or isinstance(business_days, bool) or business_days < 0:
        raise PortfolioSchemaError("business_days must be a non-negative integer")
    try:
        current = date.fromisoformat(start_date)
    except (TypeError, ValueError) as exc:
        raise PortfolioSchemaError("start_date must be a valid YYYY-MM-DD date") from exc
    remaining = business_days
    while remaining:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current.isoformat()


def watchlist_expiry_date(latest_material_date: str, watchlist_kind: str) -> str:
    if watchlist_kind not in WATCHLIST_TTL_BUSINESS_DAYS:
        choices = ", ".join(sorted(WATCHLIST_TTL_BUSINESS_DAYS))
        raise PortfolioSchemaError(f"watchlist_kind must be one of: {choices}")
    return add_business_days(
        latest_material_date,
        WATCHLIST_TTL_BUSINESS_DAYS[watchlist_kind],
    )


def _with_watchlist_compatibility_defaults(
    item: dict[str, Any],
    *,
    archived: bool = False,
) -> dict[str, Any]:
    value = _with_decision_provenance_defaults(item)
    raw_status = value.get("lifecycle_status")
    if raw_status is None:
        raw_status = _legacy_status_to_lifecycle(value.get("status"))
    if archived and raw_status in ACTIVE_WATCHLIST_STATUSES:
        raw_status = "archived"
    if raw_status not in WATCHLIST_LIFECYCLE_STATUSES:
        raw_status = "archived" if archived else "candidate"
    value["lifecycle_status"] = raw_status
    value["status"] = _lifecycle_to_legacy_status(raw_status)
    value.setdefault("watchlist_kind", "mention")
    latest = str(
        value.get("latest_material_signal_date")
        or value.get("latest_evidence_date")
        or value.get("decision_date")
        or value.get("watchlist_entry_date")
        or ""
    )
    value["latest_material_signal_date"] = latest
    if not value.get("expires_on") and latest:
        value["expires_on"] = watchlist_expiry_date(latest, value["watchlist_kind"])
    value.setdefault("archived_date", value.get("watchlist_closed_date"))
    value.setdefault("archive_reason", None)
    return value


def _empty_watchlist_changes(as_of_date: str | None = None) -> dict[str, Any]:
    return {
        "date": as_of_date,
        "added": [],
        "updated": [],
        "promoted": [],
        "rejected": [],
        "expired": [],
        "archived": [],
    }


def _watchlist_key(item: dict[str, Any]) -> str:
    thesis_id = str(item.get("thesis_id") or "").strip()
    identity = _item_key(item)
    return f"thesis:{thesis_id}:{identity}" if thesis_id else identity


def _deduplicate_watchlist(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}
    for raw_item in items:
        item = deepcopy(raw_item)
        key = _watchlist_key(item)
        current = by_key.get(key)
        if current is None:
            merged.append(item)
            by_key[key] = item
            continue
        evidence_by_url = {
            str(post.get("url") or ""): post
            for post in current.get("evidence_posts", [])
        }
        for post in item.get("evidence_posts", []):
            evidence_by_url.setdefault(str(post.get("url") or ""), post)
        prior_links = list(current.get("linked_signal_ids", []))
        entry_date = min(current["watchlist_entry_date"], item["watchlist_entry_date"])
        latest_date = max(
            current["latest_material_signal_date"],
            item["latest_material_signal_date"],
        )
        current.update(item)
        current["evidence_posts"] = list(evidence_by_url.values())
        current["linked_signal_ids"] = list(dict.fromkeys(
            prior_links + list(item.get("linked_signal_ids", []))
        ))
        current["watchlist_entry_date"] = entry_date
        current["latest_material_signal_date"] = latest_date
        current["expires_on"] = watchlist_expiry_date(
            latest_date,
            current["watchlist_kind"],
        )
    return merged


def _material_evidence_keys(item: dict[str, Any]) -> set[str]:
    signal_ids = {
        str(value).strip()
        for value in item.get("linked_signal_ids", []) or []
        if str(value).strip()
    }
    if signal_ids:
        return {f"signal:{value}" for value in signal_ids}
    return {
        f"url:{str(post.get('url') or '').strip()}"
        for post in item.get("evidence_posts", []) or []
        if str(post.get("url") or "").strip()
    }


def _watchlist_update_changes(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    before_by_key = {_watchlist_key(item): item for item in before}
    added: list[str] = []
    changed: list[str] = []
    for item in after:
        key = _watchlist_key(item)
        thesis_id = str(item.get("thesis_id") or key)
        if key not in before_by_key:
            added.append(thesis_id)
        elif item != before_by_key[key]:
            changed.append(thesis_id)
    return added, changed


def advance_watchlist_lifecycle(
    state: PortfolioStateV2,
    as_of_date: str,
) -> PortfolioStateV2:
    """Advance deterministic Watchlist states without mutating the input state."""
    return _advance_watchlist_lifecycle_payload(state.to_dict(), as_of_date)


def _advance_watchlist_lifecycle_payload(
    payload: dict[str, Any],
    as_of_date: str,
    *,
    added: list[str] | None = None,
    updated_ids: list[str] | None = None,
) -> PortfolioStateV2:
    try:
        current_date = date.fromisoformat(as_of_date)
    except (TypeError, ValueError) as exc:
        raise PortfolioSchemaError("as_of_date must be a valid YYYY-MM-DD date") from exc
    updated = _with_state_compatibility_defaults(payload)
    portfolio_keys = {_item_key(item) for item in updated["portfolio"]}
    active: list[dict[str, Any]] = []
    archive = deepcopy(updated["watchlist_archive"])
    archive_by_key = {_watchlist_key(item): item for item in archive}
    changes = _empty_watchlist_changes(as_of_date)
    changes["added"] = list(dict.fromkeys(added or []))
    changes["updated"] = list(dict.fromkeys(updated_ids or []))

    for raw_item in updated["watchlist"]:
        item = _with_watchlist_compatibility_defaults(raw_item)
        lifecycle = item["lifecycle_status"]
        if _item_key(item) in portfolio_keys:
            lifecycle = "promoted"
            item["portfolio_entry_date"] = item.get("portfolio_entry_date") or as_of_date
            item["archive_reason"] = item.get("archive_reason") or "포트폴리오 편입"
        elif lifecycle in ACTIVE_WATCHLIST_STATUSES:
            expires_on = date.fromisoformat(item["expires_on"])
            if current_date >= expires_on:
                lifecycle = "expired"
                item["archive_reason"] = item.get("archive_reason") or "Watchlist 유효기간 만료"

        if lifecycle in TERMINAL_WATCHLIST_STATUSES:
            item["lifecycle_status"] = lifecycle
            item["status"] = _lifecycle_to_legacy_status(lifecycle)
            item["watchlist_closed_date"] = item.get("watchlist_closed_date") or as_of_date
            item["archived_date"] = item.get("archived_date") or as_of_date
            key = _watchlist_key(item)
            current = archive_by_key.get(key)
            if current is None:
                archive.append(item)
                archive_by_key[key] = item
            elif current != item:
                current.update(item)
            changes[lifecycle].append(str(item["thesis_id"]))
        else:
            entry = date.fromisoformat(item["watchlist_entry_date"])
            item["watchlist_duration_days"] = max(0, (current_date - entry).days)
            active.append(item)

    for key in changes:
        if isinstance(changes[key], list):
            changes[key] = list(dict.fromkeys(changes[key]))
    updated["watchlist"] = active
    updated["watchlist_archive"] = archive
    updated["last_watchlist_changes"] = changes
    return parse_portfolio_state(updated)


def signal_event_id(event: dict[str, Any]) -> str:
    """Return a stable content-addressed id for a signal event payload."""
    canonical = _signal_event_canonical(event)
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sig_" + hashlib.sha256(encoded).hexdigest()


def _signal_event_canonical(event: dict[str, Any]) -> dict[str, Any]:
    canonical = deepcopy(event)
    for metadata_key in ("signal_id", "created_at", "created_by", "model_id"):
        canonical.pop(metadata_key, None)
    return canonical


def evidence_sha256(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise PortfolioSchemaError("evidence text must be a non-empty string")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def append_signal_events(
    state: PortfolioStateV2,
    events: list[dict[str, Any]],
) -> PortfolioStateV2:
    """Append immutable signal events; exact duplicates are idempotent."""
    if not isinstance(events, list) or any(not isinstance(item, dict) for item in events):
        raise PortfolioSchemaError("events must be a list of objects")
    updated = deepcopy(state.to_dict())
    existing = {item["signal_id"]: item for item in updated["signal_events"]}
    candidates = [deepcopy(item) for item in events]
    candidate_ids = {_require_string(item, "signal_id", "events") for item in candidates}
    known_ids = set(existing) | candidate_ids
    for index, event in enumerate(candidates):
        _validate_signal_event(event, f"events[{index}]", known_ids)
        signal_id = event["signal_id"]
        current = existing.get(signal_id)
        if current is not None:
            if _signal_event_canonical(current) != _signal_event_canonical(event):
                raise PortfolioSchemaError(
                    f"events[{index}] attempts to mutate existing signal_id {signal_id}"
                )
            continue
        updated["signal_events"].append(event)
        existing[signal_id] = event
    return parse_portfolio_state(updated)


def validate_signal_ledger_append_only(
    previous_events: list[dict[str, Any]],
    current_events: list[dict[str, Any]],
) -> None:
    """Reject removal or mutation of any previously persisted signal event."""
    previous = _validate_signal_event_ledger(previous_events, "previous_signal_events")
    current = _validate_signal_event_ledger(current_events, "current_signal_events")
    for signal_id_value, event in previous.items():
        candidate = current.get(signal_id_value)
        if candidate is None:
            raise PortfolioSchemaError(
                f"signal ledger is append-only; missing prior signal_id {signal_id_value}"
            )
        if candidate != event:
            raise PortfolioSchemaError(
                f"signal ledger is append-only; mutated signal_id {signal_id_value}"
            )


def _migrate_legacy_holding(holding: dict[str, Any], index: int) -> dict[str, Any]:
    path = f"legacy_state.holdings[{index}]"
    name = _require_string(holding, "name", path)
    code = _require_string(holding, "code", path, allow_empty=True)
    market = _require_string(holding, "market", path, allow_empty=True)
    code = _normalize_legacy_code(name, market, code)
    proposed_weight = _parse_legacy_weight(holding.get("weight"), f"{path}.weight")
    decision_date = (
        holding.get("removed_date")
        or holding.get("last_confirmed_date")
        or holding.get("entry_date")
    )
    if not isinstance(decision_date, str):
        raise PortfolioSchemaError(f"{path} must contain a decision date")

    if holding.get("status") == "active":
        return {
            "name": name,
            "code": code,
            "market": market,
            "asset_type": "stock",
            "decision_actor": "미분류",
            "action": "보유",
            "basis": "이전 판단 유지",
            "decision_date": decision_date,
            "evidence_posts": [],
            "source_mentioned": False,
            "previous_weight": proposed_weight,
            "proposed_weight": proposed_weight,
            "weight_source": "AI 제안",
            "change_reason": "기존 상태 마이그레이션: 최초 재평가 전까지 보존",
        }

    reason = str(holding.get("removed_reason") or "기존 종료 포지션 보존")
    return {
        "name": name,
        "code": code,
        "market": market,
        "asset_type": "stock",
        "decision_actor": "미분류",
        "action": "매도",
        "basis": "이전 판단 유지",
        "decision_date": decision_date,
        "evidence_posts": [],
        "source_mentioned": False,
        "previous_weight": proposed_weight,
        "proposed_weight": 0,
        "weight_source": "AI 제안",
        "change_reason": reason,
        "closed_date": decision_date,
        "close_reason": reason,
        "closed_performance": None,
    }


def _normalize_legacy_code(name: str, market: str, code: str) -> str:
    return normalize_security_code(name, market, code)


def _parse_legacy_weight(value: Any, path: str) -> float:
    match = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    if not match:
        raise PortfolioSchemaError(f"{path} must contain a numeric percentage")
    return float(match.group(0))


def _atomic_write_json(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    temporary.replace(path)


def _validate_portfolio_item(
    item: dict[str, Any],
    path: str,
    *,
    allow_unclassified: bool = False,
) -> None:
    _validate_identity(item, path)
    _validate_decision(item, path, allow_unclassified=allow_unclassified)
    if item["action"] == "매도":
        raise PortfolioSchemaError(f"{path}.action must not be '매도' in active portfolio")


def _validate_closed_position(item: dict[str, Any], path: str) -> None:
    _validate_identity(item, path)
    _validate_decision(
        item,
        path,
        allow_unclassified=True,
        allow_legacy_closed=True,
    )
    if item["action"] != "매도":
        raise PortfolioSchemaError(f"{path}.action must be '매도' in closed positions")
    if item["proposed_weight"] != 0:
        raise PortfolioSchemaError(f"{path}.proposed_weight must be 0 in closed positions")
    _require_date(item, "closed_date", path)
    _require_string(item, "close_reason", path)
    _require_optional_number(item, "closed_performance", path)


def _validate_watchlist_item(item: dict[str, Any], path: str) -> None:
    _validate_identity(item, path)
    _reject_keys(
        item,
        {"previous_weight", "proposed_weight", "weight_source"},
        path,
    )
    _require_allowed(item, "decision_actor", DECISION_ACTORS, path)
    _require_allowed(item, "basis", BASES, path)
    _require_date(item, "decision_date", path)
    _validate_evidence_posts(item, path)
    _require_bool(item, "source_mentioned", path)
    _require_date(item, "watchlist_entry_date", path)
    _require_date(item, "latest_evidence_date", path)
    _require_int(item, "watchlist_duration_days", path)
    _require_optional_string(item, "portfolio_entry_date", path)
    _require_optional_string(item, "watchlist_closed_date", path)
    _require_allowed(item, "status", WATCHLIST_STATUSES, path)
    _require_optional_allowed(item, "source_scope", SOURCE_SCOPES, path)
    _require_optional_string(item, "observation_reason", path)
    _require_allowed(
        item,
        "lifecycle_status",
        WATCHLIST_LIFECYCLE_STATUSES,
        path,
    )
    _require_allowed(item, "watchlist_kind", WATCHLIST_KINDS, path)
    _require_date(item, "latest_material_signal_date", path)
    _require_date(item, "expires_on", path)
    _require_optional_date(item, "archived_date", path)
    _require_optional_string(item, "archive_reason", path)


def _validate_provenance(
    item: dict[str, Any],
    path: str,
    signal_by_id: dict[str, dict[str, Any]],
) -> None:
    status = _require_allowed(item, "provenance_status", PROVENANCE_STATUSES, path)
    origin_type = _require_allowed(item, "origin_signal_type", ORIGIN_SIGNAL_TYPES, path)
    origin_ids = _require_string_list(item, "origin_signal_ids", path)
    linked_ids = _require_string_list(item, "linked_signal_ids", path)
    _require_string(item, "thesis_id", path)
    unknown = sorted((set(origin_ids) | set(linked_ids)) - set(signal_by_id))
    if unknown:
        raise PortfolioSchemaError(
            f"{path}.linked signal ids contain unknown ids: {', '.join(unknown)}"
        )
    if status == "legacy_unvalidated":
        if origin_type != "LEGACY_UNVALIDATED" or origin_ids:
            raise PortfolioSchemaError(
                f"{path}: legacy_unvalidated provenance must use an empty LEGACY_UNVALIDATED origin"
            )
        return
    if origin_type == "PASSIVE_INDEX":
        if origin_ids:
            raise PortfolioSchemaError(f"{path}: PASSIVE_INDEX must not claim source signal ids")
        return
    if origin_type == "LEGACY_UNVALIDATED" or not origin_ids:
        raise PortfolioSchemaError(
            f"{path}: verified provenance requires a non-legacy origin signal"
        )
    expected_signal_type = origin_type
    if not any(
        signal_by_id[signal_id]["signal_type"] == expected_signal_type
        for signal_id in origin_ids
    ):
        raise PortfolioSchemaError(
            f"{path}.origin_signal_ids must include a {expected_signal_type} event"
        )


def _validate_last_watchlist_changes(item: dict[str, Any], path: str) -> None:
    _require_optional_date(item, "date", path)
    for key in ("added", "updated", "promoted", "rejected", "expired", "archived"):
        _require_string_list(item, key, path)


def _validate_signal_event_ledger(
    events: list[dict[str, Any]],
    path: str,
) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    ids = {
        _require_string(event, "signal_id", f"{path}[{index}]")
        for index, event in enumerate(events)
    }
    if len(ids) != len(events):
        raise PortfolioSchemaError(f"{path} contains duplicate signal_id values")
    for index, event in enumerate(events):
        _validate_signal_event(event, f"{path}[{index}]", ids)
        by_id[event["signal_id"]] = event
    return by_id


def _validate_signal_event(
    event: dict[str, Any],
    path: str,
    known_ids: set[str],
) -> None:
    signal_id_value = _require_string(event, "signal_id", path)
    expected_id = signal_event_id(event)
    if signal_id_value != expected_id:
        raise PortfolioSchemaError(
            f"{path}.signal_id must equal the content-addressed id {expected_id}"
        )
    signal_type = _require_allowed(event, "signal_type", SIGNAL_TYPES, path)
    _require_string(event, "post_id", path)
    _require_string(event, "post_title", path)
    _require_url(event, "post_url", path)
    _require_date(event, "published_date", path)
    evidence_text = _require_string(event, "evidence_text", path)
    evidence_hash = _require_string(event, "evidence_sha256", path)
    if not re.fullmatch(r"[0-9a-f]{64}", evidence_hash) or evidence_hash != evidence_sha256(evidence_text):
        raise PortfolioSchemaError(f"{path}.evidence_sha256 does not match evidence_text")
    entity = _require_object(event.get("entity"), f"{path}.entity")
    _validate_identity(entity, f"{path}.entity")
    _require_allowed(event, "direction", SIGNAL_DIRECTIONS, path)
    horizon = _require_object(event.get("horizon"), f"{path}.horizon")
    minimum = _require_int(horizon, "min_days", f"{path}.horizon")
    maximum = _require_int(horizon, "max_days", f"{path}.horizon")
    if minimum < 0 or maximum < minimum:
        raise PortfolioSchemaError(
            f"{path}.horizon must satisfy 0 <= min_days <= max_days"
        )
    _require_string_list(event, "catalysts", path)
    _require_string_list(event, "invalidation_conditions", path)
    _require_string(event, "thesis_id", path)
    parent_ids = _require_string_list(event, "parent_signal_ids", path)
    unknown = sorted(set(parent_ids) - known_ids)
    if unknown:
        raise PortfolioSchemaError(
            f"{path}.parent_signal_ids contains unknown ids: {', '.join(unknown)}"
        )
    if signal_id_value in parent_ids:
        raise PortfolioSchemaError(f"{path}.parent_signal_ids must not contain itself")
    if signal_type == "AI_INFERRED" and not parent_ids:
        raise PortfolioSchemaError(f"{path}.parent_signal_ids must not be empty for AI_INFERRED")
    if signal_type != "AI_INFERRED" and parent_ids:
        raise PortfolioSchemaError(f"{path}.parent_signal_ids is only allowed for AI_INFERRED")
    _require_string(event, "created_by", path)
    _require_optional_string(event, "model_id", path)
    created_at = _require_string(event, "created_at", path)
    try:
        datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PortfolioSchemaError(f"{path}.created_at must be ISO-8601") from exc


def _validate_insight(item: dict[str, Any], path: str) -> str:
    insight_id = _require_string(item, "id", path)
    _require_string(item, "title", path)
    _require_string(item, "summary", path)
    _require_string(item, "investment_implication", path)
    _validate_evidence_posts(item, path)
    _require_string_list(item, "related_decision_codes", path)
    return insight_id


def _validate_identity(item: dict[str, Any], path: str) -> None:
    _require_string(item, "name", path)
    _require_string(item, "code", path, allow_empty=True)
    _require_string(item, "market", path, allow_empty=True)
    _require_allowed(item, "asset_type", ASSET_TYPES, path)


def _validate_decision(
    item: dict[str, Any],
    path: str,
    *,
    allow_unclassified: bool = False,
    allow_legacy_closed: bool = False,
    strict_candidate_details: bool = False,
) -> None:
    _validate_identity(item, path)
    if item["asset_type"] in {"stock", "etf"} and not item["code"].strip():
        raise PortfolioSchemaError(
            f"{path}.code must not be empty for stock or etf portfolio decisions"
        )
    actors = STATE_DECISION_ACTORS if allow_unclassified else DECISION_ACTORS
    decision_actor = _require_allowed(item, "decision_actor", actors, path)
    _require_allowed(item, "action", ACTIONS, path)
    basis = _require_allowed(item, "basis", BASES, path)
    _require_date(item, "decision_date", path)
    evidence_posts = _validate_evidence_posts(item, path)
    source_mentioned = _require_bool(item, "source_mentioned", path)
    previous_weight = _require_optional_number(item, "previous_weight", path)
    proposed_weight = _require_number(item, "proposed_weight", path)
    if not math.isfinite(float(proposed_weight)) or not 0.0 <= float(proposed_weight) <= 100.0:
        raise PortfolioSchemaError(f"{path}.proposed_weight must be finite and between 0 and 100")
    if previous_weight is not None and (
        not math.isfinite(float(previous_weight))
        or not 0.0 <= float(previous_weight) <= 100.0
    ):
        raise PortfolioSchemaError(f"{path}.previous_weight must be finite and between 0 and 100")
    _require_allowed(item, "weight_source", WEIGHT_SOURCES, path)
    _require_string(item, "change_reason", path)
    allocation_role = _require_optional_allowed(
        item,
        "allocation_role",
        ALLOCATION_ROLES,
        path,
    )
    source_scope = _require_optional_allowed(item, "source_scope", SOURCE_SCOPES, path)
    _require_optional_string(item, "investment_rationale", path)
    _require_optional_string(item, "current_entry_reason", path)
    _require_optional_string_list(item, "key_risks", path)
    _require_optional_string_list(item, "linked_insight_ids", path)
    is_passive_policy = item.get("origin_signal_type") == "PASSIVE_INDEX"
    if (
        _weight_changed(item)
        and not evidence_posts
        and not allow_legacy_closed
        and not is_passive_policy
    ):
        raise PortfolioSchemaError(
            f"{path}.evidence_posts must not be empty when weight changes"
        )
    if decision_actor == "메르":
        if basis != "직접 발언":
            raise PortfolioSchemaError(
                f"{path}.basis must be '직접 발언' when decision_actor is '메르'"
            )
        if not evidence_posts:
            raise PortfolioSchemaError(
                f"{path}.evidence_posts must not be empty when decision_actor is '메르'"
            )
        if not source_mentioned:
            raise PortfolioSchemaError(
                f"{path}.source_mentioned must be true when decision_actor is '메르'"
            )
        if strict_candidate_details and source_scope != "blogger_trade_disclosure":
            raise PortfolioSchemaError(
                f"{path}.source_scope must be 'blogger_trade_disclosure' when decision_actor is '메르'"
            )
    if decision_actor == "미분류":
        if basis != "이전 판단 유지":
            raise PortfolioSchemaError(
                f"{path}.basis must be '이전 판단 유지' when decision_actor is '미분류'"
            )
        if source_mentioned:
            raise PortfolioSchemaError(
                f"{path}.source_mentioned must be false when decision_actor is '미분류'"
            )
    if (
        decision_actor == "AI"
        and item["action"] == "매수"
        and item["asset_type"] == "stock"
        and not source_mentioned
    ):
        raise PortfolioSchemaError(
            f"{path}: an AI stock buy not mentioned in source must stay on the Watchlist"
        )
    if (
        decision_actor == "AI"
        and item["action"] != "매도"
        and strict_candidate_details
        and allocation_role is None
    ):
        raise PortfolioSchemaError(f"{path}.allocation_role must be set for an AI portfolio decision")
    if decision_actor == "AI" and item["action"] == "매수" and strict_candidate_details:
        _require_string(item, "investment_rationale", path)
        _require_string(item, "current_entry_reason", path)
        risks = _require_string_list(item, "key_risks", path)
        if not risks:
            raise PortfolioSchemaError(f"{path}.key_risks must not be empty for an AI buy")
        if not evidence_posts:
            raise PortfolioSchemaError(f"{path}.evidence_posts must not be empty for an AI buy")
        if item["asset_type"] == "stock":
            if not source_mentioned:
                raise PortfolioSchemaError(
                    f"{path}: an AI stock buy not mentioned in source must stay on the Watchlist"
                )
            if source_scope != "source_named_security":
                raise PortfolioSchemaError(
                    f"{path}.source_scope must be 'source_named_security' for an AI stock buy"
                )
        elif item["asset_type"] == "etf" and not source_mentioned:
            if basis != "섹터 분석" or source_scope != "sector_only":
                raise PortfolioSchemaError(
                    f"{path}: an AI sector ETF buy not mentioned in source requires sector_only scope and sector basis"
                )


def apply_portfolio_decisions(
    state: PortfolioStateV2,
    decisions: list[dict[str, Any]],
    *,
    closed_performance_by_key: dict[str, float | int | None] | None = None,
    rebalanced_date: str | None = None,
) -> PortfolioStateV2:
    """
    Apply structured decisions without removing unmentioned positions.

    Runtime wiring and legacy-state migration are intentionally handled by later
    tasks. This function only owns the v2 portfolio transition rules.
    """
    updated = deepcopy(state.to_dict())
    portfolio = updated["portfolio"]
    closed_positions = updated["closed_positions"]
    decision_history = updated["decision_history"]
    by_key = {_item_key(item): item for item in portfolio}
    performance = closed_performance_by_key or {}
    before_cash = _cash_weight(portfolio)

    for index, raw_decision in enumerate(decisions):
        decision = _with_decision_provenance_defaults(raw_decision)
        _validate_decision(
            decision,
            f"decisions[{index}]",
            strict_candidate_details=True,
        )
        key = _item_key(decision)
        current = by_key.get(key)
        if current is not None:
            _reject_incompatible_linked_origin_update(
                current,
                decision,
                f"decisions[{index}]",
            )
            decision = _preserve_position_origin(current, decision)

        if decision["action"] == "매도":
            if current is None:
                raise PortfolioSchemaError(
                    f"decisions[{index}] cannot sell a position that is not in portfolio"
                )
            closed = deepcopy(decision)
            closed["previous_weight"] = current["proposed_weight"]
            closed["proposed_weight"] = 0
            closed["closed_date"] = decision["decision_date"]
            closed["close_reason"] = decision["change_reason"]
            closed["closed_performance"] = performance.get(key)
            closed_positions.append(closed)
            portfolio.remove(current)
            del by_key[key]
        elif current is None:
            portfolio.append(decision)
            by_key[key] = decision
        else:
            current.update(decision)

        decision_history.append(decision)

    after_cash = _cash_weight(portfolio)
    if after_cash < DEFENSIVE_CASH_TARGET and after_cash < before_cash:
        if not any(_has_defensive_cash_reason(decision) for decision in decisions):
            raise PortfolioSchemaError(
                "decisions reduce defensive cash below 20% without explaining cash/defensive risk"
            )

    if rebalanced_date is not None:
        _validate_rebalance_defensive_cash_progress(before_cash, after_cash)
        if not rebalanced_date.strip():
            raise PortfolioSchemaError("rebalanced_date must not be empty")
        updated["last_rebalanced_date"] = rebalanced_date

    return parse_portfolio_state(updated)


def _preserve_position_origin(
    current: dict[str, Any],
    decision: dict[str, Any],
) -> dict[str, Any]:
    """Preserve a verified position's first provenance across later AI management."""
    result = deepcopy(decision)
    current_value = _with_decision_provenance_defaults(current)
    if current_value["provenance_status"] == "verified":
        for key in (
            "provenance_status",
            "origin_signal_type",
            "origin_signal_ids",
            "thesis_id",
        ):
            result[key] = deepcopy(current_value[key])
    return result


def _reject_incompatible_linked_origin_update(
    current: dict[str, Any],
    decision: dict[str, Any],
    path: str,
) -> None:
    """Do not let immutable-origin preservation bypass new-signal validation."""
    if current.get("provenance_status") != "verified":
        return
    linked_ids = [
        str(value).strip()
        for value in decision.get("linked_signal_ids", []) or []
        if str(value).strip()
    ]
    rejected_ids = [
        str(value).strip()
        for value in decision.get("rejected_linked_signal_ids", []) or []
        if str(value).strip()
    ]
    if not linked_ids and not rejected_ids:
        return
    if decision.get("provenance_status") == "verified" and not rejected_ids:
        return
    current_weight = float(current.get("proposed_weight") or 0.0)
    proposed_weight = float(decision.get("proposed_weight") or 0.0)
    if (
        decision.get("action") in {"매수", "비중확대", "보유"}
        and proposed_weight >= current_weight - 1e-9
    ):
        raise PortfolioSchemaError(
            f"{path} links signals incompatible with maintaining or increasing the verified long"
        )


def _cash_weight(portfolio: list[dict[str, Any]]) -> float:
    total = sum(float(item.get("proposed_weight", 0) or 0) for item in portfolio)
    return max(0.0, 100.0 - total)


def _has_defensive_cash_reason(item: dict[str, Any]) -> bool:
    reason = " ".join(
        str(item.get(key) or "")
        for key in ("change_reason", "investment_rationale", "current_entry_reason")
    )
    keywords = (
        "현금",
        "현금성",
        "방어",
        "리스크",
        "위험",
        "단기채",
        "유동성",
        "불확실",
        "방어자산",
    )
    return any(keyword in reason for keyword in keywords)


def _validate_rebalance_defensive_cash_progress(before_cash: float, after_cash: float) -> None:
    if before_cash < DEFENSIVE_CASH_TARGET and after_cash < DEFENSIVE_CASH_TARGET:
        raise PortfolioSchemaError(
            "rebalance must restore defensive cash to at least 20% when current cash is below target"
        )


def _item_key(item: dict[str, Any]) -> str:
    market = str(item.get("market", "")).strip().upper()
    asset_type = str(item.get("asset_type", "")).strip().lower()
    code = normalize_security_code(item.get("name", ""), market, item.get("code", ""))
    if code:
        return f"{asset_type}:{market}:{code}"
    return f"{asset_type}:{market}:NAME:{str(item.get('name', '')).strip().lower()}"


def _validate_no_current_closed_overlap(
    portfolio: list[dict[str, Any]],
    closed_positions: list[dict[str, Any]],
    path: str,
) -> None:
    current_by_key = {_item_key(item): item for item in portfolio}
    overlaps = []
    for item in closed_positions:
        key = _item_key(item)
        current = current_by_key.get(key)
        if current is None:
            continue
        active_since = str(
            current.get("portfolio_entry_date")
            or current.get("decision_date")
            or ""
        )
        closed_date = str(item.get("closed_date") or "")
        # A close before the current episode is legitimate re-entry history.
        if not active_since or not closed_date or closed_date >= active_since:
            overlaps.append(key)
    overlaps.sort()
    if overlaps:
        raise PortfolioSchemaError(
            f"{path}.closed_positions overlaps with active portfolio: {', '.join(overlaps)}"
        )


def _validate_no_watchlist_archive_overlap(
    watchlist: list[dict[str, Any]],
    archive: list[dict[str, Any]],
    path: str,
) -> None:
    active_keys = {_watchlist_key(item) for item in watchlist}
    overlaps = sorted(
        _watchlist_key(item)
        for item in archive
        if _watchlist_key(item) in active_keys
    )
    if overlaps:
        raise PortfolioSchemaError(
            f"{path}.watchlist_archive overlaps with active watchlist: {', '.join(overlaps)}"
        )


def _validate_unique_watchlist_keys(items: list[dict[str, Any]], path: str) -> None:
    seen: set[str] = set()
    for index, item in enumerate(items):
        key = _watchlist_key(item)
        if key in seen:
            raise PortfolioSchemaError(f"{path}[{index}] duplicates Watchlist key {key}")
        seen.add(key)


def _validate_evidence_posts(item: dict[str, Any], path: str) -> list[dict[str, Any]]:
    posts = _require_object_list(item, "evidence_posts", path)
    for index, post in enumerate(posts):
        post_path = f"{path}.evidence_posts[{index}]"
        _require_string(post, "title", post_path)
        _require_url(post, "url", post_path)
        _require_date(post, "published_date", post_path)
    return posts


def _require_object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PortfolioSchemaError(f"{path} must be an object")
    return value


def _require_object_list(item: dict[str, Any], key: str, path: str) -> list[dict[str, Any]]:
    value = item.get(key)
    field_path = f"{path}.{key}"
    if not isinstance(value, list):
        raise PortfolioSchemaError(f"{field_path} must be a list")
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise PortfolioSchemaError(f"{field_path}[{index}] must be an object")
    return value


def _require_string(
    item: dict[str, Any],
    key: str,
    path: str,
    *,
    allow_empty: bool = False,
) -> str:
    value = item.get(key)
    field_path = f"{path}.{key}"
    if not isinstance(value, str):
        raise PortfolioSchemaError(f"{field_path} must be a string")
    if not allow_empty and not value.strip():
        raise PortfolioSchemaError(f"{field_path} must not be empty")
    return value


def _require_optional_string(item: dict[str, Any], key: str, path: str) -> str | None:
    value = item.get(key)
    if value is not None and not isinstance(value, str):
        raise PortfolioSchemaError(f"{path}.{key} must be a string or null")
    return value


def _require_string_list(item: dict[str, Any], key: str, path: str) -> list[str]:
    value = item.get(key)
    if not isinstance(value, list) or any(
        not isinstance(entry, str) or not entry.strip() for entry in value
    ):
        raise PortfolioSchemaError(f"{path}.{key} must be a list of non-empty strings")
    return value


def _require_optional_string_list(
    item: dict[str, Any],
    key: str,
    path: str,
) -> list[str] | None:
    if key not in item:
        return None
    return _require_string_list(item, key, path)


def _require_date(item: dict[str, Any], key: str, path: str) -> str:
    value = _require_string(item, key, path)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise PortfolioSchemaError(f"{path}.{key} must use YYYY-MM-DD format")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise PortfolioSchemaError(f"{path}.{key} must be a valid date") from exc
    return value


def _require_optional_date(item: dict[str, Any], key: str, path: str) -> str | None:
    value = _require_optional_string(item, key, path)
    if value is None:
        return None
    return _require_date(item, key, path)


def _require_url(item: dict[str, Any], key: str, path: str) -> str:
    value = _require_string(item, key, path)
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PortfolioSchemaError(f"{path}.{key} must be an http(s) URL")
    return value


def _require_bool(item: dict[str, Any], key: str, path: str) -> bool:
    value = item.get(key)
    if not isinstance(value, bool):
        raise PortfolioSchemaError(f"{path}.{key} must be a boolean")
    return value


def _require_int(item: dict[str, Any], key: str, path: str) -> int:
    value = item.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise PortfolioSchemaError(f"{path}.{key} must be an integer")
    return value


def _require_number(item: dict[str, Any], key: str, path: str) -> float | int:
    value = item.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise PortfolioSchemaError(f"{path}.{key} must be a number")
    return value


def _require_optional_number(
    item: dict[str, Any],
    key: str,
    path: str,
) -> float | int | None:
    value = item.get(key)
    if value is not None and (
        not isinstance(value, (int, float)) or isinstance(value, bool)
    ):
        raise PortfolioSchemaError(f"{path}.{key} must be a number or null")
    return value


def _require_allowed(
    item: dict[str, Any],
    key: str,
    allowed: set[str],
    path: str,
) -> str:
    value = _require_string(item, key, path)
    if value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise PortfolioSchemaError(f"{path}.{key} must be one of: {choices}")
    return value


def _require_optional_allowed(
    item: dict[str, Any],
    key: str,
    allowed: set[str],
    path: str,
) -> str | None:
    if key not in item or item[key] is None:
        return None
    return _require_allowed(item, key, allowed, path)


def _reject_keys(item: dict[str, Any], keys: set[str], path: str) -> None:
    for key in sorted(keys):
        if key in item:
            raise PortfolioSchemaError(f"{path}.{key} is not allowed")


def _weight_changed(item: dict[str, Any]) -> bool:
    previous = item.get("previous_weight")
    proposed = item.get("proposed_weight")
    return previous is not None and previous != proposed


def _requires_linked_insight(item: dict[str, Any]) -> bool:
    return item.get("action") in {"매수", "매도", "비중확대", "비중축소"} or _weight_changed(item)


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 3 or lines[-1].strip() != "```":
        return stripped
    return "\n".join(lines[1:-1]).strip()
