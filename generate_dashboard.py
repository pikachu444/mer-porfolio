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


def _weights_from_state(state: dict) -> list:
    """구조화 상태에서 PNG 도넛 차트용 목표 비중을 만든다."""
    result = [
        {
            "name": item.get("name", ""),
            "weight": float(item.get("proposed_weight", 0)),
            "action": item.get("action", ""),
            "market": item.get("market", ""),
        }
        for item in state.get("portfolio", [])
        if item.get("proposed_weight", 0) > 0
    ]
    cash_weight = max(0.0, 100.0 - sum(item["weight"] for item in result))
    if cash_weight:
        result.append({
            "name": "현금",
            "weight": cash_weight,
            "action": "보유",
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
        donut_items = _weights_from_state(state)

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

        ax1.set_title("최신 추천 포트폴리오 비중", color="#f1f5f9", fontsize=12, pad=10)
    else:
        ax1.text(0.5, 0.5,
                 "최신 추천 비중 없음\n리포트 표를 확인하세요",
                 ha="center", va="center", color="#94a3b8", fontsize=11,
                 transform=ax1.transAxes, linespacing=1.8)
        ax1.set_title("최신 추천 포트폴리오 비중", color="#f1f5f9", fontsize=12)
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
) -> Path:
    """검증된 포트폴리오 상태를 중심으로 HTML 대시보드를 생성한다."""
    state = state or _load_portfolio_state()
    portfolio = state.get("portfolio", [])
    output = build_output_model(
        state,
        cache or {},
        today_str=today_str,
        status_note=(state or {}).get("status_note", ""),
    )
    watchlist = output["watchlist"]
    closed_positions = output["closed_positions"]
    insights = output["insights"]
    history = state.get("decision_history", [])
    latest_changes = [item for item in history if item.get("decision_date") == today_str]
    summaries = cache.get("report_summaries", []) if cache else []
    portfolio_rows = output["portfolio"]
    chart_rows = output["chart_rows"]

    report_escaped = report_text.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
    html = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="UTF-8">
<title>메르AI 모델 포트폴리오</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.0/marked.min.js"></script>
<style>
:root{--bg:#0f172a;--card:#1e293b;--border:#334155;--text:#f1f5f9;--muted:#94a3b8;--green:#22c55e;--red:#ef4444;--blue:#3b82f6;--yellow:#eab308}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:'Segoe UI',sans-serif}header{padding:18px 24px;background:var(--card);border-bottom:1px solid var(--border)}h1{font-size:1.25rem;margin:0 0 6px}.notice,.muted{color:var(--muted);font-size:.82rem}.container{max-width:1120px;margin:auto;padding:20px 14px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:18px;margin-bottom:16px}.card h2{font-size:1rem;margin:0 0 14px}.chart{height:280px}.table-wrap{overflow-x:auto}.data-table{width:100%;border-collapse:collapse;font-size:.82rem}.data-table th,.data-table td{padding:8px;border:1px solid var(--border);text-align:left;vertical-align:top}.data-table th{color:var(--muted);background:#0f172a}.pill{display:inline-block;padding:2px 7px;border-radius:999px;font-size:.72rem;font-weight:700;border:1px solid var(--border)}.actor-mer{color:#fcd34d;border-color:#a16207}.actor-ai{color:#93c5fd;border-color:#1d4ed8}.actor-unknown{color:#cbd5e1}.buy{color:#86efac}.sell{color:#fca5a5}.detail{display:none;background:#0f172a}.detail.open{display:table-row}.detail td{line-height:1.7}.changed td{background:#172554}.toolbar{display:flex;gap:6px;margin-bottom:8px}button{background:#0f172a;color:#cbd5e1;border:1px solid var(--border);border-radius:5px;padding:4px 7px;cursor:pointer}.empty{color:var(--muted);font-size:.85rem;padding:8px 0}a{color:#93c5fd}#report-content{font-size:.88rem;line-height:1.65}#report-content table{width:100%;border-collapse:collapse}#report-content th,#report-content td{padding:7px;border:1px solid var(--border)}
@media(max-width:640px){.container{padding:12px 8px}.card{padding:13px}.desktop{display:none}.data-table{font-size:.76rem}}
</style></head><body>
<header><h1>메르AI 모델 포트폴리오</h1><div class="notice">메르 블로거의 실제 보유 내역이 아닙니다. 블로그 판단과 AI 해석을 구분하여 만든 모델 포트폴리오입니다.</div><div class="muted">업데이트: <span id="updated"></span></div></header>
<main class="container">
<section class="card"><h2>최근 분석 요약</h2><div id="summary"></div></section>
<section class="card"><h2>핵심 인사이트</h2><div id="insights"></div></section>
<div class="grid"><section class="card"><h2>현재 모델 포트폴리오 목표 비중</h2><div class="chart"><canvas id="donut"></canvas></div></section><section class="card"><h2>포트폴리오 수익률 흐름</h2><div class="chart"><canvas id="returns"></canvas></div></section></div>
<section class="card"><h2>국내/해외 추천</h2><div id="recommendations"></div></section>
<section class="card"><h2>현재 모델 포트폴리오</h2><div id="portfolio"></div></section>
<section class="card"><h2>이번 분석 변경사항</h2><div id="changes"></div></section>
<section class="card"><h2>Watchlist</h2><div id="watchlist"></div></section>
<section class="card"><h2>종료 포지션</h2><div id="closed"></div></section>
<section class="card"><h2>전체 보고서</h2><div id="report-content"></div></section>
</main><script>
""" + (
        "const updated=" + json.dumps(today_str) + ";\n"
        "const portfolio=" + json.dumps(portfolio_rows, ensure_ascii=False) + ";\n"
        "const domestic=" + json.dumps(output["domestic"], ensure_ascii=False) + ";\n"
        "const overseas=" + json.dumps(output["overseas"], ensure_ascii=False) + ";\n"
        "const changes=" + json.dumps(latest_changes, ensure_ascii=False) + ";\n"
        "const watchlist=" + json.dumps(watchlist, ensure_ascii=False) + ";\n"
        "const closed=" + json.dumps(closed_positions, ensure_ascii=False) + ";\n"
        "const insights=" + json.dumps(insights, ensure_ascii=False) + ";\n"
        "const chartRows=" + json.dumps(chart_rows, ensure_ascii=False) + ";\n"
        "const summaries=" + json.dumps(summaries, ensure_ascii=False) + ";\n"
        "const statusNote=" + json.dumps(output.get("status_note", ""), ensure_ascii=False) + ";\n"
        "const reportText=`" + report_escaped + "`;\n"
    ) + """
document.getElementById('updated').textContent=updated;
const esc=v=>String(v??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const actorLabel=r=>r.decision_actor==='메르'?'메르 직접 발언':r.decision_actor==='AI'?'AI 제안':'미분류';
const actor=r=>`<span class="pill ${r.decision_actor==='메르'?'actor-mer':r.decision_actor==='AI'?'actor-ai':'actor-unknown'}">${actorLabel(r)} · ${esc(r.action||'')}</span>`;
const evidence=r=>(r.evidence_posts||[]).map(p=>`<a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.title)}</a> · ${esc(p.published_date)}`).join('<br>')||'근거 글 없음';
const returns=r=>{if(r.return_label)return esc(r.return_label);const v=r.return_pct_krw??r.return_pct;if(v===undefined||v===null)return '집계 전';return `${Number(v)>=0?'▲':'▼'}${Math.abs(Number(v)).toFixed(1)}%`};
const buttons=id=>`<div class="toolbar"><button onclick="toggleAll('${id}',true)">모두 펼치기</button><button onclick="toggleAll('${id}',false)">모두 접기</button></div>`;
function toggle(id){document.getElementById(id).classList.toggle('open')}
function toggleAll(id,open){document.querySelectorAll(`#${id} .detail`).forEach(el=>el.classList.toggle('open',open))}
function table(id,rows,kind){
 if(!rows.length)return '<div class="empty">표시할 항목이 없습니다.</div>';
 const body=rows.map((r,i)=>{const key=`${id}-${i}`;const detail=`판단일: ${esc(r.decision_date||r.closed_date||'-')}<br>출처: ${actorLabel(r)}<br>근거 유형: ${esc(r.basis||'-')}<br>비중 출처: ${esc(r.weight_source||'-')}<br>변경 이유: ${esc(r.change_reason||r.observation_reason||r.close_reason||'-')}<br>근거: ${evidence(r)}<br>원문 종목 등장: ${r.source_mentioned===true?'있음':r.source_mentioned===false?'없음':'-'}`;
 return `<tr class="${kind==='changes'?'changed':''}"><td>${esc(r.name)}<br><span class="muted">${esc(r.code)}</span></td><td>${actor(r)}</td><td>${r.proposed_weight===undefined?'-':esc(r.proposed_weight)+'%'}</td><td class="desktop">${esc(r.decision_date||r.closed_date||r.watchlist_entry_date||'-')}</td><td>${kind==='portfolio'?returns(r):esc(r.status||r.close_reason||'')}</td><td><button onclick="toggle('${key}')">펼치기</button></td></tr><tr id="${key}" class="detail"><td colspan="6">${detail}</td></tr>`}).join('');
 return buttons(id)+`<div class="table-wrap"><table class="data-table" id="${id}"><thead><tr><th>종목</th><th>판단</th><th>비중</th><th class="desktop">판단일</th><th>상태/수익률</th><th>상세</th></tr></thead><tbody>${body}</tbody></table></div>`;
}
document.getElementById('summary').innerHTML=`현재 ${portfolio.length}종목 · Watchlist ${watchlist.length}건 · 이번 변경 ${changes.length}건 · 종료 ${closed.length}건${statusNote?`<br><strong>분석 보류:</strong> ${esc(statusNote)}`:''}`;
document.getElementById('insights').innerHTML=insights.length?insights.map((r,i)=>`<article><h3>${i+1}. ${esc(r.title)}</h3><p>${esc(r.summary)}</p><p><strong>투자 시사점:</strong> ${esc(r.investment_implication)}</p><p class="muted">${evidence(r)}</p></article>`).join(''):'<div class="empty">표시할 인사이트가 없습니다.</div>';
document.getElementById('recommendations').innerHTML=`<h3>국내주식 추천</h3>${table('domestic-table',domestic,'portfolio')}<h3>해외주식 추천</h3>${table('overseas-table',overseas,'portfolio')}`;
document.getElementById('portfolio').innerHTML=table('portfolio-table',portfolio,'portfolio');
document.getElementById('changes').innerHTML=table('changes-table',changes,'changes');
document.getElementById('watchlist').innerHTML=table('watchlist-table',watchlist,'watchlist');
document.getElementById('closed').innerHTML=table('closed-table',closed,'closed');
const colors=['#ef4444','#3b82f6','#22c55e','#f97316','#a855f7','#06b6d4','#eab308','#ec4899','#14b8a6','#f59e0b','#6366f1','#84cc16','#64748b'];
new Chart(document.getElementById('donut'),{type:'doughnut',data:{labels:chartRows.map(r=>`${r.name} ${r.weight}% ${r.actor?`(${r.actor})`:''}`),datasets:[{data:chartRows.map(r=>r.weight),backgroundColor:chartRows.map((_,i)=>colors[i%colors.length])}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{position:'bottom',labels:{boxWidth:12}}}}});
if(summaries.length){new Chart(document.getElementById('returns'),{type:'line',data:{labels:summaries.map(r=>r.date),datasets:[{label:'모델 포트폴리오 수익률',data:summaries.map(r=>r.avg_return_krw),borderColor:'#3b82f6',tension:.25}]},options:{responsive:true,maintainAspectRatio:false}})}else{document.getElementById('returns').replaceWith(Object.assign(document.createElement('div'),{className:'empty',textContent:'성과 데이터가 아직 없습니다.'}))}
document.getElementById('report-content').innerHTML=reportText.trim()?marked.parse(reportText):'<div class="empty">보고서가 없습니다.</div>';
</script></body></html>"""

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  HTML 대시보드 생성 완료: {DASHBOARD_FILE}")
    return DASHBOARD_FILE


def generate_all(report_text: str, today: datetime, state: Optional[dict] = None) -> tuple:
    """HTML 대시보드 + PNG 차트 모두 생성. Returns: (html_path, png_path)"""
    today_str = today.strftime("%Y-%m-%d")
    cache = _load_cache()
    report_content = report_text or ""

    html_path = None
    png_path = None
    try:
        html_path = generate_html(cache or {}, report_content, today_str, state=state)
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
