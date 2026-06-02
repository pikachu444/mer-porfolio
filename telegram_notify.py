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
    "https://pikachu444.github.io/mer-portfolio/output/dashboard.html"
)


# ─── 환경변수 ─────────────────────────────────────────────────────────────────

def _get_credentials():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    return token, chat_id


def _get_dashboard_url() -> str:
    url = os.environ.get("DASHBOARD_URL", "")
    if not url or "mer-porfolio" in url:
        return DEFAULT_DASHBOARD_URL
    return url


# ─── 요약 추출 ────────────────────────────────────────────────────────────────

def extract_summary(report: str) -> str:
    """
    리포트 전문에서 핵심 내용만 추출해 텔레그램용 요약 메시지 생성.

    포함 내용:
      - 핵심 인사이트 제목 + 한 줄 설명
      - 국내주식 추천 종목
      - 해외주식 추천 종목
      - 한 줄 코멘트
      - 대시보드 URL
    """
    parts = []

    # ── 1. 핵심 인사이트 ──────────────────────────────────────────────────────
    insight_blocks = re.findall(
        r"###\s*(?:핵심\s*)?인사이트\s*\d+[^:\n]*[:：.]?\s*(.+?)\n([\s\S]+?)(?=###\s*(?:핵심\s*)?인사이트|\Z|## )",
        report,
    )

    if insight_blocks:
        parts.append("📌 *핵심 인사이트*")
        for title, body in insight_blocks:
            title = title.strip()
            # 투자판단 줄 우선 추출
            judgment_match = re.search(r"\*\*투자판단[:：]\*\*\s*(.+)", body)
            if judgment_match:
                desc = judgment_match.group(1).strip()
            else:
                # 첫 번째 번호 항목 사용
                first_line = re.search(r"^\d+\.\s*(.+)", body.strip(), re.MULTILINE)
                desc = first_line.group(1).strip() if first_line else ""
            if len(desc) > 60:
                desc = desc[:57] + "..."
            parts.append(f"• *{title}*\n  └ {desc}")

    # ── 2. 국내주식 추천 ──────────────────────────────────────────────────────
    # 헤더와 테이블 사이에 빈 줄이 있을 수 있으므로 \n\n? 로 처리
    kr_match = re.search(
        r"(?:\U0001f1f0\U0001f1f7|국내주식)[^\n]*\n\n?\|[^\n]+\|\n\|[-| :]+\|\n((?:\|[^\n]+\|\n?)+)",
        report,
    )
    if kr_match:
        parts.append("\n\U0001f1f0\U0001f1f7 *국내주식 추천*")
        for row in kr_match.group(1).strip().split("\n"):
            cells = [c.strip() for c in row.split("|")[1:-1]]
            if len(cells) >= 4 and cells[0] and not cells[0].startswith("-"):
                name, _, action, weight = cells[0], cells[1], cells[2], cells[3]
                parts.append(f"• {name} — {action} ({weight})")

    # ── 3. 해외주식 추천 ──────────────────────────────────────────────────────
    us_match = re.search(
        r"(?:\U0001f1fa\U0001f1f8|해외주식)[^\n]*\n\n?\|[^\n]+\|\n\|[-| :]+\|\n((?:\|[^\n]+\|\n?)+)",
        report,
    )
    if us_match:
        parts.append("\n\U0001f1fa\U0001f1f8 *해외주식 추천*")
        for row in us_match.group(1).strip().split("\n"):
            cells = [c.strip() for c in row.split("|")[1:-1]]
            if len(cells) >= 4 and cells[0] and not cells[0].startswith("-"):
                name, ticker, action, weight = cells[0], cells[1], cells[2], cells[3]
                parts.append(f"• {name} ({ticker}) — {action} ({weight})")

    # ── 4. 한 줄 코멘트 ───────────────────────────────────────────────────────
    comment_match = re.search(
        r"(?:한 줄 코멘트|\U0001f4ac)[^\n]*\n+>\s*(.+)",
        report,
    )
    if comment_match:
        comment = comment_match.group(1).strip().strip('"').strip("'")
        parts.append(f'\n\U0001f4ac _{comment}_')

    # ── 5. 대시보드 URL ───────────────────────────────────────────────────────
    url = _get_dashboard_url()
    parts.append(f"\n\U0001f310 [대시보드 전체 보기]({url})")
    parts.append("\n※ 깃허브 배포 지연으로 인해 대시보드 반영에 1~2분이 소요될 수 있습니다.")

    if not parts:
        return "요약 추출 실패 — 대시보드에서 전체 내용을 확인하세요."

    return "\n".join(parts)


# ─── 메시지 전송 ──────────────────────────────────────────────────────────────

def _send_message(token: str, chat_id: str, text: str,
                  parse_mode: str = "Markdown") -> bool:
    url = TELEGRAM_API.format(token=token, method="sendMessage")
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            return True
        if resp.status_code == 400 and parse_mode:
            payload.pop("parse_mode", None)
            resp2 = requests.post(url, json=payload, timeout=15)
            if resp2.status_code == 200:
                return True
            print(f"  !! Telegram message fallback error {resp2.status_code}: {resp2.text[:200]}")
            return False
        print(f"  !! 텔레그램 메시지 오류 {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        print(f"  !! 텔레그램 예외: {e}")
        return False


# ─── 이미지 전송 ──────────────────────────────────────────────────────────────

def send_status(title: str, body: str = "") -> bool:
    """Send a short operational status message to Telegram."""
    token, chat_id = _get_credentials()
    if not token or not chat_id:
        print("  !! TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing -> status notification skipped")
        return False

    lines = [f"*{title}*"]
    if body:
        lines.append(body)
    lines.append(f"[Dashboard]({_get_dashboard_url()})")
    return _send_message(token, chat_id, "\n\n".join(lines))


def send_photo(image_path: str, caption: str = "") -> bool:
    """PNG/JPG 이미지 파일을 텔레그램으로 전송."""
    token, chat_id = _get_credentials()
    if not token or not chat_id:
        print("  !! TELEGRAM 환경변수 미설정 -> 이미지 전송 스킵")
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
            print("  차트 이미지 전송 완료")
            return True
        print(f"  !! 이미지 전송 오류 {resp.status_code}: {resp.text[:200]}")
        return False
    except FileNotFoundError:
        print(f"  !! 이미지 파일 없음: {image_path}")
        return False
    except Exception as e:
        print(f"  !! 이미지 전송 예외: {e}")
        return False


# ─── 리포트 전송 (요약 + URL) ─────────────────────────────────────────────────

def _is_valid_report(report: str) -> bool:
    """실제 분석 리포트인지 확인 (테스트/빈 내용 필터링)."""
    if not report or len(report) < 500:
        return False
    # 핵심 섹션이 하나라도 있어야 함
    return any(keyword in report for keyword in ("인사이트", "포트폴리오", "국내주식", "해외주식"))


def send_report(report: str, today_str: str) -> bool:
    """
    리포트 요약 + 대시보드 URL을 텔레그램으로 전송.
    헤더와 요약을 하나의 메시지로 결합해 전송합니다.

    Returns:
        True (성공) / False (환경변수 미설정, 빈 리포트, 또는 실패)
    """
    token, chat_id = _get_credentials()
    if not token or not chat_id:
        print("  !! TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정 -> 텔레그램 알림 스킵")
        return False

    # 빈/테스트 리포트 가드
    if not _is_valid_report(report):
        print("  !! 리포트가 비어있거나 테스트 내용 -> 텔레그램 전송 스킵")
        return False

    # 헤더 + 요약을 하나의 메시지로 결합 (메시지 분리 혼란 방지)
    header = (
        f"\U0001f4ca *메르AI 포트폴리오 리포트*\n"
        f"\U0001f4c5 {today_str}\n"
        f"{'─' * 22}\n"
    )
    summary = extract_summary(report)
    combined = header + summary

    ok = all(_send_message(token, chat_id, message) for message in split_telegram_message(combined))

    print(f"  텔레그램 요약 전송: {'성공' if ok else '실패'}")
    return ok


def build_structured_summary(
    state: dict,
    today_str: str,
    performance: dict | None = None,
    *,
    no_changes: bool = False,
) -> str:
    """Build a user-facing summary from validated structured state."""
    portfolio = state.get("portfolio", [])
    watchlist = state.get("watchlist", [])
    closed = state.get("closed_positions", [])
    insights = state.get("insights", [])
    performance = performance or {}
    lines = [
        "📊 *메르AI 모델 포트폴리오*",
        f"📅 {today_str}",
        "※ 메르 블로거의 실제 보유 내역이 아닙니다.",
        "※ 블로그 직접 판단과 AI 해석을 구분해 표시합니다.",
    ]
    if no_changes:
        value = performance.get("portfolio_return_krw")
        rendered = f"{float(value):+.1f}%" if value is not None else "집계 전"
        lines += ["", "*오늘의 성과 요약*", f"• 모델 포트폴리오 수익률: {rendered}", "• 포트폴리오 변경 없음"]
    else:
        lines += ["", "📌 *핵심 인사이트*"]
        if insights:
            for item in insights:
                lines.append(f"• *{item.get('title', '')}*")
                lines.append(f"  └ {item.get('summary', '')}")
                lines.append(f"  └ 시사점: {item.get('investment_implication', '')}")
        else:
            lines.append("• 표시할 인사이트 없음")
        lines += ["", "*현재 모델 포트폴리오*"]
        if portfolio:
            for item in portfolio:
                actor = (
                    "메르 직접 발언"
                    if item.get("decision_actor") == "메르"
                    else "AI 제안"
                    if item.get("decision_actor") == "AI"
                    else "미분류"
                )
                lines.append(
                    f"• {item.get('name', '')} ({item.get('code', '')})"
                    f" | {actor} · {item.get('action', '')}"
                    f" | {item.get('proposed_weight', 0):g}%"
                )
                lines.append(f"  └ {item.get('change_reason', '')}")
        else:
            lines.append("• 편입 종목 없음")
        lines += ["", "*Watchlist*"]
        if watchlist:
            for item in watchlist:
                lines.append(f"• {item.get('name', '')} | {item.get('observation_reason', '')}")
        else:
            lines.append("• 표시할 항목 없음")
        lines += ["", f"• 종료 포지션: {len(closed)}건"]
    lines += ["", f"🌐 [대시보드 전체 보기]({_get_dashboard_url()})"]
    return "\n".join(lines)


def split_telegram_message(text: str, max_length: int = MAX_MSG_LEN) -> list[str]:
    """Split a structured summary without dropping insights or truncating the tail."""
    if len(text) <= max_length:
        return [text]

    messages: list[str] = []
    current = ""
    for line in text.splitlines():
        candidate = line if not current else current + "\n" + line
        if len(candidate) <= max_length:
            current = candidate
            continue
        if current:
            messages.append(current)
        while len(line) > max_length:
            messages.append(line[:max_length])
            line = line[max_length:]
        current = line
    if current:
        messages.append(current)
    return messages


def send_structured_summary(
    state: dict,
    today_str: str,
    performance: dict | None = None,
    *,
    no_changes: bool = False,
) -> bool:
    token, chat_id = _get_credentials()
    if not token or not chat_id:
        print("  !! TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정 -> 텔레그램 알림 스킵")
        return False
    messages = split_telegram_message(
        build_structured_summary(
            state,
            today_str,
            performance,
            no_changes=no_changes,
        )
    )
    ok = all(_send_message(token, chat_id, message) for message in messages)
    print(f"  텔레그램 구조화 요약 전송: {'성공' if ok else '실패'}")
    return ok


# ─── 직접 실행 테스트 ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as f:
            sample_report = f.read()
    else:
        sample_report = """
# 메르AI 포트폴리오 리포트

## 📌 시장 분석 핵심 인사이트

### 인사이트 1: 호르무즈 봉쇄 장기화와 조선업 나비효과

1. 이란이 호르무즈 해협 봉쇄를 선언했음.

**투자판단:** Buy 강 — 조선업 슈퍼사이클 본격화

## 📊 포트폴리오 추천

### 🇰🇷 국내주식 (한국)

| 종목명 | 코드 | 판단 | 목표비중 | 핵심 근거 |
|--------|------|------|----------|-----------|
| 한국조선해양 | 009540 | 매수 | 15% | 조선 슈퍼사이클 |

### 🇺🇸 해외주식 (미국)

| 종목명 | 티커 | 판단 | 목표비중 | 핵심 근거 |
|--------|------|------|----------|-----------|
| Nvidia | NVDA | Buy | 25% | AI 인프라 핵심 |

## 💬 한 줄 코멘트

> 지정학적 리스크가 오히려 한국 조선업의 봄을 앞당기고 있다.
"""

    from dotenv import load_dotenv
    load_dotenv()

    summary = extract_summary(sample_report)
    print("=== 요약 미리보기 ===")
    print(summary)
    print("\n=== 전송 테스트 ===")
    result = send_report(sample_report, "test")
    print(result)
