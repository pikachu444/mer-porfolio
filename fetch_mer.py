"""
fetch_mer.py
메르 블로그(blog.naver.com/ranto28) RSS 파싱 + 전문 스크래핑 모듈

네이버 블로그는 iframe 구조라 데스크탑 URL은 JS 없이 파싱 불가.
모바일 URL(m.blog.naver.com)을 사용하면 일반 HTML로 전문 접근 가능.
"""

import feedparser
import requests
from bs4 import BeautifulSoup
import hashlib
import re
import json
import os
import unicodedata
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from pathlib import Path
from google import genai
from google.genai import types
from gemini_utils import (
    SUMMARY_HTTP_TIMEOUT_MS,
    generate_content_with_retry,
    is_permanent_error,
    is_transient_error,
)

# ─── 설정 ───────────────────────────────────────────────────────────────────

BLOG_ID = "ranto28"
RSS_URL = f"https://rss.blog.naver.com/{BLOG_ID}.xml"
MOBILE_BASE = "https://m.blog.naver.com"
STATE_FILE = "last_processed.json"
DB_FILE = str(Path(os.environ.get("OUTPUT_DIR", "output")) / "posts_db.json")
_fetch_days_env = os.environ.get("FETCH_DAYS", "").strip()
DEFAULT_DAYS = int(_fetch_days_env) if _fetch_days_env else 14  # 빈 문자열 방어
_summary_env = os.environ.get("ENABLE_POST_SUMMARIES", "true").strip().lower()
ENABLE_POST_SUMMARIES = _summary_env not in ("0", "false", "no", "off")
SUMMARY_MODEL = (
    os.environ.get("GEMINI_SUMMARY_MODEL", "gemini-3.1-flash-lite").strip()
    or "gemini-3.1-flash-lite"
)
SUMMARY_VERSION = 4
MODEL_INPUT_TOKEN_LIMIT = 1_048_576
MODEL_INPUT_SAFE_RATIO = 0.8
SUMMARY_OUTPUT_TOKEN_LIMIT = 2_048
# A cache-schema upgrade must never turn one scheduled run into a large batch of
# free-tier requests.  New RSS entries are still summarized immediately; these
# limits apply only to upgrading already-persisted summaries and retrying a
# previously deferred summary.
SUMMARY_CACHE_UPGRADE_MAX_PER_RUN = max(
    0,
    int(os.environ.get("SUMMARY_CACHE_UPGRADE_MAX_PER_RUN", "4")),
)
SUMMARY_DEFERRED_RETRY_MAX_PER_RUN = max(
    0,
    int(os.environ.get("SUMMARY_DEFERRED_RETRY_MAX_PER_RUN", "1")),
)
SUMMARY_RETRY_BASE_SECONDS = max(
    60,
    int(os.environ.get("SUMMARY_RETRY_BASE_SECONDS", str(6 * 60 * 60))),
)
SUMMARY_RETRY_MAX_SECONDS = max(
    SUMMARY_RETRY_BASE_SECONDS,
    int(os.environ.get("SUMMARY_RETRY_MAX_SECONDS", str(7 * 24 * 60 * 60))),
)
SUMMARY_DEFERRED_TEXT = "글별 Flash-Lite 요약 실패로 투자 분석 보류"
_SUMMARY_CLIENT: genai.Client | None = None
SUMMARY_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "investment_relevant": {"type": "boolean"},
        "relevance_reason": {"type": "string"},
        "summary": {"type": "string"},
        "signal_candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "exact_text": {"type": "string"},
                    "classification": {
                        "type": "string",
                        "enum": ["MER_DIRECT", "DIRECTIONAL_THESIS", "MENTION_ONLY"],
                    },
                    "entity_name": {"type": "string"},
                    "entity_type": {"type": "string"},
                    "direction": {"type": "string"},
                    "horizon_kind": {"type": "string"},
                    "catalysts": {"type": "array", "items": {"type": "string"}},
                    "invalidation_conditions": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "thesis_summary": {"type": "string"},
                },
                "required": [
                    "exact_text",
                    "classification",
                    "entity_name",
                    "entity_type",
                    "direction",
                    "horizon_kind",
                    "catalysts",
                    "invalidation_conditions",
                    "thesis_summary",
                ],
            },
        },
    },
    "required": [
        "investment_relevant",
        "relevance_reason",
        "summary",
        "signal_candidates",
    ],
}

SIGNAL_CLASSIFICATIONS = {"MER_DIRECT", "DIRECTIONAL_THESIS", "MENTION_ONLY"}
SIGNAL_CANDIDATE_FIELDS = (
    "exact_text",
    "classification",
    "entity_name",
    "entity_type",
    "direction",
    "horizon_kind",
    "catalysts",
    "invalidation_conditions",
    "thesis_summary",
)

# 1차 정밀 요약 프롬프트
MAP_SUMMARY_PROMPT = """
당신은 거시경제 분석가 메르의 글을 정밀 압축하고 투자 관련 여부를 분류하는 1차 요약 엔진입니다.
제공된 블로그 전문을 읽고 JSON 객체 하나만 출력하십시오.

출력 구조:
{
  "investment_relevant": true,
  "relevance_reason": "투자 분석 포함 또는 제외 이유",
  "summary": "정밀 압축 요약",
  "signal_candidates": []
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
`summary`는 1200자 이하, `relevance_reason`은 200자 이하로 작성하십시오.

`signal_candidates`에는 반드시 원문에서 그대로 복사한 `exact_text`가 있는 후보만 기록하십시오.
문장 부호와 숫자를 바꾸거나 여러 문장을 재구성하지 마십시오. 후보별 필드는 다음과 같습니다.
- `classification`: 메르 본인의 명시적 매수·보유·매도·관심 공개는 `MER_DIRECT`, 기업·산업의
  수혜/피해 방향과 인과관계가 있는 논지는 `DIRECTIONAL_THESIS`, 이름만 언급된 경우는
  `MENTION_ONLY`.
- `entity_name`, `entity_type`: 근거가 가리키는 기업·종목·ETF·산업·국가·자산의 이름과 유형.
- `direction`: 수혜/피해/중립/혼합 등 원문이 뒷받침하는 방향. 불명확하면 빈 문자열.
- `horizon_kind`: event/tactical/cyclical/structural 중 원문으로 판단 가능한 값. 불명확하면 빈 문자열.
- `catalysts`, `invalidation_conditions`: 원문이 명시한 항목만 기록하고 없으면 빈 배열.
- `thesis_summary`: 원문 범위를 넘지 않는 한 문장 요약.
동일 근거와 대상을 반복하지 마십시오. 투자 근거 후보가 없으면 빈 배열을 출력하십시오.
출력은 반드시 지정된 JSON 스키마를 만족해야 하며, 마크다운 코드블록을 쓰지 마십시오.
"""

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
        "Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://m.blog.naver.com/",
}

LAST_FETCH_NEW_POST_COUNT = 0
LAST_FETCH_NEW_POST_URLS: set[str] = set()


class SummaryResponseError(ValueError):
    """Raised when the per-post Gemini summary response cannot be used."""


# ─── 상태 관리 ───────────────────────────────────────────────────────────────

def get_last_processed_date() -> datetime:
    """마지막 처리 날짜 로드. 없으면 DEFAULT_DAYS 이전 반환."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
                return datetime.fromisoformat(data["last_date"])
        except Exception:
            pass
    return datetime.now() - timedelta(days=DEFAULT_DAYS)


def save_last_processed_date(dt: datetime):
    """마지막 처리 날짜 저장."""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_date": dt.isoformat()}, f, ensure_ascii=False)


# ─── URL 파싱 ─────────────────────────────────────────────────────────────────

def extract_post_id(url: str) -> Optional[str]:
    """
    RSS 링크에서 포스트 번호 추출.
    예: https://blog.naver.com/ranto28/223812345678 -> '223812345678'
    """
    match = re.search(r"/(\d{10,})(?:\?|$)", url)
    if match:
        return match.group(1)
    # logNo 파라미터 방식
    match = re.search(r"[?&]logNo=(\d+)", url)
    return match.group(1) if match else None


# ─── 스크래핑 ─────────────────────────────────────────────────────────────────

def fetch_full_post(post_id: str, title: str = "") -> str:
    """
    모바일 URL에서 블로그 포스트 전문 스크래핑.
    SmartEditor 3 / 2 / 레거시 에디터 순서로 시도.
    """
    url = f"{MOBILE_BASE}/{BLOG_ID}/{post_id}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")

        # 방법 1: SmartEditor 3 (2020년 이후 대부분의 글)
        container = soup.find("div", class_="se-main-container")
        if container:
            text = container.get_text(separator="\n", strip=True)
            if len(text) > 100:
                return _clean_text(text)

        # 방법 2: 구버전 SmartEditor 2
        container = soup.find("div", id="postViewArea")
        if container:
            text = container.get_text(separator="\n", strip=True)
            if len(text) > 100:
                return _clean_text(text)

        # 방법 3: 또 다른 래퍼 클래스들
        for cls in ["post_ct", "view", "__se_component_area"]:
            container = soup.find("div", class_=cls)
            if container:
                text = container.get_text(separator="\n", strip=True)
                if len(text) > 100:
                    return _clean_text(text)

        # 최후 수단: body 전체에서 네비게이션 제거 후 추출
        for tag in soup(["nav", "header", "footer", "script", "style"]):
            tag.decompose()
        body = soup.find("body")
        if body:
            return _clean_text(body.get_text(separator="\n", strip=True))

        return ""

    except requests.exceptions.Timeout:
        print(f"  ⚠ 타임아웃: {title[:40]}")
        return ""
    except requests.exceptions.HTTPError as e:
        print(f"  ⚠ HTTP 오류 ({e.response.status_code}): {title[:40]}")
        return ""
    except Exception as e:
        print(f"  ⚠ 오류 ({type(e).__name__}): {title[:40]} — {e}")
        return ""


def _clean_text(text: str) -> str:
    """
    스크래핑된 텍스트 정리:
    - Zero-width space, BOM 등 유니코드 제어문자 제거
    - 연속 빈 줄 2개 이상 → 1개로
    - 의미 없는 짧은 줄 제거
    """
    # 네이버 스마트에디터가 삽입하는 유니코드 제어문자 제거
    import re
    text = re.sub(r"[\u200b-\u200f\u2028\u2029\ufeff\u00ad]", "", text)
    text = text.replace("\u00a0", " ")

    lines = text.split("\n")
    cleaned = []
    prev_empty = False
    for line in lines:
        line = line.strip()
        if not line:
            if not prev_empty:
                cleaned.append("")
            prev_empty = True
        else:
            prev_empty = False
            cleaned.append(line)
    return "\n".join(cleaned).strip()


# ─── 메인 수집 함수 ───────────────────────────────────────────────────────────

def _count_tokens(client: genai.Client, model: str, contents: str) -> int:
    response = client.models.count_tokens(model=model, contents=contents)
    return int(response.total_tokens)


def _fit_summary_request(client: genai.Client, content: str) -> str:
    """Trim only the transmitted tail when an abnormal article exceeds the safe budget."""
    prefix = "블로그 글:\n"
    suffix = "\n\n" + MAP_SUMMARY_PROMPT
    request = prefix + content + suffix
    safe_limit = int(MODEL_INPUT_TOKEN_LIMIT * MODEL_INPUT_SAFE_RATIO)
    if _count_tokens(client, SUMMARY_MODEL, request) <= safe_limit:
        return request

    low, high = 0, len(content)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = prefix + content[:middle] + "\n...(전송용 본문 끝부분 생략)" + suffix
        if _count_tokens(client, SUMMARY_MODEL, candidate) <= safe_limit:
            low = middle
        else:
            high = middle - 1
    return prefix + content[:low] + "\n...(전송용 본문 끝부분 생략)" + suffix


def _normalize_evidence_text(text: str) -> str:
    """Normalize source and quote identically without changing word order."""
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    normalized = re.sub(r"[\u200b-\u200f\u2028\u2029\ufeff\u00ad]", "", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _string_list(value) -> list[str] | None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return None
    return [item.strip() for item in value if item.strip()]


def _validated_signal_candidates(
    raw_candidates,
    source_text: str,
    source_key: str,
) -> list[dict]:
    """Keep only candidates whose exact quote is present in the normalized source."""
    if not isinstance(raw_candidates, list):
        return []

    normalized_source = _normalize_evidence_text(source_text)
    if not normalized_source:
        return []

    validated: list[dict] = []
    seen_ids: set[str] = set()
    for raw in raw_candidates:
        if not isinstance(raw, dict):
            continue
        if any(field not in raw for field in SIGNAL_CANDIDATE_FIELDS):
            continue

        exact_text = _normalize_evidence_text(raw.get("exact_text", ""))
        classification = str(raw.get("classification", "")).strip()
        if (
            not exact_text
            or exact_text not in normalized_source
            or classification not in SIGNAL_CLASSIFICATIONS
        ):
            continue

        string_fields: dict[str, str] = {}
        malformed = False
        for field in (
            "entity_name",
            "entity_type",
            "direction",
            "horizon_kind",
            "thesis_summary",
        ):
            value = raw.get(field)
            if not isinstance(value, str):
                malformed = True
                break
            string_fields[field] = value.strip()
        catalysts = _string_list(raw.get("catalysts"))
        invalidation_conditions = _string_list(raw.get("invalidation_conditions"))
        if malformed or catalysts is None or invalidation_conditions is None:
            continue

        evidence_sha256 = hashlib.sha256(exact_text.encode("utf-8")).hexdigest()
        identity_parts = (
            _normalize_evidence_text(source_key),
            evidence_sha256,
            classification,
            _normalize_evidence_text(string_fields["entity_name"]),
            _normalize_evidence_text(string_fields["entity_type"]),
            _normalize_evidence_text(string_fields["direction"]),
        )
        signal_id = hashlib.sha256("\x1f".join(identity_parts).encode("utf-8")).hexdigest()
        if signal_id in seen_ids:
            continue
        seen_ids.add(signal_id)
        validated.append(
            {
                "signal_id": signal_id,
                "evidence_sha256": evidence_sha256,
                "exact_text": exact_text,
                "classification": classification,
                **string_fields,
                "catalysts": catalysts,
                "invalidation_conditions": invalidation_conditions,
            }
        )
    return validated


def _parse_summary_response(
    text: str,
    *,
    source_text: str = "",
    source_key: str = "",
) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise SummaryResponseError(f"글별 요약 JSON 파싱 실패: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise SummaryResponseError("글별 요약 응답은 JSON 객체여야 합니다.")
    summary = payload.get("summary")
    relevant = payload.get("investment_relevant")
    reason = payload.get("relevance_reason")
    if not isinstance(summary, str) or not summary.strip():
        raise SummaryResponseError("글별 요약에 summary가 없습니다.")
    if not isinstance(relevant, bool):
        raise SummaryResponseError("글별 요약에 investment_relevant boolean이 없습니다.")
    if not isinstance(reason, str) or not reason.strip():
        raise SummaryResponseError("글별 요약에 relevance_reason이 없습니다.")
    signal_candidates = _validated_signal_candidates(
        payload.get("signal_candidates", []),
        source_text,
        source_key,
    )
    return {
        "summary": summary.strip(),
        "investment_relevant": relevant,
        "relevance_reason": reason.strip(),
        "signal_candidates": signal_candidates,
        "summary_version": SUMMARY_VERSION,
    }


def _get_summary_client() -> genai.Client:
    global _SUMMARY_CLIENT
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY 미설정: 글별 요약을 생성할 수 없습니다.")
    if _SUMMARY_CLIENT is None:
        _SUMMARY_CLIENT = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=SUMMARY_HTTP_TIMEOUT_MS,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
    return _SUMMARY_CLIENT


def summarize_single_post(content: str, source_key: str = "") -> dict:
    """Use the explicit Flash-Lite model to summarize and classify one article."""
    client = _get_summary_client()
    response = generate_content_with_retry(
        client=client,
        model=SUMMARY_MODEL,
        contents=_fit_summary_request(client, content),
        config=types.GenerateContentConfig(
            max_output_tokens=SUMMARY_OUTPUT_TOKEN_LIMIT,
            response_mime_type="application/json",
            response_schema=SUMMARY_RESPONSE_SCHEMA,
            thinking_config=types.ThinkingConfig(
                thinking_level=types.ThinkingLevel.MINIMAL,
            ),
        ),
        max_retries=3,
        http_timeout_ms=SUMMARY_HTTP_TIMEOUT_MS,
    )
    if not response.text:
        raise SummaryResponseError("글별 Flash 요약 응답이 비어 있습니다.")
    parsed = _parse_summary_response(
        response.text,
        source_text=content,
        source_key=source_key,
    )
    model_version = getattr(response, "model_version", None)
    if not isinstance(model_version, str) or not model_version.strip():
        model_version = SUMMARY_MODEL
    parsed["summary_model_id"] = SUMMARY_MODEL
    parsed["summary_model_version"] = model_version.strip()
    return parsed


def _deferred_summary_fields(error: Exception) -> dict:
    message = f"{type(error).__name__}: {error}"
    return {
        "summary": SUMMARY_DEFERRED_TEXT,
        "investment_relevant": False,
        "relevance_reason": SUMMARY_DEFERRED_TEXT,
        "summary_version": None,
        "summary_status": "deferred",
        "summary_model_id": SUMMARY_MODEL,
        "summary_model_version": None,
        "signal_candidates": [],
        "summary_error": message[:500],
    }


def _summary_fields(content: str, title: str, source_key: str = "") -> dict:
    if not ENABLE_POST_SUMMARIES:
        print("      글별 요약 OFF -> 기존 캐시 또는 원문을 사용")
        return {
            "summary": "",
            "investment_relevant": None,
            "relevance_reason": "",
            "summary_version": None,
            "summary_model_id": None,
            "summary_model_version": None,
            "signal_candidates": [],
        }
    print(f"      1차 요약 캐시 생성(Flash): {title[:30]}...")
    try:
        result = summarize_single_post(content, source_key=source_key)
    except SummaryResponseError as exc:
        print(f"      글별 요약 보류: {type(exc).__name__}: {exc}")
        return _deferred_summary_fields(exc)
    except Exception as exc:
        message = str(exc)
        if not is_transient_error(message) and not is_permanent_error(message):
            raise
        print(f"      글별 요약 API 실패, 보류: {type(exc).__name__}: {exc}")
        return _deferred_summary_fields(exc)
    return {
        **result,
        "summary_status": "ok",
        "summary_error": "",
    }


def _write_posts_db(posts: list[dict]) -> None:
    path = Path(DB_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(posts, f, ensure_ascii=False, indent=2)


def _retry_count(post: dict) -> int:
    try:
        return max(0, int(post.get("summary_retry_count", 0)))
    except (TypeError, ValueError):
        return 0


def _summary_retry_due(post: dict, now: datetime) -> bool:
    """Return whether a deferred summary may consume this run's retry slot."""
    next_retry = post.get("summary_next_retry_at")
    if not isinstance(next_retry, str) or not next_retry.strip():
        # Legacy deferred cache entries predate retry scheduling.  They remain
        # retryable, but only through the per-run cap below.
        return True
    try:
        return datetime.fromisoformat(next_retry) <= now
    except (TypeError, ValueError):
        return True


def _schedule_summary_retry(post: dict, now: datetime) -> None:
    """Persist exponential retry metadata without giving up on a source post."""
    retry_count = _retry_count(post) + 1
    # Cap the exponent as well as the delay to avoid oversized integer work on
    # malformed cache data while keeping retry cadence bounded at seven days.
    delay = min(
        SUMMARY_RETRY_BASE_SECONDS * (2 ** min(retry_count - 1, 16)),
        SUMMARY_RETRY_MAX_SECONDS,
    )
    post["summary_retry_count"] = retry_count
    post["summary_failed_at"] = now.isoformat(timespec="seconds")
    post["summary_next_retry_at"] = (
        now + timedelta(seconds=delay)
    ).isoformat(timespec="seconds")


def _clear_summary_retry_metadata(post: dict) -> None:
    for key in (
        "summary_retry_count",
        "summary_next_retry_at",
        "summary_failed_at",
    ):
        post.pop(key, None)


def _apply_refreshed_summary(
    post: dict,
    summary_fields: dict,
    now: datetime,
    *,
    was_pending_retry: bool,
) -> None:
    """Apply one cache refresh while preserving post-analysis semantics.

    A v2 -> v4 cache upgrade is provenance enrichment, not a newly discovered
    investment signal.  It must therefore retain its existing analysis status
    (normally ``legacy_untracked``) so a partially migrated historical window
    cannot produce a partial portfolio decision.  A real deferred/pending post
    keeps the prior fail-closed behavior and becomes pending only after a
    usable retry succeeds.
    """
    post.update(summary_fields)
    if summary_fields.get("summary_status") == "deferred":
        _schedule_summary_retry(post, now)
        return

    _clear_summary_retry_metadata(post)
    if not was_pending_retry:
        return
    if summary_fields.get("investment_relevant") is True:
        post["analysis_status"] = "pending"
        post.pop("analysis_completed_date", None)
    elif summary_fields.get("investment_relevant") is False:
        post["analysis_status"] = "not_relevant"
        post.pop("analysis_completed_date", None)


def _refresh_recent_summary_cache(posts: list[dict], now: datetime) -> bool:
    """Boundedly upgrade recent cache entries and retry deferred source posts.

    Older cache schema upgrades are intentionally not treated as current
    analysis work.  This prevents a rollout from both exhausting the free API
    quota and making a decision from only a fraction of the historical window.
    A genuinely pending/deferred input remains fail-closed and is retried even
    after the normal 14-day upgrade window, but only in a capped retry slot.
    """
    if not ENABLE_POST_SUMMARIES:
        return False

    cutoff = now - timedelta(days=14)
    retries: list[tuple[datetime, int, dict]] = []
    upgrades: list[tuple[datetime, int, dict]] = []
    for index, post in enumerate(posts):
        try:
            post_date = datetime.fromisoformat(post["date"])
        except Exception:
            continue
        retry_blocked_pending = (
            post.get("summary_status") == "deferred"
            or (
                post.get("analysis_status") == "pending"
                and not str(post.get("summary") or "").strip()
                and post.get("investment_relevant") is not False
            )
        )
        if retry_blocked_pending:
            if _summary_retry_due(post, now):
                retries.append((post_date, index, post))
            continue
        if post_date < cutoff:
            continue
        if post.get("summary_version") != SUMMARY_VERSION:
            upgrades.append((post_date, index, post))

    # Most recent source material first.  The index makes equal publication
    # dates deterministic while avoiding dependence on RSS list ordering.
    retries.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    upgrades.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    selected = [
        (post, True)
        for _, _, post in retries[:SUMMARY_DEFERRED_RETRY_MAX_PER_RUN]
    ] + [
        (post, False)
        for _, _, post in upgrades[:SUMMARY_CACHE_UPGRADE_MAX_PER_RUN]
    ]

    changed = False
    for post, was_pending_retry in selected:
        post_id = extract_post_id(post.get("url", ""))
        if post_id:
            refreshed = fetch_full_post(post_id, post.get("title", ""))
            if refreshed:
                post["content"] = refreshed
        summary_fields = _summary_fields(
            post.get("content", ""),
            post.get("title", ""),
            post.get("url", ""),
        )
        _apply_refreshed_summary(
            post,
            summary_fields,
            now,
            was_pending_retry=was_pending_retry,
        )
        _write_posts_db(posts)
        changed = True
    return changed


def fetch_recent_posts(days: int = DEFAULT_DAYS) -> List[Dict]:
    """
    신규 작성된 글만 수집해 posts_db.json에 누적하고 최신 30개를 반환한다.
    """
    global LAST_FETCH_NEW_POST_COUNT, LAST_FETCH_NEW_POST_URLS

    # 1. 기존 누적 데이터베이스 로드
    db_posts = []
    db_path = Path(DB_FILE)
    if db_path.exists():
        try:
            with open(db_path, encoding="utf-8") as f:
                db_posts = json.load(f)
            if isinstance(db_posts, list):
                for post in db_posts:
                    if isinstance(post, dict):
                        post.setdefault("signal_candidates", [])
                        post.setdefault("analysis_status", "legacy_untracked")
            print(f"기존 posts_db.json 로드 성공 (총 {len(db_posts)}편)")
        except Exception as e:
            print(f"기존 posts_db.json 로드 실패, 새로 빌드합니다: {e}")

    existing_urls = {p["url"] for p in db_posts}

    # 2. RSS 파싱 시작
    print(f"RSS 파싱 중: {RSS_URL}")
    feed = feedparser.parse(RSS_URL)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"RSS 파싱 실패: {feed.bozo_exception}")

    cutoff = datetime.now() - timedelta(days=days)
    print(f"수집 기간: {cutoff.strftime('%Y-%m-%d')} ~ 오늘")

    new_posts_count = 0
    newly_added = []

    for entry in feed.entries:
        try:
            pub_date = datetime(*entry.published_parsed[:6])
        except (AttributeError, TypeError):
            continue

        if pub_date < cutoff:
            continue

        # 중복 URL 체크 (이미 DB에 있다면 부분 수집 건너뜀)
        if entry.link in existing_urls:
            continue

        post_id = extract_post_id(entry.link)
        if not post_id:
            continue

        title = entry.get("title", "제목 없음").strip()
        print(f"  신규 글: [{pub_date.strftime('%m/%d')}] {title[:45]}...")

        # 신규 전문 스크래핑
        full_text = fetch_full_post(post_id, title)
        if not full_text:
            full_text = entry.get("summary", "")

        summary_fields = _summary_fields(full_text, title, entry.link)
        new_post = {
            "title": title,
            "date": pub_date.strftime("%Y-%m-%d"),
            "url": entry.link,
            "content": full_text,
            **summary_fields,
            "analysis_status": (
                "pending"
                if summary_fields.get("summary_status") == "deferred"
                or summary_fields.get("investment_relevant") is True
                else "not_relevant"
            ),
        }
        if summary_fields.get("summary_status") == "deferred":
            # A failed fresh source remains fail-closed, but must not be
            # retried a second time during this same collection run.
            _schedule_summary_retry(new_post, datetime.now())
        newly_added.append(new_post)
        new_posts_count += 1

    # 3. 새로운 포스트 병합 및 저장
    if newly_added:
        db_posts.extend(newly_added)
        # 날짜 최신순 정렬
        db_posts.sort(key=lambda x: x["date"], reverse=True)
        _write_posts_db(db_posts)
        print(f"신규 {new_posts_count}편을 posts_db.json에 저장했습니다.")
    else:
        print("신규 글이 없습니다.")
    if _refresh_recent_summary_cache(db_posts, datetime.now()):
        print("최근 14일 글의 원문과 Flash 요약 캐시를 새 정책으로 갱신했습니다.")
    if newly_added or db_posts:
        _write_posts_db(db_posts)
    LAST_FETCH_NEW_POST_COUNT = new_posts_count
    LAST_FETCH_NEW_POST_URLS = {post["url"] for post in newly_added}

    # 4. 요청 기간에 해당하는 글만 분석 대상으로 반환한다.
    # 무료 API에서는 입력 크기도 quota에 영향을 주므로 오래된 글을 매번 다시 넣지 않는다.
    final_posts = []
    for post in db_posts:
        try:
            post_date = datetime.fromisoformat(post["date"])
        except Exception:
            continue
        if post_date >= cutoff:
            final_posts.append(post)

    print(f"수집 기간 내 {len(final_posts)}편을 분석 대상으로 반환합니다.")
    return final_posts


def get_last_fetch_new_post_count() -> int:
    return LAST_FETCH_NEW_POST_COUNT


def get_last_fetch_new_post_urls() -> set[str]:
    return set(LAST_FETCH_NEW_POST_URLS)


def load_cached_posts() -> list[dict]:
    path = Path(DB_FILE)
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        posts = json.load(f)
    if not isinstance(posts, list):
        return []
    for post in posts:
        if isinstance(post, dict):
            post.setdefault("signal_candidates", [])
            post.setdefault("analysis_status", "legacy_untracked")
    return posts


def is_investment_relevant(post: dict) -> bool:
    """Treat legacy caches as relevant until the recent-window upgrade replaces them."""
    return post.get("investment_relevant") is not False


def is_ready_for_analysis(post: dict) -> bool:
    """Require a usable summary before a post can enter Pro analysis."""
    # A legacy cache row may look complete but has no validated v4 signal
    # candidates.  Keep this gate in the selector itself so direct callers
    # cannot accidentally treat a partial cache upgrade as fresh source input.
    if post.get("summary_version") != SUMMARY_VERSION:
        return False
    if post.get("investment_relevant") is not True:
        return False
    if post.get("summary_status") == "deferred":
        return False
    return bool(str(post.get("summary") or "").strip())


def select_new_relevant_posts(posts: list[dict], new_urls: set[str]) -> list[dict]:
    return [
        post for post in posts
        if (
            post.get("url") in new_urls
            or post.get("analysis_status") == "pending"
        )
        and is_ready_for_analysis(post)
    ]


def mark_posts_analysis_completed(
    urls: set[str],
    analysis_date: str,
    path: Path | None = None,
) -> None:
    """Acknowledge posts only after the portfolio state bundle was committed."""
    target = path or Path(DB_FILE)
    if not urls or not target.exists():
        return
    with open(target, encoding="utf-8") as file:
        posts = json.load(file)
    changed = False
    for post in posts if isinstance(posts, list) else []:
        if isinstance(post, dict) and post.get("url") in urls:
            post["analysis_status"] = "completed"
            post["analysis_completed_date"] = analysis_date
            changed = True
    if changed:
        temporary = target.with_suffix(target.suffix + ".tmp")
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump(posts, file, ensure_ascii=False, indent=2)
        temporary.replace(target)


def select_rebalance_posts(
    posts: list[dict],
    last_rebalanced_date: str | None,
    today: datetime,
) -> list[dict]:
    cutoff = (
        datetime.fromisoformat(last_rebalanced_date)
        if last_rebalanced_date
        else today - timedelta(days=14)
    )
    return [
        post for post in posts
        if datetime.fromisoformat(post["date"]) > cutoff and is_ready_for_analysis(post)
    ]


def posts_to_context(posts: List[Dict]) -> str:
    """
    수집된 포스트 목록을 AI 분석용 하나의 텍스트 블록으로 변환.
    """
    if not posts:
        return ""

    start_date = posts[-1]["date"]
    end_date = posts[0]["date"]

    blocks = [
        f"=== 메르 블로그 수집 글 ({start_date} ~ {end_date}, 총 {len(posts)}편) ===\n"
    ]

    for i, post in enumerate(posts, 1):
        blocks.append(
            f"\n[{i}/{len(posts)}] 제목: {post['title']}\n"
            f"날짜: {post['date']}\n"
            f"URL: {post['url']}\n"
            f"내용:\n{post['content']}\n"
            f"{'─' * 60}"
        )

    return "\n".join(blocks)


# ─── 직접 실행 테스트 ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    posts = fetch_recent_posts(days=7)
    print(f"\n--- 샘플 출력 (첫 번째 글) ---")
    if posts:
        print(f"제목: {posts[0]['title']}")
        print(f"날짜: {posts[0]['date']}")
