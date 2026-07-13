"""Deterministic portfolio allocation policy.

The allocator deliberately does not inspect ``decision_actor``.  A position's
sleeve is derived from the immutable signal ``origin`` so that a later AI review
cannot turn a Mer signal into an AI signal (or vice versa).

Percentages returned by this module use percentage points.  Volatility and
drawdown inputs use decimal fractions (``0.12`` means 12%).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


PASSIVE_INDEX = "passive_index"
MER_DIRECT = "mer_direct"
AI_INFERRED = "ai_inferred"
CASH = "cash"

_ORIGIN_TO_SLEEVE = {
    "PASSIVE_INDEX": PASSIVE_INDEX,
    "PASSIVE": PASSIVE_INDEX,
    "INDEX": PASSIVE_INDEX,
    "MER_DIRECT": MER_DIRECT,
    "MER_THESIS": MER_DIRECT,
    "MER": MER_DIRECT,
    "AI_INFERRED": AI_INFERRED,
    "AI": AI_INFERRED,
}

_QUALITY_WEIGHTS = {
    "explicitness": 0.30,
    "causality": 0.20,
    "catalyst": 0.15,
    "confirmation": 0.15,
    "invalidation": 0.10,
    "recency": 0.10,
}

_EPSILON = 1e-9


@dataclass(frozen=True)
class AllocationPolicy:
    """Portfolio policy expressed in percentage points."""

    sleeve_budgets: Mapping[str, float] = field(
        default_factory=lambda: {
            PASSIVE_INDEX: 20.0,
            MER_DIRECT: 40.0,
            AI_INFERRED: 20.0,
        }
    )
    base_cash_weight: float = 20.0
    mer_min_quality: float = 0.60
    ai_min_quality: float = 0.70
    mer_security_cap: float = 5.0
    ai_security_cap: float = 2.0
    issuer_cap: float = 5.0
    theme_cap: float = 15.0
    country_cap: float = 55.0
    normal_rebalance_turnover_cap: float = 15.0
    target_active_volatility: float = 0.12
    volatility_floor: float = 0.10

    def __post_init__(self) -> None:
        budget_total = self.base_cash_weight + sum(self.sleeve_budgets.values())
        if abs(budget_total - 100.0) > _EPSILON:
            raise ValueError("sleeve budgets and base cash weight must total 100")
        if self.target_active_volatility <= 0 or self.volatility_floor <= 0:
            raise ValueError("volatility settings must be positive")
        if self.normal_rebalance_turnover_cap < 0:
            raise ValueError("normal_rebalance_turnover_cap must not be negative")


DEFAULT_POLICY = AllocationPolicy()


@dataclass(frozen=True)
class AllocationResult:
    targets: dict[str, float]
    cash_weight: float
    sleeve_weights: dict[str, float]
    risk_scale: float
    quality_scores: dict[str, float]
    rejected: dict[str, str]

    def target_for(self, key: str) -> float:
        return self.targets.get(key, 0.0)


def sleeve_for_origin(origin: Any) -> str | None:
    """Return a sleeve using signal origin, never the latest decision actor."""

    normalized = str(origin or "").strip().upper()
    return _ORIGIN_TO_SLEEVE.get(normalized)


def compute_signal_quality(components: Mapping[str, Any]) -> float:
    """Compute the agreed six-component signal quality score.

    Every component is required and must be in the closed interval [0, 1].
    Failing closed prevents an incomplete LLM response from receiving capital.
    """

    missing = [name for name in _QUALITY_WEIGHTS if name not in components]
    if missing:
        raise ValueError("missing quality components: " + ", ".join(missing))

    score = 0.0
    for name, weight in _QUALITY_WEIGHTS.items():
        try:
            value = float(components[name])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"quality component {name} must be numeric") from exc
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"quality component {name} must be between 0 and 1")
        score += value * weight
    return score


def drawdown_risk_scale(max_drawdown: float) -> float:
    """Return the immediate MDD scale; recovery hysteresis is stateful upstream."""

    drawdown = abs(float(max_drawdown))
    if drawdown >= 0.15:
        return 0.25
    if drawdown >= 0.125:
        return 0.50
    if drawdown >= 0.10:
        return 0.75
    return 1.0


def portfolio_risk_scale(
    annualized_volatility: float | None,
    max_drawdown: float,
    *,
    policy: AllocationPolicy = DEFAULT_POLICY,
) -> float:
    """Combine the 12% active-volatility target and MDD risk ladder."""

    volatility_scale = 1.0
    if annualized_volatility is not None:
        volatility = float(annualized_volatility)
        if volatility <= 0:
            raise ValueError("annualized_volatility must be positive when provided")
        volatility_scale = min(1.0, policy.target_active_volatility / volatility)
    return min(volatility_scale, drawdown_risk_scale(max_drawdown))


def allocate_portfolio(
    candidates: Iterable[Mapping[str, Any]],
    *,
    portfolio_volatility: float | None = None,
    max_drawdown: float = 0.0,
    policy: AllocationPolicy = DEFAULT_POLICY,
) -> AllocationResult:
    """Allocate candidates under the 20/40/20/20 policy and exposure caps.

    Required candidate fields:

    * all sleeves: ``key``, ``origin``, and ``country_code``;
    * active sleeves: ``quality_components``, ``annualized_volatility``,
      ``issuer_id``, and non-empty ``theme_ids``.

    Passive candidates may set ``fixed_weight`` to express their relative split.
    Unallocatable budget remains cash; it is never transferred to another sleeve.
    """

    ordered = sorted((dict(item) for item in candidates), key=_candidate_key)
    duplicate_keys = _duplicates([_candidate_key(item) for item in ordered])
    if duplicate_keys:
        raise ValueError("duplicate candidate keys: " + ", ".join(duplicate_keys))

    scale = portfolio_risk_scale(
        portfolio_volatility,
        max_drawdown,
        policy=policy,
    )
    rejected: dict[str, str] = {}
    quality_scores: dict[str, float] = {}
    eligible: dict[str, list[dict[str, Any]]] = {
        PASSIVE_INDEX: [],
        MER_DIRECT: [],
        AI_INFERRED: [],
    }

    for item in ordered:
        key = _candidate_key(item)
        sleeve = sleeve_for_origin(
            item.get("origin_signal_type", item.get("origin"))
        )
        if sleeve is None:
            rejected[key] = "unsupported signal origin"
            continue
        country = str(item.get("country_code") or "").strip().upper()
        if not country:
            rejected[key] = "missing country_code"
            continue
        item["_country"] = country
        item["_sleeve"] = sleeve

        if sleeve == PASSIVE_INDEX:
            try:
                score = float(item.get("fixed_weight", 1.0))
            except (TypeError, ValueError):
                score = 0.0
            if score <= 0:
                rejected[key] = "fixed_weight must be positive"
                continue
            item["_allocation_score"] = score
            eligible[sleeve].append(item)
            continue

        issuer = str(item.get("issuer_id") or "").strip().upper()
        themes = sorted({str(value).strip().upper() for value in item.get("theme_ids", []) if str(value).strip()})
        if not issuer:
            rejected[key] = "missing issuer_id"
            continue
        if not themes:
            rejected[key] = "missing theme_ids"
            continue
        try:
            quality = compute_signal_quality(item.get("quality_components") or {})
        except ValueError as exc:
            rejected[key] = str(exc)
            continue
        threshold = policy.mer_min_quality if sleeve == MER_DIRECT else policy.ai_min_quality
        if quality + _EPSILON < threshold:
            rejected[key] = f"quality below {threshold:.2f}"
            continue
        try:
            volatility = float(item.get("annualized_volatility"))
        except (TypeError, ValueError):
            rejected[key] = "missing annualized_volatility"
            continue
        if volatility <= 0:
            rejected[key] = "annualized_volatility must be positive"
            continue

        item["_issuer"] = issuer
        item["_themes"] = themes
        item["_allocation_score"] = quality / max(volatility, policy.volatility_floor)
        quality_scores[key] = quality
        eligible[sleeve].append(item)

    targets: dict[str, float] = {}
    issuer_used: dict[str, float] = {}
    theme_used: dict[str, float] = {}
    country_used: dict[str, float] = {}

    # Passive country exposure is counted first.  Mer then receives priority over
    # AI when a cross-sleeve country/issuer/theme cap is scarce.
    for sleeve in (PASSIVE_INDEX, MER_DIRECT, AI_INFERRED):
        budget = float(policy.sleeve_budgets[sleeve])
        if sleeve in {MER_DIRECT, AI_INFERRED}:
            budget *= scale
        allocated = _allocate_sleeve(
            eligible[sleeve],
            budget,
            sleeve=sleeve,
            policy=policy,
            targets=targets,
            issuer_used=issuer_used,
            theme_used=theme_used,
            country_used=country_used,
        )
        for key, weight in allocated.items():
            targets[key] = targets.get(key, 0.0) + weight

    targets = {
        key: round(weight, 10)
        for key, weight in sorted(targets.items())
        if weight > _EPSILON
    }
    sleeve_weights = {
        sleeve: round(
            sum(targets.get(_candidate_key(item), 0.0) for item in eligible[sleeve]),
            10,
        )
        for sleeve in (PASSIVE_INDEX, MER_DIRECT, AI_INFERRED)
    }
    invested = sum(targets.values())
    cash_weight = round(max(0.0, 100.0 - invested), 10)
    sleeve_weights[CASH] = cash_weight
    return AllocationResult(
        targets=targets,
        cash_weight=cash_weight,
        sleeve_weights=sleeve_weights,
        risk_scale=scale,
        quality_scores=dict(sorted(quality_scores.items())),
        rejected=dict(sorted(rejected.items())),
    )


def _allocate_sleeve(
    candidates: list[dict[str, Any]],
    budget: float,
    *,
    sleeve: str,
    policy: AllocationPolicy,
    targets: dict[str, float],
    issuer_used: dict[str, float],
    theme_used: dict[str, float],
    country_used: dict[str, float],
) -> dict[str, float]:
    allocated = {_candidate_key(item): 0.0 for item in candidates}
    remaining = max(0.0, budget)

    for _ in range(max(1, len(candidates) * 4 + 4)):
        if remaining <= _EPSILON:
            break
        active = [
            item
            for item in candidates
            if _candidate_capacity(
                item,
                sleeve=sleeve,
                policy=policy,
                current=allocated[_candidate_key(item)],
                issuer_used=issuer_used,
                theme_used=theme_used,
                country_used=country_used,
            )
            > _EPSILON
        ]
        if not active:
            break
        score_total = sum(float(item["_allocation_score"]) for item in active)
        if score_total <= _EPSILON:
            break
        proposal = {
            _candidate_key(item): remaining * float(item["_allocation_score"]) / score_total
            for item in active
        }

        # Repeated scaling handles overlapping issuer/theme/country constraints.
        for _scaling_pass in range(12):
            changed = False
            for item in active:
                key = _candidate_key(item)
                capacity = _candidate_security_capacity(
                    sleeve,
                    policy,
                    allocated[key],
                )
                if proposal[key] > capacity + _EPSILON:
                    proposal[key] = max(0.0, capacity)
                    changed = True

            if sleeve != PASSIVE_INDEX:
                changed |= _scale_group_proposals(
                    active,
                    proposal,
                    group_value=lambda item: item["_issuer"],
                    used=issuer_used,
                    cap=policy.issuer_cap,
                )
                for theme in sorted({theme for item in active for theme in item["_themes"]}):
                    changed |= _scale_named_group(
                        active,
                        proposal,
                        predicate=lambda item, theme=theme: theme in item["_themes"],
                        capacity=max(0.0, policy.theme_cap - theme_used.get(theme, 0.0)),
                    )
            changed |= _scale_group_proposals(
                active,
                proposal,
                group_value=lambda item: item["_country"],
                used=country_used,
                cap=policy.country_cap,
            )
            if not changed:
                break

        consumed = sum(proposal.values())
        if consumed <= _EPSILON:
            break
        for item in active:
            key = _candidate_key(item)
            increment = proposal[key]
            if increment <= _EPSILON:
                continue
            allocated[key] += increment
            country = item["_country"]
            country_used[country] = country_used.get(country, 0.0) + increment
            if sleeve != PASSIVE_INDEX:
                issuer = item["_issuer"]
                issuer_used[issuer] = issuer_used.get(issuer, 0.0) + increment
                for theme in item["_themes"]:
                    theme_used[theme] = theme_used.get(theme, 0.0) + increment
        remaining -= consumed

    return allocated


def _candidate_capacity(
    item: Mapping[str, Any],
    *,
    sleeve: str,
    policy: AllocationPolicy,
    current: float,
    issuer_used: Mapping[str, float],
    theme_used: Mapping[str, float],
    country_used: Mapping[str, float],
) -> float:
    capacities = [
        _candidate_security_capacity(sleeve, policy, current),
        max(0.0, policy.country_cap - country_used.get(item["_country"], 0.0)),
    ]
    if sleeve != PASSIVE_INDEX:
        capacities.append(max(0.0, policy.issuer_cap - issuer_used.get(item["_issuer"], 0.0)))
        capacities.extend(
            max(0.0, policy.theme_cap - theme_used.get(theme, 0.0))
            for theme in item["_themes"]
        )
    return min(capacities)


def _candidate_security_capacity(
    sleeve: str,
    policy: AllocationPolicy,
    current: float,
) -> float:
    if sleeve == MER_DIRECT:
        return max(0.0, policy.mer_security_cap - current)
    if sleeve == AI_INFERRED:
        return max(0.0, policy.ai_security_cap - current)
    return float("inf")


def _scale_group_proposals(
    active: list[dict[str, Any]],
    proposal: dict[str, float],
    *,
    group_value,
    used: Mapping[str, float],
    cap: float,
) -> bool:
    changed = False
    groups = sorted({group_value(item) for item in active})
    for group in groups:
        changed |= _scale_named_group(
            active,
            proposal,
            predicate=lambda item, group=group: group_value(item) == group,
            capacity=max(0.0, cap - used.get(group, 0.0)),
        )
    return changed


def _scale_named_group(
    active: list[dict[str, Any]],
    proposal: dict[str, float],
    *,
    predicate,
    capacity: float,
) -> bool:
    keys = [_candidate_key(item) for item in active if predicate(item)]
    proposed = sum(proposal[key] for key in keys)
    if proposed <= capacity + _EPSILON:
        return False
    factor = 0.0 if capacity <= 0 else capacity / proposed
    for key in keys:
        proposal[key] *= factor
    return True


def _candidate_key(item: Mapping[str, Any]) -> str:
    key = str(item.get("key") or "").strip()
    if not key:
        raise ValueError("candidate key must not be empty")
    return key


def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(duplicates)
