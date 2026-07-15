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

import hashlib
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

from portfolio_output import build_output_model

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_MSG_LEN = 4000
MESSAGE_TIMEOUT_SECONDS = 15
PHOTO_TIMEOUT_SECONDS = 30
MAX_RATE_LIMIT_RETRY_SECONDS = 30

DEFAULT_DASHBOARD_URL = (
    "https://pikachu444.github.io/mer-portfolio/output/dashboard.html"
)


@dataclass(frozen=True)
class _TelegramAttempt:
    """A single Telegram API attempt, with no request URL or secrets retained."""

    success: bool
    status_code: int | None
    response_json: dict[str, Any] | None
    exception_type: str | None = None
    invalid_json: bool = False


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


def _target_fingerprint(chat_id: str | None) -> str:
    """Return a stable opaque log label without exposing a chat ID."""
    normalized = str(chat_id or "").strip()
    if not normalized:
        return "missing"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"sha256:{digest}"


def _execution_context_label(run_label: str | None = None) -> str:
    """Turn the run mode into a user-facing, non-secret execution label."""
    raw_label = str(run_label if run_label is not None else os.environ.get("RUN_MODE", "")).strip()
    if not raw_label:
        return ""

    known_labels = {
        "scheduled": "GitHub Actions · 정기 점검",
        "scheduled_rebalance": "GitHub Actions · 정기 리밸런싱",
        "rebalance": "GitHub Actions · 운영 리밸런싱",
        "verify": "GitHub Actions · 출력 검증",
        "full_verify": "GitHub Actions · 전체 흐름 검증",
        "test": "테스트 실행",
    }
    return known_labels.get(raw_label.lower(), raw_label[:80])


def _safe_api_description(value: object, chat_id: str | None = None) -> str:
    """Keep Telegram error logs useful without allowing an accidental token echo."""
    description = " ".join(str(value or "응답 설명 없음").split())
    if chat_id:
        description = description.replace(str(chat_id), "chat<redacted>")
    description = re.sub(r"(?:bot)?\d{5,}:[A-Za-z0-9_-]+", "<redacted>", description)
    return description[:200]


def _message_id_from_response(response_json: dict[str, Any] | None) -> str | None:
    if not isinstance(response_json, dict):
        return None
    result = response_json.get("result")
    if not isinstance(result, dict):
        return None
    message_id = result.get("message_id")
    if isinstance(message_id, int) and not isinstance(message_id, bool):
        return str(message_id)
    if isinstance(message_id, str) and message_id.isdigit():
        return message_id
    return None


def _is_success_response(status_code: int | None, response_json: dict[str, Any] | None) -> bool:
    """A 200 alone is insufficient: Telegram must acknowledge a concrete Message."""
    return (
        status_code == 200
        and isinstance(response_json, dict)
        and response_json.get("ok") is True
        and _message_id_from_response(response_json) is not None
    )


def _post_telegram(
    method: str,
    token: str,
    *,
    timeout: int,
    **request_kwargs: Any,
) -> _TelegramAttempt:
    """Execute one request without logging a URL, token, or raw response body."""
    url = TELEGRAM_API.format(token=token, method=method)
    try:
        response = requests.post(url, timeout=timeout, **request_kwargs)
    except requests.RequestException as exc:
        return _TelegramAttempt(False, None, None, exception_type=type(exc).__name__)
    except Exception as exc:  # Keep notification failures from masking the analysis result.
        return _TelegramAttempt(False, None, None, exception_type=type(exc).__name__)

    try:
        response_json = response.json()
    except (ValueError, TypeError):
        return _TelegramAttempt(False, response.status_code, None, invalid_json=True)
    if not isinstance(response_json, dict):
        return _TelegramAttempt(False, response.status_code, None, invalid_json=True)
    return _TelegramAttempt(
        _is_success_response(response.status_code, response_json),
        response.status_code,
        response_json,
    )


def _is_rate_limited(attempt: _TelegramAttempt) -> bool:
    return (
        attempt.status_code == 429
        and isinstance(attempt.response_json, dict)
        and attempt.response_json.get("error_code") == 429
    )


def _retry_delay_seconds(attempt: _TelegramAttempt) -> int:
    parameters = (attempt.response_json or {}).get("parameters", {})
    retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
    try:
        seconds = int(float(retry_after))
    except (TypeError, ValueError):
        seconds = 1
    return max(1, min(seconds, MAX_RATE_LIMIT_RETRY_SECONDS))


def _attempt_with_rate_limit_retry(
    method: str,
    token: str,
    chat_id: str,
    *,
    timeout: int,
    before_retry: Callable[[], None] | None = None,
    **request_kwargs: Any,
) -> _TelegramAttempt:
    """Retry once only when Telegram explicitly says no message was accepted yet."""
    attempt = _post_telegram(method, token, timeout=timeout, **request_kwargs)
    if not _is_rate_limited(attempt):
        return attempt

    delay = _retry_delay_seconds(attempt)
    print(
        f"  Telegram {method} rate limited: target={_target_fingerprint(chat_id)} "
        f"retry_in={delay}s"
    )
    time.sleep(delay)
    if before_retry is not None:
        before_retry()
    return _post_telegram(method, token, timeout=timeout, **request_kwargs)


def _log_attempt(method: str, chat_id: str, attempt: _TelegramAttempt) -> None:
    """Write a delivery receipt or a bounded, secret-safe failure diagnostic."""
    target = _target_fingerprint(chat_id)
    if attempt.success:
        print(
            f"  Telegram {method} accepted: target={target} "
            f"message_id={_message_id_from_response(attempt.response_json)}"
        )
        return

    if attempt.exception_type:
        print(
            f"  !! Telegram {method} request exception: target={target} "
            f"exception={attempt.exception_type}"
        )
        return
    if attempt.invalid_json:
        print(
            f"  !! Telegram {method} invalid JSON response: target={target} "
            f"http_status={attempt.status_code}"
        )
        return

    response_json = attempt.response_json or {}
    error_code = response_json.get("error_code", "없음")
    api_ok = response_json.get("ok", "없음")
    description = _safe_api_description(response_json.get("description"), chat_id)
    print(
        f"  !! Telegram {method} delivery rejected: target={target} "
        f"http_status={attempt.status_code} api_ok={api_ok} "
        f"error_code={error_code} description={description}"
    )


def _is_markdown_parse_error(attempt: _TelegramAttempt) -> bool:
    if attempt.status_code != 400 or not isinstance(attempt.response_json, dict):
        return False
    description = str(attempt.response_json.get("description") or "").lower()
    return "parse entities" in description or "can't parse" in description


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
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": False,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    attempt = _attempt_with_rate_limit_retry(
        "sendMessage",
        token,
        chat_id,
        timeout=MESSAGE_TIMEOUT_SECONDS,
        json=payload,
    )
    if attempt.success:
        _log_attempt("sendMessage", chat_id, attempt)
        return True

    # A failed Markdown parse is safe to retry without formatting: Telegram did
    # not accept the original message. Other 400 errors must not be retried.
    if parse_mode and _is_markdown_parse_error(attempt):
        print(
            f"  Telegram sendMessage Markdown fallback: "
            f"target={_target_fingerprint(chat_id)}"
        )
        fallback_payload = dict(payload)
        fallback_payload.pop("parse_mode", None)
        fallback = _attempt_with_rate_limit_retry(
            "sendMessage",
            token,
            chat_id,
            timeout=MESSAGE_TIMEOUT_SECONDS,
            json=fallback_payload,
        )
        _log_attempt("sendMessage", chat_id, fallback)
        return fallback.success

    _log_attempt("sendMessage", chat_id, attempt)
    return False


# ─── 이미지 전송 ──────────────────────────────────────────────────────────────

def send_status(title: str, body: str = "") -> bool:
    """Send a short operational status message to Telegram."""
    token, chat_id = _get_credentials()
    if not token or not chat_id:
        print(
            "  !! TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing -> "
            f"status notification skipped: target={_target_fingerprint(chat_id)}"
        )
        return False

    lines = [f"*{title}*"]
    if body:
        lines.append(body)
    execution_context = _execution_context_label()
    if execution_context:
        lines.append(f"⚙ 실행: {execution_context}")
    lines.append(f"[Dashboard]({_get_dashboard_url()})")
    return _send_message(token, chat_id, "\n\n".join(lines))


def send_photo(image_path: str, caption: str = "") -> bool:
    """PNG/JPG 이미지 파일을 텔레그램으로 전송."""
    token, chat_id = _get_credentials()
    if not token or not chat_id:
        print(
            "  !! TELEGRAM 환경변수 미설정 -> 이미지 전송 스킵: "
            f"target={_target_fingerprint(chat_id)}"
        )
        return False

    try:
        with open(image_path, "rb") as f:
            attempt = _attempt_with_rate_limit_retry(
                "sendPhoto",
                token,
                chat_id,
                timeout=PHOTO_TIMEOUT_SECONDS,
                before_retry=lambda: f.seek(0),
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": f},
            )
        if attempt.success:
            _log_attempt("sendPhoto", chat_id, attempt)
            print("  차트 이미지 전송 완료")
            return True
        _log_attempt("sendPhoto", chat_id, attempt)
        return False
    except FileNotFoundError:
        print(f"  !! 이미지 파일 없음: {image_path}")
        return False
    except Exception as exc:
        print(f"  !! 이미지 전송 예외: exception={type(exc).__name__}")
        return False


# ─── 리포트 전송 (요약 + URL) ─────────────────────────────────────────────────

def _is_valid_report(report: str) -> bool:
    """실제 분석 리포트인지 확인 (테스트/빈 내용 필터링)."""
    if not report or len(report) < 500:
        return False
    # 핵심 섹션이 하나라도 있어야 함
    return any(keyword in report for keyword in ("인사이트", "포트폴리오", "국내주식", "해외주식"))


def send_report(report: str, today_str: str, *, run_label: str | None = None) -> bool:
    """
    리포트 요약 + 대시보드 URL을 텔레그램으로 전송.
    헤더와 요약을 하나의 메시지로 결합해 전송합니다.

    Returns:
        True (성공) / False (환경변수 미설정, 빈 리포트, 또는 실패)
    """
    token, chat_id = _get_credentials()
    if not token or not chat_id:
        print(
            "  !! TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정 -> "
            f"텔레그램 알림 스킵: target={_target_fingerprint(chat_id)}"
        )
        return False

    # 빈/테스트 리포트 가드
    if not _is_valid_report(report):
        print("  !! 리포트가 비어있거나 테스트 내용 -> 텔레그램 전송 스킵")
        return False

    # 헤더 + 요약을 하나의 메시지로 결합 (메시지 분리 혼란 방지)
    header = (
        f"\U0001f4ca *메르AI 포트폴리오 리포트*\n"
        f"\U0001f4c5 {today_str}\n"
    )
    execution_context = _execution_context_label(run_label)
    if execution_context:
        header += f"⚙ 실행: {execution_context}\n"
    header += f"{'─' * 22}\n"
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
    status_note: str = "",
    include_dashboard_link: bool = True,
    run_label: str | None = None,
    insight_limit: int = 3,
) -> str:
    """모바일에서 투자자가 바로 읽는 요약 대시보드."""
    performance = performance or {}
    output = build_output_model(
        state,
        performance,
        today_str=today_str,
        status_note=status_note,
        approved_changes=not no_changes,
    )
    policy = output.get("rebalance_policy", {})

    def pct(value: Any) -> str:
        if value is None:
            return "데이터 없음"
        return f"{float(value):.2f}%"

    inception = output.get("performance_inception_date")
    return_label = f"누적 수익률{f' ({inception} 이후)' if inception else ''}"
    actual_available = output.get("actual_allocation_available")
    stock = output.get("actual_stock_weight") if actual_available else output.get("target_stock_weight")
    etf = output.get("actual_etf_weight") if actual_available else output.get("target_etf_weight")
    cash = output.get("actual_cash_weight") if actual_available else output.get("target_cash_weight")
    lines = [
        "📊 *메르AI 모델 포트폴리오*",
        f"📅 {today_str}",
        "※ 메르 블로그 공개 분석을 바탕으로 구성한 참고용 모델 포트폴리오입니다.",
        "",
        "*오늘의 요약*",
        f"• {return_label}: {output.get('portfolio_return_label', '데이터 없음')}",
        f"• 최대 낙폭: {output.get('max_drawdown_label', '데이터 없음')}",
        f"• 기준 포트폴리오 대비: {output.get('benchmark_difference_label', '데이터 없음')} (수익률 차이)",
        f"• 자산배분({'실제' if actual_available else '목표'}): 개별주 {pct(stock)} / 주식형 ETF {pct(etf)} / 현금성 {pct(cash)}",
        f"• 오늘 승인된 비중 변경: {'있음' if output.get('approved_today_changes') else '없음'}",
    ]
    if not output.get("approved_today_changes") and not output.get("actions_deferred"):
        lines.append("• 전 종목이 리밸런싱 허용 범위 또는 최소 조정 기준 안에 있습니다.")
    elif output.get("actions_deferred") and output.get("today_changes"):
        lines.append("• 현재 비중 이탈이 확인됐지만 내부 검증이 끝나지 않아 자동 조정을 보류합니다.")
    if output.get("status_note"):
        lines.append(f"• {output['status_note']}")
    if output.get("cumulative_costs") is not None:
        lines.append(f"• 누적 거래비용 추정: {float(output['cumulative_costs']):.4f} 모델단위")
    deferred_posts = output.get("deferred_posts", []) or []
    if deferred_posts:
        lines += ["", "⚠ *오늘 분석에서 제외된 글*"]
        for item in deferred_posts[:3]:
            title = str(item.get("title") or "제목 없음")
            url = str(item.get("url") or "")
            lines.append(f"• {title}" + (f" — {url}" if url else ""))

    def holding_line(item: dict) -> str:
        code = f" ({item.get('code')})" if item.get("code") else ""
        return (
            f"• {item.get('name', '')}{code} {pct(item.get('actual_weight'))} → 목표 {pct(item.get('target_weight'))}"
            f" | {item.get('display_today_action') or item.get('today_action', '유지')}"
        )

    lines += ["", "*현재 보유 종목*"]
    groups = [
        ("국내 개별주", [r for r in output["portfolio"] if r.get("asset_type") == "stock" and str(r.get("market", "")).upper().startswith("KR")]),
        ("국내·해외 ETF", [r for r in output["portfolio"] if r.get("asset_type") == "etf"]),
        ("해외 개별주", [r for r in output["portfolio"] if r.get("asset_type") == "stock" and not str(r.get("market", "")).upper().startswith("KR")]),
    ]
    for title, rows in groups:
        if rows:
            lines.append(f"[{title}]")
            lines.extend(holding_line(item) for item in rows)
    lines.append(f"[현금성 자산] • 현금성 {pct(output.get('actual_cash_weight'))} → 목표 {pct(output.get('target_cash_weight'))} | 유지")

    lines += ["", "*오늘의 조정*"]
    if output.get("approved_today_changes"):
        for item in output["approved_today_changes"]:
            lines.append(f"• {item.get('name')} {pct(item.get('actual_weight'))} → {pct(item.get('target_weight'))} | {item.get('display_today_action') or item.get('today_action')}")
    elif output.get("actions_deferred") and output.get("today_changes"):
        lines += [
            "• 승인된 매매 없음",
            "• 현재 비중 이탈이 확인됐지만 내부 검증이 끝나지 않아 자동 조정을 보류합니다.",
        ]
    else:
        lines += ["• 승인된 매매 없음", "• 전 종목이 리밸런싱 허용 범위 내에 있습니다."]
    lines += ["", "📌 *핵심 인사이트*"]
    insights = output.get("insights", []) or []
    if insights:
        for index, item in enumerate(insights[: max(0, insight_limit)], start=1):
            lines.append(f"{index}. *{item.get('title', '시장 인사이트')}*")
            lines.append(f"  └ {item.get('summary', '')}")
            lines.append(f"  └ 추적할 조건: {item.get('investment_implication', '')}")
    else:
        lines.append("• 표시할 인사이트 없음")

    changed_watchlist = output.get("watchlist_changes_display", []) or []
    if changed_watchlist:
        lines += ["", "*관심종목 변경*"]
        for item in changed_watchlist:
            reason = f" — {item['reason']}" if item.get("reason") else ""
            code = f" ({item['code']})" if item.get("code") else ""
            lines.append(f"• {item['label']}: {item['name']}{code}{reason}")
    if include_dashboard_link:
        lines += ["", f"🌐 [상세 대시보드 보기]({_get_dashboard_url()})"]
    else:
        lines += ["", "🌐 상세 대시보드는 실행 artifact에서 확인합니다."]
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
    status_note: str = "",
    include_dashboard_link: bool = True,
    run_label: str | None = None,
) -> bool:
    token, chat_id = _get_credentials()
    if not token or not chat_id:
        print(
            "  !! TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정 -> "
            f"텔레그램 알림 스킵: target={_target_fingerprint(chat_id)}"
        )
        return False
    # Shrink explanatory content first. Holdings and today's adjustment are
    # never dropped; if the result still exceeds Telegram's limit it is split
    # at line boundaries afterwards.
    summary = ""
    for insight_limit in (3, 2, 1, 0):
        candidate = build_structured_summary(
            state,
            today_str,
            performance,
            no_changes=no_changes,
            status_note=status_note,
            include_dashboard_link=include_dashboard_link,
            run_label=run_label,
            insight_limit=insight_limit,
        )
        summary = candidate
        if len(candidate) <= MAX_MSG_LEN:
            break
    messages = split_telegram_message(summary)
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
