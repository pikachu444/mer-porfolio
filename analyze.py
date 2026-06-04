"""
analyze.py
Google AI Studio (Gemini) 무료 API를 이용한 메르AI 분석 모듈

무료 티어 한도 (2026년 기준):
  - gemini-2.5-pro:   최종 분석 우선 모델
  - gemini-2.5-flash: Pro 실패 시 fallback 모델

API 키 발급: https://aistudio.google.com/app/apikey
환경변수: GEMINI_API_KEY
"""

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Dict, Tuple

from google import genai
from google.genai import types

from portfolio_schema import (
    AnalysisDecisionV2,
    apply_analysis_decision,
    parse_analysis_decision,
    parse_portfolio_state,
)
from system_prompt import (
    DECISION_SYSTEM_PROMPT,
    REPORT_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_decision_user_message,
    build_report_user_message,
    build_user_message,
)
from gemini_utils import DEFAULT_HTTP_TIMEOUT_MS, generate_content_with_retry, is_daily_quota_error


# ─── 모델 설정 ────────────────────────────────────────────────────────────────
#
# 최종 분석은 Pro를 먼저 시도하고, quota/지원 오류가 나면 Flash로 fallback한다.
# GEMINI_MODEL을 지정하면 해당 모델을 우선 시도한다.

_gemini_model_env = os.environ.get("GEMINI_MODEL", "").strip()
PRIMARY_MODEL = _gemini_model_env if _gemini_model_env else "gemini-2.5-pro"
FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash").strip()
MODEL_INPUT_TOKEN_LIMIT = 1_048_576
MODEL_INPUT_SAFE_RATIO = 0.8
MODEL_OUTPUT_TOKEN_LIMIT = 65_536

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


@dataclass(frozen=True)
class StructuredAnalysisResult:
    decision: AnalysisDecisionV2
    report: str


# ─── API 클라이언트 초기화 ────────────────────────────────────────────────────

def _get_client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError(
            "GEMINI_API_KEY 환경변수가 설정되지 않았습니다.\n"
            "발급 방법: https://aistudio.google.com/app/apikey\n"
            "설정 방법: export GEMINI_API_KEY='your-key-here'"
        )
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=DEFAULT_HTTP_TIMEOUT_MS),
    )


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

def _model_sequence() -> list[str]:
    sequence = [PRIMARY_MODEL]
    if FALLBACK_MODEL and FALLBACK_MODEL not in sequence:
        sequence.append(FALLBACK_MODEL)
    return sequence


def _retry_count_for_model(model_name: str) -> int:
    return 1 if "pro" in model_name.lower() else 5


def _try_model(
    client: genai.Client,
    model_name: str,
    user_message: str,
    max_retries: int | None = None,
) -> Tuple[bool, str]:
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
            max_output_tokens=MODEL_OUTPUT_TOKEN_LIMIT,
            safety_settings=SAFETY_SETTINGS,
        )

        # Rate limit 재시도 헬퍼를 경유하여 API 호출
        response = call_gemini_with_retry(
            client=client,
            model=model_name,
            contents=user_message,
            config=config,
            max_retries=max_retries if max_retries is not None else _retry_count_for_model(model_name),
        )

        text = response.text
        
        # [강화된 응답 검증]
        if not text:
            return False, "응답이 완전히 비어 있음"
            
        # 1. 최소 길이 검증
        if len(text) < 1500:
            return False, f"응답 길이가 너무 짧아 분석 중단으로 의심됨 ({len(text)}자)"
            
        # 2. 필수 마크다운 세션 검증 (정규식 파싱 안전성 보증)
        required_headers = ["포트폴리오 추천"]
        missing_headers = [h for h in required_headers if h not in text]
        if missing_headers:
            return False, f"필수 세션 누락: {', '.join(missing_headers)} (Gemini 답변 중간 끊김 발생)"

        return True, text

    except Exception as e:
        err = str(e)

        # 재시도 후에도 남은 일시 오류 또는 할당량 초과
        if any(x in err.lower() for x in ("429", "quota", "rate", "resource", "503", "unavailable", "high demand")):
            print(f"    Gemini API 일시 오류 또는 quota 초과: {model_name} — {err[:180]}")
            return False, f"Gemini API 오류: {err}"
        # 모델 없음
        if "404" in err or "not found" in err.lower() or "invalid" in err.lower():
            print(f"    모델 없음/유효하지 않음: {model_name}")
            return False, f"모델 없음: {err}"
        # 기타 오류
        import traceback
        print(f"    ❌ API 예외 발생: {type(e).__name__} — {err[:120]}")
        print("    [상세 에러 트레이스백]")
        traceback.print_exc()
        return False, err


def _call_model_text(
    client: genai.Client,
    model_name: str,
    user_message: str,
    system_instruction: str,
    response_mime_type: str | None = None,
) -> str:
    """Call one Gemini model and require a non-empty text response."""
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=0.2,
        top_p=0.85,
        max_output_tokens=MODEL_OUTPUT_TOKEN_LIMIT,
        safety_settings=SAFETY_SETTINGS,
        response_mime_type=response_mime_type,
    )
    response = call_gemini_with_retry(
        client=client,
        model=model_name,
        contents=user_message,
        config=config,
        max_retries=_retry_count_for_model(model_name),
    )
    text = response.text
    if not text or not text.strip():
        raise RuntimeError("응답이 완전히 비어 있음")
    return text


def _call_stage_with_fallback(
    client: genai.Client,
    user_message: str,
    system_instruction: str,
    validator: Callable[[str], object],
    stage_name: str,
    response_mime_type: str | None = None,
) -> object:
    """Run one structured-analysis stage with the configured model fallback."""
    errors: list[str] = []
    for model_name in _model_sequence():
        try:
            print(f"  {stage_name} 모델 시도: {model_name}")
            text = _call_model_text(
                client,
                model_name,
                user_message,
                system_instruction,
                response_mime_type,
            )
            for correction_attempt in range(3):
                try:
                    return validator(text)
                except Exception as validation_error:
                    if correction_attempt == 2:
                        raise
                    print(
                        f"    {stage_name} 형식 교정 재시도 "
                        f"{correction_attempt + 1}/2: {model_name} — "
                        f"{str(validation_error)[:180]}"
                    )
                    text = _call_model_text(
                        client,
                        model_name,
                        user_message
                        + "\n\n직전 응답은 다음 검증 오류가 있었습니다:\n"
                        + str(validation_error)
                        + "\n누락된 근거와 필수 필드를 보완하여 요구 형식의 전체 응답을 다시 출력하십시오.",
                        system_instruction,
                        response_mime_type,
                    )
        except Exception as exc:
            errors.append(f"{model_name}: {exc}")
            print(f"    {stage_name} 실패: {model_name} — {str(exc)[:180]}")
    raise RuntimeError(f"{stage_name} 실패. " + " | ".join(errors))


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


def _format_report_date(today_str: str) -> str:
    """LLM이 날짜를 잘못 쓰지 않도록 보고서 날짜를 코드에서 고정한다."""
    match = re.match(r"^\s*(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일\s*$", today_str)
    if not match:
        return today_str

    year, month, day = match.groups()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def _normalize_report_metadata(
    report: str,
    today_str: str,
    post_count: int,
    start_date: str,
    end_date: str,
) -> str:
    """보고서 상단의 결정적 메타데이터는 입력값으로 보정한다."""
    replacements = {
        r"(?m)^\*\*분석 기간:\*\*.*$": f"**분석 기간:** {start_date} ~ {end_date}",
        r"(?m)^\*\*리포트 생성:\*\*.*$": f"**리포트 생성:** {_format_report_date(today_str)}",
        r"(?m)^\*\*학습 글 수:\*\*.*$": f"**학습 글 수:** {post_count}편",
    }

    normalized = report
    for pattern, replacement in replacements.items():
        if re.search(pattern, normalized):
            normalized = re.sub(pattern, replacement, normalized, count=1)

    return normalized


def _structured_context(posts: List[Dict]) -> str:
    """Build the shared blog context for both structured-analysis calls."""
    blocks = []
    for index, post in enumerate(posts, 1):
        label, text = _analysis_text_for_post(post)
        blocks.append(
            f"[{index}/{len(posts)}] 제목: {post['title']}\n"
            f"날짜: {post['date']}\n"
            f"URL: {post.get('url', '')}\n"
            f"{label}:\n{text}\n"
            f"{'─' * 50}"
        )
    return "\n\n".join(blocks)


def _count_tokens(client: genai.Client, model: str, contents: str) -> int | None:
    """Return request tokens when the live client supports token counting."""
    models = getattr(client, "models", None)
    if models is None or not hasattr(models, "count_tokens"):
        return None
    response = models.count_tokens(model=model, contents=contents)
    return int(response.total_tokens)


def _fit_context_to_budget(
    client: genai.Client,
    context: str,
    message_builder: Callable[[str], str],
) -> str:
    """Keep stored inputs intact and trim only an abnormal transmitted context tail."""
    model = PRIMARY_MODEL
    safe_limit = int(MODEL_INPUT_TOKEN_LIMIT * MODEL_INPUT_SAFE_RATIO)
    message = message_builder(context)
    tokens = _count_tokens(client, model, message)
    if tokens is None or tokens <= safe_limit:
        return context

    low, high = 0, len(context)
    suffix = "\n...(전송용 분석 문맥 끝부분 생략)"
    while low < high:
        middle = (low + high + 1) // 2
        candidate = context[:middle] + suffix
        candidate_tokens = _count_tokens(client, model, message_builder(candidate))
        if candidate_tokens is not None and candidate_tokens <= safe_limit:
            low = middle
        else:
            high = middle - 1
    return context[:low] + suffix


def analyze_posts_structured(
    posts: List[Dict],
    analysis_date: str,
    current_state: dict | None,
    *,
    is_rebalance: bool = False,
    decision_validator: Callable[[AnalysisDecisionV2], object] | None = None,
) -> StructuredAnalysisResult:
    """Generate validated decision JSON first, then a Markdown report."""
    if not posts:
        raise ValueError("분석할 포스트가 없습니다.")

    context = _structured_context(posts)
    run_type = "rebalance" if is_rebalance else "regular"
    client = _get_client()

    decision_builder = lambda request_context: build_decision_user_message(
        context=request_context,
        analysis_date=analysis_date,
        run_type=run_type,
        current_state=current_state,
    )
    context = _fit_context_to_budget(client, context, decision_builder)
    decision_message = decision_builder(context)
    decision = _call_stage_with_fallback(
        client,
        decision_message,
        DECISION_SYSTEM_PROMPT,
        lambda text: _parse_and_validate_model_decision_json(
            text,
            current_state,
            decision_validator,
        ),
        "1차 포트폴리오 판단",
        response_mime_type="application/json",
    )
    assert isinstance(decision, AnalysisDecisionV2)

    projected_state = current_state or {}
    if current_state and current_state.get("schema_version") == "2.0":
        projected_state = apply_analysis_decision(
            parse_portfolio_state(current_state),
            decision,
        ).to_dict()

    report_builder = lambda request_context: build_report_user_message(
        context=request_context,
        decision_payload=decision.to_dict(),
        projected_state=projected_state,
        analysis_date=analysis_date,
    )
    report_context = _fit_context_to_budget(client, context, report_builder)
    report_message = report_builder(report_context)
    report = _call_stage_with_fallback(
        client,
        report_message,
        REPORT_SYSTEM_PROMPT,
        _validate_markdown_report,
        "2차 사용자용 보고서",
    )
    assert isinstance(report, str)
    return StructuredAnalysisResult(decision=decision, report=report)


def _validate_markdown_report(report: str) -> str:
    required_headers = [
        "핵심 인사이트",
        "현재 모델 포트폴리오",
        "Watchlist",
        "변경 및 종료 포지션",
    ]
    missing_headers = [header for header in required_headers if header not in report]
    if missing_headers:
        raise ValueError("필수 보고서 섹션 누락: " + ", ".join(missing_headers))
    if len(report) > 20_000:
        raise ValueError(f"보고서가 비정상적으로 깁니다: {len(report)}자")
    longest_line = max((len(line) for line in report.splitlines()), default=0)
    if longest_line > 3_000:
        raise ValueError(f"보고서 한 줄이 비정상적으로 깁니다: {longest_line}자")
    return report


def _parse_model_decision_json(text: str) -> AnalysisDecisionV2:
    """Exclude untradeable portfolio suggestions before strict schema validation."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    payload = json.loads(stripped)
    decisions = payload.get("portfolio_decisions", [])
    if not isinstance(decisions, list):
        return parse_analysis_decision(payload)
    tradeable = []
    for item in decisions:
        if (
            isinstance(item, dict)
            and item.get("asset_type") in {"stock", "etf"}
            and (not isinstance(item.get("code"), str) or not item["code"].strip())
        ):
            print(
                "    거래 불가능 포트폴리오 제안 제외: "
                + str(item.get("name") or "이름 없음")
            )
            continue
        tradeable.append(item)
    payload["portfolio_decisions"] = tradeable
    return parse_analysis_decision(payload)


def _parse_and_validate_model_decision_json(
    text: str,
    current_state: dict | None,
    decision_validator: Callable[[AnalysisDecisionV2], object] | None = None,
) -> AnalysisDecisionV2:
    """Require the model decision to produce an applicable target portfolio."""
    decision = _parse_model_decision_json(text)
    if decision_validator is not None:
        validated = decision_validator(decision)
        if isinstance(validated, AnalysisDecisionV2):
            decision = validated
    if current_state and "schema_version" in current_state:
        state = parse_portfolio_state(current_state)
        apply_analysis_decision(state, decision)
    return decision


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
    models = _model_sequence()
    print(f"  최종 분석 모델 순서: {' -> '.join(models)}")

    client = _get_client()
    last_model = models[-1]
    last_error = ""

    for model_name in models:
        success, result = _try_model(client, model_name, user_message)
        if success:
            result = _normalize_report_metadata(result, today_str, len(posts), start_date, end_date)
            print(f"  최종 분석 완료: {model_name} (출력: {len(result):,}자)")
            return result

        last_model = model_name
        last_error = result

        if "필수 세션 누락" in result or "응답 길이가 너무 짧" in result:
            print("  필수 섹션이 누락되어 형식 지시를 강화해 같은 모델로 한 번 재시도합니다.")
            retry_message = (
                user_message
                + "\n\n중요: 출력은 반드시 '# 메르AI 포트폴리오 리포트'로 시작하고, "
                + "'## 📌 시장 분석 핵심 인사이트', '## 📊 포트폴리오 추천', "
                + "'## 💬 한 줄 코멘트' 섹션을 모두 포함해야 합니다. "
                + "토큰이 부족하면 각 항목을 짧게 줄이더라도 섹션을 생략하지 마세요."
            )
            success, result = _try_model(client, model_name, retry_message, max_retries=1)
            if success:
                result = _normalize_report_metadata(result, today_str, len(posts), start_date, end_date)
                print(f"  최종 분석 완료: {model_name} (형식 재시도, 출력: {len(result):,}자)")
                return result
            last_error = result

        if model_name != models[-1]:
            print(f"  {model_name} 실패 -> 다음 모델로 fallback합니다.")

    if is_daily_quota_error(last_error):
        print(f"  {last_model} daily quota exceeded.")

    raise RuntimeError(
        f"{' -> '.join(models)} 호출 실패. 기존 리포트를 유지합니다.\n"
        f"Google API 오류: {last_error}\n\n"
        "해결 방법:\n"
        "1. GitHub Actions 로그에서 quota 또는 rate limit 원인을 확인하세요.\n"
        "2. 필요하면 Google AI Studio 결제 연동, 실행 빈도 조정, 또는 호출 간격 조정을 검토하세요."
    )


# ─── 직접 실행 테스트 ─────────────────────────────────────────────────────────

if __name__ == "__main__":
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
