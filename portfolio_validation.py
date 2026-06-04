"""
portfolio_validation.py
추천 포트폴리오 파싱과 근거 검증 후처리 모듈.

Gemini가 긍정 섹터만 보고 블로그에 직접 나오지 않은 신규 종목을
창작하는 것을 막기 위해, 최종 리포트 생성 후 API 재호출 없이
마크다운 표를 코드로 검증한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from portfolio_schema import AnalysisDecisionV2, parse_analysis_decision


@dataclass
class PortfolioItem:
    name: str
    code: str
    market: str
    action: str
    weight: str
    basis_type: str = ""
    thesis: str = ""

    def normalized_name(self) -> str:
        return normalize_name(self.name)


@dataclass
class ValidationResult:
    report_text: str
    parsed_portfolio: list[dict]
    removed_items: list[dict]
    moved_cash_weight: float


def validate_structured_decisions(payload: dict) -> AnalysisDecisionV2:
    """
    Validate the new structured Gemini result.

    Investment meaning is decided by the LLM. This function only applies the
    structural rules agreed for the v2 path.
    """
    return parse_analysis_decision(payload)


BUY_HINTS = (
    "매수",
    "샀",
    "사들",
    "편입",
    "보유",
    "비중 확대",
    "담았",
)
SELL_HINTS = (
    "매도",
    "팔",
    "정리",
    "비중 축소",
    "줄였",
    "회피",
    "리스크",
)
STOCK_MARKETS = {"KR", "US"}
ALLOWED_BASIS_TYPES = {"직접언급", "직접매수언급", "직접매도언급", "기존보유"}


def normalize_name(name: str) -> str:
    name = re.sub(r"\*+", "", name or "")
    return name.strip()


def _cell_text(row: str) -> list[str]:
    return [c.strip() for c in row.strip().split("|")[1:-1]]


def _is_separator(row: str) -> bool:
    cells = _cell_text(row)
    return bool(cells) and all(re.fullmatch(r"[-: ]+", c) for c in cells)


def _market_from_header(header_line: str) -> Optional[str]:
    if "국내주식" in header_line or "🇰🇷" in header_line:
        return "KR"
    if "해외주식" in header_line or "미국" in header_line or "🇺🇸" in header_line:
        return "US"
    if "ETF" in header_line or "대안자산" in header_line:
        return "ETF"
    return None


def _parse_item_from_cells(cells: list[str], market: str) -> Optional[PortfolioItem]:
    if len(cells) < 4:
        return None

    name = normalize_name(cells[0])
    if not name or name.lower() in {"종목명", "name", "이름"} or name.startswith("-"):
        return None

    basis_type = ""
    thesis = cells[4].strip() if len(cells) > 4 else ""
    if len(cells) >= 6:
        basis_type = cells[4].strip()
        thesis = cells[5].strip()

    return PortfolioItem(
        name=name,
        code=cells[1].strip(),
        market=market,
        action=cells[2].strip(),
        weight=cells[3].strip(),
        basis_type=basis_type,
        thesis=thesis,
    )


def iter_portfolio_tables(report_text: str) -> Iterable[tuple[str, int, int, list[str], list[str]]]:
    """추천 관련 마크다운 표를 찾아 (market, start, end, header, rows)로 반환한다."""
    lines = report_text.splitlines()
    current_market: Optional[str] = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#"):
            maybe_market = _market_from_header(line)
            if maybe_market:
                current_market = maybe_market
        if (
            current_market
            and line.strip().startswith("|")
            and i + 1 < len(lines)
            and _is_separator(lines[i + 1])
        ):
            start = i
            header = _cell_text(lines[i])
            i += 2
            rows: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i])
                i += 1
            yield current_market, start, i, header, rows
            current_market = None
            continue
        i += 1


def parse_portfolio_items(report_text: str, include_etf: bool = False) -> list[dict]:
    items: list[dict] = []
    for market, _start, _end, _header, rows in iter_portfolio_tables(report_text):
        if market == "ETF" and not include_etf:
            continue
        for row in rows:
            item = _parse_item_from_cells(_cell_text(row), market)
            if not item:
                continue
            items.append(
                {
                    "name": item.name,
                    "code": item.code,
                    "market": item.market,
                    "action": item.action,
                    "weight": item.weight,
                    "basis_type": item.basis_type,
                    "thesis": item.thesis,
                }
            )
    return items


def _parse_weight(weight: str) -> float:
    match = re.search(r"-?\d+(?:\.\d+)?", weight or "")
    return float(match.group(0)) if match else 0.0


def _format_weight(value: float) -> str:
    if abs(value - round(value)) < 0.05:
        return f"{round(value):.0f}%"
    return f"{value:.1f}%"


def _source_text(posts: list[dict]) -> str:
    parts: list[str] = []
    for post in posts:
        parts.append(str(post.get("title", "")))
        parts.append(str(post.get("summary", "")))
        parts.append(str(post.get("content", "")))
    return "\n".join(parts)


def _contains_item(source: str, item: PortfolioItem) -> bool:
    if item.name and item.name.lower() in source.lower():
        return True

    code = re.sub(r"[^A-Za-z0-9.\-]", "", item.code or "")
    if code and code != "-":
        if re.fullmatch(r"\d{6}", code):
            return re.search(rf"(?<!\d){re.escape(code)}(?!\d)", source) is not None
        if len(code) >= 2:
            return re.search(rf"(?<![A-Za-z0-9]){re.escape(code)}(?![A-Za-z0-9])", source, re.IGNORECASE) is not None

    digits = re.sub(r"[^0-9]", "", item.code or "")
    if len(digits) == 6:
        return re.search(rf"(?<!\d){re.escape(digits)}(?!\d)", source) is not None
    return False


def _context_around_item(source: str, item: PortfolioItem, radius: int = 90) -> str:
    lower_source = source.lower()
    candidates = [item.name, item.code]
    for candidate in candidates:
        candidate = (candidate or "").strip()
        if not candidate or candidate == "-":
            continue
        pos = lower_source.find(candidate.lower())
        if pos >= 0:
            return source[max(0, pos - radius):pos + len(candidate) + radius]
    return ""


def _is_existing_holding(item: PortfolioItem, state: Optional[dict]) -> bool:
    if not state:
        return False
    item_name = normalize_name(item.name).lower()
    item_code = re.sub(r"[^A-Za-z0-9.\-]", "", item.code or "").lower()
    for holding in state.get("holdings", []):
        if holding.get("status") != "active":
            continue
        holding_name = normalize_name(holding.get("name", "")).lower()
        holding_code = re.sub(r"[^A-Za-z0-9.\-]", "", holding.get("code", "") or "").lower()
        if item_name and item_name == holding_name:
            return True
        if item_code and holding_code and item_code == holding_code:
            return True
    return False


def _basis_for_item(item: PortfolioItem, source: str, state: Optional[dict]) -> tuple[bool, str, str]:
    existing = _is_existing_holding(item, state)
    directly_mentioned = _contains_item(source, item)
    if not existing and not directly_mentioned:
        return False, "", "블로그 직접 언급이나 기존 보유 근거가 없음"

    context = _context_around_item(source, item)
    if any(hint in context for hint in SELL_HINTS):
        return True, "직접매도언급", ""
    if any(hint in context for hint in BUY_HINTS):
        return True, "직접매수언급", ""
    if directly_mentioned:
        return True, "직접언급", ""
    return True, "기존보유", ""


def _cash_like_name(name: str) -> bool:
    lowered = name.lower()
    return any(token in lowered for token in ("현금", "대기자금", "cash", "mmf", "달러", "usd"))


def _rewrite_stock_table(rows: list[str], market: str, source: str, state: Optional[dict]) -> tuple[list[str], list[dict], float]:
    kept_rows: list[str] = []
    removed: list[dict] = []
    moved_weight = 0.0

    for row in rows:
        item = _parse_item_from_cells(_cell_text(row), market)
        if not item:
            continue

        allowed, basis_type, reason = _basis_for_item(item, source, state)
        if not allowed:
            weight = _parse_weight(item.weight)
            moved_weight += weight
            removed.append(
                {
                    "name": item.name,
                    "code": item.code,
                    "market": item.market,
                    "action": item.action,
                    "weight": item.weight,
                    "reason": reason,
                }
            )
            continue

        item.basis_type = basis_type
        kept_rows.append(
            f"| {item.name} | {item.code} | {item.action} | {item.weight} | {item.basis_type} | {item.thesis} |"
        )

    if not kept_rows:
        kept_rows.append("| 추천 없음 | - | 관망 | 0% | 직접언급 | 검증된 직접 언급/기존 보유 종목 없음 |")

    return kept_rows, removed, moved_weight


def _rewrite_etf_table(rows: list[str], moved_cash_weight: float) -> list[str]:
    rewritten: list[str] = []
    cash_added = False

    for row in rows:
        item = _parse_item_from_cells(_cell_text(row), "ETF")
        if not item:
            continue
        if _cash_like_name(item.name):
            item.weight = _format_weight(_parse_weight(item.weight) + moved_cash_weight)
            cash_added = True
        rewritten.append(f"| {item.name} | {item.code} | {item.action} | {item.weight} | {item.thesis} |")

    if moved_cash_weight > 0 and not cash_added:
        rewritten.append(f"| 현금/대기자금 | CASH | 보유 | {_format_weight(moved_cash_weight)} | 검증 제외 종목 비중 대기 |")

    if not rewritten:
        rewritten.append("| 현금/대기자금 | CASH | 보유 | 0% | 불확실성 대응 |")

    return rewritten


def _insert_or_update_cash_table(report_text: str, moved_cash_weight: float) -> str:
    if moved_cash_weight <= 0:
        return report_text

    lines = report_text.splitlines()
    for market, start, end, _header, rows in iter_portfolio_tables(report_text):
        if market != "ETF":
            continue
        new_table = [
            "| 이름 | 코드/티커 | 판단 | 목표비중 | 용도 |",
            "|------|----------|------|----------|------|",
            *_rewrite_etf_table(rows, moved_cash_weight),
        ]
        return "\n".join(lines[:start] + new_table + lines[end:])

    cash_section = (
        "\n\n### 📦 ETF / 대안자산\n\n"
        "| 이름 | 코드/티커 | 판단 | 목표비중 | 용도 |\n"
        "|------|----------|------|----------|------|\n"
        f"| 현금/대기자금 | CASH | 보유 | {_format_weight(moved_cash_weight)} | 검증 제외 종목 비중 대기 |\n"
    )
    marker = "\n## 💬 한 줄 코멘트"
    if marker in report_text:
        return report_text.replace(marker, cash_section + marker, 1)
    return report_text.rstrip() + cash_section


def _verification_section(removed_items: list[dict], moved_cash_weight: float) -> str:
    lines = [
        "## ✅ 추천 검증 결과",
        "",
        "- 검증 기준: 신규 종목은 블로그 직접 언급 또는 기존 보유 종목일 때만 유지했습니다.",
        "- 긍정 섹터만으로 생성된 신규 Buy 종목은 허용하지 않았습니다.",
        "- API 재호출: 없음. 코드 후처리로만 검증했습니다.",
    ]
    if removed_items:
        lines.append(f"- 제외 비중: {_format_weight(moved_cash_weight)}를 현금/대기자금으로 이동했습니다.")
        lines.append("")
        lines.append("| 제외 종목 | 시장 | 코드/티커 | 판단 | 목표비중 | 제외 사유 |")
        lines.append("|----------|------|----------|------|----------|-----------|")
        for item in removed_items:
            lines.append(
                f"| {item['name']} | {item['market']} | {item['code']} | {item['action']} | {item['weight']} | {item['reason']} |"
            )
    else:
        lines.append("- 제외 종목: 없음")
    return "\n".join(lines)


def _replace_verification_section(report_text: str, section: str) -> str:
    pattern = re.compile(r"\n---\n\n## ✅ 추천 검증 결과\n.*?(?=\n---\n\n## |\Z)", re.DOTALL)
    report_text = pattern.sub("", report_text)
    marker = "\n---\n\n## 💬 한 줄 코멘트"
    insert = "\n---\n\n" + section + "\n"
    if marker in report_text:
        return report_text.replace(marker, insert + marker, 1)
    return report_text.rstrip() + insert


def validate_recommendations(report_text: str, posts: list[dict], state: Optional[dict]) -> ValidationResult:
    """
    리포트 추천 표를 검증한다.

    신규 KR/US 종목은 블로그 원문/요약 직접 언급 또는 기존 active 보유 종목일 때만 유지한다.
    제거된 비중은 ETF/대안자산의 현금/대기자금으로 이동한다.
    """
    source = _source_text(posts)
    lines = report_text.splitlines()
    replacements: list[tuple[int, int, list[str]]] = []
    all_removed: list[dict] = []
    moved_cash_weight = 0.0

    for market, start, end, _header, rows in iter_portfolio_tables(report_text):
        if market not in STOCK_MARKETS:
            continue
        new_rows, removed, moved_weight = _rewrite_stock_table(rows, market, source, state)
        if market == "KR":
            header = ["| 종목명 | 코드 | 판단 | 목표비중 | 근거유형 | 핵심 근거 |"]
        else:
            header = ["| 종목명 | 티커 | 판단 | 목표비중 | 근거유형 | 핵심 근거 |"]
        new_table = header + ["|--------|------|------|----------|----------|-----------|"] + new_rows
        replacements.append((start, end, new_table))
        all_removed.extend(removed)
        moved_cash_weight += moved_weight

    for start, end, new_table in reversed(replacements):
        lines[start:end] = new_table

    validated = "\n".join(lines)
    validated = _insert_or_update_cash_table(validated, moved_cash_weight)
    validated = _replace_verification_section(validated, _verification_section(all_removed, moved_cash_weight))
    parsed = parse_portfolio_items(validated, include_etf=False)
    parsed = [p for p in parsed if p.get("name") != "추천 없음"]

    if all_removed:
        print(f"  추천 검증: 근거 없는 신규 종목 {len(all_removed)}개 제외, {_format_weight(moved_cash_weight)} 현금 이동")
    else:
        print("  추천 검증: 제외 종목 없음")

    return ValidationResult(
        report_text=validated,
        parsed_portfolio=parsed,
        removed_items=all_removed,
        moved_cash_weight=moved_cash_weight,
    )
