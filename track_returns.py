"""
track_returns.py
모델 포트폴리오 성과 추적 모듈.

성과 추적은 날짜별 추천 이력이 아니라 현재 active 포지션 원장을 기준으로 한다.
같은 종목이 반복 추천되어도 하나의 포지션만 유지하고, 명시적으로 종료된 종목은
closed position으로 이동한다.
"""

import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False
    print("  ⚠ yfinance 미설치 — 수익률 추적 비활성화 (pip install yfinance)")

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "output"))
HISTORY_FILE = OUTPUT_DIR / "portfolio_history.json"
CACHE_FILE = OUTPUT_DIR / "performance_cache.json"
MAX_CLOSED_DISPLAY = 8


def _get_usdkrw(date_str: Optional[str] = None) -> Optional[float]:
    if not YFINANCE_AVAILABLE:
        return None
    try:
        ticker = yf.Ticker("KRW=X")
        if date_str is None:
            rate = getattr(ticker.fast_info, "last_price", None)
            return float(rate) if rate and rate > 0 else None
        target = datetime.strptime(date_str, "%Y-%m-%d")
        start = (target - timedelta(days=5)).strftime("%Y-%m-%d")
        end = (target + timedelta(days=5)).strftime("%Y-%m-%d")
        hist = ticker.history(start=start, end=end)
        if hist.empty:
            return None
        hist.index = hist.index.tz_localize(None) if hist.index.tzinfo else hist.index
        closest = min(hist.index, key=lambda d: abs((d.to_pydatetime() - target).days))
        return float(hist.loc[closest, "Close"])
    except Exception:
        return None


def _normalize_code(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9.\-]", "", value or "")


def _position_key(item: dict) -> str:
    market = (item.get("market") or item.get("type") or "").upper()
    code = _normalize_code(item.get("code") or item.get("ticker") or "")
    if market == "KR" and code:
        code = re.sub(r"[^0-9]", "", code).zfill(6)
    if code:
        return f"{market}:{code.upper()}"
    return f"{market}:NAME:{(item.get('name') or '').strip().lower()}"


def _weight_value(weight: str) -> float:
    match = re.search(r"-?\d+(?:\.\d+)?", weight or "")
    return float(match.group(0)) if match else 0.0


def parse_portfolio_from_report(report_text: str) -> list[dict]:
    """마크다운 리포트에서 현재 KR/US 추천 포트폴리오를 파싱한다."""
    parsed: list[dict] = []
    for market, pattern in (
        ("KR", r"(?:🇰🇷|국내주식)[^\n]*\n\n?\|[^\n]+\|\n\|[-| :]+\|\n((?:\|[^\n]+\|\n?)+)"),
        ("US", r"(?:🇺🇸|해외주식|미국)[^\n]*\n\n?\|[^\n]+\|\n\|[-| :]+\|\n((?:\|[^\n]+\|\n?)+)"),
    ):
        match = re.search(pattern, report_text)
        if not match:
            continue
        for row in match.group(1).strip().split("\n"):
            cells = [c.strip() for c in row.split("|")[1:-1]]
            if len(cells) < 4 or not cells[0] or cells[0].startswith("-"):
                continue
            name = re.sub(r"\*+", "", cells[0]).strip()
            if name.lower() in {"종목명", "name", "추천 없음"}:
                continue
            code = cells[1].strip()
            if market == "KR":
                code = re.sub(r"[^0-9]", "", code).zfill(6)
                if not code:
                    continue
            else:
                code = _normalize_code(code).upper()
                if not code or code == "-":
                    continue
            parsed.append({
                "type": market,
                "market": market,
                "name": name,
                "code": code,
                "ticker": code,
                "action": cells[2],
                "weight": cells[3],
                "basis_type": cells[4] if len(cells) > 5 else "",
                "thesis": cells[5] if len(cells) > 5 else (cells[4] if len(cells) > 4 else ""),
            })
    return parsed


def _resolve_kr_ticker(code: str) -> Optional[str]:
    if not YFINANCE_AVAILABLE:
        return None
    for suffix in [".KS", ".KQ"]:
        ticker = code + suffix
        try:
            price = getattr(yf.Ticker(ticker).fast_info, "last_price", None)
            if price and price > 0:
                return ticker
        except Exception:
            pass
        time.sleep(0.2)
    return None


def _get_price_on_date(ticker: str, date_str: str) -> Optional[float]:
    if not YFINANCE_AVAILABLE or not ticker:
        return None
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d")
        start = (target - timedelta(days=7)).strftime("%Y-%m-%d")
        end = (target + timedelta(days=7)).strftime("%Y-%m-%d")
        hist = yf.Ticker(ticker).history(start=start, end=end)
        if hist.empty:
            return None
        hist.index = hist.index.tz_localize(None) if hist.index.tzinfo else hist.index
        closest = min(hist.index, key=lambda d: abs((d.to_pydatetime() - target).days))
        return float(hist.loc[closest, "Close"])
    except Exception:
        return None


def _get_current_price(ticker: str) -> Optional[float]:
    if not YFINANCE_AVAILABLE or not ticker:
        return None
    try:
        price = getattr(yf.Ticker(ticker).fast_info, "last_price", None)
        return float(price) if price and price > 0 else None
    except Exception:
        return None


def _load_history() -> dict:
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                return _migrate_history(json.load(f))
        except Exception:
            pass
    return {"schema_version": "2.0", "positions": [], "closed_positions": []}


def _migrate_history(history: dict) -> dict:
    if history.get("schema_version") == "2.0":
        history.setdefault("positions", [])
        history.setdefault("closed_positions", [])
        return history

    migrated = {"schema_version": "2.0", "positions": [], "closed_positions": []}
    by_key: dict[str, dict] = {}
    for rec in history.get("records", []):
        key = _position_key(rec)
        pos = by_key.get(key)
        if not pos:
            pos = {
                "key": key,
                "type": rec.get("type", ""),
                "market": rec.get("type", ""),
                "name": rec.get("name", ""),
                "code": rec.get("code", rec.get("ticker", "")),
                "ticker": rec.get("ticker", ""),
                "action": rec.get("action", ""),
                "weight": rec.get("weight", ""),
                "entry_date": rec.get("date", ""),
                "entry_price": rec.get("entry_price"),
                "entry_usdkrw": rec.get("entry_usdkrw"),
                "last_confirmed_date": rec.get("date", ""),
                "status": "active",
            }
            by_key[key] = pos
            continue
        if rec.get("date", "") < pos.get("entry_date", ""):
            pos["entry_date"] = rec.get("date", "")
            pos["entry_price"] = rec.get("entry_price")
            pos["entry_usdkrw"] = rec.get("entry_usdkrw")
        if rec.get("date", "") > pos.get("last_confirmed_date", ""):
            pos["last_confirmed_date"] = rec.get("date", "")
            pos["action"] = rec.get("action", pos.get("action", ""))
            pos["weight"] = rec.get("weight", pos.get("weight", ""))
    migrated["positions"] = list(by_key.values())
    return migrated


def _save_history(history: dict) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def _ticker_for(item: dict) -> Optional[str]:
    if item["type"] == "KR":
        return _resolve_kr_ticker(item["code"])
    return item["code"]


def _ensure_current_positions(history: dict, current_items: list[dict], today_str: str) -> None:
    positions = history.setdefault("positions", [])
    by_key = {p.get("key") or _position_key(p): p for p in positions if p.get("status", "active") == "active"}
    current_keys = {_position_key(item) for item in current_items}
    current_usdkrw = _get_usdkrw()

    for item in current_items:
        key = _position_key(item)
        pos = by_key.get(key)
        if not pos:
            ticker = _ticker_for(item)
            entry_price = _get_price_on_date(ticker, today_str) if ticker else None
            pos = {
                "key": key,
                "type": item["type"],
                "market": item["market"],
                "name": item["name"],
                "code": item["code"],
                "ticker": ticker,
                "action": item["action"],
                "weight": item["weight"],
                "basis_type": item.get("basis_type", ""),
                "thesis": item.get("thesis", ""),
                "entry_date": today_str,
                "entry_price": entry_price,
                "entry_usdkrw": current_usdkrw if item["type"] == "US" else None,
                "last_confirmed_date": today_str,
                "status": "active",
            }
            positions.append(pos)
            print(f"     신규 포지션: {item['name']} → {ticker or '-'}")
        else:
            pos.update({
                "name": item["name"],
                "code": item["code"],
                "action": item["action"],
                "weight": item["weight"],
                "basis_type": item.get("basis_type", pos.get("basis_type", "")),
                "thesis": item.get("thesis", pos.get("thesis", "")),
                "last_confirmed_date": today_str,
                "status": "active",
            })
            if not pos.get("ticker"):
                pos["ticker"] = _ticker_for(item)

    for pos in list(positions):
        if pos.get("status", "active") != "active":
            continue
        key = pos.get("key") or _position_key(pos)
        if key in current_keys:
            continue
        current_price = _get_current_price(pos.get("ticker", ""))
        pos["status"] = "closed"
        pos["closed_date"] = today_str
        pos["closed_price"] = current_price
        pos["close_reason"] = "현재 포트폴리오에서 제외"
        history.setdefault("closed_positions", []).append(dict(pos))
        print(f"     종료 포지션: {pos.get('name')} — 현재 포트폴리오에서 제외")


def calculate_active_rows(history: dict) -> list[dict]:
    current_usdkrw = _get_usdkrw()
    rows = []
    for pos in history.get("positions", []):
        if pos.get("status", "active") != "active":
            continue
        if not pos.get("entry_price") or not pos.get("ticker"):
            continue
        current_price = _get_current_price(pos["ticker"])
        if current_price is None:
            continue
        entry = pos["entry_price"]
        ret_pct = (current_price - entry) / entry * 100
        row = {
            **pos,
            "current_price": current_price,
            "return_pct": ret_pct,
        }
        if pos.get("type") == "US":
            entry_usdkrw = pos.get("entry_usdkrw")
            if entry_usdkrw and current_usdkrw:
                row["return_pct_krw"] = ((current_price * current_usdkrw) - (entry * entry_usdkrw)) / (entry * entry_usdkrw) * 100
                row["current_usdkrw"] = current_usdkrw
            else:
                row["return_pct_krw"] = ret_pct
        rows.append(row)
        time.sleep(0.1)
    return rows


def _portfolio_return(rows: list[dict]) -> float:
    weighted_sum = 0.0
    total_weight = 0.0
    for row in rows:
        ret = row.get("return_pct_krw", row.get("return_pct", 0.0))
        weight = _weight_value(row.get("weight", "")) / 100.0
        weighted_sum += ret * weight
        total_weight += weight
    if total_weight > 1.0:
        return weighted_sum / total_weight
    return weighted_sum


def _recent_closed(history: dict) -> list[dict]:
    closed = history.get("closed_positions", [])
    return sorted(closed, key=lambda x: x.get("closed_date", ""), reverse=True)[:MAX_CLOSED_DISPLAY]


def _save_performance_cache(rows: list[dict], today_str: str, history: dict, state: Optional[dict]) -> None:
    changes = (state or {}).get("last_changes", {}) if state else {}
    cache = {
        "updated": today_str,
        "portfolio_return_krw": round(_portfolio_return(rows), 2),
        "active_positions": rows,
        "closed_positions": _recent_closed(history),
        "changes": changes,
        # Backward compatible chart data: one point per generated report.
        "report_summaries": [{
            "date": today_str,
            "avg_return_krw": round(_portfolio_return(rows), 2),
            "stock_count": len(rows),
            "stocks": rows,
        }],
        "all_rows": rows,
    }
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def _format_price(row: dict, field: str) -> str:
    value = row.get(field)
    if value is None:
        return "-"
    cur = "₩" if row.get("type") == "KR" else "$"
    return f"{cur}{value:,.0f}"


def _format_ret(value: float) -> str:
    sign = "▲" if value >= 0 else "▼"
    emoji = "🟢" if value >= 5 else ("🔴" if value <= -5 else "🟡")
    return f"{emoji} {sign}{abs(value):.1f}%"


def _format_no_history(today_str: str) -> str:
    return (
        "\n\n---\n\n"
        "## 📈 모델 포트폴리오 성과\n\n"
        f"> 오늘({today_str}) 기준으로 추적 가능한 active 포지션이 없습니다.\n"
    )


def _format_performance(rows: list[dict], history: dict, today_str: str) -> str:
    if not rows:
        return _format_no_history(today_str)

    rows_sorted = sorted(rows, key=lambda x: x.get("market", "") + x.get("name", ""))
    portfolio_return = _portfolio_return(rows)
    lines = [
        "", "", "---", "",
        "## 📈 모델 포트폴리오 성과",
        f"*기준일: {today_str} | 미국 주식은 원화 환산 기준*",
        "",
        "| 종목 | 편입일 | 진입가 | 현재가 | 목표비중 | 수익률(KRW) | 판단 |",
        "|------|--------|--------|--------|----------|-------------|------|",
    ]
    for row in rows_sorted:
        ret = row.get("return_pct_krw", row.get("return_pct", 0.0))
        lines.append(
            f"| {row.get('name', '')} | {row.get('entry_date', '')} | "
            f"{_format_price(row, 'entry_price')} | {_format_price(row, 'current_price')} | "
            f"{row.get('weight', '')} | {_format_ret(ret)} | {row.get('action', '')} |"
        )
    sign = "▲" if portfolio_return >= 0 else "▼"
    lines.append(f"| **현재 포트폴리오 수익률** | — | — | — | — | **{sign}{abs(portfolio_return):.1f}%** | — |")
    lines += [
        "",
        "> 현재 포트폴리오 표의 active 종목만 반영한 목표비중 가중 수익률입니다.",
        "> 같은 종목의 반복 추천은 하나의 포지션으로 병합하며, 종료 포지션은 아래 섹션에 별도로 보존합니다.",
    ]

    closed = _recent_closed(history)
    if closed:
        lines += [
            "",
            "### 최근 종료 포지션",
            "",
            "| 종목 | 편입일 | 종료일 | 종료 사유 |",
            "|------|--------|--------|-----------|",
        ]
        for pos in closed:
            lines.append(
                f"| {pos.get('name', '')} | {pos.get('entry_date', '')} | "
                f"{pos.get('closed_date', '')} | {pos.get('close_reason') or pos.get('removed_reason', '')} |"
            )
    return "\n".join(lines)


def _assert_consistent(current_items: list[dict], rows: list[dict]) -> None:
    current_keys = {_position_key(item) for item in current_items}
    row_keys = [_position_key(row) for row in rows]
    duplicates = sorted({k for k in row_keys if row_keys.count(k) > 1})
    if duplicates:
        raise ValueError(f"active 성과 표에 중복 포지션이 있습니다: {', '.join(duplicates)}")
    extra = sorted(set(row_keys) - current_keys)
    if extra:
        raise ValueError(f"active 성과 표에 현재 포트폴리오 밖 종목이 있습니다: {', '.join(extra)}")


def update_and_get_performance(report_text: str, today: datetime, state: Optional[dict] = None) -> str:
    if not YFINANCE_AVAILABLE:
        return "\n\n---\n\n## 📈 모델 포트폴리오 성과\n\n> `yfinance` 미설치\n"

    today_str = today.strftime("%Y-%m-%d")
    current_items = parse_portfolio_from_report(report_text)
    history = _load_history()

    print("  📌 현재 포트폴리오 포지션 갱신 중...")
    _ensure_current_positions(history, current_items, today_str)
    active_rows = calculate_active_rows(history)
    _assert_consistent(current_items, active_rows)
    _save_history(history)
    _save_performance_cache(active_rows, today_str, history, state)
    print(f"  💾 포지션 원장 저장 완료 (active {len(active_rows)}건)")

    return _format_performance(active_rows, history, today_str)


if __name__ == "__main__":
    sample = """
## 📊 포트폴리오 추천

### 🇰🇷 국내주식 (한국)

| 종목명 | 코드 | 판단 | 목표비중 | 핵심 근거 |
|--------|------|------|----------|-----------|
| 삼성전자 | 005930 | 매수 | 20% | AI 반도체 수혜 |

### 🇺🇸 해외주식 (미국)

| 종목명 | 티커 | 판단 | 목표비중 | 핵심 근거 |
|--------|------|------|----------|-----------|
| NVIDIA | NVDA | Buy | 25% | AI 인프라 핵심 |
"""
    print(update_and_get_performance(sample, datetime.now()))
