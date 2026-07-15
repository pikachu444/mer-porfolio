"""
track_returns.py
모델 포트폴리오 성과 추적 모듈.

성과 추적은 날짜별 추천 이력이 아니라 현재 active 포지션 원장을 기준으로 한다.
같은 종목이 반복 추천되어도 하나의 포지션만 유지하고, 명시적으로 종료된 종목은
closed position으로 이동한다.
"""

import json
import math
import os
import re
from statistics import pstdev
import time
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from portfolio_schema import normalize_security_code
from portfolio_metrics import benchmark_returns, performance_metrics

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False
    print("  ⚠ yfinance 미설치 — 수익률 추적 비활성화 (pip install yfinance)")

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "output"))
HISTORY_FILE = OUTPUT_DIR / "portfolio_history.json"
CACHE_FILE = OUTPUT_DIR / "performance_cache.json"
MODEL_LEDGER_FILE = OUTPUT_DIR / "model_portfolio_ledger.json"
MAX_CLOSED_DISPLAY = 8
MODEL_LEDGER_SCHEMA_VERSION = "4.0"
LEGACY_MODEL_LEDGER_SCHEMA_VERSION = "3.0"
STRUCTURED_REBALANCE_DRIFT_BPS = 50.0
STRUCTURED_EXECUTION_RESIDUAL_BPS = 10.0
CORPORATE_ACTION_LOOKBACK_DAYS = 7


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


def _security_identity(item: dict) -> str:
    market = str(item.get("market") or item.get("type") or "").strip().upper()
    code = normalize_security_code(
        item.get("name", ""),
        market,
        item.get("code") or item.get("ticker") or "",
    )
    if code:
        return f"{market}:{code.upper()}"
    return f"{market}:NAME:{str(item.get('name') or '').strip().lower()}"


def _normalize_tracking_item(item: dict) -> dict:
    normalized = deepcopy(item)
    market = str(normalized.get("market") or normalized.get("type") or "").strip().upper()
    if market:
        normalized["market"] = market
        normalized["type"] = normalized.get("type") or market
    code = normalize_security_code(
        normalized.get("name", ""),
        market,
        normalized.get("code") or normalized.get("ticker") or "",
    )
    if code:
        normalized["code"] = code
        if market == "KR":
            ticker = str(normalized.get("ticker") or "")
            suffix = ".KQ" if ticker.upper().endswith(".KQ") else ".KS"
            normalized["ticker"] = f"{code}{suffix}"
        elif not normalized.get("ticker"):
            normalized["ticker"] = code
    normalized["key"] = _position_key(normalized)
    return normalized


def _position_key(item: dict) -> str:
    market = (item.get("market") or item.get("type") or "").upper()
    code = normalize_security_code(
        item.get("name", ""),
        market,
        item.get("code") or item.get("ticker") or "",
    )
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
                return _normalize_tracking_history(_migrate_history(json.load(f)))
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


def _normalize_tracking_history(history: dict) -> dict:
    normalized = deepcopy(history)
    normalized["schema_version"] = "2.0"
    normalized["positions"] = [
        _normalize_tracking_item(item)
        for item in normalized.get("positions", [])
    ]
    normalized["closed_positions"] = [
        _normalize_tracking_item(item)
        for item in normalized.get("closed_positions", [])
    ]
    return normalized


def _save_history(history: dict) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def _current_security_identities(state: Optional[dict]) -> set[str]:
    if not state:
        return set()
    return {
        _security_identity(item)
        for item in state.get("portfolio", []) or []
    }


def _sanitize_tracking_history_for_state(history: dict, state: Optional[dict]) -> dict:
    current = _current_security_identities(state)
    normalized = _normalize_tracking_history(history)
    active_by_identity = {
        _security_identity(item)
        for item in normalized.get("positions", [])
        if item.get("status", "active") == "active"
    }
    blocked = current | active_by_identity

    positions = []
    seen_active: set[str] = set()
    for item in normalized.get("positions", []):
        identity = _security_identity(item)
        if item.get("status", "active") != "active" and identity in current:
            continue
        if item.get("status", "active") == "active":
            if identity in seen_active:
                continue
            seen_active.add(identity)
        positions.append(item)

    normalized["positions"] = positions
    normalized["closed_positions"] = [
        item
        for item in normalized.get("closed_positions", [])
        if _security_identity(item) not in blocked
    ]
    return normalized


def sanitize_performance_cache_for_state(cache: dict, state: Optional[dict]) -> dict:
    current = _current_security_identities(state)
    sanitized = deepcopy(cache or {})

    active_positions = [
        _normalize_cache_item(item)
        for item in sanitized.get("active_positions", []) or []
    ]
    active_identities = {_security_identity(item) for item in active_positions}
    sanitized["active_positions"] = active_positions
    closed_positions = []
    for item in sanitized.get("closed_positions", []) or []:
        normalized_item = _normalize_cache_item(item)
        is_structured_episode = bool(item.get("asset_type")) or str(
            item.get("key", "")
        ).count(":") >= 2
        if is_structured_episode or _security_identity(item) not in (current | active_identities):
            closed_positions.append(normalized_item)
    sanitized["closed_positions"] = closed_positions
    return sanitized


def _normalize_cache_item(item: dict) -> dict:
    if item.get("asset_type") or str(item.get("key", "")).count(":") >= 2:
        return _normalize_structured_item(item)
    return _normalize_tracking_item(item)


def sanitize_performance_files_for_state(
    state: Optional[dict],
    *,
    persist: bool = True,
) -> dict:
    if HISTORY_FILE.exists():
        history = _load_history()
        sanitized_history = _sanitize_tracking_history_for_state(history, state)
        if persist and sanitized_history != history:
            _save_history(sanitized_history)

    if not CACHE_FILE.exists():
        return {}
    with open(CACHE_FILE, encoding="utf-8") as file:
        cache = json.load(file)
    sanitized_cache = sanitize_performance_cache_for_state(cache, state)
    if persist and sanitized_cache != cache:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = CACHE_FILE.with_suffix(CACHE_FILE.suffix + ".tmp")
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump(sanitized_cache, file, ensure_ascii=False, indent=2)
        temporary.replace(CACHE_FILE)
    return sanitized_cache


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


def create_model_ledger(starting_cash: float = 100.0) -> dict:
    """신규 구조 활성화 시점부터 사용하는 실제 거래 방식의 모델 원장."""
    if starting_cash <= 0:
        raise ValueError("starting_cash must be positive")
    return {
        "schema_version": MODEL_LEDGER_SCHEMA_VERSION,
        "epoch_id": None,
        "inception_date": None,
        "base_currency": "KRW",
        "starting_value": float(starting_cash),
        "cash": float(starting_cash),
        "realized_pnl": 0.0,
        "dividend_income": 0.0,
        "cumulative_costs": 0.0,
        "positions": [],
        "closed_positions": [],
        "transactions": [],
        "corporate_events": [],
        "corporate_action_state": {
            "last_checked_date": None,
            "last_applied_date": None,
        },
        "snapshots": [],
        "legacy_epochs": [],
        "cost_policy": {
            "KR_one_way_bps": 30.0,
            "US_one_way_bps_including_fx": 35.0,
        },
        "risk_state": {
            "scale": 1.0,
            "recovery_days": 0,
            "last_evaluated_date": None,
            "current_drawdown": 0.0,
        },
    }


def _migrate_legacy_model_ledger(ledger: dict) -> dict:
    """Start a clean epoch while retaining the complete v3 record for audit."""
    migrated = create_model_ledger(float(ledger.get("starting_value") or 100.0))
    migrated["legacy_epochs"] = [{
        "source_schema_version": LEGACY_MODEL_LEDGER_SCHEMA_VERSION,
        "status": "legacy_unvalidated",
        "ledger": deepcopy(ledger),
    }]
    return migrated


def load_model_ledger(path: Path = MODEL_LEDGER_FILE) -> dict:
    if not path.exists():
        return create_model_ledger()
    with open(path, encoding="utf-8") as file:
        ledger = json.load(file)
    if ledger.get("schema_version") == LEGACY_MODEL_LEDGER_SCHEMA_VERSION:
        return _migrate_legacy_model_ledger(ledger)
    if ledger.get("schema_version") != MODEL_LEDGER_SCHEMA_VERSION:
        raise ValueError("structured model ledger schema_version must be '4.0'")
    return _normalize_model_ledger(ledger)


def save_model_ledger(ledger: dict, path: Path = MODEL_LEDGER_FILE) -> None:
    if ledger.get("schema_version") != MODEL_LEDGER_SCHEMA_VERSION:
        raise ValueError("structured model ledger schema_version must be '4.0'")
    ledger = _normalize_model_ledger(ledger)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(ledger, file, ensure_ascii=False, indent=2)
    temporary.replace(path)


def get_structured_prices(items: list[dict]) -> dict[str, float]:
    """Return current KRW-denominated prices for structured portfolio items."""
    usdkrw = _get_usdkrw()
    prices: dict[str, float] = {}
    for item in items:
        normalized = _normalize_structured_item(item)
        key = _structured_position_key(normalized)
        market = str(normalized.get("market", "")).strip().upper()
        code = str(normalized.get("code", "")).strip().upper()
        ticker = _resolve_kr_ticker(code) if market == "KR" else code
        price = _get_current_price(ticker or "")
        if price is None:
            raise ValueError(f"현재 가격을 가져오지 못했습니다: {item.get('name', key)}")
        if market == "US":
            if usdkrw is None:
                raise ValueError("USD/KRW 환율을 가져오지 못했습니다.")
            price *= usdkrw
        prices[key] = price
    return prices


def get_structured_volatilities(
    items: list[dict],
    *,
    lookback_returns: int = 20,
    minimum_returns: int = 20,
) -> dict[str, float]:
    """Return 20-trading-day annualized volatility in the KRW base currency."""
    if not YFINANCE_AVAILABLE:
        return {}
    if lookback_returns <= 1 or minimum_returns <= 1:
        raise ValueError("volatility lookback and minimum returns must exceed one")

    def close_series(history) -> list[tuple[date, float]]:
        values: list[tuple[date, float]] = []
        for index, raw in history["Close"].dropna().items():
            value = float(raw)
            if math.isfinite(value) and value > 0:
                values.append((index.date(), value))
        return sorted(values)

    fx_series: list[tuple[date, float]] | None = None

    def usdkrw_for(day, series: list[tuple[date, float]]) -> float | None:
        eligible = [value for series_day, value in series if series_day <= day]
        return eligible[-1] if eligible else None

    result: dict[str, float] = {}
    for item in items:
        normalized = _normalize_structured_item(item)
        key = _structured_position_key(normalized)
        market = str(normalized.get("market", "")).strip().upper()
        code = str(normalized.get("code", "")).strip().upper()
        ticker = _resolve_kr_ticker(code) if market == "KR" else code
        if not ticker:
            continue
        try:
            history = yf.Ticker(ticker).history(period="3mo", auto_adjust=True)
            closes_by_date = close_series(history)
            if market == "US":
                if fx_series is None:
                    fx_history = yf.Ticker("KRW=X").history(
                        period="3mo",
                        auto_adjust=True,
                    )
                    fx_series = close_series(fx_history)
                closes_by_date = [
                    (day, close * fx_rate)
                    for day, close in closes_by_date
                    if (fx_rate := usdkrw_for(day, fx_series)) is not None
                ]
        except Exception:
            continue
        closes = [value for _, value in closes_by_date]
        returns = [
            closes[index] / closes[index - 1] - 1.0
            for index in range(1, len(closes))
            if closes[index - 1] > 0 and closes[index] > 0
        ]
        returns = returns[-lookback_returns:]
        if len(returns) < minimum_returns:
            continue
        volatility = pstdev(returns) * math.sqrt(252.0)
        if volatility > 0:
            result[key] = volatility
    return result


def refresh_structured_performance(
    ledger: dict,
    prices: dict[str, float],
    today_str: str,
    path: Path = CACHE_FILE,
    *,
    persist: bool = True,
    fetch_benchmark: bool = False,
) -> dict:
    """Refresh the structured performance cache without changing allocation."""
    source_ledger = ledger
    ledger = _normalize_model_ledger(ledger)
    snapshot = record_model_snapshot(ledger, prices, today_str)
    actual_weights = structured_actual_weights(ledger, prices) if ledger.get("positions") else {}
    active_positions = []
    for position in ledger.get("positions", []):
        current_price = _required_trade_price(prices, position["key"])
        average_cost = float(position["average_cost"])
        active_positions.append({
            **deepcopy(position),
            "current_price": current_price,
            "return_pct_krw": (current_price / average_cost - 1.0) * 100.0,
            "actual_weight": actual_weights.get(position["key"], 0.0),
            "target_weight": float(position.get("target_weight", 0.0)),
        })
    snapshot_values = [
        float(item["total_value"])
        for item in ledger.get("snapshots", [])
        if item.get("total_value") is not None
    ]
    daily_returns = [
        snapshot_values[index] / snapshot_values[index - 1] - 1.0
        for index in range(1, len(snapshot_values))
        if snapshot_values[index - 1] > 0
    ]
    benchmark_daily, benchmark_status = (
        _benchmark_returns_for_snapshots(ledger.get("snapshots", []))
        if fetch_benchmark
        else ([], "not_requested")
    )
    risk_metrics = (
        performance_metrics(
            daily_returns,
            benchmark_daily if len(benchmark_daily) == len(daily_returns) else None,
        )
        if daily_returns
        else {}
    )
    total_value = float(snapshot["total_value"])
    cache = sanitize_performance_cache_for_state({
        "updated": today_str,
        "epoch_id": ledger.get("epoch_id"),
        "inception_date": ledger.get("inception_date"),
        "legacy_epoch_count": len(ledger.get("legacy_epochs", []) or []),
        "portfolio_return_krw": snapshot["return_pct"],
        "cash": snapshot["cash"],
        "realized_pnl": snapshot["realized_pnl"],
        "dividend_income": float(ledger.get("dividend_income", 0.0)),
        "cumulative_costs": float(ledger.get("cumulative_costs", 0.0)),
        "actual_cash_weight": (
            float(snapshot["cash"]) / total_value * 100.0 if total_value > 0 else None
        ),
        "risk_metrics": risk_metrics,
        "benchmark": {
            "policy": "KODEX200 40% + TIGER 미국S&P500 40% + 현금 20%",
            "status": benchmark_status,
            "period_returns": benchmark_daily,
        },
        "active_positions": active_positions,
        "closed_positions": deepcopy(ledger.get("closed_positions", [])),
        "report_summaries": list({
            str(item["date"]): {
                "date": item["date"],
                "avg_return_krw": item["return_pct"],
            }
            for item in ledger.get("snapshots", [])
            if item.get("date")
        }.values()),
    }, {"portfolio": ledger.get("positions", [])})
    if persist:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump(cache, file, ensure_ascii=False, indent=2)
        temporary.replace(path)
    source_ledger.clear()
    source_ledger.update(deepcopy(ledger))
    return cache


def _benchmark_returns_for_snapshots(snapshots: list[dict]) -> tuple[list[float], str]:
    """Best-effort adjusted-close benchmark aligned to model snapshot dates."""
    dates = [str(item.get("date") or "") for item in snapshots if item.get("date")]
    if len(dates) < 2:
        return [], "insufficient_history"
    if not YFINANCE_AVAILABLE:
        return [], "unavailable"
    try:
        start = (datetime.fromisoformat(dates[0]) - timedelta(days=7)).strftime("%Y-%m-%d")
        end = (datetime.fromisoformat(dates[-1]) + timedelta(days=2)).strftime("%Y-%m-%d")
        components = []
        for ticker in ("069500.KS", "360750.KS"):
            history = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=True)
            closes = {
                index.strftime("%Y-%m-%d"): float(value)
                for index, value in history["Close"].dropna().items()
            }
            aligned = []
            for snapshot_date in dates:
                eligible = [key for key in closes if key <= snapshot_date]
                if not eligible:
                    raise ValueError(f"benchmark has no close at {snapshot_date}")
                aligned.append(closes[max(eligible)])
            components.append([
                aligned[index] / aligned[index - 1] - 1.0
                for index in range(1, len(aligned))
            ])
        return benchmark_returns(components[0], components[1]), "ok"
    except Exception as exc:
        return [], f"unavailable: {type(exc).__name__}"


def apply_structured_transactions(
    ledger: dict,
    decisions: list[dict],
    price_by_key: dict[str, float],
    trade_date: str,
    *,
    cost_bps_by_market: dict[str, float] | None = None,
) -> dict:
    """검증된 판단의 목표 비중을 신규 매수, 추가 매수, 일부 매도, 전량 매도로 기록한다."""
    updated = _normalize_model_ledger(ledger)
    if updated.get("schema_version") != MODEL_LEDGER_SCHEMA_VERSION:
        raise ValueError("structured model ledger schema_version must be '4.0'")
    if updated.get("inception_date") is None:
        updated["inception_date"] = trade_date
        updated["epoch_id"] = f"clean-v4-{trade_date}"
    positions = updated.setdefault("positions", [])
    by_key = {item["key"]: item for item in positions}
    portfolio_value = _model_portfolio_value(updated, price_by_key)
    if portfolio_value <= 0:
        raise ValueError("structured model portfolio value must be positive")
    if not updated.get("snapshots"):
        record_model_snapshot(
            updated,
            price_by_key,
            trade_date,
            snapshot_kind="opening",
        )

    plan = []
    seen_keys: set[str] = set()
    for decision in decisions:
        key = _structured_position_key(decision)
        if key in seen_keys:
            raise ValueError(f"duplicate structured transaction decision for {key}")
        seen_keys.add(key)
        price = _required_trade_price(price_by_key, key)
        position = by_key.get(key)
        if decision["action"] == "매도" and position is None:
            raise ValueError(f"cannot sell missing position {key}")
        current_quantity = float(position.get("quantity", 0.0)) if position else 0.0
        current_value = current_quantity * price
        target_weight = 0.0 if decision["action"] == "매도" else float(decision["proposed_weight"])
        target_value = portfolio_value * target_weight / 100.0
        value_delta = target_value - current_value
        drift_bps = (target_weight - current_value / portfolio_value * 100.0) * 100.0
        should_trade = (
            decision["action"] == "매도"
            or abs(drift_bps) + 1e-9 >= STRUCTURED_REBALANCE_DRIFT_BPS
        )
        if not should_trade:
            if position is not None:
                position["target_weight"] = target_weight
                position["decision_actor"] = decision["decision_actor"]
            continue
        if abs(value_delta) < 1e-9:
            if position is not None:
                position["target_weight"] = target_weight
            continue
        plan.append({
            "decision": decision,
            "key": key,
            "price": price,
            "target_weight": target_weight,
            "value_delta": value_delta,
            "drift_bps": drift_bps,
            "cost_rate": (
                float((cost_bps_by_market or {}).get(
                    str(decision.get("market") or "").upper(),
                    0.0,
                ))
                / 10_000.0
            ),
        })

    # Solve target notionals against post-cost NAV.  Otherwise trading costs are
    # taken only from cash and systematically push the cash sleeve below target.
    post_cost_nav = portfolio_value
    for _ in range(20):
        total_cost = 0.0
        for item in plan:
            decision = item["decision"]
            position = by_key.get(item["key"])
            current_value = (
                float(position.get("quantity", 0.0)) * item["price"]
                if position else 0.0
            )
            target_value = post_cost_nav * item["target_weight"] / 100.0
            value_delta = target_value - current_value
            if decision["action"] == "매도":
                value_delta = -current_value
            item["value_delta"] = value_delta
            total_cost += abs(value_delta) * item["cost_rate"]
        next_nav = portfolio_value - total_cost
        if abs(next_nav - post_cost_nav) < 1e-10:
            post_cost_nav = next_nav
            break
        post_cost_nav = next_nav

    # Use one pre-trade NAV and sell first, so rotations do not depend on the
    # order in which Gemini happened to return decisions.
    plan.sort(key=lambda item: (0 if item["value_delta"] < 0 else 1, item["key"]))
    executed_targets: dict[str, float] = {}
    for item in plan:
        decision = item["decision"]
        key = item["key"]
        price = item["price"]
        target_weight = item["target_weight"]
        value_delta = item["value_delta"]
        position = by_key.get(key)

        if value_delta > 0:
            cost_rate = item["cost_rate"]
            trade_cost = value_delta * cost_rate
            if value_delta + trade_cost > updated["cash"] + 1e-9:
                raise ValueError(f"insufficient cash for {key}")
            quantity = value_delta / price
            if position is None:
                position = {
                    "key": key,
                    "name": decision["name"],
                    "code": decision["code"],
                    "market": decision["market"],
                    "asset_type": decision["asset_type"],
                    "quantity": 0.0,
                    "average_cost": 0.0,
                    "decision_actor": decision["decision_actor"],
                    "opened_date": trade_date,
                    "episode_realized_pnl": 0.0,
                }
                positions.append(position)
                by_key[key] = position
                transaction_type = "신규 매수"
            else:
                transaction_type = "추가 매수"
            previous_cost = position["quantity"] * position["average_cost"]
            position["quantity"] += quantity
            position["average_cost"] = (previous_cost + value_delta + trade_cost) / position["quantity"]
            position["decision_actor"] = decision["decision_actor"]
            position["target_weight"] = target_weight
            updated["cash"] -= value_delta + trade_cost
        else:
            if position is None:
                raise ValueError(f"cannot sell missing position {key}")
            sell_value = -value_delta
            quantity = min(position["quantity"], sell_value / price)
            sell_value = quantity * price
            cost_rate = item["cost_rate"]
            trade_cost = sell_value * cost_rate
            realized = quantity * (price - position["average_cost"]) - trade_cost
            episode_realized = float(position.get("episode_realized_pnl", 0.0)) + realized
            position["episode_realized_pnl"] = episode_realized
            position["quantity"] -= quantity
            updated["cash"] += sell_value - trade_cost
            updated["realized_pnl"] += realized
            transaction_type = "전량 매도" if position["quantity"] < 1e-9 else "일부 매도"
            if transaction_type == "전량 매도":
                closed = {
                    **position,
                    "quantity": 0.0,
                    "closed_date": trade_date,
                    "closed_price": price,
                    "realized_pnl": episode_realized,
                    "close_reason": decision["change_reason"],
                }
                updated.setdefault("closed_positions", []).append(closed)
                positions.remove(position)
                del by_key[key]
            else:
                position["decision_actor"] = decision["decision_actor"]
                position["target_weight"] = target_weight

        updated["cumulative_costs"] = float(updated.get("cumulative_costs", 0.0)) + trade_cost
        updated.setdefault("transactions", []).append({
            "date": trade_date,
            "type": transaction_type,
            "key": key,
            "name": decision["name"],
            "decision_actor": decision["decision_actor"],
            "price": price,
            "quantity": quantity,
            "value": value_delta if value_delta > 0 else -sell_value,
            "trade_cost": trade_cost,
            "realized_pnl": realized if value_delta <= 0 else 0.0,
            "target_weight": target_weight,
            "drift_bps_before": item["drift_bps"],
            "reason": decision["change_reason"],
        })
        executed_targets[key] = target_weight

    if executed_targets:
        assert_structured_target_residuals(
            updated,
            executed_targets,
            price_by_key,
            max_residual_bps=STRUCTURED_EXECUTION_RESIDUAL_BPS,
        )
        final_position_keys = {position["key"] for position in updated.get("positions", [])}
        if final_position_keys.issubset(seen_keys):
            target_cash_weight = max(
                0.0,
                100.0 - sum(
                    float(decision["proposed_weight"])
                    for decision in decisions
                    if decision.get("action") != "매도"
                ),
            )
            final_nav = _model_portfolio_value(updated, price_by_key)
            actual_cash_weight = float(updated["cash"]) / final_nav * 100.0
            if abs(actual_cash_weight - target_cash_weight) * 100.0 > STRUCTURED_EXECUTION_RESIDUAL_BPS + 1e-6:
                raise ValueError(
                    "structured cash target residual exceeds "
                    f"{STRUCTURED_EXECUTION_RESIDUAL_BPS:g} bps"
                )

    record_model_snapshot(updated, price_by_key, trade_date)
    return updated


def transaction_decisions_for_run(
    ledger: dict,
    current_portfolio: list[dict],
    decisions: list[dict],
) -> list[dict]:
    """Use the post-reevaluation baseline on the first exact-tracking run."""
    if not ledger.get("transactions") and not ledger.get("positions"):
        return current_portfolio
    rows = [deepcopy(item) for item in decisions]
    target_keys = {_structured_position_key(item) for item in current_portfolio}
    decided_keys = {_structured_position_key(item) for item in rows}
    for position in ledger.get("positions", []) or []:
        key = _structured_position_key(position)
        if key in target_keys or key in decided_keys:
            continue
        rows.append({
            **deepcopy(position),
            "action": "매도",
            "previous_weight": float(position.get("target_weight") or 0.0),
            "proposed_weight": 0.0,
            "decision_actor": position.get("decision_actor") or "AI",
            "change_reason": "상태에 없는 원장 포지션 자동 정합화 청산",
        })
        decided_keys.add(key)
    return rows


def record_model_snapshot(
    ledger: dict,
    price_by_key: dict[str, float],
    snapshot_date: str,
    *,
    snapshot_kind: str = "closing",
) -> dict:
    """현금, 평가액, 실현 손익을 포함한 모델 포트폴리오 전체 성과를 기록한다."""
    total_value = _model_portfolio_value(ledger, price_by_key)
    starting_value = float(ledger["starting_value"])
    snapshot = {
        "date": snapshot_date,
        "kind": snapshot_kind,
        "cash": round(float(ledger["cash"]), 8),
        "invested_value": round(total_value - float(ledger["cash"]), 8),
        "total_value": round(total_value, 8),
        "return_pct": round((total_value / starting_value - 1.0) * 100.0, 8),
        "realized_pnl": round(float(ledger["realized_pnl"]), 8),
        "dividend_income": round(float(ledger.get("dividend_income", 0.0)), 8),
        "cumulative_costs": round(float(ledger.get("cumulative_costs", 0.0)), 8),
    }
    snapshots = ledger.setdefault("snapshots", [])
    if (
        snapshots
        and snapshots[-1].get("date") == snapshot_date
        and snapshots[-1].get("kind", "closing") == snapshot_kind
    ):
        snapshots[-1] = snapshot
    else:
        snapshots.append(snapshot)
    return snapshot


def _structured_position_key(item: dict) -> str:
    market = str(item.get("market", "")).strip().upper()
    asset_type = str(item.get("asset_type", "")).strip().lower()
    code = normalize_security_code(item.get("name", ""), market, item.get("code", ""))
    if code:
        return f"{asset_type}:{market}:{code}"
    return f"{asset_type}:{market}:NAME:{str(item.get('name', '')).strip().lower()}"


def _normalize_structured_item(item: dict) -> dict:
    normalized = deepcopy(item)
    market = str(normalized.get("market", "")).strip().upper()
    if market:
        normalized["market"] = market
    code = normalize_security_code(
        normalized.get("name", ""),
        market,
        normalized.get("code", ""),
    )
    if code:
        normalized["code"] = code
    normalized["key"] = _structured_position_key(normalized)
    return normalized


def _normalize_model_ledger(ledger: dict) -> dict:
    normalized = deepcopy(ledger)
    normalized.setdefault("dividend_income", 0.0)
    normalized.setdefault("corporate_events", [])
    normalized.setdefault("corporate_action_state", {
        "last_checked_date": None,
        "last_applied_date": None,
    })
    normalized["positions"] = [
        _normalize_structured_item(item)
        for item in normalized.get("positions", []) or []
    ]
    normalized["closed_positions"] = [
        _normalize_structured_item(item)
        for item in normalized.get("closed_positions", []) or []
    ]
    return normalized


def sanitize_model_ledger_for_state(ledger: dict, state: Optional[dict]) -> dict:
    """Normalize a ledger without deleting immutable closed-position episodes.

    A security can be sold and later bought again.  The active position and the
    earlier closed episode intentionally share a security identity; treating
    that as a duplicate destroys realized-P&L audit history.
    """
    _ = state  # Retained for API compatibility with the other sanitizers.
    return _normalize_model_ledger(ledger)


def _corporate_event_signature(event: dict) -> tuple:
    event_type = str(event.get("event_type") or event.get("type") or "").strip().lower()
    if event_type in {"split", "stock_split"}:
        event_type = "stock_split"
    elif event_type in {"dividend", "cash_dividend"}:
        event_type = "cash_dividend"
    return (
        event_type,
        str(event.get("key") or "").strip(),
        str(event.get("effective_date") or event.get("date") or "").strip(),
        float(event.get("ratio") or 0.0),
        float(event.get("cash_per_share") or event.get("amount_per_share") or 0.0),
    )


def _corporate_economic_key(event: dict) -> tuple[str, str, str]:
    signature = _corporate_event_signature(event)
    return signature[0], signature[1], signature[2]


def _normalize_corporate_event(event: dict) -> dict:
    event_id = str(event.get("event_id") or "").strip()
    if not event_id:
        raise ValueError("corporate action event_id is required")

    event_type = str(event.get("event_type") or event.get("type") or "").strip().lower()
    if event_type in {"split", "stock_split"}:
        event_type = "stock_split"
    elif event_type in {"dividend", "cash_dividend"}:
        event_type = "cash_dividend"
    else:
        raise ValueError(f"unsupported corporate action type: {event_type or '<empty>'}")

    key = str(event.get("key") or "").strip()
    if not key:
        key = _structured_position_key(event)
    if not key or key.endswith(":NAME:"):
        raise ValueError("corporate action position key is required")

    effective_date = str(event.get("effective_date") or event.get("date") or "").strip()
    try:
        if not effective_date or datetime.fromisoformat(effective_date).date().isoformat() != effective_date:
            raise ValueError
    except ValueError as exc:
        raise ValueError("corporate action effective_date must be YYYY-MM-DD") from exc

    normalized = {
        "event_id": event_id,
        "event_type": event_type,
        "key": key,
        "effective_date": effective_date,
    }
    for field in (
        "source",
        "ticker",
        "native_cash_per_share",
        "native_currency",
        "fx_rate",
    ):
        if event.get(field) is not None:
            normalized[field] = event[field]
    if event_type == "stock_split":
        ratio = float(event.get("ratio") or 0.0)
        if not math.isfinite(ratio) or ratio <= 0:
            raise ValueError("stock split ratio must be positive")
        normalized["ratio"] = ratio
    else:
        cash_per_share = float(
            event.get("cash_per_share") or event.get("amount_per_share") or 0.0
        )
        if not math.isfinite(cash_per_share) or cash_per_share <= 0:
            raise ValueError("cash dividend per share must be positive")
        normalized["cash_per_share"] = cash_per_share
    return normalized


def apply_corporate_action_events(ledger: dict, events: list[dict]) -> dict:
    """Apply split and base-currency cash-dividend events exactly once.

    Callers must provide a stable ``event_id`` from their data source.  Dividend
    amounts must already be converted into the ledger's base currency.  Events
    are deliberately separate from buy/sell transactions so turnover and
    execution-cost accounting remain unaffected.
    """
    updated = _normalize_model_ledger(ledger)
    if updated.get("schema_version") != MODEL_LEDGER_SCHEMA_VERSION:
        raise ValueError("structured model ledger schema_version must be '4.0'")

    applied = updated.setdefault("corporate_events", [])
    by_event_id = {
        str(item.get("event_id") or ""): item
        for item in applied
        if item.get("event_id")
    }
    by_economic_key = {
        _corporate_economic_key(item): item
        for item in applied
    }
    positions = {
        item["key"]: item
        for item in updated.get("positions", [])
    }

    for raw_event in events:
        event = _normalize_corporate_event(raw_event)
        existing = by_event_id.get(event["event_id"])
        if existing is not None:
            if _corporate_event_signature(existing) != _corporate_event_signature(event):
                raise ValueError(f"conflicting corporate action event_id: {event['event_id']}")
            continue
        economic_key = _corporate_economic_key(event)
        economic_existing = by_economic_key.get(economic_key)
        if economic_existing is not None:
            if (
                _corporate_event_signature(economic_existing)
                != _corporate_event_signature(event)
            ):
                raise ValueError(
                    "conflicting corporate action for the same security/type/date: "
                    + ":".join(economic_key)
                )
            continue

        position = positions.get(event["key"])
        if position is None or float(position.get("quantity", 0.0)) <= 0:
            raise ValueError(f"corporate action has no active position: {event['key']}")

        quantity = float(position["quantity"])
        recorded = deepcopy(event)
        recorded["quantity_at_event"] = quantity
        if event["event_type"] == "stock_split":
            ratio = float(event["ratio"])
            position["quantity"] = quantity * ratio
            position["average_cost"] = float(position["average_cost"]) / ratio
        else:
            cash_amount = quantity * float(event["cash_per_share"])
            updated["cash"] = float(updated["cash"]) + cash_amount
            updated["dividend_income"] = float(updated.get("dividend_income", 0.0)) + cash_amount
            recorded["cash_amount"] = cash_amount

        applied.append(recorded)
        by_event_id[event["event_id"]] = recorded
        by_economic_key[economic_key] = recorded
        action_state = updated.setdefault("corporate_action_state", {})
        action_state["last_applied_date"] = max(
            str(action_state.get("last_applied_date") or ""),
            event["effective_date"],
        )

    return updated


def get_structured_corporate_action_events(
    ledger: dict,
    through_date: str,
) -> list[dict]:
    """Fetch unprocessed splits/dividends for active positions from yfinance.

    The boundary is intentionally public and side-effect free so a different
    market-data provider can replace it without changing ledger accounting.
    Cash dividends are converted to the ledger's KRW base currency here.
    """
    if not YFINANCE_AVAILABLE:
        raise ValueError("corporate action lookup requires yfinance")
    try:
        through = datetime.fromisoformat(through_date).date()
    except ValueError as exc:
        raise ValueError("corporate action through_date must be YYYY-MM-DD") from exc

    normalized = _normalize_model_ledger(ledger)
    last_checked = str(
        normalized.get("corporate_action_state", {}).get("last_checked_date") or ""
    )
    events: list[dict] = []
    for position in normalized.get("positions", []):
        opened_date = str(position.get("opened_date") or "")
        overlap_start = ""
        if last_checked:
            try:
                overlap_start = (
                    datetime.fromisoformat(last_checked).date()
                    - timedelta(days=CORPORATE_ACTION_LOOKBACK_DAYS)
                ).isoformat()
            except ValueError as exc:
                raise ValueError(
                    f"invalid corporate action checkpoint date: {last_checked}"
                ) from exc
        since_text = max(opened_date, overlap_start)
        if not since_text:
            raise ValueError(f"active position has no opened_date: {position['key']}")
        try:
            since = datetime.fromisoformat(since_text).date()
        except ValueError as exc:
            raise ValueError(f"invalid corporate action start date: {since_text}") from exc
        if since >= through:
            continue

        market = str(position.get("market") or "").upper()
        code = str(position.get("code") or "").upper()
        ticker = _resolve_kr_ticker(code) if market == "KR" else code
        if not ticker:
            raise ValueError(f"corporate action ticker unavailable: {position['key']}")
        try:
            history = yf.Ticker(ticker).history(
                start=(since + timedelta(days=1)).isoformat(),
                end=(through + timedelta(days=1)).isoformat(),
                auto_adjust=False,
                actions=True,
            )
        except Exception as exc:
            raise ValueError(f"corporate action lookup failed: {position['key']}") from exc
        if history is None or history.empty:
            continue

        for index, row in history.iterrows():
            effective_date = index.date().isoformat()
            if effective_date <= since.isoformat() or effective_date > through.isoformat():
                continue
            split_ratio = float(row.get("Stock Splits", 0.0) or 0.0)
            if math.isfinite(split_ratio) and split_ratio > 0:
                ratio_text = format(split_ratio, ".12g")
                events.append({
                    "event_id": f"yfinance:{ticker}:stock_split:{effective_date}:{ratio_text}",
                    "type": "stock_split",
                    "key": position["key"],
                    "effective_date": effective_date,
                    "ratio": split_ratio,
                    "source": "yfinance",
                    "ticker": ticker,
                })

            native_dividend = float(row.get("Dividends", 0.0) or 0.0)
            if not math.isfinite(native_dividend) or native_dividend <= 0:
                continue
            fx_rate = 1.0
            native_currency = "KRW"
            if market == "US":
                native_currency = "USD"
                fetched_rate = _get_usdkrw(effective_date)
                if fetched_rate is None:
                    raise ValueError(f"dividend FX rate unavailable: {position['key']}")
                fx_rate = fetched_rate
            dividend_text = format(native_dividend, ".12g")
            events.append({
                "event_id": f"yfinance:{ticker}:cash_dividend:{effective_date}:{dividend_text}",
                "type": "cash_dividend",
                "key": position["key"],
                "effective_date": effective_date,
                "cash_per_share": native_dividend * fx_rate,
                "native_cash_per_share": native_dividend,
                "native_currency": native_currency,
                "fx_rate": fx_rate,
                "source": "yfinance",
                "ticker": ticker,
            })

    return sorted(
        events,
        key=lambda event: (
            event["effective_date"],
            0 if event["type"] == "stock_split" else 1,
            event["event_id"],
        ),
    )


def refresh_structured_corporate_actions(ledger: dict, through_date: str) -> dict:
    """Fetch and idempotently apply corporate actions before valuation."""
    normalized = _normalize_model_ledger(ledger)
    if not normalized.get("positions"):
        normalized.setdefault("corporate_action_state", {})["last_checked_date"] = through_date
        return normalized
    events = get_structured_corporate_action_events(normalized, through_date)
    updated = apply_corporate_action_events(normalized, events)
    updated.setdefault("corporate_action_state", {})["last_checked_date"] = through_date
    return updated


def _required_trade_price(price_by_key: dict[str, float], key: str) -> float:
    price = price_by_key.get(key)
    if price is None or float(price) <= 0:
        raise ValueError(f"missing positive trade price for {key}")
    return float(price)


def _model_portfolio_value(ledger: dict, price_by_key: dict[str, float]) -> float:
    total = float(ledger["cash"])
    for position in ledger.get("positions", []):
        price = _required_trade_price(price_by_key, position["key"])
        total += float(position["quantity"]) * price
    return total


def structured_actual_weights(
    ledger: dict,
    price_by_key: dict[str, float],
) -> dict[str, float]:
    """Return mark-to-market position weights using the ledger's actual NAV."""

    normalized = _normalize_model_ledger(ledger)
    portfolio_value = _model_portfolio_value(normalized, price_by_key)
    if portfolio_value <= 0:
        raise ValueError("structured model portfolio value must be positive")
    return {
        position["key"]: (
            float(position["quantity"])
            * _required_trade_price(price_by_key, position["key"])
            / portfolio_value
            * 100.0
        )
        for position in normalized.get("positions", [])
    }


def structured_target_residuals(
    ledger: dict,
    targets: dict[str, float],
    price_by_key: dict[str, float],
) -> dict[str, float]:
    """Return ``actual - target`` residuals in basis points."""

    actual = structured_actual_weights(ledger, price_by_key)
    return {
        key: (actual.get(key, 0.0) - float(target_weight)) * 100.0
        for key, target_weight in sorted(targets.items())
    }


def assert_structured_target_residuals(
    ledger: dict,
    targets: dict[str, float],
    price_by_key: dict[str, float],
    *,
    max_residual_bps: float = STRUCTURED_EXECUTION_RESIDUAL_BPS,
) -> dict[str, float]:
    """Validate execution accuracy and return the measured residuals."""

    if max_residual_bps < 0:
        raise ValueError("max_residual_bps must not be negative")
    residuals = structured_target_residuals(ledger, targets, price_by_key)
    violations = {
        key: residual
        for key, residual in residuals.items()
        if abs(residual) > max_residual_bps + 1e-7
    }
    if violations:
        detail = ", ".join(
            f"{key}={residual:+.2f}bp"
            for key, residual in violations.items()
        )
        raise ValueError(
            f"structured target residual exceeds {max_residual_bps:g}bp: {detail}"
        )
    return residuals


def update_and_get_performance(report_text: str, today: datetime, state: Optional[dict] = None) -> str:
    if not YFINANCE_AVAILABLE:
        return "\n\n---\n\n## 📈 모델 포트폴리오 성과\n\n> `yfinance` 미설치\n"

    today_str = today.strftime("%Y-%m-%d")
    current_items = parse_portfolio_from_report(report_text)
    history = _load_history()

    print("  📌 현재 포트폴리오 포지션 갱신 중...")
    _ensure_current_positions(history, current_items, today_str)
    history = _sanitize_tracking_history_for_state(
        history,
        {"portfolio": current_items},
    )
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
