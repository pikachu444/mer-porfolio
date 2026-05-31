"""
portfolio_state.py
포트폴리오 상태(portfolio_state.json) 관리 모듈

역할:
  - 실행 간 포트폴리오 연속성 유지
  - 매도 시그널(Avoid / 주의종목) 감지 및 상태 갱신
  - Gemini 프롬프트에 삽입할 현재 보유 종목 텍스트 생성

파일 위치: output/portfolio_state.json
"""

import json
import re
from pathlib import Path
from typing import Optional


# ─── 로드 / 저장 ──────────────────────────────────────────────────────────────

def load_state(state_path: Path) -> Optional[dict]:
    """
    portfolio_state.json 로드.
    파일이 없거나 파싱 오류면 None 반환 (최초 실행 처리용).
    """
    if not state_path.exists():
        return None
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
        # 기본 필드 보정
        state.setdefault("schema_version", "1.0")
        state.setdefault("holdings", [])
        state.setdefault("rebalance_count", 0)
        return state
    except Exception as e:
        print(f"  ⚠ portfolio_state.json 로드 실패: {e} — 초기화 진행")
        return None


def save_state(state: dict, state_path: Path) -> None:
    """portfolio_state.json 저장 (들여쓰기 2칸, 한국어 유지)."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    print(f"  💾 portfolio_state 저장: {state_path}")


# ─── 조회 ─────────────────────────────────────────────────────────────────────

def get_active_holdings(state: dict) -> list[dict]:
    """status == 'active'인 종목만 반환."""
    return [h for h in state.get("holdings", []) if h.get("status") == "active"]


def format_holdings_for_prompt(state: Optional[dict]) -> str:
    """
    Gemini 프롬프트에 삽입할 현재 포트폴리오 텍스트.
    state가 None이거나 active 종목이 없으면 빈 문자열 반환.
    """
    if not state:
        return ""
    active = get_active_holdings(state)
    if not active:
        return ""

    lines = ["현재 보유 포트폴리오 (portfolio_state에서):"]
    for h in active:
        market = h.get("market", "")
        name = h.get("name", "")
        code = h.get("code", "")
        action = h.get("action", "")
        weight = h.get("weight", "")
        thesis = h.get("thesis", "")
        # thesis가 길면 80자로 자름
        if len(thesis) > 80:
            thesis = thesis[:77] + "..."
        lines.append(
            f"- {name} ({code}, {market}): {action} {weight} | thesis: {thesis}"
        )
    return "\n".join(lines)


# ─── 매도 시그널 감지 ─────────────────────────────────────────────────────────

# 매도/회피 판단 키워드 (포트폴리오 테이블의 판단 컬럼)
_SELL_ACTIONS = {"Avoid", "avoid", "매도", "전량매도", "즉시매도", "Sell", "sell", "회피"}
_BUY_ACTIONS = {"Buy", "buy", "매수", "비중확대", "추가매수"}
_HOLD_ACTIONS = {"Hold", "hold", "보유", "관망"}


def _holding_key(item: dict) -> str:
    market = (item.get("market") or item.get("type") or "").upper()
    code = re.sub(r"[^A-Za-z0-9.\-]", "", item.get("code", "") or item.get("ticker", "") or "")
    if market and code:
        return f"{market}:{code.upper()}"
    return f"{market}:NAME:{_normalize_name(item.get('name', '')).lower()}"


def _parse_weight(weight: str) -> float:
    match = re.search(r"-?\d+(?:\.\d+)?", weight or "")
    return float(match.group(0)) if match else 0.0


def _action_score(action: str) -> int:
    action = (action or "").strip()
    if action in _SELL_ACTIONS:
        return 0
    if action in _HOLD_ACTIONS:
        return 1
    if action in _BUY_ACTIONS:
        return 2
    return 1


def _change_type(old: dict, new: dict) -> str:
    old_score = _action_score(old.get("action", ""))
    new_score = _action_score(new.get("action", ""))
    old_weight = _parse_weight(old.get("weight", ""))
    new_weight = _parse_weight(new.get("weight", ""))
    if new_score > old_score:
        return "판단 강화"
    if new_score < old_score:
        return "판단 약화"
    if new_weight > old_weight:
        return "비중 증가"
    if new_weight < old_weight:
        return "비중 감소"
    return "유지 확인"


def _change_row(holding: dict, change_type: str, reason: str = "") -> dict:
    return {
        "name": holding.get("name", ""),
        "code": holding.get("code", ""),
        "market": holding.get("market", ""),
        "action": holding.get("action", ""),
        "weight": holding.get("weight", ""),
        "basis_type": holding.get("basis_type", ""),
        "thesis": holding.get("thesis", ""),
        "change_type": change_type,
        "reason": reason,
    }


def detect_sell_signals(report_text: str) -> set[str]:
    """
    리포트에서 매도/회피 대상 종목명 집합 반환.

    감지 방법:
    1. 포트폴리오 테이블(KR/US) 에서 판단 컬럼이 _SELL_ACTIONS 인 종목명 추출
    2. "주의종목" / "🚨 주의" / "🚨" 헤더 이후 테이블의 첫 번째 컬럼 추출
    """
    sell_set: set[str] = set()

    # ── 방법 1: 포트폴리오 테이블에서 판단 컬럼 확인 ─────────────────────────
    for item in _parse_portfolio_from_report(report_text):
        if item.get("action") in _SELL_ACTIONS:
            sell_set.add(_normalize_name(item.get("name", "")))

    # ── 방법 2: 주의종목 섹션 ────────────────────────────────────────────────
    # 섹션 헤더 이후 테이블 파싱
    # 지원 패턴: "주의종목", "🚨 주의", "주의 종목", "리스크 종목"
    warning_section = re.search(
        r"(?:주의종목|🚨\s*주의|주의\s*종목|리스크\s*종목)[^\n]*\n"
        r"(?:\|[^\n]+\|\n)?"   # 헤더행 (선택)
        r"(?:\|[-| :]+\|\n)?"  # 구분선 (선택)
        r"((?:\|[^\n]+\|\n?)+)",
        report_text,
    )
    if warning_section:
        for row in warning_section.group(1).strip().split("\n"):
            cells = [c.strip() for c in row.split("|")]
            cells = [c for c in cells if c]  # 빈 셀 제거
            if cells and not cells[0].startswith("-"):
                sell_set.add(_normalize_name(cells[0]))

    # ── 방법 3: 불릿 형식 주의종목 (- 종목명: 이유) ─────────────────────────
    # 예: "- JR글로벌리츠: 법정관리 신청"
    bullet_pattern = re.compile(
        r"(?:주의종목|🚨)[^\n]*\n((?:\s*[-*]\s*[^\n]+\n?)+)",
    )
    for section_match in bullet_pattern.finditer(report_text):
        for line in section_match.group(1).split("\n"):
            line = line.strip().lstrip("-*").strip()
            if ":" in line:
                name_part = line.split(":")[0].strip()
                if name_part:
                    sell_set.add(_normalize_name(name_part))

    if sell_set:
        print(f"  🚨 매도 시그널 감지: {sell_set}")

    return sell_set


def _normalize_name(name: str) -> str:
    """종목명 정규화: 앞뒤 공백, 마크다운 강조(**, *) 제거."""
    name = name.strip()
    name = re.sub(r"\*+", "", name)   # ** or * 제거
    name = name.strip()
    return name


# ─── 상태 업데이트 ────────────────────────────────────────────────────────────

def update_state_from_report(
    state: dict,
    report_text: str,
    today_str: str,          # "2026-05-12" 형식
    parsed_portfolio: Optional[list[dict]] = None,
    replace_active: bool = False,
) -> dict:
    """
    새 리포트를 바탕으로 portfolio_state 업데이트.

    흐름:
    1. 매도 시그널 감지
    2. 새 포트폴리오 파싱 (parsed_portfolio 없으면 리포트에서 직접 파싱)
    3. 기존 active 종목 업데이트 / 매도 처리
    4. 신규 종목 추가
    5. 메타데이터 갱신
    """
    sell_set = detect_sell_signals(report_text)

    # 새 포트폴리오가 외부에서 파싱 안 됐으면 직접 파싱
    if parsed_portfolio is None:
        parsed_portfolio = _parse_portfolio_from_report(report_text)

    existing_holdings = state.get("holdings", [])
    active_by_key = {
        _holding_key(h): h
        for h in existing_holdings
        if h.get("status") == "active"
    }
    updated_keys: set[str] = set()
    changes = {"date": today_str, "buys": [], "changes": [], "sells": []}

    # ── 기존 종목 처리 ──────────────────────────────────────────────────────
    for key, holding in list(active_by_key.items()):
        norm_name = _normalize_name(holding.get("name", ""))

        # 매도 시그널 감지됨
        if norm_name in sell_set:
            holding["status"] = "removed"
            holding["removed_date"] = today_str
            reason = "매도 시그널 감지 (Avoid 판단 또는 주의종목 섹션)"
            holding["removed_reason"] = reason
            print(f"  ❌ 종목 제거: {holding['name']} — {reason}")
            changes["sells"].append(_change_row(holding, "매도/제외", reason))
            updated_keys.add(key)
            continue

        # 새 포트폴리오에서 매칭 찾기
        matched = _find_matching_holding(holding, parsed_portfolio)
        if matched:
            old = dict(holding)
            # action/weight/thesis 업데이트
            holding["name"] = matched.get("name", holding.get("name", ""))
            holding["code"] = matched.get("code", holding.get("code", ""))
            holding["market"] = matched.get("market", holding.get("market", ""))
            if matched.get("action"):
                holding["action"] = matched["action"]
            if matched.get("weight"):
                holding["weight"] = matched["weight"]
            if matched.get("basis_type"):
                holding["basis_type"] = matched["basis_type"]
            if matched.get("thesis"):
                holding["thesis"] = matched["thesis"]
            holding["last_confirmed_date"] = today_str
            change_type = _change_type(old, holding)
            if change_type != "유지 확인":
                changes["changes"].append(_change_row(holding, change_type))
            updated_keys.add(key)
        else:
            # 미언급은 매도 신호가 아니다. 현재 포트폴리오 표에는 기존보유로 유지한다.
            holding["basis_type"] = holding.get("basis_type") or "기존보유"
            if not holding.get("thesis"):
                holding["thesis"] = "신규 직접 언급 없음, 기존 보유 유지"

    # ── 신규 종목 추가 ──────────────────────────────────────────────────────
    for new_h in parsed_portfolio:
        key = _holding_key(new_h)
        norm_new = _normalize_name(new_h.get("name", ""))
        if key in updated_keys or norm_new in sell_set:
            continue  # 이미 처리됨 or 매도 대상

        # 기존에 없는 종목인지 확인
        exists = key in {
            _holding_key(h)
            for h in existing_holdings
            if h.get("status") == "active"
        }
        if not exists:
            new_holding = {
                "name": new_h.get("name", ""),
                "code": new_h.get("code", ""),
                "market": new_h.get("market", ""),
                "action": new_h.get("action", ""),
                "weight": new_h.get("weight", ""),
                "basis_type": new_h.get("basis_type", ""),
                "thesis": new_h.get("thesis", ""),
                "entry_date": today_str,
                "last_confirmed_date": today_str,
                "status": "active",
            }
            existing_holdings.append(new_holding)
            changes["buys"].append(_change_row(new_holding, "신규 편입"))
            print(f"  ✅ 신규 종목 추가: {new_h.get('name')}")

    state["holdings"] = existing_holdings
    state["last_updated"] = today_str
    state["last_report_date"] = today_str
    state["last_changes"] = changes

    return state


def _find_matching_holding(holding: dict, portfolio: list[dict]) -> Optional[dict]:
    """포트폴리오 목록에서 코드/티커 우선, 종목명 보조로 일치하는 항목 반환."""
    key = _holding_key(holding)
    for h in portfolio:
        if _holding_key(h) == key:
            return h
    norm = _normalize_name(holding.get("name", ""))
    for h in portfolio:
        if _normalize_name(h.get("name", "")) == norm:
            return h
    return None


def _parse_portfolio_from_report(report_text: str) -> list[dict]:
    """
    리포트 마크다운 테이블에서 포트폴리오 파싱.
    track_returns.py의 parse_portfolio_from_report와 유사하지만
    thesis(핵심 근거) 컬럼도 추출.

    KR 테이블: | 종목명 | 코드 | 판단 | 목표비중 | 핵심 근거 |
    KR 테이블: | 종목명 | 코드 | 판단 | 목표비중 | 근거유형 | 핵심 근거 |
    US 테이블: | 종목명 | 티커 | 판단 | 목표비중 | 핵심 근거 |
    US 테이블: | 종목명 | 티커 | 판단 | 목표비중 | 근거유형 | 핵심 근거 |
    """
    result = []

    # ── KR 파싱 ──────────────────────────────────────────────────────────────
    kr_match = re.search(
        r"(?:🇰🇷|국내주식)[^\n]*\n\n?\|[^\n]+\|\n\|[-| :]+\|\n((?:\|[^\n]+\|\n?)+)",
        report_text,
    )
    if kr_match:
        for row in kr_match.group(1).strip().split("\n"):
            cells = [c.strip() for c in row.split("|")[1:-1]]
            if len(cells) >= 3 and cells[0] and not cells[0].startswith("-"):
                name = _normalize_name(cells[0])
                code = cells[1].strip() if len(cells) > 1 else ""
                action = cells[2].strip() if len(cells) > 2 else ""
                weight = cells[3].strip() if len(cells) > 3 else ""
                basis_type = cells[4].strip() if len(cells) > 5 else ""
                thesis = cells[5].strip() if len(cells) > 5 else (cells[4].strip() if len(cells) > 4 else "")
                if name and not name.lower() in ("종목명", "name", "추천 없음"):
                    result.append({
                        "name": name, "code": code, "market": "KR",
                        "action": action, "weight": weight, "basis_type": basis_type, "thesis": thesis,
                    })

    # ── US 파싱 ──────────────────────────────────────────────────────────────
    us_match = re.search(
        r"(?:🇺🇸|해외주식)[^\n]*\n\n?\|[^\n]+\|\n\|[-| :]+\|\n((?:\|[^\n]+\|\n?)+)",
        report_text,
    )
    if us_match:
        for row in us_match.group(1).strip().split("\n"):
            cells = [c.strip() for c in row.split("|")[1:-1]]
            if len(cells) >= 3 and cells[0] and not cells[0].startswith("-"):
                name = _normalize_name(cells[0])
                code = cells[1].strip() if len(cells) > 1 else ""
                action = cells[2].strip() if len(cells) > 2 else ""
                weight = cells[3].strip() if len(cells) > 3 else ""
                basis_type = cells[4].strip() if len(cells) > 5 else ""
                thesis = cells[5].strip() if len(cells) > 5 else (cells[4].strip() if len(cells) > 4 else "")
                if name and not name.lower() in ("종목명", "name", "추천 없음"):
                    result.append({
                        "name": name, "code": code, "market": "US",
                        "action": action, "weight": weight, "basis_type": basis_type, "thesis": thesis,
                    })

    return result


def sync_report_with_state(report_text: str, state: dict) -> str:
    """현재 active holdings를 리포트의 KR/US 추천 표에 반영한다."""
    active = get_active_holdings(state)
    by_market = {"KR": [], "US": []}
    for holding in active:
        market = (holding.get("market") or "").upper()
        if market in by_market:
            by_market[market].append(holding)

    lines = report_text.splitlines()
    replacements: list[tuple[int, int, list[str]]] = []
    for market, start, end in _iter_stock_tables(report_text):
        rows = by_market.get(market, [])
        if market == "KR":
            header = ["| 종목명 | 코드 | 판단 | 목표비중 | 근거유형 | 핵심 근거 |"]
        else:
            header = ["| 종목명 | 티커 | 판단 | 목표비중 | 근거유형 | 핵심 근거 |"]
        table = header + ["|--------|------|------|----------|----------|-----------|"]
        for h in rows:
            table.append(
                "| {name} | {code} | {action} | {weight} | {basis} | {thesis} |".format(
                    name=h.get("name", ""),
                    code=h.get("code", ""),
                    action=h.get("action", ""),
                    weight=h.get("weight", ""),
                    basis=h.get("basis_type", "") or "기존보유",
                    thesis=h.get("thesis", "") or "신규 직접 언급 없음, 기존 보유 유지",
                )
            )
        if not rows:
            table.append("| 추천 없음 | - | 관망 | 0% | 기존보유 | active 보유 종목 없음 |")
        replacements.append((start, end, table))

    for start, end, table in reversed(replacements):
        lines[start:end] = table
    return "\n".join(lines)


def _iter_stock_tables(report_text: str):
    lines = report_text.splitlines()
    current_market = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#"):
            if "국내주식" in line or "🇰🇷" in line:
                current_market = "KR"
            elif "해외주식" in line or "미국" in line or "🇺🇸" in line:
                current_market = "US"
            elif "ETF" in line or "대안자산" in line:
                current_market = None
        if (
            current_market
            and line.strip().startswith("|")
            and i + 1 < len(lines)
            and _is_separator(lines[i + 1])
        ):
            start = i
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                i += 1
            yield current_market, start, i
            current_market = None
            continue
        i += 1


def _is_separator(row: str) -> bool:
    cells = [c.strip() for c in row.strip().split("|")[1:-1]]
    return bool(cells) and all(re.fullmatch(r"[-: ]+", c) for c in cells)


# ─── 초기 상태 생성 ───────────────────────────────────────────────────────────

def create_initial_state(
    report_text: str,
    today_str: str,
    parsed_portfolio: Optional[list[dict]] = None,
) -> dict:
    """
    portfolio_state.json이 없을 때 최초 상태 생성.
    첫 리포트에서 포트폴리오를 파싱해 초기 holdings 구성.
    """
    portfolio = parsed_portfolio if parsed_portfolio is not None else _parse_portfolio_from_report(report_text)
    sell_set = detect_sell_signals(report_text)

    holdings = []
    changes = {"date": today_str, "buys": [], "changes": [], "sells": []}
    for h in portfolio:
        norm_name = _normalize_name(h.get("name", ""))
        status = "removed" if norm_name in sell_set else "active"
        holding = {
            "name": h.get("name", ""),
            "code": h.get("code", ""),
            "market": h.get("market", ""),
            "action": h.get("action", ""),
            "weight": h.get("weight", ""),
            "basis_type": h.get("basis_type", ""),
            "thesis": h.get("thesis", ""),
            "entry_date": today_str,
            "last_confirmed_date": today_str,
            "status": status,
        }
        if status == "removed":
            holding["removed_date"] = today_str
            holding["removed_reason"] = "초기 생성 시 매도 시그널 감지"
            changes["sells"].append(_change_row(holding, "매도/제외", holding["removed_reason"]))
        else:
            changes["buys"].append(_change_row(holding, "신규 편입"))
        holdings.append(holding)
        print(f"  {'✅' if status == 'active' else '❌'} [{status}] {h.get('name')}")

    return {
        "schema_version": "1.0",
        "last_updated": today_str,
        "last_report_date": today_str,
        "rebalance_count": 1,
        "last_changes": changes,
        "holdings": holdings,
    }


# ─── 직접 실행 테스트 ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    sample_report = """
## 📊 포트폴리오 추천

### 🇰🇷 국내주식 (한국)

| 종목명 | 코드 | 판단 | 목표비중 | 핵심 근거 |
|--------|------|------|----------|-----------|
| 한국조선해양 | 009540 | 매수 | 15% | 조선 슈퍼사이클 |
| 삼성전자 | 005930 | 보유 | 20% | AI 반도체 수혜 |
| JR글로벌리츠 | 348950 | Avoid | 0% | 법정관리 신청 |

### 🇺🇸 해외주식 (미국)

| 종목명 | 티커 | 판단 | 목표비중 | 핵심 근거 |
|--------|------|------|----------|-----------|
| Nvidia | NVDA | Buy | 25% | AI 인프라 핵심 |

## 🚨 주의종목 (리스크)
| 종목명 | 사유 |
|--------|------|
| 파두 | 경영진 기소 |
"""
    print("=== detect_sell_signals 테스트 ===")
    signals = detect_sell_signals(sample_report)
    print(f"감지된 매도 시그널: {signals}")

    print("\n=== create_initial_state 테스트 ===")
    state = create_initial_state(sample_report, "2026-05-12")
    print(json.dumps(state, ensure_ascii=False, indent=2))

    print("\n=== format_holdings_for_prompt 테스트 ===")
    print(format_holdings_for_prompt(state))
