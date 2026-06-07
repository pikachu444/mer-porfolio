# 메르AI 포트폴리오 코드 구조

## 실행 흐름

| 순서 | 단계 | 주요 파일 |
|---|---|---|
| 1 | 실행 모드 결정 | `runtime_modes.py`, `main.py` |
| 2 | 상태 로드 | `portfolio_schema.py`, `output/portfolio_state.json` |
| 3 | 블로그 글 수집 | `fetch_mer.py`, `output/posts_db.json` |
| 4 | 신규 글 요약 | `fetch_mer.py`, Gemini Flash |
| 5 | 포트폴리오 판단 | `analyze.py`, Gemini Pro 우선 |
| 6 | 판단 검증과 상태 반영 | `portfolio_schema.py`, `portfolio_validation.py` |
| 7 | 수익률 계산 | `track_returns.py`, `output/performance_cache.json` |
| 8 | 사용자 출력 기준 자료 생성 | `portfolio_output.py` |
| 9 | HTML/PNG 생성 | `generate_dashboard.py` |
| 10 | Telegram 전송 | `telegram_notify.py` |

## 주요 모듈 책임

| 파일 | 책임 |
|---|---|
| `main.py` | 전체 실행 순서 제어, no-change/분석 보류/분석 성공 경로 연결 |
| `fetch_mer.py` | RSS 수집, 본문 저장, 글별 요약, 요약 실패 보류 |
| `analyze.py` | Gemini 호출, 구조화 판단 JSON과 보고서 생성 시도 |
| `system_prompt.py` | Gemini 입력 프롬프트와 출력 계약 |
| `portfolio_schema.py` | 상태 파일 스키마, 판단 검증, 상태 갱신 |
| `portfolio_validation.py` | 보고서/판단 보조 검증 |
| `track_returns.py` | 모델 포트폴리오 거래 원장과 수익률 계산 |
| `portfolio_output.py` | Telegram, HTML, Markdown이 함께 쓰는 사용자 출력 기준 자료 생성 |
| `generate_dashboard.py` | HTML 대시보드와 Telegram용 PNG 차트 생성 |
| `telegram_notify.py` | Telegram 메시지와 이미지 전송 |

## 출력 파일 관계

| 파일 | 설명 |
|---|---|
| `output/portfolio_state.json` | 현재 모델 포트폴리오, Watchlist, 종료 포지션, 핵심 인사이트 |
| `output/performance_cache.json` | 수익률 계산 자료. 현재 포트폴리오보다 항목이 적을 수 있다 |
| `output/model_portfolio_ledger.json` | 거래 원장. 수익률 계산의 기준 |
| `output/report_YYYYMMDD.md` | 실행일 기준 사용자용 Markdown 보고서 |
| `output/dashboard.html` | HTML 대시보드 |
| `output/chart_latest.png` | Telegram 첨부용 현재 차트 이미지 |
| `output/posts_db.json` | 수집 글 원문, 요약, 투자 관련 여부, 보류 상태 |
| `output/decision_latest.json` | 최근 구조화 판단 JSON. 내부 진단용 |

`latest.md`는 사용자 출력 기준으로 사용하지 않는다. 보고서가 필요하면 날짜가 들어간
`report_YYYYMMDD.md`를 사용한다.

## 사용자 출력 기준

`portfolio_output.build_output_model()`이 현재 상태와 수익률 계산 자료를 합쳐 출력 기준 자료를 만든다.

이 기준 자료에서 다음 항목을 함께 만든다.

- 핵심 인사이트
- 국내주식 추천
- 해외주식 추천
- 현재 모델 포트폴리오 목표 비중
- Watchlist
- 종료 포지션
- 수익률 표시

수익률 계산 자료에 현재 종목이 없으면 종목을 제거하지 않고 `집계 전`으로 표시한다.
전체 포트폴리오 수익률도 현재 종목 전체가 계산된 경우에만 표시한다.

## GitHub Actions

| 모드 | 목적 |
|---|---|
| `scheduled` | 매일 신규 글 수집과 필요 시 판단 실행 |
| `rebalance` | 수동 리밸런싱 실행 |
| `verify` | 운영 상태를 직접 덮어쓰지 않는 실제 출력 검증 |
| `test` | API 호출 없는 테스트 |

Actions에서 수정이 반영되려면 변경 사항이 `main` 브랜치에 병합되어야 한다.

## 구현 후 확인

기능 변경 후 기본 확인은 다음 순서로 한다.

1. `python -m py_compile main.py generate_dashboard.py telegram_notify.py portfolio_output.py`
2. `PYTHONUTF8=1 python -m unittest discover -s tests -q`
3. `git diff --check`
4. 필요 시 GitHub Actions `verify` 또는 `rebalance` 실행 결과 확인

## 셸별 명령 주의사항

명령을 실행하기 전에 현재 터미널 셸을 확인한다. 이 저장소의 현재 작업 환경은 Windows
PowerShell이다. 셸이 다르면 환경변수 설정, 경로 표기, 명령 연결 방식이 달라진다.

| 환경 | 환경변수 설정 예시 | 경로 예시 | 주의사항 |
|---|---|---|---|
| PowerShell | `$env:PYTHONUTF8='1'; python -m unittest discover -s tests -q` | `docs\code-structure.md` | `PYTHONUTF8=1 python ...`는 동작하지 않는다. |
| cmd.exe | `set PYTHONUTF8=1 && python -m unittest discover -s tests -q` | `docs\code-structure.md` | `$env:` 문법은 동작하지 않는다. |
| bash/Linux | `PYTHONUTF8=1 python -m unittest discover -s tests -q` | `docs/code-structure.md` | Windows 전용 `Remove-Item`, `$env:` 문법을 쓰지 않는다. |

현재 셸을 모르면 먼저 확인한다.

| 환경 | 확인 명령 |
|---|---|
| PowerShell | `$PSVersionTable.PSVersion` |
| cmd.exe | `echo %ComSpec%` |
| bash/Linux | `echo $SHELL` |

문서나 테스트에 명령을 남길 때도 대상 환경을 함께 적는다. 로컬 작업 환경이 PowerShell이면
PowerShell 문법을 우선으로 쓴다.
