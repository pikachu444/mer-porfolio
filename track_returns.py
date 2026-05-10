"""
track_returns.py
포트폴리오 추천 누적 수익률 추적 모듈

매 리포트 실행 시:
  1. 이번 리포트 추천 종목을 파싱 → 진입가(당일 종가) 기록
  2. 과거 추천 종목의 현재가를 조회해 수익률 계산
  3. 리포트 하단에 붙일 "📈 누적 수익률 추적" 섹션 반환

데이터 저장:
  output/portfolio_history.json — 누적 추천/가격 기록

야후파이낸스 한국 주식 티커 형식:
  KOSPI:  {6자리코드}.KS  예: 005930.KS (삼성전자)
  KOSDAQ: {6자리코드}.KQ  예: 035720.KQ (카카오)
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
MAX_HISTORY_MONTHS = 12  # 12개월 이전 레코드 자동 삭제


# ─── 리포트 파싱 ──────────────────────────────────────────────────────────────

def parse_portfolio_from_report(report_text: str) -> dict:
    """
    마크다운 리포트에서 종목 추천 테이블 파싱.

    Returns:
        {
          "kr": [{"name": "한국조선해양", "code": "009540",
                  "action": "매수", "weight": "15%"}],
          "us": [{"name": "Nvidia", "ticker": "NVDA",
                  "action": "Buy",  "weight": "20%"}]
        }
    """
    result = {"kr": [], "us": []}

    # 국내주식 섹션 파싱 (🇰🇷 또는 국내주식)
    kr_match = re.search(
        r"(?:🇰🇷|국내주식)[^\n]*\n\|[^\n]+\|\n\|[-| :]+\|\n((?:\|[^\n]+\|\n?)+)",
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

    # 해외주식 섹션 파싱 (🇺🇸 또는 해외주식)
    us_match = re.search(
        r"(?:🇺🇸|해외주식)[^\n]*\n\|[^\n]+\|\n\|[-| :]+\|\n((?:\|[^\n]+\|\n?)+)",
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
    """
    6자리 코드로 야후파이낸스 티커 반환.
    KOSPI(.KS) → KOSDAQ(.KQ) 순으로 시도.
    """
    if not YFINANCE_AVAILABLE:
        return None
    for suffix in [".KS", ".KQ"]:
        ticker = code + suffix
        try:
            fast = yf.Ticker(ticker).fast_info
            price = getattr(fast, "last_price", None)
            if price and price > 0:
                return ticker
        except Exception:
            pass
        time.sleep(0.2)
    return None


def _get_price_on_date(ticker: str, date_str: str) -> Optional[float]:
    """
    특정 날짜(YYYY-MM-DD) 종가 반환.
    해당일 거래 없으면 ±5영업일 내 가장 가까운 날짜 사용.
    """
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
    """현재가(장중 실시간 or 전일 종가) 반환."""
    if not YFINANCE_AVAILABLE:
        return None
    try:
        fast = yf.Ticker(ticker).fast_info
        price = getattr(fast, "last_price", None)
        return float(price) if price and price > 0 else None
    except Exception:
        return None


# ─── 히스토리 파일 관리 ───────────────────────────────────────────────────────

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
    """MAX_HISTORY_MONTHS 이전 오래된 레코드 삭제."""
    cutoff = datetime.now() - timedelta(days=MAX_HISTORY_MONTHS * 30)
    history["records"] = [
        r for r in history["records"]
        if datetime.strptime(r["date"], "%Y-%m-%d") >= cutoff
    ]
    return history


# ─── 성과 섹션 포맷 ───────────────────────────────────────────────────────────

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
            "> 주가 조회 실패 또는 기록 없음 — 다음 실행 시 재시도됩니다.\n"
        )

    rows.sort(key=lambda x: x["return_pct"], reverse=True)

    lines = [
        "",
        "",
        "---",
        "",
        "## 📈 누적 수익률 추적",
        f"*기준일: {today_str}*",
        "",
        "| 종목 | 추천일 | 진입가 | 현재가 | 수익률 | 판단 |",
        "|------|--------|--------|--------|--------|------|",
    ]

    total_ret = 0.0
    count = 0

    for r in rows:
        pct = r["return_pct"]
        sign = "▲" if pct >= 0 else "▼"
        emoji = "🟢" if pct >= 5 else ("🔴" if pct <= -5 else "🟡")
        cur = "₩" if r["type"] == "KR" else "$"
        lines.append(
            f"| {r['name']} | {r['date']} | "
            f"{cur}{r['entry_price']:,.0f} | "
            f"{cur}{r['current_price']:,.0f} | "
            f"{emoji} {sign}{abs(pct):.1f}% | "
            f"{r['action']} |"
        )
        total_ret += pct
        count += 1

    if count:
        avg = total_ret / count
        avg_sign = "▲" if avg >= 0 else "▼"
        lines.append(
            f"| **평균 ({count}종목)** | — | — | — | "
            f"**{avg_sign}{abs(avg):.1f}%** | — |"
        )

    lines += [
        "",
        "> ⚠️ 단순 가격 변동 기준. 배당·환율·세금·슬리피지 미반영.",
        "> 추천 후 첫 거래일 종가 기준으로 수익률 계산.",
    ]

    return "\n".join(lines)


# ─── 메인 함수 ────────────────────────────────────────────────────────────────

def update_and_get_performance(report_text: str, today: datetime) -> str:
    """
    이번 리포트 추천 종목을 히스토리에 기록하고,
    과거 추천의 누적 수익률 섹션(마크다운 문자열)을 반환.

    Args:
        report_text: 이번 리포트 전체 텍스트
        today:       실행 기준 datetime

    Returns:
        리포트 하단에 붙일 마크다운 섹션 문자열
    """
    if not YFINANCE_AVAILABLE:
        return (
            "\n\n---\n\n"
            "## 📈 누적 수익률 추적\n\n"
            "> `yfinance` 미설치 — `pip install yfinance` 후 재실행\n"
        )

    today_str = today.strftime("%Y-%m-%d")
    portfolio = parse_portfolio_from_report(report_text)
    history = _load_history()

    # ── 이번 추천 진입가 기록 ────────────────────────────────────────────────
    print("  📌 이번 추천 종목 진입가 기록 중...")
    new_entries = []

    for stock in portfolio["kr"]:
        code = stock["code"]
        print(f"     KR: {stock['name']} ({code}) — 티커 조회 중...")
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
        print(f"       → ticker={yahoo_ticker}, price={entry_price}")

    for stock in portfolio["us"]:
        ticker = stock["ticker"]
        print(f"     US: {stock['name']} ({ticker}) — 가격 조회 중...")
        entry_price = _get_price_on_date(ticker, today_str)
        new_entries.append({
            "type": "US",
            "name": stock["name"],
            "ticker": ticker,
            "action": stock["action"],
            "weight": stock["weight"],
            "date": today_str,
            "entry_price": entry_price,
        })
        print(f"       → price={entry_price}")
        time.sleep(0.3)

    # 오늘 날짜 중복 레코드 교체 후 저장
    history["records"] = [r for r in history["records"] if r["date"] != today_str]
    history["records"].extend(new_entries)
    history = _prune_old_records(history)
    _save_history(history)
    print(f"  💾 히스토리 저장 완료 (총 {len(history['records'])}건)")

    # ── 과거 추천 수익률 계산 ─────────────────────────────────────────────────
    past = [r for r in history["records"] if r["date"] < today_str]
    if not past:
        return _format_no_history(today_str)

    print("  📊 과거 추천 수익률 계산 중...")
    perf_rows = []

    for rec in past:
        if not rec.get("entry_price") or not rec.get("ticker"):
            continue
        current = _get_current_price(rec["ticker"])
        if current is None:
            continue
        ret = (current - rec["entry_price"]) / rec["entry_price"] * 100
        perf_rows.append({
            "name": rec["name"],
            "ticker": rec["ticker"],
            "type": rec["type"],
            "date": rec["date"],
            "entry_price": rec["entry_price"],
            "current_price": current,
            "return_pct": ret,
            "action": rec["action"],
        })
        time.sleep(0.1)

    print(f"  → 수익률 계산 완료: {len(perf_rows)}종목")
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
