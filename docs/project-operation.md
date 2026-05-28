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
| 2 | 상태 로드 | `portfolio_state.py` | 이전 active 보유 종목을 읽어 Gemini 프롬프트에 전달 |
| 3 | 블로그 수집 | `fetch_mer.py` | RSS에서 최근 글을 찾고 모바일 본문을 스크래핑 |
| 4 | 글별 요약 캐시 | `fetch_mer.py` | 신규 글만 Flash로 1차 요약, 실패하면 원문 사용 |
| 5 | 최종 분석 | `analyze.py` | Pro 우선으로 리포트 생성, 실패/quota 시 Flash fallback |
| 6 | 추천 검증 | `portfolio_validation.py` | 블로그 직접 언급/기존 보유 근거 없는 신규 종목 제거 |
| 7 | 상태 업데이트 | `portfolio_state.py` | 검증된 리포트 기준으로 다음 실행 참고 상태 저장 |
| 8 | 성과 추적 | `track_returns.py` | 추천 종목 진입가/현재가 기반 성과 캐시 생성 |
| 9 | 출력 생성 | `generate_dashboard.py`, `telegram_notify.py` | HTML, PNG, Telegram 메시지 생성 |

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
| 글별 1차 요약 | `gemini-2.5-flash` | 신규 글이고 `ENABLE_POST_SUMMARIES`가 꺼져 있지 않을 때 | 요약 없이 원문을 최종 분석에 사용 |
| 최종 분석 | `gemini-2.5-pro` 우선 | 수집 기간의 요약/원문을 합쳐 리포트 생성 | quota, rate limit, 미지원 등 실패 시 Flash fallback |
| 최종 fallback | `gemini-2.5-flash` | Pro 실패 후 | Flash도 실패하면 실행 실패 처리 |
| 추천 검증 | API 호출 없음 | 리포트 생성 후 코드 후처리 | 근거 없는 종목 제거, 현금/대기자금으로 이동 |

요약 캐시는 `posts_db.json`의 `summary` 필드입니다. 이미 요약된 글은 다시 요약하지 않고 재사용합니다. 요약 캐시가 없으면 “요약 캐시 없음, 원문 사용” 로그가 나올 수 있으며, 이는 해당 글이 과거에 요약 없이 저장되었거나 요약 실패 후 원문 fallback 된 경우입니다.

## 포트폴리오 구성 원칙

| 구분 | 의미 | 처리 |
|------|------|------|
| 직접 언급 종목 | 블로그 원문/요약에 종목명, 코드, 티커가 직접 나온 종목 | 신규 추천 가능 |
| 직접 매수/매도 언급 | 종목 주변 문맥에 매수, 편입, 보유, 매도, 축소 등 표현이 있는 경우 | `근거유형`에 반영 |
| 기존 보유 종목 | `portfolio_state.json`의 active 종목 | 새 글에 다시 안 나와도 유지/조정 가능 |
| 긍정 섹터 | 섹터 온도계나 거시 해석상 긍정인 업종 | 이것만으로 신규 Buy 종목 생성 금지 |
| 근거 없는 신규 종목 | 직접 언급도 기존 보유도 아닌 KR/US 추천 종목 | 표에서 제거하고 비중을 현금/대기자금으로 이동 |

국내/해외 추천 표에는 `근거유형` 컬럼이 있어야 합니다. 허용 값은 `직접언급`, `직접매수언급`, `직접매도언급`, `기존보유`입니다.

## 출력물 관계

| 출력물 | 위치 | 기준 데이터 | 역할 |
|--------|------|-------------|------|
| 최신 리포트 | `output/latest.md` | 검증 완료 리포트 | Telegram, HTML 본문, 다음 참고용 최신 리포트 |
| 일자별 리포트 | `output/report_YYYYMMDD.md` | 검증 완료 리포트 + 성과 추적 | 히스토리 |
| 대시보드 | `output/dashboard.html` | 최신 검증 리포트 + 성과 캐시 | 상세 HTML 페이지 |
| Telegram 이미지 | `output/chart_latest.png` | 최신 검증 리포트 추천 비중 + 성과 캐시 | 메시지 첨부 이미지 |
| 성과 캐시 | `output/performance_cache.json` | 과거 추천과 현재 가격 | 성과 추적 그래프 |
| 포트폴리오 상태 | `output/portfolio_state.json` | 검증된 최신 추천 | 다음 Gemini 분석에 넘길 내부 상태 |

중요한 구분은 `latest.md`와 `portfolio_state.json`입니다. 사용자에게 보이는 추천 포트폴리오는 `latest.md`의 검증된 표를 기준으로 해야 합니다. `portfolio_state.json`은 다음 분석 프롬프트에 넣기 위한 내부 상태이며, HTML/PNG의 최신 추천 비중을 만들 때 우선 기준으로 쓰면 안 됩니다.

## 현재 개선 방향

| 개선 항목 | 현재 방향 |
|-----------|-----------|
| API 과다 호출 방지 | 신규 글만 1차 요약하고 캐시 재사용, 추천 검증은 API 재호출 없이 처리 |
| 모델 품질 | 최종 분석은 Pro 우선, Pro 실패 시 Flash fallback |
| 요약 로그 | 요약 캐시 생성/원문 fallback/캐시 없음 로그를 구분 |
| 종목 창작 방지 | 코드 후처리로 직접 언급/기존 보유 외 신규 KR/US 종목 제거 |
| HTML/Telegram 괴리 방지 | 사용자 출력은 모두 검증 완료 리포트를 기준으로 생성 |
| 성과 그래프 | 추천 비중과 성과 추적을 별도 개념으로 분리 |

## 향후 작업자가 읽어야 하는 경우

| 작업 상황 | 읽어야 하는 이유 |
|-----------|------------------|
| 추천 로직 수정 | 직접 언급, 기존 보유, 현금 이동 원칙을 깨지 않기 위해 |
| Gemini 호출/요약 정책 수정 | Flash 요약, Pro 우선 최종 분석, fallback 구조를 이해하기 위해 |
| HTML/Telegram 출력 불일치 수정 | 출력 기준이 `latest.md`인지 `portfolio_state.json`인지 확인하기 위해 |
| `portfolio_state.json` 수정 | 내부 상태와 사용자 표시 포트폴리오를 혼동하지 않기 위해 |
| `performance_cache.json` 수정 | 성과 추적 그래프가 추천 비중 그래프와 다른 데이터라는 점을 유지하기 위해 |
