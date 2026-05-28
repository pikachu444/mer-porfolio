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
from pathlib import Path
from google import genai
from google.genai import types

# ─── 설정 ───────────────────────────────────────────────────────────────────

BLOG_ID = "ranto28"
RSS_URL = f"https://rss.blog.naver.com/{BLOG_ID}.xml"
MOBILE_BASE = "https://m.blog.naver.com"
STATE_FILE = "last_processed.json"
DB_FILE = "output/posts_db.json"  # 증분 누적 데이터베이스 경로
_fetch_days_env = os.environ.get("FETCH_DAYS", "").strip()
DEFAULT_DAYS = int(_fetch_days_env) if _fetch_days_env else 14  # 빈 문자열 방어

# 1차 정밀 요약 프롬프트 (나비효과 및 종목 팩트 100% 보존용)
MAP_SUMMARY_PROMPT = """
당신은 거시경제 분석가 메르의 글을 정밀 압축 요약하는 1차 요약 엔진입니다.
제공된 블로그 전문을 읽고, 다음 세 가지 정보를 100% 보존하여 콤팩트하게 요약하십시오:
1. **핵심 거시경제/지정학적 사건 팩트**: 날짜, 구체적 수치, 선언 내용 등.
2. **나비효과 인과관계**: 사건이 유발하는 1차/2차/3차 파급효과와 업종 연결 고리 (예: A로 인해 B가 발생하고 이로 인해 C가 수혜/리스크를 입는다).
3. **직간접 언급 주식/섹터 명단**: 구체적으로 거론된 종목 이름, 티커, 해당 업종.

절대 없는 사실을 창작하거나 지어내지 말고, 나비효과의 정교한 인과 관계 연결 고리를 단순화하여 누락시키지 마십시오.
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

def summarize_single_post(content: str) -> str:
    """가벼운 gemini-2.5-flash 모델을 사용하여 글 1편을 콤팩트 요약 (429 백오프 완비)"""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("    [Info] GEMINI_API_KEY 미설정으로 1차 요약 요소를 생략하고 원본을 유지합니다.")
        return ""
    try:
        client = genai.Client(api_key=api_key)
        
        # 429 및 Rate Limit 지능형 대기 구현
        import re
        backoff = 30.0
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = client.models.generate_content(
                    model="gemini-3-flash",
                    contents=f"블로그 글:\n{content}\n\n{MAP_SUMMARY_PROMPT}",
                    config=types.GenerateContentConfig(
                        temperature=0.2,
                        max_output_tokens=2048,
                    )
                )
                return response.text if response.text else ""
            except Exception as e:
                err_msg = str(e)
                is_rate_limit = any(x in err_msg.lower() for x in ["429", "resource", "exhausted", "quota", "rate", "limit"])
                if is_rate_limit and attempt < max_retries - 1:
                    print(f"      [429/RateLimit 감지] 1차 요약 중 한도 도달 ({attempt + 1}/{max_retries})")
                    
                    wait_sec = backoff
                    match = re.search(r"retry in ([\d\.]+)s", err_msg, re.IGNORECASE)
                    if not match:
                        match = re.search(r"retryDelay': '(\d+)s'", err_msg, re.IGNORECASE)
                    if match:
                        try:
                            wait_sec = float(match.group(1)) + 1.0
                        except ValueError:
                            pass
                    
                    print(f"      ⏳ {wait_sec:.1f}초 동안 대기 후 다시 시도합니다...")
                    time.sleep(wait_sec)
                    backoff = min(backoff * 1.5, 60.0)
                else:
                    raise e
        return ""
    except Exception as e:
        print(f"    ⚠ 1차 요약 생성 중 최종 API 에러 발생 (건너뜀): {e}")
        return ""


def fetch_recent_posts(days: int = DEFAULT_DAYS) -> List[Dict]:
    """
    [스마트 증분 수집 및 요약 캐싱 DB 버전]
    신규 작성된 새 글만 부분 수집하여 posts_db.json 데이터베이스에 누적하고, 
    최종적으로 최신 30개만 슬라이싱하여 안정적으로 반환합니다.
    """
    # 1. 기존 누적 데이터베이스 로드
    db_posts = []
    db_path = Path(DB_FILE)
    if db_path.exists():
        try:
            with open(db_path, encoding="utf-8") as f:
                db_posts = json.load(f)
            print(f"📂 기존 posts_db.json 로드 성공 (총 {len(db_posts)}편 누적 상태)")
        except Exception as e:
            print(f"⚠ 기존 posts_db.json 로드 실패 (새로 빌드): {e}")

    existing_urls = {p["url"] for p in db_posts}

    # 2. RSS 파싱 시작
    print(f"📡 RSS 파싱 중: {RSS_URL}")
    feed = feedparser.parse(RSS_URL)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"RSS 파싱 실패: {feed.bozo_exception}")

    cutoff = datetime.now() - timedelta(days=days)
    print(f"📅 수집 기간: {cutoff.strftime('%Y-%m-%d')} ~ 오늘")

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
        print(f"  🆕 [신규 증분 발견] [{pub_date.strftime('%m/%d')}] {title[:45]}...")

        # 신규 전문 스크래핑
        full_text = fetch_full_post(post_id, title)
        if not full_text:
            full_text = entry.get("summary", "")

        if len(full_text) > MAX_CHARS_PER_POST:
            full_text = full_text[:MAX_CHARS_PER_POST] + "\n...(이하 생략)"

        # 새로 긁어온 딱 요 글에 대해서만 Flash로 1차 정밀 요약 실행
        print(f"      → [1차 요약 캐싱 가동] {title[:30]}...")
        summary = summarize_single_post(full_text)

        newly_added.append({
            "title": title,
            "date": pub_date.strftime("%Y-%m-%d"),
            "url": entry.link,
            "content": full_text,
            "summary": summary,
        })
        new_posts_count += 1
        time.sleep(4.5)  # Gemini RPM 15 무료 한도 선제 방어 (4.5초 간격 유지)

    # 3. 새로운 포스트 병합 및 저장
    if newly_added:
        db_posts.extend(newly_added)
        # 날짜 최신순 정렬
        db_posts.sort(key=lambda x: x["date"], reverse=True)
        
        # output 폴더 확보 후 DB 저장
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with open(db_path, "w", encoding="utf-8") as f:
            json.dump(db_posts, f, ensure_ascii=False, indent=2)
        print(f"💾 신규 {new_posts_count}편 1차 요약 병합 완료 및 posts_db.json 누적 갱신 성공!")
    else:
        print("✨ 신규 업로드된 증분 글이 존재하지 않습니다. 스크래핑 0회 통과 완료.")

    # 4. 분석에 최신 30개만 슬라이싱하여 반환
    final_posts = db_posts[:30]
    print(f"🎯 최종 분석을 위한 최신 {len(final_posts)}편 데이터 보존 및 리턴 완료.")
    return final_posts


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
        