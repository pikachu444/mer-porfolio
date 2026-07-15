"""Deterministic performance, benchmark, and drawdown calculations."""

from __future__ import annotations

import math
from statistics import mean, pstdev
from typing import Iterable, Sequence


BENCHMARK_WEIGHTS = {
    # Comparison only.  These are not model-portfolio allocations.
    "kospi200_tr": 0.50,
    "sp500_tr_krw": 0.50,
    "cash": 0.00,
}


def benchmark_returns(
    kospi200_returns: Sequence[float],
    sp500_krw_returns: Sequence[float],
    cash_returns: Sequence[float] | None = None,
) -> list[float]:
    if len(kospi200_returns) != len(sp500_krw_returns):
        raise ValueError("benchmark component return lengths must match")
    if cash_returns is None:
        cash_returns = [0.0] * len(kospi200_returns)
    if len(cash_returns) != len(kospi200_returns):
        raise ValueError("cash return length must match benchmark components")
    return [
        BENCHMARK_WEIGHTS["kospi200_tr"] * float(kr)
        + BENCHMARK_WEIGHTS["sp500_tr_krw"] * float(us)
        + BENCHMARK_WEIGHTS["cash"] * float(cash)
        for kr, us, cash in zip(kospi200_returns, sp500_krw_returns, cash_returns)
    ]


def cumulative_values(returns: Iterable[float], starting_value: float = 100.0) -> list[float]:
    if starting_value <= 0:
        raise ValueError("starting_value must be positive")
    values = [float(starting_value)]
    for value in returns:
        values.append(values[-1] * (1.0 + float(value)))
    return values


def max_drawdown(values: Sequence[float]) -> float | None:
    if not values:
        return None
    peak = float(values[0])
    if peak <= 0:
        raise ValueError("portfolio values must be positive")
    worst = 0.0
    for raw in values:
        value = float(raw)
        if value <= 0:
            raise ValueError("portfolio values must be positive")
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.0)
    return worst


def _annualized_ratio(returns: Sequence[float], downside_only: bool = False) -> float | None:
    if len(returns) < 2:
        return None
    values = [float(item) for item in returns]
    if downside_only:
        # Sortino uses downside deviation around the 0% daily target.  A
        # standard deviation of a zero-padded series understates downside risk
        # because it subtracts that series' negative mean first.
        deviation = math.sqrt(
            sum(min(0.0, item) ** 2 for item in values) / len(values)
        )
    else:
        deviation = pstdev(values)
    if deviation <= 1e-12:
        return None
    return mean(values) / deviation * math.sqrt(252.0)


def performance_metrics(
    portfolio_returns: Sequence[float],
    benchmark_daily_returns: Sequence[float] | None = None,
) -> dict[str, float | None]:
    values = [float(item) for item in portfolio_returns]
    portfolio_curve = cumulative_values(values)
    result: dict[str, float | None] = {
        "total_return": portfolio_curve[-1] / portfolio_curve[0] - 1.0,
        "annualized_volatility": (
            pstdev(values) * math.sqrt(252.0) if len(values) >= 2 else None
        ),
        "sharpe": _annualized_ratio(values),
        "sortino": _annualized_ratio(values, downside_only=True),
        "max_drawdown": max_drawdown(portfolio_curve),
        "benchmark_return": None,
        "excess_return": None,
        "information_ratio": None,
    }
    if benchmark_daily_returns is None:
        return result
    benchmark = [float(item) for item in benchmark_daily_returns]
    if len(benchmark) != len(values):
        raise ValueError("portfolio and benchmark return lengths must match")
    benchmark_curve = cumulative_values(benchmark)
    benchmark_return = benchmark_curve[-1] / benchmark_curve[0] - 1.0
    active = [portfolio - reference for portfolio, reference in zip(values, benchmark)]
    result["benchmark_return"] = benchmark_return
    result["excess_return"] = float(result["total_return"] or 0.0) - benchmark_return
    result["information_ratio"] = _annualized_ratio(active)
    return result
