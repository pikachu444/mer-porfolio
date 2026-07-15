# 메르AI 포트폴리오 코드 구조

| 단계 | 주요 파일 | 책임 |
|---|---|---|
| 글 수집·요약 | `fetch_mer.py` | 메르 블로그 글 수집과 글별 요약 |
| 판단 | `analyze.py`, `system_prompt.py` | 투자 관련성, 핵심 논지, 직접 언급/AI 추론, 참고 종목 판단 |
| 상태 검증 | `portfolio_schema.py`, `portfolio_provenance.py` | 원문 근거와 현재 상태 보존 |
| 비중 보호 | `portfolio_allocator.py`, `portfolio_runtime.py` | 제안 비중의 종목별 상한과 잔여 현금 계산 |
| 성과 비교 | `track_returns.py`, `portfolio_metrics.py` | 원장·수익률과 KOSPI200/S&P500 비교 벤치마크 |
| 사용자 출력 | `portfolio_output.py`, `telegram_notify.py`, `generate_dashboard.py` | Telegram, HTML, Markdown 생성 |

`output/portfolio_state.json`은 현재 참고 포트폴리오 상태이고, `output/model_portfolio_ledger.json`은 과거 거래·NAV 이력을 보존한다. 광범위 지수 ETF 자동 편입 정책에서 제외된 기존 기록은 원장에 행정적 정책 제거 기록으로 남긴다.
