"""
telegram_notify.py
텔레그램 봇을 통해 요약 리포트와 차트 이미지를 전송하는 모듈

필요한 환경변수:
  TELEGRAM_BOT_TOKEN: BotFather에서 발급받은 봇 토큰
  TELEGRAM_CHAT_ID:   메시지를 받을 채팅 ID (숫자)
  DASHBOARD_URL:      GitHub Pages 대시보드 URL (선택)

텔레그램 봇 만들기:
  1. 텔레그램에서 @BotFather 검색
  2. /newbot 입력 → 봇 이름, 봇 아이디(@xxx_bot) 순서로 입력
  3. 발급된 토큰 복사 (형식: 123456789:ABCdef...)
  4. 만든 봇 채팅창 열고 /start 입력
  5. https://api.telegram.org/bot{TOKEN}/getUpdates 접속
     → result[0].message.chat.id 값이 TELEGRAM_CHAT_ID
"""

import os
import re
import time

import requests

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_MSG_LEN = 4000

DEFAULT_DASHBOARD_URL = (
    "https://pikachu444.github.io/mer-porfolio/output/dashboard.html"
)


# ─── 환경변수 ─────────────────────────────────────────────────────────────────

def _get_credentials():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    return token, chat_id


def _get_dashboard_url() -> str:
    return os.environ.get("DASHBOARD_URL", DEFAULT_DASHBOARD_URL)


# ─── 요약 추출 ────────────────────────────────────────────────────────────────

def extract_summary(report: str) -> str:
    """
    리포트 전문에서 핵심 내용만 추출해 텔레그램용 요약 메시지 생성.

    포함 내용:
      - 핵심 인사이트 제목 + 한 줄 설명 (최대 3개)
      - 국내주식 추천 종목
      - 해외주식 추천 종목
      - 섹터 온도계
      - 한 줄 코멘트
      - 대시보드 URL
    """
    parts = []

    # ── 1. 핵심 인사이트 ──────────────────────────────────────────────────────
    insight_blocks = re.findall(
        r"### 인사이트 \d+[:：]\s*(.+?)\n([\s\S]+?)(?=### 인사이트|\Z|## )",
        report,
    )

    if insight_blocks:
        parts.append("📌 *핵심 인사이트*")
        for title, body in insight_blocks[:3]:
            title = title.strip()
            # 투자판단 줄 우선 추출
            판단_match = re.search(r"\*\*투자판단[：:]\*\*\s*(.+)", body)
            if 판단_match:
                desc = 판단_match.group(1).strip()
            else:
                # 첫 번째 번호 항목 사용
                first_line = re.search(r"^\d+\.\s*(.+)", body.strip(), re.MULTILINE)
                desc = first_line.group(1).strip() if first_line else ""
            # 너무 길면 자름
            if len(desc) > 60:
                desc = desc[:57] + "..."
            parts.append(f"• *{title}*\n  └ {desc}")

    # ── 2. 국내주식 추천 ──────────────────────────────────────────────────────
    kr_match = re.search(
        r"(?:🇰🇷|국내주식)[^\n]*\n\|[^\n]+\|\n\|[-| :]+\|\n((?:\|[^\n]+\|\n?)+)",
        report,
    )
    if kr_match:
        parts.append("\n🇰🇷 *국내주식 추천*")
        for row in kr_match.group(1).strip().split("\n"):
            cells = [c.strip() for c in row.split("|")[1:-1]]
            if len(cells) >= 4 and cells[0] and not cells[0].startswith("-"):
                name, _, action, weight = cells[0], cells[1], cells[2], cells[3]
                parts.append(f"• {name} — {action} ({weight})")

    # ── 3. 해외주식 추천 ──────────────────────────────────────────────────────
    us_match = re.search(
        r"(?:🇺🇸|해외주식)[^\n]*\n\|[^\n]+\|\n\|[-| :]+\|\n((?:\|[^\n]+\|\n?)+)",
        report,
    )
    if us_match:
        parts.append("\n🇺🇸 *해외주식 추천*")
        for row in us_match.group(1).strip().split("\n"):
            cells = [c.strip() for c in row.split("|")[1:-1]]
            if len(cells) >= 4 and cells[0] and not cells[0].startswith("-"):
                name, ticker, action, weight = cells[0], cells[1], cells[2], cells[3]
                parts.append(f"• {name} ({ticker}) — {action} ({weight})")

    # ── 4. 섹터 온도계 ────────────────────────────────────────────────────────
    sector_match = re.search(
        r"섹터별 온도계[^\n]*\n\|[^\n]+\|\n\|[-| :]+\|\n((?:\|[^\n]+\|\n?)+)",
        report,
    )
    if sector_match:
        parts.append("\n🌡 *섹터 온도계*")
        for row in sector_match.group(1).strip().split("\n"):
            cells = [c.strip() for c in row.split("|")[1:-1]]
            if len(cells) >= 3 and cells[0] and not cells[0].startswith("-"):
                sector, temp, change = cells[0], cells[1], cells[2]
                parts.append(f"• {sector} {temp} {change}")

    # ── 5. 한 줄 코멘트 ───────────────────────────────────────────────────────
    comment_match = re.search(
        r"(?:한 줄 코멘트|💬)[^\n]*\n+>\s*(.+)",
        report,
    )
    if comment_match:
        comment = comment_match.group(1).strip().strip('"').strip("'")
        parts.append(f'\n💬 _{comment}_')

    # ── 6. 대시보드 URL ───────────────────────────────────────────────────────
    url = _get_dashboard_url()
    parts.append(f"\n🌐 [대시보드 전체 보기]({url})")

    return "\n".join(parts) if parts else "요약 추출 실패 — 대시보드에서 전체 내용을 확인하세요."


# ─── 메시지 전송 ──────────────────────────────────────────────────────────────

def _send_message(token: str, chat_id: str, text: str,
                  parse_mode: str = "Markdown") -> bool:
    url = TELEGRAM_API.format(token=token, method="sendMessage")
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": False,  # URL 미리보기 허용
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            return True
        if resp.status_code == 400 and parse_mode:
            payload["parse_mode"] = ""
            resp2 = requests.post(url, json=payload, timeout=15)
            return resp2.status_code == 200
        print(f"  ⚠ 텔레그램 메시지 오류 {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        print(f"  ⚠ 텔레그램 예외: {e}")
        return False


# ─── 이미지 전송 ──────────────────────────────────────────────────────────────

def send_photo(image_path: str, caption: str = "") -> bool:
    """PNG/JPG 이미지 파일을 텔레그램으로 전송."""
    token, chat_id = _get_credentials()
    if not token or not chat_id:
        print("  ⚠ TELEGRAM 환경변수 미설정 → 이미지 전송 스킵")
        return False

    url = TELEGRAM_API.format(token=token, method="sendPhoto")
    try:
        with open(image_path, "rb") as f:
            resp = requests.post(
                url,
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": f},
                timeout=30,
            )
        if resp.status_code == 200:
            print("  📷 차트 이미지 전송 완료")
            return True
        print(f"  ⚠ 이미지 전송 오류 {resp.status_code}: {resp.text[:200]}")
        return False
    except FileNotFoundError:
        print(f"  ⚠ 이미지 파일 없음: {image_path}")
        return False
    except Exception as e:
        print(f"  ⚠ 이미지 전송 예외: {e}")
        return False


# ─── 리포트 전송 (요약 + URL) ─────────────────────────────────────────────────

def send_report(report: str, today_str: str) -> bool:
    """
    리포트 요약 + 대시보드 URL을 텔레그램으로 전송.
    (전문 대신 핵심 요약만 전송 — 전문은 대시보드에서 확인)

    Returns:
        True (성공) / False (환경변수 미설정 또는 실패)
    """
    token, chat_id = _get_credentials()
    if not token or not chat_id:
        print("  ⚠ TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정 → 텔레그램 알림 스킵")
        return False

    # 헤더
    header = (
        f"📊 *메르AI 포트폴리오 리포트*\n"
        f"📅 {today_str}\n"
        f"{'─' * 22}"
    )
    _send_message(token, chat_id, header)
    time.sleep(0.5)

    # 요약 메시지 (핵심 인사이트 + 포트폴리오 + 섹터 + 코멘트 + URL)
    summary = extract_summary(report)
    ok = _send_message(token, chat_id, summary)

    print(f"  📱 텔레그램 요약 전송: {'성공' if ok else '실패'}")
    return ok


# ─── 직접 실행 테스트 ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    sample_report = """
# 메르AI 포트폴리오 리포트

## 📌 시장 분석 핵심 인사이트

### 인사이트 1: 호르무즈 봉쇄 장기화와 조선업 나비효과

1. 이란이 호르무즈 해협 봉쇄를 선언했음.
2. 카타르 LNG 수출 차질이 현실화되고 있음.

**해석(나비효과):**
4. 대체 물류 루트 확보 수요 급증
5. 한국 조선업 수주 폭발적 증가 예상

**투자판단:** Buy 강 — 조선업 슈퍼사이클 본격화

### 인사이트 2: 미 연준 금리 동결과 달러 약세

1. 연준이 금리를 동결했음.

**투자판단:** Watch — 원자재 가격 상승 모니터링 필요

## 📊 포트폴리오 추천

### 🇰🇷 국내주식 (한국)

| 종목명 | 코드 | 판단 | 목표비중 | 핵심 근거 |
|--------|------|------|----------|-----------|
| 한국조선해양 | 009540 | 매수 | 15% | 조선 슈퍼사이클 |
| 삼성전자 | 005930 | 보유 | 20% | AI 반도체 수혜 |

### 🇺🇸 해외주식 (미국)

| 종목명 | 티커 | 판단 | 목표비중 | 핵심 근거 |
|--------|------|------|----------|-----------|
| Nvidia | NVDA | Buy | 25% | AI 인프라 핵심 |
| ExxonMobil | XOM | Hold | 10% | 에너지 헤지 |

## 🔍 섹터별 온도계

| 섹터 | 온도 | 변화 | 근거 요약 |
|------|------|------|-----------|
| 조선/해운 | 🔥🔥🔥 | ▲ | 수주 급증 |
| 반도체 | 🔥🔥 | → | AI 수요 지속 |
| 2차전지 | 🧊 | ▼ | 수요 둔화 |

## 💬 한 줄 코멘트

> 지정학적 리스크가 오히려 한국 조선업의 봄을 앞당기고 있다.
"""
    summary = extract_summary(sample_report)
    print("=== 요약 미리보기 ===")
    print(summary)
    print("\n=== 전송 테스트 ===")
    result = send_report(sample_report, "2026년 05월 15일")
    print("✅ 전송 성공" if result else "❌ 전송 실패 (환경변수 확인 필요)")
