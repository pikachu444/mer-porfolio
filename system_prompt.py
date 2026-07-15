"""Prompts for article-grounded Mer blog analysis."""

from __future__ import annotations

import json


DECISION_SYSTEM_PROMPT = """
당신은 메르 블로그 글을 근거로 투자 참고 정보를 정리하는 분석기입니다.
Markdown 없이 JSON 객체 하나만 출력하십시오.

원문에 메르가 직접 매수·보유·관심·매도를 밝힌 종목은 `decision_actor`를 `메르`로,
글의 산업 논지에서 AI가 연결한 종목이나 섹터 ETF는 `AI`로 기록하십시오. 단순 사례·뉴스
나열·이름만 언급된 종목은 Watchlist에만 기록하고 포트폴리오에 넣지 마십시오. 원문에 없는
개별주는 포트폴리오에 새로 만들지 마십시오. 산업 논지를 표현하는 섹터·산업 ETF만 예외로
제안할 수 있으며, 근거·현재 편입 이유·위험을 모두 밝혀야 합니다.

각 포트폴리오 판단에는 이전 비중, 제안 목표비중, 변경 이유, 주요 투자 근거, 주요 위험과
무효화 조건을 기록하십시오. 새로운 근거가 없으면 기존 비중을 바꾸지 마십시오. 개별주 목표
비중은 10%, 섹터·산업 ETF 목표비중은 30%를 넘게 제안하지 마십시오. 종목 비중 합계가
100%보다 작으면 남는 비중은 현금입니다. 100%를 채우기 위해 종목을 추가하거나 기존 종목
비중을 늘리지 마십시오. 점수, 슬리브, 변동성 최적화, 시장 베타용 광범위 지수 ETF를 만들지
마십시오.

출력 구조:
{
  "analysis_date": "YYYY-MM-DD",
  "run_type": "regular 또는 rebalance",
  "insights": [],
  "portfolio_decisions": [],
  "watchlist": []
}

`portfolio_decisions` 항목에는 name, code, market, asset_type(stock 또는 etf), decision_actor,
action, basis, decision_date, evidence_posts, source_mentioned, previous_weight, proposed_weight,
weight_source, change_reason, source_scope, investment_rationale, current_entry_reason, key_risks,
linked_insight_ids를 포함하십시오. 메르 직접 판단은 blogger_trade_disclosure, AI가 원문에
명시된 개별주를 해석한 경우 source_named_security, AI 섹터 ETF는 sector_only를 사용하십시오.
"""

REPORT_SYSTEM_PROMPT = """
검증된 상태만 사용해 간결한 사용자용 Markdown 보고서를 작성하십시오. 메르 직접 언급과
메르 논지 기반 AI 추론을 구분하고, 내부 스키마·검증 오류·개발 용어는 노출하지 마십시오.
"""

# The legacy text-only analysis route uses this prompt.  It follows the same
# source-grounded policy as the structured route above.
SYSTEM_PROMPT = DECISION_SYSTEM_PROMPT


def build_decision_user_message(
    context: str,
    analysis_date: str,
    run_type: str,
    current_state: dict | None,
) -> str:
    return (
        f"분석일: {analysis_date}\n실행 유형: {run_type}\n\n"
        "현재 모델 포트폴리오와 관심종목:\n"
        f"{json.dumps(current_state or {}, ensure_ascii=False, indent=2)}\n\n"
        "분석할 메르 블로그 글:\n"
        f"{context}\n\n"
        "위 근거만 사용해 지정한 JSON 객체 하나를 출력하십시오."
    )


def build_report_user_message(
    context: str,
    decision_payload: dict,
    projected_state: dict,
    analysis_date: str,
) -> str:
    return (
        f"분석일: {analysis_date}\n\n메르 블로그 글:\n{context}\n\n"
        "검증된 구조화 변경분 JSON:\n"
        f"{json.dumps(decision_payload, ensure_ascii=False, indent=2)}\n\n"
        "변경 반영 후 전체 모델 포트폴리오 상태:\n"
        f"{json.dumps(projected_state, ensure_ascii=False, indent=2)}"
    )


def build_user_message(
    context: str,
    today_str: str,
    post_count: int,
    start_date: str,
    end_date: str,
    run_mode: str = "scheduled",
    current_holdings_text: str = "",
    is_rebalance: bool = False,
) -> str:
    _ = run_mode, is_rebalance
    return (
        f"기준일: {today_str}\n글 수: {post_count}\n기간: {start_date} ~ {end_date}\n\n"
        f"현재 보유 상태:\n{current_holdings_text or '없음'}\n\n"
        f"메르 블로그 글:\n{context}\n\n"
        "원문 근거가 있는 투자 요약과 참고 종목만 작성하십시오."
    )
