"""
generate_dashboard.py
HTML 대시보드 + PNG 차트 생성 모듈

생성 파일:
  output/dashboard.html  — Chart.js 기반 인터랙티브 대시보드
  output/chart_latest.png — 텔레그램 전송용 요약 차트 (matplotlib)
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from portfolio_output import build_output_model
from portfolio_schema import load_portfolio_state_file

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "output"))
CACHE_FILE = OUTPUT_DIR / "performance_cache.json"
DASHBOARD_FILE = OUTPUT_DIR / "dashboard.html"
PNG_FILE = OUTPUT_DIR / "chart_latest.png"


def _load_cache() -> Optional[dict]:
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def _weights_from_state(state: dict, cache: Optional[dict] = None) -> list:
    """실제비중이 있으면 실제, 없으면 명시적으로 목표비중을 그린다."""
    output = build_output_model(state, cache or {})
    use_actual = bool(output.get("actual_allocation_available"))
    result = [
        {
            "name": item.get("name", ""),
            "weight": float(item.get("actual_weight")) if use_actual else float(item.get("target_weight", 0)),
            "target_weight": float(item.get("target_weight", 0)),
            "basis": "actual" if use_actual else "target",
            "action": item.get("today_action", "유지"),
            "market": item.get("market", ""),
        }
        for item in output.get("portfolio", [])
        if (float(item.get("actual_weight")) if use_actual and item.get("actual_weight") is not None else float(item.get("target_weight", 0))) > 0
    ]
    cash_weight = (
        float(output.get("actual_cash_weight"))
        if use_actual and output.get("actual_cash_weight") is not None
        else float(output.get("target_cash_weight", 0))
    )
    if cash_weight:
        result.append({
            "name": "현금",
            "weight": cash_weight,
            "target_weight": float(output.get("target_cash_weight", 0)),
            "basis": "actual" if use_actual else "target",
            "action": "유지",
            "market": "ETF",
        })
    return result


def _load_portfolio_state() -> dict:
    """portfolio_state.json을 v2 구조로 읽는다. 기존 파일은 메모리에서 변환한다."""
    state_path = OUTPUT_DIR / "portfolio_state.json"
    if not state_path.exists():
        return {}
    try:
        return load_portfolio_state_file(state_path).to_dict()
    except Exception:
        return {}


def _donut_color(idx: int) -> str:
    """종목별 고대비 12색 팔레트 — 인접 종목이 확실히 구분되도록."""
    palette = [
        "#ef4444",  # 선명한 빨강
        "#3b82f6",  # 파랑
        "#22c55e",  # 초록
        "#f97316",  # 주황
        "#a855f7",  # 보라
        "#06b6d4",  # 시안
        "#eab308",  # 노랑
        "#ec4899",  # 핑크
        "#14b8a6",  # 틸
        "#f59e0b",  # 앰버
        "#6366f1",  # 인디고
        "#84cc16",  # 라임
    ]
    return palette[idx % len(palette)]


def _market_border_color(market: str) -> str:
    """마켓별 구분 테두리색 (범례 섹션 헤더용)."""
    m = market.upper()
    if "KR" in m:
        return "#60a5fa"
    elif "US" in m or m in ("USD", "NYSE", "NASDAQ"):
        return "#4ade80"
    else:
        return "#fbbf24"


def generate_png(
    cache: dict,
    report_text: str = "",
    today_str: str = "",
    state: Optional[dict] = None,
) -> Optional[Path]:
    """
    PNG 차트 생성 (텔레그램 전송용).

    항상 생성:
      도넛 차트 — 최신 검증 리포트의 추천 비중
                  (KR=파랑, US=초록, ETF/현금=주황)
    데이터 있을 때 추가:
      누적 수익률 라인 — performance_cache.json report_summaries
    레이아웃:
      도넛만      → 1행
      도넛+수익률 → 2행 (위: 도넛, 아래: 라인)
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        from matplotlib import font_manager
    except ImportError:
        print("  ⚠ matplotlib 미설치 — PNG 생성 스킵")
        return None

    # 한글 폰트 설정
    try:
        windows_font = Path(r"C:\Windows\Fonts\malgun.ttf")
        linux_font = Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf")
        if windows_font.exists():
            font_manager.fontManager.addfont(str(windows_font))
            plt.rcParams["font.family"] = "Malgun Gothic"
        else:
            import subprocess
            subprocess.run(["apt-get", "install", "-y", "-q", "fonts-nanum"], capture_output=True)
            if linux_font.exists():
                font_manager.fontManager.addfont(str(linux_font))
                plt.rcParams["font.family"] = "NanumGothic"
            else:
                plt.rcParams["font.family"] = "DejaVu Sans"
    except Exception:
        plt.rcParams["font.family"] = "DejaVu Sans"
    plt.rcParams["axes.unicode_minus"] = False

    # ── 도넛 데이터 준비 ───────────────────────────────────────────────
    donut_items = []
    if state:
        donut_items = _weights_from_state(state, cache)

    # ── 수익률 데이터 준비 ─────────────────────────────────────────────
    summaries = cache.get("report_summaries", []) if cache else []
    has_returns = len(summaries) >= 1

    # ── 레이아웃 결정 ───────────────────────────────────────────────
    fig_rows = 2 if (donut_items and has_returns) else 1
    fig_h    = 5.5 if fig_rows == 1 else 10
    fig, axes = plt.subplots(fig_rows, 1, figsize=(9, fig_h))
    fig.patch.set_facecolor("#0f172a")
    if fig_rows == 1:
        axes = [axes]

    # ── 차트 1: 도넓 차트 ───────────────────────────────────────────────
    ax1 = axes[0]
    ax1.set_facecolor("#0f172a")

    if donut_items:
        wedge_vals   = [d["weight"] for d in donut_items]
        wedge_colors = [_donut_color(i) for i, d in enumerate(donut_items)]
        total_w      = sum(wedge_vals)

        ax1.pie(
            wedge_vals,
            colors=wedge_colors,
            wedgeprops=dict(width=0.52, edgecolor="#0f172a", linewidth=1.8),
            startangle=90,
            counterclock=False,
        )
        ax1.text(0, 0, f"\ud569\uacc4\n{total_w:.0f}%",
                 ha="center", va="center", color="#f1f5f9",
                 fontsize=11, fontweight="bold")

        # 마켓별 섹션으로 범례 구성
        from collections import OrderedDict
        market_order = ["KR", "US", "ETF"]
        market_label = {
            "KR":  "KR (\uad6d\ub0b4)",
            "US":  "US (\ud574\uc678)",
            "ETF": "ETF / \ud604\uae08",
        }
        def _get_market_key(m):
            mu = m.upper()
            if "KR" in mu:   return "KR"
            if "US" in mu or mu in ("USD","NYSE","NASDAQ"): return "US"
            return "ETF"

        grouped = OrderedDict()
        for mk in market_order:
            grouped[mk] = []
        for i, d in enumerate(donut_items):
            mk = _get_market_key(d["market"])
            grouped.setdefault(mk, []).append((i, d))

        all_handles = []
        for mk, items in grouped.items():
            if not items:
                continue
            # 섹션 헤더 (빈 패치 + 굵은 텍스트)
            header = mpatches.Patch(color="none", label=market_label.get(mk, mk))
            all_handles.append(header)
            for i, d in items:
                patch = mpatches.Patch(
                    color=wedge_colors[i],
                    label=f"  {d['name']}  {d['weight']:.0f}%"
                )
                all_handles.append(patch)

        leg = ax1.legend(
            handles=all_handles,
            loc="center left", bbox_to_anchor=(0.90, 0.5),
            fontsize=8.5, framealpha=0.15,
            facecolor="#1e293b", edgecolor="#334155",
            labelcolor="#f1f5f9",
            handlelength=1.0, handleheight=1.1,
            borderpad=0.8, labelspacing=0.55,
        )
        # 섹션 헤더 텍스트만 볼드 + 밝은 색
        legend_texts = leg.get_texts()
        hi = 0
        for mk, items in grouped.items():
            if not items:
                continue
            legend_texts[hi].set_color("#94a3b8")
            legend_texts[hi].set_fontsize(7.5)
            hi += 1 + len(items)

        basis = "실제" if all(item.get("basis") == "actual" for item in donut_items) else "목표"
        ax1.set_title(f"{basis} 자산배분", color="#f1f5f9", fontsize=12, pad=10)
    else:
        ax1.text(0.5, 0.5,
                 "최신 추천 비중 없음\n리포트 표를 확인하세요",
                 ha="center", va="center", color="#94a3b8", fontsize=11,
                 transform=ax1.transAxes, linespacing=1.8)
        ax1.set_title("자산배분 데이터 없음", color="#f1f5f9", fontsize=12)
        ax1.axis("off")

    # ── 차트 2: 누적 수익률 라인 ───────────────────────────────────────
    if has_returns and fig_rows == 2:
        ax2 = axes[1]
        ax2.set_facecolor("#1e293b")
        dates = [s["date"][5:] for s in summaries]
        avgs  = [s["avg_return_krw"] for s in summaries]
        xs    = list(range(len(dates)))

        ax2.plot(xs, avgs, color="#3b82f6", linewidth=2.5,
                 marker="o", markersize=7, markerfacecolor="#3b82f6",
                 markeredgecolor="#0f172a", markeredgewidth=1.5, zorder=3)
        ax2.fill_between(xs, avgs, 0,
                         where=[a >= 0 for a in avgs],
                         color="#22c55e", alpha=0.12)
        ax2.fill_between(xs, avgs, 0,
                         where=[a < 0 for a in avgs],
                         color="#ef4444", alpha=0.12)
        ax2.axhline(0, color="#94a3b8", linewidth=0.8, linestyle="--")
        ax2.set_xticks(xs)
        ax2.set_xticklabels(dates, color="#94a3b8", fontsize=9)
        for i, avg in enumerate(avgs):
            sign  = "+" if avg >= 0 else ""
            color = "#22c55e" if avg >= 0 else "#ef4444"
            ax2.annotate(
                f"{sign}{avg:.1f}%",
                xy=(i, avg),
                xytext=(0, 8 if avg >= 0 else -12),
                textcoords="offset points",
                ha="center",
                va="bottom" if avg >= 0 else "top",
                color=color,
                fontsize=8.5,
                fontweight="bold",
            )
        ax2.set_title("\ub9ac\ud3ec\ud2b8 \ud68c\ucc28\ubcc4 \ud3c9\uade0 \uc218\uc775\ub960 (\uc6d0\ud654 \uae30\uc900)",
                      color="#f1f5f9", fontsize=12, pad=10)
        ax2.set_ylabel("\ud3c9\uade0 \uc218\uc775\ub960 (%)", color="#94a3b8", fontsize=9)
        ax2.tick_params(colors="#94a3b8")
        for spine in ax2.spines.values():
            spine.set_color("#334155")

    plt.suptitle(f"\uba54\ub974AI \ud3ec\ud2b8\ud3f4\ub9ac\uc624  |  {today_str}",
                 color="#f1f5f9", fontsize=13, fontweight="bold", y=1.02)
    plt.tight_layout(pad=2.0)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(PNG_FILE, dpi=150, bbox_inches="tight",
                facecolor="#0f172a", edgecolor="none")
    plt.close(fig)
    print(f"  PNG 차트 생성 완료: {PNG_FILE}")
    return PNG_FILE




def generate_html(
    cache: dict,
    report_text: str,
    today_str: str,
    state: Optional[dict] = None,
    approved_changes: bool | None = None,
) -> Path:
    """모바일에서도 현재 보유·오늘 조정·상세 근거를 분리해 보여준다."""
    state = state or _load_portfolio_state()
    output = build_output_model(
        state,
        cache or {},
        today_str=today_str,
        approved_changes=approved_changes,
    )
    watchlist = output["watchlist"]
    closed_positions = output["closed_positions"]
    insights = output["insights"]
    deferred_posts = output.get("deferred_posts", [])
    summaries = cache.get("report_summaries", []) if cache else []
    portfolio_rows = output["portfolio"]
    change_rows = output.get("approved_today_changes", output.get("today_changes", []))
    drift_rows = output.get("today_changes", [])
    chart_rows = output["chart_rows"]
    report_escaped = report_text.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
    chart_basis = "실제" if output.get("actual_allocation_available") else "목표"
    html = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>메르AI 모델 포트폴리오</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.0/marked.min.js"></script>
<style>
:root{--bg:#0f172a;--card:#1e293b;--border:#334155;--text:#f1f5f9;--muted:#94a3b8;--blue:#60a5fa}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:'Segoe UI',sans-serif;font-size:16px;line-height:1.5}header{padding:18px 24px;background:var(--card);border-bottom:1px solid var(--border)}h1{font-size:1.35rem;margin:0 0 6px}.notice,.muted{color:var(--muted);font-size:.9rem}.container{max-width:1120px;margin:auto;padding:20px 14px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:18px;margin-bottom:16px}.card h2{font-size:1.1rem;margin:0 0 14px}.chart{height:280px}.table-wrap{overflow-x:auto}.data-table{width:100%;border-collapse:collapse;font-size:.9rem}.data-table th,.data-table td{padding:10px;border:1px solid var(--border);text-align:left;vertical-align:top}.data-table th{color:var(--muted);background:#0f172a}.pill{display:inline-block;padding:3px 8px;border-radius:999px;border:1px solid var(--border)}.detail{display:none;background:#0f172a}.detail.open{display:table-row}.detail td{line-height:1.7}.changed td{background:#172554}.toolbar{display:flex;gap:8px;margin-bottom:8px}button{background:#0f172a;color:#cbd5e1;border:1px solid var(--border);border-radius:6px;padding:8px 10px;min-height:40px;cursor:pointer}.empty{color:var(--muted);padding:8px 0}a{color:var(--blue)}#report-content{font-size:.95rem;line-height:1.7}#report-content table{width:100%;border-collapse:collapse}#report-content th,#report-content td{padding:8px;border:1px solid var(--border)}
@media(max-width:640px){header{padding:16px 12px}.container{padding:12px 8px}.card{padding:14px}.grid{display:block}.chart{height:240px}.table-wrap{overflow-x:visible}.data-table{font-size:.92rem;display:block}.data-table thead{display:none}.data-table tbody,.data-table tr{display:block}.data-table tr:not(.detail){border:1px solid var(--border);margin-bottom:8px;border-radius:8px;padding:4px}.data-table td{display:block;border:0;padding:5px 8px}.data-table td:first-child{font-weight:700}.detail.open{display:block}.detail td{padding:10px}.desktop{display:none}}
</style></head><body>
<header><h1>메르AI 모델 포트폴리오</h1><div class="notice">메르 블로그 공개 분석을 바탕으로 구성한 참고용 모델 포트폴리오입니다.</div><div class="muted">업데이트: <span id="updated"></span></div></header>
<main class="container">
<section class="card"><h2>오늘의 요약</h2><div id="summary"></div></section>
<section class="card" id="deferred-card"><h2>오늘 분석에서 제외된 글</h2><div id="deferred"></div></section>
<section class="card"><h2>핵심 인사이트</h2><div id="insights"></div></section>
<div class="grid"><section class="card"><h2 id="allocation-title"></h2><div class="chart"><canvas id="donut"></canvas></div></section><section class="card"><h2>포트폴리오 수익률 흐름</h2><div class="chart"><canvas id="returns"></canvas></div></section></div>
<section class="card"><h2>현재 보유 종목</h2><div id="portfolio"></div></section>
<section class="card"><h2>오늘의 조정</h2><div id="changes"></div></section>
<section class="card"><h2>관심종목</h2><div id="watchlist"></div></section>
<section class="card"><h2>과거 편출 종목</h2><div id="closed"></div></section>
<section class="card"><h2>상세 보고서</h2><div id="report-content"></div></section>
</main><script>
""" + (
        "const updated=" + json.dumps(today_str) + ";\n"
        "const portfolio=" + json.dumps(portfolio_rows, ensure_ascii=False) + ";\n"
        "const changes=" + json.dumps(change_rows, ensure_ascii=False) + ";\n"
        "const driftRows=" + json.dumps(drift_rows, ensure_ascii=False) + ";\n"
        "const actionsDeferred=" + json.dumps(bool(output.get("actions_deferred")), ensure_ascii=False) + ";\n"
        "const watchlist=" + json.dumps(watchlist, ensure_ascii=False) + ";\n"
        "const closed=" + json.dumps(closed_positions, ensure_ascii=False) + ";\n"
        "const insights=" + json.dumps(insights, ensure_ascii=False) + ";\n"
        "const deferredPosts=" + json.dumps(deferred_posts, ensure_ascii=False) + ";\n"
        "const chartRows=" + json.dumps(chart_rows, ensure_ascii=False) + ";\n"
        "const summaries=" + json.dumps(summaries, ensure_ascii=False) + ";\n"
        "const statusNote=" + json.dumps(output.get("status_note", ""), ensure_ascii=False) + ";\n"
        "const stockWeight=" + json.dumps(output.get("target_stock_weight", 0), ensure_ascii=False) + ";\n"
        "const etfWeight=" + json.dumps(output.get("target_etf_weight", 0), ensure_ascii=False) + ";\n"
        "const cashWeight=" + json.dumps(output.get("target_cash_weight", 0), ensure_ascii=False) + ";\n"
        "const actualStockWeight=" + json.dumps(output.get("actual_stock_weight"), ensure_ascii=False) + ";\n"
        "const actualEtfWeight=" + json.dumps(output.get("actual_etf_weight"), ensure_ascii=False) + ";\n"
        "const actualCashWeight=" + json.dumps(output.get("actual_cash_weight"), ensure_ascii=False) + ";\n"
        "const allocationBasis=" + json.dumps(chart_basis, ensure_ascii=False) + ";\n"
        "const reportText=`" + report_escaped + "`;\n"
    ) + """
document.getElementById('updated').textContent=updated;
document.getElementById('allocation-title').textContent=`${allocationBasis} 자산배분`;
const esc=v=>String(v??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const evidence=r=>(r.display_evidence||r.evidence_posts||[]).map(p=>p.url?`<a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.title||'원문')}</a> · ${esc(p.published_date||'')}`:esc(p.title||'원문')).join('<br>')||'근거 링크 없음';
const weight=v=>v===null||v===undefined?'데이터 없음':`${Number(v).toFixed(2)}%`;
const buttons=id=>`<div class="toolbar"><button onclick="toggleAll('${id}',true)">모두 펼치기</button><button onclick="toggleAll('${id}',false)">모두 접기</button></div>`;
function toggle(id){document.getElementById(id).classList.toggle('open')}
function toggleAll(id,open){document.querySelectorAll(`#${id} .detail`).forEach(el=>el.classList.toggle('open',open))}
function detail(r,kind){if(kind==='watchlist')return `최근 확인일: ${esc(r.latest_material_signal_date||r.latest_evidence_date||'-')}<br>관찰 이유: ${esc(r.observation_reason||'조건을 추적합니다.')}<br>근거: ${evidence(r)}`;const reason=r.display_reason||r.change_reason||r.close_reason||'승인된 투자 논리를 추적합니다.';return `판단일: ${esc(r.decision_date||r.closed_date||'-')}<br>역할: ${esc(r.allocation_role_label||r.allocation_role||'-')}<br>근거 유형: ${esc(r.basis||'-')}<br>투자 논리: ${esc(reason)}<br>근거: ${evidence(r)}`}
function table(id,rows,kind){if(!rows.length)return '<div class="empty">표시할 항목이 없습니다.</div>';const body=rows.map((r,i)=>{const key=`${id}-${i}`;const changed=kind==='changes'?'changed':'';let cells='';if(kind==='portfolio'||kind==='changes'){cells=`<td>${esc(r.name)}<br><span class="muted">${esc(r.code)}</span></td><td>실제 ${weight(r.actual_weight)}<br>목표 ${weight(r.target_weight)}</td><td><span class="pill">${esc(r.display_today_action||r.today_action||'유지')}</span></td><td>${esc(r.allocation_role_label||'-')}</td><td>${esc(r.return_label||'데이터 없음')}</td>`}else if(kind==='watchlist'){cells=`<td>${esc(r.name)}<br><span class="muted">${esc(r.code)}</span></td><td>${esc(r.status||'관심')}</td><td>${esc(r.latest_material_signal_date||'-')}</td>`}else{cells=`<td>${esc(r.name)}<br><span class="muted">${esc(r.code)}</span></td><td>${esc(r.closed_date||'-')}</td><td>${esc(r.close_reason||'과거 편출')}</td>`}return `<tr class="${changed}">${cells}<td><button onclick="toggle('${key}')">상세</button></td></tr><tr id="${key}" class="detail"><td colspan="8">${detail(r,kind)}</td></tr>`}).join('');let head=kind==='watchlist'?'<th>종목</th><th>상태</th><th>최근 확인</th><th>상세</th>':kind==='closed'?'<th>종목</th><th>편출일</th><th>사유</th><th>상세</th>':'<th>종목</th><th>비중</th><th>오늘 상태</th><th>역할</th><th>수익률</th><th>상세</th>';return buttons(id)+`<div class="table-wrap"><table class="data-table" id="${id}"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`}
function deferredTable(rows){if(!rows.length){document.getElementById('deferred-card').style.display='none';return ''}return `<div class="notice">요약이 준비되지 않은 글은 오늘 판단에서 제외하고 다음 실행에서 다시 확인합니다.</div><ul>${rows.map(r=>`<li>${esc(r.title||'제목 없음')} ${r.url?`<a href="${esc(r.url)}" target="_blank" rel="noopener">원문</a>`:''}</li>`).join('')}</ul>`}
const allocationActual=actualStockWeight!==null&&actualEtfWeight!==null&&actualCashWeight!==null;
document.getElementById('summary').innerHTML=`누적 수익률: ${esc(""" + json.dumps(output.get("portfolio_return_label", "데이터 없음"), ensure_ascii=False) + """)} · 최대 낙폭: ${esc(""" + json.dumps(output.get("max_drawdown_label", "데이터 없음"), ensure_ascii=False) + """)}<br>기준 포트폴리오 대비: ${esc(""" + json.dumps(output.get("benchmark_difference_label", "데이터 없음"), ensure_ascii=False) + """)} (수익률 차이)<br>자산배분(${allocationActual?'실제':'목표'}): 개별주 ${weight(allocationActual?actualStockWeight:stockWeight)} / 주식형 ETF ${weight(allocationActual?actualEtfWeight:etfWeight)} / 현금성 ${weight(allocationActual?actualCashWeight:cashWeight)}<br>오늘 승인된 비중 변경: ${changes.length?'있음':'없음'}${statusNote?`<br><strong>${esc(statusNote)}</strong>`:''}`;
document.getElementById('deferred').innerHTML=deferredTable(deferredPosts);
document.getElementById('insights').innerHTML=insights.length?insights.map((r,i)=>`<article><h3>${i+1}. ${esc(r.title)}</h3><p>${esc(r.summary)}</p><p><strong>추적할 조건:</strong> ${esc(r.investment_implication)}</p><p class="muted">${evidence(r)}</p></article>`).join(''):'<div class="empty">표시할 인사이트가 없습니다.</div>';
document.getElementById('portfolio').innerHTML=table('portfolio-table',portfolio,'portfolio');
document.getElementById('changes').innerHTML=changes.length?table('changes-table',changes,'changes'):(actionsDeferred&&driftRows.length?'<div class="empty">승인된 매매 없음 · 현재 비중 이탈이 확인됐지만 내부 검증이 끝나지 않아 자동 조정을 보류합니다.</div>':'<div class="empty">승인된 매매 없음 · 전 종목이 허용 범위 안에 있습니다.</div>');
document.getElementById('watchlist').innerHTML=table('watchlist-table',watchlist,'watchlist');
document.getElementById('closed').innerHTML=table('closed-table',closed,'closed');
const colors=['#ef4444','#3b82f6','#22c55e','#f97316','#a855f7','#06b6d4','#eab308','#ec4899','#14b8a6','#f59e0b','#6366f1','#84cc16','#64748b'];const chartable=chartRows.map(r=>({...r,weight:r.weight??r.target_weight}));
new Chart(document.getElementById('donut'),{type:'doughnut',data:{labels:chartable.map(r=>`${r.name} ${weight(r.weight)}`),datasets:[{data:chartable.map(r=>r.weight),backgroundColor:chartable.map((_,i)=>colors[i%colors.length])}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{position:'bottom',labels:{boxWidth:12}}}}});
if(summaries.length){new Chart(document.getElementById('returns'),{type:'line',data:{labels:summaries.map(r=>r.date),datasets:[{label:'모델 포트폴리오 수익률',data:summaries.map(r=>r.avg_return_krw),borderColor:'#60a5fa',tension:.25}]},options:{responsive:true,maintainAspectRatio:false}})}else{document.getElementById('returns').replaceWith(Object.assign(document.createElement('div'),{className:'empty',textContent:'성과 데이터가 아직 없습니다.'}))}
document.getElementById('report-content').innerHTML=reportText.trim()?marked.parse(reportText):'<div class="empty">보고서가 없습니다.</div>';
</script></body></html>"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  HTML 대시보드 생성 완료: {DASHBOARD_FILE}")
    return DASHBOARD_FILE


def generate_all(
    report_text: str,
    today: datetime,
    state: Optional[dict] = None,
    approved_changes: bool | None = None,
) -> tuple:
    """HTML 대시보드 + PNG 차트 모두 생성. Returns: (html_path, png_path)"""
    today_str = today.strftime("%Y-%m-%d")
    cache = _load_cache()
    report_content = report_text or ""

    html_path = None
    png_path = None
    try:
        html_path = generate_html(
            cache or {},
            report_content,
            today_str,
            state=state,
            approved_changes=approved_changes,
        )
    except Exception as e:
        print(f"  ⚠ HTML 대시보드 생성 실패: {e}")
    try:
        # 수익률 데이터 없어도 항상 PNG 생성 (첫 실행 시 목표비중 표시)
        png_path = generate_png(cache or {}, report_content, today_str, state=state)
    except Exception as e:
        print(f"  !! PNG 차트 생성 실패: {e}")
    return html_path, png_path


if __name__ == "__main__":
    html, png = generate_all("# 테스트 리포트\n\n테스트입니다.", datetime.now())
    print(f"HTML: {html}")
    print(f"PNG:  {png}")
