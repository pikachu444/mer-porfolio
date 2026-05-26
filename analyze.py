"""
analyze.py
Google AI Studio (Gemini) 무료 API를 이용한 메르AI 분석 모듈

무료 티어 한도 (2026년 기준):
  - gemini-2.5-pro:        분당 5회,  일 100회 ← 기본값
  - gemini-2.5-flash:      분당 10회, 일 500회 ← 주력 폴백
  - gemini-2.5-flash-lite: 분당 30회, 일 1000회← 비상 폴백

※ gemini-2.0-flash / gemini-2.0-flash-lite 는 2026-06-01 종료 예정 — 사용 금지

API 키 발급: https://aistudio.google.com/app/apikey
환경변수: GEMINI_API_KEY
"""

import os
import time
from typing import List, Dict, Tuple

from google import genai
from google.genai import types

from system_prompt import SYSTEM_PROMPT, build_user_message
from fetch_mer import posts_to_context


# ─── 모델 설정 ────────────────────────────────────────────────────────────────
#
# 2026년 5월 기준 무료 API 모델 (ai.google.dev/gemini-api/docs/pricing 확인):
#   gemini-2.5-pro       : 무료, 5 RPM / 100 RPD  ← 최고 품질, 기본값
#   gemini-2.5-flash     : 무료, 더 넉넉한 한도     ← 주력 폴백
#   gemini-2.5-flash-lite: 무료, 매우 넉넉         ← 비상 폴백
#
# ※ gemini-2.0-flash / gemini-2.0-flash-lite : 2026-06-01 종료 — 제거됨
# ※ gemini-3.1-pro-preview : 유료 전용 (billing 필요). 무료 아님.

_gemini_model_env = os.environ.get("GEMINI_MODEL", "").strip()
DEFAULT_MODEL = _gemini_model_env if _gemini_model_env else "gemini-2.5-pro"

FALLBACK_MODELS = [
    "gemini-2.5-pro",           # 최고품질, 무료 100 RPD
    "gemini-2.5-flash",         # 준수한 품질, 한도 넉넉
    "gemini-2.5-flash-lite",    # 빠르고 한도 많음 — 분석 깊이 다소 얕아짐
]

# 투자 분석 특성상 안전 필터 완화 (주식 분석 용어 오탐 방지)
SAFETY_SETTINGS = [
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
]


# ─── API 클라이언트 초기화 ────────────────────────────────────────────────────

def _get_client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError(
            "GEMINI_API_KEY 환경변수가 설정되지 않았습니다.\n"
            "발급 방법: https://aistudio.google.com/app/apikey\n"
            "설정 방법: export GEMINI_API_KEY='your-key-here'"
        )
    return genai.Client(api_key=api_key)


# ─── 모델별 분석 시도 ─────────────────────────────────────────────────────────

def _try_model(client: genai.Client, model_name: str,
               user_message: str) -> Tuple[bool, str]:
    """
    특정 모델로 분석 시도.
    Returns: (성공 여부, 결과 텍스트 or 오류 메시지)
    """
    try:
        print(f"  🤖 모델 시도: {model_name}")

        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.3,
            top_p=0.85,
            max_output_tokens=8192,
            safety_settings=SAFETY_SETTINGS,
        )

        response = client.models.generate_content(
            model=model_name,
            contents=user_message,
            config=config,
        )

        # 응답 검증
        text = response.text
        if not text or len(text) < 200:
            return False, f"응답이 너무 짧거나 비어 있음 ({len(text) if text else 0}자)"

        return True, text

    except Exception as e:
        err = str(e)
        # 할당량 초과
        if "429" in err or "quota" in err.lower() or "rate" in err.lower() or "resource" in err.lower():
            print(f"    ⚠ 할당량 초과: {model_name}")
            return False, f"할당량 초과: {err}"
        # 모델 없음
        if "404" in err or "not found" in err.lower() or "invalid" in err.lower():
            print(f"    ⚠ 모델 없음/유효하지 않음: {model_name}")
            return False, f"모델 없음: {err}"
        # 기타 오류
        print(f"    ❌ 오류 ({type(e).__name__}): {err[:120]}")
        return False, err


# ─── 메인 분석 함수 ───────────────────────────────────────────────────────────

def analyze_posts(
    posts: List[Dict],
    today_str: str,
    run_mode: str = "scheduled",
    current_holdings_text: str = "",
    is_rebalance: bool = False,
) -> str:
    """
    수집된 포스트 목록을 받아 메르AI 스타일 포트폴리오 리포트 반환.

    Args:
        posts:                 fetch_mer.fetch_recent_posts() 반환값
        today_str:             "2026년 05월 08일" 형식의 오늘 날짜
        run_mode:              "scheduled" | "adhoc" | "test"
        current_holdings_text: 현재 보유 종목 텍스트 (portfolio_state에서)
        is_rebalance:          True면 전면 리밸런싱 모드

    Returns:
        마크다운 형식의 리포트 문자열
    """
    if not posts:
        raise ValueError("분석할 포스트가 없습니다.")

    client = _get_client()

    # 컨텍스트 + 메시지 생성
    context = posts_to_context(posts)
    start_date = posts[-1]["date"]
    end_date = posts[0]["date"]

    user_message = build_user_message(
        context=context,
        today_str=today_str,
        post_count=len(posts),
        start_date=start_date,
        end_date=end_date,
        run_mode=run_mode,
        current_holdings_text=current_holdings_text,
        is_rebalance=is_rebalance,
    )

    total_chars = len(user_message)
    print(f"  📝 총 입력 크기: {total_chars:,}자 (약 {total_chars // 4:,} 토큰 추정)")

    # ── 모델 폴백 로직 ────────────────────────────────────────────────────────
    models_to_try = [DEFAULT_MODEL]
    for m in FALLBACK_MODELS:
        if m not in models_to_try:
            models_to_try.append(m)

    last_error = ""
    for i, model_name in enumerate(models_to_try):
        success, result = _try_model(client, model_name, user_message)

        if success:
            print(f"  ✅ 분석 완료 (모델: {model_name}, 출력: {len(result):,}자)")
            return result

        last_error = result

        # 할당량 초과 시 잠시 대기 후 다음 모델
        if "할당량" in result and i < len(models_to_try) - 1:
            wait_sec = 30
            print(f"  ⏳ {wait_sec}초 대기 후 다음 모델 시도...")
            time.sleep(wait_sec)

    raise RuntimeError(
        f"모든 모델 시도 실패.\n마지막 오류: {last_error}\n\n"
        "해결 방법:\n"
        "1. GEMINI_API_KEY 확인: https://aistudio.google.com/app/apikey\n"
        "2. 무료 할당량 확인: https://ai.google.dev/pricing\n"
        "3. 일일 한도 초과 시 내일 다시 실행\n"
        "4. GEMINI_MODEL 환경변수로 다른 모델 지정 가능"
    )


# ─── 직접 실행 테스트 ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    from datetime import datetime
    from fetch_mer import fetch_recent_posts

    print("=== 분석 모듈 테스트 ===")
    posts = fetch_recent_posts(days=7)
    if not posts:
        print("수집된 글 없음")
    else:
        today_str = datetime.now().strftime("%Y년 %m월 %d일")
        report = analyze_posts(posts, today_str)
        print("\n=== 리포트 미리보기 (앞 1000자) ===")
        print(report[:1000])
