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

from portfolio_validation import parse_portfolio_items

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


def _load_report() -> str:
    latest = OUTPUT_DIR / "latest.md"
    if latest.exists():
        with open(latest, encoding="utf-8") as f:
            content = f.read()
        # 실제 리포트인지 확인 (최소 길이 & 인사이트 섹션 포함)
        if len(content) > 500 and ("인사이트" in content or "포트폴리오" in content):
            return content
    return ""


def _parse_weights_from_report(report_text: str) -> list:
    """리포트에서 종목명 + 목표비중 파싱 (차트용)."""
    result = []
    for item in parse_portfolio_items(report_text, include_etf=True):
        if item.get("name") == "추천 없음":
            continue
        weight_str = item.get("weight", "").replace("%", "").strip()
        try:
            result.append({
                "name": item.get("name", ""),
                "weight": float(weight_str),
                "action": item.get("action", ""),
                "market": item.get("market", ""),
            })
        except ValueError:
            pass
    return result


def _load_portfolio_state() -> list:
    """portfolio_state.json에서 active 종목 로드."""
    import json as _json
    state_path = OUTPUT_DIR / "portfolio_state.json"
    if not state_path.exists():
        return []
    try:
        with open(state_path, encoding="utf-8") as f:
            state = _json.load(f)
        return [h for h in state.get("holdings", []) if h.get("status") == "active"]
    except Exception:
        return []


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


def generate_png(cache: dict, report_text: str = "", today_str: str = "") -> Optional[Path]:
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

    # ── 도넓 데이터 준비 ───────────────────────────────────────────────
    donut_items = []
    if report_text:
        for item in _parse_weights_from_report(report_text):
            donut_items.append({
                "name": item.get("name", "?"),
                "weight": item["weight"],
                "market": item.get("market", "ETF"),
            })

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
            ax2.text(i, avg + (0.4 if avg >= 0 else -0.4),
                     f"{sign}{avg:.1f}%", ha="center",
                     va="bottom" if avg >= 0 else "top",
                     color=color, fontsize=8.5, fontweight="bold")
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




def generate_html(cache: dict, report_text: str, today_str: str) -> Path:
    """Chart.js 기반 인터랙티브 HTML 대시보드 생성."""
    summaries = cache.get("report_summaries", []) if cache else []
    report_dates = [s["date"][5:] for s in summaries]
    report_avgs = [round(s["avg_return_krw"], 2) for s in summaries]
    active_positions = cache.get("active_positions", []) if cache else []
    changes = cache.get("changes", {}) if cache else {}
    buy_changes = (changes.get("buys", []) if isinstance(changes, dict) else [])
    update_changes = (changes.get("changes", []) if isinstance(changes, dict) else [])
    sell_changes = (changes.get("sells", []) if isinstance(changes, dict) else [])
    closed_positions = cache.get("closed_positions", []) if cache else []
    latest_weights = _parse_weights_from_report(report_text)
    latest_names = [s["name"] for s in latest_weights]
    latest_weight_values = [round(s["weight"], 2) for s in latest_weights]
    report_escaped = report_text.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")

    html = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>메르AI 포트폴리오 대시보드</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.0/marked.min.js"></script>
<style>
  :root{--bg:#0f172a;--card:#1e293b;--border:#334155;--text:#f1f5f9;--muted:#94a3b8;--green:#22c55e;--red:#ef4444;--blue:#3b82f6;--yellow:#eab308;}
  *{box-sizing:border-box;margin:0;padding:0;}
  body{background:var(--bg);color:var(--text);font-family:'Segoe UI',sans-serif;min-height:100vh;}
  header{background:var(--card);border-bottom:1px solid var(--border);padding:16px 24px;display:flex;align-items:center;justify-content:space-between;}
  header h1{font-size:1.2rem;font-weight:700;}
  header span{font-size:.8rem;color:var(--muted);}
  .container{max-width:1100px;margin:0 auto;padding:24px 16px;}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(480px,1fr));gap:20px;margin-bottom:24px;}
  .card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px;}
  .card h2{font-size:.95rem;color:var(--muted);margin-bottom:16px;font-weight:600;}
  .chart-wrap{position:relative;height:280px;}
  .empty-msg{color:var(--muted);font-size:.85rem;text-align:center;padding:60px 0;}
  #report-section{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:28px 32px;}
  #report-section h2{font-size:.95rem;color:var(--muted);margin-bottom:20px;font-weight:600;}
  #report-content h1{font-size:1.4rem;margin:20px 0 10px;color:var(--text);}
  #report-content h2{font-size:1.1rem;margin:20px 0 8px;color:var(--blue);border-bottom:1px solid var(--border);padding-bottom:6px;}
  #report-content h3{font-size:.95rem;margin:14px 0 6px;color:var(--yellow);}
  #report-content p{font-size:.88rem;line-height:1.7;color:#cbd5e1;margin:6px 0;}
  #report-content table{width:100%;border-collapse:collapse;font-size:.82rem;margin:12px 0;}
  #report-content th{background:#0f172a;color:var(--muted);padding:8px 10px;text-align:left;border:1px solid var(--border);}
  #report-content td{padding:7px 10px;border:1px solid var(--border);color:#cbd5e1;}
  #report-content tr:hover td{background:#0f172a44;}
  #report-content blockquote{border-left:3px solid var(--blue);padding-left:12px;color:var(--muted);font-size:.85rem;margin:10px 0;}
  #report-content hr{border:none;border-top:1px solid var(--border);margin:20px 0;}
  #report-content strong{color:var(--text);}
  #report-content code{background:#0f172a;padding:2px 6px;border-radius:4px;font-size:.82rem;color:var(--green);}
  #report-content ul,#report-content ol{padding-left:20px;font-size:.88rem;color:#cbd5e1;line-height:1.7;}
  .section-grid{display:grid;grid-template-columns:1fr;gap:20px;margin-bottom:24px;}
  .data-table{width:100%;border-collapse:collapse;font-size:.82rem;}
  .data-table th{background:#0f172a;color:var(--muted);padding:8px 10px;text-align:left;border:1px solid var(--border);white-space:nowrap;}
  .data-table td{padding:7px 10px;border:1px solid var(--border);color:#cbd5e1;vertical-align:top;}
  .data-table tr:hover td{background:#0f172a44;}
  .pill{display:inline-block;padding:2px 7px;border-radius:999px;font-size:.72rem;font-weight:700;background:#0f172a;color:#cbd5e1;border:1px solid var(--border);}
  .pill.buy{color:#86efac;border-color:#166534;}
  .pill.sell{color:#fca5a5;border-color:#7f1d1d;}
  .pill.change{color:#93c5fd;border-color:#1d4ed8;}
  .empty-inline{color:var(--muted);font-size:.85rem;padding:12px 0;}
</style>
</head>
<body>
<header>
  <h1>📊 메르AI 포트폴리오 대시보드</h1>
  <span>마지막 업데이트: <strong id="updatedAt"></strong></span>
</header>
<div class="container">
  <div class="grid">
    <div class="card">
      <h2>📊 최신 추천 포트폴리오 비중</h2>
      <div class="chart-wrap">
        <canvas id="stocksChart"></canvas>
        <div id="stocksEmpty" class="empty-msg" style="display:none">최신 추천 비중 없음 — 리포트 표를 확인하세요</div>
      </div>
    </div>
    <div class="card">
      <h2>📈 리포트 회차별 평균 수익률</h2>
      <div class="chart-wrap">
        <canvas id="reportChart"></canvas>
        <div id="reportEmpty" class="empty-msg" style="display:none">데이터 2회 이상 누적 후 표시됩니다</div>
      </div>
    </div>
  </div>
  <div class="section-grid">
    <div class="card">
      <h2>현재 포트폴리오</h2>
      <div id="currentPortfolio"></div>
    </div>
    <div class="card">
      <h2>추천 매수/변경 리스트</h2>
      <div id="changeList"></div>
    </div>
    <div class="card">
      <h2>매도/제외 리스트</h2>
      <div id="sellList"></div>
    </div>
  </div>
  <div id="report-section">
    <h2>📄 최신 리포트 전문</h2>
    <div id="report-content"></div>
  </div>
</div>
<script>
""" + (
        "const reportDates=" + json.dumps(report_dates, ensure_ascii=False) + ";\n"
        "const reportAvgs=" + json.dumps(report_avgs) + ";\n"
        "const latestNames=" + json.dumps(latest_names, ensure_ascii=False) + ";\n"
        "const latestWeights=" + json.dumps(latest_weight_values) + ";\n"
        "const activePositions=" + json.dumps(active_positions, ensure_ascii=False) + ";\n"
        "const buyChanges=" + json.dumps(buy_changes, ensure_ascii=False) + ";\n"
        "const updateChanges=" + json.dumps(update_changes, ensure_ascii=False) + ";\n"
        "const sellChanges=" + json.dumps(sell_changes, ensure_ascii=False) + ";\n"
        "const closedPositions=" + json.dumps(closed_positions, ensure_ascii=False) + ";\n"
        "const reportText=`" + report_escaped + "`;\n"
        "const updatedAt=" + json.dumps(today_str) + ";\n"
    ) + """
document.getElementById('updatedAt').textContent=updatedAt;
function barColor(v){return v>=0?'#22c55e':'#ef4444';}
function esc(v){return String(v??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));}
function retText(row){
  const v = row.return_pct_krw ?? row.return_pct;
  if(v===undefined || v===null || Number.isNaN(Number(v))) return '-';
  const n=Number(v), sign=n>=0?'▲':'▼', cls=n>=0?'buy':'sell';
  return `<span class="pill ${cls}">${sign}${Math.abs(n).toFixed(1)}%</span>`;
}
function renderCurrent(){
  const el=document.getElementById('currentPortfolio');
  if(!activePositions.length){el.innerHTML='<div class="empty-inline">active 포지션 없음</div>';return;}
  el.innerHTML='<table class="data-table"><thead><tr><th>종목</th><th>시장</th><th>판단</th><th>목표비중</th><th>편입일</th><th>수익률</th><th>근거</th></tr></thead><tbody>'+
    activePositions.map(r=>`<tr><td>${esc(r.name)}<br><span style="color:#94a3b8">${esc(r.code||r.ticker)}</span></td><td>${esc(r.market||r.type)}</td><td>${esc(r.action)}</td><td>${esc(r.weight)}</td><td>${esc(r.entry_date)}</td><td>${retText(r)}</td><td>${esc(r.basis_type||'기존보유')}</td></tr>`).join('')+
    '</tbody></table>';
}
function renderChanges(){
  const rows=[...buyChanges, ...updateChanges];
  const el=document.getElementById('changeList');
  if(!rows.length){el.innerHTML='<div class="empty-inline">오늘 신규 매수/변경 없음</div>';return;}
  el.innerHTML='<table class="data-table"><thead><tr><th>구분</th><th>종목</th><th>시장</th><th>판단</th><th>목표비중</th><th>근거</th></tr></thead><tbody>'+
    rows.map(r=>`<tr><td><span class="pill ${r.change_type==='신규 편입'?'buy':'change'}">${esc(r.change_type)}</span></td><td>${esc(r.name)}<br><span style="color:#94a3b8">${esc(r.code)}</span></td><td>${esc(r.market)}</td><td>${esc(r.action)}</td><td>${esc(r.weight)}</td><td>${esc(r.basis_type||'')}</td></tr>`).join('')+
    '</tbody></table>';
}
function renderSells(){
  const rows=sellChanges.length?sellChanges:closedPositions;
  const el=document.getElementById('sellList');
  if(!rows.length){el.innerHTML='<div class="empty-inline">오늘 매도/제외 없음</div>';return;}
  el.innerHTML='<table class="data-table"><thead><tr><th>종목</th><th>시장</th><th>종료일</th><th>사유</th></tr></thead><tbody>'+
    rows.map(r=>`<tr><td>${esc(r.name)}<br><span style="color:#94a3b8">${esc(r.code||r.ticker)}</span></td><td>${esc(r.market||r.type)}</td><td>${esc(r.closed_date||r.removed_date||updatedAt)}</td><td><span class="pill sell">${esc(r.reason||r.close_reason||r.removed_reason||'매도/제외')}</span></td></tr>`).join('')+
    '</tbody></table>';
}
renderCurrent();renderChanges();renderSells();
Chart.defaults.color='#94a3b8';
Chart.defaults.borderColor='#334155';
if(latestNames.length>0){
  new Chart(document.getElementById('stocksChart'),{
    type:'bar',
    data:{labels:latestNames,datasets:[{label:'목표비중(%)',data:latestWeights,backgroundColor:'#3b82f6',borderRadius:6,borderSkipped:false}]},
    options:{indexAxis:'y',responsive:true,maintainAspectRatio:false,
      plugins:{legend:{display:false},tooltip:{callbacks:{label:ctx=>` ${ctx.raw.toFixed(1)}%`}}},
      scales:{x:{grid:{color:'#334155'},ticks:{callback:v=>v+'%'},beginAtZero:true},y:{grid:{display:false}}}}
  });
}else{document.getElementById('stocksChart').style.display='none';document.getElementById('stocksEmpty').style.display='block';}
if(reportDates.length>=2){
  new Chart(document.getElementById('reportChart'),{
    type:'bar',
    data:{labels:reportDates,datasets:[
      {type:'bar',label:'회차 평균',data:reportAvgs,backgroundColor:reportAvgs.map(barColor),borderRadius:6,yAxisID:'y'},
      {type:'line',label:'추세',data:reportAvgs,borderColor:'#3b82f6',backgroundColor:'transparent',borderWidth:2,pointRadius:4,pointBackgroundColor:'#3b82f6',tension:0.3,yAxisID:'y'}
    ]},
    options:{responsive:true,maintainAspectRatio:false,
      plugins:{legend:{labels:{boxWidth:12,font:{size:11}}},tooltip:{callbacks:{label:ctx=>` ${ctx.raw>=0?'+':''}${ctx.raw.toFixed(1)}%`}}},
      scales:{y:{grid:{color:'#334155'},ticks:{callback:v=>(v>=0?'+':'')+v+'%'}},x:{grid:{display:false}}}}
  });
}else{document.getElementById('reportChart').style.display='none';document.getElementById('reportEmpty').style.display='block';}
if(reportText.trim()){document.getElementById('report-content').innerHTML=marked.parse(reportText);}
else{document.getElementById('report-content').innerHTML='<p style="color:#94a3b8">리포트가 없습니다.</p>';}
</script>
</body>
</html>"""

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  HTML 대시보드 생성 완료: {DASHBOARD_FILE}")
    return DASHBOARD_FILE


def generate_all(report_text: str, today: datetime) -> tuple:
    """HTML 대시보드 + PNG 차트 모두 생성. Returns: (html_path, png_path)"""
    today_str = today.strftime("%Y-%m-%d")
    cache = _load_cache()
    report_content = report_text if report_text else _load_report()

    html_path = None
    png_path = None
    try:
        html_path = generate_html(cache or {}, report_content, today_str)
    except Exception as e:
        print(f"  ⚠ HTML 대시보드 생성 실패: {e}")
    try:
        # 수익률 데이터 없어도 항상 PNG 생성 (첫 실행 시 목표비중 표시)
        png_path = generate_png(cache or {}, report_content, today_str)
    except Exception as e:
        print(f"  !! PNG 차트 생성 실패: {e}")
    return html_path, png_path


if __name__ == "__main__":
    html, png = generate_all("# 테스트 리포트\n\n테스트입니다.", datetime.now())
    print(f"HTML: {html}")
    print(f"PNG:  {png}")
