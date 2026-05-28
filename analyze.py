"""
analyze.py
Google AI Studio (Gemini) 무료 API를 이용한 메르AI 분석 모듈

무료 티어 한도 (2026년 기준):
  - gemini-2.5-flash:      무료 API 기본 모델
  - gemini-2.5-pro:        프로젝트에 따라 무료 한도가 없을 수 있음

API 키 발급: https://aistudio.google.com/app/apikey
환경변수: GEMINI_API_KEY
"""

import os
from typing import List, Dict, Tuple

from google import genai
from google.genai import types

from system_prompt import SYSTEM_PROMPT, build_user_message
from gemini_utils import generate_content_with_retry, is_daily_quota_error


# ─── 모델 설정 ────────────────────────────────────────────────────────────────
#
# 무료 API 운영에서는 호출 수를 줄이는 것이 우선이다.
# 최종 분석은 기본적으로 gemini-2.5-flash 1회만 호출한다.

_gemini_model_env = os.environ.get("GEMINI_MODEL", "").strip()
FINAL_MODEL = _gemini_model_env if _gemini_model_env else "gemini-2.5-flash"

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


# ─── API 호출 재시도 헬퍼 ────────────────────────────────────────────────────

def call_gemini_with_retry(client: genai.Client, model: str, contents, config, max_retries: int = 3):
    """
    Gemini API 호출 시 rate limit 계열 오류가 발생하면 대기 후 재시도한다.
    """
    return generate_content_with_retry(
        client=client,
        model=model,
        contents=contents,
        config=config,
        max_retries=max_retries,
    )


# ─── 모델별 분석 시도 ─────────────────────────────────────────────────────────

def _try_model(client: genai.Client, model_name: str,
               user_message: str) -> Tuple[bool, str]:
    """
    특정 모델로 분석 시도.
    Returns: (성공 여부, 결과 텍스트 or 오류 메시지)
    """
    try:
        print(f"  모델 시도: {model_name}")

        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.3,
            top_p=0.85,
            max_output_tokens=8192,
            safety_settings=SAFETY_SETTINGS,
        )

        # Rate limit 재시도 헬퍼를 경유하여 API 호출
        response = call_gemini_with_retry(
            client=client,
            model=model_name,
            contents=user_message,
            config=config,
            max_retries=3,
        )

        text = response.text
        
        # [강화된 응답 검증]
        if not text:
            return False, "응답이 완전히 비어 있음"
            
        # 1. 최소 길이 검증
        if len(text) < 1500:
            return False, f"응답 길이가 너무 짧아 분석 중단으로 의심됨 ({len(text)}자)"
            
        # 2. 필수 마크다운 세션 검증 (정규식 파싱 안전성 보증)
        required_headers = ["포트폴리오 추천", "섹터별 온도계"]
        missing_headers = [h for h in required_headers if h not in text]
        if missing_headers:
            return False, f"필수 세션 누락: {', '.join(missing_headers)} (Gemini 답변 중간 끊김 발생)"

        return True, text

    except Exception as e:
        import traceback
        err = str(e)
        print(f"    ❌ API 예외 발생: {type(e).__name__} — {err[:120]}")
        print("    [상세 에러 트레이스백]")
        traceback.print_exc()
        
        # 할당량 초과
        if "429" in err or "quota" in err.lower() or "rate" in err.lower() or "resource" in err.lower():
            print(f"    Rate limit 또는 quota 초과: {model_name}")
            return False, f"할당량 초과: {err}"
        # 모델 없음
        if "404" in err or "not found" in err.lower() or "invalid" in err.lower():
            print(f"    모델 없음/유효하지 않음: {model_name}")
            return False, f"모델 없음: {err}"
        # 기타 오류
        return False, err


# ─── 입력 구성 헬퍼 ──────────────────────────────────────────────────────────

def _analysis_text_for_post(post: Dict) -> tuple[str, str]:
    """요약 캐시가 있으면 요약을, 없으면 원문을 최종 분석 입력으로 사용한다."""
    summary = post.get("summary", "").strip()
    if summary:
        return "1차 요약", summary

    content = post.get("content", "").strip()
    if content:
        return "원문", content

    return "내용 없음", ""


# ─── 메인 분석 함수 ───────────────────────────────────────────────────────────

def analyze_posts(
    posts: List[Dict],
    today_str: str,
    run_mode: str = "scheduled",
    current_holdings_text: str = "",
    is_rebalance: bool = False,
) -> str:
    """
    [2단계 분석]
    수집된 글의 1차 요약을 병합한 뒤 최종 포트폴리오 리포트를 생성한다.
    """
    if not posts:
        raise ValueError("분석할 포스트가 없습니다.")

    # 1. 요약 캐시가 있으면 사용하고, 없으면 API 호출 없이 원문을 사용한다.
    print("\n[3/7-1] 1단계: 블로그 글 요약 캐시 로드 중...")
    summarized_blocks = []
    for i, post in enumerate(posts, 1):
        label, text = _analysis_text_for_post(post)
        if label == "원문":
            print(f"      요약 캐시 없음, 원문 사용: {post.get('title', '제목 없음')[:30]}...")
            
        summarized_blocks.append(
            f"[{i}/{len(posts)}] 제목: {post['title']}\n"
            f"날짜: {post['date']}\n"
            f"{label}:\n{text}\n"
            f"{'─' * 50}"
        )

    # 요약된 블록만 병합해 최종 분석 입력을 줄인다.
    reduced_context = "\n\n".join(summarized_blocks)
    
    start_date = posts[-1]["date"]
    end_date = posts[0]["date"]

    # 2단계: 요약본을 전달하여 최종 포트폴리오 리포트 생성
    user_message = build_user_message(
        context=reduced_context,
        today_str=today_str,
        post_count=len(posts),
        start_date=start_date,
        end_date=end_date,
        run_mode=run_mode,
        current_holdings_text=current_holdings_text,
        is_rebalance=is_rebalance,
    )

    total_chars = len(user_message)
    print(f"\n[3/7-2] 2단계: 최종 분석 입력 크기: {total_chars:,}자")
    print(f"  최종 분석 모델: {FINAL_MODEL}")

    client = _get_client()
    success, result = _try_model(client, FINAL_MODEL, user_message)
    if success:
        print(f"  최종 분석 완료 (출력: {len(result):,}자)")
        return result

    if is_daily_quota_error(result):
        print(f"  {FINAL_MODEL} daily quota exceeded.")

    raise RuntimeError(
        f"{FINAL_MODEL} 호출 실패. 기존 리포트를 유지합니다.\n"
        f"Google API 오류: {result}\n\n"
        "해결 방법:\n"
        "1. GitHub Actions 로그에서 quota 또는 rate limit 원인을 확인하세요.\n"
        "2. 필요하면 Google AI Studio 결제 연동, 실행 빈도 조정, 또는 호출 간격 조정을 검토하세요."
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
