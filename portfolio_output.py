"""사용자 출력의 단일 기준 자료.

이 모듈은 승인된 포트폴리오만 사용자 화면으로 내보낸다. 과거 LLM action,
provenance, validator 메시지는 내부 상태/로그에 남아도 Telegram·Markdown·일반
대시보드에는 들어가지 않는다.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from portfolio_actions import RebalancePolicy, derive_portfolio_actions
from portfolio_schema import normalize_security_code

DEFENSIVE_CASH_TARGET = 20.0

ALLOCATION_ROLE_LABELS = {
    "core": "핵심",
    "satellite": "위성",
    "risk": "위험자산",
    "defensive": "방어",
    "watch": "관찰",
}

_PUBLIC_REPLACEMENTS = {
    "파트너쉽": "파트너십",
    "매커니즘": "메커니즘",
    "규제 허들": "승인 장애 요인",
    "관찰 유지": "관심종목으로 유지",
    "수급 디커플링": "시장 간 가격 괴리",
    "clean epoch MDD": "최대 낙폭",
    "clean epoch 최대낙폭": "최대 낙폭",
    "전략 벤치마크 대비 초과수익": "기준 포트폴리오 대비",
    "누적 추정비용": "누적 거래비용 추정",
    "누적 추정 거래비용": "누적 거래비용 추정",
    "재검증 필요 포지션": "관리자 검토 대기 종목",
    "Watchlist": "관심종목",
    "watchlist": "관심종목",
    "Hold": "유지",
    "legacy_unvalidated": "",
    "provenance_status": "",
    "source_mentioned": "",
    "weight_source": "",
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
    """Compatibility label for detailed/admin tooling; not rendered in summaries."""
    origin = str(item.get("origin_signal_type") or "").strip().upper()
    actor = item.get("decision_actor")
    if origin == "MER_DIRECT":
        return "메르 직접 신호 · AI 관리" if actor == "AI" else "메르 직접 신호"
    if origin == "MER_THESIS":
        return "메르 방향성 · AI 관리" if actor == "AI" else "메르 방향성"
    if origin == "AI_INFERRED":
        return "AI 추론"
    return "메르 직접 발언" if actor == "메르" else "AI 제안" if actor == "AI" else "미분류"


def _action_label(item: dict) -> str:
    """Historical action, retained only for backward-compatible diagnostics."""
    return str(item.get("policy_action") or item.get("action") or "보유")


def _allocation_role_label(item: dict) -> str:
    role = str(item.get("allocation_role") or "").strip()
    return ALLOCATION_ROLE_LABELS.get(role, "역할 미지정")


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
        return "데이터 없음"
    return f"{value:+.2f}%"


def _return_difference_label(value: float | None) -> str:
    """Format a return spread in percentage points, not percent."""
    if value is None:
        return "데이터 없음"
    return f"{value:+.2f}%p"


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
    for row in (performance or {}).get("active_positions", []) or []:
        identity = str(row.get("key") or _security_identity(row)).strip()
        if identity:
            by_identity[identity] = row
        for key in (row.get("code"), row.get("ticker")):
            code = _code(key)
            if not code:
                continue
            if code in by_code and by_code[code] is not row:
                ambiguous_codes.add(code)
            by_code[code] = row
            if code.endswith(".KS") or code.endswith(".KQ"):
                bare = code.split(".", 1)[0]
                if bare in by_code and by_code[bare] is not row:
                    ambiguous_codes.add(bare)
                by_code[bare] = row
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
            key = str(item.get("position_episode_id") or "").strip() or ":".join((
                _security_identity(item),
                str(item.get("closed_date") or item.get("decision_date") or ""),
            ))
            if key in episode_index:
                closed[episode_index[key]].update({
                    field: value for field, value in item.items() if value is not None
                })
            else:
                episode_index[key] = len(closed)
                closed.append(dict(item))
    return closed


def _clean_user_text(value: Any, *, fallback: str = "") -> str:
    text = " ".join(str(value or "").split())
    for source, target in _PUBLIC_REPLACEMENTS.items():
        text = text.replace(source, target)
    # Never pass schema/validator fragments to a user-facing surface.
    if re.search(r"decisions?\[|links signals|validator|schema[_ ]version|provenance", text, re.I):
        return fallback
    text = re.sub(r"\s{2,}", " ", text).strip(" /·")
    return text or fallback


def public_status_note(note: Any) -> str:
    """Map an internal failure/deferral to a short, safe user message."""
    raw = str(note or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    if any(token in lowered for token in (
        "decisions[", "links signals", "validator", "portfolio policy",
        "포트폴리오 안전 검증", "structured", "schema", "provenance", "allocator",
    )):
        return "오늘 제안된 조정안이 내부 검증 기준을 충족하지 않아 기존 포트폴리오를 유지합니다."
    if any(token in lowered for token in ("gemini", "요약 실패", "분석 보류", "transient")):
        return "오늘 일부 블로그 분석이 완료되지 않아 기존 포트폴리오를 유지합니다."
    cleaned = _clean_user_text(raw)
    return cleaned if cleaned else "기존 포트폴리오를 유지합니다."


def _watchlist_identity(item: dict) -> str:
    market = _code(item.get("market"))
    code = normalize_security_code(item.get("name"), market, item.get("code"))
    name = " ".join(str(item.get("name") or "").split()).casefold()
    return f"{market}:{code or name}"


def _dedupe_watchlist(items: list[dict]) -> list[dict]:
    """Merge duplicate security records (for example the two HLB theses)."""
    merged: dict[str, dict] = {}
    for item in items:
        key = _watchlist_identity(item)
        current = merged.get(key)
        if current is None:
            merged[key] = dict(item)
            continue
        current_date = str(current.get("latest_material_signal_date") or "")
        item_date = str(item.get("latest_material_signal_date") or "")
        if item_date > current_date:
            preferred, other = dict(item), current
        else:
            preferred, other = current, item
        ids = list(dict.fromkeys(
            [*(preferred.get("linked_signal_ids") or []), *(other.get("linked_signal_ids") or [])]
        ))
        preferred["linked_signal_ids"] = ids
        preferred["merged_thesis_ids"] = list(dict.fromkeys(
            [*(preferred.get("merged_thesis_ids") or []), str(other.get("thesis_id") or "")]
        ))
        if not preferred.get("observation_reason"):
            preferred["observation_reason"] = other.get("observation_reason")
        merged[key] = preferred
    return list(merged.values())


def _watchlist_change_rows(state: dict, watchlist: list[dict]) -> list[dict]:
    changes = state.get("last_watchlist_changes", {}) or {}
    by_id = {
        str(item.get("thesis_id")): item
        for item in [*(state.get("watchlist", []) or []), *(state.get("watchlist_archive", []) or [])]
        if item.get("thesis_id")
    }
    labels = {
        "added": "신규",
        "updated": "판단 변경",
        "promoted": "편입 검토",
        "rejected": "제외",
        "expired": "관심종목 만료",
        "archived": "보관",
    }
    result: list[dict] = []
    seen: set[str] = set()
    for kind, label in labels.items():
        for thesis_id in changes.get(kind, []) or []:
            item = by_id.get(str(thesis_id))
            if not item:
                continue
            identity = _watchlist_identity(item)
            if identity in seen:
                continue
            seen.add(identity)
            result.append({
                "category": kind,
                "label": label,
                "name": item.get("name", ""),
                "code": item.get("code", ""),
                "reason": _clean_user_text(
                    item.get("observation_reason") or item.get("change_reason"),
                    fallback="판단 조건이 변경되었습니다.",
                ),
            })
    return result


_INTERNAL_OUTPUT_FIELDS = {
    "provenance_status",
    "origin_signal_type",
    "origin_signal_ids",
    "linked_signal_ids",
    "source_mentioned",
    "weight_source",
    "allocation_role_source",
    "allocation_role_reason",
    "source_scope",
    "decision_actor",
    "action",
    "action_label",
    "policy_action",
    "policy_change_reason",
    "allocation_method",
    "decision_model_id",
    "thesis_id",
    "issuer_id",
    "theme_ids",
    "quality_components",
    "country_code",
    "fixed_weight",
    "rejected_linked_signal_ids",
    "linked_insight_ids",
    "review_required",
    "review_reason",
}


def _public_item(item: dict) -> dict:
    value = dict(item)
    for key in _INTERNAL_OUTPUT_FIELDS:
        value.pop(key, None)
    for key in ("change_reason", "observation_reason", "investment_rationale", "current_entry_reason", "close_reason"):
        if key in value:
            value[key] = _clean_user_text(value[key])
    return value


def build_output_model(
    state: dict,
    performance: dict | None = None,
    *,
    today_str: str = "",
    status_note: str = "",
    approved_changes: bool | None = None,
) -> dict:
    """현재 승인 상태를 Telegram·HTML·Markdown이 함께 사용하는 모델로 만든다."""
    performance = performance or {}
    active_by_identity, active_by_code = _performance_by_identity(performance)
    portfolio: list[dict] = []
    missing_return_codes: list[str] = []
    approved_items = [
        item for item in (state.get("portfolio", []) or [])
        if str(item.get("provenance_status") or "") != "legacy_unvalidated"
        and str(item.get("queue_status") or "") != "pending_admin"
        and str(item.get("decision_actor") or "") != "미분류"
        and not (item.get("decision_actor") == "AI" and not str(item.get("allocation_role") or "").strip())
    ]
    for item in approved_items:
        row = dict(item)
        perf_row = active_by_identity.get(_security_identity(item)) or active_by_code.get(_code(item.get("code")))
        if perf_row:
            for key in ("return_pct", "return_pct_krw", "entry_date", "current_price"):
                if key in perf_row:
                    row[key] = perf_row[key]
        else:
            missing_return_codes.append(str(item.get("code") or item.get("name") or ""))
        row["target_weight"] = _as_float(item.get("proposed_weight"))
        row["actual_weight"] = (
            _as_float(perf_row.get("actual_weight"))
            if perf_row and perf_row.get("actual_weight") is not None
            else None
        )
        row["weight"] = row["target_weight"]
        row["actor_label"] = _actor_label(item)
        row["action_label"] = _action_label(item)
        row["return_value"] = _return_value(perf_row)
        row["return_label"] = _return_label(row["return_value"])
        row["allocation_role_label"] = _allocation_role_label(row)
        rationale = _clean_user_text(
            item.get("investment_rationale") or item.get("change_reason"),
            fallback="승인된 투자 논리를 추적합니다.",
        )
        # Explanations are rendered after allocation. Avoid carrying a stale
        # historical percentage such as “현행 5% 유지” into a 1.50% target.
        rationale = re.sub(r"\b\d+(?:\.\d+)?\s*%", "", rationale)
        rationale = re.sub(r"\(\s*\)", "", rationale)
        row["display_reason"] = f"{rationale.rstrip('.')} · 최종 목표비중 {_as_float(item.get('proposed_weight')):.2f}%"
        row["display_evidence"] = [
            {
                "title": _clean_user_text(post.get("title"), fallback="원문"),
                "url": str(post.get("url") or ""),
                "published_date": str(post.get("published_date") or ""),
            }
            for post in (item.get("evidence_posts") or [])
            if isinstance(post, dict)
        ]
        portfolio.append(_public_item(row))

    portfolio, today_changes = derive_portfolio_actions(portfolio)
    # ``today_changes`` is a drift review derived from actual-vs-target
    # weights.  It is not automatically an approved order.  A fail-closed
    # run can therefore retain a drift row while explicitly suppressing it
    # from the approved-adjustment section.
    if approved_changes is None:
        approved_changes = True
    approved_today_changes = today_changes if approved_changes else []
    for item in portfolio:
        action = str(item.get("today_action") or "유지")
        item["display_today_action"] = (
            action
            if approved_changes or action in {"유지", "데이터 없음"}
            else "조정 보류"
        )
    domestic = [item for item in portfolio if _is_domestic(item)]
    overseas = [item for item in portfolio if not _is_domestic(item)]
    stock_rows = [item for item in portfolio if item.get("asset_type") == "stock"]
    etf_rows = [item for item in portfolio if item.get("asset_type") == "etf"]
    target_cash_weight = max(0.0, 100.0 - sum(item["target_weight"] for item in portfolio))
    actual_cash_weight = performance.get("actual_cash_weight")
    try:
        actual_cash_weight = float(actual_cash_weight)
    except (TypeError, ValueError):
        actual_cash_weight = None
    actual_stock_weight = (
        sum(_as_float(item.get("actual_weight")) for item in stock_rows)
        if stock_rows and all(item.get("actual_weight") is not None for item in stock_rows)
        else None
    )
    actual_etf_weight = (
        sum(_as_float(item.get("actual_weight")) for item in etf_rows)
        if etf_rows and all(item.get("actual_weight") is not None for item in etf_rows)
        else None
    )
    target_stock_weight = sum(item["target_weight"] for item in stock_rows)
    target_etf_weight = sum(item["target_weight"] for item in etf_rows)
    actual_all_available = bool(portfolio) and all(
        item.get("actual_weight") is not None for item in portfolio
    ) and actual_cash_weight is not None
    active_watchlist = [
        _public_item(item)
        for item in _dedupe_watchlist(list(state.get("watchlist", []) or []))
    ]

    def watchlist_sort_key(item: dict) -> tuple[int, int, int, str]:
        date_text = str(item.get("latest_material_signal_date") or item.get("latest_evidence_date") or "")
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
    risk_metrics = performance.get("risk_metrics", {}) or {}
    portfolio_return = None
    if not missing_return_codes:
        try:
            portfolio_return = float(performance.get("portfolio_return_krw"))
        except (TypeError, ValueError):
            pass
    chart_rows = []
    for item in portfolio:
        if item["target_weight"] <= 0:
            continue
        chart_rows.append({
            "name": item.get("name", ""),
            "code": item.get("code", ""),
            "weight": item.get("actual_weight") if item.get("actual_weight") is not None else None,
            "target_weight": item["target_weight"],
            "market": item.get("market", ""),
            "asset_type": item.get("asset_type", ""),
        })
    if actual_cash_weight is not None:
        chart_rows.append({
            "name": "현금성 자산", "code": "", "weight": actual_cash_weight,
            "target_weight": target_cash_weight, "market": "CASH", "asset_type": "cash",
        })

    public_insights = []
    for item in state.get("insights", []) or []:
        if not isinstance(item, dict):
            continue
        public_insights.append({
            **item,
            "title": _clean_user_text(item.get("title"), fallback="시장 인사이트"),
            "summary": _clean_user_text(item.get("summary"), fallback="새로운 시장 변화가 기록되었습니다."),
            "investment_implication": _clean_user_text(
                item.get("investment_implication"),
                fallback="관련 조건을 다음 점검에서 확인합니다.",
            ),
        })

    public_closed = []
    for item in _closed_positions_for_output(state, performance):
        closed_item = _public_item(item)
        if item.get("administrative_exit"):
            closed_item["close_reason"] = "승인 포트폴리오 정리 과정에서 편출"
        public_closed.append(closed_item)

    return {
        "today": today_str,
        "status_note": public_status_note(status_note or state.get("status_note", "")),
        "portfolio": portfolio,
        "approved_portfolio": portfolio,
        "domestic": domestic,
        "overseas": overseas,
        "today_changes": today_changes,
        "approved_today_changes": approved_today_changes,
        "actions_deferred": not approved_changes,
        "all_within_rebalance_band": not today_changes and all(
            item.get("actual_weight") is not None for item in portfolio
        ),
        "rebalance_policy": RebalancePolicy.from_environment().to_dict(),
        "watchlist": active_watchlist,
        "watchlist_total": len(active_watchlist),
        "watchlist_hidden_count": 0,
        "watchlist_changes": {},
        "watchlist_changes_display": _watchlist_change_rows(state, active_watchlist),
        "closed_positions": public_closed,
        # Kept as an empty compatibility key. Admin rows never enter the
        # user-facing model; they are available only in the state/log bundle.
        "review_required_positions": [],
        "decision_history": [],
        "insights": public_insights,
        "deferred_posts": [
            {
                "title": _clean_user_text(item.get("title"), fallback="제목 없음"),
                "date": str(item.get("date") or ""),
                "url": str(item.get("url") or ""),
                "reason": "요약 미완료",
            }
            for item in (state.get("deferred_posts", []) or [])
            if isinstance(item, dict)
        ],
        "chart_rows": chart_rows,
        "target_cash_weight": target_cash_weight,
        "actual_cash_weight": actual_cash_weight,
        "cash_weight": target_cash_weight,
        "stock_weight": target_stock_weight,
        "target_stock_weight": target_stock_weight,
        "actual_stock_weight": actual_stock_weight,
        "etf_weight": target_etf_weight,
        "target_etf_weight": target_etf_weight,
        "actual_etf_weight": actual_etf_weight,
        "actual_allocation_available": actual_all_available,
        "risk_weight": sum(item["target_weight"] for item in portfolio if item.get("allocation_role") == "risk"),
        "defensive_weight": target_cash_weight,
        "defensive_cash_target": DEFENSIVE_CASH_TARGET,
        "defensive_alert": (
            actual_cash_weight if actual_cash_weight is not None else target_cash_weight
        ) < DEFENSIVE_CASH_TARGET,
        "performance_inception_date": performance.get("inception_date"),
        "portfolio_return_value": portfolio_return,
        "portfolio_return_label": _return_label(portfolio_return),
        "missing_return_codes": missing_return_codes,
        "max_drawdown_label": _return_label(
            _as_float(risk_metrics.get("max_drawdown")) * 100
            if risk_metrics.get("max_drawdown") is not None else None
        ),
        "benchmark_difference_label": _return_difference_label(
            _as_float(risk_metrics.get("excess_return")) * 100
            if risk_metrics.get("excess_return") is not None else None
        ),
        "cumulative_costs": performance.get("cumulative_costs"),
    }


def _date_for_title(today_str: str) -> str:
    return today_str or datetime.now().strftime("%Y-%m-%d")


def _weight_label(value: Any) -> str:
    if value is None:
        return "데이터 없음"
    return f"{_as_float(value):.2f}%"


def _table(rows: list[dict], *, include_return: bool = True) -> list[str]:
    headers = ["종목", "실제비중", "목표비중", "오늘 상태", "역할"]
    if include_return:
        headers.append("수익률")
    headers.append("핵심 근거")
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    if not rows:
        lines.append("| 표시할 항목 없음 | " + " | ".join("" for _ in headers[1:]) + " |")
        return lines
    for item in rows:
        display_name = str(item.get("name") or "")
        if item.get("code"):
            display_name += f" ({item.get('code')})"
        values = [
            display_name,
            _weight_label(item.get("actual_weight")),
            _weight_label(item.get("target_weight", item.get("proposed_weight"))),
            str(item.get("display_today_action") or item.get("today_action") or "유지"),
            str(item.get("allocation_role_label") or _allocation_role_label(item)),
        ]
        if include_return:
            values.append(str(item.get("return_label", "데이터 없음")))
        values.append(str(item.get("display_reason") or "승인된 투자 논리를 추적합니다."))
        lines.append("| " + " | ".join(value.replace("|", "/") for value in values) + " |")
    return lines


def _append_holdings(lines: list[str], output: dict) -> None:
    lines += ["", "## 현재 보유 종목"]
    groups = [
        ("국내 개별주", [r for r in output["portfolio"] if _is_domestic(r) and r.get("asset_type") == "stock"]),
        ("국내·해외 ETF", [r for r in output["portfolio"] if r.get("asset_type") == "etf"]),
        ("해외 개별주", [r for r in output["portfolio"] if not _is_domestic(r) and r.get("asset_type") == "stock"]),
    ]
    for title, rows in groups:
        if rows:
            lines += ["", f"### {title}"] + _table(rows)
    lines += ["", "### 현금성 자산", "", f"목표 {_weight_label(output.get('target_cash_weight'))} / 실제 {_weight_label(output.get('actual_cash_weight'))}"]


def build_markdown_report(output: dict) -> str:
    """승인 상태 중심의 사용자용 Markdown 보고서."""
    today = _date_for_title(output.get("today", ""))
    inception = output.get("performance_inception_date")
    return_basis = f" ({inception} 이후)" if inception else ""
    lines = [
        "# 메르AI 모델 포트폴리오 리포트",
        "",
        f"- 기준일: {today}",
        "- 메르 블로그 분석을 바탕으로 구성한 참고용 모델 포트폴리오입니다.",
        f"- 누적 수익률{return_basis}: {output.get('portfolio_return_label', '데이터 없음')}",
        f"- 최대 낙폭: {output.get('max_drawdown_label', '데이터 없음')}",
        f"- 기준 포트폴리오 대비: {output.get('benchmark_difference_label', '데이터 없음')} (수익률 차이)",
        f"- 자산배분(목표): 개별주 {_weight_label(output.get('target_stock_weight'))} / 주식형 ETF {_weight_label(output.get('target_etf_weight'))} / 현금성 {_weight_label(output.get('target_cash_weight'))}",
    ]
    if output.get("actual_allocation_available"):
        lines.append(
            f"- 자산배분(실제): 개별주 {_weight_label(output.get('actual_stock_weight'))} / 주식형 ETF {_weight_label(output.get('actual_etf_weight'))} / 현금성 {_weight_label(output.get('actual_cash_weight'))}"
        )
    if output.get("status_note"):
        lines.append(f"- 안내: {output['status_note']}")
    if output.get("defensive_alert"):
        lines.append("- 현금성 자산이 방어 기준보다 낮아 다음 주 점검에서 위험 노출을 확인합니다.")
    if output.get("cumulative_costs") is not None:
        lines.append(f"- 누적 거래비용 추정: {_as_float(output['cumulative_costs']):.4f} 모델단위")

    deferred_posts = output.get("deferred_posts", []) or []
    if deferred_posts:
        lines += ["", "## 오늘 분석에서 제외된 글", ""]
        for item in deferred_posts:
            title = _clean_user_text(item.get("title"), fallback="제목 없음")
            url = str(item.get("url") or "")
            lines.append(f"- {title}" + (f" ([원문]({url}))" if url else ""))
        lines.append("요약이 준비된 뒤 다음 실행에서 다시 확인합니다.")

    _append_holdings(lines, output)
    lines += ["", "## 오늘의 조정"]
    approved_changes = output.get("approved_today_changes", output.get("today_changes", [])) or []
    drift_review = output.get("today_changes", []) or []
    if approved_changes:
        for item in approved_changes:
            lines.append(
                f"- {item.get('name')} {_weight_label(item.get('actual_weight'))} → {_weight_label(item.get('target_weight'))} | {item.get('display_today_action') or item.get('today_action')}"
            )
    elif output.get("actions_deferred") and drift_review:
        lines.append("- 승인된 매매 없음")
        lines.append("- 현재 비중 이탈이 확인됐지만 내부 검증이 끝나지 않아 자동 조정을 보류합니다.")
    else:
        lines += ["- 승인된 매매 없음", "- 전 종목이 리밸런싱 허용 범위 또는 최소 조정 기준 안에 있습니다."]

    lines += ["", "## 핵심 인사이트"]
    insights = output.get("insights", []) or []
    if not insights:
        lines.append("표시할 인사이트가 없습니다.")
    for index, item in enumerate(insights, start=1):
        title = _clean_user_text(item.get("title"), fallback=f"인사이트 {index}")
        summary = _clean_user_text(item.get("summary"), fallback="새로운 시장 변화가 기록되었습니다.")
        implication = _clean_user_text(item.get("investment_implication"), fallback="관련 조건을 다음 점검에서 확인합니다.")
        lines += ["", f"### 인사이트 {index}: {title}", "", summary, "", f"**추적할 조건:** {implication}"]

    changes = output.get("watchlist_changes_display", []) or []
    lines += ["", "## 관심종목 변경"]
    if changes:
        for item in changes:
            lines.append(f"- {item['label']}: {item['name']}" + (f" — {item['reason']}" if item.get("reason") else ""))
    else:
        lines.append("- 변경 없음")

    lines += ["", "## 과거 편출 종목"]
    closed = output.get("closed_positions", []) or []
    if closed:
        for item in closed:
            reason = _clean_user_text(item.get("close_reason"), fallback="과거 포트폴리오에서 편출된 종목입니다.")
            if item.get("administrative_exit"):
                reason = "승인 포트폴리오 정리 과정에서 편출"
            lines.append(f"- {item.get('name')} ({item.get('code')}) · {item.get('closed_date', '')} · {reason}")
    else:
        lines.append("- 기록 없음")
    lines += ["", "> 이 보고서는 메르 블로그의 공개 분석을 참고해 만든 모델 포트폴리오이며 실제 주문을 대신하지 않습니다.", ""]
    return "\n".join(lines)
