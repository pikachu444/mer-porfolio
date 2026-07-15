"""Minimal runtime checks between validated LLM decisions and saved state."""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any, Mapping

from portfolio_allocator import cap_target_weight, target_weight_cap
from portfolio_schema import (
    AnalysisDecisionV2,
    PortfolioStateV2,
    normalize_security_code,
    parse_portfolio_state,
)


class PortfolioPolicyBlocked(ValueError):
    """Raised when a run cannot safely apply its validated decisions."""


_EPSILON = 1e-9


def security_key(item: Mapping[str, Any]) -> str:
    market = str(item.get("market") or "").strip().upper()
    normalized_code = normalize_security_code(
        item.get("name"), market, item.get("code")
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
    """A rebalance must still review every current holding once."""

    if analysis.run_type != "rebalance":
        return
    expected = {security_key(item) for item in state.portfolio}
    covered = {security_key(item) for item in analysis.portfolio_decisions}
    missing = sorted(expected - covered)
    if missing:
        raise PortfolioPolicyBlocked(
            "rebalance did not review every current holding: " + ", ".join(missing)
        )


def _weight(value: Any, path: str) -> float:
    try:
        weight = float(value)
    except (TypeError, ValueError) as exc:
        raise PortfolioPolicyBlocked(f"{path} must be numeric") from exc
    if not math.isfinite(weight) or weight < 0:
        raise PortfolioPolicyBlocked(f"{path} must be a finite non-negative number")
    return weight


def allocate_projected_state(
    projected: PortfolioStateV2,
    *,
    as_of_date: str | None = None,
) -> tuple[PortfolioStateV2, dict[str, Any]]:
    """Keep LLM targets, cap each position, and leave the remainder as cash.

    ``as_of_date`` is retained for the runtime call contract; it is not an
    allocation input.  When the capped total exceeds 100%, every target is
    proportionally reduced and the adjustment is recorded in the summary.
    """

    _ = as_of_date
    payload = deepcopy(projected.to_dict())
    capped: list[dict[str, Any]] = []
    for item in payload["portfolio"]:
        proposed = _weight(item.get("proposed_weight"), "proposed_weight")
        final_weight = cap_target_weight(item, proposed)
        if final_weight < proposed - _EPSILON:
            capped.append({
                "key": security_key(item),
                "from": proposed,
                "to": final_weight,
                "cap": target_weight_cap(item),
            })
        item["proposed_weight"] = round(final_weight, 10)

    total = sum(float(item["proposed_weight"]) for item in payload["portfolio"])
    normalized = False
    if total > 100.0 + _EPSILON:
        scale = 100.0 / total
        for item in payload["portfolio"]:
            item["proposed_weight"] = round(float(item["proposed_weight"]) * scale, 10)
        total = sum(float(item["proposed_weight"]) for item in payload["portfolio"])
        normalized = True

    state = parse_portfolio_state(payload)
    active_weight = sum(float(item["proposed_weight"]) for item in state.portfolio)
    return state, {
        "policy": "llm_proposal_with_position_caps",
        "active_weight": round(active_weight, 10),
        "cash_weight": round(max(0.0, 100.0 - active_weight), 10),
        "capped_positions": capped,
        "normalized_to_100": normalized,
    }
