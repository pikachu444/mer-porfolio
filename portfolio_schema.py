"""
Structured portfolio state schema.

The v2 schema is intentionally separate from the legacy Markdown-derived
portfolio_state.py flow. Migration and runtime adoption happen in later tasks.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


SCHEMA_VERSION = "2.0"

DECISION_ACTORS = {"메르", "AI"}
STATE_DECISION_ACTORS = DECISION_ACTORS | {"미분류"}
ACTIONS = {"매수", "보유", "비중확대", "비중축소", "매도"}
BASES = {"직접 발언", "종목 분석", "섹터 분석", "이전 판단 유지"}
ASSET_TYPES = {"stock", "etf", "sector", "cash"}
WEIGHT_SOURCES = {"메르 직접 발언 기반", "AI 제안"}
WATCHLIST_STATUSES = {"관심", "재검토 필요", "포트폴리오 편입", "종료"}
RUN_TYPES = {"regular", "rebalance"}


class PortfolioSchemaError(ValueError):
    """Raised when structured portfolio state does not satisfy the v2 contract."""


@dataclass(frozen=True)
class PortfolioStateV2:
    portfolio: list[dict[str, Any]]
    watchlist: list[dict[str, Any]]
    closed_positions: list[dict[str, Any]]
    decision_history: list[dict[str, Any]]
    last_rebalanced_date: str | None
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "portfolio": self.portfolio,
            "watchlist": self.watchlist,
            "closed_positions": self.closed_positions,
            "decision_history": self.decision_history,
            "last_rebalanced_date": self.last_rebalanced_date,
        }


@dataclass(frozen=True)
class AnalysisDecisionV2:
    analysis_date: str
    run_type: str
    portfolio_decisions: list[dict[str, Any]]
    watchlist: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "analysis_date": self.analysis_date,
            "run_type": self.run_type,
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
    portfolio_decisions = _require_object_list(
        root,
        "portfolio_decisions",
        "analysis",
    )
    watchlist = _require_object_list(root, "watchlist", "analysis")

    for index, item in enumerate(portfolio_decisions):
        _validate_decision(item, f"analysis.portfolio_decisions[{index}]")
    for index, item in enumerate(watchlist):
        _validate_watchlist_item(item, f"analysis.watchlist[{index}]")

    return AnalysisDecisionV2(
        analysis_date=analysis_date,
        run_type=run_type,
        portfolio_decisions=portfolio_decisions,
        watchlist=watchlist,
    )


def parse_portfolio_state(payload: Any) -> PortfolioStateV2:
    """Validate a decoded v2 portfolio state payload."""
    root = _require_object(payload, "state")
    schema_version = _require_string(root, "schema_version", "state")
    if schema_version != SCHEMA_VERSION:
        raise PortfolioSchemaError(
            f"state.schema_version must be {SCHEMA_VERSION!r}, got {schema_version!r}"
        )

    portfolio = _require_object_list(root, "portfolio", "state")
    watchlist = _require_object_list(root, "watchlist", "state")
    closed_positions = _require_object_list(root, "closed_positions", "state")
    decision_history = _require_object_list(root, "decision_history", "state")
    last_rebalanced_date = _require_optional_date(
        root,
        "last_rebalanced_date",
        "state",
    )

    for index, item in enumerate(portfolio):
        _validate_portfolio_item(
            item,
            f"state.portfolio[{index}]",
            allow_unclassified=True,
        )
    for index, item in enumerate(watchlist):
        _validate_watchlist_item(item, f"state.watchlist[{index}]")
    for index, item in enumerate(closed_positions):
        _validate_closed_position(item, f"state.closed_positions[{index}]")
    for index, item in enumerate(decision_history):
        _validate_decision(item, f"state.decision_history[{index}]")

    return PortfolioStateV2(
        schema_version=schema_version,
        portfolio=portfolio,
        watchlist=watchlist,
        closed_positions=closed_positions,
        decision_history=decision_history,
        last_rebalanced_date=last_rebalanced_date,
    )


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
            "last_rebalanced_date": None,
        }
    )


def load_or_migrate_portfolio_state(payload: Any) -> PortfolioStateV2:
    """Load v2 state or migrate a legacy holdings payload in memory."""
    root = _require_object(payload, "state")
    if root.get("schema_version") == SCHEMA_VERSION and "portfolio" in root:
        return parse_portfolio_state(root)
    return migrate_legacy_state(root)


def load_portfolio_state_file(path: Path) -> PortfolioStateV2:
    """Read a portfolio state file and migrate legacy contents in memory."""
    with open(path, encoding="utf-8") as file:
        return load_or_migrate_portfolio_state(json.load(file))


def save_portfolio_state_file(state: PortfolioStateV2, path: Path) -> None:
    """Atomically persist validated v2 portfolio state."""
    validated = parse_portfolio_state(state.to_dict())
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
) -> PortfolioStateV2:
    """Apply a validated first-call result as one state transition."""
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
    updated["watchlist"] = deepcopy(analysis.watchlist)
    return parse_portfolio_state(updated)


def _migrate_legacy_holding(holding: dict[str, Any], index: int) -> dict[str, Any]:
    path = f"legacy_state.holdings[{index}]"
    name = _require_string(holding, "name", path)
    code = _require_string(holding, "code", path, allow_empty=True)
    market = _require_string(holding, "market", path, allow_empty=True)
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
) -> None:
    _validate_identity(item, path)
    actors = STATE_DECISION_ACTORS if allow_unclassified else DECISION_ACTORS
    decision_actor = _require_allowed(item, "decision_actor", actors, path)
    _require_allowed(item, "action", ACTIONS, path)
    basis = _require_allowed(item, "basis", BASES, path)
    _require_date(item, "decision_date", path)
    evidence_posts = _validate_evidence_posts(item, path)
    source_mentioned = _require_bool(item, "source_mentioned", path)
    _require_optional_number(item, "previous_weight", path)
    _require_number(item, "proposed_weight", path)
    _require_allowed(item, "weight_source", WEIGHT_SOURCES, path)
    _require_string(item, "change_reason", path)
    if _weight_changed(item) and not evidence_posts and not allow_legacy_closed:
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
        and not source_mentioned
    ):
        if basis != "섹터 분석":
            raise PortfolioSchemaError(
                f"{path}.basis must be '섹터 분석' for an AI buy not mentioned in source"
            )
        if not evidence_posts:
            raise PortfolioSchemaError(
                f"{path}.evidence_posts must not be empty for an AI buy not mentioned in source"
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

    for index, raw_decision in enumerate(decisions):
        decision = deepcopy(raw_decision)
        _validate_decision(decision, f"decisions[{index}]")
        key = _item_key(decision)
        current = by_key.get(key)

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

    if rebalanced_date is not None:
        if not rebalanced_date.strip():
            raise PortfolioSchemaError("rebalanced_date must not be empty")
        updated["last_rebalanced_date"] = rebalanced_date

    return parse_portfolio_state(updated)


def _item_key(item: dict[str, Any]) -> str:
    code = item.get("code", "").strip().upper()
    market = item.get("market", "").strip().upper()
    asset_type = item.get("asset_type", "").strip().lower()
    if code:
        return f"{asset_type}:{market}:{code}"
    return f"{asset_type}:{market}:NAME:{item.get('name', '').strip().lower()}"


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


def _reject_keys(item: dict[str, Any], keys: set[str], path: str) -> None:
    for key in sorted(keys):
        if key in item:
            raise PortfolioSchemaError(f"{path}.{key} is not allowed")


def _weight_changed(item: dict[str, Any]) -> bool:
    previous = item.get("previous_weight")
    proposed = item.get("proposed_weight")
    return previous is not None and previous != proposed


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 3 or lines[-1].strip() != "```":
        return stripped
    return "\n".join(lines[1:-1]).strip()
