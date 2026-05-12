"""
fetch_mer.py
메르 블로그(blog.naver.com/ranto28) RSS 파싱 + 전문 스크래핑 모듈

네이버 블로그는 iframe 구조라 데스크탑 URL은 JS 없이 파싱 불가.
모바일 URL(m.blog.naver.com)을 사용하면 일반 HTML로 전문 접근 가능.
"""

import feedparser
import requests
from bs4 import BeautifulSoup
import time
import re
import json
import os
from datetime import datetime, timedelta
from typing import List, Dict, Optional

# ─── 설정 ───────────────────────────────────────────────────────────────────

BLOG_ID = "ranto28"
RSS_URL = f"https://rss.blog.naver.com/{BLOG_ID}.xml"
MOBILE_BASE = "https://m.blog.naver.com"
STATE_FILE = "last_processed.json"
_fetch_days_env = os.environ.get("FETCH_DAYS", "").strip()
DEFAULT_DAYS = int(_fetch_days_env) if _fetch_days_env else 14  # 빈 문자열 방어

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
        "Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://m.blog.naver.com/",
}

# 포스트 1개당 최대 글자 수 (토큰 오버플로우 방지)
MAX_CHARS_PER_POST = 4000


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

def fetch_recent_posts(days: int = DEFAULT_DAYS) -> List[Dict]:
    """
    최근 N일간의 메르 블로그 포스트를 수집해 반환.

    반환 형식:
    [
        {
            "title": str,
            "date": "YYYY-MM-DD",
            "url": str,
            "content": str  # 전문 (없으면 RSS 요약)
        },
        ...
    ]
    날짜 내림차순 정렬 (최신 글 먼저).
    """
    print(f"📡 RSS 파싱 중: {RSS_URL}")
    feed = feedparser.parse(RSS_URL)

    if feed.bozo and not feed.entries:
        raise RuntimeError(f"RSS 파싱 실패: {feed.bozo_exception}")

    cutoff = datetime.now() - timedelta(days=days)
    print(f"📅 수집 기간: {cutoff.strftime('%Y-%m-%d')} ~ 오늘")

    posts = []
    skipped = 0

    for entry in feed.entries:
        try:
            pub_date = datetime(*entry.published_parsed[:6])
        except (AttributeError, TypeError):
            skipped += 1
            continue

        if pub_date < cutoff:
            continue

        post_id = extract_post_id(entry.link)
        if not post_id:
            print(f"  ⚠ 포스트 ID 추출 실패: {entry.link}")
            skipped += 1
            continue

        title = entry.get("title", "제목 없음").strip()
        print(f"  📄 [{pub_date.strftime('%m/%d')}] {title[:55]}...")

        # 전문 스크래핑
        full_text = fetch_full_post(post_id, title)

        # 전문 실패 시 RSS 요약 사용
        if not full_text:
            full_text = entry.get("summary", "")
            print(f"      → RSS 요약으로 대체 ({len(full_text)}자)")
        else:
            print(f"      → 전문 수집 완료 ({len(full_text)}자)")

        # 토큰 오버플로우 방지: 포스트당 최대 글자 수 제한
        if len(full_text) > MAX_CHARS_PER_POST:
            full_text = full_text[:MAX_CHARS_PER_POST] + "\n...(이하 생략)"

        posts.append({
            "title": title,
            "date": pub_date.strftime("%Y-%m-%d"),
            "url": entry.link,
            "content": full_text,
        })

        # 네이버 서버 부하 방지
        time.sleep(1.2)

    posts.sort(key=lambda x: x["date"], reverse=True)

    print(f"\n✅ 수집 완료: {len(posts)}개 (스킵: {skipped}개)")
    return posts


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
        