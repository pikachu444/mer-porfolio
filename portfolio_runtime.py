"""Runtime policy gates between LLM decisions and portfolio state commits."""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass
from statistics import pstdev
from typing import Any, Mapping

from portfolio_allocator import DEFAULT_POLICY, allocate_portfolio, drawdown_risk_scale
from portfolio_metrics import max_drawdown
from portfolio_schema import (
    AnalysisDecisionV2,
    PortfolioStateV2,
    normalize_security_code,
    parse_portfolio_state,
)


class PortfolioPolicyBlocked(ValueError):
    """Raised when a run cannot safely produce a complete deterministic plan."""


_TURNOVER_EPSILON = 1e-9
_EXECUTION_DRIFT_PCT = 0.50
_TURNOVER_EXEMPT_REASONS = {
    "forced_risk_reduction",
    "full_exit",
    "thesis_invalidation",
    "passive_initialization",
}


@dataclass(frozen=True)
class TurnoverControlResult:
    """A deterministic target vector after applying the normal-trade budget.

    Turnover is gross traded security notional in percentage points: the sum
    of ``abs(target_weight - current_weight)``.  Cash is the residual and is
    deliberately not counted a second time.
    """

    targets: dict[str, float]
    raw_turnover: float
    applied_turnover: float
    raw_normal_turnover: float
    applied_normal_turnover: float
    exempt_turnover: float
    exempt_turnover_by_reason: dict[str, float]
    cap: float
    capped: bool


def cap_normal_rebalance_turnover(
    current_weights: Mapping[str, float],
    desired_targets: Mapping[str, float],
    *,
    exemptions_by_key: Mapping[str, str] | None = None,
    cap: float = DEFAULT_POLICY.normal_rebalance_turnover_cap,
) -> TurnoverControlResult:
    """Cap normal gross target changes while applying forced changes in full.

    Supported whole-delta exemptions are forced risk reduction, full exit,
    explicit thesis invalidation, and first passive initialization.  The last
    exemption also covers only the amount of normal selling needed to fund the
    initial passive buy; this keeps the policy cash allocation from falling
    merely because the corresponding sell leg was capped.
    """

    if not math.isfinite(float(cap)) or float(cap) < 0:
        raise ValueError("turnover cap must be a finite non-negative number")
    exemptions = dict(exemptions_by_key or {})
    unknown_reasons = sorted(set(exemptions.values()) - _TURNOVER_EXEMPT_REASONS)
    if unknown_reasons:
        raise ValueError("unsupported turnover exemption: " + ", ".join(unknown_reasons))

    keys = sorted(set(current_weights) | set(desired_targets))
    current = {
        key: _validated_weight(current_weights.get(key, 0.0), f"current_weights[{key!r}]")
        for key in keys
    }
    desired = {
        key: _validated_weight(desired_targets.get(key, 0.0), f"desired_targets[{key!r}]")
        for key in keys
    }
    unknown_keys = sorted(set(exemptions) - set(keys))
    if unknown_keys:
        raise ValueError("turnover exemptions reference unknown keys: " + ", ".join(unknown_keys))

    deltas = {key: desired[key] - current[key] for key in keys}
    exempt_amounts = {
        key: abs(deltas[key]) if key in exemptions else 0.0
        for key in keys
    }
    exempt_reasons: dict[str, list[tuple[str, float]]] = {}
    for key, reason in exemptions.items():
        amount = abs(deltas[key])
        if amount > _TURNOVER_EPSILON:
            exempt_reasons.setdefault(reason, []).append((key, amount))

    # Passive initialization is a two-leg policy action.  Existing exempt sell
    # legs fund it first; any remaining funding need is taken deterministically
    # from ordinary desired reductions, including a partial final reduction.
    passive_buy = sum(
        max(0.0, deltas[key])
        for key, reason in exemptions.items()
        if reason == "passive_initialization"
    )
    already_exempt_sells = sum(
        max(0.0, -deltas[key])
        for key in keys
        if exempt_amounts[key] > _TURNOVER_EPSILON
    )
    passive_funding_needed = max(0.0, passive_buy - already_exempt_sells)
    for key in keys:
        if passive_funding_needed <= _TURNOVER_EPSILON:
            break
        if deltas[key] >= -_TURNOVER_EPSILON or exempt_amounts[key] > _TURNOVER_EPSILON:
            continue
        amount = min(-deltas[key], passive_funding_needed)
        exempt_amounts[key] = amount
        exempt_reasons.setdefault("passive_initialization", []).append((key, amount))
        passive_funding_needed -= amount

    raw_turnover = sum(abs(delta) for delta in deltas.values())
    exempt_turnover = sum(exempt_amounts.values())
    raw_normal_turnover = max(0.0, raw_turnover - exempt_turnover)
    normal_scale = (
        min(1.0, float(cap) / raw_normal_turnover)
        if raw_normal_turnover > _TURNOVER_EPSILON
        else 1.0
    )

    targets: dict[str, float] = {}
    for key in keys:
        delta = deltas[key]
        magnitude = abs(delta)
        exempt_amount = min(magnitude, exempt_amounts[key])
        applied_magnitude = exempt_amount + (magnitude - exempt_amount) * normal_scale
        target = current[key] + math.copysign(applied_magnitude, delta) if magnitude else current[key]
        if target > _TURNOVER_EPSILON:
            targets[key] = round(target, 10)

    applied_turnover = sum(
        abs(targets.get(key, 0.0) - current[key])
        for key in keys
    )
    applied_normal_turnover = max(0.0, applied_turnover - exempt_turnover)
    by_reason = {
        reason: round(sum(amount for _, amount in entries), 10)
        for reason, entries in sorted(exempt_reasons.items())
    }
    return TurnoverControlResult(
        targets=dict(sorted(targets.items())),
        raw_turnover=round(raw_turnover, 10),
        applied_turnover=round(applied_turnover, 10),
        raw_normal_turnover=round(raw_normal_turnover, 10),
        applied_normal_turnover=round(applied_normal_turnover, 10),
        exempt_turnover=round(exempt_turnover, 10),
        exempt_turnover_by_reason=by_reason,
        cap=float(cap),
        capped=raw_normal_turnover > float(cap) + _TURNOVER_EPSILON,
    )


def _validated_weight(value: Any, path: str) -> float:
    try:
        weight = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path} must be numeric") from exc
    if not math.isfinite(weight) or weight < 0:
        raise ValueError(f"{path} must be a finite non-negative number")
    return weight


def _policy_action(
    previous: float,
    target: float,
    *,
    minimum_delta: float = 0.0,
) -> str:
    if abs(target - previous) < minimum_delta - _TURNOVER_EPSILON:
        return "보유"
    if previous <= 1e-9 and target > 1e-9:
        return "매수"
    if target <= 1e-9 and previous > 1e-9:
        return "매도"
    if target > previous + 1e-9:
        return "비중확대"
    if target < previous - 1e-9:
        return "비중축소"
    return "보유"


PASSIVE_POLICY_POSITIONS = (
    ("KODEX 200", "069500", "KR", 10.0, "KR"),
    ("TIGER 미국S&P500", "360750", "KR", 10.0, "US"),
)


def ensure_policy_positions(state: PortfolioStateV2, as_of_date: str) -> PortfolioStateV2:
    """Seed the strategic passive sleeve independently of LLM output."""
    payload = deepcopy(state.to_dict())
    existing = {security_key(item) for item in payload["portfolio"]}
    for name, code, market, weight, exposure_country in PASSIVE_POLICY_POSITIONS:
        item = {
            "name": name,
            "code": code,
            "market": market,
            "asset_type": "etf",
            "decision_actor": "AI",
            "action": "보유",
            "basis": "이전 판단 유지",
            "decision_date": as_of_date,
            "evidence_posts": [],
            "source_mentioned": False,
            "previous_weight": 0.0,
            "proposed_weight": 0.0,
            "weight_source": "AI 제안",
            "change_reason": "사전 합의된 전략적 패시브 지수 슬리브",
            "allocation_role": "core",
            "source_scope": "previous_decision",
            "investment_rationale": "시장 베타 20%를 고정 패시브로 분리",
            "current_entry_reason": "20/40/20/20 전략 정책",
            "key_risks": ["지수 가격 변동", "추적오차"],
            "linked_insight_ids": [],
            "provenance_status": "verified",
            "origin_signal_type": "PASSIVE_INDEX",
            "origin_signal_ids": [],
            "linked_signal_ids": [],
            "thesis_id": f"policy-passive-{code}",
            "issuer_id": f"POLICY-{code}",
            "theme_ids": ["PASSIVE_INDEX"],
            "country_code": exposure_country,
            "fixed_weight": 1.0,
        }
        if security_key(item) not in existing:
            payload["portfolio"].append(item)
            existing.add(security_key(item))
    return parse_portfolio_state(payload)


def security_key(item: Mapping[str, Any]) -> str:
    market = str(item.get("market") or "").strip().upper()
    normalized_code = normalize_security_code(
        item.get("name"),
        market,
        item.get("code"),
    )
    return ":".join((
        str(item.get("asset_type") or "").strip().lower(),
        market,
        normalized_code or str(item.get("name") or "").strip().upper(),
    ))


def validate_rebalance_coverage(
    state: PortfolioStateV2,
    analysis: AnalysisDecisionV2,
) -> None:
    if analysis.run_type != "rebalance":
        return
    expected = {
        security_key(item)
        for item in state.portfolio
        if item.get("origin_signal_type") != "PASSIVE_INDEX"
    }
    covered = {security_key(item) for item in analysis.portfolio_decisions}
    missing = sorted(expected - covered)
    if missing:
        raise PortfolioPolicyBlocked(
            "rebalance did not review every current holding: " + ", ".join(missing)
        )


def ledger_risk_inputs(ledger: Mapping[str, Any]) -> tuple[float | None, float]:
    totals = [
        float(item["total_value"])
        for item in ledger.get("snapshots", []) or []
        if item.get("total_value") is not None and float(item["total_value"]) > 0
    ]
    if not totals:
        return None, 0.0
    drawdown = totals[-1] / max(totals) - 1.0
    returns = [totals[index] / totals[index - 1] - 1.0 for index in range(1, len(totals))]
    volatility = pstdev(returns) * math.sqrt(252.0) if len(returns) >= 20 else None
    return volatility, drawdown


def update_ledger_risk_state(
    ledger: dict[str, Any],
    current_drawdown: float,
    *,
    as_of_date: str,
) -> float:
    """Apply immediate de-risking and 20-valuation-day stepwise recovery."""
    state = ledger.setdefault("risk_state", {
        "scale": 1.0,
        "recovery_days": 0,
        "last_evaluated_date": None,
        "current_drawdown": 0.0,
    })
    current_scale = float(state.get("scale", 1.0))
    immediate = drawdown_risk_scale(current_drawdown)
    if immediate < current_scale:
        current_scale = immediate
        state["recovery_days"] = 0
    elif immediate > current_scale and state.get("last_evaluated_date") != as_of_date:
        state["recovery_days"] = int(state.get("recovery_days", 0)) + 1
        if state["recovery_days"] >= 20:
            next_scale = {0.25: 0.50, 0.50: 0.75, 0.75: 1.0}.get(current_scale, 1.0)
            current_scale = min(next_scale, immediate)
            state["recovery_days"] = 0
    elif immediate == current_scale:
        state["recovery_days"] = 0
    state.update({
        "scale": current_scale,
        "current_drawdown": float(current_drawdown),
        "last_evaluated_date": as_of_date,
    })
    return current_scale


def allocate_projected_state(
    projected: PortfolioStateV2,
    *,
    volatility_by_key: Mapping[str, float],
    portfolio_volatility: float | None,
    max_portfolio_drawdown: float,
    risk_scale_override: float | None = None,
    as_of_date: str | None = None,
    current_weights_by_key: Mapping[str, float] | None = None,
) -> tuple[PortfolioStateV2, dict[str, Any]]:
    """Allocate positions, then constrain ordinary trades to 15% gross notional.

    ``current_weights_by_key`` should contain pre-trade NAV weights when the
    ledger is available.  The prior target weights are a deterministic fallback
    for callers that do not own execution state.
    """
    payload = deepcopy(projected.to_dict())
    state_target_weights = {
        security_key(item): float(item.get("proposed_weight") or 0.0)
        for item in payload["portfolio"]
    }
    preexisting_by_key = {
        security_key(item): (
            item.get("previous_weight") is not None
            and float(item.get("previous_weight") or 0.0) > _TURNOVER_EPSILON
        )
        for item in payload["portfolio"]
    }
    turnover_current_weights = (
        {
            str(key): _validated_weight(value, f"current_weights_by_key[{key!r}]")
            for key, value in current_weights_by_key.items()
        }
        if current_weights_by_key is not None
        else dict(state_target_weights)
    )
    legacy = [
        item for item in payload["portfolio"]
        if item.get("provenance_status") != "verified"
    ]
    verified = [
        item for item in payload["portfolio"]
        if item.get("provenance_status") == "verified"
    ]
    quarantine_risk_scale = min(
        drawdown_risk_scale(max_portfolio_drawdown),
        float(risk_scale_override) if risk_scale_override is not None else 1.0,
    )
    legacy_raw_total = sum(float(item.get("proposed_weight") or 0.0) for item in legacy)
    for item in legacy:
        previous = float(item.get("proposed_weight") or 0.0)
        item["previous_weight"] = previous
        item["proposed_weight"] = min(previous, 5.0 * quarantine_risk_scale)
        item["allocation_method"] = "legacy_quarantine_wind_down"
    capped_legacy_total = sum(float(item["proposed_weight"]) for item in legacy)
    legacy_total_cap = 40.0 * quarantine_risk_scale
    if capped_legacy_total > legacy_total_cap:
        scale = legacy_total_cap / capped_legacy_total
        for item in legacy:
            item["proposed_weight"] = round(float(item["proposed_weight"]) * scale, 10)
    for item in legacy:
        item["policy_action"] = _policy_action(
            float(item.get("previous_weight") or 0.0),
            float(item.get("proposed_weight") or 0.0),
        )
        item["policy_change_reason"] = "검증 전 레거시 포지션 5%/총 40% 격리 한도 적용"

    candidates = []
    for item in verified:
        key = security_key(item)
        if item.get("origin_signal_type") == "PASSIVE_INDEX":
            candidates.append({
                "key": key,
                "origin": "PASSIVE_INDEX",
                "country_code": item.get("country_code"),
                "fixed_weight": item.get("fixed_weight", 1.0),
            })
            continue
        volatility = volatility_by_key.get(key)
        required = {
            "quality_components": item.get("quality_components"),
            "issuer_id": item.get("issuer_id"),
            "theme_ids": item.get("theme_ids"),
            "country_code": item.get("country_code"),
        }
        missing = [name for name, value in required.items() if value in (None, "", [])]
        if volatility is None:
            missing.append("annualized_volatility")
        if missing:
            raise PortfolioPolicyBlocked(
                f"verified position {key} lacks allocation data: {', '.join(missing)}"
            )
        candidates.append({
            "key": key,
            "origin": item.get("origin_signal_type"),
            "quality_components": item["quality_components"],
            "annualized_volatility": volatility,
            "issuer_id": item["issuer_id"],
            "theme_ids": item["theme_ids"],
            "country_code": item["country_code"],
        })

    allocation = allocate_portfolio(
        candidates,
        portfolio_volatility=portfolio_volatility,
        max_drawdown=max_portfolio_drawdown,
    )
    effective_risk_scale = min(
        allocation.risk_scale,
        float(risk_scale_override) if risk_scale_override is not None else 1.0,
    )
    active_scale_adjustment = (
        effective_risk_scale / allocation.risk_scale
        if allocation.risk_scale > 0
        else 0.0
    )
    legacy_weight = sum(float(item.get("proposed_weight") or 0.0) for item in legacy)
    verified_capacity = max(0.0, 80.0 - legacy_weight)
    passive_keys = {
        security_key(item)
        for item in verified
        if item.get("origin_signal_type") == "PASSIVE_INDEX"
    }
    passive_proposed = sum(
        weight for key, weight in allocation.targets.items() if key in passive_keys
    )
    passive_capacity_scale = (
        min(1.0, verified_capacity / passive_proposed)
        if passive_proposed
        else 1.0
    )
    passive_allocated = passive_proposed * passive_capacity_scale
    active_capacity = max(0.0, verified_capacity - passive_allocated)
    active_proposed = sum(
        weight for key, weight in allocation.targets.items() if key not in passive_keys
    )
    active_capacity_scale = (
        min(1.0, active_capacity / active_proposed)
        if active_proposed
        else 1.0
    )
    verified_targets = {
        key: weight * (
            passive_capacity_scale
            if key in passive_keys
            else active_capacity_scale * active_scale_adjustment
        )
        for key, weight in allocation.targets.items()
    }
    if effective_risk_scale <= 0.25:
        for item in verified:
            key = security_key(item)
            if (
                item.get("origin_signal_type") == "AI_INFERRED"
                and float(turnover_current_weights.get(key, 0.0)) <= _TURNOVER_EPSILON
            ):
                verified_targets[key] = 0.0

    desired_targets = {
        security_key(item): float(item.get("proposed_weight") or 0.0)
        for item in legacy
    }
    desired_targets.update(verified_targets)

    # A model-directed sale has already moved out of the active portfolio.  Its
    # pre-trade weight still belongs in gross turnover and is always a full-exit
    # exception.  Filtering by date avoids replaying historical closes.
    closed_this_run: dict[str, dict[str, Any]] = {}
    if as_of_date:
        for item in payload["closed_positions"]:
            if item.get("closed_date") != as_of_date:
                continue
            key = security_key(item)
            closed_this_run[key] = item
            turnover_current_weights.setdefault(
                key,
                float(item.get("previous_weight") or 0.0),
            )

    items_by_key = {
        security_key(item): item
        for item in payload["portfolio"]
    }
    items_by_key.update(closed_this_run)
    turnover_exemptions: dict[str, str] = {}
    for key in sorted(set(turnover_current_weights) | set(desired_targets)):
        current_weight = float(turnover_current_weights.get(key, 0.0))
        desired_weight = float(desired_targets.get(key, 0.0))
        item = items_by_key.get(key, {})
        if desired_weight < current_weight - _TURNOVER_EPSILON and _is_explicit_thesis_invalidation(item):
            turnover_exemptions[key] = "thesis_invalidation"
        elif desired_weight <= _TURNOVER_EPSILON and current_weight > _TURNOVER_EPSILON:
            turnover_exemptions[key] = "full_exit"
        elif (
            desired_weight < current_weight - _TURNOVER_EPSILON
            and (
                effective_risk_scale < 1.0 - _TURNOVER_EPSILON
                or item.get("provenance_status") != "verified"
            )
        ):
            turnover_exemptions[key] = "forced_risk_reduction"
        elif (
            item.get("origin_signal_type") == "PASSIVE_INDEX"
            and current_weight <= _TURNOVER_EPSILON
            and desired_weight > _TURNOVER_EPSILON
        ):
            turnover_exemptions[key] = "passive_initialization"

    turnover = cap_normal_rebalance_turnover(
        turnover_current_weights,
        desired_targets,
        exemptions_by_key=turnover_exemptions,
    )
    uncapped_targets = desired_targets
    targets = turnover.targets

    for item in legacy:
        key = security_key(item)
        actual_weight = float(
            turnover_current_weights.get(
                key,
                item.get("previous_weight") or 0.0,
            )
        )
        item["proposed_weight"] = round(targets.get(key, 0.0), 10)
        item["policy_action"] = _policy_action(
            actual_weight,
            float(item["proposed_weight"]),
            minimum_delta=_EXECUTION_DRIFT_PCT,
        )
    for item in verified:
        key = security_key(item)
        previous_target_weight = float(item.get("proposed_weight") or 0.0)
        actual_weight = float(turnover_current_weights.get(key, previous_target_weight))
        item["previous_weight"] = previous_target_weight
        item["proposed_weight"] = round(targets.get(key, 0.0), 10)
        item["allocation_method"] = "deterministic_risk_adjusted_v1"
        item["policy_action"] = _policy_action(
            actual_weight,
            item["proposed_weight"],
            minimum_delta=_EXECUTION_DRIFT_PCT,
        )
        item["policy_change_reason"] = "슬리브·품질·변동성·MDD·집중도 한도 적용"
        if abs(actual_weight - previous_target_weight) > _TURNOVER_EPSILON:
            item["policy_change_reason"] += " / 실제 NAV drift 교정"
        if abs(item["proposed_weight"] - uncapped_targets.get(key, 0.0)) > _TURNOVER_EPSILON:
            item["policy_change_reason"] += " / 일반 거래 총 명목 15% 회전율 상한 적용"
    active = []
    closed = payload["closed_positions"]
    for item in payload["portfolio"]:
        if (
            item.get("provenance_status") == "verified"
            and float(item.get("proposed_weight") or 0.0) <= 1e-9
        ):
            key = security_key(item)
            if (
                float(turnover_current_weights.get(key, 0.0)) > _TURNOVER_EPSILON
                or preexisting_by_key.get(key, False)
            ):
                closed_item = deepcopy(item)
                closed_item.update({
                    "action": "매도",
                    "proposed_weight": 0.0,
                    "closed_date": as_of_date or str(item.get("decision_date")),
                    "close_reason": item.get("policy_change_reason") or "정책 목표 0%",
                    "closed_performance": None,
                })
                closed.append(closed_item)
            continue
        active.append(item)
    payload["portfolio"] = active
    state = parse_portfolio_state(payload)
    summary = {
        "policy": "20/40/20/20",
        "legacy_reserved_weight": round(legacy_weight, 10),
        "legacy_raw_weight": round(legacy_raw_total, 10),
        "verified_capacity": round(verified_capacity, 10),
        "risk_scale": effective_risk_scale,
        "targets": {
            security_key(item): float(item.get("proposed_weight") or 0.0)
            for item in state.portfolio
        },
        "cash_weight": round(100.0 - sum(
            float(item.get("proposed_weight") or 0.0) for item in state.portfolio
        ), 10),
        "raw_turnover": turnover.raw_turnover,
        "applied_turnover": turnover.applied_turnover,
        "raw_normal_turnover": turnover.raw_normal_turnover,
        "applied_normal_turnover": turnover.applied_normal_turnover,
        "turnover_exempt": turnover.exempt_turnover,
        "turnover_exempt_by_reason": turnover.exempt_turnover_by_reason,
        "turnover_cap": turnover.cap,
        "turnover_capped": turnover.capped,
        "rejected": allocation.rejected,
    }
    return state, summary


def _is_explicit_thesis_invalidation(item: Mapping[str, Any]) -> bool:
    """Recognize only an explicit structured flag or unambiguous reason text."""

    if item.get("invalidation_triggered") is True:
        return True
    status = str(item.get("thesis_status") or "").strip().lower()
    if status in {"invalidated", "invalidation_triggered"}:
        return True
    reason = " ".join(
        str(item.get(field) or "")
        for field in ("close_reason", "change_reason", "policy_change_reason")
    ).lower()
    return any(marker in reason for marker in ("무효", "훼손", "논지 소멸", "thesis invalidat"))
