"""
main.py
메르AI 포트폴리오 자동 분석 - 메인 실행 스크립트

실행 모드:
  scheduled  : 자동 스케줄 (새 글 없으면 조기 종료, FETCH_DAYS=2)
  adhoc      : 강제 실행  (새 글 없어도 분석, FETCH_DAYS=14, 리밸런싱)
  test       : 로컬 테스트 (API 호출 O, 텔레그램/portfolio_state 저장 X, FETCH_DAYS=3)

환경변수:
  RUN_MODE      : scheduled | adhoc | test  (기본: scheduled)
  FETCH_DAYS    : 수집 기간(일)  -- 비워두면 모드 기본값 사용
  GEMINI_MODEL  : 모델 오버라이드 (기본: gemini-2.5-pro)
  OUTPUT_DIR    : 출력 디렉터리  (기본: output)
  GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DASHBOARD_URL
"""

import os
import sys
from datetime import datetime
from pathlib import Path

# .env 파일 로드 (로컬 실행용 - GitHub Actions에서는 환경변수 직접 주입)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv 미설치 환경에서도 동작

from fetch_mer import fetch_recent_posts
from analyze import analyze_posts
from track_returns import update_and_get_performance
from generate_dashboard import generate_all
from telegram_notify import send_report, send_photo, send_status
from portfolio_validation import validate_recommendations
from gemini_utils import is_daily_quota_error
from portfolio_state import (
    load_state,
    save_state,
    format_holdings_for_prompt,
    update_state_from_report,
    create_initial_state,
    get_active_holdings,
)


# --- 설정 --------------------------------------------------------------------

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "output"))
STATE_PATH = OUTPUT_DIR / "portfolio_state.json"

RUN_MODE = os.environ.get("RUN_MODE", "scheduled").lower()

# FETCH_DAYS: 환경변수 > 모드 기본값
_FETCH_DAYS_DEFAULT = {"scheduled": 2, "adhoc": 14, "test": 3}
_fetch_days_env = os.environ.get("FETCH_DAYS", "").strip()
FETCH_DAYS = int(_fetch_days_env) if _fetch_days_env else _FETCH_DAYS_DEFAULT.get(RUN_MODE, 2)


# --- 파일 저장 헬퍼 -----------------------------------------------------------

def save_report(report, today):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filename = OUTPUT_DIR / ("report_" + today.strftime("%Y%m%d") + ".md")
    with open(filename, "w", encoding="utf-8") as f:
        f.write(report)
    latest_path = OUTPUT_DIR / "latest.md"
    with open(latest_path, "w", encoding="utf-8") as f:
        f.write(report)
    return filename


def save_error_log(error, today):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    log_path = OUTPUT_DIR / ("error_" + today.strftime("%Y%m%d_%H%M%S") + ".log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("오류 발생 시각: " + today.isoformat() + "\n\n" + error)
    print("  오류 로그: " + str(log_path))


def load_latest_report() -> str:
    latest_path = OUTPUT_DIR / "latest.md"
    if not latest_path.exists():
        return ""
    with open(latest_path, encoding="utf-8") as f:
        return f.read()


# --- 메인 --------------------------------------------------------------------

def notify_status(title: str, body: str = "") -> bool:
    if RUN_MODE == "test":
        return True
    try:
        return send_status(title, body)
    except Exception as e:
        print("  !! Telegram status notification failed: " + str(e))
        return False


def main():
    today = datetime.now()
    today_str = today.strftime("%Y년 %m월 %d일")
    today_date_str = today.strftime("%Y-%m-%d")

    print("=" * 60)
    print("  메르AI 포트폴리오 분석 시작")
    print("  실행 일시: " + today.strftime("%Y-%m-%d %H:%M:%S"))
    print("  실행 모드: " + RUN_MODE.upper())
    print("  수집 기간: 최근 " + str(FETCH_DAYS) + "일")
    if RUN_MODE == "test":
        print("  !! TEST 모드 -- 텔레그램/portfolio_state 저장 스킵")
    print("=" * 60)

    # -- 1단계: portfolio_state 로드 -------------------------------------------
    print("\n[1/7] 포트폴리오 상태 로드 중...")
    state = load_state(STATE_PATH)
    is_first_run = state is None
    is_rebalance = (RUN_MODE in ("adhoc", "test")) or is_first_run

    if is_first_run:
        print("  -> 최초 실행: portfolio_state.json 없음")
    else:
        active = get_active_holdings(state)
        print("  -> 기존 포트폴리오 로드: " + str(len(active)) + "개 종목")
        print("  -> 모드: " + ("리밸런싱" if is_rebalance else "모니터링"))

    current_holdings_text = format_holdings_for_prompt(state)

    # -- 2단계: 블로그 글 수집 -------------------------------------------------
    print("\n[2/7] 메르 블로그 글 수집 중... (최근 " + str(FETCH_DAYS) + "일)")
    try:
        posts = fetch_recent_posts(days=FETCH_DAYS)
    except Exception as e:
        msg = "블로그 수집 실패: " + str(e)
        print("X " + msg)
        save_error_log(msg, today)
        notify_status("MerAI run failed", msg)
        return 1

    if not posts:
        if RUN_MODE == "scheduled":
            print("!! 최근 " + str(FETCH_DAYS) + "일간 새 글 없음 -- scheduled 모드: 정상 종료")
            notify_status("MerAI run finished", "No new posts in the scheduled collection window.")
            return 0
        else:
            print("!! 최근 " + str(FETCH_DAYS) + "일간 새 글 없음 -- " + RUN_MODE + " 모드: 30일로 재수집")
            try:
                posts = fetch_recent_posts(days=30)
                if not posts:
                    print("X 30일간 글도 없음 -- 종료")
                    notify_status("MerAI run failed", "No posts found even after expanding the collection window to 30 days.")
                    return 1
                print("  -> 30일 범위로 재수집: " + str(len(posts)) + "편")
            except Exception as e:
                msg = "재수집 실패: " + str(e)
                print("X " + msg)
                save_error_log(msg, today)
                notify_status("MerAI run failed", msg)
                return 1
    else:
        print("  -> " + str(len(posts)) + "편 수집 완료")
        for p in posts[:3]:
            print("     · [" + p["date"] + "] " + p["title"][:50])
        if len(posts) > 3:
            print("     · ... 외 " + str(len(posts) - 3) + "편")

    # -- 3단계: AI 분석 --------------------------------------------------------
    print("\n[3/7] 메르AI 분석 중... (수 분 소요)")
    try:
        report = analyze_posts(
            posts,
            today_str,
            run_mode=RUN_MODE,
            current_holdings_text=current_holdings_text,
            is_rebalance=is_rebalance,
        )
    except Exception as e:
        msg = "AI 분석 실패: " + str(e)
        print("X " + msg)
        save_error_log(msg, today)
        if is_daily_quota_error(msg) and load_latest_report():
            notify_status(
                "MerAI run skipped",
                "Gemini daily quota exceeded. Previous report was retained; no new portfolio was generated.",
            )
            print("  -> Gemini 일일 quota 초과: 기존 latest.md 유지 후 정상 종료")
            return 0
        notify_status("MerAI run failed", msg[:1500])
        return 1

    # -- 3.5단계: 추천 검증 -----------------------------------------------------
    print("\n[3.5/7] 추천 종목 근거 검증 중...")
    try:
        validation = validate_recommendations(report, posts, state)
        report = validation.report_text
        parsed_portfolio = validation.parsed_portfolio
    except Exception as e:
        print("  !! 추천 검증 실패 (원본 리포트 유지): " + str(e))
        parsed_portfolio = None

    # -- 4단계: portfolio_state 업데이트 ---------------------------------------
    print("\n[4/7] 포트폴리오 상태 업데이트 중...")
    if RUN_MODE != "test":
        try:
            if is_first_run:
                state = create_initial_state(report, today_date_str, parsed_portfolio=parsed_portfolio)
                print("  -> 초기 상태 생성: " + str(len(state["holdings"])) + "개 종목")
            else:
                state = update_state_from_report(
                    state,
                    report,
                    today_date_str,
                    parsed_portfolio=parsed_portfolio,
                    replace_active=is_rebalance,
                )
                if is_rebalance:
                    state["rebalance_count"] = state.get("rebalance_count", 0) + 1
            save_state(state, STATE_PATH)
        except Exception as e:
            print("  !! portfolio_state 업데이트 실패 (건너뜀): " + str(e))
    else:
        print("  -> TEST 모드: portfolio_state 저장 스킵")

    # -- 5단계: 수익률 추적 ---------------------------------------------------
    print("\n[5/7] 누적 수익률 계산 중...")
    try:
        performance_section = update_and_get_performance(report, today)
        report = report + performance_section
    except Exception as e:
        print("  !! 수익률 추적 실패 (건너뜀): " + str(e))

    # -- 6단계: 저장 + 대시보드 -----------------------------------------------
    print("\n[6/7] 리포트 저장 및 대시보드 생성 중...")
    try:
        saved_path = save_report(report, today)
        print("  -> 저장 완료: " + str(saved_path))
    except Exception as e:
        print("X 파일 저장 실패: " + str(e))
        notify_status("MerAI run failed", "Failed to save report: " + str(e))
        return 1

    png_path = None
    try:
        _, png_path = generate_all(report, today)
    except Exception as e:
        print("  !! 대시보드 생성 실패 (건너뜀): " + str(e))

    # -- 7단계: 텔레그램 알림 -------------------------------------------------
    print("\n[7/7] 텔레그램 알림 전송 중...")
    if RUN_MODE != "test":
        try:
            report_sent = False
            if png_path and png_path.exists():
                send_photo(str(png_path), "포트폴리오 성과 | " + today_str)
            report_sent = send_report(report, today_str)
            if not report_sent:
                return 1
        except Exception as e:
            print("  !! 텔레그램 전송 실패 (건너뜀): " + str(e))
            return 1
    else:
        print("  -> TEST 모드: 텔레그램 전송 스킵")

    # -- 완료 -----------------------------------------------------------------
    print("\n" + "=" * 60)
    print("분석 완료!")
    print("  실행 모드: " + RUN_MODE.upper())
    print("  분석 글 수: " + str(len(posts)) + "편")
    print("  리포트 크기: " + str(len(report)) + "자")
    print("  저장 경로: " + str(saved_path))
    if RUN_MODE != "test" and state:
        active = get_active_holdings(state)
        print("  포트폴리오: " + str(len(active)) + "개 종목 active")
    print("=" * 60)

    preview_lines = report.split("\n")[:20]
    print("\n--- 리포트 미리보기 ---")
    print("\n".join(preview_lines))
    if len(report.split("\n")) > 20:
        print("... (이하 파일 참조)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
