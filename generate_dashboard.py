"""
generate_dashboard.py
HTML 대시보드 + PNG 차트 생성 모듈

생성 파일:
  output/dashboard.html  — Chart.js 기반 인터랙티브 대시보드
  output/chart_latest.png — 텔레그램 전송용 요약 차트 (matplotlib)

GitHub Pages 활성화 시:
  https://{username}.github.io/{repo}/output/dashboard.html 로 접근 가능
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "output"))
CACHE_FILE = OUTPUT_DIR / "performance_cache.json"
DASHBOARD_FILE = OUTPUT_DIR / "dashboard.html"
PNG_FILE = OUTPUT_DIR / "chart_latest.png"


# ─── 데이터 로드 ──────────────────────────────────────────────────────────────

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
            return f.read()
    return ""


# ─── PNG 차트 생성 (텔레그램용) ───────────────────────────────────────────────

def generate_png(cache: dict, today_str: str) -> Optional[Path]:
    """이번 회차 종목별 수익률 + 회차별 평균 수익률 PNG 차트 생성."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        from matplotlib import font_manager
    except ImportError:
        print("  ⚠ matplotlib 미설치 — PNG 생성 스킵")
        return None

    # 한글 폰트 설정 (GitHub Actions Ubuntu 환경)
    try:
        import subprocess
        subprocess.run(
            ["apt-get", "install", "-y", "-q", "fonts-nanum"],
            capture_output=True
        )
        font_manager.fontManager.addfont("/usr/share/fonts/truetype/nanum/NanumGothic.ttf")
        plt.rcParams["font.family"] = "NanumGothic"
    except Exception:
        plt.rcParams["font.family"] = "DejaVu Sans"

    plt.rcParams["axes.unicode_minus"] = False

    summaries = cache.get("report_summaries", [])
    all_rows = cache.get("all_rows", [])

    # 이번 회차 데이터 (가장 최근 날짜보다 이전 것들)
    latest_date = max((r["date"] for r in all_rows), default=None) if all_rows else None
    if latest_date:
        latest_stocks = [r for r in all_rows if r["date"] == latest_date]
    else:
        latest_stocks = []

    fig_rows = 1 if not summaries or len(summaries) < 2 else 2
    fig, axes = plt.subplots(fig_rows, 1, figsize=(10, 5 * fig_rows))
    fig.patch.set_facecolor("#0f172a")

    if fig_rows == 1:
        axes = [axes]

    # ── 차트 1: 이번 회차 종목별 수익률 ─────────────────────────────────────
    ax1 = axes[0]
    ax1.set_facecolor("#1e293b")

    if latest_stocks:
        names = [s["name"] for s in latest_stocks]
        returns = [s.get("return_pct_krw", s["return_pct"]) for s in latest_stocks]
        colors = ["#22c55e" if r >= 0 else "#ef4444" for r in returns]

        bars = ax1.barh(names, returns, color=colors, edgecolor="none", height=0.6)
        ax1.axvline(0, color="#94a3b8", linewidth=0.8, linestyle="--")

        for bar, ret in zip(bars, returns):
            sign = "+" if ret >= 0 else ""
            ax1.text(
                bar.get_width() + (0.3 if ret >= 0 else -0.3),
                bar.get_y() + bar.get_height() / 2,
                f"{sign}{ret:.1f}%",
                va="center",
                ha="left" if ret >= 0 else "right",
                color="#f1f5f9",
                fontsize=9,
            )

        ax1.set_title(
            f"📊 종목별 수익률 ({latest_date} 추천 기준)",
            color="#f1f5f9", fontsize=12, pad=10
        )
        ax1.tick_params(colors="#94a3b8")
        ax1.set_xlabel("수익률 (%)", color="#94a3b8", fontsize=9)
        for spine in ax1.spines.values():
            spine.set_color("#334155")
    else:
        ax1.text(0.5, 0.5, "첫 실행 — 다음 리포트부터 표시",
                 ha="center", va="center", color="#94a3b8", fontsize=12,
                 transform=ax1.transAxes)
        ax1.set_title("📊 종목별 수익률", color="#f1f5f9", fontsize=12)

    # ── 차트 2: 회차별 평균 수익률 (2회 이상일 때만) ─────────────────────────
    if fig_rows == 2:
        ax2 = axes[1]
        ax2.set_facecolor("#1e293b")
        dates = [s["date"] for s in summaries]
        avgs = [s["avg_return_krw"] for s in summaries]
        colors2 = ["#22c55e" if a >= 0 else "#ef4444" for a in avgs]

        ax2.bar(range(len(dates)), avgs, color=colors2, edgecolor="none", width=0.6)
        ax2.axhline(0, color="#94a3b8", linewidth=0.8, linestyle="--")
        ax2.set_xticks(range(len(dates)))
        ax2.set_xticklabels([d[5:] for d in dates], color="#94a3b8", fontsize=9)

        for i, avg in enumerate(avgs):
            sign = "+" if avg >= 0 else ""
            ax2.text(i, avg + (0.3 if avg >= 0 else -0.3),
                     f"{sign}{avg:.1f}%",
                     ha="center",
                     va="bottom" if avg >= 0 else "top",
                     color="#f1f5f9", fontsize=9)

        ax2.set_title("📈 리포트 회차별 평균 수익률 (원화 기준)",
                      color="#f1f5f9", fontsize=12, pad=10)
        ax2.set_ylabel("평균 수익률 (%)", color="#94a3b8", fontsize=9)
        ax2.tick_params(colors="#94a3b8")
        for spine in ax2.spines.values():
            spine.set_color("#334155")

    plt.suptitle(
        f"메르AI 포트폴리오 성과  |  {today_str}",
        color="#f1f5f9", fontsize=13, fontweight="bold", y=1.01
    )
    plt.tight_layout(pad=2.0)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(PNG_FILE, dpi=150, bbox_inches="tight",
                facecolor="#0f172a", edgecolor="none")
    plt.close(fig)

    print(f"  🖼 PNG 차트 생성 완료: {PNG_FILE}")
    return PNG_FILE


# ─── HTML 대시보드 생성 ───────────────────────────────────────────────────────

def generate_html(cache: dict, report_text: str, today_str: str) -> Path:
    """Chart.js 기반 인터랙티브 HTML 대시보드 생성."""

    summaries = cache.get("report_summaries", []) if cache else []
    all_rows = cache.get("all_rows", []) if cache else []

    # 차트 데이터 준비
    report_dates = [s["date"][5:] for s in summaries]   # MM-DD
    report_avgs = [round(s["avg_return_krw"], 2) for s in summaries]

    latest_date = max((r["date"] for r in all_rows), default=None) if all_rows else None
    latest_stocks = [r for r in all_rows if r["date"] == latest_date] if latest_date else []
    latest_names = [s["name"] for s in latest_stocks]
    latest_returns = [round(s.get("return_pct_krw", s["return_pct"]), 2) for s in latest_stocks]

    # 마크다운 → HTML (marked.js가 브라우저에서 처리)
    report_escaped = report_text.replace("`", "\\`").replace("${", "\\${")

    data_js = f"""
const reportDates = {json.dumps(report_dates, ensure_ascii=False)};
const reportAvgs = {json.dumps(report_avgs)};
const latestNames = {json.dumps(latest_names, ensure_ascii=False)};
const latestReturns = {json.dumps(latest_returns)};
const reportText = `{report_escaped}`;
const updatedAt = "{today_str}";
    """

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>메르AI 포트폴리오 대시보드</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.0/marked.min.js"></script>
<style>
  :root {{
    --bg: #0f172a;
    --card: #1e293b;
    --border: #334155;
    --text: #f1f5f9;
    --muted: #94a3b8;
    --green: #22c55e;
    --red: #ef4444;
    --blue: #3b82f6;
    --yellow: #eab308;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: 'Segoe UI', sans-serif; min-height: 100vh; }}
  header {{ background: var(--card); border-bottom: 1px solid var(--border); padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; }}
  header h1 {{ font-size: 1.2rem; font-weight: 700; }}
  header span {{ font-size: 0.8rem; color: var(--muted); }}
  .container {{ max-width: 1100px; margin: 0 auto; padding: 24px 16px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(480px, 1fr)); gap: 20px; margin-bottom: 24px; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 20px; }}
  .card h2 {{ font-size: 0.95rem; color: var(--muted); margin-bottom: 16px; font-weight: 600; }}
  .chart-wrap {{ position: relative; height: 280px; }}
  .empty-msg {{ color: var(--muted); font-size: 0.85rem; text-align: center; padding: 60px 0; }}
  /* 리포트 섹션 */
  #report-section {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 28px 32px; }}
  #report-section h2 {{ font-size: 0.95rem; color: var(--muted); margin-bottom: 20px; font-weight: 600; }}
  #report-content h1 {{ font-size: 1.4rem; margin: 20px 0 10px; color: var(--text); }}
  #report-content h2 {{ font-size: 1.1rem; margin: 20px 0 8px; color: var(--blue); border-bottom: 1px solid var(--border); padding-bottom: 6px; }}
  #report-content h3 {{ font-size: 0.95rem; margin: 14px 0 6px; color: var(--yellow); }}
  #report-content p {{ font-size: 0.88rem; line-height: 1.7; color: #cbd5e1; margin: 6px 0; }}
  #report-content table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; margin: 12px 0; }}
  #report-content th {{ background: #0f172a; color: var(--muted); padding: 8px 10px; text-align: left; border: 1px solid var(--border); }}
  #report-content td {{ padding: 7px 10px; border: 1px solid var(--border); color: #cbd5e1; }}
  #report-content tr:hover td {{ background: #0f172a44; }}
  #report-content blockquote {{ border-left: 3px solid var(--blue); padding-left: 12px; color: var(--muted); font-size: 0.85rem; margin: 10px 0; }}
  #report-content hr {{ border: none; border-top: 1px solid var(--border); margin: 20px 0; }}
  #report-content strong {{ color: var(--text); }}
  #report-content code {{ background: #0f172a; padding: 2px 6px; border-radius: 4px; font-size: 0.82rem; color: var(--green); }}
  #report-content ul, #report-content ol {{ padding-left: 20px; font-size: 0.88rem; color: #cbd5e1; line-height: 1.7; }}
</style>
</head>
<body>
<header>
  <h1>📊 메르AI 포트폴리오 대시보드</h1>
  <span>마지막 업데이트: <strong id="updatedAt"></strong></span>
</header>
<div class="container">
  <div class="grid">
    <!-- 이번 회차 종목별 수익률 -->
    <div class="card">
      <h2>📊 최신 리포트 종목별 수익률 (원화 기준)</h2>
      <div class="chart-wrap">
        <canvas id="stocksChart"></canvas>
        <div id="stocksEmpty" class="empty-msg" style="display:none">첫 실행 — 다음 리포트부터 표시됩니다</div>
      </div>
    </div>
    <!-- 회차별 평균 수익률 -->
    <div class="card">
      <h2>📈 리포트 회차별 평균 수익률</h2>
      <div class="chart-wrap">
        <canvas id="reportChart"></canvas>
        <div id="reportEmpty" class="empty-msg" style="display:none">데이터 2회 이상 누적 후 표시됩니다</div>
      </div>
    </div>
  </div>
  <!-- 리포트 전문 -->
  <div id="report-section">
    <h2>📄 최신 리포트 전문</h2>
    <div id="report-content"></div>
  </div>
</div>
<script>
{data_js}

document.getElementById('updatedAt').textContent = updatedAt;

// 색상 헬퍼
function barColor(val) {{ return val >= 0 ? '#22c55e' : '#ef4444'; }}

Chart.defaults.color = '#94a3b8';
Chart.defaults.borderColor = '#334155';

// ── 종목별 수익률 차트 ──────────────────────────────────────────────────
if (latestNames.length > 0) {{
  const ctx1 = document.getElementById('stocksChart').getContext('2d');
  new Chart(ctx1, {{
    type: 'bar',
    data: {{
      labels: latestNames,
      datasets: [{{
        label: '수익률 (%)',
        data: latestReturns,
        backgroundColor: latestReturns.map(barColor),
        borderRadius: 6,
        borderSkipped: false,
      }}]
    }},
    options: {{
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{
          callbacks: {{
            label: ctx => ` ${{ctx.raw >= 0 ? '+' : ''}}${{ctx.raw.toFixed(1)}}%`
          }}
        }}
      }},
      scales: {{
        x: {{
          grid: {{ color: '#334155' }},
          ticks: {{ callback: v => (v >= 0 ? '+' : '') + v + '%' }}
        }},
        y: {{ grid: {{ display: false }} }}
      }}
    }}
  }});
}} else {{
  document.getElementById('stocksChart').style.display = 'none';
  document.getElementById('stocksEmpty').style.display = 'block';
}}

// ── 회차별 평균 수익률 차트 ─────────────────────────────────────────────
if (reportDates.length >= 2) {{
  const ctx2 = document.getElementById('reportChart').getContext('2d');
  new Chart(ctx2, {{
    type: 'bar',
    data: {{
      labels: reportDates,
      datasets: [
        {{
          type: 'bar',
          label: '회차별 평균 수익률',
          data: reportAvgs,
          backgroundColor: reportAvgs.map(barColor),
          borderRadius: 6,
          yAxisID: 'y',
        }},
        {{
          type: 'line',
          label: '추세',
          data: reportAvgs,
          borderColor: '#3b82f6',
          backgroundColor: 'transparent',
          borderWidth: 2,
          pointRadius: 4,
          pointBackgroundColor: '#3b82f6',
          tension: 0.3,
          yAxisID: 'y',
        }}
      ]
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{
        legend: {{ labels: {{ boxWidth: 12, font: {{ size: 11 }} }} }},
        tooltip: {{
          callbacks: {{
            label: ctx => ` ${{ctx.raw >= 0 ? '+' : ''}}${{ctx.raw.toFixed(1)}}%`
          }}
        }}
      }},
      scales: {{
        y: {{
          grid: {{ color: '#334155' }},
          ticks: {{ callback: v => (v >= 0 ? '+' : '') + v + '%' }}
        }},
        x: {{ grid: {{ display: false }} }}
      }}
    }}
  }});
}} else {{
  document.getElementById('reportChart').style.display = 'none';
  document.getElementById('reportEmpty').style.display = 'block';
}}

// ── 리포트 마크다운 렌더링 ─────────────────────────────────────────────
if (reportText.trim()) {{
  document.getElementById('report-content').innerHTML = marked.parse(reportText);
}} else {{
  document.getElementById('report-content').innerHTML = '<p style="color:#94a3b8">리포트가 없습니다.</p>';
}}
</script>
</body>
</html>"""

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"  🌐 HTML 대시보드 생성 완료: {DASHBOARD_FILE}")
    return DASHBOARD_FILE


# ─── 메인 함수 ────────────────────────────────────────────────────────────────

def generate_all(report_text: str, today: datetime) -> tuple:
    """
    HTML 대시보드 + PNG 차트 모두 생성.

    Returns:
        (html_path, png_path)  — 생성 실패 시 None
    """
    today_str = today.strftime("%Y-%m-%d")
    cache = _load_cache()

    html_path = None
    png_path = None

    try:
        report_content = report_text if report_text else _load_report()
        html_path = generate_html(cache or {}, report_content, today_str)
    except Exception as e:
        print(f"  ⚠ HTML 대시보드 생성 실패: {e}")

    try:
        if cache:
            png_path = generate_png(cache, today_str)
    except Exception as e:
        print(f"  ⚠ PNG 차트 생성 실패: {e}")

    return html_path, png_path


# ─── 직접 실행 테스트 ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    html, png = generate_all("# 테스트 리포트\n\n테스트입니다.", datetime.now())
    print(f"HTML: {html}")
    print(f"PNG:  {png}")
