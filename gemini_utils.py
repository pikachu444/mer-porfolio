"""
Gemini API 호출 보조 유틸리티.

무료 티어에서는 rate limit이 자주 병목이 되므로 모델별 최소 호출 간격을
보수적으로 지킨다. 품질 정책은 호출부에서 결정하고, 이 모듈은 재시도와
대기만 담당한다.
"""

import re
import time
from collections import defaultdict


MODEL_MIN_INTERVALS = {
    "gemini-2.5-pro": 15.0,
    "gemini-2.5-flash": 8.0,
    "gemini-3-flash-preview": 8.0,
}

DEFAULT_MIN_INTERVAL = 8.0

_last_call_at = defaultdict(float)


def get_min_interval(model: str) -> float:
    return MODEL_MIN_INTERVALS.get(model, DEFAULT_MIN_INTERVAL)


def is_rate_limit_error(message: str) -> bool:
    msg = message.lower()
    return any(token in msg for token in ("429", "resource", "exhausted", "quota", "rate", "limit"))


def is_transient_error(message: str) -> bool:
    msg = message.lower()
    return is_rate_limit_error(message) or any(
        token in msg for token in ("503", "unavailable", "high demand", "try again later")
    )


def is_daily_quota_error(message: str) -> bool:
    msg = message.lower()
    return any(
        token in msg
        for token in (
            "perday",
            "requestsperday",
            "tokensperday",
            "daily quota",
            "quota exceeded",
        )
    )


def parse_retry_delay(message: str) -> float | None:
    patterns = (
        r"retry in ([\d.]+)s",
        r"retryDelay': '(\d+)s'",
        r'"retryDelay"\s*:\s*"(\d+)s"',
        r"retry_delay\s*\{\s*seconds:\s*(\d+)",
        r"retryDelay[^\d]+(\d+)\s*s",
    )
    for pattern in patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            try:
                return float(match.group(1)) + 1.0
            except ValueError:
                return None
    return None


def wait_for_model_slot(model: str) -> None:
    min_interval = get_min_interval(model)
    elapsed = time.monotonic() - _last_call_at[model]
    if elapsed < min_interval:
        wait_sec = min_interval - elapsed
        print(f"      Waiting {wait_sec:.1f}s before {model} call.")
        time.sleep(wait_sec)


def mark_model_called(model: str) -> None:
    _last_call_at[model] = time.monotonic()


def generate_content_with_retry(client, model: str, contents, config, max_retries: int = 3):
    backoff = get_min_interval(model)
    for attempt in range(max_retries):
        wait_for_model_slot(model)
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            mark_model_called(model)
            return response
        except Exception as e:
            mark_model_called(model)
            message = str(e)
            if not is_transient_error(message):
                raise

            print(f"      Retryable Gemini API error: {model} ({attempt + 1}/{max_retries})")
            if attempt == max_retries - 1:
                raise

            retry_delay = parse_retry_delay(message)
            if is_daily_quota_error(message):
                raise

            wait_sec = max(retry_delay or backoff, get_min_interval(model))
            print(f"      Waiting {wait_sec:.1f}s before retry.")
            time.sleep(wait_sec)
            backoff = min(backoff * 1.5, 120.0)

    raise RuntimeError(f"{model} API call failed after {max_retries} retries.")
