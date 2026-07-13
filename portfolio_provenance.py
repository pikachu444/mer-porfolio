"""Host-validated signal events and immutable portfolio provenance wiring."""

from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from datetime import date
from typing import Any, Iterable

from portfolio_schema import (
    AnalysisDecisionV2,
    evidence_sha256,
    parse_analysis_decision,
    signal_event_id,
)


_HORIZONS = {
    "event": (0, 20),
    "tactical": (1, 63),
    "cyclical": (20, 180),
    "structural": (60, 365),
}


def _iso_date(value: Any) -> str:
    text = str(value or "").strip().replace(".", "-").replace("/", "-")
    match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if not match:
        raise ValueError(f"signal post date is not YYYY-MM-DD: {value!r}")
    result = f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    date.fromisoformat(result)
    return result


def _created_at(value: str) -> str:
    return value if "T" in value else value + "T00:00:00+09:00"


def _post_id(post: dict[str, Any]) -> str:
    explicit = str(post.get("post_id") or post.get("id") or "").strip()
    if explicit:
        return explicit
    url = str(post.get("url") or "")
    numbers = re.findall(r"\d+", url)
    if numbers:
        return numbers[-1]
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]


def _asset_type(entity_type: Any) -> str:
    normalized = str(entity_type or "").strip().lower()
    if normalized in {"company", "stock", "security", "기업", "종목"}:
        return "stock"
    if normalized == "etf":
        return "etf"
    return "sector"


def _direction(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if any(token in normalized for token in ("positive", "bull", "수혜", "긍정", "상승", "매수", "보유", "확대")):
        return "bullish"
    if any(token in normalized for token in ("negative", "bear", "피해", "부정", "하락", "매도", "축소", "회피")):
        return "bearish"
    return "neutral"


def _thesis_id(candidate: dict[str, Any]) -> str:
    # A thesis spans multiple posts/evidence events.  Do not include free-form
    # summary wording: small paraphrases used to create parallel Watchlist rows.
    identity = "|".join(
        str(candidate.get(key) or "").strip().lower()
        for key in (
            "entity_name",
            "classification",
            "direction",
            "horizon_kind",
        )
    )
    return "thesis_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def prepare_post_signal_events(
    posts: Iterable[dict[str, Any]],
    *,
    created_at: str,
    model_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Convert verified summary candidates to state events and ledger IDs."""
    prepared = deepcopy(list(posts))
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    for post in prepared:
        url = str(post.get("url") or "").strip()
        if not url:
            continue
        source_model_id = str(
            post.get("summary_model_version")
            or post.get("summary_model_id")
            or "legacy-summary-model-unknown"
        ).strip()
        published_date = _iso_date(post.get("date") or post.get("published_date"))
        for candidate in post.get("signal_candidates", []) or []:
            classification = str(candidate.get("classification") or "").strip().upper()
            signal_type = {
                "MER_DIRECT": "MER_DIRECT",
                "DIRECTIONAL_THESIS": "MER_THESIS",
                "MENTION_ONLY": "MENTION_ONLY",
            }.get(classification, "MENTION_ONLY")
            horizon_kind = str(candidate.get("horizon_kind") or "").strip().lower()
            minimum, maximum = _HORIZONS.get(horizon_kind, (0, 120))
            thesis_id = str(candidate.get("thesis_id") or "").strip() or _thesis_id(candidate)
            event = {
                "signal_type": signal_type,
                "post_id": _post_id(post),
                "post_title": str(post.get("title") or "제목 없음"),
                "post_url": url,
                "published_date": published_date,
                "evidence_text": str(candidate.get("exact_text") or "").strip(),
                "evidence_sha256": evidence_sha256(str(candidate.get("exact_text") or "").strip()),
                "entity": {
                    "name": str(candidate.get("entity_name") or "").strip(),
                    "code": str(candidate.get("code") or "").strip().upper(),
                    "market": str(candidate.get("market") or "").strip().upper(),
                    "asset_type": _asset_type(candidate.get("entity_type")),
                },
                "direction": _direction(candidate.get("direction")),
                "horizon": {"min_days": minimum, "max_days": maximum},
                "catalysts": list(candidate.get("catalysts") or []),
                "invalidation_conditions": list(candidate.get("invalidation_conditions") or []),
                "thesis_id": thesis_id,
                "parent_signal_ids": [],
                "created_by": "gemini-summary-host-verified",
                "model_id": source_model_id,
                "created_at": _created_at(created_at),
                "source_classification": classification,
            }
            event["signal_id"] = signal_event_id(event)
            candidate["signal_id"] = event["signal_id"]
            candidate["evidence_sha256"] = event["evidence_sha256"]
            candidate["thesis_id"] = thesis_id
            if event["signal_id"] not in seen:
                events.append(event)
                seen.add(event["signal_id"])
    return prepared, events


def _identity_matches(decision: dict[str, Any], event: dict[str, Any]) -> bool:
    decision_code = str(decision.get("code") or "").strip().upper()
    event_code = str(event.get("entity", {}).get("code") or "").strip().upper()
    if decision_code and event_code:
        return decision_code == event_code
    decision_name = re.sub(r"\s+", "", str(decision.get("name") or "")).lower()
    event_name = re.sub(r"\s+", "", str(event.get("entity", {}).get("name") or "")).lower()
    return bool(decision_name and event_name and decision_name == event_name)


def _direction_matches_action(decision: dict[str, Any], event: dict[str, Any]) -> bool:
    """Prevent a bearish/sold source signal from authorizing a long position."""
    action = str(decision.get("action") or "").strip()
    direction = str(event.get("direction") or "").strip().lower()
    if action in {"매수", "비중확대", "보유"}:
        return direction == "bullish"
    if action in {"매도", "비중축소"}:
        return direction == "bearish"
    return False


def _evidence_urls(decision: dict[str, Any]) -> set[str]:
    return {
        str(item.get("url") or "").strip()
        for item in decision.get("evidence_posts", []) or []
        if str(item.get("url") or "").strip()
    }


def _linked_events(
    decision: dict[str, Any],
    event_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    explicit_ids = [
        str(value).strip()
        for value in decision.get("linked_signal_ids", []) or []
        if str(value).strip() in event_by_id
    ]
    if explicit_ids:
        urls = _evidence_urls(decision)
        return [
            event_by_id[value]
            for value in explicit_ids
            if event_by_id[value]["post_url"] in urls
        ]
    urls = _evidence_urls(decision)
    return [
        event
        for event in event_by_id.values()
        if event["post_url"] in urls and _identity_matches(decision, event)
    ]


def _ai_inference_event(
    decision: dict[str, Any],
    parents: list[dict[str, Any]],
    *,
    created_at: str,
    model_id: str,
) -> dict[str, Any]:
    parent = parents[0]
    action = str(decision.get("action") or "")
    direction = "bearish" if action in {"매도", "비중축소"} else "bullish"
    event = {
        "signal_type": "AI_INFERRED",
        "post_id": parent["post_id"],
        "post_title": parent["post_title"],
        "post_url": parent["post_url"],
        "published_date": parent["published_date"],
        "evidence_text": parent["evidence_text"],
        "evidence_sha256": parent["evidence_sha256"],
        "entity": {
            "name": str(decision.get("name") or ""),
            "code": str(decision.get("code") or ""),
            "market": str(decision.get("market") or ""),
            "asset_type": str(decision.get("asset_type") or "stock"),
        },
        "direction": direction,
        "horizon": {"min_days": 20, "max_days": 63},
        "catalysts": list(decision.get("catalysts") or parent.get("catalysts") or []),
        "invalidation_conditions": list(
            decision.get("invalidation_conditions")
            or parent.get("invalidation_conditions")
            or decision.get("key_risks")
            or []
        ),
        "thesis_id": str(decision.get("thesis_id") or parent["thesis_id"]),
        "parent_signal_ids": [item["signal_id"] for item in parents],
        "created_by": "gemini-decision-host-linked",
        "model_id": model_id,
        "created_at": _created_at(created_at),
    }
    event["signal_id"] = signal_event_id(event)
    return event


def enrich_decision_provenance(
    analysis: AnalysisDecisionV2,
    source_events: list[dict[str, Any]],
    *,
    created_at: str,
    model_id: str,
) -> tuple[AnalysisDecisionV2, list[dict[str, Any]]]:
    """Attach immutable origins; unmatched decisions remain legacy_unvalidated."""
    payload = deepcopy(analysis.to_dict())
    events = deepcopy(source_events)
    event_by_id = {item["signal_id"]: item for item in events}
    for decision in payload["portfolio_decisions"]:
        requested_link_ids = list(dict.fromkeys(
            str(value).strip()
            for value in decision.get("linked_signal_ids", []) or []
            if str(value).strip()
        ))
        parents = _linked_events(decision, event_by_id)
        decision["linked_signal_ids"] = [item["signal_id"] for item in parents]
        accepted_link_ids = set(decision["linked_signal_ids"])
        decision["rejected_linked_signal_ids"] = [
            signal_id
            for signal_id in requested_link_ids
            if signal_id not in accepted_link_ids
        ]
        investable_parents = [
            item
            for item in parents
            if item["signal_type"] in {"MER_DIRECT", "MER_THESIS"}
            and _direction_matches_action(decision, item)
        ]
        direct_same_entity = [
            item
            for item in investable_parents
            if _identity_matches(decision, item)
        ]
        if direct_same_entity:
            decision.update({
                "provenance_status": "verified",
                "origin_signal_type": direct_same_entity[0]["signal_type"],
                "origin_signal_ids": [item["signal_id"] for item in direct_same_entity],
                "thesis_id": direct_same_entity[0]["thesis_id"],
            })
        if (
            not direct_same_entity
            and investable_parents
            and decision.get("decision_actor") == "AI"
            and decision.get("asset_type") == "etf"
            and decision.get("source_scope") == "sector_only"
            and str(decision.get("investment_rationale") or "").strip()
        ):
            inferred = _ai_inference_event(
                decision,
                investable_parents,
                created_at=created_at,
                model_id=model_id,
            )
            if inferred["signal_id"] not in event_by_id:
                events.append(inferred)
                event_by_id[inferred["signal_id"]] = inferred
            decision.update({
                "provenance_status": "verified",
                "origin_signal_type": "AI_INFERRED",
                "origin_signal_ids": [inferred["signal_id"]],
                "thesis_id": inferred["thesis_id"],
            })

    for item in payload["watchlist"]:
        parents = _linked_events(item, event_by_id)
        item["linked_signal_ids"] = [event["signal_id"] for event in parents]
        direct = [
            event for event in parents
            if event["signal_type"] in {"MER_DIRECT", "MER_THESIS"}
            and _identity_matches(item, event)
        ]
        if direct:
            item.update({
                "provenance_status": "verified",
                "origin_signal_type": direct[0]["signal_type"],
                "origin_signal_ids": [event["signal_id"] for event in direct],
                "thesis_id": direct[0]["thesis_id"],
            })
    return parse_analysis_decision(payload), events
