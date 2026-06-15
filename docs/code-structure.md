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

실행 기준일은 `main.py`에서 한국시간(`Asia/Seoul`)으로 계산한다. GitHub Actions runner의 UTC
날짜를 그대로 보고서 파일명이나 Telegram 날짜에 사용하지 않는다.

## 주요 모듈 책임

| 파일 | 책임 |
|---|---|
| `main.py` | 전체 실행 순서 제어, no-change/분석 보류/분석 성공 경로 연결 |
| `fetch_mer.py` | RSS 수집, 본문 저장, 글별 요약, 요약 실패 보류 |
| `analyze.py` | Gemini 호출, 구조화 판단 JSON과 보고서 생성 시도 |
| `system_prompt.py` | Gemini 입력 프롬프트와 출력 계약 |
| `portfolio_schema.py` | 상태 파일 스키마, 판단 검증, 현금성 20% 하회와 리밸런싱 현금성 개선 검증, 상태 갱신 |
| `portfolio_validation.py` | 보고서/판단 보조 검증 |
| `track_returns.py` | 모델 포트폴리오 거래 원장, 종목 코드 정규화, 수익률 계산 |
| `portfolio_output.py` | Telegram, HTML, Markdown이 함께 쓰는 사용자 출력 기준 자료 생성 |
| `generate_dashboard.py` | HTML 대시보드와 Telegram용 PNG 차트 생성 |
| `telegram_notify.py` | Telegram 메시지와 이미지 전송 |
| `scripts/backfill_legacy_evidence.py` | 2026-06-15 과거 미분류 보유 종목 근거 복구용 일회성 유지보수 스크립트 |

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

`report_YYYYMMDD.md`의 날짜는 한국시간 기준이다. 예를 들어 GitHub Actions가 UTC
2026-06-07 16:20에 실행되면 한국시간으로는 2026-06-08이므로 `report_20260608.md`를 만든다.

## 사용자 출력 기준

`portfolio_output.build_output_model()`이 현재 상태와 수익률 계산 자료를 합쳐 출력 기준 자료를 만든다.

이 기준 자료에서 다음 항목을 함께 만든다.

- 핵심 인사이트
- 국내주식 추천
- 해외주식 추천
- 현재 모델 포트폴리오 목표 비중
- 주식 노출과 현금성 비중
- 재검증 필요 포지션
- Watchlist
- 종료 포지션
- 수익률 표시

수익률 계산 자료에 현재 종목이 없으면 종목을 제거하지 않고 `집계 전`으로 표시한다.
전체 포트폴리오 수익률도 현재 종목 전체가 계산된 경우에만 표시한다.
현재 모델 포트폴리오 종목과 종료 포지션 종목이 겹치면 성과 기록을 정리한다. 대한전선처럼 과거
코드 오기(`011440`)가 있으면 실제 코드(`001440`) 기준으로 정규화한 뒤 비교한다.

기존 상태 마이그레이션, `미분류` 보유 종목, `allocation_role`이 없는 AI 포지션은 국내/해외
추천 목록에서 제외하고 `재검증 필요 포지션`으로 표시한다. 신규 매수 판단에는
`allocation_role`을 요구하며, 이 값은 핵심/core, 위성/satellite, 위험자산/risk, 방어/defensive,
관찰/watch 중 하나다.

신규 매수로 현금성 비중이 기본 방어 기준인 20% 아래로 내려가면 판단 사유에 현금성 비중을
낮추는 이유가 포함되어야 한다. 이유가 없으면 `portfolio_schema.py` 검증 단계에서 차단한다.

운영 `rebalance`에서는 현재 현금성이 20% 미만이면 결과 현금성이 20% 이상으로 회복되어야 한다.
모델이 고비중 종목을 설명문만으로 유지하거나 일부만 줄여 현금성 20% 기준을 충족하지 못하는
결과는 검증 단계에서 차단한다.

과거 상태 파일에 근거가 빠져 있는 보유 종목을 정정할 때는 운영 경로에 임의 보정 로직을 넣지 않는다.
필요한 경우 `scripts/backfill_legacy_evidence.py` 같은 일회성 유지보수 스크립트로 dry-run 결과를
확인한 뒤 상태 파일과 판단 이력을 명시적으로 보강한다. 원문 글 URL을 찾지 못하고 과거 보고서만
근거로 쓸 때는 `historical_report`로 구분한다.

## GitHub Actions

| 모드 | 목적 |
|---|---|
| `scheduled` | 매일 신규 글 수집과 필요 시 판단 실행 |
| `rebalance` | 수동 리밸런싱 실행 |
| `verify` | Gemini 호출 없이 현재 포트폴리오 기준 출력과 Telegram 검증. 운영 대시보드 링크는 보내지 않음 |
| `full_verify` | 운영 상태를 덮어쓰지 않는 실제 수집/요약/분석/출력 전체 검증. HTML은 artifact로 확인 |
| `test` | API 호출 없는 테스트 |

Actions에서 수정이 반영되려면 변경 사항이 `main` 브랜치에 병합되어야 한다.

## 구현 후 확인

기능 변경 후 기본 확인은 다음 순서로 한다.

1. `python -m py_compile main.py generate_dashboard.py telegram_notify.py portfolio_output.py`
2. `PYTHONUTF8=1 python -m unittest discover -s tests -q`
3. `git diff --check`
4. 출력 형식만 확인할 때는 GitHub Actions `verify`, 전체 운영 흐름을 확인할 때는 `full_verify` 실행 결과 확인

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
