"""
telegram_notify.py
텔레그램 봇을 통해 리포트를 전송하는 모듈

필요한 환경변수:
  TELEGRAM_BOT_TOKEN: BotFather에서 발급받은 봇 토큰
  TELEGRAM_CHAT_ID:   메시지를 받을 채팅 ID (숫자)

텔레그램 봇 만들기:
  1. 텔레그램에서 @BotFather 검색
  2. /newbot 입력 → 봇 이름, 봇 아이디(@xxx_bot) 순서로 입력
  3. 발급된 토큰 복사 (형식: 123456789:ABCdef...)
  4. 만든 봇 채팅창 열고 /start 입력
  5. https://api.telegram.org/bot{TOKEN}/getUpdates 접속
     → result[0].message.chat.id 값이 TELEGRAM_CHAT_ID
"""

import os
import time
from typing import Optional

import requests

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_MSG_LEN = 4000  # 텔레그램 단일 메시지 최대 4096자, 여유 확보


# ─── 단일 메시지 전송 ─────────────────────────────────────────────────────────

def _send_message(token: str, chat_id: str, text: str,
                  parse_mode: str = "Markdown") -> bool:
    """단일 텍스트 메시지 전송. 성공 시 True 반환."""
    url = TELEGRAM_API.format(token=token, method="sendMessage")
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            return True
        # 마크다운 파싱 오류(400)면 일반 텍스트로 재시도
        if resp.status_code == 400 and parse_mode:
            payload["parse_mode"] = ""
            resp2 = requests.post(url, json=payload, timeout=15)
            return resp2.status_code == 200
        print(f"  ⚠ 텔레그램 오류 {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        print(f"  ⚠ 텔레그램 예외: {e}")
        return False


# ─── 리포트 분할 ──────────────────────────────────────────────────────────────

def _split_report(text: str, max_len: int) -> list:
    """
    리포트를 섹션(## 헤더) 기준으로 분할.
    섹션이 max_len을 넘으면 줄 단위로 강제 분할.
    """
    lines = text.split("\n")
    chunks = []
    current = []
    current_len = 0

    for line in lines:
        line_len = len(line) + 1  # +1 개행
        # 새 섹션 시작 & 현재 청크가 절반 이상 찼으면 분할
        if line.startswith("## ") and current_len > max_len // 2 and current:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        # 강제 분할
        if current_len + line_len > max_len and current:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len

    if current:
        chunks.append("\n".join(current))

    return chunks if chunks else [text[:max_len]]


# ─── 메인 전송 함수 ───────────────────────────────────────────────────────────

def send_report(report: str, today_str: str) -> bool:
    """
    리포트 전체를 텔레그램으로 전송.
    4000자 단위로 분할해 여러 메시지로 전송.

    Args:
        report:    마크다운 리포트 문자열
        today_str: "2026년 05월 08일" 형식 날짜

    Returns:
        True (전체 성공) / False (환경변수 미설정 또는 일부 실패)
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("  ⚠ TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정 → 텔레그램 알림 스킵")
        return False

    # 헤더 메시지 먼저 전송
    header = (
        f"📊 *메르AI 포트폴리오 리포트*\n"
        f"📅 {today_str}\n"
        f"{'─' * 22}"
    )
    _send_message(token, chat_id, header)
    time.sleep(0.5)

    # 리포트 분할 전송
    chunks = _split_report(report, MAX_MSG_LEN)
    total = len(chunks)
    success_count = 0

    for i, chunk in enumerate(chunks, 1):
        prefix = f"*\\[{i}/{total}\\]*\n\n" if total > 1 else ""
        ok = _send_message(token, chat_id, prefix + chunk)
        if ok:
            success_count += 1
        if i < total:
            time.sleep(0.5)

    print(f"  📱 텔레그램 전송: {success_count}/{total} 성공")
    return success_count == total


# ─── 직접 실행 테스트 ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_report = (
        "# 메르AI 포트폴리오 리포트\n\n"
        "## 📌 시장 분석 핵심 인사이트\n\n"
        "텔레그램 연결 테스트입니다. 정상 작동 중! ✅\n\n"
        "## 📊 포트폴리오 추천\n\n"
        "테스트 종목: 삼성전자 (005930) — 매수\n"
    )
    result = send_report(test_report, "2026년 05월 08일")
    print("✅ 전송 성공" if result else "❌ 전송 실패 (환경변수 확인 필요)")
