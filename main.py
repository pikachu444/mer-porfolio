"""메르AI 모델 포트폴리오 운영 진입점."""

from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from runtime_modes import get_run_policy, should_rebalance


RUN_MODE = os.environ.get("RUN_MODE", "scheduled").lower()
RUN_POLICY = get_run_policy(RUN_MODE)
OPERATING_OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "output"))
OUTPUT_DIR = OPERATING_OUTPUT_DIR
if RUN_MODE == "verify":
    OUTPUT_DIR = OPERATING_OUTPUT_DIR / "verify"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for filename in (
        "portfolio_state.json",
        "model_portfolio_ledger.json",
        "performance_cache.json",
        "posts_db.json",
    ):
        source = OPERATING_OUTPUT_DIR / filename
        target = OUTPUT_DIR / filename
        if source.exists():
            shutil.copy2(source, target)
    os.environ["OUTPUT_DIR"] = str(OUTPUT_DIR)

if RUN_MODE == "test" and __name__ == "__main__":
    print("test 모드는 python -m unittest discover -s tests -v 로 실행합니다.")
    sys.exit(0)

from analyze import analyze_posts_structured
from fetch_mer import (
    fetch_recent_posts,
    get_last_fetch_new_post_urls,
    load_cached_posts,
    select_new_relevant_posts,
    select_rebalance_posts,
)
from generate_dashboard import generate_all
from portfolio_output import build_markdown_report, build_output_model
from portfolio_schema import (
    AnalysisDecisionV2,
    apply_analysis_decision,
    load_portfolio_state_file,
    parse_analysis_decision,
    parse_portfolio_state,
    save_analysis_decision_file,
    save_portfolio_state_file,
)
from telegram_notify import send_photo, send_status, send_structured_summary
from track_returns import (
    MODEL_LEDGER_FILE,
    apply_structured_transactions,
    get_structured_prices,
    load_model_ledger,
    refresh_structured_performance,
    sanitize_model_ledger_for_state,
    sanitize_performance_cache_for_state,
    sanitize_performance_files_for_state,
    save_model_ledger,
    transaction_decisions_for_run,
)


STATE_PATH = OUTPUT_DIR / "portfolio_state.json"
DECISION_PATH = OUTPUT_DIR / "decision_latest.json"
_fetch_days_env = os.environ.get("FETCH_DAYS", "").strip()
FETCH_DAYS = int(_fetch_days_env) if _fetch_days_env else RUN_POLICY.fetch_days
KST = ZoneInfo("Asia/Seoul")


def _to_kst_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(KST).replace(tzinfo=None)


def _now_kst() -> datetime:
    return _to_kst_naive(datetime.now(timezone.utc))


def _empty_state():
    return parse_portfolio_state({
        "schema_version": "2.0",
        "portfolio": [],
        "watchlist": [],
        "closed_positions": [],
        "decision_history": [],
        "insights": [],
        "last_rebalanced_date": None,
    })


def _load_state():
    if not STATE_PATH.exists():
        return _empty_state()
    return load_portfolio_state_file(STATE_PATH)


def _save_report(report: str, today: datetime) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"report_{today:%Y%m%d}.md"
    path.write_text(report, encoding="utf-8")
    return path


def _save_error_log(message: str, today: datetime) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"error_{today:%Y%m%d_%H%M%S}.log"
    path.write_text(message, encoding="utf-8")
    print(f"  오류 로그: {path}")


def _notify_status(title: str, body: str) -> None:
    if RUN_POLICY.send_telegram:
        send_status(title, body)


def _short_title(title: str, limit: int = 34) -> str:
    title = " ".join(str(title or "").split())
    if len(title) <= limit:
        return title
    return title[: limit - 1] + "…"


def _deferred_posts_for_run(
    posts: list[dict],
    *,
    is_rebalance: bool,
    state,
    today: datetime,
) -> list[dict]:
    deferred = [
        post for post in posts
        if post.get("summary_status") == "deferred"
    ]
    if is_rebalance:
        try:
            cutoff = datetime.fromisoformat(state.last_rebalanced_date)
        except Exception:
            cutoff = today - timedelta(days=FETCH_DAYS)
        return [
            post for post in deferred
            if datetime.fromisoformat(post["date"]) > cutoff
        ]
    new_urls = get_last_fetch_new_post_urls()
    return [post for post in deferred if post.get("url") in new_urls]


def _deferred_status_note(posts: list[dict]) -> str:
    if not posts:
        return ""
    titles = ", ".join(_short_title(post.get("title", "")) for post in posts[:2])
    if len(posts) > 2:
        titles += f" 외 {len(posts) - 2}건"
    return f"새 글 {len(posts)}건 요약 실패로 투자 분석 보류: {titles}"


def _prices_for_ledger(ledger: dict) -> dict[str, float]:
    return get_structured_prices(ledger.get("positions", []))


def _run_no_change_update(state, today: datetime, status_note: str = "") -> int:
    if status_note:
        print(f"  {status_note}: 판단과 목표 비중을 유지하고 성과만 갱신합니다.")
    else:
        print("  신규 글 없음: 판단과 목표 비중을 유지하고 성과만 갱신합니다.")
    ledger = load_model_ledger()
    ledger = sanitize_model_ledger_for_state(ledger, state.to_dict())
    if ledger.get("positions"):
        prices = _prices_for_ledger(ledger)
        cache = refresh_structured_performance(ledger, prices, today.strftime("%Y-%m-%d"))
        cache = sanitize_performance_cache_for_state(cache, state.to_dict())
        save_model_ledger(ledger)
    else:
        cache = sanitize_performance_files_for_state(state.to_dict())
    save_portfolio_state_file(state, STATE_PATH)
    output_state = state.to_dict()
    if status_note:
        output_state["status_note"] = status_note
    output = build_output_model(
        output_state,
        cache,
        today_str=today.strftime("%Y-%m-%d"),
        status_note=status_note,
    )
    report = build_markdown_report(output)
    _save_report(report, today)
    _, png_path = generate_all(report, today, state=output_state)
    if RUN_POLICY.send_telegram:
        if png_path and png_path.exists():
            send_photo(str(png_path), f"모델 포트폴리오 성과 | {today:%Y년 %m월 %d일}")
        if not send_structured_summary(
            output_state,
            today.strftime("%Y년 %m월 %d일"),
            cache,
            no_changes=True,
            status_note=status_note,
        ):
            return 1
    return 0


def _is_llm_service_unavailable_error(exc: Exception) -> bool:
    message = str(exc)
    if "1차 포트폴리오 판단 실패" not in message:
        return False
    markers = (
        "429",
        "RESOURCE_EXHAUSTED",
        "503",
        "UNAVAILABLE",
        "504",
        "DEADLINE_EXCEEDED",
        "timeout",
        "Server disconnected",
    )
    return any(marker in message for marker in markers)


def _collect_posts() -> list[dict]:
    fetch_recent_posts(days=FETCH_DAYS)
    return load_cached_posts()


def _portfolio_identity(item: dict) -> tuple[str, str, str]:
    return (
        str(item.get("asset_type", "")).strip().lower(),
        str(item.get("market", "")).strip().upper(),
        str(item.get("code", "")).strip().upper(),
    )


def _exclude_unpriceable_new_suggestions(
    decision: AnalysisDecisionV2,
    state,
) -> AnalysisDecisionV2:
    """Keep existing holdings strict while dropping unverifiable new suggestions."""
    existing = {_portfolio_identity(item) for item in state.portfolio}
    accepted = []
    for item in decision.portfolio_decisions:
        try:
            get_structured_prices([item])
        except ValueError:
            if _portfolio_identity(item) in existing:
                raise
            print(
                "    가격 검증 불가 신규 포트폴리오 제안 제외: "
                + str(item.get("name") or "이름 없음")
            )
            continue
        accepted.append(item)
    return parse_analysis_decision({
        **decision.to_dict(),
        "portfolio_decisions": accepted,
    })


def main() -> int:
    if RUN_MODE == "test":
        print("test 모드는 python -m unittest discover -s tests -v 로 실행합니다.")
        return 0

    today = _now_kst()
    today_date = today.strftime("%Y-%m-%d")
    print("=" * 60)
    print("  메르AI 모델 포트폴리오 실행")
    print(f"  모드: {RUN_MODE} | 출력: {OUTPUT_DIR}")
    print("=" * 60)

    try:
        state = _load_state()
        is_rebalance = should_rebalance(
            RUN_MODE,
            state.last_rebalanced_date,
            today.date(),
        )
        cached_posts = _collect_posts()
        if is_rebalance:
            posts = select_rebalance_posts(
                cached_posts,
                state.last_rebalanced_date,
                today,
            )
        else:
            posts = select_new_relevant_posts(
                cached_posts,
                get_last_fetch_new_post_urls(),
            )
        deferred_posts = _deferred_posts_for_run(
            cached_posts,
            is_rebalance=is_rebalance,
            state=state,
            today=today,
        )
        deferred_note = _deferred_status_note(deferred_posts)
        if not posts:
            if is_rebalance:
                print("  리밸런싱 연기: 마지막 리밸런싱 이후 투자 관련 신규 글이 없습니다.")
            return _run_no_change_update(state, today, status_note=deferred_note)

        try:
            result = analyze_posts_structured(
                posts,
                today_date,
                state.to_dict(),
                is_rebalance=is_rebalance,
                decision_validator=lambda decision: _exclude_unpriceable_new_suggestions(
                    decision,
                    state,
                ),
            )
        except RuntimeError as exc:
            if not _is_llm_service_unavailable_error(exc):
                raise
            note = "LLM 한도 초과 또는 일시 장애로 신규 글 분석 보류"
            if deferred_note:
                note += f" / {deferred_note}"
            print(f"  {note}: 기존 포트폴리오 상태로 출력만 갱신합니다.")
            _save_error_log(f"{type(exc).__name__}: {exc}", today)
            return _run_no_change_update(state, today, status_note=note)
        updated_state = apply_analysis_decision(state, result.decision)
        ledger = load_model_ledger()
        ledger = sanitize_model_ledger_for_state(ledger, updated_state.to_dict())
        transaction_decisions = transaction_decisions_for_run(
            ledger,
            updated_state.portfolio,
            result.decision.portfolio_decisions,
        )
        pricing_items = transaction_decisions + ledger.get("positions", [])
        prices = get_structured_prices(pricing_items)
        ledger = apply_structured_transactions(
            ledger,
            transaction_decisions,
            prices,
            today_date,
        )
        cache = refresh_structured_performance(ledger, prices, today_date)
        cache = sanitize_performance_cache_for_state(cache, updated_state.to_dict())

        save_analysis_decision_file(result.decision, DECISION_PATH)
        save_portfolio_state_file(updated_state, STATE_PATH)
        save_model_ledger(ledger, MODEL_LEDGER_FILE)
        output_state = updated_state.to_dict()
        if deferred_note:
            output_state["status_note"] = deferred_note
        output = build_output_model(
            output_state,
            cache,
            today_str=today_date,
            status_note=deferred_note,
        )
        report = build_markdown_report(output)
        _save_report(report, today)
        _, png_path = generate_all(report, today, state=output_state)

        if RUN_POLICY.send_telegram:
            if png_path and png_path.exists():
                send_photo(str(png_path), f"모델 포트폴리오 성과 | {today:%Y년 %m월 %d일}")
            if not send_structured_summary(
                output_state,
                today.strftime("%Y년 %m월 %d일"),
                cache,
                status_note=deferred_note,
            ):
                return 1
        print(f"  완료: 포트폴리오 {len(updated_state.portfolio)}종목")
        return 0
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        print(f"  실행 실패: {message}")
        _save_error_log(message, today)
        _notify_status("MerAI run failed", message[:1500])
        return 1


if __name__ == "__main__":
    sys.exit(main())
