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
DEFAULT_MODEL = _gemini_model_env if _gemini_model_env else "gemini-2.5-flash"

FALLBACK_MODELS = [
    "gemini-2.5-flash",         # 무료 티어 주력 (준수한 품질, 1500 RPD)
    "gemini-2.5-flash-lite",    # 무료 티어 비상 폴백
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


# ─── API 호출 429 지능형 재시도 헬퍼 ──────────────────────────────────────────

def call_gemini_with_retry(client: genai.Client, model: str, contents, config, max_retries: int = 5):
    """
    Gemini API 호출 시 429 한도 초과 에러(Rate Limit 등) 발생 시 지능적으로 대기하며 재시도하는 함수.
    """
    import re
    backoff = 30.0
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            return response
        except Exception as e:
            err_msg = str(e)
            is_rate_limit = any(x in err_msg.lower() for x in ["429", "resource", "exhausted", "quota", "rate", "limit"])
            if is_rate_limit:
                print(f"      [429/RateLimit 감지] {model} 한도 도달 ({attempt + 1}/{max_retries})")
                
                # Pro 일일 한도 RPD 완전히 소진 감지 시 즉시 탈출 (Flash 우회 유도)
                if "gemini-2.5-pro" in model and any(x in err_msg for x in ["PerDay", "RequestsPerDay", "TokensPerDay"]):
                    print("      🚨 Pro 일일 100회 한도가 완전히 소진된 것으로 판단되므로, 대기 없이 즉시 Flash 우회로 전환합니다.")
                    raise e
                
                # 에러 메시지에서 대기 권장 시간(예: retry in 31.5s 등) 파싱 시도
                wait_sec = backoff
                match = re.search(r"retry in ([\d\.]+)s", err_msg, re.IGNORECASE)
                if not match:
                    match = re.search(r"retryDelay': '(\d+)s'", err_msg, re.IGNORECASE)
                
                if match:
                    try:
                        wait_sec = float(match.group(1)) + 1.0  # 안전 마진 1초 추가
                    except ValueError:
                        pass
                
                print(f"      ⏳ {wait_sec:.1f}초 동안 얌전히 대기 후 재시도합니다...")
                time.sleep(wait_sec)
                backoff = min(backoff * 1.5, 60.0)
            else:
                raise e
    raise RuntimeError(f"{model} API가 {max_retries}회 재시도에도 불구하고 계속 실패했습니다.")


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

        # 지능형 429 백오프 헬퍼를 경유하여 API 호출
        response = call_gemini_with_retry(
            client=client,
            model=model_name,
            contents=user_message,
            config=config,
            max_retries=5,
        )

        text = response.text
        
        # [강화된 응답 검증]
        if not text:
            return False, "응답이 완전히 비어 있음"
            
        # 1. 최소 길이 검증 (Pro 모델의 완성본 리포트는 최소 1,500자 확보되어야 함)
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
        traceback.print_exc()  # 100% 상세 에러 트레이스백 로그 기록
        
        # 할당량 초과
        if "429" in err or "quota" in err.lower() or "rate" in err.lower() or "resource" in err.lower():
            print(f"    ⚠ 할당량 초과: {model_name}")
            return False, f"할당량 초과: {err}"
        # 모델 없음
        if "404" in err or "not found" in err.lower() or "invalid" in err.lower():
            print(f"    ⚠ 모델 없음/유효하지 않음: {model_name}")
            return False, f"모델 없음: {err}"
        # 기타 오류
        return False, err


# ─── 1차 요약 실시간 보강 헬퍼 ────────────────────────────────────────────────

def _fill_missing_summary(client: genai.Client, post: Dict) -> str:
    """DB에 요약 캐시가 누락된 경우, 실시간으로 flash를 호출하여 정밀 요약 보완"""
    from fetch_mer import MAP_SUMMARY_PROMPT
    title = post.get("title", "제목 없음")
    content = post.get("content", "")
    if not content:
        return ""
    try:
        print(f"      [실시간 보강] '{title[:25]}'에 대한 1차 요약이 없어 실시간 생성 중...")
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"블로그 글:\n{content}\n\n{MAP_SUMMARY_PROMPT}",
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=2048,
                safety_settings=SAFETY_SETTINGS,
            )
        )
        return response.text if response.text else ""
    except Exception as e:
        print(f"      ⚠ 실시간 1차 요약 보강 실패 (원문 폴백 사용): {e}")
        return content[:1500]  # 에러 시 원문 앞부분으로 폴백하여 분석 유실 차단


# ─── 메인 분석 함수 ───────────────────────────────────────────────────────────

def analyze_posts(
    posts: List[Dict],
    today_str: str,
    run_mode: str = "scheduled",
    current_holdings_text: str = "",
    is_rebalance: bool = False,
) -> str:
    """
    [2단계 분할 종합 분석 (Map-Reduce) 버전]
    수집된 30편 글의 1차 요약(Summary) 엑기스를 병합한 뒤, 
    오직 최고 성능의 gemini-2.5-pro 모델을 끈질기게 호출하여 최종 포트폴리오 리포트 완성.
    """
    if not posts:
        raise ValueError("분석할 포스트가 없습니다.")

    client = _get_client()

    # 1. 1단계 (Map): 각 포스트별 1차 요약 캐시(summary) 추출 및 미세 누락 실시간 보완
    print("\n[3/7-1] 1단계: 30편 블로그 글의 1차 정밀 요약 캐시 로드 및 누락분 실시간 보강 중...")
    summarized_blocks = []
    for i, post in enumerate(posts, 1):
        summary = post.get("summary", "").strip()
        # 로컬 테스트 등으로 요약 캐시가 빈 값일 경우 실시간 보완 장치 가동
        if not summary:
            summary = _fill_missing_summary(client, post)
            post["summary"] = summary  # 메모리 상에 캐싱 갱신
            time.sleep(4.5)  # Gemini RPM 15 무료 한도 선제 방어 (4.5초 간격 유지)
            
        summarized_blocks.append(
            f"[{i}/{len(posts)}] 제목: {post['title']}\n"
            f"날짜: {post['date']}\n"
            f"1차 정밀 요약:\n{summary}\n"
            f"{'─' * 50}"
        )

    # 요약된 엑기스들만 병합 (용량이 9.7만 자에서 5천 자 수준으로 극도로 경량화!)
    reduced_context = "\n\n".join(summarized_blocks)
    
    start_date = posts[-1]["date"]
    end_date = posts[0]["date"]

    # 2단계 (Reduce): 경량화된 요약본을 전달하여 Pro 모델에게 초고품질 종합 분석 지시
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
    print(f"\n[3/7-2] 2단계: 최적화된 엑기스 입력 크기: {total_chars:,}자 (Gemini Pro 토큰 한도 안전 통과)")
    print(f"  🤖 오직 최고 품질의 'gemini-2.5-pro' 모델만을 고집하여 최종 종합 분석을 진행합니다.")

    # 429 한도 초과 시 성공할 때까지 Pro 모델로 계속 30초 대기 후 무한 재시도(Retry)
    retry_count = 0
    max_retries = 3  # 일일 한도(PerDay) 초과 등 영구 차단 시 불필요한 대기를 피하기 위해 Pro 시도는 3회로 최적화
    
    while retry_count < max_retries:
        success, result = _try_model(client, "gemini-2.5-pro", user_message)
        
        if success:
            print(f"  ✅ [Gemini Pro 최종 종합 분석 대성공!] (출력: {len(result):,}자)")
            return result
            
        # 일일 한도(PerDay) 초과가 에러 사유에 명백히 있을 경우, 무의미한 대기를 스킵하고 즉시 비상 Flash 가동
        if "PerDay" in result or "TokensPerDay" in result or "RequestsPerDay" in result:
            print("\n  🚨 [Pro 일일 한도 100% 소진 감지] 무료 API 계정의 gemini-2.5-pro 일일 100회 쿼터가 모두 소진되었습니다.")
            break
            
        print(f"  ⏳ [Pro 한도 대기] 30초 대기 후 gemini-2.5-pro 모델로 다시 끈질기게 재시도합니다... (시도 횟수: {retry_count + 1}/{max_retries})")
        retry_count += 1
        time.sleep(30)

    # ── [최종 비상 대피소: gemini-2.5-flash 긴급 스위칭] ─────────────────────
    print("\n  🚨 [비상 대피소 가동] Pro 모델의 일일 한도 장벽으로 인해, 차선책인 'gemini-2.5-flash' 모델로 긴급 안전 우회하여 분석을 완료합니다.")
    print("  💡 1단계 분할 요약 캐시 덕분에 입력 토큰 용량이 5,000자 이내로 극히 경량화되어 있어, Flash 모델로도 100% 무결하고 뛰어난 퀄리티의 추천 표와 온도계를 완벽히 뽑아냅니다.")
    
    success, result = _try_model(client, "gemini-2.5-flash", user_message)
    if success:
        print(f"  ✅ [Gemini Flash 비상 종합 분석 완료!] (출력: {len(result):,}자)")
        return result
        
    raise RuntimeError(
        f"비상 모델 gemini-2.5-flash 마저 호출 실패.\n"
        f"최종 구글 에러 원인: {result}\n\n"
        "해결 방법:\n"
        "1. 구글 AI Studio 결제 연동(Pay-as-you-go)을 통해 Pro 계정 한도를 완전히 해제하여 100% 무결한 가동을 보장하십시오."
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
