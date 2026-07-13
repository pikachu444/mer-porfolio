"""사용자 출력용 단일 기준 자료를 만드는 모듈."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from portfolio_schema import normalize_security_code

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
    origin = str(item.get("origin_signal_type") or "").strip().upper()
    actor = item.get("decision_actor")
    if origin == "MER_DIRECT":
        return "메르 직접 신호 · AI 관리" if actor == "AI" else "메르 직접 신호"
    if origin == "MER_THESIS":
        return "메르 방향성 · AI 관리" if actor == "AI" else "메르 방향성"
    if origin == "AI_INFERRED":
        return "AI 추론"
    if origin == "LEGACY_UNVALIDATED" or item.get("provenance_status") == "legacy_unvalidated":
        return "검증 전 레거시"
    if actor == "메르":
        return "메르 직접 발언"
    if actor == "AI":
        return "AI 제안"
    return "미분류"


def _action_label(item: dict) -> str:
    return str(item.get("policy_action") or item.get("action") or "보유")


def _allocation_role_label(item: dict) -> str:
    role = str(item.get("allocation_role") or "").strip()
    return ALLOCATION_ROLE_LABELS.get(role, "역할 미지정")


def _review_required(item: dict) -> bool:
    if item.get("provenance_status") == "legacy_unvalidated":
        return True
    if item.get("decision_actor") == "미분류":
        return True
    reason = str(item.get("change_reason") or "")
    if "기존 상태 마이그레이션" in reason:
        return True
    if item.get("decision_actor") == "AI" and not str(item.get("allocation_role") or "").strip():
        return True
    return False


def _review_reason(item: dict) -> str:
    if item.get("provenance_status") == "legacy_unvalidated":
        return "원문 근거 신호와 연결되지 않은 과거 상태라 재검증 필요"
    if item.get("decision_actor") == "미분류":
        return "판단 주체가 미분류라 다음 리밸런싱에서 유지 근거 재검증 필요"
    if "기존 상태 마이그레이션" in str(item.get("change_reason") or ""):
        return "기존 상태 마이그레이션 비중이라 다음 리밸런싱에서 재검증 필요"
    if item.get("decision_actor") == "AI" and not str(item.get("allocation_role") or "").strip():
        return "AI 판단이지만 포트폴리오 역할이 없어 다음 리밸런싱에서 재검증 필요"
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


def _security_identity(item: dict) -> str:
    market = _code(item.get("market"))
    asset_type = str(item.get("asset_type") or "").strip().lower()
    code = normalize_security_code(item.get("name"), market, item.get("code"))
    if code:
        return f"{asset_type}:{market}:{code}"
    return f"{asset_type}:{market}:NAME:{str(item.get('name') or '').strip().lower()}"


def _performance_by_identity(performance: dict | None) -> tuple[dict[str, dict], dict[str, dict]]:
    by_identity: dict[str, dict] = {}
    by_code: dict[str, dict] = {}
    ambiguous_codes: set[str] = set()
    if not performance:
        return by_identity, by_code
    for row in performance.get("active_positions", []) or []:
        identity = str(row.get("key") or _security_identity(row)).strip()
        if identity:
            by_identity[identity] = row
        for key in (row.get("code"), row.get("ticker")):
            code = _code(key)
            if code:
                if code in by_code and by_code[code] is not row:
                    ambiguous_codes.add(code)
                by_code[code] = row
                if code.endswith(".KS") or code.endswith(".KQ"):
                    bare_code = code.split(".", 1)[0]
                    if bare_code in by_code and by_code[bare_code] is not row:
                        ambiguous_codes.add(bare_code)
                    by_code[bare_code] = row
    for code in ambiguous_codes:
        by_code.pop(code, None)
    return by_identity, by_code


def _closed_positions_for_output(state: dict, performance: dict | None) -> list[dict]:
    closed: list[dict] = []
    episode_index: dict[str, int] = {}
    for source in (
        state.get("closed_positions", []) or [],
        (performance or {}).get("closed_positions", []) or [],
    ):
        for item in source:
            # Re-entry starts a new episode; an old closed episode remains valid
            # even when the same security is active again.  Deduplicate only the
            # same episode copied from state and performance cache.
            key = str(item.get("position_episode_id") or "").strip() or ":".join((
                _security_identity(item),
                str(item.get("closed_date") or item.get("decision_date") or ""),
            ))
            if key in episode_index:
                existing = closed[episode_index[key]]
                existing.update({
                    field: value
                    for field, value in item.items()
                    if value is not None
                })
                continue
            episode_index[key] = len(closed)
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
    active_by_identity, active_by_code = _performance_by_identity(performance)
    portfolio = []
    missing_return_codes: list[str] = []

    for item in state.get("portfolio", []) or []:
        row = dict(item)
        perf_row = active_by_identity.get(_security_identity(item))
        if perf_row is None:
            # Older performance caches did not carry market/asset identity.
            # Code-only fallback is accepted only when the code is unambiguous.
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
        row["target_weight"] = _as_float(item.get("proposed_weight"))
        row["actual_weight"] = (
            _as_float(perf_row.get("actual_weight"))
            if perf_row and perf_row.get("actual_weight") is not None
            else None
        )
        row["weight"] = row["target_weight"]
        portfolio.append(row)

    recommendation_rows = [item for item in portfolio if not item.get("review_required")]
    domestic = [item for item in recommendation_rows if _is_domestic(item)]
    overseas = [item for item in recommendation_rows if not _is_domestic(item)]
    review_required_positions = [item for item in portfolio if item.get("review_required")]
    cash_weight = max(0.0, 100.0 - sum(item["weight"] for item in portfolio))
    actual_cash_weight = performance.get("actual_cash_weight")
    try:
        actual_cash_weight = float(actual_cash_weight)
    except (TypeError, ValueError):
        actual_cash_weight = None
    stock_weight = sum(item["weight"] for item in portfolio if item.get("asset_type") == "stock")
    etf_weight = sum(item["weight"] for item in portfolio if item.get("asset_type") == "etf")
    stock_rows = [item for item in portfolio if item.get("asset_type") == "stock"]
    etf_rows = [item for item in portfolio if item.get("asset_type") == "etf"]
    actual_stock_weight = (
        sum(_as_float(item.get("actual_weight")) for item in stock_rows)
        if all(item.get("actual_weight") is not None for item in stock_rows)
        else None
    )
    actual_etf_weight = (
        sum(_as_float(item.get("actual_weight")) for item in etf_rows)
        if all(item.get("actual_weight") is not None for item in etf_rows)
        else None
    )
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
            "weight": item["actual_weight"] if item["actual_weight"] is not None else item["weight"],
            "target_weight": item["weight"],
            "actor": item.get("decision_actor", ""),
            "action": item.get("action", ""),
            "reason": item.get("change_reason", ""),
            "market": item.get("market", ""),
            "allocation_role": item.get("allocation_role", ""),
        }
        for item in portfolio
        if item.get("weight", 0) > 0
    ]
    chart_cash_weight = actual_cash_weight if actual_cash_weight is not None else cash_weight
    if chart_cash_weight:
        chart_rows.append({
            "name": "현금",
            "code": "",
            "weight": chart_cash_weight,
            "target_weight": cash_weight,
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

    active_watchlist = list(state.get("watchlist", []) or [])

    def watchlist_sort_key(item: dict) -> tuple[int, int, int, str]:
        date_text = str(
            item.get("latest_material_signal_date")
            or item.get("latest_evidence_date")
            or ""
        )
        try:
            recency = datetime.fromisoformat(date_text).toordinal()
        except ValueError:
            recency = 0
        return (
            0 if item.get("origin_signal_type") in {"MER_DIRECT", "MER_THESIS"} else 1,
            0 if item.get("lifecycle_status") == "active" else 1,
            -recency,
            str(item.get("name") or ""),
        )

    active_watchlist.sort(key=watchlist_sort_key)
    watchlist_total = len(active_watchlist)
    return {
        "today": today_str,
        "status_note": status_note or state.get("status_note", ""),
        "portfolio": portfolio,
        "domestic": domestic,
        "overseas": overseas,
        "watchlist": active_watchlist[:10],
        "watchlist_total": watchlist_total,
        "watchlist_hidden_count": max(0, watchlist_total - 10),
        "watchlist_changes": state.get("last_watchlist_changes", {}) or {},
        "closed_positions": _closed_positions_for_output(state, performance),
        "review_required_positions": review_required_positions,
        "decision_history": state.get("decision_history", []) or [],
        "insights": state.get("insights", []) or [],
        "deferred_posts": state.get("deferred_posts", []) or [],
        "chart_rows": chart_rows,
        "cash_weight": cash_weight,
        "actual_cash_weight": actual_cash_weight,
        "stock_weight": stock_weight,
        "actual_stock_weight": actual_stock_weight,
        "etf_weight": etf_weight,
        "actual_etf_weight": actual_etf_weight,
        "risk_weight": risk_weight,
        "defensive_weight": defensive_weight,
        "defensive_cash_target": DEFENSIVE_CASH_TARGET,
        "defensive_alert": (
            actual_cash_weight if actual_cash_weight is not None else cash_weight
        ) < DEFENSIVE_CASH_TARGET,
        "performance": performance,
        "performance_epoch_id": performance.get("epoch_id"),
        "performance_inception_date": performance.get("inception_date"),
        "legacy_epoch_count": performance.get("legacy_epoch_count", 0),
        "portfolio_return_value": portfolio_return_value,
        "portfolio_return_label": _return_label(portfolio_return_value),
        "missing_return_codes": missing_return_codes,
    }


def _date_for_title(today_str: str) -> str:
    if not today_str:
        return datetime.now().strftime("%Y-%m-%d")
    return today_str


def _table(rows: list[dict], *, include_return: bool = True) -> list[str]:
    headers = ["종목", "코드", "판단", "역할", "목표/실제", "근거"]
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
            (
                f"{_as_float(item.get('target_weight', item.get('proposed_weight', item.get('weight')))):g}% / "
                + (f"{_as_float(item.get('actual_weight')):g}%" if item.get("actual_weight") is not None else "집계 전")
            ),
            str(item.get("policy_change_reason") or item.get("change_reason") or item.get("observation_reason") or ""),
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
        f"- 현금성 목표/실제: {output.get('cash_weight', 0):g}% / "
        + (f"{output.get('actual_cash_weight'):g}%" if output.get("actual_cash_weight") is not None else "집계 전")
        + f" (방어 기준 {output.get('defensive_cash_target', 20):g}%)",
    ]
    if output.get("defensive_alert"):
        lines.append("- 방어 기준 미달: 현금성 비중이 20% 아래라 다음 리밸런싱에서 방어 비중 재검토 필요")
    if output.get("status_note"):
        lines.append(f"- 분석 보류: {output['status_note']}")
    if output.get("missing_return_codes"):
        joined = ", ".join(output["missing_return_codes"])
        lines.append(f"- 수익률 집계 전 종목: {joined}")
    if output.get("performance_epoch_id"):
        lines.append(
            f"- 성과 원장: {output['performance_epoch_id']}"
            + (f" (시작 {output.get('performance_inception_date')})" if output.get("performance_inception_date") else "")
        )
    if output.get("legacy_epoch_count"):
        lines.append(f"- 과거 검증 전 성과원장 {output['legacy_epoch_count']}개는 현재 성과와 분리 보존")
    risk_metrics = output.get("performance", {}).get("risk_metrics", {}) or {}
    if risk_metrics.get("max_drawdown") is not None:
        lines.append(f"- clean epoch 최대낙폭: {risk_metrics['max_drawdown'] * 100:+.2f}%")
    if risk_metrics.get("excess_return") is not None:
        lines.append(f"- 전략 벤치마크 대비 초과수익: {risk_metrics['excess_return'] * 100:+.2f}%")
    if output.get("performance", {}).get("cumulative_costs") is not None:
        lines.append(f"- 누적 추정 거래비용: {output['performance']['cumulative_costs']:.4f} KRW 모델단위")

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
        if output.get("watchlist_hidden_count"):
            lines.append(f"\n활성 {output.get('watchlist_total')}건 중 상위 10건 표시, 외 {output.get('watchlist_hidden_count')}건")
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
