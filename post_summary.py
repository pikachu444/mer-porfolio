"""Shared post-summary prompt and response validation."""

from __future__ import annotations

import json
import re


SUMMARY_VERSION = 2

MAP_SUMMARY_PROMPT = """
당신은 거시경제 분석가 메르의 글을 정밀 압축하고 투자 관련 여부를 분류하는 1차 요약 엔진입니다.
제공된 블로그 전문을 읽고 JSON 객체 하나만 출력하십시오.

출력 구조:
{
  "investment_relevant": true,
  "relevance_reason": "투자 분석 포함 또는 제외 이유",
  "summary": "정밀 압축 요약"
}

투자 관련 글에는 거시경제, 정책, 지정학, 산업, 기업, 수급, 리스크처럼 투자 판단에 영향을
줄 수 있는 내용을 포함합니다. 맛집, 여행, 일상, 일반 건강 글은 투자 가능한 산업, 기업,
시장 변화와 구체적으로 연결되지 않으면 투자 관련이 아닙니다. 제목 키워드만 보고 판단하지
말고 본문 전체 문맥을 읽으십시오.

`summary`에는 다음을 보존하십시오:
1. 날짜, 구체적 수치, 선언 내용 등 핵심 사실.
2. 사건이 유발하는 1차/2차/3차 파급효과와 업종 연결 고리.
3. 직접 또는 간접 언급된 종목, 티커, ETF, 섹터.
4. 메르 본인이 특정 종목의 매수, 보유, 매도 또는 관심을 직접 밝힌 문맥.

없는 사실을 창작하지 마십시오. 투자와 무관한 글도 `summary`에 짧은 내용 요약을 남기십시오.
"""


def parse_summary_response(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    payload = json.loads(stripped)
    if not isinstance(payload, dict):
        raise ValueError("글별 요약 응답은 JSON 객체여야 합니다.")
    summary = payload.get("summary")
    relevant = payload.get("investment_relevant")
    reason = payload.get("relevance_reason")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("글별 요약에 summary가 없습니다.")
    if not isinstance(relevant, bool):
        raise ValueError("글별 요약에 investment_relevant boolean이 없습니다.")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("글별 요약에 relevance_reason이 없습니다.")
    return {
        "summary": summary.strip(),
        "investment_relevant": relevant,
        "relevance_reason": reason.strip(),
        "summary_version": SUMMARY_VERSION,
    }
