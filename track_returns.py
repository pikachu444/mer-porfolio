"""
track_returns.py
포트폴리오 추천 누적 수익률 추적 모듈

매 리포트 실행 시:
  1. 이번 리포트 추천 종목을 파싱 → 진입가 + 환율 기록
  2. 과거 추천 종목의 현재가/환율을 조회해 수익률 계산
  3. performance_cache.json 저장 (대시보드에서 사용)
  4. 리포트 하단에 붙일 마크다운 섹션 반환

야후파이낸스 티커:
  KOSPI:   {6자리코드}.KS  예: 005930.KS
  KOSDAQ:  {6자리코드}.KQ  예: 035720.KQ
  USD/KRW: KRW=X
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
MAX_HISTORY_MONTHS = 12


# ─── 환율 조회 ────────────────────────────────────────────────────────────────

def _get_usdkrw(date_str: Optional[str] = None) -> Optional[float]:
    """
    USD/KRW 환율 반환.
    date_str=None 이면 현재 환율, 날짜 지정 시 해당일 환율.
    """
    if not YFINANCE_AVAILABLE:
        return None
    try:
        ticker = yf.Ticker("KRW=X")
        if date_str is None:
            rate = getattr(ticker.fast_info, "last_price", None)
            return float(rate) if rate and rate > 0 else None
        # 특정 날짜
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


# ─── 리포트 파싱 ──────────────────────────────────────────────────────────────

def parse_portfolio_from_report(report_text: str) -> dict:
    """마크다운 리포트에서 종목 추천 테이블 파싱."""
    result = {"kr": [], "us": []}

    kr_match = re.search(
        r"(?:🇰🇷|국내주식)[^\n]*\n\n?\|[^\n]+\|\n\|[-| :]+\|\n((?:\|[^\n]+\|\n?)+)",
        report_text,
    )
    if kr_match:
        for row in kr_match.group(1).strip().split("\n"):
            cells = [c.strip() for c in row.split("|")[1:-1]]
            if len(cells) >= 4 and cells[0] and not cells[0].startswith("-"):
                code = re.sub(r"[^0-9]", "", cells[1])
                if code:
                    result["kr"].append({
                        "name": cells[0],
                        "code": code.zfill(6),
                        "action": cells[2],
                        "weight": cells[3],
                    })

    us_match = re.search(
        r"(?:🇺🇸|해외주식)[^\n]*\n\n?\|[^\n]+\|\n\|[-| :]+\|\n((?:\|[^\n]+\|\n?)+)",
        report_text,
    )
    if us_match:
        for row in us_match.group(1).strip().split("\n"):
            cells = [c.strip() for c in row.split("|")[1:-1]]
            if len(cells) >= 4 and cells[0] and not cells[0].startswith("-"):
                ticker = re.sub(r"[^A-Za-z0-9.\-]", "", cells[1]).upper()
                if ticker and ticker != "-":
                    result["us"].append({
                        "name": cells[0],
                        "ticker": ticker,
                        "action": cells[2],
                        "weight": cells[3],
                    })

    return result


# ─── 주가 조회 ────────────────────────────────────────────────────────────────

def _resolve_kr_ticker(code: str) -> Optional[str]:
    """6자리 코드 → 야후파이낸스 티커 (KOSPI 우선, KOSDAQ 폴백)."""
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
    """특정 날짜 종가 반환. 없으면 ±7일 내 가장 가까운 날짜 사용."""
    if not YFINANCE_AVAILABLE:
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
    """현재가 반환."""
    if not YFINANCE_AVAILABLE:
        return None
    try:
        price = getattr(yf.Ticker(ticker).fast_info, "last_price", None)
        return float(price) if price and price > 0 else None
    except Exception:
        return None


# ─── 히스토리 관리 ────────────────────────────────────────────────────────────

def _load_history() -> dict:
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"records": []}


def _save_history(history: dict) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def _prune_old_records(history: dict) -> dict:
    cutoff = datetime.now() - timedelta(days=MAX_HISTORY_MONTHS * 30)
    history["records"] = [
        r for r in history["records"]
        if datetime.strptime(r["date"], "%Y-%m-%d") >= cutoff
    ]
    return history


# ─── 성과 계산 ────────────────────────────────────────────────────────────────

def calculate_performance_rows(history: dict, today_str: str) -> list:
    """
    과거 추천 종목의 현재 수익률을 계산해서 반환.
    US 종목은 USD 수익률 + KRW 수익률 모두 계산.
    """
    past = [r for r in history["records"] if r["date"] < today_str]
    if not past:
        return []

    current_usdkrw = _get_usdkrw()
    rows = []

    for rec in past:
        if not rec.get("entry_price") or not rec.get("ticker"):
            continue

        current_price = _get_current_price(rec["ticker"])
        if current_price is None:
            continue

        entry = rec["entry_price"]
        ret_pct = (current_price - entry) / entry * 100

        row = {
            "name": rec["name"],
            "ticker": rec["ticker"],
            "type": rec["type"],
            "date": rec["date"],
            "action": rec["action"],
            "weight": rec.get("weight", ""),
            "entry_price": entry,
            "current_price": current_price,
            "return_pct": ret_pct,  # USD 기준 (KR은 KRW 기준)
        }

        # US 종목: KRW 기준 수익률 추가
        if rec["type"] == "US":
            entry_usdkrw = rec.get("entry_usdkrw")
            if entry_usdkrw and current_usdkrw:
                entry_krw = entry * entry_usdkrw
                current_krw = current_price * current_usdkrw
                row["return_pct_krw"] = (current_krw - entry_krw) / entry_krw * 100
                row["entry_usdkrw"] = entry_usdkrw
                row["current_usdkrw"] = current_usdkrw
            else:
                row["return_pct_krw"] = ret_pct  # 환율 없으면 USD와 동일

        rows.append(row)
        time.sleep(0.1)

    return rows


def _save_performance_cache(rows: list, today_str: str) -> None:
    """대시보드용 성과 데이터 캐시 저장."""
    # 날짜별로 그룹화
    by_date = {}
    for r in rows:
        d = r["date"]
        if d not in by_date:
            by_date[d] = []
        by_date[d].append(r)

    # 날짜별 가중 평균 수익률 계산
    report_summaries = []
    for d, stocks in sorted(by_date.items()):
        weighted_sum = 0.0
        total_weight = 0.0
        
        for s in stocks:
            ret = s.get("return_pct_krw", s["return_pct"])
            w_str = s.get("weight", "0").replace("%", "").strip()
            try:
                w_val = float(w_str) / 100.0 if w_str else 0.0
            except ValueError:
                w_val = 0.0
            
            weighted_sum += ret * w_val
            total_weight += w_val
        
        # 가중 평균 수익률 (현금 비중은 0% 수익률로 처리)
        # 즉, 전체 비중이 100% 미만이면 나머지는 현금(0% 수익률)이므로 weighted_sum 자체가 포트폴리오 수익률임.
        if total_weight > 1.0:
            portfolio_return = weighted_sum / total_weight
        else:
            portfolio_return = weighted_sum
            
        report_summaries.append({
            "date": d,
            "avg_return_krw": round(portfolio_return, 2),
            "stock_count": len(stocks),
            "stocks": stocks,
        })

    cache = {
        "updated": today_str,
        "report_summaries": report_summaries,
        "all_rows": rows,
    }

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


# ─── 마크다운 섹션 포맷 ───────────────────────────────────────────────────────

def _format_no_history(today_str: str) -> str:
    return (
        "\n\n---\n\n"
        "## 📈 누적 수익률 추적\n\n"
        f"> 오늘({today_str})이 첫 기록입니다. "
        "다음 리포트부터 수익률이 표시됩니다.\n"
    )


def _format_performance(rows: list, today_str: str) -> str:
    if not rows:
        return (
            "\n\n---\n\n"
            "## 📈 누적 수익률 추적\n\n"
            "> 주가 조회 실패 또는 기록 없음\n"
        )

    rows_sorted = sorted(rows, key=lambda x: x.get("return_pct_krw", x["return_pct"]), reverse=True)

    lines = [
        "", "", "---", "",
        "## 📈 누적 수익률 추적",
        f"*기준일: {today_str} | 미국 주식은 원화 환산 기준*",
        "",
        "| 종목 | 추천일 | 진입가 | 현재가 | 비중 | 수익률(KRW) | 판단 |",
        "|------|--------|--------|--------|------|-------------|------|",
    ]

    # 날짜별로 그룹화하여 가중 평균 계산
    by_date = {}
    for r in rows:
        d = r["date"]
        if d not in by_date:
            by_date[d] = []
        by_date[d].append(r)
        
    # 모든 날짜(회차)의 가중 평균 수익률들을 계산하여 평균을 냄
    weighted_returns_by_date = []
    for d, stocks in by_date.items():
        weighted_sum = 0.0
        total_weight = 0.0
        for s in stocks:
            ret = s.get("return_pct_krw", s["return_pct"])
            w_str = s.get("weight", "0").replace("%", "").strip()
            try:
                w_val = float(w_str) / 100.0 if w_str else 0.0
            except ValueError:
                w_val = 0.0
            weighted_sum += ret * w_val
            total_weight += w_val
        
        if total_weight > 1.0:
            weighted_returns_by_date.append(weighted_sum / total_weight)
        else:
            weighted_returns_by_date.append(weighted_sum)
            
    # 전체 회차의 평균 포트폴리오 수익률
    overall_portfolio_return = sum(weighted_returns_by_date) / len(weighted_returns_by_date) if weighted_returns_by_date else 0.0

    for r in rows_sorted:
        ret = r.get("return_pct_krw", r["return_pct"])
        sign = "▲" if ret >= 0 else "▼"
        emoji = "🟢" if ret >= 5 else ("🔴" if ret <= -5 else "🟡")
        cur = "₩" if r["type"] == "KR" else "$"
        weight = r.get("weight", "0%")
        lines.append(
            f"| {r['name']} | {r['date']} | "
            f"{cur}{r['entry_price']:,.0f} | "
            f"{cur}{r['current_price']:,.0f} | "
            f"{weight} | "
            f"{emoji} {sign}{abs(ret):.1f}% | "
            f"{r['action']} |"
        )

    s = "▲" if overall_portfolio_return >= 0 else "▼"
    lines.append(f"| **포트폴리오 누적 수익률** | — | — | — | — | **{s}{abs(overall_portfolio_return):.1f}%** | — |")

    lines += [
        "",
        "> ⚠️ 실제 펀드 운용과 동일하게 각 회차별 추천 비중(현금 비중 포함)을 반영한 **포트폴리오 가중 평균 수익률**입니다.",
        "> 미국 주식: 추천일 환율 vs 현재 환율 적용.",
    ]

    return "\n".join(lines)


# ─── 메인 함수 ────────────────────────────────────────────────────────────────

def update_and_get_performance(report_text: str, today: datetime) -> str:
    """
    이번 리포트 추천 종목을 히스토리에 기록하고,
    누적 수익률 마크다운 섹션을 반환.
    performance_cache.json도 저장 (대시보드용).
    """
    if not YFINANCE_AVAILABLE:
        return "\n\n---\n\n## 📈 누적 수익률 추적\n\n> `yfinance` 미설치\n"

    today_str = today.strftime("%Y-%m-%d")
    portfolio = parse_portfolio_from_report(report_text)
    history = _load_history()

    # ── 이번 추천 진입가 + 환율 기록 ─────────────────────────────────────────
    print("  📌 이번 추천 종목 진입가 기록 중...")
    current_usdkrw = _get_usdkrw()
    new_entries = []

    for stock in portfolio["kr"]:
        code = stock["code"]
        yahoo_ticker = _resolve_kr_ticker(code)
        entry_price = _get_price_on_date(yahoo_ticker, today_str) if yahoo_ticker else None
        new_entries.append({
            "type": "KR",
            "name": stock["name"],
            "code": code,
            "ticker": yahoo_ticker,
            "action": stock["action"],
            "weight": stock["weight"],
            "date": today_str,
            "entry_price": entry_price,
        })
        print(f"     KR: {stock['name']} → {yahoo_ticker}, ₩{entry_price:,.0f}" if entry_price else f"     KR: {stock['name']} → 가격 조회 실패")

    for stock in portfolio["us"]:
        ticker = stock["ticker"]
        entry_price = _get_price_on_date(ticker, today_str)
        new_entries.append({
            "type": "US",
            "name": stock["name"],
            "ticker": ticker,
            "action": stock["action"],
            "weight": stock["weight"],
            "date": today_str,
            "entry_price": entry_price,
            "entry_usdkrw": current_usdkrw,
        })
        krw_price = f"₩{entry_price * current_usdkrw:,.0f}" if entry_price and current_usdkrw else "환율 없음"
        print(f"     US: {stock['name']} → ${entry_price:,.2f} ({krw_price})" if entry_price else f"     US: {stock['name']} → 가격 조회 실패")
        time.sleep(0.3)

    if current_usdkrw:
        print(f"  💱 현재 USD/KRW: {current_usdkrw:,.1f}")

    history["records"] = [r for r in history["records"] if r["date"] != today_str]
    history["records"].extend(new_entries)
    history = _prune_old_records(history)
    _save_history(history)
    print(f"  💾 히스토리 저장 완료 (총 {len(history['records'])}건)")

    # ── 과거 수익률 계산 + 캐시 저장 ─────────────────────────────────────────
    print("  📊 과거 추천 수익률 계산 중...")
    perf_rows = calculate_performance_rows(history, today_str)
    _save_performance_cache(perf_rows, today_str)
    print(f"  → 수익률 계산 완료: {len(perf_rows)}종목")

    if not perf_rows:
        return _format_no_history(today_str)

    return _format_performance(perf_rows, today_str)


# ─── 직접 실행 테스트 ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    sample = """
## 📊 포트폴리오 추천

### 🇰🇷 국내주식 (한국)

| 종목명 | 코드 | 판단 | 목표비중 | 핵심 근거 |
|--------|------|------|----------|-----------|
| 삼성전자 | 005930 | 매수 | 20% | AI 반도체 수혜 |
| HD현대중공업 | 329180 | 매수 | 15% | 조선업 슈퍼사이클 |

### 🇺🇸 해외주식 (미국)

| 종목명 | 티커 | 판단 | 목표비중 | 핵심 근거 |
|--------|------|------|----------|-----------|
| Nvidia | NVDA | Buy | 25% | AI 인프라 핵심 |
| ExxonMobil | XOM | Buy | 10% | 에너지 헤지 |
"""
    section = update_and_get_performance(sample, datetime.now())
    print(section)
