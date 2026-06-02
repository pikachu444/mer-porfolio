# 프로젝트 작동 방식 참고 문서

이 문서는 자동으로 매번 읽히는 지침 파일이 아니라, 코드 작업자가 필요할 때 참고하는 운영/구조 설명서입니다. 추천 로직, Gemini 호출 정책, 출력물 불일치, 상태 파일을 수정할 때만 읽는 용도입니다.

## 프로젝트 목적

| 항목 | 설명 |
|------|------|
| 목적 | 메르 네이버 블로그 글을 수집하고 Gemini로 시장 인사이트와 포트폴리오 리포트를 생성 |
| 핵심 결과물 | `latest.md`, `dashboard.html`, `chart_latest.png`, Telegram 메시지 |
| 핵심 원칙 | 블로그 직접 근거와 기존 보유 상태를 분리하고, 근거 없는 신규 종목 창작을 막음 |

## 전체 실행 흐름

| 순서 | 단계 | 주요 파일/함수 | 설명 |
|------|------|----------------|------|
| 1 | 실행 시작 | `main.py` | GitHub Actions, 로컬 `python main.py`, 또는 테스트 모드로 실행 |
| 2 | 상태 로드 | `portfolio_schema.py` | 이전 모델 포트폴리오와 Watchlist를 읽어 Gemini 프롬프트에 전달 |
| 3 | 블로그 수집 | `fetch_mer.py` | RSS에서 최근 글을 찾고 모바일 본문을 스크래핑 |
| 4 | 글별 요약 캐시 | `fetch_mer.py` | 신규 글 전체를 Flash로 요약하고 투자 관련 여부와 근거를 분류 |
| 5 | 구조화 판단 | `analyze.py` | 관련 글만 사용하여 Pro 우선으로 판단 JSON과 핵심 인사이트 생성 |
| 6 | 사용자용 보고서 | `analyze.py` | Pro 우선으로 Markdown 보고서 생성, 실패/quota 시 Flash fallback |
| 7 | 추천 검증 | `portfolio_schema.py` | 메르 직접 발언, AI 편입 근거, Watchlist 구분의 구조적 모순 차단 |
| 8 | 상태 업데이트 | `portfolio_schema.py` | 검증된 판단 JSON 기준으로 다음 실행 참고 상태 저장 |
| 9 | 성과 추적 | `track_returns.py` | 모델 포트폴리오 거래 원장과 현재 가격 기반 성과 캐시 생성 |
| 10 | 출력 생성 | `generate_dashboard.py`, `telegram_notify.py` | HTML, PNG, Telegram 메시지 생성 |

## 주요 입력

| 입력 | 위치 | 용도 |
|------|------|------|
| 네이버 블로그 RSS | `https://rss.blog.naver.com/ranto28.xml` | 신규 글 목록 수집 |
| 모바일 본문 | `https://m.blog.naver.com/ranto28/{post_id}` | 실제 분석 텍스트 수집 |
| 글 DB | `output/posts_db.json` | 원문과 1차 요약 캐시 보관 |
| 포트폴리오 상태 | `output/portfolio_state.json` | 다음 분석에 넘길 기존 active 보유 종목 |
| 성과 캐시 | `output/performance_cache.json` | HTML/PNG 성과 추적 그래프 데이터 |
| 환경변수/Secrets | GitHub Secrets 또는 `.env` | `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` 등 |

## Gemini 호출 구조

| 호출 | 모델 | 호출 조건 | 실패 처리 |
|------|------|-----------|-----------|
| 글별 1차 요약 | `gemini-2.5-flash` | 신규 글이고 `ENABLE_POST_SUMMARIES`가 꺼져 있지 않을 때 | 실행 실패 처리 |
| 1차 포트폴리오 판단 JSON | `gemini-2.5-pro` 우선 | 일별 관련 신규 글 또는 리밸런싱 누적 관련 글과 현재 상태로 투자 판단 생성 | quota, rate limit, 미지원 등 실패 시 Flash fallback |
| 2차 사용자용 Markdown 보고서 | `gemini-2.5-pro` 우선 | 검증된 판단 JSON과 글로 사람이 읽는 보고서 생성 | quota, rate limit, 미지원 등 실패 시 Flash fallback |
| 분석 fallback | `gemini-2.5-flash` | 각 분석 단계에서 Pro 실패 후 | Flash도 실패하면 실행 실패 처리 |
| 추천 검증 | API 호출 없음 | 판단 JSON 생성 후 코드 검증 | 구조 오류 차단, 검증 불가 신규 제안 제외 |

요약 캐시는 `posts_db.json`의 `summary` 필드입니다. 신규 글 원문은 고정 글자 수로 자르지 않고
저장합니다. 신규 글은 모두 Flash로 요약하며 투자 관련 여부와 분류 이유를 함께 기록합니다.
투자와 무관한 글도 DB에는 보존하지만 Pro 입력에서는 제외합니다.

API 요청은 토큰 수를 확인합니다. 저장 원문은 유지하고, 모델 입력 한도의 `80%`를 넘는
비정상적인 요청에만 전송용 본문의 끝부분을 줄입니다.

Actions의 정상 `scheduled`와 `rebalance`에서는 글별 Flash 요약을 활성화합니다. 반복 실행되는 개발 검증용 `verify`에서는 요약 호출을 끄고 원문을 사용하여, 최종 투자 판단과 보고서 생성 경로를 확인할 quota를 보존합니다.

## 포트폴리오 구성 원칙

| 구분 | 의미 | 처리 |
|------|------|------|
| 메르 직접 발언 | 메르 본인이 특정 종목의 매수, 보유, 매도를 밝혔다고 문맥상 판정 | `메르 직접 발언`으로 표시 |
| 원문 직접 등장 종목 | 방향성 논리, 현재 편입 이유, 위험, 근거 글을 모두 제시 | `AI 제안` 편입 후보 가능 |
| 단순 언급 종목 | 뉴스 사례, 비교 대상, 맥락 없는 나열 | Watchlist |
| 섹터만 등장 | AI가 추론한 개별 종목 | Watchlist만 허용 |
| 원문 미등장 섹터 ETF | 섹터 논리, 대표성, 현재 편입 이유, 위험을 모두 제시 | 조건부 편입 후보 가능 |

섹터 온도계는 사용하지 않습니다. 섹터 분석은 근거 글 URL과 투자 시사점을 포함한 핵심
인사이트로 제공합니다.

## 출력물 관계

| 출력물 | 위치 | 기준 데이터 | 역할 |
|--------|------|-------------|------|
| 최신 리포트 | `output/latest.md` | 검증 완료 리포트 | HTML 전체 보고서 본문 |
| 일자별 리포트 | `output/report_YYYYMMDD.md` | 검증 완료 리포트 + 성과 추적 | 히스토리 |
| 대시보드 | `output/dashboard.html` | 최신 검증 리포트 + 성과 캐시 | 상세 HTML 페이지 |
| Telegram 이미지 | `output/chart_latest.png` | 구조화 포트폴리오 목표 비중 + 성과 캐시 | 메시지 첨부 이미지 |
| 성과 캐시 | `output/performance_cache.json` | 과거 추천과 현재 가격 | 성과 추적 그래프 |
| 포트폴리오 상태 | `output/portfolio_state.json` | 검증된 최신 추천 | 다음 Gemini 분석에 넘길 내부 상태 |

사용자에게 보이는 최신 포트폴리오, HTML, PNG, Telegram은 모두 검증된 구조화 상태와 핵심
인사이트를 기준으로 생성합니다. Markdown 보고서는 같은 판단을 사람이 읽기 쉽게 설명하는
전체 보고서입니다.

## 현재 개선 방향

| 개선 항목 | 현재 방향 |
|-----------|-----------|
| API 호출 관리 | 신규 글만 Flash 요약하고 캐시 재사용, 판단 JSON과 보고서에 Pro를 각각 1회 사용 |
| 모델 품질 | 구조화 판단과 사용자용 보고서는 모두 Pro 우선, Pro 실패 시 Flash fallback |
| 요약 로그 | 요약 캐시 생성, 투자 관련 여부, 토큰 보호 적용 여부를 구분 |
| 종목 창작 방지 | 원문 미등장 개별 종목은 Watchlist만 허용하고, AI 편입 후보는 필수 근거를 검증 |
| HTML/Telegram 괴리 방지 | 사용자 출력은 모두 검증 완료 구조화 상태와 핵심 인사이트를 기준으로 생성 |
| 성과 그래프 | 추천 비중과 성과 추적을 별도 개념으로 분리 |

## 향후 작업자가 읽어야 하는 경우

| 작업 상황 | 읽어야 하는 이유 |
|-----------|------------------|
| 추천 로직 수정 | 직접 언급, 기존 보유, 현금 이동 원칙을 깨지 않기 위해 |
| Gemini 호출/요약 정책 수정 | Flash 요약, Pro 우선 최종 분석, fallback 구조를 이해하기 위해 |
| HTML/Telegram 출력 불일치 수정 | 출력 기준이 `latest.md`인지 `portfolio_state.json`인지 확인하기 위해 |
| `portfolio_state.json` 수정 | 내부 상태와 사용자 표시 포트폴리오를 혼동하지 않기 위해 |
| `performance_cache.json` 수정 | 성과 추적 그래프가 추천 비중 그래프와 다른 데이터라는 점을 유지하기 위해 |

## 무료 LLM API 비교 평가

운영 공급자를 바꾸기 전에는 `scripts/evaluate_llm_provider.py`로 별도 비교 평가를 수행한다.
기본 실행은 API 키 없이 요청 JSON만 만들며, 운영 상태와 Telegram을 변경하지 않는다.

```bash
python scripts/evaluate_llm_provider.py --provider cerebras
python scripts/evaluate_llm_provider.py --provider opencode-zen
```

실제 외부 호출은 공급자별 API 키를 준비한 뒤 `--execute`를 명시한 경우에만 수행한다.
OpenCode Zen의 무료 모델은 현재 API 키 없이도 `--execute` 비교 실행이 가능하다.
글별 요약만 비교할 때는 `--task summary`를 사용한다. 이 모드도 운영 요약 캐시를 변경하지
않는다.

```bash
python scripts/evaluate_llm_provider.py --provider opencode-zen --model mimo-v2.5-free --task summary --execute
```
