"""
Compare an OpenAI-compatible LLM API without touching the operational workflow.

The default mode only writes the exact evaluation requests. Add --execute after
setting the provider API key to call the remote API and validate its outputs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from analyze import _parse_and_validate_model_decision_json, _structured_context, _validate_markdown_report
from portfolio_schema import load_or_migrate_portfolio_state
from system_prompt import (
    DECISION_SYSTEM_PROMPT,
    REPORT_SYSTEM_PROMPT,
    build_decision_user_message,
    build_report_user_message,
)


@dataclass(frozen=True)
class ProviderConfig:
    endpoint: str
    api_key_env: str
    default_model: str
    api_key_required: bool = True


PROVIDERS = {
    "cerebras": ProviderConfig(
        endpoint="https://api.cerebras.ai/v1/chat/completions",
        api_key_env="CEREBRAS_API_KEY",
        default_model="gpt-oss-120b",
    ),
    "opencode-zen": ProviderConfig(
        endpoint="https://opencode.ai/zen/v1/chat/completions",
        api_key_env="OPENCODE_API_KEY",
        default_model="deepseek-v4-flash-free",
        api_key_required=False,
    ),
}


def _safe_name(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "-", value).strip("-") or "model"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def select_evaluation_posts(posts: list[dict[str, Any]], days: int, limit: int | None) -> list[dict[str, Any]]:
    """Use recent posts, excluding only posts already classified as unrelated."""
    cutoff = date.today() - timedelta(days=days)
    selected = [
        post
        for post in posts
        if date.fromisoformat(post["date"]) >= cutoff
        and post.get("investment_relevant") is not False
    ]
    selected.sort(key=lambda post: post["date"], reverse=True)
    return selected[:limit] if limit else selected


def build_chat_payload(model: str, system_instruction: str, user_message: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system_instruction.strip()},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.2,
        "max_tokens": 16384,
    }


def call_openai_compatible(endpoint: str, api_key: str, payload: dict[str, Any]) -> str:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    response = requests.post(
        endpoint,
        headers=headers,
        json=payload,
        timeout=180,
    )
    response.raise_for_status()
    body = response.json()
    text = body["choices"][0]["message"]["content"]
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("API 응답 텍스트가 비어 있습니다.")
    return text


def prepare_evaluation(
    posts_path: Path,
    state_path: Path,
    analysis_date: str,
    days: int,
    limit: int | None,
    model: str,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], str]:
    posts = select_evaluation_posts(_read_json(posts_path), days, limit)
    if not posts:
        raise ValueError("비교 평가에 사용할 최근 글이 없습니다.")

    state = load_or_migrate_portfolio_state(_read_json(state_path)).to_dict()
    context = _structured_context(posts)
    decision_message = build_decision_user_message(
        context=context,
        analysis_date=analysis_date,
        run_type="regular",
        current_state=state,
    )
    decision_payload = build_chat_payload(model, DECISION_SYSTEM_PROMPT, decision_message)
    metadata = {
        "analysis_date": analysis_date,
        "post_count": len(posts),
        "post_titles": [post["title"] for post in posts],
        "state_path": str(state_path),
        "posts_path": str(posts_path),
    }
    return metadata, decision_payload, posts, context


def run(args: argparse.Namespace) -> Path:
    provider = PROVIDERS[args.provider]
    model = args.model or provider.default_model
    output_dir = args.output_dir or Path("evaluation_runs") / (
        f"{datetime.now():%Y%m%d-%H%M%S}-{args.provider}-{_safe_name(model)}"
    )
    output_dir.mkdir(parents=True, exist_ok=False)

    metadata, decision_payload, _, context = prepare_evaluation(
        posts_path=args.posts,
        state_path=args.state,
        analysis_date=args.analysis_date,
        days=args.days,
        limit=args.limit,
        model=model,
    )
    metadata.update(
        {
            "provider": args.provider,
            "model": model,
            "endpoint": provider.endpoint,
            "executed": bool(args.execute),
            "status": "prepared",
        }
    )
    _write_json(output_dir / "metadata.json", metadata)
    _write_json(output_dir / "01-decision-request.json", decision_payload)

    if not args.execute:
        return output_dir

    api_key = os.environ.get(provider.api_key_env, "").strip()
    if provider.api_key_required and not api_key:
        raise ValueError(f"{provider.api_key_env} 환경변수를 설정한 뒤 다시 실행하십시오.")

    try:
        decision_text = call_openai_compatible(provider.endpoint, api_key, decision_payload)
        (output_dir / "02-decision-raw.txt").write_text(decision_text, encoding="utf-8")

        state = load_or_migrate_portfolio_state(_read_json(args.state)).to_dict()
        decision = _parse_and_validate_model_decision_json(decision_text, state)
        _write_json(output_dir / "03-decision-validated.json", decision.to_dict())

        report_message = build_report_user_message(
            context=context,
            decision_payload=decision.to_dict(),
            analysis_date=args.analysis_date,
        )
        report_payload = build_chat_payload(model, REPORT_SYSTEM_PROMPT, report_message)
        _write_json(output_dir / "04-report-request.json", report_payload)

        report = call_openai_compatible(provider.endpoint, api_key, report_payload)
        _validate_markdown_report(report)
        (output_dir / "05-report-validated.md").write_text(report, encoding="utf-8")
    except Exception as exc:
        metadata.update(
            {
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc)[:1000],
            }
        )
        _write_json(output_dir / "metadata.json", metadata)
        raise

    metadata["status"] = "completed"
    _write_json(output_dir / "metadata.json", metadata)
    return output_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="무료 LLM API 비교 평가 요청을 생성하거나 실행합니다.")
    parser.add_argument("--provider", choices=sorted(PROVIDERS), required=True)
    parser.add_argument("--model", help="기본 모델 대신 평가할 모델 ID")
    parser.add_argument("--execute", action="store_true", help="실제 API를 호출하고 응답을 검증")
    parser.add_argument("--analysis-date", default=date.today().isoformat())
    parser.add_argument("--days", type=int, default=14, help="최근 글 조회 기간")
    parser.add_argument("--limit", type=int, help="평가 글 수를 제한할 때만 지정")
    parser.add_argument("--posts", type=Path, default=Path("output/posts_db.json"))
    parser.add_argument("--state", type=Path, default=Path("output/portfolio_state.json"))
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args()


if __name__ == "__main__":
    result_dir = run(parse_args())
    print(f"평가 결과 저장: {result_dir}")
