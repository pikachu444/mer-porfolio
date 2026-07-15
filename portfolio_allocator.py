"""Small, explicit weight guardrails for the model portfolio.

The LLM proposes target weights from the article evidence.  This module does
not rank signals, create sleeves, or redistribute capital; it only applies the
two published per-position ceilings.
"""

from __future__ import annotations

from typing import Any


STOCK_TARGET_WEIGHT_CAP = 10.0
SECTOR_ETF_TARGET_WEIGHT_CAP = 30.0


def target_weight_cap(item: dict[str, Any]) -> float | None:
    """Return the applicable target-weight ceiling, if any.

    The existing schema calls sector and industry ETFs ``etf``.  Broad-market
    index ETFs are not created by the system and therefore receive no special
    treatment here.
    """

    asset_type = str(item.get("asset_type") or "").strip().lower()
    if asset_type == "stock":
        return STOCK_TARGET_WEIGHT_CAP
    if asset_type in {"etf", "sector_etf"}:
        return SECTOR_ETF_TARGET_WEIGHT_CAP
    return None


def cap_target_weight(item: dict[str, Any], proposed_weight: float) -> float:
    """Apply only the asset-type ceiling to an LLM-proposed target weight."""

    cap = target_weight_cap(item)
    return min(float(proposed_weight), cap) if cap is not None else float(proposed_weight)
