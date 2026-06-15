"""사용자 출력용 단일 기준 자료를 만드는 모듈."""

from __future__ import annotations

from datetime import datetime
from typing import Any

DEFENSIVE_CASH_TARGET = 20.0

ALLOCATION_ROLE_LABELS = {
    "core": "핵심",
    "satellite": "위성",
    "risk": "위험자산",
    "defensive": "방어",
    "watch": "관찰",
}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _code(value: Any) -> str:
    return str(value or "").strip().upper()


def _is_domestic(item: dict) -> bool:
    return _code(item.get("market")).startswith("KR")


def _actor_label(item: dict) -> str:
    actor = item.get("decision_actor")
    if actor == "메르":
        return "메르 직접 발언"
    if actor == "AI":
        return "AI 제안"
    return "미분류"


def _action_label(item: dict) -> str:
    return str(item.get("action") or "보유")


def _allocation_role_label(item: dict) -> str:
    role = str(item.get("allocation_role") or "").strip()
    return ALLOCATION_ROLE_LABELS.get(role, "역할 미지정")


def _review_required(item: dict) -> bool:
    if item.get("decision_actor") == "미분류":
        return True
    reason = str(item.get("change_reason") or "")
    return "기존 상태 마이그레이션" in reason


def _review_reason(item: dict) -> str:
    if item.get("decision_actor") == "미분류":
        return "판단 주체가 미분류라 다음 리밸런싱에서 유지 근거 재검증 필요"
    if "기존 상태 마이그레이션" in str(item.get("change_reason") or ""):
        return "기존 상태 마이그레이션 비중이라 다음 리밸런싱에서 재검증 필요"
    return ""


def _return_value(row: dict | None) -> float | None:
    if not row:
        return None
    value = row.get("return_pct_krw")
    if value is None:
        value = row.get("return_pct")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _return_label(value: float | None) -> str:
    if value is None:
        return "집계 전"
    return f"{value:+.1f}%"


def _performance_by_code(performance: dict | None) -> dict[str, dict]:
    result: dict[str, dict] = {}
    if not performance:
        return result
    for row in performance.get("active_positions", []) or []:
        for key in (row.get("code"), row.get("ticker")):
            code = _code(key)
            if code:
                result[code] = row
                if code.endswith(".KS") or code.endswith(".KQ"):
                    result[code.split(".", 1)[0]] = row
    return result


def _item_code_set(items: list[dict]) -> set[str]:
    return {_code(item.get("code")) for item in items if _code(item.get("code"))}


def _closed_positions_for_output(state: dict, performance: dict | None, current_codes: set[str]) -> list[dict]:
    closed: list[dict] = []
    seen: set[str] = set()
    for source in (
        state.get("closed_positions", []) or [],
        (performance or {}).get("closed_positions", []) or [],
    ):
        for item in source:
            code = _code(item.get("code"))
            if code and code in current_codes:
                continue
            key = code or f"{item.get('market', '')}:{item.get('name', '')}"
            if key in seen:
                continue
            seen.add(key)
            closed.append(dict(item))
    return closed


def build_output_model(
    state: dict,
    performance: dict | None = None,
    *,
    today_str: str = "",
    status_note: str = "",
) -> dict:
    """현재 상태를 기준으로 Telegram, HTML, Markdown이 함께 쓸 자료를 만든다."""
    performance = performance or {}
    active_by_code = _performance_by_code(performance)
    portfolio = []
    missing_return_codes: list[str] = []
    current_codes = _item_code_set(state.get("portfolio", []) or [])

    for item in state.get("portfolio", []) or []:
        row = dict(item)
        perf_row = active_by_code.get(_code(item.get("code")))
        return_value = _return_value(perf_row)
        if perf_row:
            for key in ("return_pct", "return_pct_krw", "entry_date", "current_price"):
                if key in perf_row:
                    row[key] = perf_row[key]
        else:
            missing_return_codes.append(str(item.get("code") or item.get("name") or ""))
        row["actor_label"] = _actor_label(item)
        row["action_label"] = _action_label(item)
        row["decision_label"] = f"{row['actor_label']} · {row['action_label']}"
        row["allocation_role_label"] = _allocation_role_label(row)
        row["review_required"] = _review_required(row)
        row["review_reason"] = _review_reason(row)
        row["return_value"] = return_value
        row["return_label"] = _return_label(return_value)
        row["weight"] = _as_float(item.get("proposed_weight"))
        portfolio.append(row)

    recommendation_rows = [item for item in portfolio if not item.get("review_required")]
    domestic = [item for item in recommendation_rows if _is_domestic(item)]
    overseas = [item for item in recommendation_rows if not _is_domestic(item)]
    review_required_positions = [item for item in portfolio if item.get("review_required")]
    cash_weight = max(0.0, 100.0 - sum(item["weight"] for item in portfolio))
    stock_weight = sum(item["weight"] for item in portfolio if item.get("asset_type") == "stock")
    etf_weight = sum(item["weight"] for item in portfolio if item.get("asset_type") == "etf")
    risk_weight = sum(item["weight"] for item in portfolio if item.get("allocation_role") == "risk")
    defensive_weight = cash_weight + sum(
        item["weight"]
        for item in portfolio
        if item.get("allocation_role") == "defensive"
    )
    chart_rows = [
        {
            "name": item.get("name", ""),
            "code": item.get("code", ""),
            "weight": item["weight"],
            "actor": item.get("decision_actor", ""),
            "action": item.get("action", ""),
            "reason": item.get("change_reason", ""),
            "market": item.get("market", ""),
            "allocation_role": item.get("allocation_role", ""),
        }
        for item in portfolio
        if item.get("weight", 0) > 0
    ]
    if cash_weight:
        chart_rows.append({
            "name": "현금",
            "code": "",
            "weight": cash_weight,
            "actor": "",
            "action": "보유",
            "reason": "",
            "market": "CASH",
        })

    portfolio_return = performance.get("portfolio_return_krw")
    portfolio_return_value: float | None = None
    if portfolio and not missing_return_codes:
        try:
            portfolio_return_value = float(portfolio_return)
        except (TypeError, ValueError):
            portfolio_return_value = None

    return {
        "today": today_str,
        "status_note": status_note or state.get("status_note", ""),
        "portfolio": portfolio,
        "domestic": domestic,
        "overseas": overseas,
        "watchlist": state.get("watchlist", []) or [],
        "closed_positions": _closed_positions_for_output(state, performance, current_codes),
        "review_required_positions": review_required_positions,
        "decision_history": state.get("decision_history", []) or [],
        "insights": state.get("insights", []) or [],
        "deferred_posts": state.get("deferred_posts", []) or [],
        "chart_rows": chart_rows,
        "cash_weight": cash_weight,
        "stock_weight": stock_weight,
        "etf_weight": etf_weight,
        "risk_weight": risk_weight,
        "defensive_weight": defensive_weight,
        "defensive_cash_target": DEFENSIVE_CASH_TARGET,
        "defensive_alert": cash_weight < DEFENSIVE_CASH_TARGET,
        "performance": performance,
        "portfolio_return_value": portfolio_return_value,
        "portfolio_return_label": _return_label(portfolio_return_value),
        "missing_return_codes": missing_return_codes,
    }


def _date_for_title(today_str: str) -> str:
    if not today_str:
        return datetime.now().strftime("%Y-%m-%d")
    return today_str


def _table(rows: list[dict], *, include_return: bool = True) -> list[str]:
    headers = ["종목", "코드", "판단", "역할", "목표비중", "근거"]
    if include_return:
        headers.append("수익률")
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    if not rows:
        empty = ["표시할 항목 없음", "", "", "", "", ""]
        if include_return:
            empty.append("")
        lines.append("| " + " | ".join(empty) + " |")
        return lines
    for item in rows:
        values = [
            str(item.get("name", "")),
            str(item.get("code", "")),
            str(item.get("decision_label") or f"{_actor_label(item)} · {_action_label(item)}"),
            str(item.get("allocation_role_label") or _allocation_role_label(item)),
            f"{_as_float(item.get('proposed_weight', item.get('weight'))):g}%",
            str(item.get("change_reason") or item.get("observation_reason") or ""),
        ]
        if include_return:
            values.append(str(item.get("return_label", "집계 전")))
        lines.append("| " + " | ".join(value.replace("|", "/") for value in values) + " |")
    return lines


def build_markdown_report(output: dict) -> str:
    """단일 기준 자료에서 사용자용 Markdown 보고서를 만든다."""
    today = _date_for_title(output.get("today", ""))
    lines = [
        "# 메르AI 모델 포트폴리오 리포트",
        "",
        f"- 기준일: {today}",
        "- 메르 블로거의 실제 보유 내역이 아닙니다.",
        "- 블로그 직접 판단과 AI 해석을 구분해 표시합니다.",
        f"- 모델 포트폴리오 수익률: {output.get('portfolio_return_label', '집계 전')}",
        f"- 주식 노출: {output.get('stock_weight', 0):g}%",
        f"- 현금성 비중: {output.get('cash_weight', 0):g}% (방어 기준 {output.get('defensive_cash_target', 20):g}%)",
    ]
    if output.get("defensive_alert"):
        lines.append("- 방어 기준 미달: 현금성 비중이 20% 아래라 다음 리밸런싱에서 방어 비중 재검토 필요")
    if output.get("status_note"):
        lines.append(f"- 분석 보류: {output['status_note']}")
    if output.get("missing_return_codes"):
        joined = ", ".join(output["missing_return_codes"])
        lines.append(f"- 수익률 집계 전 종목: {joined}")

    deferred_posts = output.get("deferred_posts", [])
    if deferred_posts:
        lines += [
            "",
            "## 분석 보류 글",
            "",
            "| 제목 | 날짜 | 사유 | URL |",
            "| --- | --- | --- | --- |",
        ]
        for item in deferred_posts:
            title = str(item.get("title", "제목 없음")).replace("|", "/")
            date = str(item.get("date", "")).replace("|", "/")
            reason = str(item.get("reason", "")).replace("|", "/")
            url = str(item.get("url", "")).replace("|", "/")
            lines.append(f"| {title} | {date} | {reason} | {url} |")
        lines += [
            "",
            "위 글은 요약이 준비되지 않아 이번 투자 판단에서 제외됐고 다음 실행에서 다시 확인합니다.",
        ]

    lines += ["", "## 핵심 인사이트"]
    insights = output.get("insights", [])
    if insights:
        for index, item in enumerate(insights, start=1):
            lines += [
                "",
                f"### 인사이트 {index}: {item.get('title', '')}",
                "",
                str(item.get("summary", "")),
                "",
                f"**투자판단:** {item.get('investment_implication', '')}",
            ]
    else:
        lines += ["", "표시할 인사이트가 없습니다."]

    lines += ["", "## 포트폴리오 추천", "", "### 국내주식 추천"]
    lines += _table(output.get("domestic", []))
    lines += ["", "### 해외주식 추천"]
    lines += _table(output.get("overseas", []))

    lines += ["", "## Watchlist"]
    watchlist = output.get("watchlist", [])
    if watchlist:
        lines += [
            "| 대상 | 판단 | 상태 | 근거 |",
            "| --- | --- | --- | --- |",
        ]
        for item in watchlist:
            lines.append(
                "| "
                + " | ".join([
                    str(item.get("name", "")),
                    f"{_actor_label(item)} · {item.get('basis', '')}",
                    str(item.get("status", "")),
                    str(item.get("observation_reason") or item.get("change_reason") or ""),
                ])
                + " |"
            )
    else:
        lines.append("표시할 항목이 없습니다.")

    lines += ["", "## 재검증 필요 포지션"]
    review_required = output.get("review_required_positions", [])
    if review_required:
        lines += [
            "| 종목 | 코드 | 현재비중 | 재검증 사유 |",
            "| --- | --- | --- | --- |",
        ]
        for item in review_required:
            lines.append(
                "| "
                + " | ".join([
                    str(item.get("name", "")),
                    str(item.get("code", "")),
                    f"{_as_float(item.get('weight', item.get('proposed_weight'))):g}%",
                    str(item.get("review_reason", "")),
                ])
                + " |"
            )
    else:
        lines.append("표시할 항목이 없습니다.")

    lines += ["", "## 종료 포지션"]
    closed = output.get("closed_positions", [])
    if closed:
        lines += [
            "| 종목 | 코드 | 종료일 | 종료 사유 |",
            "| --- | --- | --- | --- |",
        ]
        for item in closed:
            lines.append(
                "| "
                + " | ".join([
                    str(item.get("name", "")),
                    str(item.get("code", "")),
                    str(item.get("closed_date", "")),
                    str(item.get("close_reason") or item.get("change_reason") or ""),
                ])
                + " |"
            )
    else:
        lines.append("표시할 항목이 없습니다.")

    lines += [
        "",
        "## 한 줄 코멘트",
        "",
        "> 이 보고서는 현재 모델 포트폴리오 상태를 기준으로 작성되며, 분석 보류 항목은 다음 실행에서 다시 확인합니다.",
        "",
    ]
    return "\n".join(lines)
