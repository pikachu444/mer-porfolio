"""Domain rules for turning an approved target into today's user-facing action.

The LLM's historical ``action`` field describes how a decision was created.  It
is deliberately not used as an order signal.  This module compares the latest
approved target with the latest measured weight and applies the same explicit
drift policy everywhere (Telegram, Markdown, and the dashboard).
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or not str(value).strip():
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _env_bool(name: str, default: bool) -> bool:
    value = str(os.environ.get(name, "")).strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class RebalancePolicy:
    """Operational thresholds, expressed in percentage points/model units.

    The defaults retain the existing 0.50 percentage-point drift guard and add
    a matching minimum trade threshold so small price noise cannot produce a
    new recommendation.  Every setting is overridable for a deployment or a
    test; the model never silently falls back to an LLM action.
    """

    rebalance_band_pct: float = 0.50
    minimum_trade_weight_pct: float = 0.50
    minimum_trade_amount: float = 0.50
    cash_buffer: float = 20.0
    allow_fractional_shares: bool = True
    market_open_check: bool = False
    stale_price_threshold_days: int = 3

    @classmethod
    def from_environment(cls) -> "RebalancePolicy":
        stale = int(_env_float("STALE_PRICE_THRESHOLD_DAYS", 3.0))
        return cls(
            rebalance_band_pct=_env_float("REBALANCE_BAND_PCT", cls.rebalance_band_pct),
            minimum_trade_weight_pct=_env_float(
                "MINIMUM_TRADE_WEIGHT_PCT", cls.minimum_trade_weight_pct
            ),
            minimum_trade_amount=_env_float(
                "MINIMUM_TRADE_AMOUNT", cls.minimum_trade_amount
            ),
            cash_buffer=_env_float("CASH_BUFFER_PCT", cls.cash_buffer),
            allow_fractional_shares=_env_bool("ALLOW_FRACTIONAL_SHARES", cls.allow_fractional_shares),
            market_open_check=_env_bool("MARKET_OPEN_CHECK", cls.market_open_check),
            stale_price_threshold_days=max(0, stale),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def derive_today_action(
    item: dict[str, Any],
    actual_weight: float | None,
    *,
    portfolio_value: float = 100.0,
    policy: RebalancePolicy | None = None,
    approved_order: bool = False,
) -> dict[str, Any]:
    """Return a fresh action based only on approved target and actual weight.

    ``actual_weight`` is intentionally allowed to be ``None``.  Missing data
    becomes ``데이터 없음`` instead of pretending the target is the holding.
    This function returns review language because this project creates a model
    portfolio and does not submit broker orders.
    """

    policy = policy or RebalancePolicy.from_environment()
    target = _number(item.get("proposed_weight"))
    if target is None:
        target = _number(item.get("target_weight"))
    if target is None:
        target = 0.0

    result: dict[str, Any] = {
        "target_weight": target,
        "actual_weight": actual_weight,
        "difference_pct": None,
        "estimated_trade_amount": None,
        "today_action": "데이터 없음",
        "today_action_reason": "실제 비중 데이터 없음",
        "approved_order": bool(approved_order),
    }
    if actual_weight is None:
        return result

    actual = float(actual_weight)
    difference = target - actual
    estimated_amount = abs(difference) / 100.0 * max(float(portfolio_value), 0.0)
    result["actual_weight"] = actual
    result["difference_pct"] = difference
    result["estimated_trade_amount"] = estimated_amount

    within_band = abs(difference) <= policy.rebalance_band_pct + 1e-9
    below_weight_minimum = abs(difference) < policy.minimum_trade_weight_pct - 1e-9
    below_amount_minimum = estimated_amount < policy.minimum_trade_amount - 1e-9
    if within_band or below_weight_minimum or below_amount_minimum:
        result["today_action"] = "유지"
        result["today_action_reason"] = "리밸런싱 허용 범위 또는 최소 조정 기준 안"
        return result

    if target <= 1e-9 and actual > 1e-9:
        action = "편출 검토"
    elif difference > 0:
        action = "비중확대 검토"
    else:
        action = "비중축소 검토"
    result["today_action"] = action
    result["today_action_reason"] = (
        f"목표 {target:.2f}%와 실제 {actual:.2f}%의 차이 {difference:+.2f}%p"
    )
    return result


def derive_portfolio_actions(
    rows: list[dict[str, Any]],
    *,
    policy: RebalancePolicy | None = None,
    portfolio_value: float = 100.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Annotate rows and return ``(all_rows, changed_rows)``."""

    policy = policy or RebalancePolicy.from_environment()
    annotated: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    for row in rows:
        result = derive_today_action(
            row,
            row.get("actual_weight"),
            portfolio_value=portfolio_value,
            policy=policy,
        )
        updated = dict(row)
        updated.update(result)
        annotated.append(updated)
        if updated["today_action"] not in {"유지", "데이터 없음"}:
            changed.append(updated)
    return annotated, changed
