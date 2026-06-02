# 메르AI 포트폴리오 자동 분석

## 전체 구성 요약

이 프로젝트는 **3가지 방식**으로 메르AI를 사용할 수 있습니다.

| 방식 | 위치 | 자동화 | 필요한 것 |
|------|------|--------|---------|
| **GitHub Actions 자동화** | 이 폴더 | ✅ 매일 자동 실행 | Gemini API 키 |
| **ChatGPT GPTs 페르소나** | `personas/chatgpt-gpt/` | 수동 트리거 | ChatGPT Plus (이미 있음) |
| **Gemini Gems 페르소나** | `personas/gemini-gem/` | 수동 트리거 | Gemini Advanced (이미 있음) |

→ 셋 중 하나만 써도 되고, 셋 다 써도 됩니다.
→ 채팅으로 빠르게 쓰고 싶으면 Gems, 자동으로 쌓아두고 싶으면 GitHub Actions.

메르(blog.naver.com/ranto28)의 블로그 글을 자동 수집해서
Gemini AI가 메르 스타일로 한국·미국 주식 포트폴리오를 분석해주는 시스템.

prophit_(blog.naver.com/prophit_)이 수동으로 하던 "메르ai포트"를 완전 자동화한 버전.

**비용:** GitHub Actions와 Gemini API 무료 tier 범위에서는 $0. Pro 호출을 안정적으로 운영하려면 Google Cloud Billing 연결이 필요할 수 있습니다.

---

## 작동 방식

```
[GitHub Actions 스케줄러 — 매일 자정 KST]
        ↓
[메르 블로그 RSS 파싱 + 전문 스크래핑]
        ↓
[원문/Flash 요약 캐시 구성 + Gemini 2.5 Pro 우선 판단 및 보고서]
        ↓
[output/report_YYYYMMDD.md 로 자동 저장 + 커밋]
```

---

## 셋업 (5분)

### 1단계 — Gemini API 키 발급 (무료)

1. https://aistudio.google.com/app/apikey 접속
2. Google 계정으로 로그인
3. **"Create API key"** 클릭
4. 발급된 키 복사 (형식: `AIza...`)

> Gemini API 무료 한도는 모델과 Google Cloud 프로젝트 상태에 따라 달라집니다.
> 정확한 현재 한도는 Google AI Studio의 rate limits 화면에서 확인하세요.

---

### 2단계 — GitHub 저장소 만들기

1. https://github.com/new 에서 새 레포 생성
   - 이름 예: `mer-portfolio`
   - **Private** 권장 (투자 정보이므로)
   - README 체크 해제

2. 이 프로젝트 파일들을 레포에 업로드
   ```
   mer-portfolio/
   ├── .github/workflows/schedule.yml
   ├── output/.gitkeep
   ├── fetch_mer.py
   ├── system_prompt.py
   ├── analyze.py
   ├── main.py
   └── requirements.txt
   ```

---

### 3단계 — API 키를 GitHub Secrets에 등록

1. 레포 페이지 → **Settings** 탭
2. 왼쪽 메뉴 → **Secrets and variables** → **Actions**
3. **"New repository secret"** 클릭
4. Name: `GEMINI_API_KEY`
5. Secret: 1단계에서 복사한 키 붙여넣기
6. **"Add secret"** 클릭

---

### 4단계 — 첫 실행 테스트

1. 레포 → **Actions** 탭
2. 왼쪽에서 **"메르AI 포트폴리오 분석"** 클릭
3. **"Run workflow"** 버튼 클릭 → **"Run workflow"** 확인
4. 실행 완료 후 `output/latest.md` 파일 확인

---

## 자동 실행 스케줄

| 실행 시점 | 수집 기간 |
|-----------|-----------|
| 매일 자정 (KST) | 신규 글 |

수동 실행은 Actions → Run workflow에서 가능합니다.
`scheduled`는 신규 글만 분석합니다. 마지막 실제 리밸런싱 후 `14일`이 지났으면 그 이후
누적된 투자 관련 글로 리밸런싱합니다. 신규 근거가 없으면 리밸런싱 날짜를 갱신하지 않고
다음 관련 글까지 연기합니다. `verify`는 운영 상태를 변경하지 않는 실제 출력 검증이며,
`test`는 API 호출 없이 mock 테스트만 실행합니다.

---

## 출력 파일

```
output/
├── latest.md              ← 항상 최신 리포트
├── report_20260515.md     ← 날짜별 히스토리
├── report_20260501.md
└── ...
```

리포트 형식:
```markdown
# 메르AI 포트폴리오 리포트
분석 기간: 2026-05-03 ~ 2026-05-15

## 📌 시장 분석 핵심 인사이트
### 인사이트 1: 호르무즈 봉쇄 장기화와 조선업 나비효과
1. 확인된 사실 ...
해석(나비효과): ...
투자판단: Buy 강 — ...

## 📊 포트폴리오 추천
### 🇰🇷 국내주식
| 종목명 | 코드 | 판단 | 목표비중 | 핵심 근거 |
...
### 🇺🇸 해외주식
...
```

---

## 환경변수 옵션

| 변수명 | 기본값 | 설명 |
|--------|--------|------|
| `GEMINI_API_KEY` | (필수) | Google AI Studio API 키 |
| `RUN_MODE` | `scheduled` | `scheduled`, `rebalance`, `verify`, `test` |
| `FETCH_DAYS` | 모드별 기본값 | RSS 조회 범위를 임시 조정할 때만 사용하는 일수 |
| `GEMINI_MODEL` | `gemini-2.5-pro` | 최종 리포트 생성 우선 모델 |
| `GEMINI_FALLBACK_MODEL` | `gemini-2.5-flash` | Pro 실패/quota 시 fallback 모델 |
| `ENABLE_POST_SUMMARIES` | 켜짐 | 신규 글별 1차 요약 API 호출 여부. Actions의 `verify`에서는 꺼짐 |
| `OUTPUT_DIR` | `output` | 리포트 저장 경로 |

로컬 실행 예:
```bash
# 기본 실행
export GEMINI_API_KEY="AIza..."
python main.py

# 최근 14일 수집, 리밸런싱 모드
RUN_MODE=rebalance FETCH_DAYS=14 python main.py
```

---

## 모델 및 한도 운영 정책

- 신규 글 원문은 고정 글자 수로 자르지 않고 저장합니다.
- 신규 글은 모두 Flash로 1차 요약하고, 투자 관련 여부와 분류 이유를 함께 기록합니다.
- 투자와 무관한 글은 DB에 저장하지만 Pro 투자 판단 입력에서는 제외합니다.
- 저장 원문은 유지하며, 모델 입력 한도의 `80%`를 넘는 비정상 요청에만 전송용 본문 끝부분을 줄입니다.
- Actions의 정상 `scheduled`와 `rebalance`에서는 글별 Flash 요약을 켜고, 반복 검증용 `verify`에서는 끕니다.
- 구조화 판단 JSON과 사용자용 Markdown 보고서는 각각 `gemini-2.5-pro`를 먼저 시도하고 실패/quota 시 `gemini-2.5-flash`로 fallback합니다.
- 일별 분석은 관련 신규 글만, `14일` 리밸런싱은 마지막 실제 리밸런싱 이후 누적 관련 글만 사용합니다.
- 관련 신규 글이 없으면 LLM을 호출하지 않고 가격, 성과, 출력만 갱신합니다.
- HTML, PNG, Telegram은 추가 LLM 호출 없이 구조화 판단과 핵심 인사이트로 생성합니다.
- `gemini-2.5-pro` 한도가 낮거나 지원되지 않으면 자동으로 Flash fallback을 사용합니다.
- `flash-lite` 계열은 품질 저하 우려가 있어 기본 경로에서 사용하지 않습니다.
- rate limit 또는 quota 오류가 나면 모델별 호출 간격을 두고 재시도합니다.

### 무료 LLM API 비교 평가

운영 상태와 Telegram 전송을 건드리지 않고 OpenAI 호환 API의 요청 묶음을 만들 수 있습니다.
기본 실행은 외부 API를 호출하지 않습니다.

```bash
python scripts/evaluate_llm_provider.py --provider cerebras
python scripts/evaluate_llm_provider.py --provider opencode-zen
```

API 키가 준비되면 명시적으로 `--execute`를 추가합니다.

```bash
export CEREBRAS_API_KEY="..."
python scripts/evaluate_llm_provider.py --provider cerebras --execute
```
- Pro와 Flash가 모두 실패하면 기존 `latest.md`와 운영 상태를 유지한 채 GitHub Actions를 실패 처리합니다.

기본 호출 간격:

| 모델 | 최소 간격 |
|------|-----------|
| `gemini-2.5-pro` | 15초 |
| `gemini-2.5-flash` | 8초 |
| `gemini-3-flash-preview` | 8초 |

---

## 로컬 개발 환경 셋업

```bash
# 패키지 설치
pip install -r requirements.txt

# 스크래핑만 테스트
python fetch_mer.py

# 분석만 테스트 (API 키 필요)
export GEMINI_API_KEY="AIza..."
python analyze.py

# 전체 실행
python main.py
```

---

## 주의사항

- 이 리포트는 **참고용**이며 투자 권유가 아닙니다
- 메르 블로그 분석을 AI가 재해석한 것으로, 실제 메르의 견해와 다를 수 있습니다
- 네이버 블로그 구조 변경 시 스크래핑이 일시 중단될 수 있습니다
  - 이 경우 RSS 요약본으로 자동 폴백됩니다
- Gemini 무료 모델이 업데이트되면 `GEMINI_MODEL` 환경변수로 조정하세요
