"""Backfill evidence for legacy portfolio positions.

This is a one-off maintenance script. It restores evidence metadata for
portfolio positions that were migrated before structured evidence fields were
enforced.
"""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any


STATE_PATH = Path("output/portfolio_state.json")
POSTS_DB_PATH = Path("output/posts_db.json")
BACKFILL_DATE = "2026-06-15"
BACKFILL_SOURCE = "legacy_evidence_backfill_2026-06-15"
REPORT_BASE_URL = "https://github.com/pikachu444/mer-portfolio/blob/main/output"


def _report_evidence(filename: str, title: str, published_date: str) -> dict[str, str]:
    return {
        "title": f"과거 보고서: {filename} - {title}",
        "url": f"{REPORT_BASE_URL}/{filename}",
        "published_date": published_date,
        "evidence_type": "historical_report",
    }


BACKFILL_RULES: dict[tuple[str, str], dict[str, Any]] = {
    ("KR", "006220"): {
        "name": "LS",
        "basis": "섹터 분석",
        "source_scope": "previous_decision",
        "source_mentioned": False,
        "allocation_role": "satellite",
        "allocation_role_reason": "전력/인프라 수혜 보조 포지션",
        "evidence_posts": [
            _report_evidence(
                "report_20260512.md",
                "해저/초고압 케이블 수요 증가, 전력 인프라 투자 확대 수혜",
                "2026-05-12",
            )
        ],
        "investment_rationale": "AI 데이터센터발 전력 수요 증가와 전력망 증설 흐름에서 전선/케이블 인프라 수혜 가능성이 과거 보고서에 기록됨.",
        "current_entry_reason": "기존 보유 포지션이며 과거 보고서에서 전력 인프라 수혜 근거가 확인되어 재검증 전까지 보유 근거를 복구함.",
        "key_risks": ["전력 인프라 투자 지연", "구리 등 원자재 가격 변동", "수주 경쟁 심화"],
        "change_reason": "과거 보고서 근거 복구: 전력 인프라 및 해저/초고압 케이블 수혜 근거 확인.",
    },
    ("KR", "001440"): {
        "name": "대한전선",
        "basis": "섹터 분석",
        "source_scope": "previous_decision",
        "source_mentioned": False,
        "allocation_role": "satellite",
        "allocation_role_reason": "전력 케이블 수혜 보조 포지션",
        "evidence_posts": [
            _report_evidence(
                "report_20260512.md",
                "해저/초고압 케이블 수요 증가, 전력 인프라 투자 확대 수혜",
                "2026-05-12",
            )
        ],
        "investment_rationale": "AI 데이터센터 확산과 노후 전력망 증설에 따른 초고압 케이블 수요 증가 근거가 과거 보고서에 기록됨.",
        "current_entry_reason": "기존 보유 포지션이며 과거 보고서에서 전력 인프라 수혜 근거가 확인되어 재검증 전까지 보유 근거를 복구함.",
        "key_risks": ["전력 인프라 투자 지연", "원자재 가격 변동", "국내외 케이블 업체 경쟁"],
        "change_reason": "과거 보고서 근거 복구: 전력 인프라 및 해저/초고압 케이블 수혜 근거 확인.",
    },
    ("US", "MSFT"): {
        "name": "Microsoft",
        "basis": "종목 분석",
        "source_scope": "source_named_security",
        "source_mentioned": True,
        "allocation_role": "core",
        "allocation_role_reason": "클라우드 AI 핵심 포지션",
        "post_title": "DNA 데이터 저장장치가 삼성전자와 SK하이닉스를 위협할까?",
        "investment_rationale": "AI, 클라우드, 장기 데이터 저장 기술 확장 흐름에서 Microsoft의 AI 생태계와 인프라 경쟁력이 확인됨.",
        "current_entry_reason": "기존 보유 포지션이며 원문에서 Microsoft의 DNA 데이터 저장 기술 개발 사례가 확인되어 보유 근거를 복구함.",
        "key_risks": ["AI 서비스 수익성 압박", "클라우드 경쟁 심화", "규제 리스크"],
        "change_reason": "원문 근거 복구: Microsoft의 DNA 데이터 저장 및 AI/클라우드 생태계 확장 근거 확인.",
    },
    ("KR", "011070"): {
        "name": "LG이노텍",
        "basis": "종목 분석",
        "source_scope": "source_named_security",
        "source_mentioned": True,
        "allocation_role": "satellite",
        "allocation_role_reason": "로봇/AI 하드웨어 보조 성장 포지션",
        "post_title": "4일 5시간 30분째 일하고 있는 피겨AI의 택배 분류로봇 근황",
        "investment_rationale": "Figure AI 투자와 휴머노이드 로봇 부품 공급 가능성이 원문에서 확인되어 피지컬 AI 하드웨어 생태계 수혜 가능성이 있음.",
        "current_entry_reason": "기존 보유 포지션이며 원문에서 LG이노텍의 Figure AI 투자와 휴머노이드 협력 근거가 확인되어 보유 근거를 복구함.",
        "key_risks": ["휴머노이드 로봇 상용화 지연", "부품 공급 계약 불확실성", "고객사 투자 속도 변화"],
        "change_reason": "원문 근거 복구: Figure AI 투자 및 휴머노이드 로봇 부품 공급 기대 확인.",
    },
    ("KR", "005380"): {
        "name": "현대차",
        "basis": "종목 분석",
        "source_scope": "source_named_security",
        "source_mentioned": True,
        "allocation_role": "core",
        "allocation_role_reason": "로봇 제조/피지컬 AI 핵심 포지션",
        "post_title": "젠슨 황이 삼성과 LG가 아니라 이기업을 선택한 이유는?",
        "investment_rationale": "보스턴 다이내믹스, 제미나이 로보틱스 결합, 휴머노이드 생산 계획이 원문에서 확인되어 피지컬 AI 핵심 제조 포지션으로 볼 수 있음.",
        "current_entry_reason": "기존 보유 포지션이며 원문에서 현대차그룹의 피지컬 AI 전략과 로봇 생산 계획이 확인되어 보유 근거를 복구함.",
        "key_risks": ["로봇 상용화 지연", "자동차 경기 둔화", "AI 로보틱스 투자 회수 지연"],
        "change_reason": "원문 근거 복구: 보스턴 다이내믹스와 제미나이 로보틱스 결합, 휴머노이드 생산 계획 확인.",
    },
}


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _posts_by_title(posts_db: Any) -> dict[str, dict[str, Any]]:
    posts = posts_db.get("posts") if isinstance(posts_db, dict) else posts_db
    if not isinstance(posts, list):
        raise ValueError("posts_db must be a list or contain a posts list")
    return {
        str(post.get("title") or ""): post
        for post in posts
        if isinstance(post, dict)
    }


def _post_evidence(rule: dict[str, Any], posts: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    if "evidence_posts" in rule:
        return deepcopy(rule["evidence_posts"])
    title = rule["post_title"]
    post = posts.get(title)
    if not post:
        raise ValueError(f"missing post in posts_db: {title}")
    return [
        {
            "title": str(post["title"]),
            "url": str(post["url"]),
            "published_date": str(post["date"]),
            "evidence_type": "blog_post",
        }
    ]


def _item_key(item: dict[str, Any]) -> tuple[str, str]:
    return str(item.get("market") or "").upper(), str(item.get("code") or "").upper()


def backfill(state: dict[str, Any], posts_db: Any) -> tuple[dict[str, Any], list[str]]:
    updated = deepcopy(state)
    posts = _posts_by_title(posts_db)
    changed: list[str] = []

    for item in updated.get("portfolio", []):
        key = _item_key(item)
        rule = BACKFILL_RULES.get(key)
        if not rule:
            continue
        if item.get("decision_actor") != "미분류" and item.get("evidence_posts"):
            continue

        previous = deepcopy(item)
        evidence_posts = _post_evidence(rule, posts)

        item.update({
            "decision_actor": "AI",
            "action": "보유",
            "basis": rule["basis"],
            "decision_date": BACKFILL_DATE,
            "evidence_posts": evidence_posts,
            "source_mentioned": rule["source_mentioned"],
            "previous_weight": previous.get("proposed_weight"),
            "weight_source": "AI 제안",
            "change_reason": rule["change_reason"],
            "allocation_role": rule["allocation_role"],
            "allocation_role_source": BACKFILL_SOURCE,
            "allocation_role_reason": rule["allocation_role_reason"],
            "source_scope": rule["source_scope"],
            "investment_rationale": rule["investment_rationale"],
            "current_entry_reason": rule["current_entry_reason"],
            "key_risks": rule["key_risks"],
            "legacy_backfill_source": BACKFILL_SOURCE,
        })

        history_entry = deepcopy(item)
        history_entry["backfill_note"] = "과거 보고서/글 DB에서 누락된 근거를 복구한 1회성 데이터 보정"
        updated.setdefault("decision_history", []).append(history_entry)
        changed.append(f"{item.get('name')}({item.get('code')})")

    return updated, changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=STATE_PATH)
    parser.add_argument("--posts-db", type=Path, default=POSTS_DB_PATH)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    state = _load_json(args.state)
    posts_db = _load_json(args.posts_db)
    updated, changed = backfill(state, posts_db)

    if not changed:
        print("No legacy evidence backfill needed.")
        return 0
    print("Backfilled:", ", ".join(changed))
    if args.apply:
        _write_json(args.state, updated)
        print(f"Updated {args.state}")
    else:
        print("Dry-run only. Re-run with --apply to write changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
