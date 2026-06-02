"""
fetch_mer.py
메르 블로그(blog.naver.com/ranto28) RSS 파싱 + 전문 스크래핑 모듈

네이버 블로그는 iframe 구조라 데스크탑 URL은 JS 없이 파싱 불가.
모바일 URL(m.blog.naver.com)을 사용하면 일반 HTML로 전문 접근 가능.
"""

import feedparser
import requests
from bs4 import BeautifulSoup
import re
import json
import os
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from pathlib import Path
from google import genai
from google.genai import types
from gemini_utils import generate_content_with_retry
from post_summary import MAP_SUMMARY_PROMPT, SUMMARY_VERSION, parse_summary_response

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
MODEL_INPUT_TOKEN_LIMIT = 1_048_576
MODEL_INPUT_SAFE_RATIO = 0.8

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
    if _count_tokens(client, "gemini-2.5-flash", request) <= safe_limit:
        return request

    low, high = 0, len(content)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = prefix + content[:middle] + "\n...(전송용 본문 끝부분 생략)" + suffix
        if _count_tokens(client, "gemini-2.5-flash", candidate) <= safe_limit:
            low = middle
        else:
            high = middle - 1
    return prefix + content[:low] + "\n...(전송용 본문 끝부분 생략)" + suffix


def _parse_summary_response(text: str) -> dict:
    return parse_summary_response(text)


def summarize_single_post(content: str) -> dict:
    """Use gemini-2.5-flash to summarize and classify one full article."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY 미설정: 글별 Flash 요약을 생성할 수 없습니다.")
    client = genai.Client(api_key=api_key)
    response = generate_content_with_retry(
        client=client,
        model="gemini-2.5-flash",
        contents=_fit_summary_request(client, content),
        config=types.GenerateContentConfig(
            temperature=0.2,
            max_output_tokens=2048,
            response_mime_type="application/json",
        ),
        max_retries=3,
    )
    if not response.text:
        raise RuntimeError("글별 Flash 요약 응답이 비어 있습니다.")
    return _parse_summary_response(response.text)


def _summary_fields(content: str, title: str) -> dict:
    if not ENABLE_POST_SUMMARIES:
        print("      글별 요약 OFF -> 기존 캐시 또는 원문을 사용")
        return {
            "summary": "",
            "investment_relevant": None,
            "relevance_reason": "",
            "summary_version": None,
        }
    print(f"      1차 요약 캐시 생성(Flash): {title[:30]}...")
    return summarize_single_post(content)


def _write_posts_db(posts: list[dict]) -> None:
    path = Path(DB_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(posts, f, ensure_ascii=False, indent=2)


def _refresh_recent_summary_cache(posts: list[dict], now: datetime) -> bool:
    """Upgrade only the recent rebalance window once after the summary policy changes."""
    if not ENABLE_POST_SUMMARIES:
        return False

    cutoff = now - timedelta(days=14)
    changed = False
    for post in posts:
        try:
            post_date = datetime.fromisoformat(post["date"])
        except Exception:
            continue
        if post_date < cutoff or post.get("summary_version") == SUMMARY_VERSION:
            continue
        post_id = extract_post_id(post.get("url", ""))
        if post_id:
            refreshed = fetch_full_post(post_id, post.get("title", ""))
            if refreshed:
                post["content"] = refreshed
        post.update(_summary_fields(post.get("content", ""), post.get("title", "")))
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

        newly_added.append({
            "title": title,
            "date": pub_date.strftime("%Y-%m-%d"),
            "url": entry.link,
            "content": full_text,
            **_summary_fields(full_text, title),
        })
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
    return posts if isinstance(posts, list) else []


def is_investment_relevant(post: dict) -> bool:
    """Treat legacy caches as relevant until the recent-window upgrade replaces them."""
    return post.get("investment_relevant") is not False


def select_new_relevant_posts(posts: list[dict], new_urls: set[str]) -> list[dict]:
    return [
        post for post in posts
        if post.get("url") in new_urls and is_investment_relevant(post)
    ]


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
        if datetime.fromisoformat(post["date"]) > cutoff and is_investment_relevant(post)
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
