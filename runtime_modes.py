"""Execution-mode policy for local runs and GitHub Actions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


REBALANCE_INTERVAL_DAYS = 14


@dataclass(frozen=True)
class RunPolicy:
    mode: str
    fetch_days: int
    persist_operating_state: bool
    send_telegram: bool
    use_fixture: bool
    upload_artifact: bool


POLICIES = {
    "scheduled": RunPolicy("scheduled", 2, True, True, False, False),
    "rebalance": RunPolicy("rebalance", 14, True, True, False, False),
    "verify": RunPolicy("verify", 14, False, True, False, True),
    "test": RunPolicy("test", 3, False, False, True, False),
}


def get_run_policy(mode: str) -> RunPolicy:
    try:
        return POLICIES[mode.lower()]
    except KeyError as exc:
        raise ValueError(f"unknown RUN_MODE: {mode}") from exc


def should_rebalance(mode: str, last_rebalanced_date: str | None, today: date) -> bool:
    """Return whether this run should reevaluate the complete target allocation."""
    policy = get_run_policy(mode)
    if policy.mode in {"rebalance", "verify"}:
        return True
    if policy.mode == "test":
        return False
    if last_rebalanced_date is None:
        return True
    previous = date.fromisoformat(last_rebalanced_date)
    return (today - previous).days >= REBALANCE_INTERVAL_DAYS
