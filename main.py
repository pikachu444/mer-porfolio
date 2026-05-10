"""
main.py
메르AI 포트폴리오 자동 분석 — 메인 실행 스크립트

실행 방법:
  python main.py                  # 기본 (최근 14일)
  FETCH_DAYS=7 python main.py     # 최근 7일
  FETCH_DAYS=30 python main.py    # 최근 30일
"""

import os
import sys
from datetime import datetime
from pathlib import Path

from fetch_mer import fetch_recent_posts
from analyze import analyze_posts


# ─── 설정 ────────────────────────────────────────────────────────────────────

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "output"))
FETCH_DAYS = int(os.environ.get("FETCH_DAYS", "14"))


# ─── 출력 저장 ────────────────────────────────────────────────────────────────

def save_report(report: str, today: datetime) -> Path:
    """리포트를 마크다운 파일로 저장하고 경로 반환."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    filename = OUTPUT_DIR / f"report_{today.strftime('%Y%m%d')}.md"

    with open(filename, "w", encoding="utf-8") as f:
        f.write(report)

    # 항상 최신 리포트를 latest.md 로도 저장 (쉬운 접근용)
    latest_path = OUTPUT_DIR / "latest.md"
    with open(latest_path, "w", encoding="utf-8") as f:
        f.write(report)

    return filename


def save_error_log(error: str, today: datetime) -> None:
    """오류 발생 시 로그 파일 저장."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    log_path = OUTPUT_DIR / f"error_{today.strftime('%Y%m%d_%H%M%S')}.log"
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"오류 발생 시각: {today.isoformat()}\n\n{error}")
    print(f"  오류 로그: {log_path}")


# ─── 메인 ─────────────────────────────────────────────────────────────────────

def main() -> int:
    """
    전체 파이프라인 실행.
    Returns: 0 (성공) or 1 (실패)
    """
    today = datetime.now()
    today_str = today.strftime("%Y년 %m월 %d일")

    print("=" * 60)
    print(f"  메르AI 포트폴리오 분석 시작")
    print(f"  실행 일시: {today.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  수집 기간: 최근 {FETCH_DAYS}일")
    print("=" * 60)

    # ── 1단계: 블로그 글 수집 ──────────────────────────────────────────────────
    print("\n[1/3] 메르 블로그 글 수집 중...")
    try:
        posts = fetch_recent_posts(days=FETCH_DAYS)
    except Exception as e:
        msg = f"블로그 수집 실패: {e}"
        print(f"❌ {msg}")
        save_error_log(msg, today)
        return 1

    if not posts:
        msg = (
            f"최근 {FETCH_DAYS}일간 수집된 글이 없습니다.\n"
            "메르가 휴가 중이거나 네트워크 문제일 수 있습니다.\n"
            "FETCH_DAYS 값을 늘려서 재시도해 보세요."
        )
        print(f"⚠ {msg}")
        save_error_log(msg, today)
        return 1

    print(f"  → {len(posts)}편 수집 완료")
    for p in posts[:3]:
        print(f"     · [{p['date']}] {p['title'][:50]}")
    if len(posts) > 3:
        print(f"     · ... 외 {len(posts) - 3}편")

    # ── 2단계: AI 분석 ─────────────────────────────────────────────────────────
    print("\n[2/3] 메르AI 분석 중... (모델에 따라 수 분 소요)")
    try:
        report = analyze_posts(posts, today_str)
    except Exception as e:
        msg = f"AI 분석 실패: {e}"
        print(f"❌ {msg}")
        save_error_log(msg, today)
        return 1

    # ── 3단계: 저장 ────────────────────────────────────────────────────────────
    print("\n[3/3] 리포트 저장 중...")
    try:
        saved_path = save_report(report, today)
        print(f"  → 저장 완료: {saved_path}")
        print(f"  → 최신본: {OUTPUT_DIR}/latest.md")
    except Exception as e:
        print(f"❌ 파일 저장 실패: {e}")
        # 저장 실패해도 stdout에는 출력
        print("\n" + "=" * 60)
        print("리포트 (stdout 출력):")
        print("=" * 60)
        print(report)
        return 1

    # ── 완료 요약 ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("✅ 분석 완료!")
    print(f"   분석 글 수: {len(posts)}편")
    print(f"   리포트 크기: {len(report):,}자")
    print(f"   저장 경로: {saved_path}")
    print("=" * 60)

    # 리포트 앞부분 미리보기 (GitHub Actions 로그에서 확인용)
    preview_lines = report.split("\n")[:30]
    print("\n--- 리포트 미리보기 ---")
    print("\n".join(preview_lines))
    if len(report.split("\n")) > 30:
        print("... (이하 파일 참조)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
